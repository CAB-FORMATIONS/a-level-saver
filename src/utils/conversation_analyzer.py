"""
Conversation Analyzer V3 — Intelligence conversationnelle pour ThreadMemory.

Analyse TOUS les threads d'un ticket pour extraire :
- Engagements pris (par le bot ou un humain)
- Décisions du candidat (date choisie, session confirmée, etc.)
- Mode de conversation (initial, confirmation, relance, etc.)
- Mode de réponse recommandé (full, brief, targeted, status_update)

Utilise un LLM (Sonnet) pour analyser la conversation, avec short-circuit
si le ticket n'a qu'un seul message entrant (pas besoin d'analyse).

Dégradation gracieuse : si le LLM échoue, retourne un ConversationState vide
et V2 ThreadMemory prend le relai normalement.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

import anthropic

from src.utils.text_utils import clean_html_content
from src.constants.models import MODEL_CONVERSATION
from src.constants.emails import INTERNAL_DOMAIN_MARKERS

logger = logging.getLogger(__name__)


@dataclass
class Commitment:
    """An engagement taken by the bot or a human agent."""
    type: str       # report_date, change_session, contact_cma, callback, wait_validation,
                    # create_account, escalate, refund_processing, send_convocation
    details: str    # "Changement de date vers le 28/04/2026"
    value: str      # "2026-04-28" (machine-readable)
    actor: str      # "bot" or human agent first name


@dataclass
class CandidateDecision:
    """A decision made by the candidate."""
    type: str       # date_choice, session_choice, keep_current, annulation_confirmed,
                    # documents_sent, preference_jour, preference_soir
    value: str      # "2026-04-28", "jour", "soir 16/03-27/03"
    confidence: str # "explicit" or "implicit"


@dataclass
class ConversationState:
    """Result of conversation analysis."""
    conversation_mode: str = 'initial_contact'
    response_mode: str = 'full'
    commitments: List[Commitment] = field(default_factory=list)
    candidate_decisions: List[CandidateDecision] = field(default_factory=list)
    target_date: str = ''
    target_session: str = ''
    human_is_handling: bool = False
    proposed_dates: List[str] = field(default_factory=list)
    proposed_sessions: List[str] = field(default_factory=list)
    analyzer_error: str = ''
    analyzer_latency_ms: int = 0

    def has_commitments(self) -> bool:
        return len(self.commitments) > 0

    def has_candidate_decisions(self) -> bool:
        return len(self.candidate_decisions) > 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for template engine context."""
        return {
            'conversation_mode': self.conversation_mode,
            'response_mode': self.response_mode,
            'commitments': [
                {'type': c.type, 'details': c.details, 'value': c.value, 'actor': c.actor}
                for c in self.commitments
            ],
            'candidate_decisions': [
                {'type': d.type, 'value': d.value, 'confidence': d.confidence}
                for d in self.candidate_decisions
            ],
            'target_date': self.target_date,
            'target_session': self.target_session,
            'human_is_handling': self.human_is_handling,
            'has_commitments': self.has_commitments(),
            'has_candidate_decisions': self.has_candidate_decisions(),
            'proposed_dates': self.proposed_dates,
            'proposed_sessions': self.proposed_sessions,
            'analyzer_error': self.analyzer_error,
            'analyzer_latency_ms': self.analyzer_latency_ms,
        }


ANALYZER_SYSTEM_PROMPT = """Tu es un analyseur de conversation pour un service de formation VTC.
Tu analyses l'historique complet d'un échange email entre un candidat et le service DOC de CAB Formations.

Tu dois extraire :

1. **commitments** : Engagements pris par le bot ou un agent humain envers le candidat.
   Types possibles :
   - report_date : "on change votre date vers le XX"
   - change_session : "on vous inscrit à la session du jour/soir"
   - contact_cma : "on contacte la CMA pour vous"
   - callback : "je reviens vers vous"
   - wait_validation : "la CMA vérifie votre dossier"
   - create_account : "on crée votre compte ExamT3P"
   - escalate : "je transmets à un collègue"
   - refund_processing : "le remboursement est en cours"
   - send_convocation : "la convocation arrive bientôt"

2. **candidate_decisions** : Décisions prises par le candidat.
   Types possibles :
   - date_choice : "OK pour le 28/04"
   - session_choice : "je prends les cours du soir du 16/03"
   - keep_current : "je garde ma date actuelle"
   - annulation_confirmed : "oui je veux annuler"
   - documents_sent : "j'ai envoyé les documents"
   - preference_jour : "je préfère le jour"
   - preference_soir : "je préfère le soir"

3. **conversation_mode** : Le mode actuel de la conversation.
   - initial_contact : Premier message du candidat
   - confirmation : Le candidat confirme un choix qu'on lui a proposé
   - clarification : Le candidat demande une précision
   - status_check : "Où en est-on ?", "Avez-vous des nouvelles ?"
   - insistence : Le candidat répète la même demande sans réponse
   - new_topic : Le candidat aborde un nouveau sujet
   - follow_up : Le candidat répond sans confirmer explicitement
   - complaint : Le candidat est mécontent
   - gratitude : Simple remerciement ("merci", "c'est noté")

4. **response_mode** : Le mode de réponse recommandé.
   - full : Réponse complète (initial_contact, new_topic)
   - brief_confirmation : Très court, 3-5 lignes (confirmation, gratitude)
   - targeted : Ciblé sur la question (clarification, insistence, follow_up, complaint)
   - status_update : Mise à jour factuelle (status_check)

5. **target_date** : Date cible si le candidat a confirmé ou si un engagement a été pris (format YYYY-MM-DD)

6. **target_session** : Session cible si confirmée ("jour" ou "soir")

7. **human_is_handling** : true UNIQUEMENT si le dernier message SORTANT a été envoyé par un humain (pas le bot) ET qu'il n'y a PAS eu de message ENTRANT après.

8. **proposed_dates** : Liste des dates d'examen proposées au candidat dans les messages sortants (format YYYY-MM-DD)

9. **proposed_sessions** : Liste des sessions proposées (ex: "jour 23/03-27/03", "soir 16/03-27/03")

RÈGLES CRITIQUES :
- Analyse TOUS les messages chronologiquement
- Le DERNIER message ENTRANT = celui auquel on va répondre
- Dates en format YYYY-MM-DD (convertir "28/04/2026" → "2026-04-28")
- Listes vides [] si rien trouvé
- En cas de doute : conversation_mode → "follow_up", response_mode → "targeted"
- Si le candidat change d'avis, seule la DERNIÈRE décision compte
- Engagements humains PRIMENT sur ceux du bot (plus récents)
- human_is_handling: true SEULEMENT si dernier message sortant = humain ET pas de message entrant après

RÉPONDS UNIQUEMENT en JSON valide, sans texte autour."""


def analyze_conversation(
    threads: list,
    current_deal_data: dict = None,
    enriched_lookups: dict = None,
) -> ConversationState:
    """Analyze conversation threads to extract intelligence.

    Args:
        threads: List of thread dicts from Zoho Desk API
        current_deal_data: Current deal data from CRM (optional)
        enriched_lookups: Enriched lookup data (optional)

    Returns:
        ConversationState with extracted intelligence
    """
    state = ConversationState()

    if not threads:
        return state

    try:
        # Short-circuit: count incoming messages
        incoming_count = _count_incoming_threads(threads)
        if incoming_count <= 1:
            logger.info("ConversationAnalyzer: <= 1 incoming thread → skip LLM (initial_contact)")
            return state

        # Format threads for the LLM
        formatted_threads = _format_threads_for_analyzer(threads)
        if not formatted_threads:
            logger.warning("ConversationAnalyzer: no formatted threads → skip")
            return state

        # Call LLM
        start_time = time.time()
        raw_result = _call_analyzer_llm(formatted_threads)
        elapsed_ms = int((time.time() - start_time) * 1000)
        state.analyzer_latency_ms = elapsed_ms

        if not raw_result:
            state.analyzer_error = 'llm_returned_empty'
            logger.warning(f"ConversationAnalyzer: LLM returned empty ({elapsed_ms}ms)")
            return state

        # Parse LLM result
        state = _parse_analyzer_result(raw_result, threads)
        state.analyzer_latency_ms = elapsed_ms

        # Validate extracted dates against thread content
        _validate_extracted_dates(state, threads)

        logger.info(
            f"ConversationAnalyzer: mode={state.conversation_mode}, "
            f"response={state.response_mode}, "
            f"commitments={len(state.commitments)}, "
            f"decisions={len(state.candidate_decisions)}, "
            f"target_date={state.target_date}, "
            f"human_handling={state.human_is_handling}, "
            f"latency={elapsed_ms}ms"
        )

    except Exception as e:
        state.analyzer_error = str(e)
        logger.warning(f"ConversationAnalyzer failed (graceful): {e}")

    return state


def _count_incoming_threads(threads: list) -> int:
    """Count incoming (candidate) threads."""
    count = 0
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        direction = thread.get('direction', '')
        if direction == 'in':
            count += 1
        elif not direction:
            # Fallback: check fromEmailAddress
            from_email = thread.get('fromEmailAddress', '')
            if from_email and not any(m in from_email.lower() for m in INTERNAL_DOMAIN_MARKERS):
                count += 1
    return count


def _format_threads_for_analyzer(threads: list) -> str:
    """Format threads for the LLM analyzer prompt.

    - Sort chronologically (oldest first)
    - Mark direction: [ENTRANT] or [SORTANT]
    - Clean HTML content
    - Truncate: 800 chars for last 4, 300 chars for older
    - Max 15 threads
    """
    # Build list of (timestamp_str, direction, content, raw_thread)
    thread_entries = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue

        # Get direction
        direction = thread.get('direction', '')
        if direction == 'in':
            dir_label = 'ENTRANT'
        elif direction == 'out':
            dir_label = 'SORTANT'
        else:
            from_email = thread.get('fromEmailAddress', '')
            if from_email and not any(m in from_email.lower() for m in INTERNAL_DOMAIN_MARKERS):
                dir_label = 'ENTRANT'
            else:
                dir_label = 'SORTANT'

        # Get timestamp
        created = thread.get('createdTime', '') or thread.get('Created_Time', '')
        if created:
            # Format as DD/MM/YYYY HH:MM
            try:
                from datetime import datetime
                if 'T' in str(created):
                    dt = datetime.fromisoformat(str(created).replace('Z', '+00:00'))
                    ts_display = dt.strftime('%d/%m/%Y %H:%M')
                else:
                    ts_display = str(created)[:16]
            except Exception:
                ts_display = str(created)[:16]
        else:
            ts_display = '??/??/???? ??:??'

        # Get clean content
        content = _get_clean_content(thread)
        if not content or content == 'N/A':
            continue

        thread_entries.append({
            'ts_display': ts_display,
            'ts_raw': created,
            'direction': dir_label,
            'content': content,
        })

    if not thread_entries:
        return ''

    # Sort chronologically (oldest first)
    thread_entries.sort(key=lambda x: str(x['ts_raw']))

    # Limit to 15 most recent
    if len(thread_entries) > 15:
        thread_entries = thread_entries[-15:]

    # Truncate: 800 chars for last 4, 300 chars for older
    total = len(thread_entries)
    lines = []
    for i, entry in enumerate(thread_entries):
        max_chars = 800 if i >= total - 4 else 300
        content = entry['content'][:max_chars]
        if len(entry['content']) > max_chars:
            content += '...'
        lines.append(f"[{entry['direction']} - {entry['ts_display']}]\n{content}")

    return '\n\n---\n\n'.join(lines)


def _get_clean_content(thread: dict) -> str:
    """Extract and clean thread content, stripping forwarded/quoted parts."""
    # Try plainText first
    plain = thread.get('plainText', '').strip()
    if plain:
        content = plain
    else:
        html_content = thread.get('content', '')
        if html_content:
            content = clean_html_content(html_content)
        else:
            return ''

    # Strip forwarded/quoted content (inline implementation to avoid circular imports)
    # Remove blockquotes
    content = re.sub(r'<blockquote[^>]*>.*?</blockquote>', '', content, flags=re.DOTALL | re.IGNORECASE)
    # Remove Gmail quote divs
    content = re.sub(r'<div\s+class="gmail_quote"[^>]*>.*', '', content, flags=re.DOTALL | re.IGNORECASE)
    # Remove French reply headers
    content = re.sub(
        r'Le\s+\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s+[àa]\s+\d{1,2}[h:]\d{2}.*?(?:a\s+[eé]crit|wrote)\s*:.*',
        '', content, flags=re.DOTALL | re.IGNORECASE
    )
    # Remove "De:/From:" headers
    content = re.sub(r'(?:De|From)\s*:.*?(?:Objet|Subject)\s*:.*', '', content, flags=re.DOTALL | re.IGNORECASE)
    # Remove "-----Message d'origine-----"
    content = re.sub(r'-{3,}\s*(?:Message d.origine|Original Message)\s*-{3,}.*', '', content, flags=re.DOTALL | re.IGNORECASE)
    # Remove Outlook underscores
    content = re.sub(r'_{10,}.*', '', content, flags=re.DOTALL)
    # Remove quoted lines
    content = re.sub(r'^>.*$', '', content, flags=re.MULTILINE)
    # Remove signatures
    content = re.sub(r'(?:Sent from my iPhone|Envoyé depuis mon|Envoy[eé] de mon).*', '', content, flags=re.DOTALL | re.IGNORECASE)
    # Remove SalesIQ metadata
    idx = content.lower().find('informations sur le visiteur')
    if idx != -1:
        content = content[:idx]

    return content.strip()


def _call_analyzer_llm(formatted_threads: str) -> Optional[str]:
    """Call the LLM to analyze the conversation."""
    try:
        from config import settings
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        user_prompt = f"""Analyse cette conversation et extrais les informations demandées.

CONVERSATION :
{formatted_threads}

Réponds en JSON avec cette structure exacte :
{{
  "conversation_mode": "...",
  "response_mode": "...",
  "commitments": [
    {{"type": "...", "details": "...", "value": "...", "actor": "bot"}}
  ],
  "candidate_decisions": [
    {{"type": "...", "value": "...", "confidence": "explicit"}}
  ],
  "target_date": "",
  "target_session": "",
  "human_is_handling": false,
  "proposed_dates": [],
  "proposed_sessions": []
}}"""

        response = client.messages.create(
            model=MODEL_CONVERSATION,
            max_tokens=600,
            system=ANALYZER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=15.0,
        )

        return response.content[0].text.strip()

    except anthropic.APITimeoutError:
        logger.warning("ConversationAnalyzer: LLM timeout (15s)")
        return None
    except Exception as e:
        logger.warning(f"ConversationAnalyzer: LLM call failed: {e}")
        return None


def _parse_analyzer_result(raw_text: str, threads: list) -> ConversationState:
    """Parse the LLM JSON response into a ConversationState."""
    state = ConversationState()

    # Try direct JSON parse
    data = None
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        # Regex fallback: extract JSON object
        match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if not data or not isinstance(data, dict):
        state.analyzer_error = 'json_parse_failed'
        logger.warning(f"ConversationAnalyzer: JSON parse failed. Raw: {raw_text[:200]}")
        return state

    # Extract fields
    state.conversation_mode = data.get('conversation_mode', 'follow_up')
    state.response_mode = data.get('response_mode', 'targeted')
    state.target_date = data.get('target_date', '')
    state.target_session = data.get('target_session', '')
    state.human_is_handling = bool(data.get('human_is_handling', False))
    state.proposed_dates = data.get('proposed_dates', [])
    state.proposed_sessions = data.get('proposed_sessions', [])

    # Parse commitments
    for c in data.get('commitments', []):
        if isinstance(c, dict) and c.get('type'):
            state.commitments.append(Commitment(
                type=c.get('type', ''),
                details=c.get('details', ''),
                value=c.get('value', ''),
                actor=c.get('actor', 'bot'),
            ))

    # Parse candidate decisions
    for d in data.get('candidate_decisions', []):
        if isinstance(d, dict) and d.get('type'):
            state.candidate_decisions.append(CandidateDecision(
                type=d.get('type', ''),
                value=d.get('value', ''),
                confidence=d.get('confidence', 'implicit'),
            ))

    return state


def _validate_extracted_dates(state: ConversationState, threads: list) -> None:
    """Validate that extracted dates actually appear in the thread content.

    Anti-hallucination: if the LLM invents a date, clear it.
    """
    if not state.target_date:
        return

    # Collect all text from threads
    all_text = ''
    for thread in threads:
        if isinstance(thread, dict):
            all_text += ' ' + _get_clean_content(thread)

    # Check if target_date (YYYY-MM-DD) appears in thread content
    # Either as YYYY-MM-DD or as DD/MM/YYYY
    target = state.target_date
    if re.match(r'^\d{4}-\d{2}-\d{2}$', target):
        # Convert to DD/MM/YYYY for searching
        parts = target.split('-')
        date_ddmmyyyy = f"{parts[2]}/{parts[1]}/{parts[0]}"
        date_ddmm = f"{parts[2]}/{parts[1]}"

        if target not in all_text and date_ddmmyyyy not in all_text and date_ddmm not in all_text:
            logger.warning(f"ConversationAnalyzer: target_date {target} not found in threads → cleared (anti-hallucination)")
            state.target_date = ''
