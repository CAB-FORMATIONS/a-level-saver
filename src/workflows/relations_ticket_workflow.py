"""Relations entreprises ticket workflow.

Separate draft-only workflow for B2B emails. It deliberately does not reuse the
DOC state engine because DOC is candidate/VTC/Uber specific.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from config import settings
from src.agents.relations_response_agent import RelationsResponseAgent
from src.agents.relations_triage_agent import EXTRACTED_FIELDS, MISSING_FIELDS, RelationsTriageAgent
from src.constants.departments import DEPT_RELATIONS_ENTREPRISES_ID
from src.utils.planbot_api_client import PlanBotAPIClient
from src.utils.relations_crm_lookup import RelationsCRMLookup, email_domain, is_internal_domain, normalize_email
from src.utils.relations_response_builder import build_internal_note, build_relations_response
from src.utils.relations_response_validator import (
    extract_dates,
    has_verified_availability,
    has_verified_direct_availability,
    validate_relations_response,
)
from src.utils.relations_session_history import (
    EXACT_AVAILABILITY_OPERATIONS,
    NO_AVAILABILITY_OPERATIONS,
    extract_current_training_facts,
    reconstruct_session_context,
)
from src.utils.text_utils import get_clean_thread_content
from src.zoho_client import ZohoCRMClient, ZohoDeskClient


logger = logging.getLogger(__name__)


NOISE_DOMAINS = {
    "linkedin.com",
    "spotify.com",
    "notify.aircall.io",
    "aircall.io",
    "slack-mail.com",
    "google.com",
    "googlemail.com",
    "zohocrm.com",
    "zoho.com",
    "aprovall.com",
    "marketing.ryanairemail.com",
}

NOISE_EMAIL_PARTS = (
    "no-reply",
    "noreply",
    "donotreply",
    "do-not-reply",
    "nepasrepondre",
    "mailer-daemon",
    "bounce",
    "newsletter",
    "notification",
    "systemgenerated",
)

FRENCH_MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}

NUMBER_WORDS = {
    1: ("un", "une"),
    2: ("deux",),
    3: ("trois",),
    4: ("quatre",),
    5: ("cinq",),
    6: ("six",),
    7: ("sept",),
    8: ("huit",),
    9: ("neuf",),
    10: ("dix",),
}

KNOWN_PLANBOT_CENTRES = (
    "Tremblay",
    "Herblay",
    "Villabe",
    "Venissieux",
    "Bois d'Arcy",
    "Seclin",
    "Roissy",
    "Montreuil",
)

class RelationsTicketWorkflow:
    """Draft-only workflow for the Relations entreprises desk department."""

    def __init__(self):
        self.desk_client = ZohoDeskClient()
        self.crm_client = ZohoCRMClient()
        self.crm_lookup = RelationsCRMLookup(self.crm_client)
        self.triage_agent = RelationsTriageAgent()
        self.response_agent = RelationsResponseAgent()
        self.planbot_client = PlanBotAPIClient()
        self._desk_agents_cache: list[dict[str, Any]] | None = None

    def process_ticket(
        self,
        ticket_id: str,
        auto_create_draft: bool = False,
        auto_update_ticket: bool = False,
        ignore_existing_draft: bool = False,
    ) -> dict[str, Any]:
        """Process one Relations entreprises ticket.

        The workflow never sends emails and never updates CRM records. It only
        creates Zoho Desk drafts when `auto_create_draft=True`.
        """
        result: dict[str, Any] = {
            "success": False,
            "ticket_id": ticket_id,
            "workflow_stage": "START",
            "draft_created": False,
            "triage_result": {},
            "crm_context": {},
            "planbot_result": None,
            "validation": {},
            "errors": [],
        }

        try:
            ticket = self.desk_client.get_ticket(ticket_id)
            department_id = str(ticket.get("departmentId") or "")
            if department_id not in {settings.zoho_desk_relations_department_id, DEPT_RELATIONS_ENTREPRISES_ID}:
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_WRONG_DEPARTMENT",
                    "skip_reason": f"Ticket hors departement Relations entreprises: {department_id}",
                })
                return result
            if self._ticket_is_closed(ticket):
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_TICKET_CLOSED",
                    "skip_reason": "Le ticket est ferme",
                })
                return result

            result["workflow_stage"] = "DRAFT_CHECK"
            if not ignore_existing_draft and self.desk_client.has_existing_draft_strict(ticket_id):
                result.update({
                    "success": True,
                    "workflow_stage": "SKIPPED_DRAFT_EXISTS",
                    "skip_reason": "Un brouillon existe deja pour ce ticket",
                })
                return result

            threads = self.desk_client.get_all_threads_with_full_content(ticket_id)
            customer_thread = self._latest_customer_thread(threads)
            if not customer_thread:
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_NO_EXTERNAL_RECIPIENT",
                    "skip_reason": "Aucun thread entrant avec expediteur externe fiable",
                })
                return result
            message = self._clean_current_message(customer_thread) if customer_thread else ""
            sender_email = self._thread_sender_email(customer_thread)
            subject = ticket.get("subject") or ""
            conversation_entries = self._build_conversation_entries(threads, sender_email)
            conversation = self._render_conversation_context(conversation_entries)
            attachments = self._attachment_context(customer_thread)
            context_snapshot = {
                "latest_thread_id": self._latest_thread_id(threads),
                "customer_thread_id": str(customer_thread.get("id") or ""),
                "department_id": department_id,
            }
            if not context_snapshot["latest_thread_id"]:
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_UNRELIABLE_THREAD_ORDER",
                    "skip_reason": "Horodatage des threads absent ou invalide",
                })
                return result

            result["sender_email"] = sender_email
            result["customer_message"] = message
            result["attachments"] = attachments
            if self._has_external_reply_after(customer_thread, threads, sender_email):
                result.update({
                    "success": True,
                    "workflow_stage": "SKIPPED_ALREADY_REPLIED",
                    "skip_reason": "Une reponse externe plus recente existe deja",
                })
                return result

            if self._is_noise(sender_email, subject, message):
                result.update({
                    "success": True,
                    "workflow_stage": "SKIPPED_NOISE",
                    "skip_reason": "Bruit/no-reply/notification detecte",
                })
                return result

            if not sender_email or is_internal_domain(email_domain(sender_email)):
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_NO_EXTERNAL_RECIPIENT",
                    "skip_reason": "Aucun destinataire externe fiable",
                })
                if auto_update_ticket:
                    self._add_internal_note(ticket_id, "[REL_META] Workflow stoppe: aucun destinataire externe fiable.")
                return result

            result["workflow_stage"] = "CRM_LOOKUP"
            crm_context = self.crm_lookup.lookup_sender(sender_email)
            result["crm_context"] = crm_context

            result["workflow_stage"] = "TRIAGE"
            triage = self.triage_agent.process({
                "subject": subject,
                "message": message,
                "email": sender_email,
                "crm_context": crm_context,
                "conversation": conversation,
                "attachments": attachments,
            })
            session_context = reconstruct_session_context(
                conversation_entries,
                str(customer_thread.get("id") or ""),
                KNOWN_PLANBOT_CENTRES,
            )
            result["session_context"] = session_context
            self._apply_session_context(triage, session_context)
            validation_source = self._build_validation_source(message, conversation, session_context)
            if not triage.get("history_context_applied"):
                self._prepare_planbot_search_context(triage, crm_context, message, conversation)
            self._apply_training_defaults(triage)
            history_source = "\n".join(str(entry.get("text") or "") for entry in conversation_entries)
            planbot_source = "\n".join(filter(None, [message, history_source, crm_context.get("account_name") or ""]))
            self._sanitize_extracted_facts(
                triage,
                planbot_source,
                date_source_text=validation_source,
            )
            self._enforce_planbot_missing_fields(triage, has_previous_cab="[CAB]" in conversation)
            result["triage_result"] = triage

            if triage.get("action") in {"IGNORE_NOISE", "ROUTE_HUMAN", "ROUTE_COMPTA"}:
                result.update({
                    "success": True,
                    "workflow_stage": f"STOPPED_{triage.get('action')}",
                    "skip_reason": triage.get("reason"),
                })
                if auto_update_ticket:
                    self._add_internal_note(
                        ticket_id,
                        f"[REL_META] Workflow stoppe: {triage.get('action')} | {triage.get('intent')} | {triage.get('reason')}",
                    )
                return result

            result["workflow_stage"] = "ACCOUNT_ASSIGNMENT"
            assignment = self._resolve_account_manager(crm_context, ticket)
            result["assignment"] = assignment
            if not assignment.get("ready"):
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_ACCOUNT_MANAGER_UNRESOLVED",
                    "skip_reason": assignment.get("reason") or "Gestionnaire du compte CRM introuvable",
                })
                if auto_update_ticket:
                    self._add_internal_note(
                        ticket_id,
                        f"[REL_META] Brouillon bloque: {result['skip_reason']}",
                    )
                return result

            result["workflow_stage"] = "PLANBOT"
            planbot_result = None
            planbot_action = self._select_planbot_action(triage, planbot_source)
            result["planbot_action"] = planbot_action
            if planbot_action:
                payload = self._build_planbot_payload(triage, action=planbot_action)
                planbot_result = self.planbot_client.check_availability(payload, action=planbot_action)
                result["planbot_payload"] = payload
                if (
                    planbot_action == "prevision_planif"
                    and str((planbot_result or {}).get("status") or "").lower() in {"error", "skipped"}
                ):
                    legacy_payload = self._build_planbot_payload(triage, action="check_availability")
                    planbot_result = self.planbot_client.check_availability(
                        legacy_payload,
                        action="check_availability",
                    )
                    result["planbot_legacy_payload"] = legacy_payload
                if (
                    planbot_action in {"prevision_planif", "check_availability"}
                    and str((planbot_result or {}).get("status") or "").lower() == "ok"
                    and not has_verified_direct_availability(planbot_result)
                ):
                    direct_result = planbot_result
                    alternatives_payload = self._build_exact_alternative_dates_payload(triage)
                    alternatives_result = self.planbot_client.check_availability(
                        alternatives_payload,
                        action="search_alternative_dates",
                    )
                    result["planbot_alternative_dates_payload"] = alternatives_payload
                    centres_result = None
                    if not self._planbot_has_available_options(alternatives_result):
                        centres_payload = self._build_exact_alternative_centres_payload(triage)
                        centres_result = self.planbot_client.check_availability(
                            centres_payload,
                            action="search_alternative_centres",
                        )
                        result["planbot_alternative_centres_payload"] = centres_payload
                    planbot_result = {
                        "status": "ok",
                        "mode": "exact_with_alternatives",
                        "direct": direct_result,
                        "alternative_dates": alternatives_result,
                        "alternative_centres": centres_result,
                    }
                if (
                    planbot_action == "search_alternative_dates"
                    and str((planbot_result or {}).get("status") or "").lower() not in {"error", "skipped"}
                    and not self._planbot_has_available_options(planbot_result)
                ):
                    centres_payload = self._build_planbot_alternative_centres_payload(triage)
                    centres_result = self.planbot_client.check_availability(
                        centres_payload,
                        action="search_alternative_centres",
                    )
                    result["planbot_alternative_centres_payload"] = centres_payload
                    planbot_result = {
                        "status": "ok",
                        "mode": "next_sessions",
                        "same_centre": planbot_result,
                        "alternative_centres": centres_result,
                    }
            result["planbot_result"] = planbot_result

            result["workflow_stage"] = "RESPONSE"
            fallback_response = build_relations_response(triage, crm_context, planbot_result, attachments)
            response_request = {
                "subject": subject,
                "message": message,
                "conversation": conversation,
                "triage": triage,
                "crm_context": crm_context,
                "attachments": attachments,
                "session_context": session_context,
                "fallback_response": fallback_response,
            }
            response_generation = self.response_agent.process(response_request)
            response_html = response_generation.get("response_html") or fallback_response
            planbot_status = str((planbot_result or {}).get("status") or "").lower()
            exact_availability_unverified = (
                planbot_action in {"prevision_planif", "check_availability"}
                and not has_verified_direct_availability(planbot_result)
            )
            if exact_availability_unverified:
                if has_verified_availability(planbot_result):
                    fallback_reason = "planbot_alternatives_only"
                    human_reason = (
                        "La session demandee est indisponible; des alternatives PlanBot sont proposees."
                    )
                elif planbot_status in {"error", "skipped"}:
                    fallback_reason = f"planbot_{planbot_status}"
                    human_reason = (
                        "La disponibilite PlanBot n'a pas pu etre verifiee; controle manuel requis avant reponse."
                    )
                else:
                    fallback_reason = "planbot_no_direct_availability"
                    human_reason = (
                        "PlanBot n'a identifie aucune disponibilite complete sur la session demandee."
                    )
                response_generation = {
                    **response_generation,
                    "response_html": fallback_response,
                    "used_ai": False,
                    "requires_human_action": True,
                    "human_action_reason": human_reason,
                    "fallback_reason": fallback_reason,
                }
                response_html = fallback_response
            validation = validate_relations_response(
                response_html,
                triage,
                planbot_result,
                source_response_html=fallback_response,
                allowed_source_text=validation_source,
            )
            attempts = 1

            if (
                not validation.get("valid")
                and response_html != fallback_response
                and response_generation.get("used_ai")
            ):
                retry_generation = self.response_agent.process({
                    **response_request,
                    "validation_errors": validation.get("errors") or [],
                })
                retry_html = retry_generation.get("response_html") or fallback_response
                retry_validation = validate_relations_response(
                    retry_html,
                    triage,
                    planbot_result,
                    source_response_html=fallback_response,
                    allowed_source_text=validation_source,
                )
                response_generation = retry_generation
                response_html = retry_html
                validation = retry_validation
                attempts = 2

            if not validation.get("valid") and response_html != fallback_response:
                fallback_validation = validate_relations_response(
                    fallback_response,
                    triage,
                    planbot_result,
                    source_response_html=fallback_response,
                    allowed_source_text=validation_source,
                )
                if fallback_validation.get("valid"):
                    fallback_validation.setdefault("warnings", []).append(
                        "Reponse IA rejetee; brouillon deterministe utilise"
                    )
                    response_generation = {
                        **response_generation,
                        "used_ai": False,
                        "fallback_reason": "; ".join(validation.get("errors") or []),
                    }
                    response_html = fallback_response
                    validation = fallback_validation

            response_generation["attempts"] = attempts

            if response_generation.get("requires_human_action"):
                reason = response_generation.get("human_action_reason") or "Verification humaine requise"
                validation.setdefault("warnings", []).append(str(reason))

            result["response_generation"] = response_generation
            result["validation"] = validation
            result["draft_content"] = response_html

            if not validation.get("valid"):
                result.update({
                    "success": True,
                    "workflow_stage": "STOPPED_VALIDATION",
                    "skip_reason": "Validation B2B echouee",
                })
                if auto_update_ticket:
                    note = build_internal_note(ticket_id, triage, crm_context, planbot_result, validation)
                    self._add_internal_note(ticket_id, note)
                return result

            result["workflow_stage"] = "DRAFT_DELIVERY"
            if auto_create_draft:
                if not self._delivery_context_is_current(ticket_id, context_snapshot, sender_email):
                    result.update({
                        "success": True,
                        "workflow_stage": "SKIPPED_STALE_CONTEXT",
                        "skip_reason": "Le ticket a change pendant la generation du brouillon",
                    })
                    return result
                fresh_crm_context = self.crm_lookup.lookup_sender(sender_email)
                fresh_assignment = self._resolve_account_manager(
                    fresh_crm_context,
                    ticket,
                    force_refresh=True,
                )
                if not fresh_assignment.get("ready"):
                    result["assignment"] = fresh_assignment
                    result.update({
                        "success": True,
                        "workflow_stage": "STOPPED_ACCOUNT_MANAGER_UNRESOLVED",
                        "skip_reason": fresh_assignment.get("reason") or "Gestionnaire CRM non resolu avant affectation",
                    })
                    return result
                assignment_result = self._assign_ticket_to_account_manager(ticket_id, ticket, fresh_assignment)
                result["assignment"] = assignment_result
                if not assignment_result.get("assigned"):
                    result.update({
                        "success": True,
                        "workflow_stage": "STOPPED_ACCOUNT_ASSIGNMENT_FAILED",
                        "skip_reason": assignment_result.get("reason") or "Echec de l'affectation du ticket",
                    })
                    return result
                if not self._delivery_context_is_current(
                    ticket_id,
                    context_snapshot,
                    sender_email,
                    expected_assignee_id=assignment_result.get("desk_agent_id") or "",
                ):
                    result.update({
                        "success": True,
                        "workflow_stage": "SKIPPED_STALE_CONTEXT",
                        "skip_reason": "Le ticket a change pendant son affectation",
                    })
                    return result
                from_email = settings.zoho_desk_email_relations or settings.zoho_desk_email_default
                self.desk_client.create_ticket_reply_draft(
                    ticket_id=ticket_id,
                    content=response_html,
                    content_type="html",
                    from_email=from_email,
                    to_email=sender_email,
                )
                result["draft_created"] = True
                self._mark_brouillon_auto(ticket_id)

            if auto_update_ticket:
                note = build_internal_note(ticket_id, triage, crm_context, planbot_result, validation)
                self._add_internal_note(ticket_id, note)

            result["success"] = True
            return result
        except Exception as exc:
            logger.error("Relations workflow failed for %s: %s", ticket_id, exc, exc_info=True)
            result["errors"].append(str(exc))
            result["error"] = str(exc)
            return result

    def _latest_customer_message(self, threads: list[dict[str, Any]]) -> str:
        thread = self._latest_customer_thread(threads)
        return self._clean_current_message(thread) if thread else ""

    def _latest_customer_thread(self, threads: list[dict[str, Any]]) -> dict[str, Any] | None:
        candidates = [thread for thread in threads if self._is_external_inbound(thread)]
        if not candidates or any(self._thread_timestamp(thread) <= 0 for thread in candidates):
            return None
        return max(candidates, key=self._thread_timestamp)

    def _clean_current_message(self, thread: dict[str, Any] | None) -> str:
        if not thread:
            return ""
        content = get_clean_thread_content(thread).strip()
        if not content or content == "N/A":
            return ""

        reply_patterns = (
            r"\n\s*-{2,}\s*le\s+[^\n]{0,500}(?:e|é)crit\s*-{2,}",
            r"\n\s*le\s+(?:\w+\.?\s+)?\d{1,2}(?:[/.-]\d{1,2}[/.-]\d{2,4}|\s+\w+\.?\s+\d{4})\b.{0,500}?(?:a|à)\s+(?:e|é)crit\s*:",
            r"\n\s*le\s+[^\n]{0,500}(?:a|à)\s+(?:e|é)crit\s*:",
            r"\n\s*on\s+[^\n]{0,500}wrote\s*:",
            r"\n\s*-{3,}\s*(?:original message|message d.origine)",
            r"\n\s*(?:de|from)\s*:.*?\n\s*(?:envoy[eé]|sent)\s*:",
        )
        cut_at = len(content)
        for pattern in reply_patterns:
            match = re.search(pattern, content, flags=re.IGNORECASE | re.DOTALL)
            if match:
                cut_at = min(cut_at, match.start())
        return content[:cut_at].strip()

    def _is_external_inbound(self, thread: dict[str, Any]) -> bool:
        if str(thread.get("status") or "").upper() == "DRAFT":
            return False
        if str(thread.get("direction") or "").lower() != "in":
            return False
        sender = self._thread_sender_email(thread)
        return bool(sender) and not is_internal_domain(email_domain(sender))

    def _thread_sender_email(self, thread: dict[str, Any]) -> str:
        author = thread.get("author") if isinstance(thread.get("author"), dict) else {}
        return normalize_email(thread.get("fromEmailAddress") or author.get("email") or "")

    @staticmethod
    def _thread_timestamp(thread: dict[str, Any]) -> float:
        value = str(thread.get("createdTime") or thread.get("created_time") or "").strip()
        if not value:
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0

    def _latest_thread_id(self, threads: list[dict[str, Any]]) -> str:
        candidates = [
            thread for thread in threads
            if str(thread.get("status") or "").upper() != "DRAFT"
        ]
        if not candidates or any(self._thread_timestamp(thread) <= 0 for thread in candidates):
            return ""
        latest = max(candidates, key=self._thread_timestamp)
        return str(latest.get("id") or "")

    def _has_external_reply_after(
        self,
        customer_thread: dict[str, Any],
        threads: list[dict[str, Any]],
        expected_recipient: str,
    ) -> bool:
        customer_time = self._thread_timestamp(customer_thread)
        if not customer_time:
            return False
        for thread in threads:
            if str(thread.get("status") or "").upper() == "DRAFT":
                continue
            if str(thread.get("direction") or "").lower() != "out":
                continue
            if not self._has_external_recipient(thread, expected_recipient):
                continue
            reply_time = self._thread_timestamp(thread)
            if reply_time <= 0 or reply_time > customer_time:
                return True
        return False

    @staticmethod
    def _has_external_recipient(thread: dict[str, Any], expected_email: str = "") -> bool:
        raw_recipients = " ".join(str(thread.get(key) or "") for key in ("to", "toEmailAddress", "cc"))
        recipients = [
            normalize_email(email)
            for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", raw_recipients)
        ]
        if expected_email:
            return normalize_email(expected_email) in recipients
        return any(not is_internal_domain(email_domain(email)) for email in recipients)

    def _build_conversation_entries(
        self,
        threads: list[dict[str, Any]],
        expected_external_email: str = "",
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        expected_email = normalize_email(expected_external_email)
        for thread in threads:
            if str(thread.get("status") or "").upper() == "DRAFT":
                continue
            if self._is_external_inbound(thread):
                role = "CLIENT"
                if expected_email and self._thread_sender_email(thread) != expected_email:
                    continue
            elif (
                str(thread.get("direction") or "").lower() == "out"
                and self._has_external_recipient(thread, expected_email)
            ):
                role = "CAB"
            else:
                continue
            content = self._clean_current_message(thread)
            timestamp = self._thread_timestamp(thread)
            if content and timestamp > 0:
                entries.append({
                    "id": str(thread.get("id") or ""),
                    "timestamp": timestamp,
                    "created_time": str(thread.get("createdTime") or thread.get("created_time") or ""),
                    "role": role,
                    "text": content,
                })
        return sorted(entries, key=lambda item: (item["timestamp"], item["id"]))

    @staticmethod
    def _render_conversation_context(
        entries: list[dict[str, Any]],
        max_entries: int = 10,
        max_chars: int = 7600,
    ) -> str:
        selected: list[str] = []
        used = 0
        for entry in reversed(entries[-max_entries:]):
            block = f"[{entry.get('role')}]\n{str(entry.get('text') or '')[:1600]}"
            block_size = len(block) + (7 if selected else 0)
            if selected and used + block_size > max_chars:
                break
            selected.append(block)
            used += block_size
        return "\n\n---\n\n".join(reversed(selected))

    def _build_conversation_context(self, threads: list[dict[str, Any]]) -> str:
        return self._render_conversation_context(self._build_conversation_entries(threads))

    def _resolve_account_manager(
        self,
        crm_context: dict[str, Any],
        ticket: dict[str, Any],
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        owner = crm_context.get("account_owner")
        result: dict[str, Any] = {
            "ready": False,
            "assigned": False,
            "changed": False,
            "account_id": crm_context.get("account_id") or "",
            "account_name": crm_context.get("account_name") or "",
            "crm_owner": owner,
            "desk_agent_id": "",
            "desk_agent_name": "",
            "desk_agent_email": "",
            "current_assignee_id": self._ticket_assignee_id(ticket),
            "reason": "",
        }
        if crm_context.get("classification") != "client_crm":
            result["reason"] = crm_context.get("lookup_error") or "Expediteur absent des Contacts CRM"
            return result
        if not crm_context.get("account_id") or not crm_context.get("account"):
            result["reason"] = "Aucun compte CRM associe au contact"
            return result
        if not isinstance(owner, dict):
            result["reason"] = "Le compte CRM n'a pas de gestionnaire exploitable"
            return result

        owner_email = normalize_email(owner.get("email") or "")
        if not owner_email:
            result["reason"] = "Email du gestionnaire CRM absent ou invalide"
            return result

        try:
            agents = self._get_desk_agents(force_refresh=force_refresh)
        except Exception as exc:
            logger.warning("Unable to list Desk agents for account assignment: %s", exc)
            result["reason"] = f"Liste des agents Zoho Desk indisponible: {exc}"
            return result

        email_matches = [
            agent for agent in agents
            if normalize_email(agent.get("emailId") or agent.get("email") or "") == owner_email
        ]
        active_matches = [
            agent for agent in email_matches
            if str(agent.get("status") or "").upper() == "ACTIVE"
            and agent.get("isConfirmed") is not False
        ]
        department_ids = {settings.zoho_desk_relations_department_id, DEPT_RELATIONS_ENTREPRISES_ID}
        eligible_matches = [
            agent for agent in active_matches
            if department_ids.intersection(str(value) for value in agent.get("associatedDepartmentIds") or [])
        ]

        if not email_matches:
            result["reason"] = f"Aucun agent Desk ne correspond a {owner_email}"
            return result
        if not active_matches:
            result["reason"] = f"L'agent Desk {owner_email} est inactif"
            return result
        if not eligible_matches:
            result["reason"] = f"L'agent Desk {owner_email} n'est pas rattache au departement Relations entreprises"
            return result
        if len(eligible_matches) != 1:
            result["reason"] = f"Plusieurs agents Desk actifs correspondent a {owner_email}"
            return result

        agent = eligible_matches[0]
        result.update({
            "ready": True,
            "desk_agent_id": str(agent.get("id") or ""),
            "desk_agent_name": str(agent.get("name") or "").strip(),
            "desk_agent_email": owner_email,
            "would_change": self._ticket_assignee_id(ticket) != str(agent.get("id") or ""),
        })
        return result

    def _get_desk_agents(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        cached = getattr(self, "_desk_agents_cache", None)
        if cached is None or force_refresh:
            cached = self.desk_client.list_agents()
            self._desk_agents_cache = cached
        return cached

    def _assign_ticket_to_account_manager(
        self,
        ticket_id: str,
        ticket: dict[str, Any],
        assignment: dict[str, Any],
    ) -> dict[str, Any]:
        result = dict(assignment)
        target_id = str(assignment.get("desk_agent_id") or "")
        current_id = self._ticket_assignee_id(ticket)
        if not assignment.get("ready") or not target_id:
            result["reason"] = assignment.get("reason") or "Gestionnaire de compte non resolu"
            return result
        if current_id == target_id:
            result.update({"assigned": True, "changed": False, "reason": "Ticket deja affecte au gestionnaire du compte"})
            return result

        try:
            updated = self.desk_client.update_ticket(ticket_id, {"assigneeId": target_id})
            verified_id = self._ticket_assignee_id(updated if isinstance(updated, dict) else {})
            if not verified_id:
                verified_id = self._ticket_assignee_id(self.desk_client.get_ticket(ticket_id))
            if verified_id != target_id:
                result["reason"] = f"Affectation non confirmee par Zoho Desk: {verified_id or 'aucun agent'}"
                return result
            result.update({"assigned": True, "changed": True, "reason": "Ticket affecte au gestionnaire du compte CRM"})
            return result
        except Exception as exc:
            logger.error("Unable to assign Relations ticket %s to %s: %s", ticket_id, target_id, exc)
            result["reason"] = f"Erreur d'affectation Zoho Desk: {exc}"
            return result

    @staticmethod
    def _ticket_assignee_id(ticket: dict[str, Any]) -> str:
        assignee = ticket.get("assignee") if isinstance(ticket.get("assignee"), dict) else {}
        return str(ticket.get("assigneeId") or assignee.get("id") or "")

    def _delivery_context_is_current(
        self,
        ticket_id: str,
        snapshot: dict[str, str],
        sender_email: str,
        expected_assignee_id: str = "",
    ) -> bool:
        try:
            ticket = self.desk_client.get_ticket(ticket_id)
            department_id = str(ticket.get("departmentId") or "")
            if department_id != snapshot.get("department_id") or self._ticket_is_closed(ticket):
                return False
            if expected_assignee_id and self._ticket_assignee_id(ticket) != str(expected_assignee_id):
                return False
            if self.desk_client.has_existing_draft_strict(ticket_id):
                return False

            threads = self.desk_client.list_ticket_threads(ticket_id)
            if any(str(thread.get("status") or "").upper() == "DRAFT" for thread in threads):
                return False
            if self._latest_thread_id(threads) != snapshot.get("latest_thread_id"):
                return False
            customer_thread = self._latest_customer_thread(threads)
            if not customer_thread or str(customer_thread.get("id") or "") != snapshot.get("customer_thread_id"):
                return False
            if self._thread_sender_email(customer_thread) != normalize_email(sender_email):
                return False
            return not self._has_external_reply_after(customer_thread, threads, sender_email)
        except Exception as exc:
            logger.warning("Unable to revalidate Relations ticket %s: %s", ticket_id, exc)
            return False

    @staticmethod
    def _ticket_is_closed(ticket: dict[str, Any]) -> bool:
        status = str(ticket.get("statusType") or ticket.get("status") or "").strip().lower()
        return status in {"closed", "ferme", "fermé"}

    @staticmethod
    def _attachment_context(thread: dict[str, Any] | None) -> dict[str, Any]:
        if not thread:
            return {"has_attachments": False, "names": []}
        try:
            attachment_count = int(thread.get("attachmentCount") or 0)
        except (TypeError, ValueError):
            attachment_count = 0
        raw_attachments = thread.get("attachments") or []
        names = []
        for attachment in raw_attachments:
            if not isinstance(attachment, dict):
                continue
            name = attachment.get("name") or attachment.get("fileName")
            if name:
                names.append(str(name))
        return {
            "has_attachments": bool(thread.get("hasAttach") or attachment_count > 0 or raw_attachments),
            "names": names[:10],
        }

    def _is_noise(self, email: str, subject: str, message: str) -> bool:
        domain = email_domain(email)
        text = f"{subject}\n{message}".lower()
        return (
            self._email_is_noise(email)
            or domain in NOISE_DOMAINS
            or any(part in text for part in [
                "aircall incident",
                "message vocal",
                "linkedin",
                "spotify",
                "réponse automatique",
                "reponse automatique",
                "absence du bureau",
                "out of office",
                "votre avis nous interesse",
                "votre avis nous intéresse",
            ])
        )

    def _email_is_noise(self, email: str) -> bool:
        lower = (email or "").lower()
        return any(part in lower for part in NOISE_EMAIL_PARTS)

    @staticmethod
    def _apply_session_context(triage: dict[str, Any], session_context: dict[str, Any]) -> None:
        operation = str(session_context.get("operation") or "unknown")
        if operation == "availability_check" and not session_context.get("candidate_count"):
            operation = "new_search"
        triage["session_operation"] = operation
        triage["session_context_status"] = session_context.get("status")
        triage["session_context"] = {
            "status": session_context.get("status"),
            "reason": session_context.get("reason"),
            "facts": session_context.get("facts") or {},
        }

        if operation not in EXACT_AVAILABILITY_OPERATIONS:
            if operation in {"absence", "cancel", "candidate_removal"}:
                triage["intent"] = "ANNULATION_REPORT_ABSENCE"
            return

        triage["intent"] = "DEMANDE_DISPONIBILITE_SESSION"
        triage["request_mode"] = "follow_up"
        triage["history_context_applied"] = True
        facts = session_context.get("facts") or {}
        extracted = triage.get("extracted")
        if not isinstance(extracted, dict):
            extracted = {}

        empty_values = {
            "formation_type": "",
            "centre": "",
            "start_date": None,
            "end_date": None,
            "nb_candidates": None,
            "categories": [],
            "nb_categories": None,
            "type_ir": "",
            "financement": "B2B",
            "nombre_jours_souhaites": None,
        }
        for field, empty_value in empty_values.items():
            extracted[field] = facts.get(field, empty_value)
        if extracted.get("categories"):
            extracted["nb_categories"] = len(extracted["categories"])
        triage["extracted"] = extracted
        triage["missing_fields"] = list(session_context.get("missing_fields") or [])
        triage["history_verified_fields"] = list(session_context.get("verified_fields") or [])
        triage["defaulted_fields"] = []

    def _prepare_planbot_search_context(
        self,
        triage: dict[str, Any],
        crm_context: dict[str, Any],
        message: str,
        conversation: str,
    ) -> None:
        if triage.get("intent") not in {
            "DEMANDE_DEVIS_FORMATION",
            "DEMANDE_DISPONIBILITE_SESSION",
            "INSCRIPTION_CANDIDATS",
        }:
            return
        normalized_message = self._normalize_search_text(message)
        if re.search(
            r"\b(?:je\s+)?reviens?\b.{0,50}\bdate\b|\bdate\b.{0,50}\b(?:demain|plus tard)\b",
            normalized_message,
        ) and not re.search(r"\breviens?\s+(?:a|sur)\s+la\s+date\b", normalized_message):
            triage["date_will_follow"] = True
        extracted = triage.get("extracted")
        if not isinstance(extracted, dict):
            extracted = {}
            triage["extracted"] = extracted

        current_training_facts = extract_current_training_facts(message)
        current_formation = str(current_training_facts.get("formation_type") or "")
        if current_training_facts.get("ambiguous_fields"):
            triage["ambiguous_fields"] = list(dict.fromkeys([
                *(triage.get("ambiguous_fields") or []),
                *current_training_facts["ambiguous_fields"],
            ]))
            for field in current_training_facts["ambiguous_fields"]:
                if field == "formation_type":
                    extracted["formation_type"] = ""
                elif field == "type_ir":
                    extracted["type_ir"] = ""
                elif field == "nb_candidates":
                    extracted["nb_candidates"] = None
        historical_formation = RelationsTriageAgent._extract_formation(conversation)
        deterministic_formation = current_formation
        if current_formation:
            extracted["categories"] = []
            extracted["nb_categories"] = None
            extracted["type_ir"] = ""
            extracted["nb_candidates"] = None
        if (
            not deterministic_formation
            and "formation_type" not in (current_training_facts.get("ambiguous_fields") or [])
            and triage.get("request_mode") == "follow_up"
        ):
            deterministic_formation = historical_formation
        if deterministic_formation:
            extracted["formation_type"] = deterministic_formation
        deterministic_categories = current_training_facts.get("categories") or []
        if not deterministic_categories and not current_formation and triage.get("request_mode") == "follow_up":
            deterministic_categories = RelationsTriageAgent._extract_categories(conversation)
        if deterministic_categories:
            extracted["categories"] = deterministic_categories
            extracted["nb_categories"] = len(deterministic_categories)
        elif current_formation and not self._is_caces_request(current_formation):
            extracted["categories"] = []
            extracted["nb_categories"] = None

        account_name = crm_context.get("account_name") or ""
        excluded_centres = self._excluded_planbot_centres(message)
        existing_centre = str(extracted.get("centre") or "")
        centre_evidence = self._normalize_location(f"{message}\n{account_name}")
        if existing_centre:
            existing_pattern = rf"(?<![a-z0-9]){re.escape(self._normalize_location(existing_centre))}(?![a-z0-9])"
            existing_excluded = any(
                self._normalize_location(existing_centre) == self._normalize_location(centre)
                for centre in excluded_centres
            )
            if existing_excluded or not re.search(existing_pattern, centre_evidence):
                extracted["centre"] = ""
        message_centres = self._known_planbot_centres(message, excluded_centres)
        message_centre = message_centres[0] if len(message_centres) == 1 else ""
        account_centre = self._infer_known_planbot_centre(account_name, excluded_centres)
        if message_centre:
            extracted["centre"] = message_centre
        elif len(message_centres) > 1:
            extracted["centre"] = ""
            triage["ambiguous_fields"] = list(dict.fromkeys([
                *(triage.get("ambiguous_fields") or []),
                "centre",
            ]))
        elif not extracted.get("centre") and account_centre:
            extracted["centre"] = account_centre

        current_candidate_count = current_training_facts.get("nb_candidates")
        if (
            not current_candidate_count
            and not current_formation
            and triage.get("request_mode") == "follow_up"
        ):
            current_candidate_count = self._extract_candidate_count(conversation)
        if current_candidate_count:
            extracted["nb_candidates"] = current_candidate_count

        current_type_ir = str(current_training_facts.get("type_ir") or "")
        if current_type_ir:
            extracted["type_ir"] = current_type_ir
        elif (
            not current_formation
            and triage.get("request_mode") == "follow_up"
            and extracted.get("type_ir") not in {"initial", "recyclage"}
        ):
            historical_type = extract_current_training_facts(conversation).get("type_ir")
            if historical_type:
                extracted["type_ir"] = historical_type
        elif current_formation and triage.get("request_mode") == "new_request":
            extracted["type_ir"] = ""

        explicit_dates, _ = extract_dates(message)
        if "disponibil" not in normalized_message:
            return
        if explicit_dates or self._date_mentions_without_year(message) or self._has_vague_date_preference(message):
            return
        triage["planbot_search_mode"] = "next_sessions"

    def _apply_training_defaults(self, triage: dict[str, Any]) -> None:
        if triage.get("history_context_applied"):
            return
        if triage.get("intent") not in {
            "DEMANDE_DEVIS_FORMATION",
            "DEMANDE_DISPONIBILITE_SESSION",
            "INSCRIPTION_CANDIDATS",
        }:
            return
        if triage.get("request_mode") not in {"new_request", "follow_up", "other"}:
            return
        extracted = triage.get("extracted")
        if not isinstance(extracted, dict):
            extracted = {}
            triage["extracted"] = extracted
        defaulted = list(triage.get("defaulted_fields") or [])
        if not extracted.get("nb_candidates"):
            extracted["nb_candidates"] = 1
            defaulted.append("nb_candidates")
        if self._is_caces_request(extracted.get("formation_type")) and extracted.get("type_ir") not in {"initial", "recyclage"}:
            extracted["type_ir"] = "initial"
            defaulted.append("type_ir")
        triage["defaulted_fields"] = list(dict.fromkeys(defaulted))

    def _infer_known_planbot_centre(self, value: str, excluded: set[str] | None = None) -> str:
        matches = self._known_planbot_centres(value, excluded)
        return matches[0] if len(matches) == 1 else ""

    def _known_planbot_centres(self, value: str, excluded: set[str] | None = None) -> list[str]:
        normalized = self._normalize_location(value)
        excluded = excluded or set()
        return [
            centre for centre in KNOWN_PLANBOT_CENTRES
            if centre not in excluded
            if re.search(
                rf"(?<![a-z0-9]){re.escape(self._normalize_location(centre))}(?![a-z0-9])",
                normalized,
            )
        ]

    def _extract_candidate_count(self, value: str) -> int | None:
        normalized = self._normalize_search_text(value)
        word_to_number = {
            word: number
            for number, words in NUMBER_WORDS.items()
            for word in words
        }
        number_pattern = "|".join(re.escape(word) for word in word_to_number)
        match = re.search(
            rf"\b(?P<number>\d{{1,3}}|{number_pattern})\s+"
            r"(?:candidat|candidats|stagiaire|stagiaires|participant|participants|"
            r"personne|personnes|interimaire|interimaires|salarie|salaries|collaborateur|collaborateurs)\b",
            normalized,
        )
        if not match:
            return None
        raw = match.group("number")
        count = int(raw) if raw.isdigit() else word_to_number.get(raw, 0)
        return count if 0 < count <= 500 else None

    def _excluded_planbot_centres(self, value: str) -> set[str]:
        normalized = self._normalize_location(value)
        excluded = set()
        for centre in KNOWN_PLANBOT_CENTRES:
            centre_value = self._normalize_location(centre)
            if re.search(
                rf"\b(?:ne\s+[a-z0-9 ]{{0,30}}\s+pas|pas|sauf|hors|exclure|exclu)\s+"
                rf"(?:de\s+|sur\s+|a\s+|au\s+centre\s+de\s+|le\s+centre\s+(?:de\s+)?)?"
                rf"{re.escape(centre_value)}\b",
                normalized,
            ):
                excluded.add(centre)
        return excluded

    def _has_vague_date_preference(self, value: str) -> bool:
        normalized = self._normalize_search_text(value)
        month_names = "|".join(FRENCH_MONTHS)
        return bool(
            re.search(rf"\b(?:{month_names})\b", normalized)
            or re.search(r"\b(?:semaine|mois)\s+(?:prochaine?|suivante?)\b", normalized)
            or re.search(
                r"\bdans\s+(?:\d+|un|une|deux|trois|quatre|cinq|six|sept|huit)\s+"
                r"(?:jour|jours|semaine|semaines|mois)\b",
                normalized,
            )
            or re.search(r"\ba partir de\b|\bdes que possible\b|\bdemain\b|\bplus tard\b", normalized)
        )

    @staticmethod
    def _normalize_location(value: str) -> str:
        normalized = unicodedata.normalize("NFD", value or "")
        without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        return re.sub(r"[^a-z0-9]+", " ", without_accents.lower()).strip()

    def _build_validation_source(
        self,
        message: str,
        conversation: str,
        session_context: dict[str, Any] | None = None,
    ) -> str:
        relevant_dates = set()
        if session_context is None:
            mentions = self._date_mentions_without_year(message)
            conversation_dates, _ = extract_dates(conversation)
            for value in conversation_dates:
                parsed = datetime.strptime(value, "%Y-%m-%d")
                if (parsed.day, parsed.month) in mentions:
                    relevant_dates.add(value)
        elif session_context.get("status") == "resolved":
            facts = session_context.get("facts") or {}
            for field in ("start_date", "end_date"):
                value = str(facts.get(field) or "")
                parsed_dates, invalid = extract_dates(value)
                if not invalid:
                    relevant_dates.update(parsed_dates)
        if not relevant_dates:
            return message
        return "\n".join([message, *(f"DATE_REPRISE: {value}" for value in sorted(relevant_dates))])

    def _date_mentions_without_year(self, value: str) -> set[tuple[int, int]]:
        normalized = self._normalize_search_text(value)
        mentions: set[tuple[int, int]] = set()
        for day, month in re.findall(r"\b(\d{1,2})[/-](\d{1,2})(?![/-]\d)", normalized):
            try:
                datetime(2000, int(month), int(day))
                mentions.add((int(day), int(month)))
            except ValueError:
                continue
        month_names = "|".join(FRENCH_MONTHS)
        range_pattern = rf"\b(\d{{1,2}})\s+(?:au|a)\s+(\d{{1,2}})\s+({month_names})\b(?!\s+\d{{4}})"
        for start_day, end_day, month_name in re.findall(range_pattern, normalized):
            month = FRENCH_MONTHS[month_name]
            for day in (start_day, end_day):
                try:
                    datetime(2000, month, int(day))
                    mentions.add((int(day), month))
                except ValueError:
                    continue
        single_pattern = rf"\b(\d{{1,2}})(?:er)?\s+({month_names})\b(?!\s+\d{{4}})"
        for day, month_name in re.findall(single_pattern, normalized):
            month = FRENCH_MONTHS[month_name]
            try:
                datetime(2000, month, int(day))
                mentions.add((int(day), month))
            except ValueError:
                continue
        return mentions

    def _sanitize_extracted_facts(
        self,
        triage: dict[str, Any],
        source_text: str,
        date_source_text: str | None = None,
    ) -> None:
        extracted = triage.get("extracted")
        if not isinstance(extracted, dict):
            triage["extracted"] = {}
            return

        unknown_fields = [field for field in extracted if field not in EXTRACTED_FIELDS]
        for field in unknown_fields:
            extracted.pop(field, None)
        extracted["financement"] = "B2B"

        normalized_source = self._normalize_search_text(source_text)
        source_dates, _ = extract_dates(date_source_text if date_source_text is not None else source_text)
        removed: list[str] = []
        defaulted_fields = set(triage.get("defaulted_fields") or [])
        history_verified_fields = set(triage.get("history_verified_fields") or [])

        formation = str(extracted.get("formation_type") or "").strip()
        if formation and "formation_type" not in history_verified_fields:
            normalized_formation = self._normalize_search_text(formation)
            formation_tokens = [token for token in re.findall(r"[a-z0-9]+", normalized_formation) if len(token) > 1]
            if not formation_tokens or not all(token in normalized_source for token in formation_tokens):
                extracted["formation_type"] = ""
                removed.append("formation_type")

        centre = str(extracted.get("centre") or "").strip()
        if centre and "centre" not in history_verified_fields:
            normalized_centre = self._normalize_location(centre)
            normalized_location_source = self._normalize_location(source_text)
            centre_pattern = rf"(?<![a-z0-9]){re.escape(normalized_centre)}(?![a-z0-9])"
            if not re.search(centre_pattern, normalized_location_source):
                extracted["centre"] = ""
                removed.append("centre")

        for field in ("start_date", "end_date"):
            value = extracted.get(field)
            if not value:
                continue
            if field in history_verified_fields:
                continue
            value_dates, invalid_dates = extract_dates(str(value))
            if invalid_dates or not value_dates or not value_dates.issubset(source_dates):
                extracted[field] = None
                removed.append(field)

        start_dates, _ = extract_dates(str(extracted.get("start_date") or ""))
        end_dates, _ = extract_dates(str(extracted.get("end_date") or ""))
        if start_dates and end_dates and next(iter(start_dates)) > next(iter(end_dates)):
            extracted["start_date"] = None
            extracted["end_date"] = None
            removed.extend(["start_date", "end_date"])

        categories = extracted.get("categories") or []
        if not isinstance(categories, list):
            categories = []
            extracted["categories"] = []
            removed.append("categories")
        if categories and "categories" not in history_verified_fields:
            supported_categories = RelationsTriageAgent._extract_categories(source_text)
            if not all(str(category).upper() in supported_categories for category in categories):
                extracted["categories"] = []
                extracted["nb_categories"] = None
                removed.append("categories")

        candidate_count = extracted.get("nb_candidates")
        if candidate_count is not None:
            try:
                candidate_count_value = int(candidate_count)
            except (TypeError, ValueError):
                candidate_count_value = 0
            if (
                "nb_candidates" not in defaulted_fields
                and "nb_candidates" not in history_verified_fields
                and not self._number_has_source_evidence(
                candidate_count_value,
                normalized_source,
                (
                    "candidat", "candidats", "stagiaire", "stagiaires", "participant", "participants",
                    "personne", "personnes", "interimaire", "interimaires", "salarie", "salaries",
                ),
                )
            ):
                extracted["nb_candidates"] = None
                removed.append("nb_candidates")
            else:
                extracted["nb_candidates"] = candidate_count_value

        category_count = extracted.get("nb_categories")
        if category_count is not None:
            try:
                category_count_value = int(category_count)
            except (TypeError, ValueError):
                category_count_value = 0
            categories = extracted.get("categories") or []
            category_count_supported = bool(categories) and len(categories) == category_count_value
            if not category_count_supported:
                category_count_supported = self._number_has_source_evidence(
                    category_count_value,
                    normalized_source,
                    ("categorie", "categories"),
                )
            if not category_count_supported:
                extracted["nb_categories"] = None
                removed.append("nb_categories")
            else:
                extracted["nb_categories"] = category_count_value

        type_ir = str(extracted.get("type_ir") or "").strip().lower()
        if (
            type_ir
            and "type_ir" not in defaulted_fields
            and "type_ir" not in history_verified_fields
            and type_ir not in normalized_source
        ):
            extracted["type_ir"] = ""
            removed.append("type_ir")

        duration = extracted.get("nombre_jours_souhaites")
        if duration:
            try:
                duration_value = int(duration)
            except (TypeError, ValueError):
                duration_value = 0
            explicit_duration = bool(re.search(
                rf"\b{duration_value}\s*(?:jour|jours|j)\b",
                normalized_source,
            )) if duration_value else False
            derived_duration = False
            start_dates, _ = extract_dates(str(extracted.get("start_date") or ""))
            end_dates, _ = extract_dates(str(extracted.get("end_date") or ""))
            if start_dates and end_dates and duration_value:
                start = datetime.strptime(next(iter(start_dates)), "%Y-%m-%d")
                end = datetime.strptime(next(iter(end_dates)), "%Y-%m-%d")
                derived_duration = (end - start).days + 1 == duration_value
            if not explicit_duration and not derived_duration:
                extracted["nombre_jours_souhaites"] = None
                removed.append("nombre_jours_souhaites")

        if removed:
            triage["unverified_fields"] = list(dict.fromkeys(removed))
            if triage.get("request_mode") in {"new_request", "follow_up", "other"}:
                missing = list(triage.get("missing_fields") or [])
                for field in removed:
                    missing_field = "dates" if field in {"start_date", "end_date"} else field
                    if missing_field not in missing:
                        missing.append(missing_field)
                triage["missing_fields"] = missing

    @staticmethod
    def _number_has_source_evidence(number: int, source: str, nouns: tuple[str, ...]) -> bool:
        if number <= 0 or number > 500:
            return False
        number_tokens = [str(number), *NUMBER_WORDS.get(number, ())]
        noun_pattern = "|".join(re.escape(noun) for noun in nouns)
        token_pattern = "|".join(re.escape(token) for token in number_tokens)
        return bool(
            re.search(rf"\b(?:{token_pattern})\b\s+(?:{noun_pattern})\b", source)
            or re.search(rf"\b(?:{noun_pattern})\b\s*[:=-]?\s*\b(?:{token_pattern})\b", source)
        )

    @staticmethod
    def _normalize_search_text(value: str) -> str:
        normalized = unicodedata.normalize("NFD", value or "")
        without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        without_accents = re.sub(r"\br\s+(48\d|490)\b", r"r\1", without_accents.lower())
        return re.sub(r"[ \t]+", " ", without_accents).strip()

    def _enforce_planbot_missing_fields(
        self,
        triage: dict[str, Any],
        has_previous_cab: bool = False,
    ) -> None:
        intent = triage.get("intent")
        if intent not in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
            return
        if triage.get("request_mode") == "acknowledgement":
            triage["missing_fields"] = []
            return
        missing = [
            str(field) for field in (triage.get("missing_fields") or [])
            if str(field) in MISSING_FIELDS
        ]
        extracted = triage.get("extracted") or {}
        resolved_fields = {
            "formation_type": bool(extracted.get("formation_type")),
            "centre": bool(extracted.get("centre")),
            "dates": bool(extracted.get("start_date") and extracted.get("end_date")),
            "nb_candidates": bool(extracted.get("nb_candidates")),
            "categories": bool(extracted.get("categories")),
            "type_ir": extracted.get("type_ir") in {"initial", "recyclage"},
            "nombre_jours_souhaites": bool(extracted.get("nombre_jours_souhaites")),
        }
        missing = [field for field in missing if not resolved_fields.get(field, False)]
        for field in triage.get("ambiguous_fields") or []:
            if field in MISSING_FIELDS and field not in missing:
                missing.append(field)
        if triage.get("date_will_follow"):
            missing = [field for field in missing if field != "dates"]
        if triage.get("planbot_search_mode") == "next_sessions":
            missing = []
            if not extracted.get("formation_type"):
                missing.append("formation_type")
            if not extracted.get("centre"):
                missing.append("centre")
            if not extracted.get("nb_candidates"):
                missing.append("nb_candidates")
            if self._is_caces_request(extracted.get("formation_type")):
                if not extracted.get("categories"):
                    missing.append("categories")
                if extracted.get("type_ir") not in {"initial", "recyclage"}:
                    missing.append("type_ir")
            triage["missing_fields"] = missing
            return
        request_mode = triage.get("request_mode")
        if request_mode in {"new_request", "follow_up", "other"}:
            if not extracted.get("formation_type"):
                missing.append("formation_type")
            if not extracted.get("centre"):
                missing.append("centre")
            if (
                not triage.get("date_will_follow")
                and (not extracted.get("start_date") or not extracted.get("end_date"))
            ):
                missing.append("dates")
            if not extracted.get("nb_candidates"):
                missing.append("nb_candidates")
            if self._is_caces_request(extracted.get("formation_type")):
                if not extracted.get("categories"):
                    missing.append("categories")
                if extracted.get("type_ir") not in {"initial", "recyclage"}:
                    missing.append("type_ir")
                if not extracted.get("nombre_jours_souhaites") and not triage.get("date_will_follow"):
                    missing.append("nombre_jours_souhaites")
        triage["missing_fields"] = list(dict.fromkeys(missing))

    def _select_planbot_action(self, triage: dict[str, Any], source_text: str = "") -> str:
        operation = str(triage.get("session_operation") or "unknown")
        if operation in NO_AVAILABILITY_OPERATIONS:
            return ""
        if operation in EXACT_AVAILABILITY_OPERATIONS:
            if triage.get("session_context_status") != "resolved":
                return ""
            return "prevision_planif" if self._should_call_planbot(triage, source_text) else ""
        if triage.get("planbot_search_mode") == "next_sessions":
            return "search_alternative_dates" if self._should_search_next_sessions(triage) else ""
        return "full" if self._should_call_planbot(triage, source_text) else ""

    @classmethod
    def _should_search_next_sessions(cls, triage: dict[str, Any]) -> bool:
        if triage.get("request_mode") in {"acknowledgement", "document_submission", "confirmation_request"}:
            return False
        if triage.get("missing_fields"):
            return False
        extracted = triage.get("extracted") or {}
        if not all([
            extracted.get("formation_type"),
            extracted.get("centre"),
            extracted.get("nb_candidates"),
        ]):
            return False
        if cls._is_caces_request(extracted.get("formation_type")):
            return bool(
                extracted.get("categories")
                and extracted.get("type_ir") in {"initial", "recyclage"}
            )
        return True

    def _should_call_planbot(self, triage: dict[str, Any], source_text: str = "") -> bool:
        operation = str(triage.get("session_operation") or "unknown")
        if operation in NO_AVAILABILITY_OPERATIONS:
            return False
        if operation in EXACT_AVAILABILITY_OPERATIONS and triage.get("session_context_status") != "resolved":
            return False
        if triage.get("intent") not in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
            return False
        if triage.get("request_mode") in {
            "acknowledgement",
            "document_submission",
            "confirmation_request",
        }:
            return False
        if triage.get("missing_fields"):
            return False
        extracted = triage.get("extracted") or {}
        has_required_fields = all([
            extracted.get("formation_type"),
            extracted.get("centre"),
            extracted.get("start_date"),
            extracted.get("end_date"),
            extracted.get("nb_candidates"),
        ])
        if not has_required_fields:
            return False
        history_verified_fields = set(triage.get("history_verified_fields") or [])
        if source_text and not {"start_date", "end_date"}.issubset(history_verified_fields):
            source_dates, _ = extract_dates(source_text)
            requested_dates = set()
            for field in ("start_date", "end_date"):
                field_dates, invalid = extract_dates(str(extracted.get(field) or ""))
                if invalid:
                    return False
                requested_dates.update(field_dates)
            if not requested_dates or not requested_dates.issubset(source_dates):
                return False
        start_dates, _ = extract_dates(str(extracted.get("start_date") or ""))
        end_dates, _ = extract_dates(str(extracted.get("end_date") or ""))
        if not start_dates or not end_dates or next(iter(start_dates)) > next(iter(end_dates)):
            return False
        end_date = datetime.strptime(next(iter(end_dates)), "%Y-%m-%d").date()
        if end_date < datetime.now(ZoneInfo("Europe/Paris")).date():
            return False
        if self._is_caces_request(extracted.get("formation_type")):
            return bool(
                extracted.get("categories")
                and extracted.get("type_ir") in {"initial", "recyclage"}
                and extracted.get("nombre_jours_souhaites")
            )
        return True

    @staticmethod
    def _is_caces_request(formation_type: Any) -> bool:
        value = str(formation_type or "").lower()
        return "caces" in value or bool(re.search(r"\br\s?(?:48[24569]|490)\b", value, flags=re.IGNORECASE))

    def _build_planbot_payload(self, triage: dict[str, Any], action: str = "full") -> dict[str, Any]:
        extracted = triage.get("extracted") or {}
        payload = {
            "centre": extracted.get("centre"),
            "formation_type": extracted.get("formation_type"),
            "start_date": extracted.get("start_date"),
            "end_date": extracted.get("end_date"),
            "nb_candidates": int(extracted.get("nb_candidates") or 1),
            "categories": extracted.get("categories") or [],
            "nb_categories": extracted.get("nb_categories") or None,
            "type_ir": extracted.get("type_ir") or "",
            "financement": extracted.get("financement") or "B2B",
            "nombre_jours_souhaites": extracted.get("nombre_jours_souhaites") or None,
        }
        if action == "search_alternative_dates":
            payload.update({
                "around_date": datetime.now(ZoneInfo("Europe/Paris")).date().isoformat(),
                "direction": "after",
                "nb_weeks": 6,
            })
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    def _build_exact_alternative_dates_payload(self, triage: dict[str, Any]) -> dict[str, Any]:
        payload = self._build_planbot_payload(triage)
        start_date = str(payload.pop("start_date", ""))
        end_date = str(payload.pop("end_date", ""))
        payload.pop("nombre_jours_souhaites", None)
        try:
            min_start_date = (
                datetime.strptime(end_date, "%Y-%m-%d").date() + timedelta(days=1)
            ).isoformat()
        except ValueError:
            min_start_date = end_date or start_date
        payload.update({
            "around_date": start_date,
            "min_start_date": min_start_date,
            "requested_start_date": start_date,
            "requested_end_date": end_date,
            "strict_requested_period": True,
            "direction": "after",
            "nb_weeks": 12,
        })
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    def _build_exact_alternative_centres_payload(self, triage: dict[str, Any]) -> dict[str, Any]:
        payload = self._build_planbot_payload(triage)
        payload["exclude_centre"] = payload.pop("centre", "")
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    def _build_planbot_alternative_centres_payload(self, triage: dict[str, Any]) -> dict[str, Any]:
        extracted = triage.get("extracted") or {}
        today = datetime.now(ZoneInfo("Europe/Paris")).date()
        days_until_monday = (7 - today.weekday()) % 7 or 7
        start = today + timedelta(days=days_until_monday)
        end = start + timedelta(weeks=12, days=-3)
        payload = {
            "exclude_centre": extracted.get("centre"),
            "formation_type": extracted.get("formation_type"),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "nb_candidates": int(extracted.get("nb_candidates") or 1),
            "categories": extracted.get("categories") or [],
            "nb_categories": extracted.get("nb_categories") or None,
            "type_ir": extracted.get("type_ir") or "",
            "financement": extracted.get("financement") or "B2B",
        }
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

    @staticmethod
    def _planbot_has_available_options(result: dict[str, Any] | None) -> bool:
        if not isinstance(result, dict):
            return False
        require_sequence = RelationsTicketWorkflow._is_caces_request(result.get("formation"))
        return any(
            isinstance(item, dict)
            and item.get("dispo_reelle")
            and (
                (item.get("sequence_valide") is True and bool(item.get("options")))
                or (not require_sequence and item.get("sequence_valide") is None and bool(item.get("jours")))
            )
            for key in ("semaines", "centres")
            for item in result.get(key) or []
        )

    def _add_internal_note(self, ticket_id: str, content: str) -> None:
        try:
            self.desk_client.add_ticket_comment(ticket_id, content, is_public=False)
        except Exception as exc:
            logger.warning("Unable to add Relations internal note on %s: %s", ticket_id, exc)

    def _mark_brouillon_auto(self, ticket_id: str) -> None:
        try:
            self.desk_client.update_ticket(ticket_id, {"cf": {"cf_brouillon_auto": True}})
        except Exception as exc:
            logger.debug("Unable to mark Relations draft flag on %s: %s", ticket_id, exc)
