"""
DOC Ticket Workflow - Complete orchestration for DOC department tickets.

This workflow implements the 8-step process from 00_CHECKLIST_EXECUTION.md:

1. AGENT TRIEUR (Triage with STOP & GO logic)
2. AGENT ANALYSTE (6-source data extraction)
3. AGENT RÉDACTEUR (Response generation with Claude + RAG)
4. CRM Note Creation (before draft)
5. Ticket Update (status, tags)
6. Deal Update (if scenario requires)
7. Draft Creation (Zoho Desk)
8. Final Validation

Gates:
- If AGENT TRIEUR says STOP (routing) → no draft, end workflow
- If AGENT ANALYSTE finds ANCIEN_DOSSIER → internal alert, end workflow
- If data missing → escalate, end workflow
"""
import logging
import os
import sys
from pathlib import Path
from typing import Dict, Optional, List, Any
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Load environment variables (for Anthropic API key)
from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from src.agents.deal_linking_agent import DealLinkingAgent
from src.agents.examt3p_agent import ExamT3PAgent
from src.agents.dispatcher_agent import TicketDispatcherAgent
from src.agents.crm_update_agent import CRMUpdateAgent
from src.agents.triage_agent import TriageAgent
from src.zoho_client import ZohoDeskClient, ZohoCRMClient
from knowledge_base.scenarios_mapping import (
    detect_scenario_from_text,
    should_stop_workflow,
    requires_crm_update,
    get_crm_update_fields,
    SCENARIOS
)

# State Engine - Architecture State-Driven
from src.state_engine import StateDetector, TemplateEngine, ResponseValidator, CRMUpdater
from src.utils.crm_lookup_helper import enrich_deal_lookups
from src.utils.response_humanizer import humanize_response
from src.utils.intent_parser import IntentParser
from src.utils.date_filter import DateFilter, apply_final_filter
from src.constants.models import MODEL_EXTRACTION, MODEL_PERSONALIZATION, MODEL_TRIAGE
from src.constants.amounts import UBER_OFFER_AMOUNT
from src.constants.intents import (
    DATE_CONFIRMATION_INTENTS, DATE_RELATED_INTENTS, NEEDS_NEXT_DATES_INTENTS,
    SESSION_CHANGE_INTENTS, FULL_RECAP_INTENTS,
)
from src.constants.keywords import (
    ANNULATION_MARKERS, CMA_MARKERS, ANNULATION_KEYWORDS, SPAM_KEYWORDS,
    CMA_EMAIL_DOMAINS, REPLY_MARKERS, BATCH_EXCLUSION, SALESIQ_MARKERS,
    NON_UBER_REGISTRATION, DUPLICATE_MARKERS, UBER_CONVERTED, INFO_REQUEST,
    OUT_OF_SCOPE, UBER_KEYWORDS, SKIP_PATTERNS, LOGO_SIGNATURE_PATTERNS,
    DOCUMENT_KEYWORDS as DOC_KEYWORDS,
)
import anthropic

logger = logging.getLogger(__name__)


class DOCTicketWorkflow:
    """Complete workflow orchestrator for DOC tickets."""

    def __init__(self):
        """
        Initialize workflow with all required components.

        Creates only 2 Zoho clients (Desk + CRM) and injects them into all agents
        to share token management and reduce API calls.
        """
        # Create shared clients (TokenManager singleton handles token caching)
        self.desk_client = ZohoDeskClient()
        self.crm_client = ZohoCRMClient()

        # Inject shared clients into all agents
        self.deal_linker = DealLinkingAgent(
            desk_client=self.desk_client,
            crm_client=self.crm_client
        )
        self.examt3p_agent = ExamT3PAgent()  # Uses Playwright, not Zoho API
        self.dispatcher = TicketDispatcherAgent(desk_client=self.desk_client)
        self.crm_update_agent = CRMUpdateAgent(crm_client=self.crm_client)
        self.triage_agent = TriageAgent()  # Uses Anthropic API, not Zoho API

        # State Engine - Architecture State-Driven (seul mode supporté)
        self.state_detector = StateDetector()
        self.template_engine = TemplateEngine()
        self.response_validator = ResponseValidator()
        self.state_crm_updater = CRMUpdater(crm_client=self.crm_client)
        # Anthropic client for AI personalization (using Sonnet for best quality)
        self.anthropic_client = anthropic.Anthropic()
        self.personalization_model = MODEL_PERSONALIZATION

        logger.info("✅ DOCTicketWorkflow initialized (State Engine, shared clients)")

    # Scénarios éligibles à l'auto-send.
    # Chaque entrée: (subject_contains, max_threads ou None si pas de limite)
    # subject_contains: comparaison case-insensitive
    AUTO_SEND_SCENARIOS = [
        {'subject_contains': 'test de sélection réussi', 'max_threads': 1},
        {'subject_contains': 'test de selection reussi', 'max_threads': 1},
        # Ajouter d'autres scénarios ici, ex:
        # {'subject_contains': 'autre sujet', 'max_threads': None},  # pas de limite de threads
    ]

    def _can_auto_send(self, response_result: Dict, triage_result: Dict = None) -> tuple:
        """Check if the response is safe to auto-send (vs fallback to draft).

        Guard rails:
        1. Scenario eligibility: subject must match a whitelist entry + respect max_threads if defined
        2. Response quality: non-empty, humanized, validation passed

        Returns:
            (can_send: bool, fallback_reason: Optional[str])
        """
        # --- Scenario eligibility ---
        if not triage_result:
            return False, 'no_triage_data'

        subject = (triage_result.get('ticket_subject') or '').lower().strip()
        incoming_count = triage_result.get('incoming_thread_count', 0)

        matched_scenario = None
        for scenario in self.AUTO_SEND_SCENARIOS:
            if scenario['subject_contains'] in subject:
                matched_scenario = scenario
                break

        if not matched_scenario:
            return False, f'subject_not_eligible: {subject[:50]}'

        if matched_scenario.get('max_threads') is not None and incoming_count > matched_scenario['max_threads']:
            return False, f'too_many_threads: {incoming_count}'

        # --- Response quality ---
        if not response_result.get('response_text', '').strip():
            return False, 'empty_response'
        if not response_result.get('was_humanized', False):
            return False, 'humanizer_failed'
        for sid, val in response_result.get('validation', {}).items():
            if not val.get('compliant', True):
                return False, 'validation_errors'

        return True, None

    def _mark_brouillon_auto(self, ticket_id: str) -> None:
        """Mark ticket with BROUILLON AUTO = true after draft creation."""
        try:
            self.desk_client.update_ticket(ticket_id, {'cf': {'cf_brouillon_auto': True}})
            logger.debug(f"  ✅ BROUILLON AUTO coché pour ticket {ticket_id}")
        except Exception as e:
            logger.warning(f"  ⚠️ Erreur marquage BROUILLON AUTO: {e}")

    def _check_pending_duplicate_clarification(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """
        Check if this ticket has a pending duplicate clarification.

        Looks for internal notes containing [DUPLICATE_PENDING:deal_id] marker.
        Also extracts the duplicate's email and phone for comparison.

        Returns:
            None if no pending clarification
            Dict with pending_deal_id, duplicate_type, duplicate_email, duplicate_phone
        """
        import re

        try:
            comments = self.desk_client.get_ticket_comments(
                ticket_id=ticket_id,
                include_public=False,
                include_private=True
            )

            for comment in comments:
                content = comment.get('content', '')
                # Look for the marker [DUPLICATE_PENDING:deal_id]
                match = re.search(r'\[DUPLICATE_PENDING:(\d+)\]', content)
                if match:
                    deal_id = match.group(1)

                    # Extract duplicate type from the note
                    type_match = re.search(r'Type:\s*(\w+)', content)
                    dup_type = type_match.group(1) if type_match else 'UNKNOWN'

                    # Extract duplicate email from the note
                    email_match = re.search(r'Email doublon:\s*([^\s\n]+)', content)
                    dup_email = email_match.group(1) if email_match else ''
                    if dup_email == 'N/A':
                        dup_email = ''

                    # Extract duplicate phone from the note
                    phone_match = re.search(r'Téléphone doublon:\s*([^\s\n]+)', content)
                    dup_phone = phone_match.group(1) if phone_match else ''
                    if dup_phone == 'N/A':
                        dup_phone = ''

                    # Extract original intent from the note
                    intent_match = re.search(r'Intention originale:\s*(\w+)', content)
                    original_intent = intent_match.group(1) if intent_match else 'UNKNOWN'

                    logger.info(f"  📝 Clarification doublon en attente trouvée: Deal {deal_id}")
                    logger.info(f"     Email doublon: {dup_email or 'N/A'}")
                    logger.info(f"     Téléphone doublon: {dup_phone or 'N/A'}")
                    logger.info(f"     Intention originale: {original_intent}")

                    return {
                        'pending_deal_id': deal_id,
                        'duplicate_type': dup_type,
                        'duplicate_email': dup_email,
                        'duplicate_phone': dup_phone,
                        'original_intent': original_intent,
                        'comment_id': comment.get('id')
                    }

            return None

        except Exception as e:
            logger.warning(f"  ⚠️ Erreur vérification clarification en attente: {e}")
            return None

    def _verify_duplicate_clarification_response(
        self,
        ticket_id: str,
        pending_clarification: Dict[str, Any],
        latest_message: str
    ) -> Dict[str, Any]:
        """
        Verify if the candidate's response matches the duplicate's credentials.

        Extracts email/phone from the latest message and compares with stored values.

        Returns:
            {
                'verified': bool,
                'match_type': 'email' | 'phone' | 'both' | 'none',
                'extracted_email': str or None,
                'extracted_phone': str or None,
                'reason': str
            }
        """
        import re

        result = {
            'verified': False,
            'match_type': 'none',
            'extracted_email': None,
            'extracted_phone': None,
            'reason': ''
        }

        # Get stored duplicate credentials
        dup_email = pending_clarification.get('duplicate_email', '').lower().strip()
        dup_phone = pending_clarification.get('duplicate_phone', '').strip()

        # Normalize phone (remove spaces, dots, dashes)
        def normalize_phone(phone: str) -> str:
            if not phone:
                return ''
            # Remove all non-digits except leading +
            normalized = re.sub(r'[^\d+]', '', phone)
            # Convert +33 to 0
            if normalized.startswith('+33'):
                normalized = '0' + normalized[3:]
            elif normalized.startswith('33') and len(normalized) > 10:
                normalized = '0' + normalized[2:]
            return normalized

        dup_phone_normalized = normalize_phone(dup_phone)

        # Extract email from message
        email_pattern = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
        email_matches = email_pattern.findall(latest_message)
        if email_matches:
            result['extracted_email'] = email_matches[0].lower()

        # Extract phone from message (French format)
        phone_pattern = re.compile(r'(?:(?:\+33|0033|33)|0)[67][\s.-]?\d{2}[\s.-]?\d{2}[\s.-]?\d{2}[\s.-]?\d{2}')
        phone_matches = phone_pattern.findall(latest_message)
        if phone_matches:
            result['extracted_phone'] = phone_matches[0]

        # Compare
        email_match = False
        phone_match = False

        if result['extracted_email'] and dup_email:
            email_match = result['extracted_email'] == dup_email
            logger.info(f"  📧 Comparaison email: '{result['extracted_email']}' vs '{dup_email}' → {'MATCH' if email_match else 'NO MATCH'}")

        if result['extracted_phone'] and dup_phone_normalized:
            extracted_normalized = normalize_phone(result['extracted_phone'])
            phone_match = extracted_normalized == dup_phone_normalized
            logger.info(f"  📱 Comparaison téléphone: '{extracted_normalized}' vs '{dup_phone_normalized}' → {'MATCH' if phone_match else 'NO MATCH'}")

        # Determine result
        if email_match and phone_match:
            result['verified'] = True
            result['match_type'] = 'both'
            result['reason'] = 'Email ET téléphone correspondent'
        elif email_match:
            result['verified'] = True
            result['match_type'] = 'email'
            result['reason'] = 'Email correspond'
        elif phone_match:
            result['verified'] = True
            result['match_type'] = 'phone'
            result['reason'] = 'Téléphone correspond'
        else:
            result['verified'] = False
            result['match_type'] = 'none'
            if not result['extracted_email'] and not result['extracted_phone']:
                result['reason'] = 'Aucun email ou téléphone trouvé dans la réponse'
            else:
                result['reason'] = 'Email/téléphone ne correspondent pas au dossier doublon'

        return result

    def process_ticket(
        self,
        ticket_id: str,
        auto_create_draft: bool = False,
        auto_update_crm: bool = False,
        auto_update_ticket: bool = False,
        auto_send: bool = False
    ) -> Dict:
        """
        Process a DOC ticket through the complete workflow.

        Args:
            ticket_id: Zoho Desk ticket ID
            auto_create_draft: Automatically create draft in Zoho Desk
            auto_update_crm: Automatically update CRM deal fields
            auto_update_ticket: Automatically update ticket status/tags

        Returns:
            {
                'success': bool,
                'ticket_id': str,
                'workflow_stage': str,  # Which stage we stopped at
                'triage_result': Dict,
                'analysis_result': Dict,
                'response_result': Dict,
                'crm_note': str,
                'draft_created': bool,
                'errors': List[str]
            }
        """
        logger.info(f"=" * 80)
        logger.info(f"Processing DOC ticket: {ticket_id}")
        logger.info(f"=" * 80)

        result = {
            'success': False,
            'ticket_id': ticket_id,
            'workflow_stage': '',
            'triage_result': {},
            'analysis_result': {},
            'response_result': {},
            'crm_note': '',
            'draft_created': False,
            'reply_sent': False,
            'delivery_method': 'none',
            'send_fallback_reason': None,
            'crm_updated': False,
            'ticket_updated': False,
            'errors': []
        }

        try:
            # ================================================================
            # STEP 0: VÉRIFIER SI UN BROUILLON EXISTE DÉJÀ
            # ================================================================
            logger.info("\n0️⃣  VÉRIFICATION BROUILLON EXISTANT...")
            if not os.environ.get('SKIP_DRAFT_CHECK') and self.desk_client.has_existing_draft(ticket_id):
                logger.warning("⚠️  BROUILLON EXISTANT DÉTECTÉ → SKIP WORKFLOW")
                result['workflow_stage'] = 'SKIPPED_DRAFT_EXISTS'
                result['success'] = True
                result['skip_reason'] = 'Un brouillon existe déjà pour ce ticket'
                return result
            logger.info("  ✅ Pas de brouillon existant, continuation du workflow")

            # ================================================================
            # STEP 0.1: SKIP INSTANT MESSAGES (SalesIQ chat widget)
            # ================================================================
            ticket_data = self.desk_client.get_ticket(ticket_id)
            ticket_source = ticket_data.get('source', {})
            if isinstance(ticket_source, dict) and ticket_source.get('type') == 'INSTANT_MESSAGE':
                logger.warning(f"⚠️  INSTANT MESSAGE détecté (channel: {ticket_data.get('channel', 'N/A')}) → SKIP + CLOSE")
                if auto_update_ticket:
                    try:
                        self.desk_client.update_ticket(ticket_id, {'status': 'Closed'})
                        logger.info("  ✅ Ticket IM clôturé")
                    except Exception as e:
                        logger.warning(f"  ⚠️ Erreur clôture ticket IM: {e}")
                result['workflow_stage'] = 'SKIPPED_INSTANT_MESSAGE'
                result['success'] = True
                result['skip_reason'] = 'Ticket Instant Message (SalesIQ) - clôturé automatiquement'
                return result

            # ================================================================
            # STEP 0.5: VÉRIFIER SI CLARIFICATION DOUBLON EN ATTENTE
            # (Si le candidat répond à une demande de clarification de doublon)
            # ================================================================
            pending_clarification = self._check_pending_duplicate_clarification(ticket_id)
            if pending_clarification:
                logger.info(f"\n📝 CLARIFICATION DOUBLON EN ATTENTE: Deal {pending_clarification['pending_deal_id']}")
                result['pending_duplicate_clarification'] = pending_clarification
                # Note: La réponse du candidat sera analysée par le triage_agent
                # qui détectera l'intention CONFIRMATION_DOUBLON ou REFUS_DOUBLON

            # ================================================================
            # STEP 1: AGENT TRIEUR (Triage with STOP & GO)
            # ================================================================
            logger.info("\n1️⃣  AGENT TRIEUR - Triage du ticket...")
            result['workflow_stage'] = 'TRIAGE'

            # auto_transfer=False if we're in dry-run mode (no ticket updates)
            triage_result = self._run_triage(ticket_id, auto_transfer=auto_update_ticket)
            result['triage_result'] = triage_result

            # ================================================================
            # CHECK: Réponse à une clarification de doublon en attente ?
            # On vérifie si l'email ou téléphone fourni correspond au doublon
            # ================================================================
            if pending_clarification:
                pending_deal_id = pending_clarification['pending_deal_id']
                logger.info(f"\n🔄 VÉRIFICATION CLARIFICATION DOUBLON (Deal {pending_deal_id})")

                # Récupérer le dernier message du candidat
                try:
                    threads_response = self.desk_client.get_ticket_threads(ticket_id)
                    threads = threads_response.get('data', []) if isinstance(threads_response, dict) else threads_response
                    latest_message = ''
                    for thread in threads:
                        # Chercher le dernier message du client (pas de l'agent)
                        if thread.get('direction') == 'in' or thread.get('isForward'):
                            latest_message = thread.get('content', '') or thread.get('plainText', '')
                            break

                    if latest_message:
                        # Vérifier si l'email/téléphone correspond
                        verification = self._verify_duplicate_clarification_response(
                            ticket_id=ticket_id,
                            pending_clarification=pending_clarification,
                            latest_message=latest_message
                        )
                        result['duplicate_verification'] = verification

                        if verification['verified']:
                            # ✅ MATCH - Le candidat a fourni un email/téléphone qui correspond
                            logger.info(f"  ✅ VÉRIFICATION RÉUSSIE: {verification['reason']}")

                            # 1. Récupérer le deal doublon
                            duplicate_deal = self.crm_client.get_deal(pending_deal_id)
                            if duplicate_deal:
                                # 2. Mettre à jour cf_opportunite vers le deal doublon
                                deal_url = f"https://crm.zoho.com/crm/org123/tab/Potentials/{pending_deal_id}"
                                try:
                                    self.desk_client.update_ticket(ticket_id, {
                                        'cf': {'cf_opportunite': deal_url}
                                    })
                                    logger.info(f"  ✅ cf_opportunite mis à jour vers deal doublon: {pending_deal_id}")
                                    result['cf_opportunite_updated'] = pending_deal_id
                                except Exception as e:
                                    logger.error(f"  ⚠️ Erreur mise à jour cf_opportunite: {e}")

                                # 3. Classifier le type de doublon et traiter comme DUPLICATE_RECOVERABLE
                                duplicate_type = pending_clarification.get('duplicate_type', 'RECOVERABLE_NOT_PAID')
                                original_intent = pending_clarification.get('original_intent', 'UNKNOWN')

                                # Injecter les infos du doublon dans triage_result
                                triage_result['action'] = 'DUPLICATE_RECOVERABLE'
                                triage_result['duplicate_type'] = duplicate_type
                                triage_result['duplicate_deals'] = [duplicate_deal]
                                triage_result['selected_deal'] = duplicate_deal
                                triage_result['deal_to_work_on'] = duplicate_deal
                                triage_result['already_paid_to_cma'] = self.deal_linker._is_already_paid_to_cma(duplicate_deal)

                                # Réinjecter l'intention originale pour que le workflow y réponde
                                triage_result['detected_intent'] = original_intent
                                triage_result['original_intent_restored'] = True
                                logger.info(f"  📋 Intention originale restaurée: {original_intent}")

                                # 4. Ajouter une note de résolution
                                resolution_note = f"""✅ CLARIFICATION DOUBLON RÉSOLUE - IDENTITÉ VÉRIFIÉE

Le candidat a fourni des informations qui CORRESPONDENT au dossier doublon.
→ Méthode de vérification: {verification['match_type']}
→ {verification['reason']}
→ Email fourni: {verification.get('extracted_email') or 'N/A'}
→ Téléphone fourni: {verification.get('extracted_phone') or 'N/A'}
→ Deal ID confirmé: {pending_deal_id}
→ cf_opportunite mis à jour vers ce deal
→ Intention originale restaurée: {original_intent}

[DUPLICATE_RESOLVED:VERIFIED]"""

                                try:
                                    self.desk_client.add_ticket_comment(
                                        ticket_id=ticket_id,
                                        content=resolution_note,
                                        is_public=False
                                    )
                                except Exception as e:
                                    logger.warning(f"  ⚠️ Erreur ajout note résolution: {e}")

                                logger.info("  → Continuation comme DUPLICATE_RECOVERABLE")

                        else:
                            # ❌ PAS DE MATCH - L'email/téléphone ne correspond pas
                            logger.info(f"  ❌ VÉRIFICATION ÉCHOUÉE: {verification['reason']}")

                            # Ajouter une note avec les détails
                            no_match_note = f"""⚠️ CLARIFICATION DOUBLON - VÉRIFICATION ÉCHOUÉE

Le candidat a répondu mais les informations NE CORRESPONDENT PAS.
→ Email fourni: {verification.get('extracted_email') or 'Aucun'}
→ Téléphone fourni: {verification.get('extracted_phone') or 'Aucun'}
→ Raison: {verification['reason']}

ACTION: Traitement comme nouveau dossier (homonyme probable)

[DUPLICATE_VERIFICATION_FAILED]"""

                            try:
                                self.desk_client.add_ticket_comment(
                                    ticket_id=ticket_id,
                                    content=no_match_note,
                                    is_public=False
                                )
                            except Exception as e:
                                logger.warning(f"  ⚠️ Erreur ajout note: {e}")

                            # Continuer comme nouveau dossier
                            triage_result['action'] = 'GO'
                            logger.info("  → Continuation comme nouveau dossier (GO)")

                except Exception as e:
                    logger.error(f"  ❌ Erreur vérification clarification: {e}")
                    result['errors'].append(f"Erreur vérification clarification: {e}")

            # Check if we should STOP (routing to another department)
            if triage_result.get('action') == 'ROUTE':
                target_dept = triage_result.get('target_department')
                detected_intent = triage_result.get('detected_intent')
                logger.warning(f"⚠️  TRIAGE → ROUTE to {target_dept}")

                # CAS SPÉCIAL: TRANSMET_DOCUMENTS vers Refus CMA → créer un brouillon d'accusé réception
                if target_dept == 'Refus CMA' and detected_intent == 'TRANSMET_DOCUMENTS':
                    logger.info("  📝 Création d'un brouillon d'accusé réception avant transfert...")

                    # Récupérer le prénom du candidat depuis le deal
                    selected_deal = triage_result.get('selected_deal', {})
                    deal_name = selected_deal.get('Deal_Name', '') if selected_deal else ''
                    # Extraire le prénom : "BFS NP Jonathan Alvarez" → "Jonathan"
                    # Le prénom est généralement après "BFS NP" ou "BFS ONLINE"
                    prenom = 'Candidat'
                    if deal_name:
                        parts = deal_name.split()
                        if len(parts) >= 3:
                            # Skip BFS, NP/ONLINE, prendre le 3ème mot (prénom)
                            prenom = parts[2].capitalize()
                        elif len(parts) >= 1:
                            prenom = parts[-1].capitalize()

                    # Message d'accusé réception simple
                    acknowledgment_html = f"""Bonjour {prenom},<br>
<br>
Nous avons bien reçu votre document et nous vous en remercions.<br>
<br>
Notre équipe va le traiter dans les plus brefs délais. Si des informations complémentaires sont nécessaires, nous reviendrons vers vous.<br>
<br>
Cordialement,<br>
L'équipe CAB Formations"""

                    result['response_result'] = {
                        'response_text': acknowledgment_html,
                        'template_used': 'transmet_documents_acknowledgment'
                    }
                    result['draft_content'] = acknowledgment_html

                    # Créer le brouillon si demandé
                    if auto_create_draft:
                        try:
                            from config import settings

                            ticket = self.desk_client.get_ticket(ticket_id)
                            to_email = ticket.get('email', '')
                            from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                            logger.info(f"  📧 Draft TRANSMET_DOCUMENTS: from={from_email}, to={to_email}")

                            draft_result = self.desk_client.create_ticket_reply_draft(
                                ticket_id=ticket_id,
                                content=acknowledgment_html,
                                content_type='html',
                                from_email=from_email,
                                to_email=to_email
                            )

                            if draft_result:
                                logger.info(f"  ✅ Brouillon d'accusé réception créé")
                                result['draft_created'] = True
                                self._mark_brouillon_auto(ticket_id)

                                # Transférer le ticket vers Refus CMA
                                if auto_update_ticket:
                                    try:
                                        self.desk_client.move_ticket_to_department(ticket_id, "Refus CMA")
                                        logger.info("  ✅ Ticket transféré vers Refus CMA")
                                        result['transferred_to'] = "Refus CMA"
                                    except Exception as transfer_error:
                                        logger.error(f"  ❌ Erreur transfert: {transfer_error}")
                            else:
                                logger.warning("  ⚠️ Échec création brouillon")
                                result['draft_created'] = False
                        except Exception as e:
                            logger.error(f"  ❌ Erreur création brouillon: {e}")
                            result['draft_created'] = False
                    else:
                        logger.info("  ℹ️ Brouillon non créé (dry-run ou auto_create_draft=False)")
                        result['draft_created'] = False
                else:
                    logger.warning("🛑 STOP WORKFLOW (pas de draft selon règles)")

                result['workflow_stage'] = 'STOPPED_AT_TRIAGE'
                result['success'] = True
                return result

            # Check if SPAM
            if triage_result.get('action') == 'SPAM':
                logger.warning("⚠️  SPAM détecté → Clôturer sans note CRM")
                result['workflow_stage'] = 'STOPPED_SPAM'
                if auto_update_ticket:
                    self.desk_client.update_ticket(ticket_id, {"status": "Closed"})
                result['success'] = True
                return result

            # Check if CMA NOTIFICATION (dossier incomplet / validé)
            if triage_result.get('action') == 'CMA_NOTIFICATION':
                cma_type = triage_result.get('cma_type', 'INCONNU')
                logger.warning(f"🏛️ CMA NOTIFICATION ({cma_type}) → Clôture automatique")

                # Note interne pour traçabilité
                try:
                    note = f"🏛️ Email CMA - {cma_type}\nClôturé automatiquement (notification CMA, pas d'action requise)."
                    self.desk_client.add_ticket_comment(ticket_id, note, is_public=False)
                except Exception as e:
                    logger.warning(f"Erreur ajout note CMA: {e}")

                result['workflow_stage'] = f'CLOSED_CMA_{cma_type}'
                if auto_update_ticket:
                    self.desk_client.update_ticket(ticket_id, {"status": "Closed"})
                result['success'] = True
                return result

            # CMA email non catégorisé → reste dans DOC sans action
            if triage_result.get('action') == 'CMA_OTHER':
                logger.warning("🏛️ Email CMA non catégorisé → Reste dans DOC (pas de route, pas de clôture)")
                result['workflow_stage'] = 'SKIPPED_CMA_OTHER'
                result['success'] = True
                return result

            # Check if DUPLICATE UBER 20€
            if triage_result.get('action') == 'DUPLICATE_UBER':
                logger.warning("⚠️  DOUBLON UBER 20€ → Candidat a déjà bénéficié de l'offre")
                result['workflow_stage'] = 'DUPLICATE_UBER_OFFER'
                result['duplicate_deals'] = triage_result.get('duplicate_deals', [])

                # Mettre à jour EXAM_INCLUS = Non sur le deal du ticket
                if auto_update_crm:
                    selected_deal = triage_result.get('selected_deal', {})
                    deal_id_to_update = selected_deal.get('id') if selected_deal else None
                    if deal_id_to_update:
                        try:
                            self.crm_client.update_deal(deal_id_to_update, {'EXAM_INCLUS': 'Non'})
                            logger.info(f"  ✅ EXAM_INCLUS=Non sur deal {selected_deal.get('Deal_Name', deal_id_to_update)}")
                        except Exception as e:
                            logger.warning(f"  ⚠️ Erreur mise à jour EXAM_INCLUS: {e}")

                # Générer une réponse spécifique pour ce cas
                duplicate_response = self._generate_duplicate_uber_response(
                    ticket_id=ticket_id,
                    triage_result=triage_result
                )
                result['response_result'] = duplicate_response
                result['duplicate_response'] = duplicate_response.get('response_text', '')

                # Créer le brouillon si demandé
                if auto_create_draft and duplicate_response.get('response_text'):
                    try:
                        from config import settings

                        # Récupérer les infos du ticket pour l'email
                        ticket = self.desk_client.get_ticket(ticket_id)
                        to_email = ticket.get('email', '')
                        department = ticket.get('departmentId', '')

                        # Convertir en HTML
                        html_content = duplicate_response['response_text'].replace('\n', '<br>')

                        # Email source selon le département
                        from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                        logger.info(f"📧 Draft DOUBLON: from={from_email}, to={to_email}")

                        self.desk_client.create_ticket_reply_draft(
                            ticket_id=ticket_id,
                            content=html_content,
                            content_type="html",
                            from_email=from_email,
                            to_email=to_email
                        )
                        logger.info("✅ DRAFT DOUBLON → Brouillon créé dans Zoho Desk")
                        result['draft_created'] = True
                        self._mark_brouillon_auto(ticket_id)
                    except Exception as e:
                        logger.error(f"Erreur création brouillon doublon: {e}")
                        result['draft_created'] = False

                result['success'] = True
                return result

            # Check if DUPLICATE_CLARIFICATION (doublon potentiel, clarification nécessaire)
            if triage_result.get('action') == 'DUPLICATE_CLARIFICATION':
                logger.warning("❓ DOUBLON POTENTIEL → Demande de clarification")
                result['workflow_stage'] = 'DUPLICATE_CLARIFICATION'
                result['duplicate_contact_info'] = triage_result.get('duplicate_contact_info', {})
                result['duplicate_type'] = triage_result.get('duplicate_type')

                # Générer une réponse de clarification
                clarification_response = self._generate_duplicate_clarification_response(
                    ticket_id=ticket_id,
                    triage_result=triage_result
                )
                result['response_result'] = clarification_response
                result['clarification_response'] = clarification_response.get('response_text', '')

                # Créer le brouillon si demandé
                if auto_create_draft and clarification_response.get('response_text'):
                    try:
                        from config import settings

                        # Récupérer les infos du ticket pour l'email
                        ticket = self.desk_client.get_ticket(ticket_id)
                        to_email = ticket.get('email', '')

                        # Convertir en HTML
                        html_content = clarification_response['response_text'].replace('\n', '<br>')

                        # Email source selon le département
                        from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                        logger.info(f"📧 Draft CLARIFICATION DOUBLON: from={from_email}, to={to_email}")

                        self.desk_client.create_ticket_reply_draft(
                            ticket_id=ticket_id,
                            content=html_content,
                            content_type="html",
                            from_email=from_email,
                            to_email=to_email
                        )
                        logger.info("✅ DRAFT CLARIFICATION → Brouillon créé dans Zoho Desk")
                        result['draft_created'] = True
                        self._mark_brouillon_auto(ticket_id)
                    except Exception as e:
                        logger.error(f"Erreur création brouillon clarification: {e}")
                        result['draft_created'] = False

                # ================================================================
                # AJOUTER NOTE INTERNE AVEC INFO DOUBLON POTENTIEL
                # (Pour pouvoir récupérer l'info quand le candidat répond)
                # ================================================================
                duplicate_contact_info = triage_result.get('duplicate_contact_info', {})
                if duplicate_contact_info:
                    try:
                        duplicate_deal_id = duplicate_contact_info.get('duplicate_deal_id', '')
                        duplicate_deal_name = duplicate_contact_info.get('duplicate_deal_name', '')
                        duplicate_type = triage_result.get('duplicate_type', 'UNKNOWN')

                        # Stocker aussi l'intention originale pour la reprendre après vérification
                        original_intent = triage_result.get('detected_intent', 'UNKNOWN')

                        note_content = f"""⚠️ DOUBLON POTENTIEL DÉTECTÉ - EN ATTENTE CLARIFICATION

Dossier doublon trouvé par NOM + CODE POSTAL (email/téléphone différents)
• Deal ID: {duplicate_deal_id}
• Deal Name: {duplicate_deal_name}
• Type: {duplicate_type}
• Email doublon: {duplicate_contact_info.get('duplicate_email', 'N/A')}
• Téléphone doublon: {duplicate_contact_info.get('duplicate_phone', 'N/A')}
• Intention originale: {original_intent}

ACTION REQUISE: Attendre réponse candidat pour confirmer s'il s'agit bien du même dossier.
[DUPLICATE_PENDING:{duplicate_deal_id}]"""

                        self.desk_client.add_ticket_comment(
                            ticket_id=ticket_id,
                            content=note_content,
                            is_public=False  # Note interne uniquement
                        )
                        logger.info(f"📝 Note interne ajoutée avec info doublon: {duplicate_deal_id}")
                        result['duplicate_note_added'] = True
                    except Exception as e:
                        logger.error(f"Erreur ajout note doublon: {e}")
                        result['duplicate_note_added'] = False

                result['success'] = True
                return result

            # Check if DUPLICATE_RECOVERABLE (doublon récupérable)
            if triage_result.get('action') == 'DUPLICATE_RECOVERABLE':
                logger.info("🟢 DOUBLON RÉCUPÉRABLE → Proposer reprise d'inscription")
                result['workflow_stage'] = 'DUPLICATE_RECOVERABLE'
                result['duplicate_type'] = triage_result.get('duplicate_type')
                result['duplicate_deals'] = triage_result.get('duplicate_deals', [])

                # ================================================================
                # GESTION DES 2 DEALS GAGNÉ
                # ================================================================
                deal_to_work_on = triage_result.get('deal_to_work_on')
                deal_to_disable = triage_result.get('deal_to_disable')
                already_paid_to_cma = triage_result.get('already_paid_to_cma', False)

                # 1. Mettre à jour EXAM_INCLUS = "Non" sur le deal à désactiver
                if deal_to_disable:
                    try:
                        deal_to_disable_id = deal_to_disable.get('id')
                        logger.info(f"  ❌ Désactivation deal: {deal_to_disable.get('Deal_Name')} (EXAM_INCLUS=Non)")
                        self.crm_client.update_deal(deal_to_disable_id, {'EXAM_INCLUS': 'Non'})
                        result['deal_disabled'] = deal_to_disable_id
                        logger.info(f"  ✅ Deal désactivé: EXAM_INCLUS=Non")
                    except Exception as e:
                        logger.error(f"  ⚠️ Erreur désactivation deal: {e}")

                # 2. Ajouter une note au ticket si frais CMA déjà payés
                if already_paid_to_cma:
                    try:
                        note_content = """⚠️⚠️⚠️ ATTENTION - FRAIS CMA DÉJÀ PAYÉS ⚠️⚠️⚠️

Ce candidat a un dossier déjà payé à la CMA (Dossier Synchronisé ou Refusé CMA).

👉 NE PAS REPAYER LES 241€ DE FRAIS D'EXAMEN

Le dossier peut être repris sans frais supplémentaires auprès de la CMA."""

                        self.desk_client.add_ticket_comment(
                            ticket_id,
                            note_content,
                            is_public=False
                        )
                        result['cma_payment_note_added'] = True
                        logger.warning(f"  📝 Note ajoutée au ticket: FRAIS CMA DÉJÀ PAYÉS")
                    except Exception as e:
                        logger.error(f"  ⚠️ Erreur ajout note frais CMA: {e}")

                # Stocker le deal sur lequel travailler
                result['deal_to_work_on'] = deal_to_work_on

                # Générer une réponse de reprise d'inscription
                recoverable_response = self._generate_duplicate_recoverable_response(
                    ticket_id=ticket_id,
                    triage_result=triage_result
                )
                result['response_result'] = recoverable_response
                result['recoverable_response'] = recoverable_response.get('response_text', '')

                # Créer le brouillon si demandé
                if auto_create_draft and recoverable_response.get('response_text'):
                    try:
                        from config import settings

                        # Récupérer les infos du ticket pour l'email
                        ticket = self.desk_client.get_ticket(ticket_id)
                        to_email = ticket.get('email', '')

                        # Convertir en HTML
                        html_content = recoverable_response['response_text'].replace('\n', '<br>')

                        # Email source selon le département
                        from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                        logger.info(f"📧 Draft REPRISE INSCRIPTION: from={from_email}, to={to_email}")

                        self.desk_client.create_ticket_reply_draft(
                            ticket_id=ticket_id,
                            content=html_content,
                            content_type="html",
                            from_email=from_email,
                            to_email=to_email
                        )
                        logger.info("✅ DRAFT REPRISE → Brouillon créé dans Zoho Desk")
                        result['draft_created'] = True
                        self._mark_brouillon_auto(ticket_id)
                    except Exception as e:
                        logger.error(f"Erreur création brouillon reprise: {e}")
                        result['draft_created'] = False

                result['success'] = True
                return result

            # Check if NEEDS_CLARIFICATION (candidat non trouvé)
            if triage_result.get('action') == 'NEEDS_CLARIFICATION':
                logger.warning("⚠️  CANDIDAT NON TROUVÉ → Demande de clarification")
                result['workflow_stage'] = 'NEEDS_CLARIFICATION'
                result['clarification_reason'] = triage_result.get('clarification_reason')

                # Générer une réponse de clarification
                clarification_response = self._generate_clarification_response(
                    ticket_id=ticket_id,
                    triage_result=triage_result
                )
                result['response_result'] = clarification_response
                result['clarification_response'] = clarification_response.get('response_text', '')

                # Créer le brouillon si demandé
                if auto_create_draft and clarification_response.get('response_text'):
                    try:
                        from config import settings

                        # Récupérer les infos du ticket pour l'email
                        ticket = self.desk_client.get_ticket(ticket_id)
                        to_email = ticket.get('email', '')

                        # Convertir en HTML
                        html_content = clarification_response['response_text'].replace('\n', '<br>')

                        # Email source selon le département
                        from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                        logger.info(f"📧 Draft CLARIFICATION: from={from_email}, to={to_email}")

                        self.desk_client.create_ticket_reply_draft(
                            ticket_id=ticket_id,
                            content=html_content,
                            content_type="html",
                            from_email=from_email,
                            to_email=to_email
                        )
                        logger.info("✅ DRAFT CLARIFICATION → Brouillon créé dans Zoho Desk")
                        result['draft_created'] = True
                        self._mark_brouillon_auto(ticket_id)
                    except Exception as e:
                        logger.error(f"Erreur création brouillon clarification: {e}")
                        result['draft_created'] = False

                result['success'] = True
                return result

            # FEU VERT → Continue
            logger.info("✅ TRIAGE → FEU VERT (continue workflow)")

            # ================================================================
            # NOTE CRM : Ancien deal payé CMA (doublon RECOVERABLE_PAID/REFUS_CMA)
            # Si un ancien deal a déjà été payé à la CMA, ajouter une note interne
            # avec les infos pour payer par chèque avec l'ancien numéro de dossier
            # ================================================================
            old_paid_deal = triage_result.get('old_paid_deal')
            if old_paid_deal:
                try:
                    old_deal_id = old_paid_deal.get('id', '')
                    old_deal_name = old_paid_deal.get('Deal_Name', '')
                    old_evalbox = old_paid_deal.get('Evalbox', 'N/A')
                    old_dup_type = triage_result.get('duplicate_type', '')
                    crm_link = f"https://crm.zoho.com/crm/tab/Potentials/{old_deal_id}"

                    note_content = f"""⚠️ ANCIEN DOSSIER CMA DÉJÀ PAYÉ

Doublon détecté (type: {old_dup_type})
Le candidat a un ancien dossier dont les frais CMA (241€) ont déjà été réglés.

📋 Ancien deal: {old_deal_name}
🔗 Lien: {crm_link}
📊 Evalbox ancien dossier: {old_evalbox}

👉 ACTION REQUISE: Payer le dossier CMA par chèque en indiquant l'ancien numéro de dossier Evalbox ({old_evalbox}).
⚠️ NE PAS REPAYER en ligne les 241€ de frais d'examen."""

                    self.desk_client.add_ticket_comment(
                        ticket_id,
                        note_content,
                        is_public=False
                    )
                    logger.info(f"📝 Note CRM ajoutée: ancien deal payé {old_deal_name} (Evalbox: {old_evalbox})")
                except Exception as e:
                    logger.error(f"⚠️ Erreur ajout note ancien deal payé: {e}")

            # ================================================================
            # DEMANDE_ANNULATION: Détection d'insistance
            # Si on a déjà répondu à une demande d'annulation (thread sortant
            # contenant "non remboursable"), le candidat insiste → escalade
            # GARDE-FOU: Vérifier que le DERNIER message entrant parle encore
            # d'annulation. Si le candidat a accepté la proposition, ce n'est
            # plus une insistance.
            # ================================================================
            detected_intent_go = triage_result.get('detected_intent', '')
            if detected_intent_go == 'DEMANDE_ANNULATION':
                # Vérifier les threads sortants pour détecter une réponse précédente
                from src.utils.text_utils import get_clean_thread_content
                annulation_already_answered = False
                cma_payment_mentioned = False
                candidate_still_wants_annulation = False
                try:
                    threads = self.desk_client.get_all_threads_with_full_content(ticket_id)
                    annulation_markers = ANNULATION_MARKERS
                    cma_markers = CMA_MARKERS

                    # 1. Vérifier si on a déjà répondu à une demande d'annulation
                    for thread in threads:
                        if thread.get('direction') == 'out':
                            thread_content = get_clean_thread_content(thread).lower()
                            if any(marker in thread_content for marker in annulation_markers):
                                annulation_already_answered = True
                                if any(marker in thread_content for marker in cma_markers):
                                    cma_payment_mentioned = True
                                break

                    # 2. GARDE-FOU: Vérifier que le dernier message entrant parle
                    # encore d'annulation/remboursement (pas une acceptation)
                    if annulation_already_answered:
                        annulation_keywords = ANNULATION_KEYWORDS
                        last_inbound = next(
                            (t for t in threads if t.get('direction') == 'in'),
                            None
                        )
                        if last_inbound:
                            last_msg = get_clean_thread_content(last_inbound).lower()
                            # Nettoyer le contenu cité pour ne garder que le message du candidat
                            from business_rules import BusinessRules
                            last_msg = BusinessRules.strip_forwarded_content(last_msg).lower()
                            candidate_still_wants_annulation = any(
                                kw in last_msg for kw in annulation_keywords
                            )
                            if not candidate_still_wants_annulation:
                                logger.info("  ✅ DEMANDE_ANNULATION: Le dernier message du candidat ne mentionne plus l'annulation → pas d'insistance")
                        else:
                            # Pas de message entrant trouvé — ne pas escalader par sécurité
                            candidate_still_wants_annulation = False

                except Exception as e:
                    logger.warning(f"⚠️ Erreur vérification insistance annulation: {e}")

                if annulation_already_answered and candidate_still_wants_annulation:
                    logger.warning("🔴 DEMANDE_ANNULATION: INSISTANCE DÉTECTÉE → Escalade Lamia (priorité HIGH)")
                    # Construire la note selon que la CMA a été payée ou non
                    if cma_payment_mentioned:
                        escalation_note = (
                            "⚠️ INSISTANCE ANNULATION — CMA DÉJÀ PAYÉE\n\n"
                            "Le candidat a déjà reçu une réponse mentionnant le paiement CMA (241€) "
                            "et insiste pour annuler/être remboursé.\n\n"
                            "→ ANNULATION DE L'EXAMEN : demander remboursement à la CMA en urgence.\n"
                            "→ Ticket escaladé en priorité HIGH et assigné à Lamia pour traitement manuel."
                        )
                    else:
                        escalation_note = (
                            "⚠️ INSISTANCE ANNULATION/REMBOURSEMENT\n\n"
                            "Le candidat a déjà reçu une réponse expliquant la politique de non-remboursement "
                            "et insiste pour annuler/être remboursé.\n\n"
                            "→ Ticket escaladé en priorité HIGH et assigné à Lamia pour traitement manuel."
                        )

                    # Mettre à jour le ticket: priorité HIGH + assignation
                    from config import settings as _cfg
                    escalation_agent_id = _cfg.escalation_agent_id
                    if auto_update_ticket:
                        try:
                            self.desk_client.update_ticket(ticket_id, {
                                'priority': 'High',
                                'assigneeId': escalation_agent_id,
                            })
                            self.desk_client.add_ticket_comment(
                                ticket_id,
                                escalation_note,
                                is_public=False
                            )
                            logger.info("  ✅ Ticket mis à jour: priorité HIGH + assigné à Lamia")
                        except Exception as e:
                            logger.error(f"  ❌ Erreur mise à jour ticket: {e}")

                    result['workflow_stage'] = 'ESCALATED_ANNULATION_INSISTENCE'
                    result['escalated_to'] = _cfg.escalation_agent_name
                    result['cma_payment_at_risk'] = cma_payment_mentioned
                    result['success'] = True
                    return result

            # ================================================================
            # STEP 2: AGENT ANALYSTE (6-source data extraction)
            # ================================================================
            logger.info("\n2️⃣  AGENT ANALYSTE - Extraction des données...")
            result['workflow_stage'] = 'ANALYSIS'

            analysis_result = self._run_analysis(ticket_id, triage_result)
            result['analysis_result'] = analysis_result

            # Check for early exit (e.g., VTC classique → DOCS CAB)
            if analysis_result.get('workflow_stage') == 'STOPPED_DOCS_CAB':
                logger.info("🛑 SORTIE ANTICIPÉE → Deal VTC classique transféré vers DOCS CAB")
                result['workflow_stage'] = 'STOPPED_DOCS_CAB'
                result['transferred_to'] = analysis_result.get('transferred_to')
                result['draft_created'] = False
                result['crm_updated'] = False
                result['success'] = True
                return result

            # Check for early exit: no GAGNÉ deal → manual investigation
            if analysis_result.get('workflow_stage') == 'STOPPED_NO_DEAL':
                logger.info("🛑 SORTIE ANTICIPÉE → Pas de deal GAGNÉ, note ajoutée")
                result['workflow_stage'] = 'STOPPED_NO_DEAL'
                result['reason'] = analysis_result.get('reason')
                result['draft_created'] = False
                result['crm_updated'] = False
                result['success'] = True
                return result

            # Check VÉRIFICATION #1: Identifiants ExamenT3P
            examt3p_data = analysis_result.get('examt3p_data', {})
            if examt3p_data.get('should_respond_to_candidate'):
                logger.warning("⚠️  IDENTIFIANTS EXAMENT3P INVALIDES OU MANQUANTS")
                logger.info("→ L'agent rédacteur intégrera la demande d'identifiants dans la réponse globale")
            elif not examt3p_data.get('compte_existe'):
                # Pas de compte ExamT3P = cas normal (compte à créer par CAB)
                # Le State Engine détectera l'état approprié (NO_COMPTE_EXAMT3P, UBER_DOCS_MISSING, etc.)
                logger.info("ℹ️  Pas de compte ExamT3P → compte à créer")
            else:
                logger.info(f"✅ Identifiants validés (source: {examt3p_data.get('credentials_source')})")

            # Check VÉRIFICATION #2: Date examen VTC
            date_examen_vtc_result = analysis_result.get('date_examen_vtc_result', {})
            if date_examen_vtc_result.get('should_include_in_response'):
                logger.warning(f"⚠️  DATE EXAMEN VTC - CAS {date_examen_vtc_result.get('case')}: {date_examen_vtc_result.get('case_description')}")
                logger.info("→ L'agent rédacteur intégrera les infos date examen dans la réponse globale")
            else:
                logger.info(f"✅ Date examen VTC OK (CAS {date_examen_vtc_result.get('case', 'N/A')})")

            logger.info("✅ ANALYSIS → Données extraites")

            # ================================================================
            # CROSS-TICKET INSISTENCE: Détection via ThreadMemory META
            # Si on a déjà traité DEMANDE_ANNULATION sur un ticket précédent
            # (META record avec intent=DEMANDE_ANNULATION), c'est une insistance.
            # ================================================================
            detected_intent_go = triage_result.get('detected_intent', '')
            if detected_intent_go == 'DEMANDE_ANNULATION':
                thread_memory_result = analysis_result.get('thread_memory')
                if thread_memory_result and thread_memory_result.has_history:
                    previous_annulation = any(
                        rec.intent == 'DEMANDE_ANNULATION'
                        for rec in thread_memory_result.previous_records
                    )
                    if previous_annulation:
                        # Cross-ticket insistence detected — check if candidate still wants annulation
                        from src.utils.text_utils import get_clean_thread_content
                        from business_rules import BusinessRules
                        candidate_still_wants = True  # Default: assume yes for cross-ticket
                        try:
                            threads = self.desk_client.get_all_threads_with_full_content(ticket_id)
                            last_inbound = next(
                                (t for t in threads if t.get('direction') == 'in'), None
                            )
                            if last_inbound:
                                last_msg = BusinessRules.strip_forwarded_content(
                                    get_clean_thread_content(last_inbound).lower()
                                ).lower()
                                annulation_keywords = ANNULATION_KEYWORDS
                                candidate_still_wants = any(kw in last_msg for kw in annulation_keywords)
                                if not candidate_still_wants:
                                    logger.info("  ✅ CROSS-TICKET: Le dernier message ne mentionne plus l'annulation → pas d'insistance")
                        except Exception as e:
                            logger.warning(f"  ⚠️ Erreur vérification cross-ticket insistance: {e}")

                        if candidate_still_wants:
                            logger.warning("🔴 CROSS-TICKET INSISTANCE DÉTECTÉE → Escalade Lamia (META précédent avec DEMANDE_ANNULATION)")
                            # Vérifier si CMA déjà payée via les sections de la META précédente
                            last_annulation_meta = next(
                                (rec for rec in reversed(thread_memory_result.previous_records) if rec.intent == 'DEMANDE_ANNULATION'),
                                None
                            )
                            cma_payment_at_risk = last_annulation_meta and 'paiement' in last_annulation_meta.sections

                            if cma_payment_at_risk:
                                escalation_note = (
                                    "⚠️ INSISTANCE ANNULATION CROSS-TICKET — CMA DÉJÀ PAYÉE\n\n"
                                    "Le candidat a déjà reçu une réponse sur un ticket précédent mentionnant le paiement CMA (241€) "
                                    "et revient avec un nouveau ticket pour annuler/être remboursé.\n\n"
                                    "→ ANNULATION DE L'EXAMEN : demander remboursement à la CMA en urgence.\n"
                                    "→ Ticket escaladé en priorité HIGH et assigné à Lamia pour traitement manuel."
                                )
                            else:
                                escalation_note = (
                                    "⚠️ INSISTANCE ANNULATION/REMBOURSEMENT CROSS-TICKET\n\n"
                                    "Le candidat a déjà reçu une réponse sur un ticket précédent expliquant la politique de non-remboursement "
                                    "et revient avec un nouveau ticket pour annuler/être remboursé.\n\n"
                                    "→ Ticket escaladé en priorité HIGH et assigné à Lamia pour traitement manuel."
                                )

                            from config import settings as _cfg
                            escalation_agent_id = _cfg.escalation_agent_id
                            if auto_update_ticket:
                                try:
                                    self.desk_client.update_ticket(ticket_id, {
                                        'priority': 'High',
                                        'assigneeId': escalation_agent_id,
                                    })
                                    self.desk_client.add_ticket_comment(
                                        ticket_id, escalation_note, is_public=False
                                    )
                                    logger.info("  ✅ Ticket mis à jour: priorité HIGH + assigné à Lamia")
                                except Exception as e:
                                    logger.error(f"  ❌ Erreur mise à jour ticket: {e}")

                            result['workflow_stage'] = 'ESCALATED_ANNULATION_INSISTENCE'
                            result['escalated_to'] = _cfg.escalation_agent_name
                            result['cma_payment_at_risk'] = cma_payment_at_risk
                            result['success'] = True
                            return result

            # ================================================================
            # CHECK: Date d'examen passée → Traitement manuel obligatoire
            # ================================================================
            # Si la date d'examen est dans le passé (Zoho CRM ou ExamT3P),
            # on stoppe le workflow pour éviter les incohérences de dates.
            # Un humain doit vérifier: examen passé? résultat? nouvelle inscription?
            detected_intent = triage_result.get('detected_intent', '')
            secondary_intents = triage_result.get('secondary_intents', [])
            all_intents = [detected_intent] + secondary_intents

            date_case = date_examen_vtc_result.get('case')
            # CAS 2, 7 = date d'examen dans le passé → traitement manuel requis
            # NOTE: CAS 8 = clôture passée mais examen FUTUR → on peut traiter automatiquement
            date_passee_cases = [2, 7]

            # Intentions qui savent gérer une date passée → ne pas bloquer
            intents_ok_date_passee = ['RESULTAT_EXAMEN', 'REPORT_DATE', 'FORCE_MAJEURE_REPORT']

            if date_case in date_passee_cases and detected_intent not in intents_ok_date_passee:
                logger.warning(f"🚨 DATE D'EXAMEN PASSÉE DÉTECTÉE (CAS {date_case}) → Traitement manuel requis")

                # Récupérer les infos pour la note
                deal_data = analysis_result.get('deal_data', {})
                contact_data = analysis_result.get('contact_data', {})
                enriched_lookups = analysis_result.get('enriched_lookups', {})
                threads_data = analysis_result.get('threads', [])  # Clé correcte: 'threads'

                prenom = contact_data.get('First_Name', 'Candidat')
                nom = contact_data.get('Last_Name', '')
                date_examen = enriched_lookups.get('date_examen', 'N/A')
                evalbox = deal_data.get('Evalbox', 'N/A')

                # Générer un résumé des échanges via IA
                threads_summary = "Non disponible"
                try:
                    import anthropic
                    from config import settings

                    # Extraire le contenu des threads pour le résumé
                    threads_text = []
                    for t in threads_data[:10]:  # Max 10 derniers threads
                        direction = "CANDIDAT" if t.get('direction') == 'in' else "CAB"
                        content = t.get('content', t.get('summary', ''))[:500]
                        threads_text.append(f"[{direction}]: {content}")

                    if threads_text:
                        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                        summary_response = client.messages.create(
                            model=MODEL_EXTRACTION,
                            max_tokens=300,
                            messages=[{
                                "role": "user",
                                "content": f"""Résume en 3-4 phrases les échanges suivants entre un candidat VTC et CAB Formations.
Focus sur: ce que demande le candidat, les problèmes mentionnés, les actions déjà faites.

ÉCHANGES:
{chr(10).join(threads_text)}

RÉSUMÉ (3-4 phrases, en français):"""
                            }]
                        )
                        threads_summary = summary_response.content[0].text.strip()
                except Exception as e:
                    logger.warning(f"⚠️ Impossible de générer le résumé des échanges: {e}")
                    threads_summary = f"Erreur: {str(e)[:100]}"

                # Récupérer l'état ExamT3P
                examt3p_status = "Non disponible"
                try:
                    statut_dossier = examt3p_data.get('statut_dossier', 'N/A')
                    num_dossier = examt3p_data.get('num_dossier', 'N/A')
                    documents = examt3p_data.get('documents', [])
                    examens = examt3p_data.get('examens', [])
                    paiements = examt3p_data.get('paiements', [])

                    # S'assurer que ce sont des listes
                    if not isinstance(documents, list):
                        documents = []
                    if not isinstance(examens, list):
                        examens = []
                    if not isinstance(paiements, list):
                        paiements = []

                    docs_status = []
                    for doc in documents[:5] if documents else []:
                        if isinstance(doc, dict):
                            doc_name = doc.get('name', doc.get('type', 'Document'))
                            doc_state = doc.get('status', doc.get('state', 'N/A'))
                            docs_status.append(f"• {doc_name}: {doc_state}")

                    exams_status = []
                    for exam in examens[:3] if examens else []:
                        if isinstance(exam, dict):
                            exam_date = exam.get('date', 'N/A')
                            exam_result = exam.get('result', exam.get('status', 'N/A'))
                            exams_status.append(f"• {exam_date}: {exam_result}")

                    nb_docs = len(documents) if documents else 0
                    nb_exams = len(examens) if examens else 0
                    nb_paie = len(paiements) if paiements else 0

                    examt3p_status = f"""<b>Statut dossier:</b> {statut_dossier}<br>
<b>N° dossier:</b> {num_dossier}<br>
<b>Documents ({nb_docs}):</b><br>{'<br>'.join(docs_status) if docs_status else '• Aucun document'}<br>
<b>Examens ({nb_exams}):</b><br>{'<br>'.join(exams_status) if exams_status else '• Aucun examen enregistré'}<br>
<b>Paiements:</b> {nb_paie} enregistré(s)"""
                except Exception as e:
                    logger.warning(f"⚠️ Impossible de récupérer l'état ExamT3P: {e}")
                    examt3p_status = f"Erreur: {str(e)[:100]}"

                # Créer le draft avec note manuelle enrichie
                manual_note = f"""<b>⚠️ À TRAITER MANUELLEMENT - DATE D'EXAMEN PASSÉE</b><br>
<br>
La date d'examen dans Zoho CRM est dans le passé. Le workflow a été stoppé pour éviter d'envoyer des informations incohérentes au candidat.<br>
<br>
<hr>
<b>📋 INFORMATIONS CANDIDAT</b><br>
<b>Nom:</b> {prenom} {nom}<br>
<b>Date d'examen CRM:</b> {date_examen}<br>
<b>Evalbox:</b> {evalbox}<br>
<b>Intention détectée:</b> {detected_intent}<br>
<br>
<hr>
<b>💬 RÉSUMÉ DES ÉCHANGES</b><br>
{threads_summary}<br>
<br>
<hr>
<b>🌐 ÉTAT EXAMT3P</b><br>
{examt3p_status}<br>
<br>
<hr>
<b>🔧 ACTIONS POSSIBLES</b><br>
→ Vérifier si le candidat a passé l'examen<br>
→ Vérifier le résultat si examen passé<br>
→ Proposer une nouvelle inscription si échec/absence<br>
<br>
<i>Ce ticket nécessite une intervention humaine.</i>"""

                # Créer le brouillon
                try:
                    from config import settings
                    ticket = self.desk_client.get_ticket(ticket_id)
                    to_email = ticket.get('email', '')
                    from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                    self.desk_client.create_ticket_reply_draft(
                        ticket_id=ticket_id,
                        content=manual_note,
                        content_type="html",
                        from_email=from_email,
                        to_email=to_email
                    )
                    logger.info("✅ DRAFT MANUEL → Note créée pour traitement humain")
                    result['draft_created'] = True
                    self._mark_brouillon_auto(ticket_id)
                except Exception as e:
                    logger.error(f"❌ Erreur création draft manuel: {e}")
                    result['draft_created'] = False

                result['workflow_stage'] = 'STOPPED_EXAM_DATE_PASSED'
                result['reason'] = f'Date examen passée ({date_examen}) - CAS {date_case} - Traitement manuel requis'
                result['success'] = True
                return result

            # ================================================================
            # STEP 3: AGENT RÉDACTEUR (Response generation with Claude + RAG)
            # ================================================================
            logger.info("\n3️⃣  AGENT RÉDACTEUR - Génération de la réponse...")
            result['workflow_stage'] = 'RESPONSE_GENERATION'

            response_result = self._run_response_generation(
                ticket_id=ticket_id,
                triage_result=triage_result,
                analysis_result=analysis_result
            )
            result['response_result'] = response_result

            # Check if workflow should stop based on scenario
            if response_result.get('should_stop_workflow'):
                logger.warning("🛑 Workflow should STOP based on scenario")
                result['workflow_stage'] = 'STOPPED_AT_SCENARIO'
                result['success'] = True
                return result

            logger.info("✅ RESPONSE → Réponse générée")

            # Note: CRM NOTE sera créée après STEP 6 (après les mises à jour CRM)
            # pour inclure les vraies mises à jour effectuées

            # ================================================================
            # STEP 4: TICKET UPDATE (status, tags)
            # ================================================================
            logger.info("\n4️⃣  TICKET UPDATE - Mise à jour du ticket...")
            result['workflow_stage'] = 'TICKET_UPDATE'

            if auto_update_ticket:
                ticket_updates = self._prepare_ticket_updates(response_result)
                if ticket_updates:
                    self.desk_client.update_ticket(ticket_id, ticket_updates)
                    logger.info(f"✅ TICKET UPDATE → {len(ticket_updates)} champs mis à jour")
                    result['ticket_updated'] = True
            else:
                logger.info("✅ TICKET UPDATE → Préparé (pas d'auto-update)")

            # ================================================================
            # STEP 5: DEAL UPDATE (via CRMUpdateAgent)
            # ================================================================
            logger.info("\n5️⃣  DEAL UPDATE - Mise à jour CRM via CRMUpdateAgent...")
            result['workflow_stage'] = 'DEAL_UPDATE'

            # Check both scenario flag and AI-extracted updates
            ai_updates = response_result.get('crm_updates', {}).copy() if response_result.get('crm_updates') else {}

            # ================================================================
            # GUARD RAIL: Dossier terminé → bloquer les auto-updates
            # ================================================================
            dossier_termine = analysis_result.get('dossier_termine', False)
            if dossier_termine and ai_updates:
                blocked_fields = [k for k in ai_updates if k in ('Date_examen_VTC', 'Session', 'Preference_horaire')]
                if blocked_fields:
                    logger.warning(f"  🛑 GUARD RAIL: Dossier terminé (Resultat={analysis_result.get('resultat_raw', '?')}) → CRM updates bloqués: {blocked_fields}")
                    for field in blocked_fields:
                        del ai_updates[field]

            # D-8: Si deadline passée avant paiement, injecter la nouvelle date d'examen
            date_examen_vtc_result = analysis_result.get('date_examen_vtc_result', {})

            # GUARD RAIL suite: bloquer auto-reschedule/auto-assign pour dossiers terminés
            if dossier_termine:
                if date_examen_vtc_result.get('deadline_passed_reschedule'):
                    logger.warning("  🛑 GUARD RAIL: Dossier terminé → CAS 8 auto-reschedule BLOQUÉ")
                    date_examen_vtc_result['deadline_passed_reschedule'] = False
                if date_examen_vtc_result.get('auto_assigned_exam_date'):
                    logger.warning("  🛑 GUARD RAIL: Dossier terminé → auto_assigned_exam_date BLOQUÉ")
                    date_examen_vtc_result['auto_assigned_exam_date'] = None

            if date_examen_vtc_result.get('deadline_passed_reschedule') and date_examen_vtc_result.get('new_exam_date'):
                new_date = date_examen_vtc_result['new_exam_date']
                logger.info(f"  📅 D-8: Deadline passée → inscription sur prochaine date: {new_date}")
                ai_updates['Date_examen_VTC'] = new_date
                result['deadline_passed_reschedule'] = True
                result['new_exam_date'] = new_date

            # CONFIRMATION_DATE_EXAMEN: Si le candidat a confirmé une nouvelle date d'examen
            if analysis_result.get('confirmed_exam_date_valid') and analysis_result.get('confirmed_exam_date_id'):
                confirmed_date_id = analysis_result['confirmed_exam_date_id']
                confirmed_date = analysis_result.get('confirmed_new_exam_date', '')
                logger.info(f"  📅 CONFIRMATION_DATE_EXAMEN: Date confirmée → {confirmed_date} (ID: {confirmed_date_id})")
                ai_updates['Date_examen_VTC'] = confirmed_date_id
                result['exam_date_confirmed_update'] = True
                result['confirmed_exam_date'] = confirmed_date

            # IMPLICIT DATE REPOSITIONING: Repositionnement automatique de la date d'examen
            if date_examen_vtc_result.get('implicit_date_repositioning'):
                engagement = date_examen_vtc_result.get('engagement_level', {})
                engagement_level = engagement.get('level', -1)
                can_reposition = engagement.get('can_reposition', False)

                if can_reposition:
                    # Utiliser la date cible chargée pendant l'enrichissement
                    new_date_id = date_examen_vtc_result.get('repositioning_target_date_id')
                    new_date_str = date_examen_vtc_result.get('repositioning_target_date_str', '')

                    if new_date_id:
                        logger.info(f"  📅 REPOSITIONNEMENT IMPLICITE: Date → {new_date_str} (ID: {new_date_id})")
                        ai_updates['Date_examen_VTC'] = new_date_id
                        result['implicit_date_repositioned'] = True
                        result['repositioned_exam_date'] = new_date_str
                    else:
                        logger.warning(f"  ⚠️ REPOSITIONNEMENT IMPLICITE: Pas de date cible trouvée")

                    # Note interne sera créée APRÈS le draft (STEP 7)
                    result['repositioning_note_pending'] = True
                    result['repositioning_note_data'] = {
                        'engagement_level': engagement_level,
                        'month_name': date_examen_vtc_result.get('requested_month_name', ''),
                        'current_date': date_examen_vtc_result.get('date_examen_info', {}).get('Date_Examen', ''),
                        'new_date_str': new_date_str,
                        'description': engagement.get('description', ''),
                    }

            # CONFIRMATION_SESSION: Si le candidat a confirmé sa session avec des dates
            if analysis_result.get('session_confirmed') and analysis_result.get('matched_session_id'):
                matched_session_id = analysis_result['matched_session_id']
                matched_session_name = analysis_result.get('matched_session_name', '')
                matched_session_type = analysis_result.get('matched_session_type', '')
                logger.info(f"  📚 CONFIRMATION_SESSION: Session confirmée → {matched_session_name} (ID: {matched_session_id})")
                ai_updates['Session'] = matched_session_id
                if matched_session_type:
                    ai_updates['Preference_horaire'] = matched_session_type
                result['session_confirmed_update'] = True

            # CAB ERROR CORRECTION: Si on a confirmé une erreur et trouvé la session correcte
            if analysis_result.get('cab_error_corrected') and analysis_result.get('cab_error_corrected_session_id'):
                corrected_session_id = analysis_result['cab_error_corrected_session_id']
                corrected_session_name = analysis_result.get('cab_error_corrected_session_name', '')
                corrected_session_type = analysis_result.get('cab_error_corrected_session_type', '')
                logger.info(f"  📚 CAB ERROR CORRECTION: Session corrigée → {corrected_session_name} (ID: {corrected_session_id})")
                ai_updates['Session'] = corrected_session_id
                if corrected_session_type:
                    ai_updates['Preference_horaire'] = corrected_session_type
                result['cab_error_correction_update'] = True

            # SESSION YEAR ERROR CORRECTION: Erreur d'année (mars 2024 → mars 2026)
            if analysis_result.get('session_year_error_corrected') and analysis_result.get('session_year_error_corrected_id'):
                corrected_session_id = analysis_result['session_year_error_corrected_id']
                corrected_session_name = analysis_result.get('session_year_error_corrected_name', '')
                corrected_session_type = analysis_result.get('session_year_error_corrected_type', '')
                logger.info(f"  📚 SESSION YEAR ERROR CORRECTION: Session corrigée → {corrected_session_name} (ID: {corrected_session_id})")
                ai_updates['Session'] = corrected_session_id
                if corrected_session_type:
                    ai_updates['Preference_horaire'] = corrected_session_type
                result['session_year_error_correction_update'] = True

            has_ai_updates = bool(ai_updates)
            scenario_requires_update = response_result.get('requires_crm_update')

            if has_ai_updates or scenario_requires_update:
                if scenario_requires_update:
                    logger.info(f"Champs à updater (scénario): {response_result.get('crm_update_fields', [])}")
                if has_ai_updates:
                    logger.info(f"Champs à updater: {ai_updates}")

                if auto_update_crm and analysis_result.get('deal_id'):
                    # Utiliser CRMUpdateAgent pour centraliser la logique
                    crm_update_result = self.crm_update_agent.update_from_ticket_response(
                        deal_id=analysis_result['deal_id'],
                        ai_updates=ai_updates,
                        deal_data=analysis_result.get('deal_data', {}),
                        session_data=analysis_result.get('session_data', {}),
                        ticket_id=ticket_id
                    )

                    if crm_update_result.get('updates_applied'):
                        logger.info(f"✅ DEAL UPDATE → {len(crm_update_result['updates_applied'])} champs mis à jour: {list(crm_update_result['updates_applied'].keys())}")
                        result['crm_updated'] = True

                    if crm_update_result.get('updates_blocked'):
                        logger.warning(f"🔒 DEAL UPDATE → {len(crm_update_result['updates_blocked'])} champs bloqués (règles métier)")
                        result['crm_updates_blocked'] = crm_update_result['updates_blocked']

                    if crm_update_result.get('errors'):
                        for error in crm_update_result['errors']:
                            logger.warning(f"⚠️ DEAL UPDATE: {error}")
                        result['crm_update_error'] = '; '.join(crm_update_result['errors'])

                    if not crm_update_result.get('updates_applied') and not crm_update_result.get('updates_blocked'):
                        logger.info("✅ DEAL UPDATE → Aucune mise à jour après mapping")
                else:
                    logger.info("✅ DEAL UPDATE → Préparé (pas d'auto-update)")
                    crm_update_result = {}
            else:
                logger.info("✅ DEAL UPDATE → Non requis pour ce scénario")
                crm_update_result = {}

            # Stocker les mises à jour appliquées pour la note CRM
            result['crm_updates_applied'] = crm_update_result.get('updates_applied', {}) if crm_update_result else {}

            # ================================================================
            # STEP 6: CRM NOTE (après les mises à jour CRM)
            # ================================================================
            logger.info("\n6️⃣  CRM NOTE - Création de la note CRM...")
            result['workflow_stage'] = 'CRM_NOTE'

            crm_note = self._create_crm_note(
                ticket_id=ticket_id,
                triage_result=triage_result,
                analysis_result=analysis_result,
                response_result=response_result,
                crm_updates_applied=result.get('crm_updates_applied', {})
            )
            result['crm_note'] = crm_note

            if auto_update_crm and analysis_result.get('deal_id'):
                # Add note to deal
                self.crm_client.add_deal_note(
                    deal_id=analysis_result['deal_id'],
                    note_title="Note automatique - Ticket DOC",
                    note_content=crm_note
                )
                logger.info("✅ CRM NOTE → Note ajoutée au deal")
            else:
                logger.info("✅ CRM NOTE → Note générée (pas d'auto-update)")

            # ================================================================
            # STEP 7: REPLY DELIVERY - Envoi ou brouillon
            # ================================================================
            logger.info("\n7️⃣  REPLY DELIVERY - Envoi ou brouillon...")
            result['workflow_stage'] = 'REPLY_DELIVERY'

            if auto_send or auto_create_draft:
                # Convertir markdown en HTML pour des liens cliquables
                draft_content = response_result.get('response_text', '')
                import re
                html_content = draft_content

                # Convertir liens markdown [text](url) → <a href="url">text</a>
                html_content = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', html_content)
                # Convertir **gras** → <strong>gras</strong>
                html_content = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', html_content)
                # Convertir ## headers → <h3>
                html_content = re.sub(r'^## (.+)$', r'<h3>\1</h3>', html_content, flags=re.MULTILINE)
                # Convertir sauts de ligne en <br>
                html_content = html_content.replace('\n\n', '</p><p>').replace('\n', '<br>')
                # Wrapper dans des paragraphes
                html_content = f'<p>{html_content}</p>'

                try:
                    # Récupérer from_email selon le département
                    from config import settings

                    # Récupérer le ticket pour le département et l'email destinataire
                    ticket = self.desk_client.get_ticket(ticket_id)
                    department = ticket.get('departmentId') or ticket.get('department', {}).get('name', '')

                    # Utiliser l'email du client extrait (ex: forward) si disponible
                    # Sinon fallback sur l'email du ticket
                    to_email = triage_result.get('email_searched') or ticket.get('email')

                    # Mapping département → email expéditeur
                    dept_email_map = {
                        'DOC': settings.zoho_desk_email_doc,
                        'Contact': settings.zoho_desk_email_contact,
                        'Comptabilité': settings.zoho_desk_email_compta,
                    }

                    # Déterminer l'email selon le département
                    from_email = dept_email_map.get(department) or settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                    if auto_send:
                        # Auto-send: vérifier les guard rails avant envoi
                        can_send, fallback_reason = self._can_auto_send(response_result, triage_result)

                        if can_send:
                            logger.info(f"📧 Send: from={from_email}, to={to_email}, dept={department}")
                            self.desk_client.send_ticket_reply(
                                ticket_id=ticket_id,
                                content=html_content,
                                content_type="html",
                                from_email=from_email,
                                to_email=to_email
                            )
                            logger.info("✅ REPLY DELIVERY → Réponse envoyée directement")
                            result['reply_sent'] = True
                            result['delivery_method'] = 'sent'
                            # Fermer le ticket après envoi réussi
                            try:
                                self.desk_client.update_ticket(ticket_id, {'status': 'Closed'})
                                logger.info("✅ Ticket fermé après envoi auto-send")
                            except Exception as close_err:
                                logger.warning(f"⚠️ Impossible de fermer le ticket: {close_err}")
                        else:
                            # Fallback: créer un draft au lieu d'envoyer
                            logger.warning(f"⚠️ Auto-send bloqué ({fallback_reason}) → fallback draft")
                            self.desk_client.create_ticket_reply_draft(
                                ticket_id=ticket_id,
                                content=html_content,
                                content_type="html",
                                from_email=from_email,
                                to_email=to_email
                            )
                            logger.info("✅ REPLY DELIVERY → Brouillon créé (fallback)")
                            result['draft_created'] = True
                            result['delivery_method'] = 'draft'
                            result['send_fallback_reason'] = fallback_reason
                            self._mark_brouillon_auto(ticket_id)
                    else:
                        # Mode draft classique
                        logger.info(f"📧 Draft: from={from_email}, to={to_email}, dept={department}")
                        self.desk_client.create_ticket_reply_draft(
                            ticket_id=ticket_id,
                            content=html_content,
                            content_type="html",
                            from_email=from_email,
                            to_email=to_email
                        )
                        logger.info("✅ REPLY DELIVERY → Brouillon créé dans Zoho Desk")
                        result['draft_created'] = True
                        result['delivery_method'] = 'draft'
                        self._mark_brouillon_auto(ticket_id)

                except Exception as delivery_error:
                    if auto_send and not result.get('draft_created') and not result.get('reply_sent'):
                        # Si l'envoi direct a échoué, tenter un fallback draft
                        logger.warning(f"⚠️ Envoi échoué: {delivery_error} → tentative fallback draft")
                        try:
                            self.desk_client.create_ticket_reply_draft(
                                ticket_id=ticket_id,
                                content=html_content,
                                content_type="html",
                                from_email=from_email,
                                to_email=to_email
                            )
                            result['draft_created'] = True
                            result['delivery_method'] = 'draft'
                            result['send_fallback_reason'] = f'api_error: {delivery_error}'
                            self._mark_brouillon_auto(ticket_id)
                            logger.info("✅ Fallback draft créé après échec envoi")
                        except Exception as fallback_error:
                            logger.warning(f"⚠️ Fallback draft aussi échoué: {fallback_error}")
                            logger.info("📋 La réponse est disponible ci-dessus pour copier-coller manuellement")
                            result['delivery_method'] = 'none'
                            result['send_fallback_reason'] = f'api_error: {delivery_error}'
                    else:
                        logger.warning(f"⚠️ Impossible de créer le draft dans Zoho Desk: {delivery_error}")
                        logger.info("📋 La réponse est disponible ci-dessus pour copier-coller manuellement")
                        result['draft_created'] = False

                # Note: la note CRM consolidée est créée au STEP 4

                # Note interne repositionnement implicite (après le draft/envoi)
                if result.get('repositioning_note_pending') and auto_update_crm:
                    note_data = result.get('repositioning_note_data', {})
                    try:
                        if note_data.get('engagement_level') == 0:
                            note = (
                                f"⚠️ REPOSITIONNEMENT DATE EXAMEN (auto)\n\n"
                                f"Candidat demande formation en {note_data['month_name']}, examen actuel le {note_data['current_date']}.\n"
                                f"Engagement: niveau 0 — Pas de compte ExamT3P.\n\n"
                                f"👉 Date d'examen repositionnée automatiquement vers {note_data['new_date_str']}."
                            )
                        else:
                            note = (
                                f"⚠️ REPOSITIONNEMENT DATE EXAMEN\n\n"
                                f"Candidat demande formation en {note_data['month_name']}, examen actuel le {note_data['current_date']}.\n"
                                f"Engagement: niveau {note_data['engagement_level']} — {note_data['description']}\n\n"
                                f"👉 Action: Envoyer message CMA pour changement de date vers {note_data['month_name']} ({note_data['new_date_str']})"
                            )
                        self.desk_client.add_ticket_comment(ticket_id, note, is_public=False)
                        logger.info("  📝 Note repositionnement ajoutée")
                    except Exception as e:
                        logger.warning(f"Erreur ajout note repositionnement: {e}")

                # Note interne faux Refusé CMA (après le draft/envoi)
                if date_examen_vtc_result.get('faux_refus_cma'):
                    identifiant = examt3p_data.get('identifiant', examt3p_data.get('email', ''))
                    mdp = examt3p_data.get('mot_de_passe', '')
                    faux_refus_note = (
                        f"⚠️ INCOHÉRENCE ExamT3P - Faux Refusé CMA\n"
                        f"Statut ExamT3P: Incomplet, mais 0 pièce avec statut REFUSÉ.\n"
                        f"Les anciennes pièces refusées n'ont pas été supprimées.\n\n"
                        f"ACTION REQUISE: Se connecter sur exament3p.fr\n"
                        f"→ Identifiant: {identifiant}\n"
                        f"→ Mot de passe: {mdp}\n"
                        f"Supprimer les pièces qui étaient en refus pour ajuster le dossier."
                    )
                    try:
                        self.desk_client.add_ticket_comment(ticket_id, faux_refus_note, is_public=False)
                        logger.info("  📝 Note interne ajoutée: faux Refusé CMA")
                    except Exception as e:
                        logger.warning(f"  ⚠️ Erreur ajout note faux refus: {e}")
            else:
                logger.info("✅ REPLY DELIVERY → Préparé (pas d'auto-create/send)")

            # ================================================================
            # STEP 8: FINAL VALIDATION
            # ================================================================
            logger.info("\n8️⃣  FINAL VALIDATION - Vérifications finales...")
            result['workflow_stage'] = 'COMPLETED'

            validation_errors = []

            # Check mandatory blocks compliance
            for scenario_id, validation in response_result.get('validation', {}).items():
                if not validation['compliant']:
                    validation_errors.append(
                        f"Scenario {scenario_id}: missing {validation['missing_blocks']}"
                    )
                if validation['forbidden_terms_found']:
                    validation_errors.append(
                        f"Forbidden terms used: {validation['forbidden_terms_found']}"
                    )

            if validation_errors:
                logger.warning(f"⚠️  Validation warnings: {validation_errors}")
                result['errors'].extend(validation_errors)
            else:
                logger.info("✅ VALIDATION → Tous les contrôles passés")

            # ================================================================
            # STEP 8b: TRANSFER TO DOCS CAB (si VTC hors partenariat)
            # ================================================================
            # Les deals VTC classiques (Amount != 20€) doivent être transférés
            # vers DOCS CAB après création du draft
            deal_amount = analysis_result.get('deal_data', {}).get('Amount', 0)
            is_vtc_hors_partenariat = (deal_amount != 0 and deal_amount != UBER_OFFER_AMOUNT)

            if is_vtc_hors_partenariat and result.get('draft_created') and auto_update_ticket:
                logger.info("\n8️⃣b TRANSFER DOCS CAB - Deal VTC classique (hors partenariat)...")
                try:
                    self.desk_client.move_ticket_to_department(ticket_id, "DOCS CAB")
                    logger.info("✅ TRANSFER → Ticket transféré vers DOCS CAB")
                    result['transferred_to'] = "DOCS CAB"
                except Exception as transfer_error:
                    logger.warning(f"⚠️ Impossible de transférer vers DOCS CAB: {transfer_error}")
                    result['transfer_error'] = str(transfer_error)
            elif is_vtc_hors_partenariat and not auto_update_ticket:
                logger.info("\n8️⃣b TRANSFER DOCS CAB → Préparé (pas d'auto-update)")
                result['transfer_prepared'] = "DOCS CAB"

            result['success'] = True
            logger.info("\n" + "=" * 80)
            logger.info("✅ WORKFLOW COMPLET TERMINÉ")
            logger.info("=" * 80)

            return result

        except Exception as e:
            logger.error(f"❌ Error in workflow: {e}")
            result['errors'].append(str(e))
            import traceback
            traceback.print_exc()
            return result

    def _run_triage(self, ticket_id: str, auto_transfer: bool = True) -> Dict:
        """
        Run AGENT TRIEUR logic with AI-based triage.

        Uses TriageAgent (Claude) for intelligent context-aware routing:
        - Comprend le SENS du message, pas juste les mots-clés
        - Évite les faux positifs ("j'ai envoyé mes documents" ≠ Refus CMA)
        - Deal-based routing (Uber €20, CMA, etc.)
        - Evalbox status (Refusé CMA, Documents manquants, etc.)

        Args:
            ticket_id: Ticket to triage
            auto_transfer: If True, automatically transfer ticket to target department

        Returns:
            {
                'action': 'GO' | 'ROUTE' | 'SPAM',
                'target_department': str (if ROUTE),
                'reason': str,
                'transferred': bool (if auto_transfer and ROUTE)
            }
        """
        from src.utils.text_utils import get_clean_thread_content

        # Get ticket details
        ticket = self.desk_client.get_ticket(ticket_id)
        subject = ticket.get('subject', '')
        current_department = ticket.get('departmentId') or ticket.get('department', {}).get('name', 'DOC')

        # Get threads for content analysis
        # API returns newest first, but we want the most MEANINGFUL customer message
        # Skip: feedback/ratings, very short messages, "lisez mon mail précédent"
        threads = self.desk_client.get_all_threads_with_full_content(ticket_id)
        min_meaningful_length = 80  # Ignore very short messages

        # Patterns to skip (feedback, automated, follow-ups asking to read previous)
        skip_patterns = SKIP_PATTERNS

        # Collecter les messages récents du candidat (pour avoir le contexte complet)
        # Ex: candidat envoie "je choisis cours du jour" puis "confirmez les dates svp"
        # On doit voir les deux messages pour comprendre l'intention
        recent_candidate_messages = []
        first_cab_response_seen = False

        for thread in threads:
            direction = thread.get('direction')
            status = thread.get('status', '')

            # Ignorer les drafts (status: DRAFT) - ce ne sont pas des réponses envoyées
            if status == 'DRAFT':
                continue

            # Arrêter si on trouve une réponse CAB ENVOYÉE
            if direction == 'out':
                first_cab_response_seen = True
                continue

            # Si on a déjà vu une réponse CAB envoyée, on arrête (messages trop vieux)
            if first_cab_response_seen:
                break

            if direction == 'in':
                content = get_clean_thread_content(thread)

                # Strip quoted/forwarded content to avoid contamination
                # (e.g., our own "Mot de passe", "@email", "241€" in Gmail/Outlook quotes)
                try:
                    from business_rules import BusinessRules
                    content = BusinessRules.strip_forwarded_content(content)
                except Exception:
                    pass  # Graceful degradation

                content_lower = content.lower()

                # Skip feedback/automated messages
                if any(pattern in content_lower for pattern in skip_patterns):
                    continue

                # Collecter ce message s'il est significatif
                if len(content) >= min_meaningful_length or not recent_candidate_messages:
                    recent_candidate_messages.append(content)

        # Combiner les messages récents (du plus récent au plus ancien)
        # Limite: 3 messages max pour éviter trop de contexte
        last_thread_content = "\n---\n".join(recent_candidate_messages[:3]) if recent_candidate_messages else ""

        # Compter les threads entrants (pour éligibilité auto-send)
        incoming_thread_count = sum(1 for t in threads if isinstance(t, dict) and t.get('direction') == 'in' and t.get('status') != 'DRAFT')

        # Default result
        triage_result = {
            'action': 'GO',
            'target_department': 'DOC',
            'reason': 'Ticket reste dans DOC',
            'transferred': False,
            'current_department': current_department,
            'method': 'default',
            'ticket_subject': subject,
            'customer_message': last_thread_content,
            'incoming_thread_count': incoming_thread_count,
        }

        # Rule #1: SPAM detection (simple keywords - pas besoin d'IA)
        spam_keywords = SPAM_KEYWORDS
        combined_content = (subject + ' ' + last_thread_content).lower()
        if any(kw in combined_content for kw in spam_keywords):
            triage_result['action'] = 'SPAM'
            triage_result['reason'] = 'Spam détecté'
            triage_result['method'] = 'spam_filter'
            logger.info("🚫 SPAM détecté → Clôturer sans réponse")
            return triage_result

        # Rule #1.5: CMA notification detection (dossier incomplet / validé)
        # Les CMA envoient des notifications sur l'état des dossiers ExamT3P.
        # On vérifie le FROM du thread le plus récent (pas ticket.email qui peut être
        # un forward client). Si le thread le plus récent est d'un client → pas CMA.
        cma_email_domains = CMA_EMAIL_DOMAINS

        # Identifier le FROM du thread le plus récent (= premier dans la liste, API newest first)
        most_recent_from = ''
        for _th in threads:
            if _th.get('direction') == 'in' and _th.get('status') != 'DRAFT':
                most_recent_from = (_th.get('fromEmailAddress') or '').lower()
                break

        is_cma_sender = bool(most_recent_from) and any(domain in most_recent_from for domain in cma_email_domains)

        if is_cma_sender:
            import re as _re
            cma_type = None

            # IMPORTANT: Nettoyer le contenu pour ne garder que le dernier message CMA
            # Les blockquotes/citations d'anciens messages peuvent contenir "incomplet" d'un ancien échange
            cma_thread = next((_th for _th in threads if _th.get('direction') == 'in' and _th.get('status') != 'DRAFT'), None)
            cleaned_cma_content = get_clean_thread_content(cma_thread).lower() if cma_thread else last_thread_content.lower()
            # Couper au premier marqueur de citation (réponses précédentes)
            reply_markers = REPLY_MARKERS
            for marker in reply_markers:
                pos = cleaned_cma_content.find(marker)
                if pos > 50:  # Must be after some real content
                    cleaned_cma_content = cleaned_cma_content[:pos]
                    break
            cma_combined = (subject + ' ' + cleaned_cma_content).lower()

            # Exclusion: emails batch listant PLUSIEURS candidats (pas une notification individuelle)
            batch_exclusion = BATCH_EXCLUSION
            is_batch = any(excl in cma_combined for excl in batch_exclusion)

            # Pattern DOSSIER INCOMPLET
            incomplet_patterns = [
                r'dossier.*incomplet',
                r"s'avère incomplet",
                r'toujours en incomplet',
            ]
            if not is_batch and any(_re.search(p, cma_combined) for p in incomplet_patterns):
                cma_type = 'DOSSIER_INCOMPLET'

            # Pattern DOSSIER VALIDÉ / COMPLET
            valide_patterns = [
                r'dossier.*est complet',
                r'dossier.*a été validé',
                r'confirmons que votre dossier.*complet',
            ]
            if not cma_type and any(_re.search(p, cma_combined) for p in valide_patterns):
                cma_type = 'DOSSIER_VALIDE'

            if cma_type:
                triage_result['action'] = 'CMA_NOTIFICATION'
                triage_result['cma_type'] = cma_type
                triage_result['reason'] = f'Email CMA ({most_recent_from}) - {cma_type}'
                triage_result['method'] = 'cma_notification_filter'
                logger.info(f"🏛️ CMA NOTIFICATION ({cma_type}) détectée → Clôture automatique")
                return triage_result
            else:
                # CMA mail mais pas incomplet/validé → rester dans DOC, ne PAS router vers Contact
                triage_result['action'] = 'CMA_OTHER'
                triage_result['reason'] = f'Email CMA ({most_recent_from}) - contenu non catégorisé, reste dans DOC'
                triage_result['method'] = 'cma_notification_filter'
                logger.info(f"🏛️ Email CMA détecté ({most_recent_from}) mais pas dossier incomplet/validé → reste dans DOC (pas de route Contact)")
                return triage_result

        # Rule #2: Get deals from CRM for context
        linking_result = self.deal_linker.process({"ticket_id": ticket_id})
        all_deals = linking_result.get('all_deals', [])
        selected_deal = linking_result.get('selected_deal') or linking_result.get('deal') or {}

        # TOUJOURS stocker l'email utilisé pour la recherche (pour destinataire brouillon si forward)
        # Cet email peut être différent de ticket.email si c'est un forward interne
        if linking_result.get('email'):
            triage_result['email_searched'] = linking_result.get('email')

        # Rule #2.4bis: DEMANDES RGPD - Priorité sur DUPLICATE_UBER
        # Les demandes de suppression de données doivent être transférées au référent RGPD
        detected_intent = triage_result.get('detected_intent', '')
        if detected_intent == 'DEMANDE_SUPPRESSION_DONNEES':
            logger.info("🔒 DEMANDE RGPD DÉTECTÉE → Routage vers Contact + note référent RGPD")
            from config import settings as _cfg_rgpd
            rgpd_email = _cfg_rgpd.rgpd_referent_email
            triage_result['action'] = 'ROUTE'
            triage_result['target_department'] = 'Contact'
            triage_result['reason'] = 'Demande RGPD (suppression données) - Transférer au référent RGPD'
            triage_result['rgpd_referent'] = rgpd_email
            # Ajouter une note sur le ticket
            try:
                self.desk_client.add_ticket_comment(
                    ticket_id,
                    f"⚠️ DEMANDE RGPD - À TRANSFÉRER\n\nCe ticket contient une demande de suppression de données (article 17 RGPD).\n\n👉 Transférer à : {rgpd_email} (Référent RGPD)",
                    is_public=False
                )
                logger.info("  ✅ Note RGPD ajoutée sur le ticket")
            except Exception as e:
                logger.warning(f"  ⚠️ Impossible d'ajouter la note RGPD: {e}")
            return triage_result

        # ================================================================
        # RÈGLE CRITIQUE: NON-UBER REGISTRATION REQUESTS
        # Si le candidat demande une formation avec un financement NON-UBER
        # (CPF, France Travail/KAIROS, financement personnel, etc.),
        # on doit router vers Contact SANS appliquer la logique doublon Uber.
        # ================================================================
        # Keywords indiquant une demande d'inscription NON-UBER
        non_uber_registration_keywords = NON_UBER_REGISTRATION

        # Nettoyer les métadonnées SalesIQ avant le check keywords
        # (les chats SalesIQ incluent "Informations sur le visiteur" suivi de
        # données techniques comme "prise en charge de java" qui causent des faux positifs)
        clean_thread_content = last_thread_content
        salesiq_markers = SALESIQ_MARKERS
        for marker in salesiq_markers:
            marker_idx = clean_thread_content.lower().find(marker)
            if marker_idx != -1:
                clean_thread_content = clean_thread_content[:marker_idx].strip()
                break

        content_to_check = (subject + ' ' + clean_thread_content).lower()
        is_non_uber_registration = any(kw in content_to_check for kw in non_uber_registration_keywords)

        # Si c'est une demande non-Uber ET il y a un doublon potentiel → Router vers Contact
        # (ignorer la logique doublon Uber, ce n'est pas pertinent)
        has_duplicate = linking_result.get('has_duplicate_uber_offer') or linking_result.get('needs_duplicate_confirmation')

        if is_non_uber_registration and has_duplicate:
            logger.info(f"📋 DEMANDE NON-UBER détectée (CPF/France Travail/etc.) + doublon existant → Router vers Contact")
            logger.info(f"   → Ignorer logique doublon Uber car intention différente")
            triage_result['action'] = 'ROUTE'
            triage_result['target_department'] = 'Contact'
            triage_result['reason'] = "Candidat avec dossier Uber existant mais demande formation non-Uber (CPF/France Travail/autre financement)"
            triage_result['method'] = 'non_uber_registration_routing'
            triage_result['has_existing_uber_deal'] = True
            triage_result['selected_deal'] = selected_deal

            # Note interne pour le conseiller Contact
            deal_name = selected_deal.get('Deal_Name', 'N/A') if selected_deal else 'N/A'
            internal_note = (
                f"⚡ DEMANDE NON-UBER — Candidat avec dossier Uber 20€ existant ({deal_name}).\n"
                f"Sa demande actuelle concerne un autre financement (CPF / France Travail / fi perso).\n"
                f"Le dossier Uber 20€ n'est pas concerné → à traiter comme un nouveau prospect."
            )
            try:
                self.desk_client.add_ticket_comment(ticket_id, internal_note, is_public=False)
                logger.info("  📝 Note interne ajoutée (demande non-Uber)")
            except Exception as e:
                logger.warning(f"  ⚠️ Erreur ajout note interne: {e}")

            # Auto-transfer vers Contact
            if auto_transfer:
                try:
                    logger.info(f"🔄 Transfert automatique vers Contact...")
                    transfer_success = self.dispatcher._reassign_ticket(ticket_id, 'Contact')
                    if transfer_success:
                        logger.info(f"✅ Ticket transféré vers Contact")
                        triage_result['transferred'] = True
                except Exception as e:
                    logger.error(f"Erreur transfert: {e}")

            return triage_result

        # Si c'est une demande non-Uber mais PAS de doublon → Router vers Contact aussi
        # (le département DOC ne gère que les dossiers Uber 20€)
        if is_non_uber_registration and not all_deals:
            logger.info(f"📋 DEMANDE NON-UBER détectée + pas de dossier → Router vers Contact (prospect)")
            triage_result['action'] = 'ROUTE'
            triage_result['target_department'] = 'Contact'
            triage_result['reason'] = "Demande formation non-Uber (CPF/France Travail/autre) - prospect à traiter manuellement"
            triage_result['method'] = 'non_uber_prospect_routing'

            # Note interne pour le conseiller Contact
            internal_note = (
                "⚡ PROSPECT NON-UBER — Pas de dossier Uber 20€ existant.\n"
                "Demande de formation via autre financement (CPF / France Travail / fi perso).\n"
                "À traiter comme un nouveau prospect."
            )
            try:
                self.desk_client.add_ticket_comment(ticket_id, internal_note, is_public=False)
                logger.info("  📝 Note interne ajoutée (prospect non-Uber)")
            except Exception as e:
                logger.warning(f"  ⚠️ Erreur ajout note interne: {e}")

            if auto_transfer:
                try:
                    transfer_success = self.dispatcher._reassign_ticket(ticket_id, 'Contact')
                    if transfer_success:
                        logger.info(f"✅ Ticket transféré vers Contact")
                        triage_result['transferred'] = True
                except Exception as e:
                    logger.error(f"Erreur transfert: {e}")

            return triage_result

        # ================================================================
        # À partir d'ici: le candidat demande quelque chose lié à Uber 20€
        # → La logique doublon s'applique
        # ================================================================

        # Rule #2.4b: VÉRIFICATION DOUBLON POTENTIEL (CLARIFICATION NÉCESSAIRE)
        # Si on détecte un doublon par nom+CP mais avec email/téléphone différents,
        # on demande confirmation au candidat pour éviter les homonymes
        if linking_result.get('needs_duplicate_confirmation'):
            duplicate_info = linking_result.get('duplicate_contact_info', {})
            duplicate_type = linking_result.get('duplicate_type')
            logger.info(f"❓ DOUBLON POTENTIEL - Clarification nécessaire (type: {duplicate_type})")

            triage_result['action'] = 'DUPLICATE_CLARIFICATION'
            triage_result['reason'] = "Doublon potentiel détecté par nom+CP mais email/téléphone différents - clarification requise"
            triage_result['method'] = 'duplicate_name_postal_confirmation'
            triage_result['duplicate_contact_info'] = duplicate_info
            triage_result['duplicate_type'] = duplicate_type
            triage_result['selected_deal'] = selected_deal

            # Stocker les infos pour le template
            triage_result['uber_doublon_clarification'] = True
            triage_result['duplicate_deal_name'] = duplicate_info.get('duplicate_deal_name', '')
            # Déterminer si le doublon est récupérable
            triage_result['duplicate_type_recoverable'] = duplicate_type in ['RECOVERABLE_REFUS_CMA', 'RECOVERABLE_NOT_PAID']
            triage_result['duplicate_type_refus_cma'] = duplicate_type == 'RECOVERABLE_REFUS_CMA'

            logger.info(f"   Deal doublon: {duplicate_info.get('duplicate_deal_name')}")
            logger.info(f"   Type: {duplicate_type}")
            return triage_result

        # Rule #2.5: VÉRIFICATION DOUBLON UBER 20€
        # Si le candidat a déjà bénéficié de l'offre Uber 20€, il ne peut pas en bénéficier à nouveau
        # NOTE: Les demandes non-Uber (CPF, France Travail, etc.) sont gérées plus haut et routées vers Contact
        # NOTE: Si le département a été recalculé vers Contact (ex: "épreuve pratique" détecté), router vers Contact
        if linking_result.get('has_duplicate_uber_offer'):
            # Vérifier si le département recalculé indique un service hors-scope (Contact)
            recalc_dept = linking_result.get('recommended_department', '')
            if recalc_dept == 'Contact':
                logger.info(f"📋 DOUBLON UBER détecté MAIS département recalculé vers Contact → Router vers Contact")
                triage_result['action'] = 'ROUTE'
                triage_result['target_department'] = 'Contact'
                triage_result['reason'] = f"Doublon Uber mais demande hors-scope détectée (département recalculé: Contact)"
                triage_result['method'] = 'duplicate_with_other_service'

                # Note de contexte routing
                self._generate_routing_context_note(
                    ticket_id, 'Contact', last_thread_content, subject,
                    all_deals, selected_deal, routing_method='duplicate_with_other_service'
                )

                if auto_transfer:
                    try:
                        logger.info(f"🔄 Transfert automatique vers Contact...")
                        transfer_success = self.dispatcher._reassign_ticket(ticket_id, 'Contact')
                        if transfer_success:
                            logger.info(f"✅ Ticket transféré vers Contact")
                            triage_result['transferred'] = True
                    except Exception as e:
                        logger.error(f"Erreur transfert: {e}")

                return triage_result
            duplicate_deals = linking_result.get('duplicate_deals', [])
            logger.warning(f"⚠️ DOUBLON UBER 20€ DÉTECTÉ: {len(duplicate_deals)} opportunités 20€ GAGNÉ")

            # Vérifier si le doublon est de type RECOVERABLE
            # RECOVERABLE = pas d'examen passé, pas de dossier validé → peut reprendre l'inscription
            duplicate_type = linking_result.get('duplicate_type')
            is_recoverable = duplicate_type in ['RECOVERABLE_REFUS_CMA', 'RECOVERABLE_NOT_PAID', 'RECOVERABLE_PAID']

            if is_recoverable:
                if duplicate_type == 'RECOVERABLE_NOT_PAID':
                    # ============================================================
                    # RECOVERABLE_NOT_PAID : Ancien deal jamais payé CMA
                    # → Ignorer le doublon, continuer le workflow normal sur le nouveau deal
                    # ============================================================
                    logger.info(f"🟢 DOUBLON IGNORÉ (RECOVERABLE_NOT_PAID) → Ancien deal jamais payé, workflow normal")
                    triage_result['action'] = 'GO'
                    triage_result['reason'] = "Doublon Uber détecté mais ancien deal jamais payé CMA - ignoré"
                    triage_result['method'] = 'duplicate_not_paid_ignored'
                    # Annuler le flag doublon pour que le workflow continue normalement
                    linking_result['has_duplicate_uber_offer'] = False
                    # Pas de return → le triage IA va s'exécuter normalement
                else:
                    # ============================================================
                    # RECOVERABLE_PAID / RECOVERABLE_REFUS_CMA : Ancien deal payé CMA
                    # → Continuer workflow normal mais ajouter note CRM avec infos ancien deal
                    # ============================================================
                    logger.info(f"🟡 DOUBLON AVEC CMA PAYÉE (type: {duplicate_type}) → Workflow normal + note CRM")
                    triage_result['action'] = 'GO'
                    triage_result['reason'] = f"Doublon Uber avec CMA payée ({duplicate_type}) - workflow normal + note"
                    triage_result['method'] = 'duplicate_paid_continue'
                    # Annuler le flag doublon pour que le workflow continue normalement
                    linking_result['has_duplicate_uber_offer'] = False
                    # Stocker les infos de l'ancien deal pour la note CRM
                    deals_sorted = sorted(duplicate_deals, key=lambda d: d.get("Closing_Date", "") or "", reverse=True)
                    old_deal = deals_sorted[-1] if len(deals_sorted) >= 2 else deals_sorted[0]
                    triage_result['old_paid_deal'] = old_deal
                    triage_result['old_paid_deal_evalbox'] = old_deal.get('Evalbox', 'N/A')
                    triage_result['old_paid_deal_id'] = old_deal.get('id')
                    triage_result['old_paid_deal_name'] = old_deal.get('Deal_Name')
                    triage_result['duplicate_type'] = duplicate_type
                    logger.info(f"  📋 Ancien deal payé: {old_deal.get('Deal_Name')} (Evalbox: {old_deal.get('Evalbox')})")
                    # Pas de return → le triage IA va s'exécuter normalement

            if not is_recoverable:
                # GUARD RAIL: Si le Resultat indique que l'examen a été passé (ADMIS, ADMISSIBLE, etc.),
                # le candidat ne demande PAS une nouvelle inscription — il demande ses résultats.
                # → Bypass doublon, laisser le workflow normal gérer (intention RESULTAT_EXAMEN)
                resultat_raw = selected_deal.get('Resultat', '') if selected_deal else ''
                resultat_info = self._classify_resultat(resultat_raw)
                if resultat_info['category'] in ('mid_exam', 'post_exam', 'closed'):
                    logger.info(f"🟢 DOUBLON IGNORÉ: Resultat='{resultat_raw}' ({resultat_info['category']}) → Examen passé, workflow normal")
                    triage_result['action'] = 'GO'
                    triage_result['reason'] = f"Doublon Uber mais Resultat={resultat_raw} - candidat a passé l'examen, workflow normal"
                    triage_result['method'] = 'duplicate_with_resultat_bypass'
                    linking_result['has_duplicate_uber_offer'] = False
                    # Pas de return → le triage IA va s'exécuter normalement
                else:
                    # Vérifier si le doublon a DÉJÀ été communiqué (threads sortants OU contenu cité dans l'entrant)
                    # Si oui, le candidat répond à autre chose → laisser le triage IA gérer
                    duplicate_already_communicated = False
                    duplicate_markers = DUPLICATE_MARKERS
                    for thread in threads:
                        status = thread.get('status', '').upper()
                        direction = thread.get('direction', '')
                        if status == 'DRAFT':
                            continue
                        # Chercher UNIQUEMENT dans les threads sortants (réponses de CAB)
                        # Les threads entrants contiennent du contenu cité (ex: "offre uber à 20€"
                        # dans l'email de confirmation d'inscription) → faux positifs
                        if direction == 'out':
                            content = get_clean_thread_content(thread).lower()
                            if any(marker in content for marker in duplicate_markers):
                                duplicate_already_communicated = True
                                logger.info(f"  📋 Marqueur doublon trouvé dans thread sortant: {thread.get('id')}")
                                break

                    if duplicate_already_communicated:
                        # Le candidat a déjà été informé du doublon et revient (souvent pour être rappelé)
                        # → Router vers Contact pour upsell (offre fi perso ou CPF)
                        logger.info("🔄 DOUBLON UBER déjà communiqué → Route vers Contact pour upsell")
                        triage_result['action'] = 'ROUTE'
                        triage_result['target_department'] = 'Contact'
                        triage_result['reason'] = "Doublon Uber déjà communiqué - candidat revient (upsell fi perso/CPF)"
                        triage_result['method'] = 'duplicate_already_communicated_upsell'

                        # Ajouter note interne pour le conseiller Contact
                        internal_note = (
                            "⚡ UPSELL - Candidat Uber ayant déjà bénéficié de l'offre 20€.\n"
                            "On lui a expliqué qu'il ne pouvait pas s'inscrire une seconde fois gratuitement.\n"
                            "Il revient pour être recontacté par un conseiller → prospect pour offre fi perso ou CPF."
                        )
                        try:
                            self.desk_client.add_ticket_comment(ticket_id, internal_note, is_public=False)
                            logger.info("  📝 Note interne ajoutée (upsell)")
                        except Exception as e:
                            logger.warning(f"  ⚠️ Erreur ajout note interne: {e}")

                        # Transférer vers Contact si auto_transfer
                        if auto_transfer:
                            try:
                                transfer_success = self.dispatcher._reassign_ticket(ticket_id, 'Contact')
                                if transfer_success:
                                    logger.info("  ✅ Ticket transféré vers Contact")
                                    triage_result['transferred'] = True
                                else:
                                    logger.warning("  ⚠️ Échec transfert vers Contact")
                            except Exception as e:
                                logger.error(f"  ❌ Erreur transfert: {e}")

                        return triage_result
                    else:
                        # Pas de demande CPF et pas récupérable → workflow doublon Uber standard (offre épuisée)
                        triage_result['action'] = 'DUPLICATE_UBER'
                        triage_result['reason'] = f"Candidat a déjà bénéficié de l'offre Uber 20€ ({len(duplicate_deals)} opportunités GAGNÉ)"
                        triage_result['method'] = 'duplicate_detection'
                        triage_result['duplicate_deals'] = duplicate_deals
                        triage_result['selected_deal'] = selected_deal
                        logger.info("🚫 DOUBLON UBER → Workflow spécifique (pas de gratuité)")
                        return triage_result

        # Rule #2.6: CANDIDAT NON TROUVÉ - Vérifier si demande d'info/CPF avant clarification
        # Si c'est un nouveau ticket et qu'on ne trouve pas le candidat dans le CRM,
        # vérifier d'abord si c'est une demande d'information (pas un dossier en cours)
        if linking_result.get('needs_clarification'):
            # Vérifier d'abord si c'est un candidat Uber converti (email différent du CRM)
            # Ces keywords indiquent une connaissance du parcours Uber → pas un prospect random
            uber_converted_keywords = UBER_CONVERTED
            content_check_uber = (subject + ' ' + last_thread_content).lower()
            is_uber_converted = any(kw in content_check_uber for kw in uber_converted_keywords)

            if is_uber_converted:
                # Candidat Uber converti avec email différent → NEEDS_CLARIFICATION (pas Contact)
                logger.info(f"🎯 Candidat non trouvé MAIS mention 'test de sélection' → Uber converti avec email différent")
                logger.info(f"   → NEEDS_CLARIFICATION pour retrouver le dossier")
                triage_result['action'] = 'NEEDS_CLARIFICATION'
                triage_result['reason'] = "Candidat Uber converti (test de sélection réussi) - email différent du CRM"
                triage_result['method'] = 'uber_converted_different_email'
                triage_result['clarification_reason'] = 'uber_converted_different_email'
                triage_result['email_searched'] = linking_result.get('email')
                logger.info("❓ CLARIFICATION → Demander coordonnées au candidat")
                return triage_result

            # Keywords indiquant une demande d'information (pas un candidat existant)
            # Ces personnes doivent être redirigées vers Contact, pas DOC
            info_request_keywords = INFO_REQUEST

            # Vérifier si le contenu indique une demande d'info
            # Utiliser clean_thread_content (sans métadonnées SalesIQ) pour éviter les faux positifs
            content_to_check = (subject + ' ' + clean_thread_content).lower()
            is_info_request = any(kw in content_to_check for kw in info_request_keywords)

            if is_info_request:
                # C'est une demande d'information → Router vers Contact
                logger.info(f"📋 Candidat non trouvé MAIS demande d'information détectée → Contact")
                triage_result['action'] = 'ROUTE'
                triage_result['target_department'] = 'Contact'
                triage_result['reason'] = "Demande d'information (CPF/renseignement) - candidat non inscrit"
                triage_result['method'] = 'info_request_routing'
                triage_result['email_searched'] = linking_result.get('email')

                # Note de contexte routing
                self._generate_routing_context_note(
                    ticket_id, 'Contact', clean_thread_content, subject,
                    all_deals, None, routing_method='info_request_routing'
                )

                # Auto-transfer vers Contact
                if auto_transfer:
                    try:
                        logger.info(f"🔄 Transfert automatique vers Contact...")
                        transfer_success = self.dispatcher._reassign_ticket(ticket_id, 'Contact')
                        if transfer_success:
                            logger.info(f"✅ Ticket transféré vers Contact")
                            triage_result['transferred'] = True
                        else:
                            logger.warning(f"⚠️ Échec transfert vers Contact")
                    except Exception as e:
                        logger.error(f"Erreur transfert: {e}")

                logger.info("🔄 ROUTE → Contact (demande d'info, pas de dossier en cours)")
                return triage_result

            # Rule #2.6b: DEMANDE HORS PÉRIMÈTRE VTC - Router vers Contact
            # Si le contenu indique clairement une demande sans rapport avec la formation VTC,
            # ne pas demander de clarification (inutile) - router vers Contact pour traitement manuel
            out_of_scope_keywords = OUT_OF_SCOPE

            is_out_of_scope = any(kw in content_to_check for kw in out_of_scope_keywords)

            if is_out_of_scope:
                # Demande hors périmètre VTC → Router vers Contact (un humain décidera)
                logger.info(f"🚫 Candidat non trouvé ET demande HORS PÉRIMÈTRE VTC détectée → Contact")
                triage_result['action'] = 'ROUTE'
                triage_result['target_department'] = 'Contact'
                triage_result['reason'] = "Demande hors périmètre VTC (CACES/taxi/autre) - pas un candidat"
                triage_result['method'] = 'out_of_scope_routing'
                triage_result['email_searched'] = linking_result.get('email')

                # Note de contexte routing
                self._generate_routing_context_note(
                    ticket_id, 'Contact', clean_thread_content, subject,
                    all_deals, None, routing_method='out_of_scope_routing'
                )

                # Auto-transfer vers Contact
                if auto_transfer:
                    try:
                        logger.info(f"🔄 Transfert automatique vers Contact...")
                        transfer_success = self.dispatcher._reassign_ticket(ticket_id, 'Contact')
                        if transfer_success:
                            logger.info(f"✅ Ticket transféré vers Contact")
                            triage_result['transferred'] = True
                        else:
                            logger.warning(f"⚠️ Échec transfert vers Contact")
                    except Exception as e:
                        logger.error(f"Erreur transfert: {e}")

                logger.info("🔄 ROUTE → Contact (hors périmètre VTC, pas de clarification)")
                return triage_result

            # Sinon, demander clarification comme avant
            logger.warning(f"⚠️ CANDIDAT NON TROUVÉ - Clarification nécessaire")
            triage_result['action'] = 'NEEDS_CLARIFICATION'
            triage_result['reason'] = f"Candidat non trouvé dans le CRM avec l'email {linking_result.get('email', 'inconnu')}"
            triage_result['method'] = 'candidate_not_found'
            triage_result['clarification_reason'] = linking_result.get('clarification_reason', 'candidate_not_found')
            triage_result['email_searched'] = linking_result.get('email')
            triage_result['alternative_email_used'] = linking_result.get('alternative_email_used')
            logger.info("❓ CLARIFICATION → Demander coordonnées au candidat")
            return triage_result

        # Rule #2.7: ROUTAGE AUTOMATIQUE SI DÉPARTEMENT DIFFÉRENT DE DOC
        # BusinessRules a déterminé que ce ticket devrait aller ailleurs (ex: "examen pratique" → Contact)
        suggested_department = linking_result.get('recommended_department') or linking_result.get('department', 'DOC')
        if suggested_department and suggested_department.upper() not in ['DOC', 'DOCUMENTS']:
            logger.warning(f"⚠️ ROUTAGE AUTOMATIQUE → {suggested_department} (règle métier)")
            triage_result['action'] = 'ROUTE'
            triage_result['target_department'] = suggested_department
            triage_result['reason'] = f"Routage automatique via BusinessRules: {linking_result.get('routing_reason', 'département différent de DOC')}"
            triage_result['method'] = 'business_rules_routing'
            triage_result['selected_deal'] = selected_deal

            # Note de contexte routing
            self._generate_routing_context_note(
                ticket_id, suggested_department, last_thread_content, subject,
                all_deals, selected_deal, routing_method='business_rules_routing'
            )

            # Auto-transfer if enabled
            if auto_transfer:
                try:
                    logger.info(f"🔄 Transfert automatique vers {suggested_department}...")
                    transfer_success = self.dispatcher._reassign_ticket(ticket_id, suggested_department)
                    if transfer_success:
                        logger.info(f"✅ Ticket transféré vers {suggested_department}")
                        triage_result['transferred'] = True
                    else:
                        logger.warning(f"⚠️ Échec transfert vers {suggested_department}")
                except Exception as e:
                    logger.error(f"Erreur transfert: {e}")

            return triage_result

        # If no deals found, also check by email directly
        if not all_deals:
            email = ticket.get('email', '')
            if email:
                try:
                    all_deals = self.crm_client.search_deals_by_email(email) or []
                    if all_deals:
                        selected_deal = all_deals[0]
                except Exception as e:
                    logger.warning(f"Erreur recherche deals: {e}")
                    all_deals = []

        # Rule #3: UTILISER L'IA POUR LE TRIAGE INTELLIGENT
        # L'IA comprend le contexte et évite les faux positifs

        # IMPORTANT: Enrichir le deal avec la vraie date d'examen (lookup → module)
        # Les champs lookup contiennent juste {'name': '...', 'id': '...'}, pas les vraies données
        if selected_deal and selected_deal.get('Date_examen_VTC'):
            date_lookup = selected_deal.get('Date_examen_VTC')
            if isinstance(date_lookup, dict) and date_lookup.get('id'):
                try:
                    exam_session = self.crm_client.get_record('Dates_Examens_VTC_TAXI', date_lookup['id'])
                    if exam_session:
                        selected_deal['_real_exam_date'] = exam_session.get('Date_Examen')
                        selected_deal['_real_exam_departement'] = exam_session.get('Departement')
                        logger.info(f"  📅 Date examen enrichie: {selected_deal['_real_exam_date']} (dept {selected_deal['_real_exam_departement']})")
                except Exception as e:
                    logger.warning(f"  ⚠️ Impossible d'enrichir Date_examen_VTC: {e}")

        # Générer un résumé de l'historique si plusieurs threads
        conversation_summary = None
        if len(threads) > 2:
            logger.info("📝 Génération du résumé de conversation...")
            try:
                import anthropic
                from config import settings

                # Extraire le contenu des threads pour le résumé
                threads_text = []
                for t in threads[:10]:  # Max 10 derniers threads
                    direction = "CANDIDAT" if t.get('direction') == 'in' else "CAB"
                    content = get_clean_thread_content(t)[:400]
                    if content:
                        threads_text.append(f"[{direction}]: {content}")

                if threads_text:
                    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
                    summary_response = client.messages.create(
                        model=MODEL_EXTRACTION,
                        max_tokens=200,
                        messages=[{
                            "role": "user",
                            "content": f"""Résume en 2-3 phrases l'historique de cette conversation entre un candidat VTC et CAB Formations.
Focus sur: le problème principal, ce qui a été fait, ce qui reste à résoudre.

CONVERSATION:
{chr(10).join(threads_text)}

RÉSUMÉ (2-3 phrases):"""
                        }]
                    )
                    conversation_summary = summary_response.content[0].text.strip()
                    logger.info(f"  ✅ Résumé généré ({len(conversation_summary)} chars)")
            except Exception as e:
                logger.warning(f"  ⚠️ Impossible de générer le résumé: {e}")

        # Règle déterministe: Pièces jointes + sujet document → Refus CMA (TRANSMET_DOCUMENTS)
        # Cette règle s'exécute AVANT l'appel IA pour économiser un appel API
        has_attachments = False
        attachment_count = 0
        real_attachments = []

        # Patterns pour identifier les logos/signatures à ignorer
        logo_signature_patterns = LOGO_SIGNATURE_PATTERNS

        if threads:
            for t in reversed(threads):
                if t.get('direction') == 'in':
                    thread_attachments = t.get('attachments', [])
                    for att in thread_attachments:
                        att_name = (att.get('name') or att.get('fileName') or '').lower()
                        att_size_raw = att.get('size') or att.get('fileSize') or 0
                        try:
                            att_size = int(att_size_raw) if att_size_raw else 0
                        except (ValueError, TypeError):
                            att_size = 0

                        # Ignorer les petites images (< 50KB) qui sont probablement des logos/signatures
                        is_small_image = (
                            att_size < 50000 and
                            any(att_name.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif', '.bmp'])
                        )

                        # Ignorer si le nom contient des patterns de logo/signature
                        is_logo_signature = any(pattern in att_name for pattern in logo_signature_patterns)

                        # Garder seulement les vraies pièces jointes
                        if not is_small_image and not is_logo_signature:
                            real_attachments.append(att)
                            logger.debug(f"  📎 Vraie pièce jointe: {att_name} ({att_size} bytes)")
                        else:
                            logger.debug(f"  🚫 Logo/signature ignoré: {att_name} ({att_size} bytes)")

                    if real_attachments:
                        has_attachments = True
                        attachment_count = len(real_attachments)
                    break

        subject_lower = subject.lower() if subject else ''
        content_lower = last_thread_content.lower() if last_thread_content else ''
        document_keywords = DOC_KEYWORDS
        subject_has_doc_keyword = any(kw in subject_lower for kw in document_keywords)
        content_has_doc_keyword = any(kw in content_lower for kw in document_keywords)

        if has_attachments and (subject_has_doc_keyword or content_has_doc_keyword):
            logger.info(f"  🔍 Pièces jointes détectées ({attachment_count}) + sujet document → Route vers Refus CMA")
            ai_triage = {
                'action': 'ROUTE',
                'target_department': 'Refus CMA',
                'reason': f"Candidat envoie {attachment_count} document(s) en pièce jointe - à uploader sur ExamT3P",
                'confidence': 1.0,
                'method': 'rule_transmet_documents',
                'primary_intent': 'TRANSMET_DOCUMENTS',
                'secondary_intents': [],
                'detected_intent': 'TRANSMET_DOCUMENTS',
                'intent_context': {'has_attachments': True, 'attachment_count': attachment_count}
            }
        else:
            logger.info("🤖 Triage IA en cours...")
            ai_triage = self.triage_agent.triage_ticket(
                ticket_subject=subject,
                thread_content=last_thread_content,
                deal_data=selected_deal,
                current_department='DOC',
                conversation_summary=conversation_summary  # Nouveau: contexte historique
            )

        logger.info(f"  🤖 Résultat IA: {ai_triage['action']} → {ai_triage['target_department']} ({ai_triage['reason']})")
        logger.info(f"  🤖 Confiance: {ai_triage['confidence']:.0%} | Méthode: {ai_triage['method']}")

        # Appliquer le résultat de l'IA
        triage_result['action'] = ai_triage['action']
        triage_result['target_department'] = ai_triage['target_department']
        triage_result['reason'] = ai_triage['reason']
        triage_result['method'] = ai_triage['method']
        triage_result['confidence'] = ai_triage['confidence']

        # Copier l'intention détectée et son contexte (pour State Engine)
        triage_result['detected_intent'] = ai_triage.get('detected_intent')
        triage_result['intent_context'] = ai_triage.get('intent_context', {})
        # Multi-intentions
        triage_result['primary_intent'] = ai_triage.get('primary_intent')
        triage_result['secondary_intents'] = ai_triage.get('secondary_intents', [])
        # Ajouter selected_deal pour utilisation ultérieure (ex: draft TRANSMET_DOCUMENTS)
        triage_result['selected_deal'] = selected_deal

        # Log intention si détectée
        if triage_result.get('detected_intent'):
            logger.info(f"  🎯 Intention: {triage_result['detected_intent']}")
            if triage_result.get('intent_context', {}).get('mentions_force_majeure'):
                logger.info(f"  ⚠️ Force majeure: {triage_result['intent_context'].get('force_majeure_type')}")

        # ================================================================
        # RÈGLE CRITIQUE: TRANSMET_DOCUMENTS + Date_Dossier_reçu vide → GO (pas ROUTE)
        # Si le candidat envoie ses documents pour la PREMIÈRE fois (dossier pas encore reçu),
        # on reste dans DOC pour traiter. On ne route vers Refus CMA que si c'est une correction.
        # ================================================================
        if (ai_triage['action'] == 'ROUTE'
            and ai_triage['target_department'] == 'Refus CMA'
            and ai_triage.get('primary_intent') == 'TRANSMET_DOCUMENTS'):

            date_dossier_recu = selected_deal.get('Date_Dossier_re_u') if selected_deal else None
            if not date_dossier_recu:
                logger.info("  📋 TRANSMET_DOCUMENTS + Date_Dossier_reçu VIDE → Envoi initial, on reste dans DOC")
                ai_triage['action'] = 'GO'
                ai_triage['target_department'] = 'DOC'
                ai_triage['reason'] = 'Envoi initial de documents (Date_Dossier_reçu vide) - traitement dans DOC'
            else:
                logger.info(f"  📋 TRANSMET_DOCUMENTS + Date_Dossier_reçu={date_dossier_recu} → Correction, route vers Refus CMA")

        # ================================================================
        # RÈGLE: Candidat Uber 20€ + mention "taxi" → rester en DOC
        # Les candidats VTC inscrits par erreur à l'examen taxi se plaignent
        # auprès de CAB. L'IA peut router vers Contact par erreur.
        # Si le candidat a un deal Uber 20€, c'est une erreur interne → DOC gère.
        # ================================================================
        if (ai_triage['action'] == 'ROUTE'
            and ai_triage['target_department'] == 'Contact'
            and selected_deal
            and selected_deal.get('Amount') == UBER_OFFER_AMOUNT):
            content_lower = (last_thread_content or '').lower() + ' ' + (subject or '').lower()
            if 'taxi' in content_lower:
                logger.info("  🚕 Candidat Uber 20€ + mention 'taxi' → Override IA: rester en DOC (erreur inscription interne)")
                ai_triage['action'] = 'GO'
                ai_triage['target_department'] = 'DOC'
                ai_triage['reason'] = 'Candidat Uber 20€ mentionne taxi (erreur inscription) - traitement interne DOC'
                triage_result['action'] = 'GO'
                triage_result['target_department'] = 'DOC'
                triage_result['reason'] = ai_triage['reason']

        # Determine action based on AI recommendation
        if ai_triage['action'] == 'ROUTE' and ai_triage['target_department'] != 'DOC':
            # Note de contexte routing
            self._generate_routing_context_note(
                ticket_id, ai_triage['target_department'], last_thread_content, subject,
                all_deals, selected_deal, routing_method='ai',
                ai_reason=ai_triage.get('reason', '')
            )
            # Auto-transfer if enabled
            if auto_transfer:
                logger.info(f"🔄 Transfert automatique vers {ai_triage['target_department']}...")
                try:
                    # Use dispatcher to reassign
                    transfer_success = self.dispatcher._reassign_ticket(ticket_id, ai_triage['target_department'])
                    triage_result['transferred'] = transfer_success
                    if transfer_success:
                        logger.info(f"✅ Ticket transféré vers {ai_triage['target_department']}")
                    else:
                        logger.warning(f"⚠️ Échec transfert vers {ai_triage['target_department']}")
                except Exception as e:
                    logger.error(f"❌ Erreur transfert: {e}")
                    triage_result['transferred'] = False
        else:
            # Stay in DOC
            triage_result['action'] = 'GO'
            triage_result['target_department'] = 'DOC'
            triage_result['reason'] = 'Ticket DOC valide - continuer workflow'

        return triage_result

    # ─── Routing Context Notes ───────────────────────────────────────────

    ROUTING_RULES = {
        'UPSELL_OPPORTUNITY': {
            'header': '💰 UPSELL POTENTIEL',
            'recommendation': (
                "Ce candidat a un deal {amount}€ ({stage}) mais demande l'offre Uber 20€.\n"
                "→ Opportunité de closer le deal existant plutôt que créer un dossier 20€."
            ),
        },
        'PROSPECT_UBER_20': {
            'header': '🆕 PROSPECT UBER 20€',
            'recommendation': (
                "Nouveau prospect mentionnant l'offre Uber 20€.\n"
                "Aucun dossier existant. À contacter pour qualification."
            ),
        },
        'PROSPECT_FORMATION': {
            'header': '🎓 PROSPECT FORMATION',
            'recommendation': (
                "Demande d'information sur la formation VTC.\n"
                "Pas de dossier existant. À qualifier et orienter."
            ),
        },
        'OUT_OF_SCOPE': {
            'header': '🚫 HORS PÉRIMÈTRE VTC',
            'recommendation': (
                "Demande hors périmètre (CACES, taxi, ambulance, etc.).\n"
                "Informer que CAB ne traite pas ce type de formation."
            ),
        },
        'DUPLICATE_OTHER_SERVICE': {
            'header': '⚠️ DOUBLON + AUTRE SERVICE',
            'recommendation': (
                "Doublon Uber détecté mais le candidat demande un service différent.\n"
                "Traiter comme une nouvelle demande indépendante."
            ),
        },
        'AI_ROUTE_CONTEXT': {
            'header': '🤖 ROUTAGE IA',
            'recommendation': '',
        },
        'BUSINESS_RULES_GENERIC': {
            'header': '⚙️ ROUTAGE AUTOMATIQUE',
            'recommendation': (
                "Ticket routé par les règles métier.\n"
                "Vérifier le deal associé et le message du candidat."
            ),
        },
    }

    def _generate_routing_context_note(self, ticket_id, target_department, message_content,
                                       subject, all_deals, selected_deal,
                                       routing_method='', ai_reason=''):
        """Génère et poste une note interne contextuelle lors du routing d'un ticket."""
        try:
            rule = self._classify_routing_context(
                routing_method, message_content, subject, all_deals, selected_deal
            )
            excerpt = self._extract_message_excerpt(message_content)
            deals_summary = self._format_deals_for_note(all_deals)
            note = self._build_routing_note(
                rule, target_department, excerpt, deals_summary,
                selected_deal, ai_reason
            )
            self.desk_client.add_ticket_comment(ticket_id, note, is_public=False)
            logger.info(f"  📝 Note de contexte routing ajoutée ({rule})")
            return note
        except Exception as e:
            logger.warning(f"  ⚠️ Erreur note routing: {e}")
            return None

    def _classify_routing_context(self, routing_method, message_content, subject,
                                  all_deals, selected_deal):
        """Classifie le type de contexte de routing pour choisir le bon template de note."""
        content_lower = ((message_content or '') + ' ' + (subject or '')).lower()
        uber_keywords = UBER_KEYWORDS

        if routing_method == 'business_rules_routing':
            if any(kw in content_lower for kw in uber_keywords):
                return 'UPSELL_OPPORTUNITY'
            return 'BUSINESS_RULES_GENERIC'
        elif routing_method == 'info_request_routing':
            if any(kw in content_lower for kw in uber_keywords + ['uber']):
                return 'PROSPECT_UBER_20'
            return 'PROSPECT_FORMATION'
        elif routing_method == 'out_of_scope_routing':
            return 'OUT_OF_SCOPE'
        elif routing_method == 'duplicate_with_other_service':
            return 'DUPLICATE_OTHER_SERVICE'
        else:
            return 'AI_ROUTE_CONTEXT'

    def _extract_message_excerpt(self, content, max_length=300):
        """Extrait un résumé propre du message (strip HTML, tronquer)."""
        if not content:
            return "(Message vide)"
        import re
        text = re.sub(r'<[^>]+>', ' ', content)
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > max_length:
            text = text[:max_length] + "..."
        return text or "(Message vide)"

    def _format_deals_for_note(self, all_deals, max_deals=3):
        """Formate un résumé des deals pour la note de contexte."""
        if not all_deals:
            return "Aucun deal trouvé"
        lines = []
        for deal in all_deals[:max_deals]:
            name = deal.get('Deal_Name', 'N/A')
            amount = deal.get('Amount', '?')
            stage = deal.get('Stage', '?')
            evalbox = deal.get('Evalbox', 'N/A')
            lines.append(f"- {name} | {amount}€ | {stage} | Evalbox: {evalbox}")
        if len(all_deals) > max_deals:
            lines.append(f"  ... et {len(all_deals) - max_deals} autre(s)")
        return "\n".join(lines)

    def _build_routing_note(self, rule, target_department, excerpt, deals_summary,
                            selected_deal, ai_reason):
        """Construit la note interne de contexte routing."""
        rule_info = self.ROUTING_RULES.get(rule, self.ROUTING_RULES['BUSINESS_RULES_GENERIC'])
        header = rule_info['header']
        recommendation = rule_info['recommendation']

        # Remplir les placeholders dynamiques
        if rule == 'UPSELL_OPPORTUNITY' and selected_deal:
            amount = selected_deal.get('Amount', '?')
            stage = selected_deal.get('Stage', '?')
            recommendation = recommendation.format(amount=amount, stage=stage)
        elif rule == 'AI_ROUTE_CONTEXT':
            recommendation = ai_reason or "Routé par l'IA de triage."

        note_parts = [
            f"{header} — Transfert vers {target_department}",
            "",
            "MESSAGE DU CANDIDAT:",
            excerpt,
            "",
            "DEALS EXISTANTS:",
            deals_summary,
            "",
            "ACTION RECOMMANDÉE:",
            recommendation,
        ]
        return "\n".join(note_parts)

    # ─── End Routing Context Notes ───────────────────────────────────────

    def _run_analysis(self, ticket_id: str, triage_result: Dict) -> Dict:
        """
        Run AGENT ANALYSTE logic - extract data from 6 sources.

        Sources:
        1. CRM Zoho (contact, deal)
        2. ExamenT3P (documents, paiement, compte)
        3. Evalbox (Google Sheet - eligibility)
        4. Sessions sheet (SESSIONSUBER2026.xlsx)
        5. Ticket threads (conversation history)
        6. Google Drive (if needed)

        Returns:
            {
                'contact_data': Dict,
                'deal_id': str,
                'deal_data': Dict,
                'examt3p_data': Dict,
                'evalbox_data': Dict,
                'session_data': Dict,
                'ancien_dossier': bool
            }
        """
        # Initialisation variables de confirmation date (évite UnboundLocalError si skip CAS A)
        confirmed_exam_date_valid = False
        confirmed_exam_date_id = None
        confirmed_exam_date_info = None
        confirmed_exam_date_unavailable = False
        available_exam_dates_for_dept = []
        confirmed_new_exam_date = None
        session_year_error_corrected = None

        # Get ticket
        ticket = self.desk_client.get_ticket(ticket_id)
        email = ticket.get('email', '')

        # Source 1: CRM - Find contact and deal
        logger.info("  📊 Source 1/6: CRM Zoho...")

        # Use DealLinkingAgent.process() to find deal
        linking_result = self.deal_linker.process({"ticket_id": ticket_id})

        deal_id = linking_result.get('deal_id')
        deal_data = linking_result.get('selected_deal') or linking_result.get('deal') or {}

        # ================================================================
        # RÉCUPÉRER LES DONNÉES DU CONTACT LIÉ (First_Name, Last_Name)
        # ================================================================
        contact_data = {}
        contact_id = deal_data.get('Contact_Name', {}).get('id') if deal_data else None
        if contact_id:
            try:
                contact_data = self.crm_client.get_contact(contact_id)
                logger.info(f"  ✅ Contact récupéré: {contact_data.get('First_Name', '')} {contact_data.get('Last_Name', '')}")
            except Exception as e:
                logger.warning(f"  ⚠️  Erreur récupération contact: {e}")

        if email:
            contact_data['email'] = email
            contact_data['contact_id'] = contact_id

        # ================================================================
        # ENRICHIR LES LOOKUPS CRM (Date_examen_VTC et Session)
        # ================================================================
        # Utilise le helper centralisé pour récupérer les vraies données
        # des modules Zoho CRM au lieu de parser le champ "name"
        lookup_cache = {}  # Cache partagé pour éviter les appels répétés
        enriched_lookups = enrich_deal_lookups(self.crm_client, deal_data, lookup_cache)

        # Extraire la date d'examen enrichie pour compatibilité
        date_examen_vtc_value = enriched_lookups.get('date_examen')
        if not date_examen_vtc_value:
            # Fallback: essayer de récupérer depuis le lookup name (compatibilité legacy)
            date_examen_lookup = deal_data.get('Date_examen_VTC')
            if date_examen_lookup:
                if isinstance(date_examen_lookup, dict):
                    date_examen_vtc_value = date_examen_lookup.get('name')
                else:
                    date_examen_vtc_value = date_examen_lookup
        logger.debug(f"  📅 Date_examen_VTC extraite: {date_examen_vtc_value}")

        # Resultat CRM — classification lifecycle
        resultat_raw = deal_data.get('Resultat', '') if deal_data else ''
        resultat_info = self._classify_resultat(resultat_raw)
        if resultat_info['category'] != 'pre_exam':
            logger.info(f"  📊 Resultat CRM: '{resultat_raw}' → {resultat_info['category']} (dossier_termine={resultat_info['dossier_termine']})")

        if not deal_id:
            logger.warning("  ⚠️  No deal found for this ticket")

            # ================================================================
            # STOP WORKFLOW: Pas de deal GAGNÉ → investigation manuelle requise
            # Le candidat peut être un prospect Uber (deals PERDU) ou un
            # prospect non-Uber → ne pas générer de réponse automatique
            # ================================================================
            all_deals = linking_result.get('all_deals', [])
            deals_summary = "; ".join([
                f"{d.get('Deal_Name', '?')} ({d.get('Amount', '?')}€, {d.get('Stage', '?')})"
                for d in all_deals[:5]
            ]) if all_deals else "Aucun deal trouvé"

            note = (
                "⚠️ WORKFLOW STOPPÉ — PAS DE DEAL GAGNÉ\n\n"
                "Potentiel prospect Uber sans deal Gagné 20 euros à investiguer.\n"
                "Attention : si pas Uber, déplacer vers Contact.\n\n"
                f"Deals trouvés ({len(all_deals)}) : {deals_summary}"
            )

            try:
                self.desk_client.add_ticket_comment(ticket_id, note, is_public=False)
                logger.info("  ✅ Note interne ajoutée sur le ticket")
            except Exception as e:
                logger.warning(f"  ⚠️ Impossible d'ajouter la note: {e}")

            logger.warning("🛑 STOP WORKFLOW — Pas de deal GAGNÉ → investigation manuelle")
            return {
                'success': True,
                'workflow_stage': 'STOPPED_NO_DEAL',
                'reason': 'Pas de deal GAGNÉ - Potentiel prospect Uber à investiguer',
                'ticket_id': ticket_id,
                'deal_id': None,
                'all_deals_count': len(all_deals),
                'deals_summary': deals_summary,
                'draft_created': False,
                'crm_updated': False,
            }

        # Source 2: ExamenT3P avec gestion complète des identifiants
        logger.info("  🌐 Source 2/6: ExamenT3P...")

        # Import du helper pour la gestion des identifiants
        from src.utils.examt3p_credentials_helper import get_credentials_with_validation
        from src.utils.date_examen_vtc_helper import analyze_exam_date_situation

        # Récupérer les threads du ticket avec contenu complet
        threads_data = self.desk_client.get_all_threads_with_full_content(ticket_id)

        # Workflow complet de validation des identifiants
        credentials_result = get_credentials_with_validation(
            deal_data=deal_data,
            threads=threads_data,
            crm_client=self.crm_client,
            deal_id=deal_id,
            auto_update_crm=True  # Toujours mettre à jour le CRM si identifiants trouvés dans mails
        )

        # Initialiser examt3p_data
        examt3p_data = {
            'compte_existe': False,
            'identifiant': credentials_result.get('identifiant'),
            'mot_de_passe': credentials_result.get('mot_de_passe'),  # Sera masqué dans les logs
            'credentials_source': credentials_result.get('credentials_source'),
            'connection_test_success': credentials_result.get('connection_test_success'),
            'documents': [],
            'documents_manquants': [],
            'paiement_cma_status': 'N/A',
            'should_respond_to_candidate': credentials_result.get('should_respond_to_candidate', False),
            'candidate_response_message': credentials_result.get('candidate_response_message'),
            # Flag compte personnel potentiel
            'potential_personal_account': credentials_result.get('potential_personal_account', False),
            'potential_personal_email': credentials_result.get('potential_personal_email'),
            'personal_account_warning': credentials_result.get('personal_account_warning')
        }

        # ================================================================
        # ALERTE COMPTE PERSONNEL POTENTIEL
        # ================================================================
        if credentials_result.get('potential_personal_account'):
            personal_email = credentials_result.get('potential_personal_email', 'inconnu')
            logger.warning(f"  🚨 COMPTE PERSONNEL POTENTIEL: {personal_email}")
            logger.warning(f"     → Le candidat pourrait voir un statut différent sur son compte perso")
            logger.warning(f"     → La réponse doit clarifier d'utiliser UNIQUEMENT le compte CAB")

        # ================================================================
        # ALERTE DOUBLON DE PAIEMENT
        # ================================================================
        if credentials_result.get('duplicate_payment_alert'):
            logger.error("  🚨🚨🚨 ALERTE CRITIQUE: DEUX COMPTES EXAMT3P PAYÉS DÉTECTÉS! 🚨🚨🚨")
            duplicate_accounts = credentials_result.get('duplicate_accounts', {})
            logger.error(f"     → Compte CRM: {duplicate_accounts.get('crm', {}).get('identifiant')}")
            logger.error(f"     → Compte Candidat: {duplicate_accounts.get('thread', {}).get('identifiant')}")
            logger.error("     → INTERVENTION MANUELLE REQUISE - Vérifier les paiements!")

            # Ajouter le flag dans examt3p_data pour visibilité
            examt3p_data['duplicate_payment_alert'] = True
            examt3p_data['duplicate_accounts'] = duplicate_accounts

            # Créer une note CRM d'alerte
            try:
                alert_content = f"""⚠️ ATTENTION - INTERVENTION MANUELLE REQUISE ⚠️

Deux comptes ExamenT3P fonctionnels ont été détectés pour ce candidat, et les deux semblent avoir été payés.

📧 Compte 1 (CRM): {duplicate_accounts.get('crm', {}).get('identifiant')}
📧 Compte 2 (Candidat): {duplicate_accounts.get('thread', {}).get('identifiant')}

✅ Action requise:
1. Vérifier les deux comptes sur ExamenT3P
2. Identifier lequel a réellement été payé par CAB Formations
3. Si double paiement confirmé, demander remboursement
4. Mettre à jour le CRM avec le bon compte

⚠️ Risque: Paiement en double des frais CMA (60€)"""

                self.crm_client.add_deal_note(
                    deal_id=deal_id,
                    note_title="🚨 ALERTE: DOUBLE COMPTE EXAMT3P PAYÉ",
                    note_content=alert_content
                )
                logger.info("  ✅ Note CRM d'alerte créée")
            except Exception as e:
                logger.error(f"  ❌ Erreur création note CRM d'alerte: {e}")

        # Info si basculement vers compte payé du candidat
        if credentials_result.get('switched_to_paid_account'):
            logger.info("  🔄 Basculement vers le compte ExamT3P déjà payé du candidat")
            examt3p_data['switched_to_paid_account'] = True

        # Si les identifiants sont valides, procéder à l'extraction
        if credentials_result.get('connection_test_success'):
            logger.info(f"  ✅ Identifiants validés (source: {credentials_result['credentials_source']})")

            if credentials_result.get('crm_updated'):
                logger.info("  ✅ CRM mis à jour avec les nouveaux identifiants")

            try:
                # Extraction complète des données ExamenT3P
                logger.info("  📥 Extraction des données ExamenT3P...")
                examt3p_result = self.examt3p_agent.process({
                    'username': credentials_result['identifiant'],
                    'password': credentials_result['mot_de_passe']
                })

                if examt3p_result.get('success'):
                    # Fusionner les données extraites avec examt3p_data
                    examt3p_data.update(examt3p_result)
                    examt3p_data['compte_existe'] = True
                    logger.info("  ✅ Données ExamenT3P extraites avec succès")

                    # Log des pièces refusées pour debug
                    pieces_refusees = examt3p_data.get('pieces_refusees_details', [])
                    if pieces_refusees:
                        logger.info(f"  📄 Pièces refusées trouvées: {len(pieces_refusees)}")
                        for piece in pieces_refusees:
                            logger.info(f"     - {piece.get('nom')}: {piece.get('motif')}")
                    else:
                        docs = examt3p_data.get('documents', [])
                        logger.info(f"  📄 Aucune pièce refusée. Documents: {[(d.get('nom'), d.get('statut')) for d in docs]}")
                else:
                    logger.warning(f"  ⚠️  Échec extraction ExamenT3P: {examt3p_result.get('error')}")
                    examt3p_data['extraction_error'] = examt3p_result.get('error')

            except Exception as e:
                logger.error(f"  ❌ Erreur lors de l'extraction ExamenT3P: {e}")
                examt3p_data['extraction_error'] = str(e)

        elif credentials_result.get('credentials_found'):
            # Identifiants trouvés mais connexion échouée (mot de passe changé par le candidat)
            logger.warning(f"  ❌ Identifiants trouvés mais connexion échouée: {credentials_result.get('connection_error')}")
            examt3p_data['extraction_error'] = f"Connexion échouée: {credentials_result.get('connection_error')}"
            examt3p_data['credentials_login_failed'] = True  # Flag pour template: mot de passe changé

        else:
            # Identifiants non trouvés
            logger.warning("  ⚠️  Identifiants ExamenT3P introuvables")
            examt3p_data['extraction_error'] = "Identifiants non trouvés dans le CRM ni dans les threads"

        # Source 3: Evalbox (Google Sheet)
        logger.info("  📊 Source 3/6: Evalbox...")
        evalbox_data = {
            'eligible_uber': None,
            'scope': None
        }
        # TODO: Query Evalbox Google Sheet

        # Source 4: Sessions (CRM module Sessions1)
        logger.info("  📅 Source 4/6: Sessions...")
        session_data = {}
        # Les sessions seront récupérées après l'analyse date_examen_vtc

        # Source 5: Ticket threads (déjà récupérés pour ExamenT3P)
        logger.info("  💬 Source 5/6: Ticket threads...")
        # threads déjà récupérés plus haut pour la validation des identifiants

        # Source 6: Google Drive (if needed)
        logger.info("  📁 Source 6/6: Google Drive...")
        # Only if specific documents needed

        # ================================================================
        # VÉRIFICATION ÉLIGIBILITÉ UBER 20€ (PRIORITAIRE)
        # ================================================================
        # Pour les candidats Uber 20€, ils doivent d'abord:
        # 1. Envoyer leurs documents (Date_Dossier_re_u non vide)
        # 2. Passer le test de sélection (Date_test_selection non vide)
        # Si ces étapes ne sont pas complétées, on ne peut pas les inscrire à l'examen
        from src.utils.uber_eligibility_helper import analyze_uber_eligibility
        from src.utils.examt3p_crm_sync import sync_examt3p_to_crm, sync_exam_date_from_examt3p
        from src.utils.ticket_info_extractor import extract_confirmations_from_threads, extract_cab_proposals_from_threads, detect_candidate_references, detect_dossier_completion_request

        # ================================================================
        # SYNC EXAMT3P → CRM (AVANT toute analyse)
        # ================================================================
        # ExamT3P est la SOURCE DE VÉRITÉ - on synchronise d'abord vers CRM
        sync_result = None
        if examt3p_data.get('compte_existe') and deal_id:
            logger.info("  🔄 Synchronisation ExamT3P → CRM...")
            sync_result = sync_examt3p_to_crm(
                deal_id=deal_id,
                deal_data=deal_data,
                examt3p_data=examt3p_data,
                crm_client=self.crm_client,
                dry_run=False
            )
            if sync_result.get('crm_updated'):
                logger.info("  ✅ CRM synchronisé avec ExamT3P")
                # Recharger deal_data après mise à jour
                updated_deal = self.crm_client.get_deal(deal_id)
                if updated_deal:
                    deal_data = updated_deal
            # Note: sync_result sera inclus dans la note consolidée finale

            # ================================================================
            # SYNC DATE D'EXAMEN DEPUIS EXAMT3P
            # ================================================================
            # Si la date d'examen ExamT3P diffère du CRM → mettre à jour automatiquement
            # (sauf si règle de blocage: VALIDE CMA + clôture passée)
            logger.info("  📅 Synchronisation date d'examen ExamT3P → CRM...")
            date_sync_result = sync_exam_date_from_examt3p(
                deal_id=deal_id,
                deal_data=deal_data,
                examt3p_data=examt3p_data,
                crm_client=self.crm_client,
                dry_run=False
            )

            if date_sync_result.get('date_changed'):
                logger.info(f"  ✅ Date_examen_VTC mis à jour: {date_sync_result['old_date'] or 'VIDE'} → {date_sync_result['new_date']}")
                # Recharger deal_data après mise à jour
                updated_deal = self.crm_client.get_deal(deal_id)
                if updated_deal:
                    deal_data = updated_deal
                # CRITIQUE: Mettre à jour enriched_lookups avec la nouvelle date
                # Sinon le template utilisera l'ancienne date du CRM
                new_date = date_sync_result.get('new_date')
                if new_date:
                    enriched_lookups['date_examen'] = new_date
                    logger.info(f"  📅 enriched_lookups['date_examen'] mis à jour: {new_date}")
                # Ajouter au sync_result pour la note CRM
                sync_result['date_sync'] = date_sync_result
            elif date_sync_result.get('blocked'):
                logger.warning(f"  🔒 Date_examen_VTC non modifiée: {date_sync_result['blocked_reason']}")
                sync_result['date_sync'] = date_sync_result
            elif date_sync_result.get('error'):
                logger.warning(f"  ⚠️ Erreur sync date: {date_sync_result['error']}")

        # ================================================================
        # EXTRACTION CONFIRMATIONS DU TICKET
        # ================================================================
        ticket_confirmations = None
        if threads_data and deal_id:
            logger.info("  📥 Extraction des confirmations du ticket...")
            ticket_confirmations = extract_confirmations_from_threads(
                threads=threads_data,
                deal_data=deal_data
            )
            if ticket_confirmations.get('raw_confirmations'):
                logger.info(f"  📋 {len(ticket_confirmations['raw_confirmations'])} confirmation(s) détectée(s)")

            # Alerter sur les mises à jour bloquées (règle critique)
            if ticket_confirmations.get('blocked_updates'):
                for blocked in ticket_confirmations['blocked_updates']:
                    logger.warning(f"  🔒 BLOCAGE: {blocked['reason']}")

        # ================================================================
        # DETECTION DATES DEJA COMMUNIQUEES (anti-repetition)
        # ================================================================
        cab_proposals = extract_cab_proposals_from_threads(threads_data) if threads_data else {}
        dates_already_communicated = cab_proposals.get('proposal_count', 0) > 0
        dates_proposed_recently = cab_proposals.get('dates_proposed_recently', False)
        sessions_proposed_recently = cab_proposals.get('sessions_proposed_recently', False)

        if dates_already_communicated:
            logger.info(f"  📋 Dates deja proposees: {len(cab_proposals.get('dates_already_proposed', []))} date(s)")
            if dates_proposed_recently:
                logger.info("  ⏰ Proposees recemment (< 48h)")
        if sessions_proposed_recently:
            logger.info("  📚 Sessions deja proposees recemment (< 48h)")

        # ================================================================
        # THREAD MEMORY - Mémoire persistante via notes CRM [META]
        # ================================================================
        thread_memory_result = None
        if deal_id:
            try:
                from src.utils.thread_memory import analyze_thread_memory
                deal_notes = self.crm_client.get_deal_notes(deal_id)

                # Timeline API (v8) — field changes + human interventions
                deal_timeline = None
                try:
                    deal_timeline = self.crm_client.get_deal_timeline(deal_id)
                except Exception as e:
                    logger.warning(f"  ⚠️ Timeline API failed (graceful degradation): {e}")

                current_intent = triage_result.get('detected_intent', '')
                thread_memory_result = analyze_thread_memory(
                    notes=deal_notes,
                    current_deal_data=deal_data,
                    current_intent=current_intent,
                    ticket_threads=threads_data,
                    timeline=deal_timeline
                )
                if thread_memory_result and thread_memory_result.has_history:
                    logger.info(f"  🧠 ThreadMemory: {len(thread_memory_result.previous_records)} interactions précédentes")
                    if thread_memory_result.is_relance:
                        logger.info(f"  ⚠️ RELANCE détectée (dernière réponse il y a {thread_memory_result.days_since_last}j, {thread_memory_result.unanswered_count} msg sans réponse)")
                    if thread_memory_result.evalbox_changed:
                        logger.info(f"  📈 Progression Evalbox: {thread_memory_result.evalbox_previous} → {thread_memory_result.evalbox_current}")
                    if thread_memory_result.human_intervention_detected:
                        logger.info(f"  👤 Intervention humaine détectée: {thread_memory_result.human_intervention_actor} → suppression reset")
                else:
                    logger.info("  🧠 ThreadMemory: première interaction")
            except Exception as e:
                logger.warning(f"  ⚠️ ThreadMemory failed (graceful degradation): {e}")

            # ================================================================
            # CONVERSATION INTELLIGENCE V3
            # ================================================================
            conversation_state = None
            if deal_id and threads_data:
                try:
                    from src.utils.conversation_analyzer import analyze_conversation
                    conversation_state = analyze_conversation(
                        threads=threads_data,
                        current_deal_data=deal_data,
                        enriched_lookups=enriched_lookups,
                    )
                    if conversation_state and conversation_state.conversation_mode != 'initial_contact':
                        logger.info(f"  🧠 V3: mode={conversation_state.conversation_mode}, response={conversation_state.response_mode}, "
                                    f"target_date={conversation_state.target_date}, human_handling={conversation_state.human_is_handling}, "
                                    f"commitments={len(conversation_state.commitments)}, decisions={len(conversation_state.candidate_decisions)}, "
                                    f"latency={conversation_state.analyzer_latency_ms}ms")
                    elif conversation_state and conversation_state.analyzer_error:
                        logger.warning(f"  ⚠️ V3 analyzer error: {conversation_state.analyzer_error}")
                except Exception as e:
                    logger.warning(f"  ⚠️ ConversationAnalyzer failed (graceful): {e}")

        # ================================================================
        # DETECTION MODE COMMUNICATION CANDIDAT
        # ================================================================
        # Detecte si le candidat fait reference a une communication precedente
        # et s'il questionne une incoherence (clarification vs request)
        from src.utils.text_utils import get_clean_thread_content
        # Trouver le dernier message ENTRANT du candidat (direction: 'in')
        # threads_data[0] peut être une réponse sortante 'out'
        latest_candidate_thread = None
        for thread in threads_data:
            if thread.get('direction') == 'in':
                latest_candidate_thread = thread
                break

        latest_thread_content = get_clean_thread_content(latest_candidate_thread) if latest_candidate_thread else ""
        logger.debug(f"  📋 Latest candidate thread direction: {latest_candidate_thread.get('direction', 'none') if latest_candidate_thread else 'no incoming thread'}")
        candidate_refs = detect_candidate_references(latest_thread_content)

        communication_mode = candidate_refs.get('communication_mode', 'request')
        references_previous = candidate_refs.get('references_previous_communication', False)
        mentions_discrepancy = candidate_refs.get('mentions_discrepancy', False)

        # DEBUG: Toujours logger le mode communication
        logger.info(f"  📝 Mode communication: {communication_mode} (discrepancy={mentions_discrepancy}, refs_previous={references_previous})")

        # ================================================================
        # DETECTION DEMANDE DE COMPLETION DOSSIER PRECEDENTE
        # ================================================================
        # Si on a déjà demandé au candidat de compléter son dossier ExamT3P
        dossier_completion_request = detect_dossier_completion_request(threads_data) if threads_data else {}
        previously_asked_to_complete = dossier_completion_request.get('previously_asked_to_complete', False)
        if previously_asked_to_complete:
            logger.info(f"  📋 Demande de complétion précédente détectée (date: {dossier_completion_request.get('completion_request_date')})")

        logger.info("  🚗 Vérification éligibilité Uber 20€...")
        uber_eligibility_result = analyze_uber_eligibility(deal_data)

        # ================================================================
        # FLAG: Blocage dates/sessions si CAS A ou B
        # A = documents non envoyés → BLOCAGE (pas d'info candidat)
        # B = test sélection non passé → BLOCAGE (workflow pas complet)
        # D = Compte_Uber non vérifié → ALERTE (peut être résolu)
        # E = Non éligible selon Uber → ALERTE (peut être résolu)
        # ================================================================
        uber_case_blocks_dates = False
        uber_case_alert = None  # Pour CAS D/E: alerte à inclure dans la réponse normale
        if uber_eligibility_result.get('is_uber_20_deal'):
            uber_case = uber_eligibility_result.get('case')
            blocking_cases = ['A', 'B']  # Seuls A et B bloquent
            alert_cases = ['D', 'E']  # D et E = alerte sans blocage

            if uber_case in blocking_cases:
                logger.warning(f"  🚨 CAS {uber_case}: {uber_eligibility_result['case_description']}")
                logger.warning("  ⛔ BLOCAGE DATES/SESSIONS: Candidat doit résoudre le problème")
                uber_case_blocks_dates = True
            elif uber_case in alert_cases:
                logger.warning(f"  ⚠️ CAS {uber_case}: {uber_eligibility_result['case_description']}")
                logger.info("  📝 Traitement normal + ALERTE Uber à inclure dans la réponse")
                uber_case_alert = {
                    'case': uber_case,
                    'description': uber_eligibility_result.get('case_description', ''),
                    'response_message': uber_eligibility_result.get('response_message', '')
                }
            else:
                logger.info("  ✅ Candidat Uber éligible - peut être inscrit à l'examen")
        else:
            logger.info("  ℹ️ Pas une opportunité Uber 20€")

            # ================================================================
            # SORTIE ANTICIPÉE: Deal VTC classique (hors partenariat Uber)
            # ================================================================
            # Routage selon l'intention détectée :
            # - TRANSMET_DOCUMENTS → DOCS CAB + brouillon accusé réception
            # - Autre intention → Contact sans brouillon (traitement manuel)
            deal_stage = deal_data.get('Stage', '')
            if deal_stage == 'GAGNÉ':
                detected_intent = triage_result.get('detected_intent', '')

                logger.info("\n🚦 SORTIE ANTICIPÉE - Deal VTC classique détecté")
                logger.info(f"  Deal: {deal_data.get('Deal_Name', 'N/A')} ({deal_data.get('Amount', 0)}€)")
                logger.info(f"  Stage: {deal_stage}")
                logger.info(f"  Intention: {detected_intent}")

                # TRANSMET_DOCUMENTS → DOCS CAB avec brouillon
                if detected_intent == 'TRANSMET_DOCUMENTS':
                    logger.info("  → Envoi documents détecté → DOCS CAB + brouillon")

                    # Extraire le prénom
                    deal_name = deal_data.get('Deal_Name', '')
                    prenom = 'Candidat'
                    if deal_name:
                        parts = deal_name.split()
                        if len(parts) >= 3:
                            prenom = parts[2].capitalize()
                        elif len(parts) >= 1:
                            prenom = parts[-1].capitalize()

                    # Message d'accusé réception
                    acknowledgment_html = f"""Bonjour {prenom},<br>
<br>
Nous avons bien reçu votre message et nous vous en remercions.<br>
<br>
Notre équipe va le traiter dans les plus brefs délais. Si des informations complémentaires sont nécessaires, nous reviendrons vers vous.<br>
<br>
Cordialement,<br>
L'équipe CAB Formations"""

                    draft_created = False
                    transferred = False

                    # Créer le brouillon
                    try:
                        from config import settings

                        ticket = self.desk_client.get_ticket(ticket_id)
                        to_email = ticket.get('email', '')
                        from_email = settings.zoho_desk_email_doc or settings.zoho_desk_email_default

                        logger.info(f"  📧 Draft DOCS CAB: from={from_email}, to={to_email}")

                        draft_result = self.desk_client.create_ticket_reply_draft(
                            ticket_id=ticket_id,
                            content=acknowledgment_html,
                            content_type='html',
                            from_email=from_email,
                            to_email=to_email
                        )

                        if draft_result:
                            logger.info("  ✅ Brouillon d'accusé réception créé")
                            draft_created = True
                            self._mark_brouillon_auto(ticket_id)

                            # Transférer le ticket vers DOCS CAB
                            try:
                                self.desk_client.move_ticket_to_department(ticket_id, "DOCS CAB")
                                logger.info("  ✅ Ticket transféré vers DOCS CAB")
                                transferred = True
                            except Exception as transfer_error:
                                logger.warning(f"  ⚠️ Impossible de transférer vers DOCS CAB: {transfer_error}")
                    except Exception as e:
                        logger.error(f"  ❌ Erreur création brouillon DOCS CAB: {e}")

                    return {
                        'success': True,
                        'workflow_stage': 'STOPPED_DOCS_CAB',
                        'reason': 'Deal VTC classique (non-Uber) + envoi documents - Transféré vers DOCS CAB',
                        'ticket_id': ticket_id,
                        'deal_id': deal_id,
                        'deal_name': deal_data.get('Deal_Name', 'N/A'),
                        'deal_amount': deal_data.get('Amount', 0),
                        'transferred_to': 'DOCS CAB' if transferred else None,
                        'draft_created': draft_created,
                        'draft_content': acknowledgment_html if draft_created else None,
                        'crm_updated': False
                    }

                # Autre intention → Contact sans brouillon
                else:
                    logger.info(f"  → Demande d'information ({detected_intent}) → Contact sans brouillon")

                    transferred = False
                    try:
                        self.desk_client.move_ticket_to_department(ticket_id, "Contact")
                        logger.info("  ✅ Ticket transféré vers Contact")
                        transferred = True
                    except Exception as transfer_error:
                        logger.warning(f"  ⚠️ Impossible de transférer vers Contact: {transfer_error}")

                    return {
                        'success': True,
                        'workflow_stage': 'STOPPED_CONTACT',
                        'reason': f'Deal VTC classique (non-Uber) + demande info ({detected_intent}) - Transféré vers Contact',
                        'ticket_id': ticket_id,
                        'deal_id': deal_id,
                        'deal_name': deal_data.get('Deal_Name', 'N/A'),
                        'deal_amount': deal_data.get('Amount', 0),
                        'transferred_to': 'Contact' if transferred else None,
                        'draft_created': False,
                        'crm_updated': False
                    }

        # ================================================================
        # RÈGLE: Si pas de Date_Dossier_re_u → pas de dates/sessions
        # ================================================================
        # IMPORTANT: Cette règle ne s'applique QU'AUX DEALS 20€ (Uber)
        # Pour les deals classiques (1299€, etc.), pas besoin de Date_Dossier_re_u
        dossier_not_received_blocks_dates = False
        deal_amount = deal_data.get('Amount', 0)
        is_uber_20_deal = (deal_amount == UBER_OFFER_AMOUNT)

        if is_uber_20_deal:
            date_dossier_recu = deal_data.get('Date_Dossier_re_u')
            evalbox_status = deal_data.get('Evalbox', '')

            # Statuts Evalbox qui prouvent que le dossier a été traité
            ADVANCED_EVALBOX_STATUSES = {
                "VALIDE CMA", "Convoc CMA reçue", "Dossier Synchronisé",
                "Pret a payer", "Refusé CMA"
            }

            if not date_dossier_recu:
                if evalbox_status in ADVANCED_EVALBOX_STATUSES:
                    logger.info(f"  ℹ️ Deal 20€: Date_Dossier_re_u vide MAIS Evalbox='{evalbox_status}' → OK")
                else:
                    logger.warning("  🚨 Deal 20€: PAS DE DATE_DOSSIER_RECU")
                    logger.warning("  ⛔ BLOCAGE: On ne peut pas proposer de dates sans dossier")
                    dossier_not_received_blocks_dates = True
        else:
            logger.info(f"  ℹ️ Deal {deal_amount}€ (non-Uber): règle Date_Dossier_re_u non applicable")

        # ================================================================
        # RÈGLE CRITIQUE: SI IDENTIFIANTS NON ACCESSIBLES → SKIP DATES/SESSIONS
        # ================================================================
        # On ne peut RIEN faire tant qu'on n'a pas accès au compte ExamT3P
        # Cas possibles:
        # 1. Identifiants trouvés mais connexion échouée → demander réinitialisation
        # 2. Création de compte demandée mais pas d'identifiants → relancer le candidat
        skip_date_session_analysis = False
        skip_reason = None

        # Raison 1: Identifiants non accessibles
        # EXCEPTION: Pour les candidats Uber ÉLIGIBLES, CAB gère le compte pour eux
        # Donc on NE BLOQUE PAS sur les identifiants manquants
        is_uber_eligible = uber_eligibility_result.get('is_eligible', False)
        has_exam_date = bool(deal_data.get('Date_examen_VTC'))

        if examt3p_data.get('should_respond_to_candidate') and not examt3p_data.get('compte_existe'):
            if is_uber_eligible or has_exam_date:
                # Uber éligible ou date déjà assignée → on continue l'analyse
                logger.info("  ℹ️ Identifiants manquants MAIS candidat Uber éligible ou date assignée")
                logger.info("  → On continue l'analyse dates/sessions (CAB gère le compte)")
                # Ne pas skip, on répond à la question du candidat
            elif examt3p_data.get('credentials_request_sent'):
                logger.warning("  🚨 DEMANDE D'IDENTIFIANTS DÉJÀ ENVOYÉE MAIS PAS DE RÉPONSE")
                logger.warning("  → La réponse doit confirmer que c'est normal et redemander les identifiants")
                skip_date_session_analysis = True
                skip_reason = 'credentials_invalid'
            elif examt3p_data.get('account_creation_requested'):
                logger.warning("  🚨 CRÉATION DE COMPTE DEMANDÉE MAIS PAS D'IDENTIFIANTS REÇUS")
                logger.warning("  → La réponse doit relancer le candidat sur la création de compte")
                skip_date_session_analysis = True
                skip_reason = 'credentials_invalid'
            else:
                logger.warning("  🚨 IDENTIFIANTS INVALIDES → SKIP analyse dates/sessions")
                logger.warning("  → La réponse doit UNIQUEMENT demander les bons identifiants")
                skip_date_session_analysis = True
                skip_reason = 'credentials_invalid'

        # Raison 2: CAS A, B, D ou E (problème Uber - vérification/éligibilité)
        if uber_case_blocks_dates:
            skip_date_session_analysis = True
            uber_case = uber_eligibility_result.get('case', '?')
            skip_reason = skip_reason or f'uber_case_{uber_case}'
            logger.warning(f"  → La réponse doit UNIQUEMENT traiter CAS {uber_case}: {uber_eligibility_result.get('case_description', '')}")

        # Raison 3: Dossier non reçu (pour tous les deals)
        if dossier_not_received_blocks_dates and not skip_date_session_analysis:
            skip_date_session_analysis = True
            skip_reason = skip_reason or 'dossier_not_received'
            logger.warning("  → La réponse doit demander de finaliser l'inscription / envoyer le dossier")

        # ================================================================
        # VÉRIFICATION DATE EXAMEN VTC
        # ================================================================
        date_examen_vtc_result = {}
        if not skip_date_session_analysis:
            logger.info("  📅 Vérification date examen VTC...")

            # Récupérer la préférence de session depuis le triage
            triage_session_pref = None
            if triage_result:
                intent_parser = IntentParser(triage_result)
                triage_session_pref = intent_parser.session_preference

            date_examen_vtc_result = analyze_exam_date_situation(
                deal_data=deal_data,
                threads=threads_data,
                crm_client=self.crm_client,
                examt3p_data=examt3p_data,
                session_preference=triage_session_pref,
                enriched_lookups=enriched_lookups
            )

            if date_examen_vtc_result.get('should_include_in_response'):
                logger.info(f"  ➡️ CAS {date_examen_vtc_result['case']}: {date_examen_vtc_result['case_description']}")
            else:
                logger.info(f"  ✅ Date examen VTC OK (CAS {date_examen_vtc_result['case']})")

            # ================================================================
            # AUTO-REPORT: Date passée + dossier non validé → nouvelle date
            # ================================================================
            # Si le système détecte un auto-report (date passée + statut pré-validation),
            # vérifier si le candidat confirme une date spécifique dans son message
            if date_examen_vtc_result.get('auto_report'):
                from src.utils.date_confirmation_extractor import extract_confirmed_exam_date
                from src.utils.examt3p_crm_sync import find_exam_session_by_date_and_dept

                # Extraire le dernier message du candidat
                candidate_message = ''
                if threads_data:
                    # Trouver le premier thread (le plus récent du candidat)
                    for thread in threads_data:
                        if thread.get('direction') == 'in':
                            candidate_message = thread.get('content', '')
                            break

                confirmed = extract_confirmed_exam_date(candidate_message)
                departement = enriched_lookups.get('cma_departement') or str(deal_data.get('CMA_de_depot', ''))

                if confirmed:
                    logger.info(f"  📅 Candidat confirme nouvelle date: {confirmed['formatted']}")

                    # Valider que cette date existe pour le département
                    session = find_exam_session_by_date_and_dept(
                        self.crm_client, confirmed['date'], departement
                    )
                    if session:
                        date_examen_vtc_result['confirmed_date'] = confirmed['date']
                        date_examen_vtc_result['confirmed_date_formatted'] = confirmed['formatted']
                        date_examen_vtc_result['confirmed_session_id'] = session.get('id')
                        logger.info(f"  ✅ Date {confirmed['formatted']} validée pour dept {departement}")
                    else:
                        logger.warning(f"  ⚠️ Date {confirmed['formatted']} non trouvée pour dept {departement}")
                else:
                    logger.info(f"  📅 Pas de date confirmée par le candidat - utilisation auto-report: {date_examen_vtc_result.get('auto_report_date')}")

                # Déterminer la nouvelle date à utiliser (confirmée ou auto-report)
                new_date = date_examen_vtc_result.get('confirmed_date') or date_examen_vtc_result.get('auto_report_date')
                new_session_id = date_examen_vtc_result.get('confirmed_session_id') or date_examen_vtc_result.get('auto_report_session_id')

                if new_session_id and deal_id:
                    # Préparer la mise à jour CRM
                    date_examen_vtc_result['should_update_exam_date'] = True
                    date_examen_vtc_result['new_exam_date'] = new_date
                    date_examen_vtc_result['new_exam_session_id'] = new_session_id

                    # Appliquer la mise à jour CRM immédiatement
                    try:
                        self.crm_client.update_deal(deal_id, {'Date_examen_VTC': new_session_id})
                        logger.info(f"  ✅ CRM mis à jour: Date_examen_VTC → {new_date}")

                        # Mettre à jour enriched_lookups pour que la réponse utilise la nouvelle date
                        enriched_lookups['date_examen'] = new_date
                    except Exception as e:
                        logger.error(f"  ❌ Erreur mise à jour CRM Date_examen_VTC: {e}")

            # ================================================================
            # AUTO-ASSIGNATION: Appliquer les mises à jour CRM si détectées
            # ================================================================
            if resultat_info['dossier_termine'] and date_examen_vtc_result.get('auto_assigned'):
                logger.warning(f"  🛑 GUARD RAIL: Dossier terminé (Resultat={resultat_raw}) → AUTO-ASSIGNATION BLOQUÉE")
                date_examen_vtc_result['auto_assigned'] = False
                date_examen_vtc_result['crm_updates'] = {}

            if date_examen_vtc_result.get('auto_assigned') and date_examen_vtc_result.get('crm_updates'):
                crm_updates = date_examen_vtc_result['crm_updates']
                logger.info(f"  🔄 AUTO-ASSIGNATION détectée - Mises à jour CRM à appliquer: {list(crm_updates.keys())}")

                if deal_id:
                    try:
                        self.crm_client.update_deal(deal_id, crm_updates)
                        logger.info(f"  ✅ Mises à jour CRM appliquées: {crm_updates}")

                        # Log détaillé des assignations
                        if crm_updates.get('Date_examen_VTC'):
                            logger.info(f"     → Date_examen_VTC: {date_examen_vtc_result.get('auto_assigned_exam_date')}")
                        if crm_updates.get('Session'):
                            session_name = date_examen_vtc_result.get('auto_assigned_session', {}).get('Name', 'N/A')
                            logger.info(f"     → Session: {session_name}")
                        if crm_updates.get('Preference_horaire'):
                            logger.info(f"     → Preference_horaire: {crm_updates.get('Preference_horaire')}")
                    except Exception as e:
                        logger.error(f"  ❌ Erreur lors de la mise à jour CRM: {e}")
                else:
                    logger.warning("  ⚠️ Pas de deal_id - impossible d'appliquer les mises à jour CRM")

            # ================================================================
            # CONFIRMATION DE DATE D'EXAMEN: Vérifier et valider la date demandée
            # ================================================================
            confirmed_exam_date_valid = False
            confirmed_exam_date_id = None
            confirmed_exam_date_info = None
            confirmed_exam_date_unavailable = False
            available_exam_dates_for_dept = []

            intent_for_date_check = IntentParser(triage_result)
            confirmed_new_exam_date = intent_for_date_check.confirmed_new_exam_date
            detected_intent_for_date = triage_result.get('detected_intent', '')

            if confirmed_new_exam_date and detected_intent_for_date in DATE_CONFIRMATION_INTENTS:
                logger.info(f"  📅 Date d'examen confirmée par le candidat: {confirmed_new_exam_date}")

                # Trouver le département du candidat
                current_dept = None
                if date_examen_vtc_result.get('current_departement'):
                    current_dept = str(date_examen_vtc_result.get('current_departement'))
                elif date_examen_vtc_result.get('date_examen_info', {}).get('Departement'):
                    current_dept = str(date_examen_vtc_result.get('date_examen_info', {}).get('Departement'))

                if current_dept:
                    # Vérifier si la date existe pour ce département
                    from src.utils.date_examen_vtc_helper import get_next_exam_dates
                    dept_dates = get_next_exam_dates(self.crm_client, current_dept, limit=20)
                    available_exam_dates_for_dept = dept_dates

                    # Chercher la date confirmée
                    for d in dept_dates:
                        if d.get('Date_Examen') == confirmed_new_exam_date:
                            confirmed_exam_date_valid = True
                            confirmed_exam_date_id = d.get('id')
                            confirmed_exam_date_info = d
                            logger.info(f"  ✅ Date {confirmed_new_exam_date} DISPONIBLE pour département {current_dept} (ID: {confirmed_exam_date_id})")
                            break

                    if not confirmed_exam_date_valid:
                        confirmed_exam_date_unavailable = True
                        logger.warning(f"  ⚠️ Date {confirmed_new_exam_date} NON DISPONIBLE pour département {current_dept}")
                        logger.info(f"  📅 Dates disponibles: {[d.get('Date_Examen') for d in dept_dates[:5]]}")
                else:
                    logger.warning(f"  ⚠️ Département non trouvé, impossible de vérifier la date")

            # ================================================================
            # V3 FALLBACK: Confirmation de date via V3 Conversation Analyzer
            # Si le triage n'a pas extrait confirmed_new_exam_date mais V3 a
            # détecté un target_date confirmé par le candidat (date_choice explicite)
            # ET que cette date était dans les proposed_dates d'un META précédent
            # (= date proposée par le système, pas inventée par le candidat)
            # ================================================================
            if not confirmed_exam_date_valid and conversation_state and conversation_state.target_date:
                v3_has_date_choice = any(
                    d.type == 'date_choice' and d.confidence == 'explicit'
                    for d in conversation_state.candidate_decisions
                )
                if v3_has_date_choice:
                    v3_target = conversation_state.target_date  # YYYY-MM-DD
                    logger.info(f"  📅 V3 FALLBACK: target_date={v3_target} avec date_choice explicite")

                    # Vérifier que cette date était dans les proposed_dates d'un META précédent
                    previously_proposed = False
                    if thread_memory_result and thread_memory_result.has_history:
                        for rec in thread_memory_result.previous_records:
                            if v3_target in (rec.proposed_dates or []):
                                previously_proposed = True
                                logger.info(f"  ✅ V3 FALLBACK: Date {v3_target} trouvée dans META proposed_dates")
                                break

                    if previously_proposed:
                        # Vérifier engagement level (guard rail: VALIDE CMA, clôture passée, etc.)
                        evalbox_for_v3 = deal_data.get('Evalbox', '') if deal_data else ''
                        cloture_for_v3 = date_examen_vtc_result.get('date_cloture')
                        from src.utils.date_examen_vtc_helper import classify_engagement_level as _classify_eng_v3
                        engagement_v3 = _classify_eng_v3(evalbox_for_v3, cloture_for_v3, examt3p_data)

                        if engagement_v3.get('can_reposition', False):
                            # Trouver la date dans les dates du département
                            current_dept_v3 = None
                            if date_examen_vtc_result.get('current_departement'):
                                current_dept_v3 = str(date_examen_vtc_result['current_departement'])
                            elif date_examen_vtc_result.get('date_examen_info', {}).get('Departement'):
                                current_dept_v3 = str(date_examen_vtc_result['date_examen_info']['Departement'])

                            if current_dept_v3:
                                from src.utils.date_examen_vtc_helper import get_next_exam_dates as _get_dates_v3
                                dept_dates_v3 = _get_dates_v3(self.crm_client, current_dept_v3, limit=20)

                                for d in dept_dates_v3:
                                    if str(d.get('Date_Examen', ''))[:10] == v3_target:
                                        confirmed_exam_date_valid = True
                                        confirmed_exam_date_id = d.get('id')
                                        confirmed_exam_date_info = d
                                        confirmed_new_exam_date = v3_target
                                        logger.info(f"  ✅ V3 FALLBACK: Date {v3_target} VALIDÉE pour dept {current_dept_v3} (ID: {confirmed_exam_date_id}, engagement level {engagement_v3['level']})")
                                        break

                                if not confirmed_exam_date_valid:
                                    logger.warning(f"  ⚠️ V3 FALLBACK: Date {v3_target} non disponible pour département {current_dept_v3}")
                            else:
                                logger.warning(f"  ⚠️ V3 FALLBACK: Département non trouvé")
                        else:
                            logger.warning(f"  🛑 V3 FALLBACK: Engagement level {engagement_v3['level']} → repositionnement bloqué ({engagement_v3['description']})")
                    else:
                        logger.info(f"  ℹ️ V3: target_date={v3_target} pas dans META proposed_dates → pas de confirmation CRM automatique")

            # ================================================================
            # ENRICHISSEMENT: Si intention date-related avec mois/lieu spécifiques
            # ================================================================
            # Inclut REPORT_DATE, DEMANDE_DATES_FUTURES, DEMANDE_AUTRES_DATES
            # DATE_RELATED_INTENTS imported from src.constants.intents
            if triage_result.get('primary_intent') in DATE_RELATED_INTENTS:
                intent = IntentParser(triage_result)
                requested_month = intent.requested_month
                requested_location = intent.requested_location  # Nom original (ex: "Montpellier")
                requested_dept_code = intent.requested_dept_code  # Code département (ex: "34")

                if requested_month or requested_location or requested_dept_code:
                    from src.utils.date_examen_vtc_helper import search_dates_for_month_and_location

                    # Utiliser le code département extrait par TriageAgent (prioritaire)
                    # Fallback: département du candidat depuis son deal/date_examen
                    dept_for_search = requested_dept_code or requested_location
                    if not dept_for_search:
                        # Département du candidat depuis date_examen_info
                        candidate_dept = date_examen_vtc_result.get('current_departement') or \
                                         date_examen_vtc_result.get('date_examen_info', {}).get('Departement')
                        if candidate_dept:
                            dept_for_search = str(candidate_dept)
                            logger.info(f"  📍 Département fallback depuis deal: {dept_for_search}")
                    if requested_dept_code:
                        logger.info(f"  📍 Département extrait par TriageAgent: {requested_dept_code} (location: {requested_location})")

                    # Récupérer la date d'examen actuelle pour l'exclure des alternatives
                    current_exam_date = date_examen_vtc_result.get('date_examen_info', {}).get('Date_Examen')

                    search_result = search_dates_for_month_and_location(
                        crm_client=self.crm_client,
                        requested_month=requested_month,
                        requested_location=dept_for_search,
                        candidate_region=date_examen_vtc_result.get('candidate_region'),
                        current_exam_date=current_exam_date
                    )

                    # Propager les résultats
                    date_examen_vtc_result['no_date_for_requested_month'] = search_result['no_date_for_requested_month']
                    date_examen_vtc_result['requested_month_name'] = search_result['requested_month_name']
                    date_examen_vtc_result['requested_location'] = requested_location  # Nom original pour l'affichage
                    date_examen_vtc_result['requested_dept_code'] = requested_dept_code  # Code département
                    date_examen_vtc_result['same_month_other_depts'] = search_result['same_month_other_depts']
                    date_examen_vtc_result['same_dept_other_months'] = search_result['same_dept_other_months']
                    date_examen_vtc_result['exact_match_dates'] = search_result.get('exact_match_dates', [])

                    if search_result['no_date_for_requested_month']:
                        logger.info(f"  ⚠️ Pas de date en {search_result['requested_month_name']} sur {requested_location or requested_dept_code}")

            # ================================================================
            # ENRICHISSEMENT: Implicit Date Repositioning
            # ================================================================
            # Si le candidat demande une formation APRÈS sa date d'examen → repositionnement implicite
            intent_for_repositioning = IntentParser(triage_result)
            if intent_for_repositioning.implicit_date_repositioning and intent_for_repositioning.detected_intent == 'REPORT_DATE':
                from src.utils.date_examen_vtc_helper import classify_engagement_level

                evalbox_for_engagement = date_examen_vtc_result.get('evalbox_status', '')
                cloture_for_engagement = date_examen_vtc_result.get('date_cloture', '')
                engagement = classify_engagement_level(evalbox_for_engagement, cloture_for_engagement, examt3p_data)

                date_examen_vtc_result['engagement_level'] = engagement
                date_examen_vtc_result['implicit_date_repositioning'] = True

                logger.info(f"  🔄 REPOSITIONNEMENT IMPLICITE: niveau {engagement['level']} — {engagement['description']}")

                if not engagement['can_reposition']:
                    # Niveaux 3-4: laisser le flow report_bloqué existant gérer
                    date_examen_vtc_result['report_bloque_engagement'] = True
                    logger.info(f"  ❌ Report bloqué (engagement niveau {engagement['level']})")
                else:
                    # Niveaux 0-2: Charger la date cible du mois demandé pour le département
                    requested_month = intent_for_repositioning.requested_month
                    current_dept = date_examen_vtc_result.get('current_departement') or date_examen_vtc_result.get('date_examen_info', {}).get('Departement')
                    if requested_month and current_dept:
                        from src.utils.date_examen_vtc_helper import get_next_exam_dates
                        dept_dates = get_next_exam_dates(self.crm_client, str(current_dept), limit=20)
                        # Filtrer pour le mois demandé
                        from datetime import datetime as dt_repo
                        target_dates = []
                        for d in dept_dates:
                            try:
                                d_date = dt_repo.strptime(str(d.get('Date_Examen', ''))[:10], '%Y-%m-%d')
                                if d_date.month == requested_month:
                                    target_dates.append(d)
                            except Exception:
                                continue
                        if target_dates:
                            # Stocker la date cible pour CRM update (STEP 5) et sessions
                            target = target_dates[0]
                            date_examen_vtc_result['repositioning_target_date'] = target
                            date_examen_vtc_result['repositioning_target_date_id'] = target.get('id')
                            date_examen_vtc_result['repositioning_target_date_str'] = target.get('Date_Examen', '')
                            logger.info(f"  ✅ Date cible repositionnement: {target.get('Date_Examen')} (dept {current_dept}, ID: {target.get('id')})")
                        else:
                            logger.warning(f"  ⚠️ Aucune date trouvée en mois {requested_month} pour département {current_dept}")
                    else:
                        logger.warning(f"  ⚠️ Impossible de charger la date cible: month={requested_month}, dept={current_dept}")

            # ================================================================
            # GARDE-FOU: REPORT_DATE → DEMANDE_DATE_PLUS_TOT si mois demandé < date actuelle
            # ================================================================
            # Le triage LLM confond parfois REPORT_DATE et DEMANDE_DATE_PLUS_TOT.
            # Si le candidat demande un mois AVANT sa date d'examen actuelle,
            # c'est une demande de date plus tôt, pas un report.
            _triage_intent = triage_result.get('detected_intent', '')
            if _triage_intent == 'REPORT_DATE':
                _intent_ctx = triage_result.get('intent_context', {})
                _requested_month = _intent_ctx.get('requested_month')
                _current_date_str = date_examen_vtc_result.get('date_examen_info', {}).get('Date_Examen', '') if date_examen_vtc_result.get('date_examen_info') else ''
                if _requested_month and _current_date_str:
                    try:
                        from datetime import datetime as _dt
                        _current_date = _dt.strptime(str(_current_date_str)[:10], '%Y-%m-%d')
                        _requested_month_int = int(_requested_month)
                        if 1 <= _requested_month_int <= 12 and _requested_month_int < _current_date.month:
                            logger.info(f"  🔄 GARDE-FOU: REPORT_DATE → DEMANDE_DATE_PLUS_TOT (mois demandé {_requested_month_int} < date actuelle {_current_date.month}/{_current_date.year})")
                            triage_result['detected_intent'] = 'DEMANDE_DATE_PLUS_TOT'
                            triage_result['primary_intent'] = 'DEMANDE_DATE_PLUS_TOT'
                            _intent_ctx['wants_earlier_date'] = True
                            triage_result['intent_context'] = _intent_ctx
                    except (ValueError, TypeError) as e:
                        logger.debug(f"  ⚠️ Garde-fou REPORT_DATE: erreur parsing date: {e}")

            # ================================================================
            # ENRICHISSEMENT: Dates alternatives si candidat demande date plus tôt
            # ================================================================
            # Si le candidat demande explicitement une date plus proche
            # → Charger les dates alternatives d'autres départements
            # → TOUJOURS vérifier, même si compte ExamT3P existe (on signalera le process)
            intent = IntentParser(triage_result)
            wants_earlier_date = intent.wants_earlier_date
            is_early_date_intent = intent.is_early_date_intent
            can_choose_other_dept = date_examen_vtc_result.get('can_choose_other_department', False)
            current_dept = date_examen_vtc_result.get('current_departement') or date_examen_vtc_result.get('departement')

            # Déclencher si intention explicite OU flag wants_earlier_date
            if (is_early_date_intent or wants_earlier_date) and current_dept:
                logger.info(f"  🚀 Candidat demande date plus tôt (intent={intent.detected_intent}, wants_earlier={wants_earlier_date})")
                from src.utils.date_examen_vtc_helper import get_earlier_dates_other_departments

                # Trouver la date de référence (date actuelle assignée ou première date du dept)
                current_dates = date_examen_vtc_result.get('next_dates', [])
                reference_date = None
                if date_examen_vtc_result.get('date_examen_info', {}).get('Date_Examen'):
                    reference_date = date_examen_vtc_result['date_examen_info']['Date_Examen']
                elif current_dates:
                    reference_date = current_dates[0].get('Date_Examen')

                if reference_date:
                    # Utiliser le helper enrichi avec priorite regionale et urgence
                    from src.utils.cross_department_helper import get_cross_department_alternatives
                    compte_existe = examt3p_data.get('compte_existe', False)

                    cross_dept_data = get_cross_department_alternatives(
                        self.crm_client,
                        current_dept=current_dept,
                        reference_date=reference_date,
                        compte_existe=compte_existe,
                        limit=5
                    )

                    # Stocker les donnees enrichies
                    date_examen_vtc_result['cross_department_data'] = cross_dept_data

                    # Retrocompatibilite: populer alternative_department_dates avec toutes les options
                    all_options = cross_dept_data.get('same_region_options', []) + cross_dept_data.get('other_region_options', [])

                    # Flag pour le template: y a-t-il des options plus tôt ?
                    date_examen_vtc_result['has_earlier_options'] = bool(all_options)

                    if all_options:
                        date_examen_vtc_result['alternative_department_dates'] = all_options
                        date_examen_vtc_result['should_include_in_response'] = True
                        logger.info(f"  📅 {len(all_options)} date(s) plus tôt (region: {len(cross_dept_data.get('same_region_options', []))}, autres: {len(cross_dept_data.get('other_region_options', []))})")
                    else:
                        # Aucune date plus tôt disponible - garder date actuelle
                        logger.info("  ⚠️ Aucune date plus tôt disponible (clôtures passées) - garder date actuelle")
                        date_examen_vtc_result['no_earlier_dates_available'] = True
                        # NE PAS afficher les dates ultérieures pour cette intention
                        if is_early_date_intent:
                            date_examen_vtc_result['suppress_next_dates'] = True
                else:
                    # Pas de date de référence - impossible de chercher plus tôt
                    date_examen_vtc_result['has_earlier_options'] = False
                    if is_early_date_intent:
                        date_examen_vtc_result['suppress_next_dates'] = True

            # ================================================================
            # ENRICHISSEMENT: Cross-département pour clarification/discordance
            # ================================================================
            # Si le candidat mentionne un mois en mode clarification OU avec discordance
            # ET est dans un état pré-convocation → proposer alternatives de ce mois
            if not date_examen_vtc_result.get('month_cross_department'):
                # Réutilise l'IntentParser créé plus haut (ou en crée un si pas encore fait)
                if 'intent' not in dir() or intent is None:
                    intent = IntentParser(triage_result)

                mentioned_month = intent.mentioned_month
                mentions_discrepancy = intent.mentions_discrepancy
                communication_mode = intent.communication_mode
                can_choose_other_dept = date_examen_vtc_result.get('can_choose_other_department', False)
                current_dept = date_examen_vtc_result.get('departement')

                # Condition: mois mentionné + (clarification OU discordance) + pré-convocation
                should_search_month = (
                    mentioned_month and
                    can_choose_other_dept and
                    current_dept and
                    (communication_mode == 'clarification' or mentions_discrepancy)
                )

                if should_search_month:
                    logger.info(f"  🔍 Mode {communication_mode} avec mois {mentioned_month} mentionné - recherche cross-département")
                    from src.utils.cross_department_helper import get_dates_for_month_other_departments

                    compte_existe = examt3p_data.get('compte_existe', False)
                    month_options = get_dates_for_month_other_departments(
                        crm_client=self.crm_client,
                        current_dept=current_dept,
                        requested_month=mentioned_month,
                        compte_existe=compte_existe,
                        limit=5
                    )

                    date_examen_vtc_result['month_cross_department'] = month_options
                    date_examen_vtc_result['has_month_in_other_depts'] = month_options.get('has_month_options', False)
                    date_examen_vtc_result['mentioned_month'] = mentioned_month

                    if month_options.get('has_month_options'):
                        logger.info(f"  ✅ Alternatives trouvées pour mois {mentioned_month}")
                        # Propager le nom du mois pour l'affichage
                        date_examen_vtc_result['requested_month_name'] = month_options.get('requested_month_name')
        else:
            # Construire le message de raison du skip
            skip_reason_msg = {
                'credentials_invalid': 'identifiants invalides',
                'dossier_not_received': 'dossier non reçu'
            }.get(skip_reason, None)
            # Gérer les cas Uber dynamiquement (uber_case_A, uber_case_B, uber_case_D, uber_case_E)
            if not skip_reason_msg and skip_reason and skip_reason.startswith('uber_case_'):
                uber_case = skip_reason.replace('uber_case_', '')
                skip_reason_msg = f'CAS {uber_case} Uber'
            skip_reason_msg = skip_reason_msg or skip_reason or 'raison inconnue'
            logger.info(f"  📅 Vérification date examen VTC... SKIPPED ({skip_reason_msg})")

        # ================================================================
        # VÉRIFICATION COHÉRENCE FORMATION / EXAMEN
        # ================================================================
        # Cas critique: candidat a manqué sa formation + examen imminent
        # → Proposer 2 options: maintenir examen (e-learning suffit) ou reporter (force majeure requise)
        from src.utils.training_exam_consistency_helper import analyze_training_exam_consistency

        training_exam_consistency_result = {}
        if not skip_date_session_analysis:
            logger.info("  🔍 Vérification cohérence formation/examen...")
            training_exam_consistency_result = analyze_training_exam_consistency(
                deal_data=deal_data,
                threads=threads_data,
                session_data=session_data,
                crm_client=self.crm_client
            )

            if training_exam_consistency_result.get('has_consistency_issue'):
                logger.warning(f"  🚨 PROBLÈME DE COHÉRENCE DÉTECTÉ: {training_exam_consistency_result['issue_type']}")
                logger.info(f"  📅 Examen prévu le: {training_exam_consistency_result['exam_date_formatted']}")
                if training_exam_consistency_result.get('next_exam_date_formatted'):
                    logger.info(f"  📅 Prochaine date disponible: {training_exam_consistency_result['next_exam_date_formatted']}")
                if training_exam_consistency_result.get('force_majeure_detected'):
                    logger.info(f"  📋 Force majeure détectée: {training_exam_consistency_result['force_majeure_type']}")
                logger.info("  → Réponse avec options A/B sera proposée au candidat")
            else:
                logger.info("  ✅ Pas de problème de cohérence formation/examen")
        else:
            logger.info(f"  🔍 Vérification cohérence formation/examen... SKIPPED ({skip_reason_msg})")

        # ================================================================
        # ANALYSE SESSIONS DE FORMATION
        # ================================================================
        # Si des dates d'examen sont proposées OU si date examen assignée mais pas de session
        from src.utils.session_helper import analyze_session_situation

        next_dates = date_examen_vtc_result.get('next_dates', [])
        date_examen_info = date_examen_vtc_result.get('date_examen_info')

        # Vérifier si session déjà assignée dans CRM
        current_session = deal_data.get('Session')
        session_is_empty = not current_session

        # Détecter erreur de saisie session (session passée impossible)
        from src.utils.training_exam_consistency_helper import detect_session_assignment_error
        session_error_check = detect_session_assignment_error(deal_data, enriched_lookups)
        has_session_assignment_error = session_error_check.get('is_assignment_error', False)
        session_year_error_corrected = None  # Session corrigée si erreur d'année
        if has_session_assignment_error:
            logger.warning(f"  🚨 ERREUR SAISIE SESSION détectée: {session_error_check.get('session_name')} (créé {session_error_check.get('days_difference')} jours après)")

        # ================================================================
        # LOGIQUE PRIORITÉ DATES POUR SESSIONS:
        # ================================================================
        # 1. Si CONFIRMATION_SESSION + date assignée → sessions pour cette date uniquement
        # 2. Si REPORT_DATE + alternatives trouvées → sessions pour les dates ALTERNATIVES (pas la date actuelle)
        # 3. Sinon si next_dates existe → utiliser next_dates
        # 4. Sinon si date assignée + session vide → utiliser date assignée
        # ================================================================
        # IntentParser centralisé pour cette section
        intent = IntentParser(triage_result)
        detected_intent = intent.detected_intent  # Rétrocompatibilité
        has_assigned_date = date_examen_info and isinstance(date_examen_info, dict) and date_examen_info.get('Date_Examen')

        # CAS SPÉCIAL: Date passée + non validé (CAS 2) → traiter comme date vide
        # Le candidat n'a jamais été inscrit à l'examen, proposer la prochaine date du département
        date_case = date_examen_vtc_result.get('case')
        if date_case == 2:
            current_dept = date_examen_vtc_result.get('current_departement') or (date_examen_info.get('Departement') if date_examen_info else None)
            if current_dept:
                from src.utils.date_examen_vtc_helper import get_next_exam_dates
                exam_dates_for_session = get_next_exam_dates(self.crm_client, current_dept, limit=2)
                logger.info(f"  📚 CAS 2 (date passée non validée) → prochaines dates département {current_dept}: {len(exam_dates_for_session)}")
            else:
                exam_dates_for_session = next_dates if next_dates else []
                logger.info(f"  📚 CAS 2 (date passée non validée) → next_dates par défaut")
        elif conversation_state and conversation_state.target_date and any(
            d.type == 'date_choice' and d.confidence == 'explicit'
            for d in conversation_state.candidate_decisions
        ):
            # CAS V3: Candidat a confirmé une date via V3 conversation analysis
            v3_target = conversation_state.target_date
            current_dept = date_examen_vtc_result.get('current_departement') or (date_examen_info.get('Departement') if date_examen_info else None)
            v3_matched = False
            if current_dept:
                from src.utils.date_examen_vtc_helper import get_next_exam_dates
                dept_dates = get_next_exam_dates(self.crm_client, str(current_dept), limit=20)
                matching = [d for d in dept_dates if str(d.get('Date_Examen', ''))[:10] == v3_target]
                if matching:
                    exam_dates_for_session = matching
                    v3_matched = True
                    logger.info(f"  📚 V3: Session loading ciblé sur target_date={v3_target} (confirmed by candidate)")
            if not v3_matched:
                # Fall through to normal logic
                exam_dates_for_session = next_dates if next_dates else []
                logger.info(f"  📚 V3: target_date={v3_target} not matched in dept dates, using next_dates fallback")
        elif has_assigned_date and detected_intent == 'CONFIRMATION_SESSION':
            # Vérifier si la clôture de la date actuelle est passée (CAS 8)
            if date_case == 8 and date_examen_vtc_result.get('deadline_passed_reschedule'):
                # CAS 8 + CONFIRMATION_SESSION: La clôture est passée
                # → Utiliser la NOUVELLE date proposée, pas l'ancienne
                new_exam_date = date_examen_vtc_result.get('new_exam_date')
                if new_exam_date and next_dates:
                    exam_dates_for_session = [d for d in next_dates if d.get('Date_Examen') == new_exam_date]
                    logger.info(f"  📚 CONFIRMATION_SESSION + CLÔTURE PASSÉE (CAS 8)")
                    logger.info(f"     → Ancienne date: {date_examen_info.get('Date_Examen')} (clôture passée)")
                    logger.info(f"     → Nouvelle date: {new_exam_date} → sessions pour cette date")
                else:
                    exam_dates_for_session = next_dates if next_dates else []
                    logger.warning(f"  📚 CONFIRMATION_SESSION + CAS 8: pas de nouvelle date trouvée, utilisation next_dates")
            else:
                # CAS normal: Candidat confirme sa session → utiliser SA date assignée
                exam_dates_for_session = [date_examen_info]
                logger.info(f"  📚 CONFIRMATION_SESSION + date assignée ({date_examen_info.get('Date_Examen')}) → sessions pour cette date uniquement")
        elif detected_intent in DATE_CONFIRMATION_INTENTS:
            # CAS 2: REPORT_DATE ou CONFIRMATION_DATE_EXAMEN
            current_date = date_examen_info.get('Date_Examen') if date_examen_info else None
            current_dept = date_examen_vtc_result.get('current_departement') or date_examen_vtc_result.get('date_examen_info', {}).get('Departement')

            # CAS 2-IMPLICITE: Repositionnement implicite → sessions pour la date cible UNIQUEMENT
            if date_examen_vtc_result.get('implicit_date_repositioning') and date_examen_vtc_result.get('repositioning_target_date'):
                target_date = date_examen_vtc_result['repositioning_target_date']
                exam_dates_for_session = [target_date]
                logger.info(f"  📚 REPOSITIONNEMENT IMPLICITE: sessions pour date cible {target_date.get('Date_Examen')} uniquement")
            # CAS 2a: Date confirmée par le candidat → charger sessions pour CETTE date
            elif confirmed_exam_date_valid and confirmed_exam_date_info:
                exam_dates_for_session = [confirmed_exam_date_info]
                logger.info(f"  📚 DATE CONFIRMÉE: {confirmed_exam_date_info.get('Date_Examen')} → sessions pour cette date")
            # CAS 2b: Date demandée non disponible → afficher alternatives
            elif confirmed_exam_date_unavailable and available_exam_dates_for_dept:
                exam_dates_for_session = available_exam_dates_for_dept
                logger.info(f"  📚 DATE NON DISPONIBLE: affichage de {len(available_exam_dates_for_dept)} alternative(s)")
            # CAS 2c: Pas de date spécifique → charger les dates du département
            elif current_dept:
                from src.utils.date_examen_vtc_helper import get_next_exam_dates
                dept_dates = get_next_exam_dates(self.crm_client, current_dept, limit=10)
                # Filtrer la date actuelle
                exam_dates_for_session = [d for d in dept_dates if d.get('Date_Examen') != current_date]
                logger.info(f"  📚 REPORT_DATE: {len(exam_dates_for_session)} date(s) du département {current_dept} (date actuelle {current_date} exclue)")
            else:
                exam_dates_for_session = []
                logger.warning(f"  📚 REPORT_DATE: département non trouvé, pas de dates chargées")
        elif next_dates:
            # CAS 3: Nouvelles dates proposées (changement de date ou première attribution)
            # Si deadline_passed_reschedule, on ne propose que la nouvelle date (pas toutes les next_dates)
            if date_examen_vtc_result.get('deadline_passed_reschedule') and date_examen_vtc_result.get('new_exam_date'):
                new_date = date_examen_vtc_result['new_exam_date']
                exam_dates_for_session = [d for d in next_dates if d.get('Date_Examen') == new_date]
                logger.info(f"  📚 DEADLINE PASSÉE → sessions uniquement pour la nouvelle date: {new_date}")
            else:
                exam_dates_for_session = next_dates
        elif has_assigned_date and session_is_empty:
            # CAS 4: Pas de nouvelles dates, mais date existante et session vide
            exam_dates_for_session = [date_examen_info]
            logger.info("  📚 Session vide mais date examen assignée - recherche sessions correspondantes...")
        elif has_session_assignment_error and has_assigned_date:
            # CAS 5: Erreur de saisie session → proposer sessions pour la date d'examen assignée
            exam_dates_for_session = [date_examen_info]
            logger.info(f"  📚 ERREUR SAISIE SESSION → recherche sessions avant date examen {date_examen_info.get('Date_Examen')}")
        elif training_exam_consistency_result.get('has_consistency_issue') and has_assigned_date:
            # CAS 6: Formation manquée + examen futur → proposer sessions de rafraîchissement
            exam_dates_for_session = [date_examen_info]
            logger.info(f"  📚 FORMATION MANQUÉE + examen futur → recherche sessions de rafraîchissement pour {date_examen_info.get('Date_Examen')}")
        elif (detected_intent == 'DEMANDE_CHANGEMENT_SESSION' or 'DEMANDE_CHANGEMENT_SESSION' in triage_result.get('secondary_intents', [])) and has_assigned_date:
            # CAS 7: Demande de changement de session avec date d'examen assignée
            # → proposer sessions alternatives avant cette date d'examen
            # NOTE: Vérifie aussi les intentions secondaires (ex: principale=ENVOIE_IDENTIFIANTS + secondaire=DEMANDE_CHANGEMENT_SESSION)
            # CASCADE: Charger date actuelle + prochaine date pour proposer des alternatives
            exam_dates_for_session = [date_examen_info]
            current_dept = date_examen_vtc_result.get('current_departement') or date_examen_info.get('Departement')
            if current_dept:
                evalbox_for_cascade = enriched_lookups.get('evalbox_status') or deal_data.get('Evalbox', '')
                cloture_for_cascade = date_examen_vtc_result.get('date_cloture')
                from src.utils.date_examen_vtc_helper import classify_engagement_level as _classify_eng_cas7
                engagement_cas7 = _classify_eng_cas7(evalbox_for_cascade, cloture_for_cascade, examt3p_data)
                if engagement_cas7.get('can_reposition'):
                    from src.utils.date_examen_vtc_helper import get_next_exam_dates as _get_next_dates_cas7
                    next_dept_dates = _get_next_dates_cas7(self.crm_client, str(current_dept), limit=3)
                    for d in next_dept_dates:
                        if d.get('Date_Examen') != date_examen_info.get('Date_Examen'):
                            exam_dates_for_session.append(d)
                            break
            logger.info(f"  📚 DEMANDE_CHANGEMENT_SESSION + date assignée → {len(exam_dates_for_session)} date(s) pour recherche sessions")
        else:
            exam_dates_for_session = []

        # ================================================================
        # CAS 6b: Session passée (enriched_lookups) + examen futur
        # ================================================================
        # Détecte le cas où deal_data['Session'] est null mais les dates viennent du lookup Session1
        # Ce cas n'est pas détecté par training_exam_consistency car il regarde deal_data['Session']
        if not exam_dates_for_session and has_assigned_date and enriched_lookups.get('session_date_fin'):
            from datetime import datetime as dt_local
            try:
                session_end = dt_local.strptime(enriched_lookups['session_date_fin'], '%Y-%m-%d').date()
                exam_date_str = date_examen_info.get('Date_Examen', '') if date_examen_info else ''
                exam_date_parsed = dt_local.strptime(exam_date_str, '%Y-%m-%d').date() if exam_date_str else None
                today_local = dt_local.now().date()
                if session_end < today_local and exam_date_parsed and exam_date_parsed > today_local:
                    exam_dates_for_session = [date_examen_info]
                    logger.info(f"  📚 CAS 6b: SESSION PASSÉE (fin: {session_end}) + examen futur ({exam_date_parsed}) → recherche nouvelles sessions")
            except (ValueError, TypeError) as e:
                logger.debug(f"  ⚠️ CAS 6b: Erreur parsing dates session/examen: {e}")

        # Pour REPORT_DATE, toujours chercher les sessions des dates alternatives
        is_report_date = detected_intent == 'REPORT_DATE'
        is_session_change_request = detected_intent == 'DEMANDE_CHANGEMENT_SESSION' or 'DEMANDE_CHANGEMENT_SESSION' in triage_result.get('secondary_intents', [])
        is_session_complaint = is_session_change_request and intent.is_complaint
        # Pour DEMANDE_CHANGEMENT_SESSION avec dates spécifiques, on n'a pas besoin de exam_dates_for_session
        has_specific_dates = intent.has_date_range_request if is_session_change_request else False

        # Safety net: Si changement de session demandé mais exam_dates_for_session n'a qu'1 date
        # (possible si V3 a overridé CAS 7), ajouter la prochaine date pour la cascade d'alternatives
        if is_session_change_request and exam_dates_for_session and has_assigned_date and len(exam_dates_for_session) < 2:
            _sn_dept = date_examen_vtc_result.get('current_departement') or (date_examen_info.get('Departement') if date_examen_info else None)
            if _sn_dept:
                _sn_evalbox = enriched_lookups.get('evalbox_status') or deal_data.get('Evalbox', '')
                _sn_cloture = date_examen_vtc_result.get('date_cloture')
                from src.utils.date_examen_vtc_helper import classify_engagement_level as _classify_eng_sn
                _sn_engagement = _classify_eng_sn(_sn_evalbox, _sn_cloture, examt3p_data)
                if _sn_engagement.get('can_reposition'):
                    from src.utils.date_examen_vtc_helper import get_next_exam_dates as _get_next_dates_sn
                    _sn_current_dates = {d.get('Date_Examen') for d in exam_dates_for_session}
                    _sn_next_dates = _get_next_dates_sn(self.crm_client, str(_sn_dept), limit=3)
                    for d in _sn_next_dates:
                        if d.get('Date_Examen') not in _sn_current_dates:
                            exam_dates_for_session.append(d)
                            logger.info(f"  📚 CHANGEMENT SESSION safety net: +1 date ({d.get('Date_Examen')})")
                            break

        # Détecter si la session assignée est passée (pour CAS 6b)
        session_is_passed = False
        if enriched_lookups.get('session_date_fin'):
            try:
                from datetime import datetime as dt_check
                session_end_check = dt_check.strptime(enriched_lookups['session_date_fin'], '%Y-%m-%d').date()
                session_is_passed = session_end_check < dt_check.now().date()
            except (ValueError, TypeError):
                pass
        should_analyze_sessions = (
            not skip_date_session_analysis
            and (exam_dates_for_session or has_specific_dates or is_session_complaint)  # Permettre le matching même sans dates d'examen, ou sur plainte
            and (date_examen_vtc_result.get('should_include_in_response') or session_is_empty or is_report_date or is_session_change_request or has_session_assignment_error or session_is_passed)
        )

        if should_analyze_sessions:
            logger.info("  📚 Recherche des sessions de formation associées...")
            # Récupérer la préférence du TriageAgent via IntentParser
            triage_session_pref = intent.session_preference

            # NOTE: Pas de blocage pour documents manquants ou credentials invalides
            # Le candidat peut choisir sa session même avec un dossier incomplet
            # La complétion des documents est un processus séparé
            session_confirmation_blocked = False
            session_blocking_reason = None

            if session_confirmation_blocked:
                # Ne pas proposer de sessions - créer un session_data minimal avec la raison du blocage
                session_data = {
                    'session_preference': triage_session_pref,
                    'proposed_options': [],
                    'sessions_proposees': [],
                    'session_confirmation_blocked': True,
                    'session_blocking_reason': session_blocking_reason,
                }
            # ================================================================
            # NOUVEAU: Matching par dates spécifiques demandées
            # ================================================================
            elif intent.has_date_range_request:
                from src.utils.session_helper import match_sessions_by_date_range

                requested_dates = intent.requested_training_dates
                logger.info(f"  📅 Dates spécifiques demandées: {requested_dates.get('raw_text', 'N/A')}")

                # Utiliser la préférence effective (explicite ou inférée des dates)
                effective_pref = intent.effective_session_preference
                if effective_pref:
                    logger.info(f"  ➡️ Préférence effective: {effective_pref}")

                # Matching des sessions par dates demandées
                match_result = match_sessions_by_date_range(
                    crm_client=self.crm_client,
                    requested_dates=requested_dates,
                    session_type=effective_pref
                )

                # Construire session_data avec les résultats du matching
                session_data = {
                    'session_preference': effective_pref,
                    'has_date_range_request': True,
                    'requested_dates_raw': requested_dates.get('raw_text', ''),
                    'match_type': match_result.get('match_type'),
                    'date_range_match': match_result,
                    'proposed_options': [],  # Format standard pour compatibilité
                    'sessions_proposees': match_result.get('sessions_proposees', []),
                    'closest_before': match_result.get('closest_before'),
                    'closest_after': match_result.get('closest_after'),
                    # Sessions par type (jour/soir) pour proposer les deux quand pas de préférence
                    'closest_before_jour': match_result.get('closest_before_jour'),
                    'closest_before_soir': match_result.get('closest_before_soir'),
                    'closest_after_jour': match_result.get('closest_after_jour'),
                    'closest_after_soir': match_result.get('closest_after_soir'),
                    # Fallback: type demandé indisponible, alternatives d'un autre type
                    'no_sessions_of_requested_type': match_result.get('no_sessions_of_requested_type', False),
                    'alternative_type': match_result.get('alternative_type'),
                    'alternative_type_label': match_result.get('alternative_type_label', ''),
                }

                logger.info(f"  🎯 Résultat matching: {match_result.get('match_type')} ({len(match_result.get('sessions_proposees', []))} session(s))")
                if match_result.get('closest_before'):
                    cb = match_result.get('closest_before')
                    logger.info(f"  📅 Closest before: {cb.get('Name')} ({cb.get('date_debut')} - {cb.get('date_fin')})")
                if match_result.get('closest_after'):
                    ca = match_result.get('closest_after')
                    logger.info(f"  📅 Closest after: {ca.get('Name')} ({ca.get('date_debut')} - {ca.get('date_fin')})")

            else:
                # Flux standard: analyze_session_situation
                # V3: Si le candidat a confirmé un changement de date, il faut proposer
                # une nouvelle session adaptée à la nouvelle date (même type que l'existante)
                v3_date_change = bool(
                    conversation_state and conversation_state.target_date
                    and any(d.type == 'date_choice' and d.confidence == 'explicit' for d in conversation_state.candidate_decisions)
                )
                # Pour changement de session, charger TOUS les types (jour + soir) pour la cascade d'alternatives
                session_pref_for_loading = None if is_session_change_request else triage_session_pref
                _is_explicit_change = (detected_intent in SESSION_CHANGE_INTENTS or is_session_change_request)
                session_data = analyze_session_situation(
                    deal_data=deal_data,
                    exam_dates=exam_dates_for_session,
                    threads=threads_data,
                    crm_client=self.crm_client,
                    triage_session_preference=session_pref_for_loading,
                    allow_change=(_is_explicit_change or date_examen_vtc_result.get('implicit_date_repositioning') or v3_date_change),
                    enriched_lookups=enriched_lookups,
                    is_explicit_session_change=_is_explicit_change
                )

            if session_data.get('session_preference'):
                logger.info(f"  ➡️ Préférence détectée: {session_data['session_preference']}")
            if session_data.get('proposed_options'):
                logger.info(f"  ✅ {len(session_data['proposed_options'])} option(s) de session proposée(s)")

            # ================================================================
            # CASCADE D'ALTERNATIVES (DEMANDE_CHANGEMENT_SESSION)
            # ================================================================
            if is_session_change_request and not has_specific_dates and not is_session_complaint:
                current_session_type = enriched_lookups.get('session_type')  # 'jour' ou 'soir'
                current_session_id = deal_data.get('Session', {}).get('id') if isinstance(deal_data.get('Session'), dict) else None
                _cascade_evalbox = enriched_lookups.get('evalbox_status') or deal_data.get('Evalbox', '')
                _cascade_cloture = date_examen_vtc_result.get('date_cloture')
                from src.utils.date_examen_vtc_helper import classify_engagement_level as _classify_eng_cascade
                _cascade_engagement = _classify_eng_cascade(_cascade_evalbox, _cascade_cloture, examt3p_data)

                session_data = self._apply_session_change_cascade(
                    session_data, current_session_type, current_session_id, _cascade_engagement
                )

            # ================================================================
            # CORRECTION AUTOMATIQUE ERREUR D'ANNÉE (mars 2024 → mars 2026)
            # ================================================================
            if has_session_assignment_error and session_error_check.get('error_type') == 'wrong_year':
                wrong_month = session_error_check.get('wrong_session_month')
                session_type = session_error_check.get('wrong_session_type')  # 'jour' ou 'soir'
                proposed = session_data.get('proposed_options', [])

                # Extraire toutes les sessions (proposed_options imbriqué OU sessions_proposees flat)
                all_sessions = []
                if proposed:
                    for opt in proposed:
                        sessions_list = opt.get('sessions', [])
                        all_sessions.extend(sessions_list)
                elif session_data.get('sessions_proposees'):
                    all_sessions = list(session_data['sessions_proposees'])

                if all_sessions and wrong_month and session_type:
                    logger.info(f"  🔍 Recherche session corrigée: mois={wrong_month}, type={session_type}")
                    from src.utils.date_utils import parse_date_flexible

                    # Chercher la session qui correspond au même mois
                    best_match = None
                    for sess in all_sessions:
                        date_fin_str = sess.get('Date_fin')
                        if date_fin_str:
                            date_fin = parse_date_flexible(date_fin_str)
                            if date_fin and date_fin.month == wrong_month:
                                best_match = sess
                                break

                    # Si pas de match exact sur le mois, prendre la première session disponible
                    if not best_match and all_sessions:
                        best_match = all_sessions[0]

                    if best_match:
                        session_year_error_corrected = {
                            'id': best_match.get('id'),
                            'Name': best_match.get('Name'),
                            'session_type': best_match.get('session_type'),
                            'date_debut': best_match.get('Date_d_but'),
                            'date_fin': best_match.get('Date_fin'),
                        }
                        logger.info(f"  ✅ SESSION CORRIGÉE AUTOMATIQUEMENT: {session_year_error_corrected.get('Name')} ({session_year_error_corrected.get('date_debut')} - {session_year_error_corrected.get('date_fin')})")

            # ================================================================
            # VÉRIFICATION PLAINTE SESSION (erreur CAB)
            # ================================================================
            if is_session_change_request and intent.is_complaint:
                logger.info("  ⚠️ PLAINTE SESSION détectée - vérification de l'erreur...")
                from src.utils.session_helper import verify_session_complaint

                # Récupérer la date d'examen pour chercher des sessions alternatives
                exam_date_for_complaint = date_examen_info.get('Date_Examen') if date_examen_info else None

                complaint_verification = verify_session_complaint(
                    crm_client=self.crm_client,
                    claimed_session=intent.claimed_session,
                    assigned_session=deal_data.get('Session'),
                    enriched_lookups=enriched_lookups,
                    session_preference=intent.session_preference,
                    exam_date=exam_date_for_complaint
                )

                # Stocker les résultats dans session_data
                session_data['is_complaint'] = True
                session_data['is_cab_error'] = complaint_verification.get('is_cab_error', False)
                session_data['complaint_error_type'] = complaint_verification.get('error_type', 'NO_ERROR')
                session_data['complaint_verification'] = complaint_verification.get('verification_details', '')
                session_data['corrected_session'] = complaint_verification.get('matched_session')
                session_data['complaint_alternatives'] = complaint_verification.get('alternatives', [])
                session_data['assigned_session_info'] = complaint_verification.get('assigned_session_info', {})
                session_data['claimed_session_info'] = complaint_verification.get('claimed_session_info', {})
                # Nouvelles variables pour proposer TOUTES les sessions quand pas de type spécifié
                session_data['has_all_sessions'] = complaint_verification.get('has_all_sessions', False)
                session_data['all_sessions_jour'] = complaint_verification.get('all_sessions_jour', [])
                session_data['all_sessions_soir'] = complaint_verification.get('all_sessions_soir', [])

                if complaint_verification.get('is_cab_error'):
                    logger.info(f"  ✅ ERREUR CAB CONFIRMÉE: {complaint_verification.get('verification_details')}")
                    # Stocker les infos de la session corrigée pour mise à jour CRM
                    corrected = complaint_verification.get('matched_session')
                    if corrected:
                        session_data['cab_error_corrected'] = True
                        session_data['cab_error_corrected_session_id'] = corrected.get('id')
                        session_data['cab_error_corrected_session_name'] = corrected.get('Name')
                        session_data['cab_error_corrected_session_type'] = corrected.get('session_type')
                        logger.info(f"  📊 Session corrigée: {corrected.get('Name')} (ID: {corrected.get('id')})")
                else:
                    logger.info(f"  ❌ Pas d'erreur CAB: {complaint_verification.get('verification_details')}")

        elif skip_date_session_analysis:
            logger.info(f"  📚 Recherche sessions... SKIPPED (raison: {skip_reason})")

        # INFO: Ancien dossier (pour information uniquement, ne bloque plus)
        ancien_dossier = False
        if deal_data.get('Date_de_depot_CMA'):
            date_depot = deal_data['Date_de_depot_CMA']
            if date_depot < '2025-11-01':
                ancien_dossier = True
                logger.info("ℹ️  Ancien dossier (avant 01/11/2025) - traitement normal")

        # ================================================================
        # NETTOYAGE date_examen_vtc_result POUR CONFIRMATION_SESSION
        # ================================================================
        # Si c'est une confirmation de session avec date assignée,
        # on ne veut pas que l'IA propose des dates alternatives
        if has_assigned_date and detected_intent == 'CONFIRMATION_SESSION':
            # Remplacer next_dates par uniquement la date assignée
            date_examen_vtc_result = dict(date_examen_vtc_result)  # Copie pour ne pas modifier l'original
            date_examen_vtc_result['next_dates'] = [date_examen_info]
            date_examen_vtc_result['alternative_department_dates'] = []  # Pas d'alternatives
            logger.info("  📝 CONFIRMATION_SESSION: dates alternatives supprimées du contexte IA")

        # ================================================================
        # MATCHING SESSION CONFIRMÉE PAR LE CANDIDAT
        # ================================================================
        # Si le candidat a confirmé sa session avec des dates (ex: "du 16/03 au 27/03"),
        # on essaie de matcher avec les sessions proposées pour mettre à jour le CRM.
        session_confirmed = False
        matched_session_id = None
        matched_session_name = None
        matched_session_type = None
        matched_session_start = None
        matched_session_end = None
        matched_session_already_started = False

        if detected_intent == 'CONFIRMATION_SESSION':
            confirmed_dates = intent.confirmed_session_dates
            session_preference = intent.session_preference  # 'jour' ou 'soir'

            # Fallback: utiliser requested_training_dates si confirmed_session_dates est vide
            # Le triage peut retourner les dates dans l'un ou l'autre champ
            requested_dates = intent.requested_training_dates
            if not confirmed_dates and requested_dates:
                start = requested_dates.get('start_date', '')
                end = requested_dates.get('end_date', '')
                if start and end:
                    # Convertir du format YYYY-MM-DD au format DD/MM/YYYY-DD/MM/YYYY
                    from src.utils.date_utils import parse_date_flexible
                    start_dt = parse_date_flexible(start)
                    end_dt = parse_date_flexible(end)
                    if start_dt and end_dt:
                        confirmed_dates = f"{start_dt.strftime('%d/%m/%Y')}-{end_dt.strftime('%d/%m/%Y')}"
                        logger.info(f"  📅 Dates extraites de requested_training_dates: {confirmed_dates}")

            # Fallback 2: extraire les dates directement du message candidat via regex
            # Ex: "du 13 Avril au 24" → "13/04/2026-24/04/2026"
            # IMPORTANT: nettoyer le contenu cité/forwardé pour éviter de matcher
            # les dates de la réponse CAB précédente (ex: "du 16/03/2026 au 27/03/2026")
            if not confirmed_dates:
                customer_msg = triage_result.get('customer_message', '')
                if customer_msg:
                    from business_rules import BusinessRules
                    clean_msg = BusinessRules.strip_forwarded_content(customer_msg)
                    extracted = self._extract_dates_from_message(clean_msg)
                    if extracted:
                        confirmed_dates = extracted
                        logger.info(f"  📅 Dates extraites du message candidat (regex): {confirmed_dates}")

            matched = None

            # 1. Essayer matching par dates si fournies
            if confirmed_dates and session_data and session_data.get('proposed_options'):
                logger.info(f"  🔍 Matching session par dates: {confirmed_dates}")
                matched = self._match_session_by_confirmed_dates(
                    confirmed_dates,
                    session_data['proposed_options']
                )
                if not matched:
                    logger.warning(f"  ⚠️ Aucune session ne matche les dates: {confirmed_dates}")

            # 1b. Si pas de match et sessions_proposees disponibles (cas has_date_range_request)
            if not matched and confirmed_dates and session_data and session_data.get('sessions_proposees'):
                logger.info(f"  🔍 Matching session par dates dans sessions_proposees: {confirmed_dates}")
                matched = self._match_session_in_flat_list(
                    confirmed_dates,
                    session_data['sessions_proposees']
                )
                if not matched:
                    logger.warning(f"  ⚠️ Aucune session ne matche les dates dans sessions_proposees")

            # 2. Sinon, essayer matching par préférence (jour/soir)
            if not matched and session_preference and session_data and session_data.get('proposed_options'):
                logger.info(f"  🔍 Matching session par préférence: {session_preference}")
                matched = self._match_session_by_preference(
                    session_preference,
                    session_data['proposed_options']
                )

            # 2b. Matching par préférence dans sessions_proposees
            if not matched and session_preference and session_data and session_data.get('sessions_proposees'):
                logger.info(f"  🔍 Matching session par préférence dans sessions_proposees: {session_preference}")
                matched = self._match_session_by_preference_flat(
                    session_preference,
                    session_data['sessions_proposees']
                )

            # 3. Résultat du matching
            if matched:
                session_confirmed = True
                matched_session_id = matched.get('id')
                matched_session_name = matched.get('name')
                matched_session_type = matched.get('session_type')
                matched_session_start = matched.get('Date_d_but')
                matched_session_end = matched.get('Date_fin')
                matched_session_already_started = matched.get('already_started', False)
                logger.info(f"  ✅ Session matchée: {matched_session_name} (ID: {matched_session_id})")
                logger.info(f"     Du {matched_session_start} au {matched_session_end}")
                if matched_session_already_started:
                    logger.info(f"     ⚠️ Session déjà commencée (Date_debut < aujourd'hui)")
            elif session_preference:
                # Le candidat a exprimé une préférence mais on n'a pas pu matcher
                logger.warning(f"  ⚠️ Préférence '{session_preference}' exprimée mais aucune session disponible")

        return {
            'contact_data': contact_data,  # Données du contact lié (First_Name, Last_Name)
            'deal_id': deal_id,
            'deal_data': deal_data,
            'date_examen_vtc_value': date_examen_vtc_value,  # Date réelle extraite du lookup
            'examt3p_data': examt3p_data,
            'uber_eligibility_result': uber_eligibility_result,  # Éligibilité Uber 20€
            'date_examen_vtc_result': date_examen_vtc_result,
            'evalbox_data': evalbox_data,
            'session_data': session_data,
            'threads': threads_data,  # threads_data déjà récupérés au début
            'ancien_dossier': ancien_dossier,
            # Nouveaux champs pour traçabilité
            'sync_result': sync_result,  # Résultat sync ExamT3P → CRM
            'ticket_confirmations': ticket_confirmations,  # Confirmations extraites du ticket
            # Flag critique: identifiants invalides = SEUL sujet de la réponse
            # IMPORTANT: credentials_only_response = True UNIQUEMENT si skip_reason == 'credentials_invalid'
            # Pour les cas Uber A/B, on utilise uber_case_response avec le message pré-généré
            # Pour D/E, on utilise uber_case_alert (alerte dans réponse normale)
            'credentials_only_response': skip_reason == 'credentials_invalid',
            'uber_case_response': uber_case_blocks_dates,  # True seulement pour CAS A/B
            'uber_case_alert': uber_case_alert,  # Pour CAS D/E: alerte à inclure dans réponse normale
            'skip_reason': skip_reason,  # Raison du skip (credentials_invalid, uber_case_X, dossier_not_received)
            'dossier_not_received': dossier_not_received_blocks_dates,
            'uber_case_blocks_dates': uber_case_blocks_dates,
            # Cohérence formation/examen (cas manqué formation + examen imminent)
            'training_exam_consistency_result': training_exam_consistency_result,
            # Dates deja communiquees (anti-repetition)
            'dates_already_communicated': dates_already_communicated,
            'dates_proposed_recently': dates_proposed_recently,
            'sessions_proposed_recently': sessions_proposed_recently,
            'cab_proposals': cab_proposals,
            # ThreadMemory - mémoire persistante (suppression sections, progression, relance)
            'thread_memory': thread_memory_result,
            # Conversation Intelligence V3
            'conversation_state': conversation_state,
            # Mode de communication du candidat (request/clarification/verification/follow_up)
            'communication_mode': communication_mode,
            'references_previous_communication': references_previous,
            'mentions_discrepancy': mentions_discrepancy,
            'is_clarification_mode': communication_mode == 'clarification',
            'is_verification_mode': communication_mode == 'verification',
            'is_follow_up_mode': communication_mode == 'follow_up',
            # Demande de complétion dossier précédente
            'previously_asked_to_complete': previously_asked_to_complete,
            # Lookups CRM enrichis (v2.2) - données complètes depuis les modules Zoho
            'enriched_lookups': enriched_lookups,
            'lookup_cache': lookup_cache,
            # Session confirmée par le candidat (CONFIRMATION_SESSION avec dates)
            'session_confirmed': session_confirmed,
            'matched_session_id': matched_session_id,
            'matched_session_name': matched_session_name,
            'matched_session_type': matched_session_type,
            'matched_session_start': matched_session_start,
            'matched_session_end': matched_session_end,
            'session_already_started': matched_session_already_started,
            # Correction erreur CAB (DEMANDE_CHANGEMENT_SESSION avec plainte)
            'cab_error_corrected': session_data.get('cab_error_corrected', False) if session_data else False,
            'cab_error_corrected_session_id': session_data.get('cab_error_corrected_session_id') if session_data else None,
            'cab_error_corrected_session_name': session_data.get('cab_error_corrected_session_name') if session_data else None,
            'cab_error_corrected_session_type': session_data.get('cab_error_corrected_session_type') if session_data else None,
            # Erreur de saisie session (A5) - session passée impossible
            'session_assignment_error': has_session_assignment_error,
            'session_error_data': session_error_check if has_session_assignment_error else {},
            # Correction automatique erreur d'année (session mars 2024 → mars 2026)
            'session_year_error_corrected': session_year_error_corrected is not None,
            'session_year_error_corrected_id': session_year_error_corrected.get('id') if session_year_error_corrected else None,
            'session_year_error_corrected_name': session_year_error_corrected.get('Name') if session_year_error_corrected else None,
            'session_year_error_corrected_type': session_year_error_corrected.get('session_type') if session_year_error_corrected else None,
            'session_year_error_corrected_start': session_year_error_corrected.get('date_debut') if session_year_error_corrected else None,
            'session_year_error_corrected_end': session_year_error_corrected.get('date_fin') if session_year_error_corrected else None,
            # Confirmation de date d'examen (CONFIRMATION_DATE_EXAMEN / REPORT_DATE avec date spécifique)
            'confirmed_exam_date_valid': confirmed_exam_date_valid,
            'confirmed_exam_date_id': confirmed_exam_date_id,
            'confirmed_exam_date_info': confirmed_exam_date_info,
            'confirmed_exam_date_unavailable': confirmed_exam_date_unavailable,
            'available_exam_dates_for_dept': available_exam_dates_for_dept,
            'confirmed_new_exam_date': confirmed_new_exam_date,
            # Resultat CRM — lifecycle (pour guard rail STEP 5)
            'dossier_termine': resultat_info['dossier_termine'],
            'resultat_raw': resultat_raw,
            'resultat_category': resultat_info['category'],
        }

    def _match_session_by_confirmed_dates(
        self,
        confirmed_dates: str,
        proposed_options: List[Dict]
    ) -> Optional[Dict]:
        """
        Matche une session confirmée par le candidat avec les sessions proposées.

        Supporte deux formats:
        - "DD/MM/YYYY-DD/MM/YYYY" (range début-fin)
        - "DD/MM/YYYY" (date unique → matching par start_date seulement)

        Args:
            confirmed_dates: Dates au format "DD/MM/YYYY-DD/MM/YYYY" ou "DD/MM/YYYY"
            proposed_options: Liste des options de session proposées

        Returns:
            Dict avec id, name, session_type si trouvé, None sinon
        """
        from src.utils.date_utils import parse_date_flexible

        try:
            # Déterminer si c'est une date unique ou un range
            parts = confirmed_dates.split('-')
            single_date_mode = len(parts) == 1

            if single_date_mode:
                # Date unique → matching par start_date seulement
                confirmed_start = parse_date_flexible(parts[0].strip())
                if not confirmed_start:
                    logger.warning(f"Impossible de parser la date unique: {confirmed_dates}")
                    return None

                logger.info(f"  📅 Mode date unique: matching par start_date = {confirmed_start.strftime('%d/%m/%Y')}")

                for option in proposed_options:
                    sessions = option.get('sessions', [])
                    for session in sessions:
                        session_start = parse_date_flexible(session.get('Date_d_but', ''))
                        if not session_start:
                            continue

                        if abs((session_start - confirmed_start).days) <= 1:
                            session_type = session.get('session_type', '')
                            session_name = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else session.get('Name', '')

                            return {
                                'id': session.get('id'),
                                'name': session_name,
                                'session_type': session_type,
                                'Date_d_but': session.get('Date_d_but'),
                                'Date_fin': session.get('Date_fin'),
                                'already_started': session.get('already_started', False),
                            }

                return None

            # Range mode: "DD/MM/YYYY-DD/MM/YYYY"
            if len(parts) != 2:
                logger.warning(f"Format dates confirmées invalide: {confirmed_dates}")
                return None

            start_str, end_str = parts[0].strip(), parts[1].strip()
            confirmed_start = parse_date_flexible(start_str)
            confirmed_end = parse_date_flexible(end_str)

            if not confirmed_start or not confirmed_end:
                logger.warning(f"Impossible de parser les dates: {start_str}, {end_str}")
                return None

            # Chercher dans les sessions proposées
            for option in proposed_options:
                sessions = option.get('sessions', [])
                for session in sessions:
                    session_start = parse_date_flexible(session.get('Date_d_but', ''))
                    session_end = parse_date_flexible(session.get('Date_fin', ''))

                    if not session_start or not session_end:
                        continue

                    # Vérifier si les dates correspondent (tolérance de 1 jour)
                    start_match = abs((session_start - confirmed_start).days) <= 1
                    end_match = abs((session_end - confirmed_end).days) <= 1

                    if start_match and end_match:
                        session_type = session.get('session_type', '')
                        session_name = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else session.get('Name', '')

                        return {
                            'id': session.get('id'),
                            'name': session_name,
                            'session_type': session_type,
                            'Date_d_but': session.get('Date_d_but'),
                            'Date_fin': session.get('Date_fin'),
                            'already_started': session.get('already_started', False),
                        }

            return None

        except Exception as e:
            logger.error(f"Erreur lors du matching de session: {e}")
            return None

    def _extract_dates_from_message(self, message: str) -> Optional[str]:
        """
        Extrait une plage de dates ou une date unique depuis le message du candidat.

        Patterns supportés:
        - "du 13 Avril au 24" → 13/04/YYYY-24/04/YYYY
        - "du 13/04 au 24/04" → 13/04/YYYY-24/04/YYYY
        - "du 13 avril au 24 avril" → 13/04/YYYY-24/04/YYYY
        - "formation du 13 au 24 avril" → 13/04/YYYY-24/04/YYYY
        - "le 09/02" → 09/02/YYYY (date unique)
        - "le 9 février" → 09/02/YYYY (date unique)

        Returns:
            String "DD/MM/YYYY-DD/MM/YYYY" (range) ou "DD/MM/YYYY" (date unique) ou None
        """
        import re
        from datetime import datetime

        # Nettoyage HTML basique
        clean = re.sub(r'<[^>]+>', ' ', message)
        clean = clean.lower().strip()

        MONTH_MAP = {
            'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
            'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
            'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12, 'decembre': 12
        }
        month_pattern = '|'.join(MONTH_MAP.keys())

        now = datetime.now()

        # Pattern 1: "du DD mois au DD" (mois seulement sur la date de début)
        # Ex: "du 13 avril au 24"
        m = re.search(
            rf'du\s+(\d{{1,2}})\s+({month_pattern})\s+au\s+(\d{{1,2}})\b',
            clean
        )
        if m:
            day1, month_name, day2 = int(m.group(1)), m.group(2), int(m.group(3))
            month = MONTH_MAP[month_name]
            year = now.year if month >= now.month else now.year + 1
            return f"{day1:02d}/{month:02d}/{year}-{day2:02d}/{month:02d}/{year}"

        # Pattern 2: "du DD mois au DD mois" (mois sur les deux dates)
        # Ex: "du 13 avril au 24 avril"
        m = re.search(
            rf'du\s+(\d{{1,2}})\s+({month_pattern})\s+au\s+(\d{{1,2}})\s+({month_pattern})',
            clean
        )
        if m:
            day1, month_name1, day2, month_name2 = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
            month1, month2 = MONTH_MAP[month_name1], MONTH_MAP[month_name2]
            year1 = now.year if month1 >= now.month else now.year + 1
            year2 = now.year if month2 >= now.month else now.year + 1
            return f"{day1:02d}/{month1:02d}/{year1}-{day2:02d}/{month2:02d}/{year2}"

        # Pattern 3: "du DD/MM au DD/MM" ou "du DD/MM/YYYY au DD/MM/YYYY"
        m = re.search(
            r'du\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\s+au\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?',
            clean
        )
        if m:
            day1, month1 = int(m.group(1)), int(m.group(2))
            year1 = int(m.group(3)) if m.group(3) else (now.year if month1 >= now.month else now.year + 1)
            if year1 < 100:
                year1 += 2000
            day2, month2 = int(m.group(4)), int(m.group(5))
            year2 = int(m.group(6)) if m.group(6) else (now.year if month2 >= now.month else now.year + 1)
            if year2 < 100:
                year2 += 2000
            return f"{day1:02d}/{month1:02d}/{year1}-{day2:02d}/{month2:02d}/{year2}"

        # Pattern 4: "du DD au DD mois" (mois seulement sur la fin)
        # Ex: "du 13 au 24 avril"
        m = re.search(
            rf'du\s+(\d{{1,2}})\s+au\s+(\d{{1,2}})\s+({month_pattern})',
            clean
        )
        if m:
            day1, day2, month_name = int(m.group(1)), int(m.group(2)), m.group(3)
            month = MONTH_MAP[month_name]
            year = now.year if month >= now.month else now.year + 1
            return f"{day1:02d}/{month:02d}/{year}-{day2:02d}/{month:02d}/{year}"

        # Pattern 5: "le DD/MM" ou "le DD/MM/YYYY" (date unique, souvent confirmation d'une session proposée)
        # Ex: "le 09/02" → "09/02/2026" (date unique, pas de range)
        # Negative lookahead: ne pas matcher si suivi de " au" (c'est un range, géré par Pattern 3)
        m = re.search(
            r'(?:le|du)\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?!\s*au)',
            clean
        )
        if m:
            day1, month1 = int(m.group(1)), int(m.group(2))
            year1 = int(m.group(3)) if m.group(3) else (now.year if month1 >= now.month else now.year + 1)
            if year1 < 100:
                year1 += 2000
            return f"{day1:02d}/{month1:02d}/{year1}"

        # Pattern 6: "le DD mois" (date unique avec nom de mois)
        # Ex: "le 9 février" → "09/02/2026"
        m = re.search(
            rf'le\s+(\d{{1,2}})\s+({month_pattern})',
            clean
        )
        if m:
            day1 = int(m.group(1))
            month1 = MONTH_MAP[m.group(2)]
            year1 = now.year if month1 >= now.month else now.year + 1
            return f"{day1:02d}/{month1:02d}/{year1}"

        return None

    def _match_session_by_preference(
        self,
        preference: str,
        proposed_options: List[Dict]
    ) -> Optional[Dict]:
        """
        Matche une session par préférence jour/soir.

        Quand le candidat confirme juste "cours du soir" sans dates précises,
        on sélectionne la première session disponible correspondant à cette préférence.

        Args:
            preference: 'jour' ou 'soir'
            proposed_options: Liste des options de session proposées

        Returns:
            Dict avec id, name, session_type si trouvé, None sinon
        """
        try:
            for option in proposed_options:
                sessions = option.get('sessions', [])
                for session in sessions:
                    session_type = session.get('session_type', '')

                    if session_type == preference:
                        session_name = 'Cours du jour' if preference == 'jour' else 'Cours du soir'

                        logger.info(f"  ✅ Session matchée par préférence: {session_name}")
                        logger.info(f"     Du {session.get('Date_d_but', '')} au {session.get('Date_fin', '')}")

                        return {
                            'id': session.get('id'),
                            'name': session_name,
                            'session_type': preference,
                            'Date_d_but': session.get('Date_d_but'),
                            'Date_fin': session.get('Date_fin'),
                        }

            logger.warning(f"  ⚠️ Aucune session de type '{preference}' trouvée")
            return None

        except Exception as e:
            logger.error(f"Erreur lors du matching par préférence: {e}")
            return None

    def _match_session_in_flat_list(
        self,
        confirmed_dates: str,
        sessions_list: List[Dict]
    ) -> Optional[Dict]:
        """
        Matche une session par dates dans une liste plate de sessions.

        Utilisé pour matcher dans sessions_proposees (format flat) quand
        proposed_options (format imbriqué) est vide.

        Supporte deux formats:
        - "DD/MM/YYYY-DD/MM/YYYY" (range début-fin)
        - "DD/MM/YYYY" (date unique → matching par start_date seulement)

        Args:
            confirmed_dates: Dates au format "DD/MM/YYYY-DD/MM/YYYY" ou "DD/MM/YYYY"
            sessions_list: Liste plate de sessions

        Returns:
            Dict avec id, name, session_type si trouvé, None sinon
        """
        from src.utils.date_utils import parse_date_flexible

        try:
            # Déterminer si c'est une date unique ou un range
            parts = confirmed_dates.split('-')
            single_date_mode = len(parts) == 1

            if single_date_mode:
                confirmed_start = parse_date_flexible(parts[0].strip())
                if not confirmed_start:
                    logger.warning(f"Impossible de parser la date unique: {confirmed_dates}")
                    return None

                logger.info(f"  📅 Recherche session (date unique): {confirmed_start.strftime('%d/%m/%Y')}")

                for session in sessions_list:
                    session_start = parse_date_flexible(session.get('Date_d_but', '') or session.get('date_debut', ''))
                    if not session_start:
                        continue

                    if abs((session_start - confirmed_start).days) <= 1:
                        session_type = session.get('session_type', '')
                        session_name = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else session.get('Name', '')

                        logger.info(f"  ✅ Session matchée dans liste plate (date unique): {session_name}")
                        logger.info(f"     Du {session.get('Date_d_but', '')} au {session.get('Date_fin', '')}")

                        return {
                            'id': session.get('id'),
                            'name': session_name,
                            'session_type': session_type,
                            'Date_d_but': session.get('Date_d_but'),
                            'Date_fin': session.get('Date_fin'),
                            'already_started': session.get('already_started', False),
                        }

                return None

            # Range mode
            if len(parts) != 2:
                logger.warning(f"Format dates confirmées invalide: {confirmed_dates}")
                return None

            start_str, end_str = parts[0].strip(), parts[1].strip()
            confirmed_start = parse_date_flexible(start_str)
            confirmed_end = parse_date_flexible(end_str)

            if not confirmed_start or not confirmed_end:
                logger.warning(f"Impossible de parser les dates: {start_str}, {end_str}")
                return None

            logger.info(f"  📅 Recherche session: {confirmed_start.strftime('%d/%m/%Y')} - {confirmed_end.strftime('%d/%m/%Y')}")

            # Chercher dans la liste plate de sessions
            for session in sessions_list:
                session_start = parse_date_flexible(session.get('Date_d_but', '') or session.get('date_debut', ''))
                session_end = parse_date_flexible(session.get('Date_fin', '') or session.get('date_fin', ''))

                if not session_start or not session_end:
                    continue

                # Vérifier si les dates correspondent (tolérance de 1 jour)
                start_match = abs((session_start - confirmed_start).days) <= 1
                end_match = abs((session_end - confirmed_end).days) <= 1

                if start_match and end_match:
                    session_type = session.get('session_type', '')
                    session_name = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else session.get('Name', '')

                    logger.info(f"  ✅ Session matchée dans liste plate: {session_name}")
                    logger.info(f"     Du {session.get('Date_d_but', '')} au {session.get('Date_fin', '')}")

                    return {
                        'id': session.get('id'),
                        'name': session_name,
                        'session_type': session_type,
                        'Date_d_but': session.get('Date_d_but'),
                        'Date_fin': session.get('Date_fin'),
                        'already_started': session.get('already_started', False),
                    }

            return None

        except Exception as e:
            logger.error(f"Erreur lors du matching dans liste plate: {e}")
            return None

    def _match_session_by_preference_flat(
        self,
        preference: str,
        sessions_list: List[Dict]
    ) -> Optional[Dict]:
        """
        Matche une session par préférence jour/soir dans une liste plate.

        Args:
            preference: 'jour' ou 'soir'
            sessions_list: Liste plate de sessions

        Returns:
            Dict avec id, name, session_type si trouvé, None sinon
        """
        try:
            for session in sessions_list:
                session_type = session.get('session_type', '')

                if session_type == preference:
                    session_name = 'Cours du jour' if preference == 'jour' else 'Cours du soir'

                    logger.info(f"  ✅ Session matchée par préférence (flat): {session_name}")
                    logger.info(f"     Du {session.get('Date_d_but', '')} au {session.get('Date_fin', '')}")

                    return {
                        'id': session.get('id'),
                        'name': session_name,
                        'session_type': preference,
                        'Date_d_but': session.get('Date_d_but'),
                        'Date_fin': session.get('Date_fin'),
                    }

            logger.warning(f"  ⚠️ Aucune session de type '{preference}' trouvée dans liste plate")
            return None

        except Exception as e:
            logger.error(f"Erreur lors du matching par préférence (flat): {e}")
            return None

    def _generate_duplicate_uber_response(
        self,
        ticket_id: str,
        triage_result: Dict
    ) -> Dict:
        """
        Génère une réponse pour les candidats ayant déjà bénéficié de l'offre Uber 20€.

        L'offre Uber 20€ n'est valable qu'UNE SEULE FOIS.
        Si le candidat souhaite se réinscrire, il devra :
        - Payer lui-même les frais d'examen (241€)
        - Gérer son inscription sur ExamT3P
        - Nous pouvons lui proposer la formation (VISIO ou présentiel)
        """
        logger.info("📝 Génération de la réponse DOUBLON UBER 20€...")

        duplicate_deals = triage_result.get('duplicate_deals', [])
        selected_deal = triage_result.get('selected_deal', {})

        # Formater les dates des opportunités précédentes
        previous_dates = []
        for deal in duplicate_deals:
            closing_date = deal.get('Closing_Date', 'N/A')
            deal_name = deal.get('Deal_Name', 'Opportunité')
            previous_dates.append(f"{deal_name} ({closing_date})")

        # --- Tri des deals: ancien (première offre) vs récent (inscription en cours) ---
        deals_sorted = sorted(
            duplicate_deals,
            key=lambda d: d.get('Closing_Date', '') or d.get('Created_Time', '') or '',
        )
        old_deal = deals_sorted[0] if deals_sorted else {}
        recent_deal = deals_sorted[-1] if len(deals_sorted) >= 2 else None

        # Cas B: l'opportunité récente est-elle vraiment différente de l'ancienne?
        has_recent_deal = recent_deal is not None and recent_deal.get('id') != old_deal.get('id')

        # Date de l'ANCIEN deal (quand le candidat a bénéficié de l'offre)
        old_exam_date_str = None
        old_date_exam = old_deal.get('Date_exam')
        if old_date_exam:
            old_exam_date_str = str(old_date_exam).strip()

        # Date du deal RÉCENT (opportunité en cours)
        recent_exam_date_str = None
        if has_recent_deal:
            recent_date_exam = recent_deal.get('Date_exam')
            if recent_date_exam:
                recent_exam_date_str = str(recent_date_exam).strip()

        # --- Logs enrichis ---
        logger.info(f"  📅 Ancien deal: {old_deal.get('Deal_Name', 'N/A')} | Date_exam: {old_exam_date_str or 'N/A'}")
        if has_recent_deal:
            logger.info(f"  📅 Deal récent: {recent_deal.get('Deal_Name', 'N/A')} | Date_exam: {recent_exam_date_str or 'N/A'}")
            logger.info(f"  📋 CAS B: Doublon avec opportunité récente en cours")
        else:
            logger.info(f"  📋 CAS A: Doublon sans opportunité récente")

        # --- Ligne "déjà bénéficié" — toujours basée sur l'ancien deal ---
        if old_exam_date_str:
            benefited_line = f"Après vérification de votre dossier, je constate que vous avez déjà bénéficié de l'offre Uber à 20€ pour le passage de l'examen VTC, avec une inscription à la date d'examen du {old_exam_date_str}."
        else:
            benefited_line = "Après vérification de votre dossier, je constate que vous avez déjà bénéficié de l'offre Uber à 20€ pour le passage de l'examen VTC."

        # --- Générer la réponse selon le cas ---
        if has_recent_deal:
            # CAS B: Candidat avec une inscription récente en plus de l'ancienne
            recent_ref = f" (date d'examen du {recent_exam_date_str})" if recent_exam_date_str else ""
            response_text = f"""Bonjour,

Je vous remercie pour votre message.

{benefited_line} Cette offre n'est valable qu'une seule fois par candidat.

Je constate que vous avez une inscription récente{recent_ref}. Cependant, nous ne sommes pas en mesure de prendre en charge une seconde inscription dans le cadre de l'offre Uber à 20€.

Pour cette inscription, les frais d'examen (241€) restent à votre charge et doivent être réglés en autonomie auprès de la CMA via le site ExamT3P : https://www.exament3p.fr

Si vous avez besoin d'une formation de préparation à l'examen VTC, nous pouvons vous proposer :

📚 Formation en présentiel : sur l'un de nos centres de formation

📚 Formation E-learning

Ces formations sont finançables via votre CPF (Compte Personnel de Formation).

Bien cordialement,

L'équipe Cab Formations"""
        else:
            # CAS A: Candidat avec uniquement un ancien deal (pas d'inscription récente)
            response_text = f"""Bonjour,

Je vous remercie pour votre message.

{benefited_line} Cette offre n'est valable qu'une seule fois par candidat.

Si vous souhaitez vous réinscrire à l'examen VTC, voici vos options :

OPTION 1 : Inscription autonome

• Vous pouvez vous inscrire vous-même sur le site de la CMA (ExamT3P)
• Les frais d'inscription à l'examen s'élèvent à 241€, à votre charge
• Site d'inscription : https://www.exament3p.fr

OPTION 2 : Formation avec CAB Formations
Si vous souhaitez suivre une formation de préparation à l'examen VTC, nous pouvons vous proposer :

📚 Formation en présentiel : sur l'un de nos centres de formation

📚 Formation E-learning

Ces deux formations sont finançables via votre CPF (Compte Personnel de Formation).

Merci de me préciser si vous êtes intéressé(e) par l'une de ces options, et je vous transmettrai les tarifs et disponibilités.

Bien cordialement,

L'équipe Cab Formations"""

        logger.info(f"✅ Réponse DOUBLON générée ({len(response_text)} caractères)")

        return {
            'response_text': response_text,
            'is_duplicate_uber_response': True,
            'duplicate_deals_count': len(duplicate_deals),
            'previous_dates': previous_dates,
            'has_recent_deal': has_recent_deal,
            'old_deal_name': old_deal.get('Deal_Name'),
            'recent_deal_name': recent_deal.get('Deal_Name') if has_recent_deal else None,
            'crm_updates': {},  # Pas de mise à jour CRM pour les doublons
            'detected_scenarios': ['DUPLICATE_UBER_OFFER']
        }

    def _generate_duplicate_clarification_response(
        self,
        ticket_id: str,
        triage_result: Dict
    ) -> Dict:
        """
        Génère une réponse pour demander des clarifications quand un doublon
        potentiel est détecté par nom + code postal mais avec email/téléphone différents.

        Permet d'éviter les homonymes en demandant au candidat de confirmer
        ses coordonnées utilisées lors de sa précédente inscription.

        La réponse s'adapte à l'intention du candidat (STATUT_DOSSIER, DEMANDE_IDENTIFIANTS, etc.)
        """
        logger.info("📝 Génération de la réponse CLARIFICATION DOUBLON...")

        duplicate_contact_info = triage_result.get('duplicate_contact_info', {})
        duplicate_type = triage_result.get('duplicate_type', '')
        duplicate_deal_name = duplicate_contact_info.get('duplicate_deal_name', 'un dossier')
        detected_intent = triage_result.get('detected_intent', '')

        # Déterminer si le doublon est récupérable
        is_recoverable = duplicate_type in ['RECOVERABLE_REFUS_CMA', 'RECOVERABLE_NOT_PAID', 'RECOVERABLE_PAID']

        # Message adapté à l'intention du candidat
        if detected_intent == 'STATUT_DOSSIER':
            intro = "Pour vérifier l'état de votre dossier"
        elif detected_intent == 'DEMANDE_REINSCRIPTION':
            intro = "Bonne nouvelle ! Nous avons retrouvé votre dossier. Pour reprendre votre inscription"
        elif detected_intent in ['DEMANDE_IDENTIFIANTS', 'ENVOIE_IDENTIFIANTS']:
            intro = "Pour vous transmettre vos identifiants en toute sécurité"
        elif detected_intent in ['DEMANDE_DATES_FUTURES', 'DEMANDE_DATE_EXAMEN', 'REPORT_DATE']:
            intro = "Avant de vous communiquer les dates disponibles"
        elif detected_intent in ['DEMANDE_ELEARNING_ACCESS', 'DEMANDE_DATE_VISIO']:
            intro = "Pour vous donner accès à votre formation"
        elif detected_intent == 'DEMANDE_CONVOCATION':
            intro = "Pour vérifier votre convocation"
        else:
            intro = "Afin de nous assurer qu'il s'agit bien de vous et non d'un homonyme"

        # Note sur la possibilité de récupérer le dossier
        recovery_note = ""
        if is_recoverable:
            if duplicate_type == 'RECOVERABLE_REFUS_CMA':
                recovery_note = "\n\nSi c'est bien vous, votre précédent dossier avait été refusé par la CMA. Bonne nouvelle : vous pouvez vous réinscrire en utilisant la même offre Uber 20€ !"
            else:
                recovery_note = "\n\nSi c'est bien vous, nous pourrons reprendre votre dossier existant et poursuivre votre inscription !"

        response_text = f"""Bonjour,

Je vous remercie pour votre message.

Nous avons trouvé un dossier existant ({duplicate_deal_name}) dans notre système qui correspond à votre nom et code postal.

{intro}, merci de nous confirmer :

• L'adresse email utilisée lors de votre précédente inscription
• Le numéro de téléphone renseigné à l'époque{recovery_note}

Dans l'attente de votre retour, je reste à votre disposition.

Bien cordialement,

L'équipe Cab Formations"""

        logger.info(f"✅ Réponse CLARIFICATION DOUBLON générée ({len(response_text)} caractères)")
        logger.info(f"   Intention adaptée: {detected_intent or 'générique'}")

        return {
            'response_text': response_text,
            'is_duplicate_clarification_response': True,
            'duplicate_type': duplicate_type,
            'is_recoverable': is_recoverable,
            'duplicate_contact_info': duplicate_contact_info,
            'detected_intent': detected_intent,
            'crm_updates': {},  # Pas de mise à jour CRM pour les clarifications
            'detected_scenarios': ['DUPLICATE_CLARIFICATION']
        }

    def _generate_duplicate_recoverable_response(
        self,
        ticket_id: str,
        triage_result: Dict
    ) -> Dict:
        """
        Génère une réponse pour les doublons récupérables.

        Cas récupérables :
        - RECOVERABLE_PAID : Dossier Synchronisé (payé, en attente validation) → peut reprendre
        - RECOVERABLE_REFUS_CMA : Dossier précédemment refusé par la CMA (payé) → peut se réinscrire
        - RECOVERABLE_NOT_PAID : Inscription jamais finalisée (pas de paiement) → peut reprendre

        Dans ces cas, le candidat peut reprendre son inscription avec la même offre Uber 20€.
        """
        logger.info("📝 Génération de la réponse DOUBLON RÉCUPÉRABLE...")

        duplicate_type = triage_result.get('duplicate_type', '')
        duplicate_deals = triage_result.get('duplicate_deals', [])
        already_paid_to_cma = triage_result.get('already_paid_to_cma', False)

        # Déterminer le message selon le type
        if duplicate_type == 'RECOVERABLE_REFUS_CMA':
            reason_text = """Après vérification, nous constatons que votre précédent dossier avait été refusé par la CMA. Cela peut arriver en cas de documents incomplets ou non conformes.

Bonne nouvelle : votre dossier est déjà enregistré auprès de la CMA, vous pouvez vous réinscrire sans frais supplémentaires !"""
        elif duplicate_type == 'RECOVERABLE_PAID':
            reason_text = """Après vérification, nous constatons que votre précédent dossier est en cours de traitement auprès de la CMA.

Bonne nouvelle : votre dossier est déjà enregistré, nous pouvons reprendre votre inscription sans frais supplémentaires !"""
        else:
            # RECOVERABLE_NOT_PAID
            reason_text = """Après vérification, nous constatons que votre précédente inscription n'avait pas été finalisée.

Bonne nouvelle : nous pouvons reprendre votre dossier existant et poursuivre votre inscription !"""

        response_text = f"""Bonjour,

Je vous remercie pour votre message.

{reason_text}

Pour continuer, merci de nous renvoyer vos documents à jour :

• Pièce d'identité (carte d'identité ou passeport)
• Permis de conduire (recto + verso)
• Justificatif de domicile de moins de 6 mois

Vous pouvez nous les envoyer en réponse à cet email.

Si vous avez des questions sur la démarche, n'hésitez pas à me contacter.

Bien cordialement,

L'équipe Cab Formations"""

        logger.info(f"✅ Réponse DOUBLON RÉCUPÉRABLE générée ({len(response_text)} caractères)")

        return {
            'response_text': response_text,
            'is_duplicate_recoverable_response': True,
            'duplicate_type': duplicate_type,
            'duplicate_deals_count': len(duplicate_deals),
            'already_paid_to_cma': already_paid_to_cma,
            'crm_updates': {},  # Pas de mise à jour CRM pour les doublons récupérables
            'detected_scenarios': ['DUPLICATE_RECOVERABLE']
        }

    def _generate_clarification_response(
        self,
        ticket_id: str,
        triage_result: Dict
    ) -> Dict:
        """
        Génère une réponse pour demander des clarifications quand le candidat
        n'est pas trouvé dans le CRM.

        Reconnaît l'intention du candidat avant de demander les informations.
        """
        logger.info("📝 Génération de la réponse de CLARIFICATION...")

        email_searched = triage_result.get('email_searched', 'non identifié')
        alternative_email = triage_result.get('alternative_email_used')
        primary_intent = triage_result.get('primary_intent', '')

        # Adapter l'intro selon l'intention détectée
        intent_acknowledgment = ""
        if primary_intent == 'STATUT_DOSSIER':
            intent_acknowledgment = "Concernant votre demande sur l'avancement de votre dossier : "
        elif primary_intent in ('DEMANDE_DATES_FUTURES', 'DEMANDE_DATE_EXAMEN'):
            intent_acknowledgment = "Concernant votre demande sur les dates d'examen : "
        elif primary_intent == 'REPORT_DATE':
            intent_acknowledgment = "Concernant votre demande de changement de date : "
        elif primary_intent == 'DEMANDE_IDENTIFIANTS':
            intent_acknowledgment = "Concernant votre demande d'identifiants : "
        elif primary_intent == 'DEMANDE_CONVOCATION':
            intent_acknowledgment = "Concernant votre demande de convocation : "
        elif primary_intent == 'CONFIRMATION_SESSION':
            intent_acknowledgment = "Concernant votre choix de session de formation : "
        elif primary_intent == 'RESULTAT_EXAMEN':
            intent_acknowledgment = "Concernant votre demande de résultat d'examen : "
        elif primary_intent:
            intent_acknowledgment = "Concernant votre demande : "

        # Générer la réponse
        response_text = f"""Bonjour,

Je vous remercie pour votre message.

{intent_acknowledgment}Nous avons du mal à retrouver votre dossier via l'adresse mail **{email_searched}**.

Afin de pouvoir accéder à votre dossier et vous apporter une réponse précise, pourriez-vous nous communiquer les informations suivantes :

- **Votre nom et prénom** (tels qu'indiqués lors de l'inscription)
- **L'adresse email utilisée lors de votre inscription** (si différente de celle-ci)
- **Votre numéro de téléphone**

Dès réception de ces informations, nous reviendrons vers vous rapidement.

Bien cordialement,

L'équipe CAB Formations"""

        logger.info(f"✅ Réponse CLARIFICATION générée ({len(response_text)} caractères), intent={primary_intent}")

        return {
            'response_text': response_text,
            'is_clarification_response': True,
            'email_searched': email_searched,
            'alternative_email_tried': alternative_email,
            'intent_acknowledged': primary_intent,
            'crm_updates': {},  # Pas de mise à jour CRM - candidat non trouvé
            'detected_scenarios': ['CANDIDATE_NOT_FOUND']
        }

    def _run_response_generation(
        self,
        ticket_id: str,
        triage_result: Dict,
        analysis_result: Dict
    ) -> Dict:
        """
        Run AGENT RÉDACTEUR - Generate response using State Engine.

        Uses deterministic state detection + templates + validation.

        Returns response_result dict.
        """
        # Get ticket info
        ticket = self.desk_client.get_ticket(ticket_id)
        ticket_subject = ticket.get('subject', '')

        # Extract customer message and our previous response
        from src.utils.text_utils import get_clean_thread_content

        customer_message = ""
        previous_response = ""
        for thread in analysis_result.get('threads', []):
            if thread.get('direction') == 'in' and not customer_message:
                customer_message = get_clean_thread_content(thread)
            elif thread.get('direction') == 'out' and not previous_response:
                previous_response = get_clean_thread_content(thread)
            # Stop once we have both
            if customer_message and previous_response:
                break

        # State Engine - Deterministic response generation
        logger.info("  🎯 Mode: STATE ENGINE (deterministic)")
        return self._run_state_driven_response(
            ticket_id=ticket_id,
            triage_result=triage_result,
            analysis_result=analysis_result,
            customer_message=customer_message,
            previous_response=previous_response,
            ticket_subject=ticket_subject
        )

    def _run_state_driven_response(
        self,
        ticket_id: str,
        triage_result: Dict,
        analysis_result: Dict,
        customer_message: str,
        previous_response: str,
        ticket_subject: str
    ) -> Dict:
        """
        Run State-Driven response generation (deterministic).

        Uses:
        1. StateDetector → Detect candidate state from context
        2. TemplateEngine → Generate response from templates
        3. ResponseValidator → Validate response (forbidden terms, etc.)
        4. CRMUpdater → Determine CRM updates (pattern matching)

        Args:
            ticket_id: Ticket ID
            triage_result: Result from triage step (contains detected_intent)
            analysis_result: Result from analysis step (contains all data)
            customer_message: Candidate's message content
            previous_response: Our previous message to the candidate
            ticket_subject: Ticket subject

        Returns:
            response_result dict compatible with current workflow
        """
        logger.info("  🎯 STATE ENGINE: Détection de l'état...")

        # ================================================================
        # STEP 1: Detect State
        # ================================================================
        deal_data = analysis_result.get('deal_data', {})
        examt3p_data = analysis_result.get('examt3p_data', {})
        threads_data = analysis_result.get('threads', [])
        enriched_lookups = analysis_result.get('enriched_lookups', {})

        # Build linking_result from analysis data
        linking_result = {
            'deal_id': analysis_result.get('deal_id'),
            'deal': deal_data,
            'selected_deal': deal_data,
            'has_duplicate_uber_offer': analysis_result.get('has_duplicate_uber_offer', False),
            'needs_clarification': analysis_result.get('needs_clarification', False),
        }

        # MULTI-ÉTATS: Utiliser detect_all_states pour collecter tous les états
        # Récupérer les données de cohérence formation/examen pour FM-1
        training_exam_consistency_data = analysis_result.get('training_exam_consistency_result', {})
        session_data = analysis_result.get('session_data', {})

        detected_states = self.state_detector.detect_all_states(
            deal_data=deal_data,
            examt3p_data=examt3p_data,
            triage_result=triage_result,
            linking_result=linking_result,
            threads_data=threads_data,
            session_data=session_data,
            training_exam_consistency_data=training_exam_consistency_data,
            enriched_lookups=enriched_lookups
        )

        # Pour rétrocompatibilité, on utilise primary_state comme référence principale
        detected_state = detected_states.primary_state

        state_id = detected_state.id
        state_name = detected_state.name
        priority = detected_state.priority

        logger.info(f"  ✅ État primaire: {state_id} - {state_name} (priorité {priority})")

        # Log multi-états détaillés
        if detected_states.blocking_state:
            logger.info(f"  🚫 État BLOCKING: {detected_states.blocking_state.name}")
        if detected_states.warning_states:
            warning_names = [s.name for s in detected_states.warning_states]
            logger.info(f"  ⚠️ États WARNING: {warning_names}")
        if detected_states.info_states:
            info_names = [s.name for s in detected_states.info_states]
            logger.info(f"  ℹ️ États INFO: {info_names}")

        # Log intentions (multi-intentions)
        primary_intent = triage_result.get('primary_intent') or triage_result.get('detected_intent')
        secondary_intents = triage_result.get('secondary_intents', [])
        if primary_intent:
            logger.info(f"  🎯 Intention principale: {primary_intent}")
        if secondary_intents:
            logger.info(f"  🎯 Intentions secondaires: {secondary_intents}")

        # Log context for debugging
        ctx = detected_state.context_data
        logger.debug(f"     Evalbox: {ctx.get('evalbox')}")
        logger.debug(f"     Uber case: {ctx.get('uber_case')}")
        logger.debug(f"     Date case: {ctx.get('date_case')}")

        # ================================================================
        # SUPPRESSION WARNING DÉJÀ COMMUNIQUÉ
        # Si un état WARNING (ex: Uber CAS D) a déjà été envoyé dans un thread
        # précédent, le downgrader pour ne pas répéter le même message.
        # ================================================================
        suppressed_warnings = []
        if detected_states.warning_states:
            from src.utils.text_utils import get_clean_thread_content as _get_clean_content
            threads_data_for_check = analysis_result.get('threads', [])
            # Markers pour chaque WARNING qui peut être supprimé si déjà communiqué
            warning_suppression_markers = {
                'UBER_ACCOUNT_NOT_VERIFIED': ['compte uber driver', 'uber chauffeur actif'],
                'UBER_DOCS_MISSING': ['documents requis par uber', 'pièces demandées par uber'],
                'UBER_TEST_MISSING': ['test de sélection', 'test en ligne uber'],
                'UBER_NOT_ELIGIBLE': ['non éligible', 'pas éligible'],
            }
            # Collecter le contenu des réponses sortantes précédentes
            outgoing_content_lower = ""
            for t in threads_data_for_check:
                if t.get('direction') == 'out' and t.get('status') != 'DRAFT':
                    outgoing_content_lower += " " + _get_clean_content(t).lower()

            if outgoing_content_lower:
                for ws in list(detected_states.warning_states):
                    markers = warning_suppression_markers.get(ws.name, [])
                    if markers and any(m in outgoing_content_lower for m in markers):
                        suppressed_warnings.append(ws.name)
                        detected_states.warning_states.remove(ws)
                if suppressed_warnings:
                    logger.info(f"  🔇 WARNING supprimés (déjà communiqués): {suppressed_warnings}")

        # ================================================================
        # CAS D UBER: Détection d'email alternatif fourni par le candidat
        # Si le CAS D a déjà été communiqué ET le candidat fournit un autre email
        # → on l'acknowledge au lieu de répéter "contactez Uber"
        # ================================================================
        uber_cas_d_email_received = False
        uber_alternative_email = ''
        if 'UBER_ACCOUNT_NOT_VERIFIED' in suppressed_warnings:
            import re
            candidate_msg = (customer_message or '').strip()
            if candidate_msg:
                # Extraire les emails du message candidat
                emails_found = re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', candidate_msg)
                # Filtrer les emails CAB/system
                candidate_emails = [
                    e for e in emails_found
                    if 'cabformation' not in e.lower()
                    and 'cab-formation' not in e.lower()
                    and 'zoho' not in e.lower()
                ]
                if candidate_emails:
                    uber_cas_d_email_received = True
                    uber_alternative_email = candidate_emails[0]
                    logger.info(f"  📧 CAS D: Email alternatif Uber détecté dans message candidat: {uber_alternative_email}")
                    # Mise à jour de l'email du contact CRM avec l'email Uber fourni
                    _contact_data = analysis_result.get('contact_data', {})
                    _contact_id = _contact_data.get('contact_id')
                    _old_email = _contact_data.get('Email') or _contact_data.get('email', '')
                    if _contact_id and uber_alternative_email.lower() != _old_email.lower():
                        try:
                            self.crm_client.update_contact(_contact_id, {'Email': uber_alternative_email})
                            logger.info(f"  ✅ Email contact CRM mis à jour: {_old_email} → {uber_alternative_email}")
                        except Exception as e:
                            logger.warning(f"  ⚠️ Erreur mise à jour email contact CRM: {e}")

                    # Note interne pour l'équipe : vérification manuelle requise
                    try:
                        internal_note = (
                            f"📧 EMAIL ALTERNATIF UBER — Le candidat a fourni une adresse email différente "
                            f"pour son compte Uber Driver : {uber_alternative_email}\n"
                            f"Email du contact CRM mis à jour ({_old_email} → {uber_alternative_email}).\n"
                            f"Action requise : Vérifier avec Uber si cette adresse est liée à un compte Driver actif "
                            f"et mettre à jour le champ Compte_Uber si confirmé."
                        )
                        self.desk_client.add_ticket_comment(ticket_id, internal_note, is_public=False)
                        logger.info("  📝 Note interne ajoutée (email alternatif Uber)")
                    except Exception as e:
                        logger.warning(f"  ⚠️ Erreur ajout note interne CAS D email: {e}")

        # ================================================================
        # STEP 2: Generate Response from Template
        # ================================================================
        logger.info("  📝 STATE ENGINE: Génération de la réponse...")

        # Enrich context_data with additional analysis data
        # (TemplateEngine uses state.context_data for placeholders)
        date_examen_vtc_result = analysis_result.get('date_examen_vtc_result', {})
        session_data = analysis_result.get('session_data', {})
        uber_result = analysis_result.get('uber_eligibility_result', {})

        # Récupérer contact_data et date_examen_vtc_value depuis analysis_result
        contact_data = analysis_result.get('contact_data', {})
        date_examen_vtc_value = analysis_result.get('date_examen_vtc_value')

        # ================================================================
        # CAS 8: Si deadline passée → utiliser la NOUVELLE date d'examen
        # Mettre à jour enriched_lookups AVANT la génération du template
        # ================================================================
        if date_examen_vtc_result.get('deadline_passed_reschedule') and date_examen_vtc_result.get('new_exam_date'):
            new_exam_date = date_examen_vtc_result['new_exam_date']
            logger.info(f"  📅 CAS 8: Mise à jour enriched_lookups avec nouvelle date: {new_exam_date}")
            enriched_lookups['date_examen'] = new_exam_date
            # Mettre à jour aussi date_examen_vtc_value pour cohérence
            date_examen_vtc_value = new_exam_date

        # ================================================================
        # CONFIRMATION_DATE_EXAMEN: Si candidat a confirmé une nouvelle date
        # Mettre à jour enriched_lookups AVANT la génération du template
        # ================================================================
        if analysis_result.get('confirmed_exam_date_valid') and analysis_result.get('confirmed_new_exam_date'):
            confirmed_date = analysis_result['confirmed_new_exam_date']
            logger.info(f"  📅 CONFIRMATION_DATE_EXAMEN: Mise à jour enriched_lookups avec date confirmée: {confirmed_date}")
            enriched_lookups['date_examen'] = confirmed_date
            # Mettre à jour aussi date_examen_vtc_value pour cohérence
            date_examen_vtc_value = confirmed_date

        # DEBUG: Vérifier session_data avant l'injection dans le contexte
        logger.info(f"  🔍 DEBUG session_data: has_date_range={session_data.get('has_date_range_request')}, match_type={session_data.get('match_type')}, closest_before={session_data.get('closest_before') is not None}")

        # ================================================================
        # Extraire sessions_proposees depuis proposed_options si non déjà défini
        # ================================================================
        # proposed_options est une structure imbriquée retournée par analyze_session_situation()
        # sessions_proposees doit être une liste plate pour le template
        if not session_data.get('sessions_proposees') and session_data.get('proposed_options'):
            sessions_flat = []
            for option in session_data.get('proposed_options', []):
                sessions_list = option.get('sessions', [])
                sessions_flat.extend(sessions_list)
            session_data['sessions_proposees'] = sessions_flat
            if sessions_flat:
                logger.info(f"  📚 Sessions extraites de proposed_options: {len(sessions_flat)} session(s)")

        # Resultat CRM — classification lifecycle (recalcul pour ce scope)
        resultat_raw = deal_data.get('Resultat', '') if deal_data else ''
        resultat_info = self._classify_resultat(resultat_raw)

        detected_state.context_data.update({
            # Données brutes
            'deal_data': deal_data,
            'contact_data': contact_data,  # Données du contact (First_Name, Last_Name)
            'examt3p_data': examt3p_data,
            'credentials_invalid': examt3p_data.get('credentials_login_failed', False),  # Mot de passe changé par candidat
            'date_examen_vtc_data': date_examen_vtc_result,
            'date_examen_vtc_value': date_examen_vtc_value,  # Date réelle extraite du lookup
            'session_data': session_data,
            'uber_eligibility_data': uber_result,
            'training_exam_consistency_data': analysis_result.get('training_exam_consistency_result', {}),
            'ticket_subject': ticket_subject,
            'customer_message': customer_message,
            'threads': analysis_result.get('threads', []),

            # Données extraites pour les placeholders (niveau racine)
            # Filtrer next_dates: exclure la date actuelle
            # DEMANDE_ANNULATION: proposer plus de dates pour alternative au candidat
            'next_dates': self._filter_next_dates(
                date_examen_vtc_result.get('next_dates', []),
                date_examen_vtc_result.get('date_examen_info', {}).get('Date_Examen', '') if date_examen_vtc_result.get('date_examen_info') else '',
                limit=5 if triage_result.get('detected_intent') == 'DEMANDE_ANNULATION' else 1
            ),
            'date_case': date_examen_vtc_result.get('case'),
            'date_cloture': date_examen_vtc_result.get('date_cloture'),
            'can_choose_other_department': date_examen_vtc_result.get('can_choose_other_department', False),
            'alternative_department_dates': date_examen_vtc_result.get('alternative_department_dates', []),
            'cross_department_data': date_examen_vtc_result.get('cross_department_data', {}),
            'has_earlier_options': date_examen_vtc_result.get('has_earlier_options', False),
            'no_earlier_dates_available': date_examen_vtc_result.get('no_earlier_dates_available', False),
            'suppress_next_dates': date_examen_vtc_result.get('suppress_next_dates', False),
            'deadline_passed_reschedule': date_examen_vtc_result.get('deadline_passed_reschedule', False),
            'new_exam_date': date_examen_vtc_result.get('new_exam_date'),
            'new_exam_date_cloture': date_examen_vtc_result.get('new_exam_date_cloture'),
            'original_exam_date': date_examen_vtc_result.get('original_exam_date'),
            'original_date_cloture': date_examen_vtc_result.get('original_date_cloture'),

            # Examen passé (CAS 7: date passée + dossier validé)
            'examen_passe': date_examen_vtc_result.get('case') == 7 or resultat_info['category'] in ('post_exam', 'closed'),
            'examen_pas_encore_passe': (date_examen_vtc_result.get('case') not in [2, 7] if date_examen_vtc_result.get('case') else True) and resultat_info['category'] == 'pre_exam',

            # Resultat CRM — flags individuels pour templates
            'resultat_raw': resultat_raw,
            'resultat_category': resultat_info['category'],
            'dossier_termine': resultat_info['dossier_termine'],
            'resultat_admis': resultat_info['flag'] == 'resultat_admis',
            'resultat_non_admis': resultat_info['flag'] == 'resultat_non_admis',
            'resultat_non_admissible': resultat_info['flag'] == 'resultat_non_admissible',
            'resultat_admissible': resultat_info['flag'] == 'resultat_admissible',
            'resultat_absent': resultat_info['flag'] == 'resultat_absent',
            'resultat_convoc_pas_recu': resultat_info['flag'] == 'resultat_convoc_pas_recu',
            'resultat_plus_interesse': resultat_info['flag'] == 'resultat_plus_interesse',

            # Détection demande attestation réussite/admissibilité (mobilité taxi → CMA de dépôt)
            # Seulement si ADMIS/ADMISSIBLE ET message contient "attestation" SANS mots-clés France Travail
            'demande_attestation_resultat': self._detect_attestation_resultat(
                resultat_info, customer_message
            ),

            # Force majeure (examen manqué)
            'force_majeure_possible': date_examen_vtc_result.get('force_majeure_possible', True),  # Default True pour backward compat
            'days_since_exam': date_examen_vtc_result.get('days_since_exam'),

            # Auto-assignation date/session (CAS 1 avec date vide)
            'auto_assigned': date_examen_vtc_result.get('auto_assigned', False),
            'auto_assigned_exam_date': date_examen_vtc_result.get('auto_assigned_exam_date'),
            'auto_assigned_session': date_examen_vtc_result.get('auto_assigned_session'),

            # Auto-report (CAS 2: date passée + non validé → nouvelle date sélectionnée)
            'auto_report': date_examen_vtc_result.get('auto_report', False),
            'auto_report_date': date_examen_vtc_result.get('auto_report_date'),

            # Données de recherche par mois/lieu (REPORT_DATE intelligent)
            'no_date_for_requested_month': date_examen_vtc_result.get('no_date_for_requested_month', False),
            'requested_month_name': date_examen_vtc_result.get('requested_month_name', ''),
            'requested_location': date_examen_vtc_result.get('requested_location', ''),
            'same_month_other_depts': date_examen_vtc_result.get('same_month_other_depts', []),
            'same_dept_other_months': date_examen_vtc_result.get('same_dept_other_months', []),

            # Cross-département par mois (mode clarification/discordance)
            'month_cross_department': date_examen_vtc_result.get('month_cross_department', {}),
            'has_month_in_other_depts': date_examen_vtc_result.get('has_month_in_other_depts', False),
            'mentioned_month': date_examen_vtc_result.get('mentioned_month'),

            # Session
            'proposed_sessions': session_data.get('proposed_options', []),
            'session_preference': session_data.get('session_preference'),

            # Matching par dates spécifiques (DEMANDE_CHANGEMENT_SESSION avec dates)
            'has_date_range_request': session_data.get('has_date_range_request', False),
            'requested_dates_raw': session_data.get('requested_dates_raw', ''),
            'session_match_type': session_data.get('match_type', ''),
            'sessions_proposees': session_data.get('sessions_proposees', []),
            'has_sessions_proposees': len(session_data.get('sessions_proposees', [])) > 0,
            'closest_session_before': session_data.get('closest_before'),
            'closest_session_after': session_data.get('closest_after'),
            # Sessions par type (jour/soir) pour proposer les deux quand pas de préférence
            'closest_session_before_jour': session_data.get('closest_before_jour'),
            'closest_session_before_soir': session_data.get('closest_before_soir'),
            'closest_session_after_jour': session_data.get('closest_after_jour'),
            'closest_session_after_soir': session_data.get('closest_after_soir'),
            # Flags booléens pour conditions template (pybars3 ne supporte pas eq)
            'is_exact_match': session_data.get('match_type') == 'EXACT',
            'is_overlap_match': session_data.get('match_type') == 'OVERLAP',
            'is_no_match': session_data.get('match_type') in ('NO_MATCH', 'CLOSEST', 'CLOSEST_FALLBACK'),
            # Fallback quand type demandé indisponible (ex: pas de cours du jour)
            'no_sessions_of_requested_type': session_data.get('no_sessions_of_requested_type', False),
            'alternative_type_label': session_data.get('alternative_type_label', ''),

            # Vérification plainte session (erreur CAB)
            'is_complaint': session_data.get('is_complaint', False),
            'is_cab_error': session_data.get('is_cab_error', False),
            'complaint_error_type': session_data.get('complaint_error_type', ''),
            'complaint_verification': session_data.get('complaint_verification', ''),
            'corrected_session': session_data.get('corrected_session'),
            'complaint_alternatives': session_data.get('complaint_alternatives', []),
            'has_complaint_alternatives': len(session_data.get('complaint_alternatives', [])) > 0,
            'assigned_session_info': session_data.get('assigned_session_info', {}),
            'claimed_session_info': session_data.get('claimed_session_info', {}),
            # Toutes les sessions (jour + soir) quand le candidat a des contraintes sur les deux types
            'has_all_sessions': session_data.get('has_all_sessions', False),
            'all_sessions_jour': session_data.get('all_sessions_jour', []),
            'all_sessions_soir': session_data.get('all_sessions_soir', []),

            # Cascade d'alternatives (DEMANDE_CHANGEMENT_SESSION)
            'session_change_includes_next_date': session_data.get('_includes_next_date', False),
            'session_change_needs_cma': session_data.get('_needs_cma', False),

            # Blocage confirmation session (documents manquants ou credentials invalides)
            # NOTE: La clôture passée (CAS 8) n'est PAS un blocage - on redirige vers la nouvelle date
            'session_confirmation_blocked': session_data.get('session_confirmation_blocked', False),
            'session_blocking_reason': session_data.get('session_blocking_reason'),
            'session_blocked_documents_manquants': session_data.get('session_blocking_reason') == 'documents_manquants',
            'session_blocked_credentials_invalides': session_data.get('session_blocking_reason') == 'credentials_invalides',

            # Session confirmée par le candidat (CONFIRMATION_SESSION avec dates)
            'session_confirmed': analysis_result.get('session_confirmed', False),
            'matched_session_id': analysis_result.get('matched_session_id'),
            'matched_session_name': analysis_result.get('matched_session_name'),
            'matched_session_type': analysis_result.get('matched_session_type'),
            'matched_session_start': analysis_result.get('matched_session_start'),
            'matched_session_end': analysis_result.get('matched_session_end'),
            'session_already_started': analysis_result.get('session_already_started', False),

            # Erreur de saisie session corrigée automatiquement (erreur d'année)
            'session_assignment_error': analysis_result.get('session_assignment_error', False),
            'session_error_dates': analysis_result.get('session_error_data', {}).get('session_name', ''),
            'session_year_error_corrected': analysis_result.get('session_year_error_corrected', False),
            'session_year_error_corrected_name': analysis_result.get('session_year_error_corrected_name', ''),
            'session_year_error_corrected_start': analysis_result.get('session_year_error_corrected_start', ''),
            'session_year_error_corrected_end': analysis_result.get('session_year_error_corrected_end', ''),

            # Confirmation de date d'examen (CONFIRMATION_DATE_EXAMEN)
            'confirmed_exam_date_valid': analysis_result.get('confirmed_exam_date_valid', False),
            'confirmed_exam_date_unavailable': analysis_result.get('confirmed_exam_date_unavailable', False),
            'available_exam_dates_for_dept': analysis_result.get('available_exam_dates_for_dept', []),

            # Uber
            'is_uber_20_deal': uber_result.get('is_uber_20_deal', False),
            'uber_case': uber_result.get('case', ''),
            'uber_cas_d_email_received': uber_cas_d_email_received,
            'uber_alternative_email': uber_alternative_email,

            # Repositionnement implicite de date d'examen
            'implicit_date_repositioning': date_examen_vtc_result.get('implicit_date_repositioning', False),
            'engagement_level': date_examen_vtc_result.get('engagement_level', {}),
            'repositioning_month_name': date_examen_vtc_result.get('requested_month_name', ''),
            'repositioning_target_date': date_examen_vtc_result.get('repositioning_target_date_str', ''),

            # Dates deja communiquees (anti-repetition)
            'dates_already_communicated': analysis_result.get('dates_already_communicated', False),
            'dates_proposed_recently': analysis_result.get('dates_proposed_recently', False),
            'sessions_proposed_recently': analysis_result.get('sessions_proposed_recently', False),
            # Mode de communication du candidat
            'communication_mode': analysis_result.get('communication_mode', 'request'),
            'references_previous_communication': analysis_result.get('references_previous_communication', False),
            'mentions_discrepancy': analysis_result.get('mentions_discrepancy', False),
            'is_clarification_mode': analysis_result.get('is_clarification_mode', False),
            'is_verification_mode': analysis_result.get('is_verification_mode', False),
            'is_follow_up_mode': analysis_result.get('is_follow_up_mode', False),
            # Demande de complétion dossier précédente
            'previously_asked_to_complete': analysis_result.get('previously_asked_to_complete', False),

            # Pièces refusées (extraites de examt3p_data pour les templates Refus CMA)
            'pieces_refusees_details': examt3p_data.get('pieces_refusees_details', []),
            'has_pieces_refusees': len(examt3p_data.get('pieces_refusees_details', [])) > 0,
            'documents_refuses': examt3p_data.get('documents_refuses', []),
            'statut_documents': examt3p_data.get('statut_documents', ''),
            'action_candidat_requise': examt3p_data.get('action_candidat_requise', False),
            'faux_refus_cma': date_examen_vtc_result.get('faux_refus_cma', False),

            # Lookups CRM enrichis (v2.2) - données complètes depuis les modules Zoho
            # CRITIQUE: Contient session_date_debut, session_date_fin, session_type, etc.
            'enriched_lookups': analysis_result.get('enriched_lookups', {}),
        })

        # ================================================================
        # SUPPRESSION WARNING: Appliquer uber_case override APRÈS le context_data.update
        # (le update() ci-dessus réécrit uber_case depuis uber_result)
        # ================================================================
        if suppressed_warnings and any(w.startswith('UBER_') for w in suppressed_warnings):
            detected_state.context_data['uber_case'] = None
            logger.info("  🔇 uber_case forcé à None (WARNING Uber déjà communiqué)")

        # ================================================================
        # THREAD MEMORY: Injecter les flags dans le contexte du template
        # ================================================================
        thread_memory = analysis_result.get('thread_memory')
        if thread_memory and thread_memory.has_history:
            detected_state.context_data['thread_memory'] = {
                'has_history': thread_memory.has_history,
                'is_relance': thread_memory.is_relance,
                'days_since_last': thread_memory.days_since_last,
                'unanswered_count': thread_memory.unanswered_count,
                'evalbox_changed': thread_memory.evalbox_changed,
                'evalbox_previous': thread_memory.evalbox_previous,
                'evalbox_current': thread_memory.evalbox_current,
                'date_exam_changed': thread_memory.date_exam_changed,
                'date_exam_previous': thread_memory.date_exam_previous,
                'date_exam_current': thread_memory.date_exam_current,
                'suppress_identifiants': thread_memory.suppress_identifiants,
                'suppress_dates': thread_memory.suppress_dates,
                'suppress_sessions': thread_memory.suppress_sessions,
                'suppress_elearning': thread_memory.suppress_elearning,
                'suppress_statut': thread_memory.suppress_statut,
                'suppress_paiement': thread_memory.suppress_paiement,
                'human_intervention_detected': thread_memory.human_intervention_detected,
                'human_intervention_actor': thread_memory.human_intervention_actor,
            }

        # ================================================================
        # CONVERSATION INTELLIGENCE V3: Injecter dans le contexte du template
        # ================================================================
        conv_state = analysis_result.get('conversation_state')
        if conv_state and hasattr(conv_state, 'to_dict'):
            detected_state.context_data['conversation_state'] = conv_state.to_dict()

            # Flatten key V3 flags for direct template access
            if conv_state.target_date and any(
                d.type == 'date_choice' for d in conv_state.candidate_decisions
            ):
                # Format YYYY-MM-DD → DD/MM/YYYY for display
                raw_date = conv_state.target_date
                try:
                    parts = raw_date.split('-')
                    formatted_v3_date = f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else raw_date
                except Exception:
                    formatted_v3_date = raw_date
                detected_state.context_data['tm_candidate_confirmed_date'] = formatted_v3_date
            if conv_state.target_session:
                detected_state.context_data['tm_candidate_confirmed_session'] = conv_state.target_session
            if any(c.type == 'report_date' for c in conv_state.commitments):
                detected_state.context_data['tm_report_in_progress'] = True
                report_commit = next((c for c in conv_state.commitments if c.type == 'report_date'), None)
                if report_commit and report_commit.value:
                    detected_state.context_data['tm_report_target_date'] = report_commit.value
            if conv_state.human_is_handling:
                detected_state.context_data['v3_human_handling'] = True
                logger.info("  👤 V3: human_is_handling=True (advisory)")

        # RECALCULATE cloture_passed et can_modify_exam_date avec date_cloture enrichie
        # (le StateDetector n'a pas accès à date_cloture lors de la détection)
        date_cloture = date_examen_vtc_result.get('date_cloture')
        if date_cloture:
            from datetime import datetime
            try:
                if 'T' in str(date_cloture):
                    cloture_date = datetime.fromisoformat(str(date_cloture).replace('Z', '+00:00')).date()
                else:
                    cloture_date = datetime.strptime(str(date_cloture)[:10], '%Y-%m-%d').date()
                today = datetime.now().date()
                cloture_passed = cloture_date < today

                # Toujours mettre à jour cloture_passed (utilisé par d'autres logiques)
                detected_state.context_data['cloture_passed'] = cloture_passed

                # Recalculer can_modify_exam_date selon règle B1
                evalbox = detected_state.context_data.get('evalbox', '')
                blocking_statuses = {'VALIDE CMA', 'Convoc CMA reçue'}
                if evalbox in blocking_statuses and cloture_passed:
                    detected_state.context_data['can_modify_exam_date'] = False
                    logger.info(f"  ⚠️ can_modify_exam_date recalculé: False (clôture {date_cloture} passée)")
            except Exception as e:
                logger.warning(f"  ⚠️ Erreur parsing date_cloture: {e}")

        # LOAD next_dates si intention nécessite des dates alternatives mais dates vides
        # (CAS 7, 9 et autres cas ne chargent pas next_dates par défaut)
        detected_intent = detected_state.context_data.get('detected_intent', '')
        next_dates = detected_state.context_data.get('next_dates', [])
        needs_next_dates = detected_intent in NEEDS_NEXT_DATES_INTENTS
        if needs_next_dates and not next_dates:
            from src.utils.date_examen_vtc_helper import get_next_exam_dates
            departement = detected_state.context_data.get('departement')
            if departement and self.crm_client:
                logger.info(f"  📅 Chargement next_dates pour {detected_intent} (dept {departement})...")
                next_dates = get_next_exam_dates(self.crm_client, departement, limit=5)
                detected_state.context_data['next_dates'] = next_dates
                detected_state.context_data['has_next_dates'] = bool(next_dates)
                logger.info(f"  ✅ {len(next_dates)} date(s) chargées")

        # FILTRER next_dates selon le mois demandé par le candidat
        intent = IntentParser(triage_result)
        requested_month = intent.requested_month
        requested_location = intent.requested_location

        # Validation: requested_month doit être entre 1 et 12
        if requested_month and isinstance(requested_month, int) and 1 <= requested_month <= 12 and next_dates:
            from datetime import datetime
            filtered_dates = []
            has_date_in_exact_month = False
            for date_info in next_dates:
                date_str = date_info.get('Date_Examen') or date_info.get('date_examen')
                if date_str:
                    try:
                        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                        # Garder les dates du mois demandé ou après
                        if date_obj.month >= requested_month:
                            filtered_dates.append(date_info)
                            # Vérifier si on a une date exactement dans le mois demandé
                            if date_obj.month == requested_month:
                                has_date_in_exact_month = True
                    except ValueError:
                        filtered_dates.append(date_info)  # En cas d'erreur, garder la date

            month_names = ['', 'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
                           'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']

            if filtered_dates:
                logger.info(f"  📅 Filtrage par mois {requested_month}: {len(next_dates)} → {len(filtered_dates)} date(s)")
                detected_state.context_data['next_dates'] = filtered_dates

                # Si pas de date exactement dans le mois demandé, ajouter le message explicatif
                if not has_date_in_exact_month:
                    logger.info(f"  ℹ️ Pas de date exactement en {month_names[requested_month]} - dates ultérieures proposées")
                    detected_state.context_data['no_date_for_requested_month'] = True
                    detected_state.context_data['requested_month_name'] = month_names[requested_month] if 1 <= requested_month <= 12 else str(requested_month)

                    # CROSS-DEPARTMENT: Chercher des dates du mois demandé dans autres départements
                    self._search_month_in_other_departments(
                        detected_state, requested_month, month_names
                    )
            else:
                # Aucune date ne correspond - garder toutes les dates et ajouter message
                logger.warning(f"  ⚠️ Aucune date en mois {requested_month} ou après - on garde toutes les dates")
                detected_state.context_data['no_date_for_requested_month'] = True
                detected_state.context_data['requested_month_name'] = month_names[requested_month] if 1 <= requested_month <= 12 else str(requested_month)

                # CROSS-DEPARTMENT: Chercher des dates du mois demandé dans autres départements
                self._search_month_in_other_departments(
                    detected_state, requested_month, month_names
                )

        # Create AI generator for personalization sections
        # This uses Sonnet to generate contextual personalization based on threads/message
        def ai_personalization_generator(state, instructions="", max_length=150):
            return self._generate_personalization(
                state=state,
                customer_message=customer_message,
                threads=threads_data,
                instructions=instructions,
                max_length=max_length
            )

        # MULTI-ÉTATS: Generate response using generate_response_multi
        # Enrichir le primary_state avec le contexte combiné (y compris warnings)
        detected_states.primary_state = detected_state  # Avec le context_data enrichi

        # FILTRE FINAL: Exclure la date actuelle et limiter les dates alternatives
        # Utilise DateFilter centralisé
        current_exam_date = detected_state.context_data.get('date_examen_vtc_data', {}).get('date_examen_info', {})
        current_date_str = current_exam_date.get('Date_Examen', '')[:10] if current_exam_date and current_exam_date.get('Date_Examen') else ''

        raw_next_dates = detected_state.context_data.get('next_dates', [])
        if raw_next_dates and current_date_str:
            # DEMANDE_ANNULATION: proposer plus de dates pour que le candidat choisisse
            final_limit = 5 if detected_intent in ['DEMANDE_ANNULATION', 'REPORT_DATE'] else 1
            filtered_next_dates = apply_final_filter(raw_next_dates, current_date_str, limit=final_limit)
            detected_state.context_data['next_dates'] = filtered_next_dates
            logger.info(f"  📅 Filtre final next_dates: {len(raw_next_dates)} → {len(filtered_next_dates)} (exclu {current_date_str}, limit={final_limit})")

        # V3: Si le candidat a déjà confirmé une date, vider next_dates
        # pour ne PAS re-proposer de dates — le template V3 gère la confirmation
        v3_confirmed_date = detected_state.context_data.get('tm_candidate_confirmed_date', '')
        if v3_confirmed_date:
            # Convertir DD/MM/YYYY → YYYY-MM-DD pour comparaison
            v3_iso = v3_confirmed_date
            if '/' in v3_confirmed_date:
                parts = v3_confirmed_date.split('/')
                if len(parts) == 3:
                    v3_iso = f"{parts[2]}-{parts[1]}-{parts[0]}"

            current_next_dates = detected_state.context_data.get('next_dates', [])
            # Vider next_dates: le candidat a déjà choisi, pas besoin de proposer des alternatives
            detected_state.context_data['next_dates'] = []
            detected_state.context_data['has_next_dates'] = False
            logger.info(f"  🎯 V3: date confirmée {v3_confirmed_date} → next_dates vidé (pas de re-proposition)")

        # CROSS-DÉPARTEMENT: Si REPORT_DATE et aucune date alternative dans le département
        # → chercher TOUTES les dates dans d'autres départements (avant ET après)
        # SAUF si V3 a vidé next_dates (candidat a déjà confirmé une date)
        filtered_next_dates = detected_state.context_data.get('next_dates', [])
        if detected_intent == 'REPORT_DATE' and not filtered_next_dates and current_date_str and not v3_confirmed_date:
            departement = detected_state.context_data.get('departement')
            if departement and self.crm_client:
                logger.info(f"  🔄 REPORT_DATE: Aucune date alternative dans dept {departement} → recherche cross-département (toutes dates)...")
                from src.utils.date_examen_vtc_helper import get_next_exam_dates_any_department, DEPT_TO_REGION, REGION_TO_DEPTS
                compte_existe = detected_state.context_data.get('compte_examt3p', False)

                def _fmt_date(d):
                    """YYYY-MM-DD → DD/MM/YYYY"""
                    try:
                        parts = str(d)[:10].split('-')
                        return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else str(d)
                    except Exception:
                        return str(d)

                all_dates = get_next_exam_dates_any_department(self.crm_client, limit=30)
                # Exclure les dates du département actuel et la date actuelle
                other_dept_dates = [
                    d for d in all_dates
                    if str(d.get('Departement', '')) != str(departement)
                    and d.get('Date_Examen', '')[:10] != current_date_str
                ]

                # Séparer par région
                current_region = DEPT_TO_REGION.get(str(departement), 'Autre')
                same_region_depts = set(REGION_TO_DEPTS.get(current_region, []))
                same_region = []
                other_region = []
                today = datetime.now().date()

                for d in other_dept_dates:
                    dept = str(d.get('Departement', ''))
                    cloture_str = d.get('Date_Cloture_Inscription', '')
                    # Calculer jours jusqu'à clôture
                    days_until = 999
                    try:
                        if 'T' in str(cloture_str):
                            cloture_dt = datetime.fromisoformat(str(cloture_str).replace('Z', '+00:00')).date()
                        else:
                            cloture_dt = datetime.strptime(str(cloture_str)[:10], '%Y-%m-%d').date()
                        days_until = (cloture_dt - today).days
                    except Exception:
                        pass
                    if days_until < 7:
                        continue  # Pas assez de temps

                    # Formater pour le template
                    exam_date_str = d.get('Date_Examen', '')[:10]
                    enriched = {
                        **d,
                        'days_until_cloture': days_until,
                        'is_urgent': days_until < 14,
                        'region': DEPT_TO_REGION.get(dept, 'Autre'),
                        'date_examen_formatted': _fmt_date(exam_date_str),
                        'date_cloture_formatted': _fmt_date(str(cloture_str)[:10]),
                    }
                    if dept in same_region_depts:
                        same_region.append(enriched)
                    else:
                        other_region.append(enriched)

                same_region = same_region[:5]
                other_region = other_region[:5]
                all_options = same_region + other_region

                cross_dept_data = {
                    'same_region_options': same_region,
                    'other_region_options': other_region,
                    'has_same_region_options': bool(same_region),
                    'has_other_region_options': bool(other_region),
                    'requires_department_change_process': compte_existe,
                    'current_region': current_region,
                }

                if all_options:
                    detected_state.context_data['alternative_department_dates'] = all_options
                    detected_state.context_data['cross_department_data'] = cross_dept_data
                    detected_state.context_data['no_dates_in_own_dept'] = True
                    logger.info(f"  ✅ {len(all_options)} date(s) cross-département trouvée(s) (region: {len(same_region)}, autres: {len(other_region)})")
                else:
                    detected_state.context_data['no_dates_in_own_dept'] = True
                    logger.info(f"  ⚠️ Aucune date cross-département disponible non plus")

        template_result = self.template_engine.generate_response_multi(
            detected_states=detected_states,
            triage_result=triage_result,
            ai_generator=ai_personalization_generator
        )
        response_text = template_result.get('response_text', '')

        logger.info(f"  ✅ Réponse générée ({len(response_text)} caractères)")
        if template_result.get('template_used'):
            logger.info(f"     Template: {template_result['template_used']}")
        if template_result.get('states_used'):
            logger.info(f"     États utilisés: {template_result['states_used']}")
        if template_result.get('intents_handled'):
            logger.info(f"     Intentions traitées: {template_result['intents_handled']}")

        # ================================================================
        # STEP 3a: Humanize Response (Optional AI polish)
        # ================================================================
        # DEBUG: Afficher la réponse avant humanisation pour vérifier le contenu
        if 'Alternatives disponibles' in response_text or 'closest' in response_text.lower():
            logger.info(f"  📋 AVANT HUMANISATION - Alternatives détectées dans la réponse")
        else:
            logger.info(f"  ⚠️ AVANT HUMANISATION - Pas d'alternatives dans la réponse. First 500 chars: {response_text[:500]}")

        logger.info("  🤖 STATE ENGINE: Humanisation de la réponse...")

        # Get candidate name for personalization
        contact_data = analysis_result.get('contact_data', {})
        candidate_name = contact_data.get('First_Name', '')

        # Determine response_mode from V3 conversation state
        v3_response_mode = 'full'
        conv_state_for_humanizer = analysis_result.get('conversation_state')
        if conv_state_for_humanizer and hasattr(conv_state_for_humanizer, 'response_mode'):
            v3_response_mode = conv_state_for_humanizer.response_mode or 'full'

        # ENVOIE_IDENTIFIANTS / QUESTION_GENERALE: force full mode
        # Le candidat envoie ses accès ou pose une question → réponse complète, pas brief
        detected_intent_for_mode = triage_result.get('primary_intent') or triage_result.get('detected_intent', '')
        if detected_intent_for_mode in FULL_RECAP_INTENTS and v3_response_mode != 'full':
            logger.info(f"  🔓 {detected_intent_for_mode}: override response_mode {v3_response_mode} → full (point complet)")
            v3_response_mode = 'full'

        # Si CAS D email reçu, nettoyer previous_response pour éviter que
        # le humanizer ne copie les instructions "contacter Uber" de l'ancien message
        prev_resp_for_humanizer = previous_response
        if uber_cas_d_email_received and prev_resp_for_humanizer:
            import re as _re_humanizer
            # Retirer la section CAS D de l'ancien message (entre "Vérification" et la fin du bloc Uber)
            prev_resp_for_humanizer = _re_humanizer.sub(
                r'(?i)<b>V[ée]rification de votre compte Uber</b>.*?(?=<b>|$)',
                '',
                prev_resp_for_humanizer,
                flags=_re_humanizer.DOTALL
            )
            logger.info("  📧 CAS D email reçu: section Uber retirée du previous_response pour humanizer")

        humanize_result = humanize_response(
            template_response=response_text,
            candidate_message=customer_message,
            candidate_name=candidate_name,
            previous_response=prev_resp_for_humanizer,
            use_ai=True,  # Activer l'humanisation IA
            response_mode=v3_response_mode
        )

        if humanize_result.get('was_humanized'):
            response_text = humanize_result['humanized_response']
            logger.info(f"  ✅ Réponse humanisée ({len(response_text)} caractères)")
        else:
            if humanize_result.get('validation_failed'):
                logger.warning(f"  ⚠️ Humanisation annulée (validation échouée): {humanize_result.get('validation_issues')}")
            elif humanize_result.get('error'):
                logger.warning(f"  ⚠️ Humanisation échouée: {humanize_result.get('error')}")
            else:
                logger.info("  ℹ️ Humanisation désactivée")

        # Update template_result with humanized response
        template_result['response_text'] = response_text
        template_result['was_humanized'] = humanize_result.get('was_humanized', False)

        # ================================================================
        # STEP 3b: Validate Response
        # ================================================================
        logger.info("  🔍 STATE ENGINE: Validation de la réponse...")

        # Get proposed dates for validation
        proposed_dates = analysis_result.get('date_examen_vtc_result', {}).get('next_dates', [])

        # Montants autorisés selon l'intention
        allowed_amounts = None
        if detected_intent == 'DEMANDE_ANNULATION':
            allowed_amounts = [20]  # Template mentionne le prix de l'offre Uber 20€

        validation_result = self.response_validator.validate(
            response_text=response_text,
            state=detected_state,
            proposed_dates=proposed_dates,
            allowed_amounts=allowed_amounts,
            template_used=template_result.get('template_used')
        )

        if validation_result.valid:
            logger.info("  ✅ Validation OK")
        else:
            logger.warning(f"  ⚠️ Validation échouée: {len(validation_result.errors)} erreur(s)")
            for error in validation_result.errors:
                logger.warning(f"     - {error.message}")

            # Log warnings too
            for warning in validation_result.warnings:
                logger.info(f"     ⚡ {warning.message}")

        # ================================================================
        # STEP 4: Determine CRM Updates (Deterministic)
        # ================================================================
        logger.info("  📊 STATE ENGINE: Détermination des mises à jour CRM...")

        # Check for CRM updates defined in STATE:INTENTION matrix
        # These have priority over state-level crm_updates
        matrix_crm_updates = template_result.get('crm_updates_from_matrix')
        if matrix_crm_updates:
            # Matrix provides config in correct format: {'method': '...', 'fields': [...]}
            # or list format: [{'field': '...', 'value': '...'}]
            if isinstance(matrix_crm_updates, dict) and 'method' in matrix_crm_updates:
                # New format with method: {'method': 'extract_date_choice', 'fields': [...]}
                detected_state.crm_updates_config = matrix_crm_updates
                method = matrix_crm_updates.get('method', 'unknown')
                fields = [f.get('field') for f in matrix_crm_updates.get('fields', [])]
            else:
                # Legacy list format: [{'field': '...', 'value': '...'}]
                fields_list = matrix_crm_updates if isinstance(matrix_crm_updates, list) else [matrix_crm_updates]
                detected_state.crm_updates_config = {
                    'method': 'direct',
                    'fields': fields_list
                }
                method = 'direct'
                fields = [f.get('field') for f in fields_list if isinstance(f, dict)]

            logger.info(f"  📋 CRM updates depuis matrice STATE:INTENTION")
            logger.info(f"     Méthode: {method}")
            logger.info(f"     Champs: {fields}")

        # Get proposed sessions/dates for CRM updates
        proposed_sessions = []
        session_data = analysis_result.get('session_data', {})
        for option in session_data.get('proposed_options', []):
            for sess in option.get('sessions', []):
                proposed_sessions.append(sess)

        proposed_dates = analysis_result.get('date_examen_vtc_result', {}).get('next_dates', [])

        # Injecter proposed_sessions dans le contexte pour extraction LLM si nécessaire
        detected_state.context_data['proposed_sessions'] = proposed_sessions

        crm_update_result = self.state_crm_updater.determine_updates(
            state=detected_state,
            candidate_message=customer_message,
            proposed_sessions=proposed_sessions,
            proposed_dates=proposed_dates
        )

        crm_updates = crm_update_result.updates_applied

        if crm_updates:
            logger.info(f"  ✅ Mises à jour CRM déterminées: {list(crm_updates.keys())}")
        else:
            logger.info("  ✅ Aucune mise à jour CRM nécessaire")

        if crm_update_result.updates_blocked:
            for field, reason in crm_update_result.updates_blocked.items():
                logger.warning(f"  🔒 {field} bloqué: {reason}")

        # ================================================================
        # BUILD RESPONSE RESULT (compatible with current workflow)
        # ================================================================
        # Extract forbidden terms found from validation errors
        forbidden_terms_found = [
            e.message for e in validation_result.errors
            if e.error_type == 'forbidden_term'
        ]

        response_result = {
            'response_text': response_text,
            'detected_scenarios': [state_id],
            'crm_updates': crm_updates,
            'requires_crm_update': len(crm_updates) > 0,
            'should_stop_workflow': detected_state.response_config.get('stop_workflow', False),
            'validation': {
                state_id: {
                    'compliant': validation_result.valid,
                    'errors': [e.message for e in validation_result.errors],
                    'warnings': [w.message for w in validation_result.warnings],
                    'missing_blocks': [],
                    'forbidden_terms_found': forbidden_terms_found,
                }
            },
            # State Engine specific metadata
            'state_engine': {
                'state_id': state_id,
                'state_name': state_name,
                'priority': priority,
                'context': ctx,
                'crm_updates_blocked': crm_update_result.updates_blocked,
                'crm_updates_skipped': crm_update_result.updates_skipped,
            },
            # Multi-états / Multi-intentions metadata
            'states_used': template_result.get('states_used', []),
            'warning_states': template_result.get('warning_states', []),
            'info_states': template_result.get('info_states', []),
            'intents_handled': template_result.get('intents_handled', []),
            'is_blocking': template_result.get('is_blocking', False),
            'primary_intent': template_result.get('primary_intent'),
            'secondary_intents': template_result.get('secondary_intents', []),
            'was_humanized': template_result.get('was_humanized', False),
        }

        return response_result

    def _search_month_in_other_departments(
        self,
        detected_state,
        requested_month: int,
        month_names: list
    ) -> None:
        """
        Recherche des dates du mois demandé dans d'autres départements.

        Appelé quand le candidat demande un mois spécifique qui n'existe pas
        dans son département. Enrichit le context_data avec les alternatives.
        """
        from src.utils.cross_department_helper import get_dates_for_month_other_departments

        context = detected_state.context_data
        current_dept = context.get('departement', '')
        compte_existe = context.get('compte_existe', False)

        if not current_dept or not self.crm_client:
            return

        # month_names est une liste (index 1-12), pas un dict
        month_name = month_names[requested_month] if 1 <= requested_month <= len(month_names) - 1 else str(requested_month)
        logger.info(f"  🔍 Recherche de dates en {month_name} dans autres départements...")

        try:
            month_options = get_dates_for_month_other_departments(
                crm_client=self.crm_client,
                current_dept=current_dept,
                requested_month=requested_month,
                compte_existe=compte_existe,
                limit=5
            )

            # Ajouter au contexte
            context['month_cross_department'] = month_options
            context['has_month_in_other_depts'] = month_options.get('has_month_options', False)

            if month_options.get('has_same_region_options'):
                logger.info(f"  ✅ {len(month_options['same_region_options'])} date(s) trouvée(s) dans la même région")
            if month_options.get('has_other_region_options'):
                logger.info(f"  ✅ {len(month_options['other_region_options'])} date(s) trouvée(s) dans d'autres régions")
            if not month_options.get('has_month_options'):
                logger.info(f"  ℹ️ Aucune date en {month_name} disponible")

        except Exception as e:
            logger.warning(f"  ⚠️ Erreur recherche cross-département: {e}")

    # ================================================================
    # Attestation réussite/admissibilité — Détection mobilité taxi
    # ================================================================
    ATTESTATION_RESULTAT_KEYWORDS = [
        'attestation de réussite', 'attestation réussite',
        'attestation d\'admissibilité', 'attestation admissibilité',
        'attestation d admissibilité', 'attestation d admissibilite',
        'attestation de résultat', 'attestation résultat',
        'attestation examen',
    ]
    ATTESTATION_EXCLUDE_KEYWORDS = [
        'france travail', 'pôle emploi', 'pole emploi',
        'attestation de formation', 'certificat de formation',
        'justificatif de formation',
    ]

    def _detect_attestation_resultat(self, resultat_info: dict, customer_message: str) -> bool:
        """Détecte si un candidat ADMIS/ADMISSIBLE demande son attestation de résultat.

        Cas typique: mobilité professionnelle vers le taxi → rediriger vers CMA de dépôt.
        Exclut les demandes d'attestation France Travail / attestation de formation.
        """
        if resultat_info['flag'] not in ('resultat_admis', 'resultat_admissible'):
            return False

        msg = (customer_message or '').lower()

        # Exclure les demandes France Travail / attestation de formation
        if any(kw in msg for kw in self.ATTESTATION_EXCLUDE_KEYWORDS):
            return False

        # Chercher les mots-clés attestation spécifiques
        if any(kw in msg for kw in self.ATTESTATION_RESULTAT_KEYWORDS):
            logger.info(f"  📄 Demande attestation résultat détectée (Resultat={resultat_info['flag']}) → CMA de dépôt")
            return True

        return False

    # ================================================================
    # Resultat CRM — Classification lifecycle
    # ================================================================
    RESULTAT_COMPLETED = {
        'ADMIS', 'NON ADMIS', 'NON ADMISSIBLE',
        'ABSENT TH', 'ABSENT PR', 'Convoc pas recu',
        'NON ADMIS PLUS INTERRESSE', 'NON ADMISSIBLE PLUS INTERRESSE'
    }
    RESULTAT_MID = {'ADMISSIBLE'}

    def _apply_session_change_cascade(self, session_data, current_type, current_id, engagement):
        """Cascade d'alternatives pour DEMANDE_CHANGEMENT_SESSION.

        Niveaux:
        1. Même type, même date, session différente → proposer celle-là uniquement
        2. Combo: autre type même date (Alt A) + même type prochaine date (Alt B)
        3. Fallback: prochaine date (tout type disponible)
        0. Rien trouvé → garder les options originales
        """
        proposed = session_data.get('proposed_options', [])
        if not proposed:
            session_data['_cascade_level'] = 0
            return session_data

        current_option = proposed[0]  # Date actuelle (toujours en premier)
        next_option = proposed[1] if len(proposed) > 1 else None
        current_sessions = current_option.get('sessions', [])

        # Step 1: Autre session du MÊME type sur la MÊME date
        same_type_alts = [s for s in current_sessions
                          if s.get('session_type') == current_type
                          and str(s.get('id', '')) != str(current_id or '')]
        if same_type_alts:
            current_option['sessions'] = same_type_alts
            session_data['proposed_options'] = [current_option]
            session_data['_cascade_level'] = 1
            logger.info(f"  🔄 CASCADE 1: {len(same_type_alts)} alternative(s) même type trouvée(s)")
            return session_data

        # Step 2: Combo — autre type même date (Alt A) + même type prochaine date (Alt B)
        import copy
        other_type = [s for s in current_sessions if s.get('session_type') != current_type]
        next_same_type = []
        if next_option and engagement.get('can_reposition'):
            next_sessions = next_option.get('sessions', [])
            next_same_type = [s for s in next_sessions if s.get('session_type') == current_type]

        combo = []
        if other_type:
            alt_a = copy.deepcopy(current_option)
            alt_a['sessions'] = other_type
            combo.append(alt_a)
        if next_same_type:
            alt_b = copy.deepcopy(next_option)
            alt_b['sessions'] = next_same_type
            combo.append(alt_b)

        if combo:
            session_data['proposed_options'] = combo
            session_data['_cascade_level'] = 2
            session_data['_includes_next_date'] = bool(next_same_type)
            session_data['_needs_cma'] = engagement.get('needs_cma_message', False)
            logger.info(f"  🔄 CASCADE 2: combo {len(other_type)} autre type + {len(next_same_type)} même type prochaine date")
            return session_data

        # Step 3: Fallback — tout ce qui est dispo sur prochaine date
        if next_option and engagement.get('can_reposition'):
            session_data['proposed_options'] = [next_option]
            session_data['_cascade_level'] = 3
            session_data['_includes_next_date'] = True
            session_data['_needs_cma'] = engagement.get('needs_cma_message', False)
            logger.info(f"  🔄 CASCADE 3: fallback prochaine date")
            return session_data

        # Rien trouvé — garder les options originales
        session_data['_cascade_level'] = 0
        logger.info(f"  🔄 CASCADE 0: aucune alternative trouvée")
        return session_data

    def _classify_resultat(self, resultat_value: str) -> dict:
        """Classifie le Resultat CRM en catégorie de lifecycle.

        Returns dict with:
            - category: 'pre_exam' | 'mid_exam' | 'post_exam' | 'closed'
            - flag: string flag name (e.g. 'resultat_admis') or '' for pre_exam
            - dossier_termine: bool — True if exam cycle is over
        """
        val = (resultat_value or '').strip()

        if val == 'ADMIS':
            return {'category': 'post_exam', 'flag': 'resultat_admis', 'dossier_termine': True}
        elif val == 'NON ADMIS':
            return {'category': 'post_exam', 'flag': 'resultat_non_admis', 'dossier_termine': True}
        elif val == 'NON ADMISSIBLE':
            return {'category': 'post_exam', 'flag': 'resultat_non_admissible', 'dossier_termine': True}
        elif val == 'ADMISSIBLE':
            return {'category': 'mid_exam', 'flag': 'resultat_admissible', 'dossier_termine': False}
        elif val.startswith('ABSENT'):
            return {'category': 'post_exam', 'flag': 'resultat_absent', 'dossier_termine': True}
        elif val == 'Convoc pas recu':
            return {'category': 'post_exam', 'flag': 'resultat_convoc_pas_recu', 'dossier_termine': True}
        elif 'PLUS INTERRESSE' in val:
            return {'category': 'closed', 'flag': 'resultat_plus_interesse', 'dossier_termine': True}
        else:
            return {'category': 'pre_exam', 'flag': '', 'dossier_termine': False}

    def _filter_next_dates(
        self,
        next_dates: list,
        current_date,
        limit: int = 1
    ) -> list:
        """
        Filtre next_dates: exclut la date actuelle et limite le nombre de résultats.

        Args:
            next_dates: Liste des dates d'examen disponibles
            current_date: Date actuelle à exclure (string, dict avec 'name', ou None)
            limit: Nombre max de dates à retourner

        Returns:
            Liste filtrée des dates alternatives
        """
        if not next_dates:
            return []

        # Normaliser current_date (peut être string, dict avec lookup, ou None)
        if isinstance(current_date, dict):
            # Format lookup CRM: {'name': '34_2026-03-31', 'id': '...'}
            current_date_str = str(current_date.get('name', ''))
            # Extraire la date du format "dept_YYYY-MM-DD"
            if '_' in current_date_str:
                current_date_str = current_date_str.split('_')[-1][:10]
            else:
                current_date_str = current_date_str[:10]
        else:
            current_date_str = str(current_date)[:10] if current_date else ''

        # Exclure la date actuelle
        filtered = [
            d for d in next_dates
            if str(d.get('Date_Examen', ''))[:10] != current_date_str
        ]

        # Limiter le nombre de résultats
        return filtered[:limit] if limit else filtered

    def _generate_personalization(
        self,
        state,
        customer_message: str,
        threads: list,
        instructions: str = "",
        max_length: int = 150
    ) -> str:
        """
        Generate personalized introduction using Sonnet.

        This creates a contextual 1-3 sentence introduction that:
        - Acknowledges the candidate's specific concern/question
        - Takes into account the thread history
        - Sets up the factual content that follows in the template

        Args:
            state: DetectedState with context data
            customer_message: The candidate's last message
            threads: Thread history
            instructions: Additional instructions for personalization
            max_length: Max characters for personalization (soft limit)

        Returns:
            Personalized text (1-3 sentences)
        """
        # Format thread history for context
        thread_history = self._format_thread_history_for_personalization(threads)

        # Get state context
        state_name = state.name
        state_description = state.description if hasattr(state, 'description') else state_name

        # Build the system prompt
        system_prompt = """Tu es un assistant de CAB Formations, organisme de formation VTC.

Tu dois rédiger une COURTE introduction personnalisée (1 à 3 phrases maximum) pour une réponse email.

Cette introduction doit:
1. Reconnaître la demande ou préoccupation spécifique du candidat
2. Être empathique et professionnelle
3. Préparer le terrain pour les informations factuelles qui suivront

RÈGLES STRICTES:
- NE JAMAIS inventer de dates, numéros de dossier, identifiants ou informations factuelles
- NE JAMAIS mentionner de montants (prix, frais)
- NE JAMAIS promettre quoi que ce soit de spécifique
- Être concis: 1 à 3 phrases, pas plus
- Utiliser un ton professionnel mais chaleureux
- Ne pas répéter le sujet de l'email
- Ne pas commencer par "Je" ou "Nous"

TERMES INTERDITS (ne jamais utiliser):
- "Evalbox", "BFS", "deal", "CRM", "CAS", "workflow"
- Tout jargon technique interne

FORMAT DE SORTIE:
Écris UNIQUEMENT le texte de personnalisation, sans guillemets, sans préfixe, sans explication."""

        # Build user prompt
        user_prompt = f"""## CONTEXTE

**État détecté du dossier**: {state_description}

**Dernier message du candidat**:
{customer_message}

---

## HISTORIQUE DES ÉCHANGES:

{thread_history}

---

## INSTRUCTION SPÉCIFIQUE:
{instructions if instructions else "Rédige une introduction adaptée à la situation."}

---

Génère maintenant la personnalisation (1-3 phrases):"""

        try:
            response = self.anthropic_client.messages.create(
                model=self.personalization_model,
                max_tokens=200,
                temperature=0.3,  # Low temperature for consistency
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )

            personalization = response.content[0].text.strip()

            # Safety: truncate if too long
            if len(personalization) > max_length * 2:
                # Find last sentence end before limit
                truncated = personalization[:max_length * 2]
                last_period = max(truncated.rfind('.'), truncated.rfind('!'), truncated.rfind('?'))
                if last_period > 0:
                    personalization = truncated[:last_period + 1]

            logger.info(f"  ✅ Personnalisation générée ({len(personalization)} caractères)")
            return personalization

        except Exception as e:
            logger.error(f"  ❌ Erreur génération personnalisation: {e}")
            # Fallback to a generic but appropriate response
            return "Nous avons bien reçu votre message et nous vous remercions de votre patience."

    def _format_thread_history_for_personalization(self, threads: list) -> str:
        """Format thread history for personalization prompt."""
        if not threads:
            return "(Premier contact - aucun historique)"

        lines = []

        # Sort by date
        sorted_threads = sorted(
            threads,
            key=lambda t: t.get('createdTime', '') or t.get('created_time', '') or '',
            reverse=False
        )

        # Show last 5 exchanges max to avoid context overflow
        recent_threads = sorted_threads[-5:] if len(sorted_threads) > 5 else sorted_threads

        for i, thread in enumerate(recent_threads, 1):
            direction = thread.get('direction', 'unknown')
            created_time = thread.get('createdTime', '') or thread.get('created_time', '')

            # Format date
            date_str = ""
            if created_time:
                try:
                    from datetime import datetime
                    if 'T' in str(created_time):
                        dt = datetime.fromisoformat(str(created_time).replace('Z', '+00:00'))
                    else:
                        dt = datetime.strptime(str(created_time), "%Y-%m-%d %H:%M:%S")
                    date_str = dt.strftime("%d/%m/%Y")
                except Exception as e:
                    date_str = ""

            # Sender
            sender = "CANDIDAT" if direction == 'in' else "CAB Formations" if direction == 'out' else "?"

            # Content (truncated)
            content = thread.get('content', '') or thread.get('summary', '') or thread.get('plainText', '') or ''
            content = content.strip()
            if len(content) > 500:
                content = content[:500] + "..."

            lines.append(f"[{date_str}] {sender}:\n{content}\n")

        return "\n".join(lines) if lines else "(Aucun contenu)"

    def _create_crm_note(
        self,
        ticket_id: str,
        triage_result: Dict,
        analysis_result: Dict,
        response_result: Dict,
        crm_updates_applied: Dict = None
    ) -> str:
        """
        Crée une note CRM unique et consolidée avec toutes les infos du traitement.

        Format:
        0. [META] ligne structurée parseable par ThreadMemory
        1. Lien vers le ticket Desk
        2. Résumé de la réponse envoyée au candidat
        3. Mises à jour CRM effectuées
        4. Next steps (candidat + CAB)
        5. Alertes si nécessaire
        """
        if crm_updates_applied is None:
            crm_updates_applied = {}
        import anthropic

        lines = []

        # === LIGNE META (parseable par ThreadMemory) ===
        meta_line = self._build_meta_line(ticket_id, triage_result, analysis_result, response_result)
        lines.append(meta_line)
        lines.append("")

        # === EN-TÊTE avec lien ticket ===
        lines.append(f"Ticket #{ticket_id}")
        lines.append(f"https://desk.zoho.com/agent/cabformations/cab-formations/tickets/{ticket_id}")
        lines.append("")

        # === MISES À JOUR CRM ===
        updates = []

        # Sync ExamT3P
        sync_result = analysis_result.get('sync_result', {})
        if sync_result and sync_result.get('changes_made'):
            for change in sync_result['changes_made']:
                field = change['field']
                old_val = change.get('old_value', '') or '—'
                new_val = change.get('new_value', '')
                if 'MDP' in field:
                    new_val = '***'
                    old_val = '***' if old_val != '—' else '—'
                updates.append(f"• {field}: {old_val} → {new_val}")

        # Date sync
        date_sync = sync_result.get('date_sync', {}) if sync_result else {}
        if date_sync.get('date_changed'):
            old_date = date_sync.get('old_date') or '—'
            new_date = date_sync.get('new_date', '')
            updates.append(f"• Date_examen_VTC: {old_date} → {new_date}")

        # Mises à jour CRM appliquées (passées en paramètre après STEP 5)
        if crm_updates_applied:
            for field, value in crm_updates_applied.items():
                # Éviter les doublons
                if not any(field in u for u in updates):
                    updates.append(f"• {field}: → {value}")

        if updates:
            lines.append("Mises à jour CRM:")
            lines.extend(updates)
        else:
            lines.append("Mises à jour CRM: aucune")
        lines.append("")

        # === GÉNÉRER RÉSUMÉ + NEXT STEPS avec Claude ===
        note_content = self._generate_note_content_with_ai(analysis_result, response_result)
        if note_content:
            lines.append(note_content)
            lines.append("")

        # === ALERTES ===
        alerts = []

        # Blocages de sync
        if sync_result and sync_result.get('blocked_changes'):
            for blocked in sync_result['blocked_changes']:
                alerts.append(f"⚠️ {blocked['field']}: {blocked['reason']}")

        # Date sync bloquée
        if date_sync.get('blocked'):
            alerts.append(f"⚠️ Date_examen_VTC: {date_sync.get('blocked_reason', 'bloqué')}")

        # Incohérences détectées
        training_result = analysis_result.get('training_exam_consistency_result', {})
        if training_result and training_result.get('problem_detected'):
            alerts.append(f"⚠️ {training_result.get('problem_description', 'Cohérence formation/examen à vérifier')}")

        # Double compte ExamT3P
        examt3p_data = analysis_result.get('examt3p_data', {})
        if examt3p_data.get('duplicate_paid_accounts'):
            alerts.append("⚠️ DOUBLE COMPTE PAYÉ - vérifier paiement")

        if alerts:
            lines.append("Alertes:")
            lines.extend(alerts)
        else:
            lines.append("✓ Aucune alerte")

        return "\n".join(lines)

    def _build_meta_line(
        self,
        ticket_id: str,
        triage_result: Dict,
        analysis_result: Dict,
        response_result: Dict
    ) -> str:
        """
        Construit une ligne [META] structurée parseable par ThreadMemory.

        Format:
        [META] ticket=XXX | state=YYY | intent=ZZZ | evalbox=AAA | date_exam=DD/MM/YYYY | session=type DD/MM-DD/MM | sections=a,b,c

        Cette ligne est conçue pour être :
        - Parsée par regex (key=value séparés par ' | ')
        - Ignorée visuellement par les humains
        - Source de vérité pour savoir ce qui a été communiqué
        """
        from datetime import datetime

        parts = []

        # Ticket ID
        parts.append(f"ticket={ticket_id}")

        # Timestamp ISO
        parts.append(f"ts={datetime.now().strftime('%Y-%m-%dT%H:%M')}")

        # État détecté
        state_engine = response_result.get('state_engine', {})
        state_name = state_engine.get('state_name') or state_engine.get('state_id') or 'N/A'
        parts.append(f"state={state_name}")

        # Intention principale
        primary_intent = (
            response_result.get('primary_intent')
            or triage_result.get('primary_intent')
            or triage_result.get('detected_intent')
            or 'N/A'
        )
        parts.append(f"intent={primary_intent}")

        # Intentions secondaires
        secondary = response_result.get('secondary_intents') or triage_result.get('secondary_intents', [])
        if secondary:
            parts.append(f"intents_sec={','.join(secondary)}")

        # Evalbox
        deal_data = analysis_result.get('deal_data', {})
        evalbox = deal_data.get('Evalbox', 'N/A')
        parts.append(f"evalbox={evalbox}")

        # Date examen
        date_result = analysis_result.get('date_examen_vtc_result', {})
        date_info = date_result.get('date_examen_info', {}) if isinstance(date_result.get('date_examen_info'), dict) else {}
        date_exam = date_info.get('Date_Examen', 'N/A')
        parts.append(f"date_exam={date_exam}")

        # Cas date
        date_case = date_result.get('cas_detecte') or date_result.get('case')
        if date_case:
            parts.append(f"date_case={date_case}")

        # Session
        enriched = analysis_result.get('enriched_lookups', {})
        session_start = enriched.get('session_date_debut', '')
        session_end = enriched.get('session_date_fin', '')
        session_type = enriched.get('session_type', '')
        if session_start and session_end:
            parts.append(f"session={session_type} {session_start}/{session_end}")

        # Sections communiquées (déduites de la réponse)
        sections = self._detect_sections_communicated(response_result)
        if sections:
            parts.append(f"sections={','.join(sections)}")

        # V3 Conversation Intelligence fields
        conv_state = analysis_result.get('conversation_state')
        if conv_state and hasattr(conv_state, 'target_date') and conv_state.target_date:
            parts.append(f"target_date={conv_state.target_date}")
        if conv_state and hasattr(conv_state, 'proposed_dates') and conv_state.proposed_dates:
            parts.append(f"proposed_dates={','.join(conv_state.proposed_dates)}")
        if conv_state and hasattr(conv_state, 'response_mode') and conv_state.response_mode:
            parts.append(f"response_mode={conv_state.response_mode}")

        return f"[META] {' | '.join(parts)}"

    def _detect_sections_communicated(self, response_result: Dict) -> list:
        """
        Détecte quelles sections ont été incluses dans la réponse,
        en analysant le texte HTML généré.
        """
        response_text = response_result.get('response_text', '')
        if not response_text:
            return []

        sections = []
        text_lower = response_text.lower()

        # Identifiants ExamT3P
        if 'exament3p' in text_lower or 'identifiant' in text_lower:
            sections.append('identifiants')

        # Statut dossier
        if 'statut' in text_lower or 'validé' in text_lower or 'en cours' in text_lower:
            sections.append('statut')

        # Convocation
        if 'convocation' in text_lower:
            sections.append('convocation')

        # Dates d'examen
        if 'date d' in text_lower or "date de l'examen" in text_lower or 'prochaine' in text_lower:
            sections.append('dates')

        # Sessions de formation
        if 'session' in text_lower or 'cours du jour' in text_lower or 'cours du soir' in text_lower:
            sections.append('sessions')

        # E-learning
        if 'e-learning' in text_lower or 'espace e-learning' in text_lower:
            sections.append('elearning')

        # Paiement
        if 'paiement' in text_lower or '241' in response_text:
            sections.append('paiement')

        # Annulation/remboursement
        if 'annulation' in text_lower or 'remboursement' in text_lower or 'rétractation' in text_lower:
            sections.append('annulation')

        return sections

    def _generate_note_content_with_ai(
        self,
        analysis_result: Dict,
        response_result: Dict
    ) -> str:
        """
        Utilise Claude Sonnet pour générer:
        1. Résumé de ce qui a été répondu au candidat
        2. Next steps candidat et CAB
        """
        import anthropic

        # Récupérer la réponse envoyée
        response_text = response_result.get('response_text', '')

        # Préparer le contexte
        deal_data = analysis_result.get('deal_data', {})
        examt3p_data = analysis_result.get('examt3p_data', {})
        date_result = analysis_result.get('date_examen_vtc_result', {})
        uber_result = analysis_result.get('uber_eligibility_result', {})

        # État détecté
        detected_state = response_result.get('detected_state', {})
        state_name = detected_state.get('name', 'N/A') if isinstance(detected_state, dict) else str(detected_state)

        # Uber status
        is_uber = uber_result.get('is_uber_20_deal', False)
        uber_case = uber_result.get('case', '')

        prompt = f"""Tu es un assistant qui génère des notes CRM concises pour le suivi des candidats VTC.

CONTEXTE:
- État: {state_name}
- Evalbox: {deal_data.get('Evalbox', 'N/A')}
- Deal Uber 20€: {'Oui - ' + uber_case if is_uber else 'Non'}
- Date examen: {date_result.get('date_examen_info', {}).get('Date_Examen', 'N/A') if isinstance(date_result.get('date_examen_info'), dict) else 'N/A'}
- Session assignée: {'Oui' if deal_data.get('Session') else 'Non'}

RÉPONSE ENVOYÉE AU CANDIDAT:
{response_text[:1500]}

---

Génère une note CRM avec EXACTEMENT ce format:

Réponse envoyée:
• [point clé 1 de ce qui a été communiqué]
• [point clé 2]
• [point clé 3 si pertinent]

Next steps candidat:
• [action concrète 1]
• [action concrète 2 si nécessaire]

Next steps CAB:
• [action concrète 1]
• [action concrète 2 si nécessaire]

RÈGLES:
- Résumer ce qui a RÉELLEMENT été dit dans la réponse (pas d'invention)
- Next steps SPÉCIFIQUES au contexte actuel
- Si Uber ÉLIGIBLE et frais pris en charge: ne PAS dire au candidat de payer
- Maximum 3 points par section
- Phrases courtes (5-10 mots max)
- Pas de formules vides comme "suivre le dossier"

Réponds UNIQUEMENT avec le format demandé, rien d'autre."""

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model=MODEL_TRIAGE,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}]
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.warning(f"Erreur génération note IA: {e}")
            # Fallback basique
            return "Réponse envoyée:\n• Voir brouillon dans Zoho Desk\n\nNext steps candidat:\n• Consulter la réponse\n\nNext steps CAB:\n• Vérifier et envoyer"

    def _prepare_ticket_updates(self, response_result: Dict) -> Dict:
        """Prepare ticket field updates."""
        updates = {}

        # Note: Les tags Zoho Desk ne peuvent pas être mis à jour via l'API standard
        # (erreur "An extra parameter 'tags' is found")
        # Pour le moment, on ne met pas à jour les tags automatiquement

        return updates

    def _prepare_deal_updates(
        self,
        response_result: Dict,
        analysis_result: Dict
    ) -> Dict:
        """
        Prepare CRM deal field updates.

        Uses pattern-matched updates from State Engine (crm_updates)
        which analyzes the conversation context to determine what needs updating.

        IMPORTANT: Utilise les fonctions existantes de examt3p_crm_sync.py pour
        convertir les valeurs string en IDs CRM (lookup fields).
        """
        from src.utils.examt3p_crm_sync import find_exam_session_by_date_and_dept
        import re

        # Get AI-extracted updates (primary source)
        ai_updates = response_result.get('crm_updates', {})

        if not ai_updates:
            logger.info(f"  📊 No CRM updates extracted by AI")
            return {}

        logger.info(f"  📊 AI extracted CRM updates (raw): {ai_updates}")

        crm_updates = {}
        deal_data = analysis_result.get('deal_data', {})
        session_data = analysis_result.get('session_data', {})

        # ================================================================
        # 1. Date_examen_VTC (string → session ID via existing function)
        # ================================================================
        if 'Date_examen_VTC' in ai_updates:
            date_str = ai_updates['Date_examen_VTC']
            # Récupérer le département depuis le deal
            departement = deal_data.get('CMA_de_depot', '')
            if departement:
                match = re.search(r'\b(\d{2,3})\b', str(departement))
                if match:
                    departement = match.group(1)

            if departement:
                # Utiliser la fonction existante de examt3p_crm_sync.py
                session = find_exam_session_by_date_and_dept(
                    self.crm_client, date_str, departement
                )
                if session and session.get('id'):
                    crm_updates['Date_examen_VTC'] = session['id']
                    logger.info(f"  📊 Date_examen_VTC: {date_str} → ID {session['id']}")
                else:
                    logger.warning(f"  ⚠️ Session examen non trouvée: {date_str} / dept {departement}")
            else:
                logger.warning(f"  ⚠️ Département non trouvé, impossible de mapper Date_examen_VTC")

        # ================================================================
        # 2. Session_choisie (session name → session ID from proposed options)
        # ================================================================
        if 'Session_choisie' in ai_updates:
            session_name = ai_updates['Session_choisie']
            # Chercher dans les sessions proposées par l'analyse
            proposed_options = session_data.get('proposed_options', [])

            # Extraire toutes les sessions (proposed_options imbriqué OU sessions_proposees flat)
            all_sessions_flat = []
            if proposed_options:
                for option in proposed_options:
                    for sess in option.get('sessions', []):
                        all_sessions_flat.append(sess)
            elif session_data.get('sessions_proposees'):
                all_sessions_flat = list(session_data['sessions_proposees'])

            session_found = False
            for sess in all_sessions_flat:
                sess_id = sess.get('id')
                sess_debut = sess.get('Date_d_but', '') or sess.get('date_debut', '')
                sess_fin = sess.get('Date_fin', '') or sess.get('date_fin', '')
                sess_type = sess.get('session_type_label', '') or sess.get('session_type', '')

                # Matching: soit par dates, soit par type (jour/soir)
                if sess_id:
                    match_date = (sess_debut and sess_debut in session_name) or \
                                (sess_fin and sess_fin in session_name)
                    match_type = ('soir' in session_name.lower() and 'soir' in str(sess_type).lower()) or \
                                ('jour' in session_name.lower() and 'jour' in str(sess_type).lower())

                    if match_date or match_type:
                        crm_updates['Session_choisie'] = sess_id
                        logger.info(f"  📊 Session_choisie: {session_name} → ID {sess_id}")
                        session_found = True
                        break

            if not session_found:
                logger.warning(f"  ⚠️ Session formation non trouvée: {session_name}")

        # ================================================================
        # 2.5 Session confirmée par le candidat (CONFIRMATION_SESSION avec dates)
        # ================================================================
        # Si le candidat a confirmé sa session avec des dates et qu'on a matché une session
        if analysis_result.get('session_confirmed') and analysis_result.get('matched_session_id'):
            matched_session_id = analysis_result['matched_session_id']
            matched_session_name = analysis_result.get('matched_session_name', '')
            crm_updates['Session'] = matched_session_id
            logger.info(f"  📊 Session (confirmée): {matched_session_name} → ID {matched_session_id}")

            # Aussi mettre à jour Preference_horaire si on a le type
            matched_type = analysis_result.get('matched_session_type')
            if matched_type:
                crm_updates['Preference_horaire'] = matched_type
                logger.info(f"  📊 Preference_horaire: {matched_type}")

        # ================================================================
        # 2.6 Correction erreur CAB (DEMANDE_CHANGEMENT_SESSION avec plainte)
        # ================================================================
        # Si on a confirmé une erreur CAB et trouvé la session correcte
        if analysis_result.get('cab_error_corrected') and analysis_result.get('cab_error_corrected_session_id'):
            corrected_session_id = analysis_result['cab_error_corrected_session_id']
            corrected_session_name = analysis_result.get('cab_error_corrected_session_name', '')
            crm_updates['Session'] = corrected_session_id
            logger.info(f"  📊 Session (correction erreur CAB): {corrected_session_name} → ID {corrected_session_id}")

            # Aussi mettre à jour Preference_horaire avec le type correct
            corrected_type = analysis_result.get('cab_error_corrected_session_type')
            if corrected_type:
                crm_updates['Preference_horaire'] = corrected_type
                logger.info(f"  📊 Preference_horaire (corrigé): {corrected_type}")

        # ================================================================
        # 3. Autres champs (texte simple - pas de mapping nécessaire)
        # ================================================================
        for key, value in ai_updates.items():
            if key not in ['Date_examen_VTC', 'Session_choisie']:
                crm_updates[key] = value
                logger.info(f"  📊 {key}: {value}")

        if crm_updates:
            logger.info(f"  ✅ Final CRM updates: {list(crm_updates.keys())}")
        else:
            logger.warning(f"  ⚠️ No valid CRM updates after mapping")

        return crm_updates

    def close(self):
        """Clean up resources."""
        if hasattr(self, 'desk_client'):
            self.desk_client.close()
        if hasattr(self, 'crm_client'):
            self.crm_client.close()
        if hasattr(self, 'deal_linker') and hasattr(self.deal_linker, 'close'):
            self.deal_linker.close()
        if hasattr(self, 'dispatcher') and hasattr(self.dispatcher, 'close'):
            self.dispatcher.close()
        if hasattr(self, 'crm_update_agent') and hasattr(self.crm_update_agent, 'close'):
            self.crm_update_agent.close()
        # ExamT3PAgent, TriageAgent, and State Engine components don't have close() method


def test_workflow():
    """Test workflow with a sample ticket."""
    print("\n" + "=" * 80)
    print("TEST DOC TICKET WORKFLOW")
    print("=" * 80)

    print("\n🎯 Initializing workflow (State Engine)...")
    workflow = DOCTicketWorkflow()

    print("\n✅ Workflow initialized successfully")

    print("\n📋 Workflow stages:")
    print("  1. AGENT TRIEUR (triage with STOP & GO)")
    print("  2. AGENT ANALYSTE (6-source data extraction)")
    print("  3. STATE ENGINE (deterministic response generation)")
    print("     - StateDetector → Detect candidate state")
    print("     - TemplateEngine → Generate from templates")
    print("     - ResponseValidator → Validate response")
    print("     - CRMUpdater → Deterministic CRM updates")
    print("  4. CRM NOTE (mandatory before draft)")
    print("  5. TICKET UPDATE (status, tags)")
    print("  6. DEAL UPDATE (if scenario requires)")
    print("  7. DRAFT CREATION (Zoho Desk)")
    print("  8. FINAL VALIDATION")

    print("\n🎯 To run with a real ticket:")
    print("  workflow = DOCTicketWorkflow()")
    print("  workflow.process_ticket(")
    print("    ticket_id='198709000445353417',")
    print("    auto_create_draft=False,")
    print("    auto_update_crm=False,")
    print("    auto_update_ticket=False")
    print("  )")

    workflow.close()


if __name__ == "__main__":
    test_workflow()
