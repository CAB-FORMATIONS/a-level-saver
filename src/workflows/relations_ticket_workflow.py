"""Relations entreprises ticket workflow.

Separate draft-only workflow for B2B emails. It deliberately does not reuse the
DOC state engine because DOC is candidate/VTC/Uber specific.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from config import settings
from src.agents.relations_triage_agent import RelationsTriageAgent
from src.constants.departments import DEPT_RELATIONS_ENTREPRISES_ID
from src.utils.planbot_api_client import PlanBotAPIClient
from src.utils.relations_crm_lookup import RelationsCRMLookup, email_domain, normalize_email
from src.utils.relations_response_builder import build_internal_note, build_relations_response
from src.utils.relations_response_validator import validate_relations_response
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

INTERNAL_DOMAINS = {"cab-formations.fr", "formalogistics.fr", "formalogistics.com"}


class RelationsTicketWorkflow:
    """Draft-only workflow for the Relations entreprises desk department."""

    def __init__(self):
        self.desk_client = ZohoDeskClient()
        self.crm_client = ZohoCRMClient()
        self.crm_lookup = RelationsCRMLookup(self.crm_client)
        self.triage_agent = RelationsTriageAgent()
        self.planbot_client = PlanBotAPIClient()

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
            if department_id and department_id != settings.zoho_desk_relations_department_id and department_id != DEPT_RELATIONS_ENTREPRISES_ID:
                logger.info("Relations workflow called for non-relations dept: %s", department_id)

            result["workflow_stage"] = "DRAFT_CHECK"
            if not ignore_existing_draft and self.desk_client.has_existing_draft(ticket_id):
                result.update({
                    "success": True,
                    "workflow_stage": "SKIPPED_DRAFT_EXISTS",
                    "skip_reason": "Un brouillon existe deja pour ce ticket",
                })
                return result

            threads = self.desk_client.get_all_threads_with_full_content(ticket_id)
            message = self._latest_customer_message(threads)
            sender_email = self._resolve_sender_email(ticket, threads, message)
            subject = ticket.get("subject") or ""

            result["sender_email"] = sender_email
            if self._is_noise(sender_email, subject, message):
                result.update({
                    "success": True,
                    "workflow_stage": "SKIPPED_NOISE",
                    "skip_reason": "Bruit/no-reply/notification detecte",
                })
                return result

            if not sender_email or email_domain(sender_email) in INTERNAL_DOMAINS:
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
            })
            self._enforce_planbot_missing_fields(triage)
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

            result["workflow_stage"] = "PLANBOT"
            planbot_result = None
            if self._should_call_planbot(triage):
                payload = self._build_planbot_payload(triage)
                planbot_result = self.planbot_client.check_availability(payload, action="full")
                result["planbot_payload"] = payload
            result["planbot_result"] = planbot_result

            result["workflow_stage"] = "RESPONSE"
            response_html = build_relations_response(triage, crm_context, planbot_result)
            validation = validate_relations_response(response_html, triage, planbot_result)
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
        for thread in threads:
            if thread.get("status", "").upper() == "DRAFT":
                continue
            if thread.get("direction") == "in":
                content = get_clean_thread_content(thread)
                if content.strip():
                    return content.strip()
        return ""

    def _resolve_sender_email(self, ticket: dict[str, Any], threads: list[dict[str, Any]], message: str) -> str:
        for thread in threads:
            if thread.get("status", "").upper() == "DRAFT":
                continue
            if thread.get("direction") == "in":
                email = normalize_email(thread.get("fromEmailAddress") or "")
                if email and email_domain(email) not in INTERNAL_DOMAINS:
                    return email

        ticket_email = normalize_email(ticket.get("email") or "")
        if ticket_email and email_domain(ticket_email) not in INTERNAL_DOMAINS:
            return ticket_email

        for candidate in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", message or ""):
            candidate = normalize_email(candidate)
            if email_domain(candidate) not in INTERNAL_DOMAINS and not self._email_is_noise(candidate):
                return candidate
        return ticket_email

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

    def _enforce_planbot_missing_fields(self, triage: dict[str, Any]) -> None:
        intent = triage.get("intent")
        if intent not in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
            return
        extracted = triage.get("extracted") or {}
        missing = list(triage.get("missing_fields") or [])
        required = {
            "formation_type": extracted.get("formation_type"),
            "centre": extracted.get("centre"),
            "start_date": extracted.get("start_date"),
            "end_date": extracted.get("end_date"),
            "nb_candidates": extracted.get("nb_candidates"),
        }
        for key, value in required.items():
            if not value and key not in missing:
                missing.append(key)
        formation = str(extracted.get("formation_type") or "").lower()
        if "caces" in formation:
            if not extracted.get("categories") and not extracted.get("nb_categories") and "categories" not in missing:
                missing.append("categories")
            if not extracted.get("nombre_jours_souhaites") and "nombre_jours_souhaites" not in missing:
                missing.append("nombre_jours_souhaites")
        triage["missing_fields"] = missing

    def _should_call_planbot(self, triage: dict[str, Any]) -> bool:
        if triage.get("intent") not in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
            return False
        if triage.get("missing_fields"):
            return False
        extracted = triage.get("extracted") or {}
        return all([
            extracted.get("formation_type"),
            extracted.get("centre"),
            extracted.get("start_date"),
            extracted.get("end_date"),
            extracted.get("nb_candidates"),
        ])

    def _build_planbot_payload(self, triage: dict[str, Any]) -> dict[str, Any]:
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
        return {key: value for key, value in payload.items() if value not in (None, "", [])}

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
