"""
ThreadMemory - Persistent memory for ticket workflow.

Reads [META] lines from CRM deal notes to know what was previously communicated
to a candidate. Optionally uses the Zoho CRM Timeline API (v8) to detect real
field changes and human interventions (manual agent responses).

Hybrid approach:
- [META] → which sections were communicated (suppression logic)
- Timeline → real CRM field changes + human intervention detection (guard rail)

Usage:
    from src.utils.thread_memory import analyze_thread_memory

    deal_notes = crm_client.get_deal_notes(deal_id)
    deal_timeline = crm_client.get_deal_timeline(deal_id)
    result = analyze_thread_memory(
        notes=deal_notes,
        current_deal_data=deal_data,
        current_intent=detected_intent,
        ticket_threads=threads,
        timeline=deal_timeline
    )
    # result.suppress_dates, result.is_relance, result.human_intervention_detected, etc.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Intents that explicitly ask about a section → never suppress that section
INTENT_PROTECTS_SECTION = {
    'ENVOIE_IDENTIFIANTS': 'identifiants',
    'DEMANDE_DATE_EXAMEN': 'dates',
    'DEMANDE_DATE_PLUS_TOT': 'dates',
    'DEMANDE_CHANGEMENT_SESSION': 'sessions',
    'CONFIRMATION_SESSION': 'sessions',
    'DEMANDE_ACCES_ELEARNING': 'elearning',
    'CONFIRMATION_PAIEMENT': 'paiement',
    'DEMANDE_STATUT_DOSSIER': 'statut',
    'DEMANDE_ANNULATION': 'annulation',
}

# CRM fields tracked via Timeline API
TRACKED_FIELDS = {
    'Evalbox',
    'Session',
    'Session_souhait_e',
    'Date_examen_VTC',
    'IDENTIFIANT_EVALBOX',
    'Stage',
    'Date_de_depot_CMA',
    'Frais_Examen',
    'PAYE_EN_PROD',
}

# Timeline sources indicating human (non-automation) actions
HUMAN_SOURCES = {'crm_ui', 'webform', 'manual'}


@dataclass
class FieldChange:
    """A tracked field change from the Timeline API."""
    field: str
    old_value: str
    new_value: str
    timestamp: Optional[datetime] = None
    actor: str = ''
    source: str = ''


@dataclass
class HumanIntervention:
    """A human action detected via the Timeline API."""
    actor: str = ''
    timestamp: Optional[datetime] = None
    action: str = ''  # 'note_added', 'email_sent', 'field_updated'
    details: str = ''


@dataclass
class MetaRecord:
    """Parsed [META] line from a CRM note."""
    ticket_id: str = ''
    timestamp: Optional[datetime] = None
    state: str = ''
    intent: str = ''
    evalbox: str = ''
    date_exam: str = ''
    date_case: str = ''
    session: str = ''
    sections: List[str] = field(default_factory=list)
    secondary_intents: List[str] = field(default_factory=list)
    # V3 fields (backward-compatible)
    target_date: str = ''
    proposed_dates: List[str] = field(default_factory=list)
    proposed_sessions: List[str] = field(default_factory=list)
    response_mode: str = ''


@dataclass
class ThreadMemoryResult:
    """Result of thread memory analysis."""
    has_history: bool = False
    previous_records: List[MetaRecord] = field(default_factory=list)
    last_record: Optional[MetaRecord] = None
    # Suppression flags
    suppress_identifiants: bool = False
    suppress_dates: bool = False
    suppress_sessions: bool = False
    suppress_elearning: bool = False
    suppress_statut: bool = False
    suppress_paiement: bool = False
    # Progression detection
    evalbox_changed: bool = False
    evalbox_previous: str = ''
    evalbox_current: str = ''
    date_exam_changed: bool = False
    date_exam_previous: str = ''
    date_exam_current: str = ''
    # Relance detection
    is_relance: bool = False
    days_since_last: int = 0
    unanswered_count: int = 0
    # Timeline-based (V2)
    human_intervention_detected: bool = False
    human_intervention_actor: str = ''
    human_intervention_time: Optional[datetime] = None
    field_changes_since_last: List[FieldChange] = field(default_factory=list)


def parse_meta_line(line: str) -> Optional[MetaRecord]:
    """Parse a [META] line into a MetaRecord.

    Format: [META] key=value | key=value | ...

    Example:
        [META] ticket=198709000449714052 | ts=2026-02-07T14:30 | state=VALIDE_CMA_WAITING_CONVOC | intent=DEMANDE_STATUT_DOSSIER | evalbox=VALIDE CMA | date_exam=2026-03-31 | sections=statut,dates
    """
    if not line or '[META]' not in line:
        return None

    try:
        # Extract everything after [META]
        meta_part = line.split('[META]', 1)[1].strip()
        if not meta_part:
            return None

        # Parse key=value pairs separated by ' | '
        pairs = {}
        for segment in meta_part.split(' | '):
            segment = segment.strip()
            if '=' not in segment:
                continue
            key, value = segment.split('=', 1)
            pairs[key.strip()] = value.strip()

        if not pairs:
            return None

        # Build MetaRecord
        record = MetaRecord()
        record.ticket_id = pairs.get('ticket', '')

        ts_str = pairs.get('ts', '')
        if ts_str:
            try:
                record.timestamp = datetime.strptime(ts_str, '%Y-%m-%dT%H:%M')
            except ValueError:
                try:
                    record.timestamp = datetime.strptime(ts_str, '%Y-%m-%dT%H:%M:%S')
                except ValueError:
                    logger.debug(f"Could not parse META timestamp: {ts_str}")

        record.state = pairs.get('state', '')
        record.intent = pairs.get('intent', '')
        record.evalbox = pairs.get('evalbox', '')
        record.date_exam = pairs.get('date_exam', '')
        record.date_case = pairs.get('date_case', '')
        record.session = pairs.get('session', '')

        sections_str = pairs.get('sections', '')
        if sections_str:
            record.sections = [s.strip() for s in sections_str.split(',') if s.strip()]

        intents_sec_str = pairs.get('intents_sec', '')
        if intents_sec_str:
            record.secondary_intents = [s.strip() for s in intents_sec_str.split(',') if s.strip()]

        # V3 fields (backward-compatible: unknown keys are simply ignored)
        record.target_date = pairs.get('target_date', '')
        proposed_dates_str = pairs.get('proposed_dates', '')
        if proposed_dates_str:
            record.proposed_dates = [d.strip() for d in proposed_dates_str.split(',') if d.strip()]
        proposed_sessions_str = pairs.get('proposed_sessions', '')
        if proposed_sessions_str:
            record.proposed_sessions = [s.strip() for s in proposed_sessions_str.split('|') if s.strip()]
        record.response_mode = pairs.get('response_mode', '')

        return record

    except Exception as e:
        logger.debug(f"Failed to parse META line: {e}")
        return None


def extract_meta_records_from_notes(notes_response: dict) -> List[MetaRecord]:
    """Extract MetaRecords from CRM notes API response.

    Args:
        notes_response: Raw response from get_deal_notes() — {"data": [...]}

    Returns:
        List of MetaRecord sorted by timestamp (oldest first)
    """
    records = []

    if not notes_response or not isinstance(notes_response, dict):
        return records

    notes_list = notes_response.get('data', [])
    if not notes_list or not isinstance(notes_list, list):
        return records

    for note in notes_list:
        if not isinstance(note, dict):
            continue

        content = note.get('Note_Content', '')
        if not content or '[META]' not in content:
            continue

        # [META] line is at the top of the note content
        for line in content.split('\n'):
            line = line.strip()
            if line.startswith('[META]'):
                record = parse_meta_line(line)
                if record:
                    records.append(record)
                break  # Only first [META] line per note

    # Sort by timestamp (oldest first), None timestamps at the beginning
    records.sort(key=lambda r: r.timestamp or datetime.min)

    return records


def parse_timeline(timeline_response: dict) -> Tuple[List[FieldChange], List[HumanIntervention]]:
    """Parse the Zoho CRM v8 Timeline API response.

    Extracts:
    - Field changes on tracked fields (Evalbox, Session, Date_examen_VTC, etc.)
    - Human interventions (notes added via UI, emails sent manually)

    Args:
        timeline_response: Raw response from get_deal_timeline()

    Returns:
        Tuple of (field_changes, human_interventions)
    """
    field_changes = []
    human_interventions = []

    if not timeline_response or not isinstance(timeline_response, dict):
        return field_changes, human_interventions

    entries = timeline_response.get('__timeline', [])
    if not entries or not isinstance(entries, list):
        return field_changes, human_interventions

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        action = entry.get('action', '')
        timestamp = _parse_timeline_timestamp(entry.get('audited_time') or entry.get('done_time'))
        actor = _extract_actor_name(entry)
        source = entry.get('source', '')

        if action == 'updated':
            # Field updates — extract from field_history (v8 Timeline API)
            field_history = entry.get('field_history', [])
            if not isinstance(field_history, list):
                continue

            for fh in field_history:
                if not isinstance(fh, dict):
                    continue
                api_name = fh.get('api_name', '')
                if api_name not in TRACKED_FIELDS:
                    continue

                # v8 API: values are nested in _value.old / _value.new
                value_obj = fh.get('_value', {})
                if not isinstance(value_obj, dict):
                    continue
                old_val = str(value_obj.get('old', '')) if value_obj.get('old') is not None else ''
                new_val = str(value_obj.get('new', '')) if value_obj.get('new') is not None else ''

                field_changes.append(FieldChange(
                    field=api_name,
                    old_value=old_val,
                    new_value=new_val,
                    timestamp=timestamp,
                    actor=actor,
                    source=source,
                ))

                # If updated via UI by a human → also record as intervention
                if source in HUMAN_SOURCES:
                    human_interventions.append(HumanIntervention(
                        actor=actor,
                        timestamp=timestamp,
                        action='field_updated',
                        details=f"{api_name}: {old_val} → {new_val}",
                    ))

        elif action == 'added':
            # Check if it's a note added via CRM UI (not automation)
            record_info = entry.get('record', {})
            module = record_info.get('module', {})
            module_name = module.get('api_name', '') if isinstance(module, dict) else str(module)

            if module_name == 'Notes' and source in HUMAN_SOURCES:
                human_interventions.append(HumanIntervention(
                    actor=actor,
                    timestamp=timestamp,
                    action='note_added',
                    details=record_info.get('name', ''),
                ))

        elif action == 'sent':
            # Email sent manually
            if source in HUMAN_SOURCES:
                human_interventions.append(HumanIntervention(
                    actor=actor,
                    timestamp=timestamp,
                    action='email_sent',
                    details=entry.get('record', {}).get('name', ''),
                ))

    # Sort by timestamp
    field_changes.sort(key=lambda fc: fc.timestamp or datetime.min)
    human_interventions.sort(key=lambda hi: hi.timestamp or datetime.min)

    return field_changes, human_interventions


def _parse_timeline_timestamp(val) -> Optional[datetime]:
    """Parse a timestamp from a timeline entry.

    Returns a naive datetime (no timezone) to be comparable with META timestamps.
    """
    if not val:
        return None
    if isinstance(val, datetime):
        # Strip timezone to make comparable with naive META timestamps
        return val.replace(tzinfo=None)
    if isinstance(val, str):
        # Try common Zoho formats
        for fmt in (
            '%Y-%m-%dT%H:%M:%S%z',
            '%Y-%m-%dT%H:%M:%S.%f%z',
            '%Y-%m-%dT%H:%M:%SZ',
            '%Y-%m-%dT%H:%M:%S.%fZ',
        ):
            try:
                dt = datetime.strptime(val, fmt)
                return dt.replace(tzinfo=None)
            except ValueError:
                continue
        # Fallback: truncate to 19 chars (already naive)
        try:
            return datetime.strptime(val[:19], '%Y-%m-%dT%H:%M:%S')
        except (ValueError, IndexError):
            pass
    return None


def _extract_actor_name(entry: dict) -> str:
    """Extract the actor name from a timeline entry."""
    done_by = entry.get('done_by', {})
    if isinstance(done_by, dict):
        return done_by.get('name', '')
    return str(done_by) if done_by else ''


def _detect_human_intervention(
    interventions: List[HumanIntervention],
    last_meta: Optional[MetaRecord]
) -> dict:
    """Check if a human intervened after the last META record.

    Args:
        interventions: All human interventions from timeline
        last_meta: The most recent META record

    Returns:
        dict with 'detected', 'actor', 'timestamp'
    """
    result = {'detected': False, 'actor': '', 'timestamp': None}

    if not interventions or not last_meta or not last_meta.timestamp:
        return result

    # Find the first human intervention AFTER the last META timestamp
    for hi in interventions:
        if hi.timestamp and hi.timestamp > last_meta.timestamp:
            result['detected'] = True
            result['actor'] = hi.actor
            result['timestamp'] = hi.timestamp
            return result

    return result


def _compute_progression_from_timeline(
    field_changes: List[FieldChange],
    records: List[MetaRecord]
) -> dict:
    """Compute progression using timeline field changes (more reliable than snapshots)."""
    last = records[-1]

    evalbox_changed = False
    evalbox_previous = ''
    evalbox_current = ''
    date_exam_changed = False
    date_exam_previous = ''
    date_exam_current = ''

    for fc in field_changes:
        if fc.field == 'Evalbox' and fc.old_value != fc.new_value:
            evalbox_changed = True
            evalbox_previous = fc.old_value
            evalbox_current = fc.new_value
        elif fc.field == 'Date_examen_VTC' and fc.old_value != fc.new_value:
            date_exam_changed = True
            # Clean up the value (may contain "34_2026-03-31" format)
            old_v = fc.old_value
            new_v = fc.new_value
            if '_' in old_v:
                old_v = old_v.split('_', 1)[1]
            if '_' in new_v:
                new_v = new_v.split('_', 1)[1]
            date_exam_previous = old_v
            date_exam_current = new_v

    return {
        'evalbox_changed': evalbox_changed,
        'evalbox_previous': evalbox_previous,
        'evalbox_current': evalbox_current,
        'date_exam_changed': date_exam_changed,
        'date_exam_previous': date_exam_previous,
        'date_exam_current': date_exam_current,
    }


def analyze_thread_memory(
    notes: dict,
    current_deal_data: dict,
    current_intent: str,
    ticket_threads: list = None,
    timeline: dict = None
) -> ThreadMemoryResult:
    """Main entry point — analyze thread memory from CRM notes + timeline.

    Args:
        notes: Raw response from get_deal_notes()
        current_deal_data: Current deal data from CRM
        current_intent: Primary intent detected by triage
        ticket_threads: List of ticket threads (for relance detection)
        timeline: Raw response from get_deal_timeline() (v8 API, optional)

    Returns:
        ThreadMemoryResult with suppression flags, progression, and relance info
    """
    result = ThreadMemoryResult()

    try:
        records = extract_meta_records_from_notes(notes)

        if not records:
            logger.info("ThreadMemory: No META records found — first interaction")
            return result

        result.has_history = True
        result.previous_records = records
        result.last_record = records[-1]

        logger.info(f"ThreadMemory: Found {len(records)} META records, last from {result.last_record.timestamp}")

        # Parse timeline if available
        field_changes = []
        human_interventions = []
        if timeline:
            field_changes, human_interventions = parse_timeline(timeline)
            logger.info(f"ThreadMemory: Timeline loaded ({len(field_changes)} field changes, {len(human_interventions)} human interventions)")

        # Detect human intervention since last META
        hi_result = _detect_human_intervention(human_interventions, result.last_record)
        result.human_intervention_detected = hi_result.get('detected', False)
        result.human_intervention_actor = hi_result.get('actor', '')
        result.human_intervention_time = hi_result.get('timestamp')

        # Filter field_changes to those after last META timestamp
        if result.last_record.timestamp and field_changes:
            result.field_changes_since_last = [
                fc for fc in field_changes
                if fc.timestamp and fc.timestamp > result.last_record.timestamp
            ]
        else:
            result.field_changes_since_last = field_changes

        # Compute suppression flags
        suppression = _compute_suppression_flags(records, current_deal_data, current_intent)
        result.suppress_identifiants = suppression.get('suppress_identifiants', False)
        result.suppress_dates = suppression.get('suppress_dates', False)
        result.suppress_sessions = suppression.get('suppress_sessions', False)
        result.suppress_elearning = suppression.get('suppress_elearning', False)
        result.suppress_statut = suppression.get('suppress_statut', False)
        result.suppress_paiement = suppression.get('suppress_paiement', False)

        # Human intervention guard rail: if a human responded after our last META,
        # reset ALL suppressions — we don't know what the human said
        if result.human_intervention_detected:
            logger.info(f"ThreadMemory: Human intervention detected ({result.human_intervention_actor}) → suppression reset")
            result.suppress_identifiants = False
            result.suppress_dates = False
            result.suppress_sessions = False
            result.suppress_elearning = False
            result.suppress_statut = False
            result.suppress_paiement = False

        # Compute progression — prefer timeline field_changes when available
        if result.field_changes_since_last:
            progression = _compute_progression_from_timeline(result.field_changes_since_last, records)
        else:
            progression = _compute_progression(records, current_deal_data)
        result.evalbox_changed = progression.get('evalbox_changed', False)
        result.evalbox_previous = progression.get('evalbox_previous', '')
        result.evalbox_current = progression.get('evalbox_current', '')
        result.date_exam_changed = progression.get('date_exam_changed', False)
        result.date_exam_previous = progression.get('date_exam_previous', '')
        result.date_exam_current = progression.get('date_exam_current', '')

        # Detect relance
        relance = _detect_relance(records, ticket_threads)
        result.is_relance = relance.get('is_relance', False)
        result.days_since_last = relance.get('days_since_last', 0)
        result.unanswered_count = relance.get('unanswered_count', 0)

        _log_summary(result)

    except Exception as e:
        logger.warning(f"ThreadMemory analysis failed (graceful degradation): {e}")

    return result


def _compute_suppression_flags(
    records: List[MetaRecord],
    current_deal_data: dict,
    current_intent: str
) -> dict:
    """Compute which sections to suppress based on previous communications.

    Rules:
    - Suppress section if it was communicated in the last record
    - UNLESS a relevant CRM field changed since then (progression detected)
    - UNLESS the current intent explicitly asks about that section
    """
    last = records[-1]
    last_sections = set(last.sections)

    if not last_sections:
        return {}

    # Determine which section the current intent protects
    protected_section = INTENT_PROTECTS_SECTION.get(current_intent, '')

    # Check for CRM changes since last record
    current_evalbox = current_deal_data.get('Evalbox', 'N/A')
    current_date_exam = _extract_current_date_exam(current_deal_data)

    evalbox_changed = (
        last.evalbox
        and last.evalbox != 'N/A'
        and current_evalbox != last.evalbox
    )
    date_exam_changed = (
        last.date_exam
        and last.date_exam != 'N/A'
        and current_date_exam != last.date_exam
    )

    flags = {}

    # For each section, check if we should suppress
    section_checks = {
        'identifiants': {
            'suppress_key': 'suppress_identifiants',
            'change_invalidates': False,  # Identifiants don't change with CRM fields
        },
        'dates': {
            'suppress_key': 'suppress_dates',
            'change_invalidates': date_exam_changed,  # New date = must communicate
        },
        'sessions': {
            'suppress_key': 'suppress_sessions',
            'change_invalidates': False,  # Sessions are independent
        },
        'elearning': {
            'suppress_key': 'suppress_elearning',
            'change_invalidates': False,
        },
        'statut': {
            'suppress_key': 'suppress_statut',
            'change_invalidates': evalbox_changed,  # Status changed = must communicate
        },
        'paiement': {
            'suppress_key': 'suppress_paiement',
            'change_invalidates': evalbox_changed,  # Payment status linked to evalbox
        },
    }

    for section_name, config in section_checks.items():
        suppress_key = config['suppress_key']

        if section_name not in last_sections:
            flags[suppress_key] = False
            continue

        # Section was communicated last time
        if section_name == protected_section:
            # Current intent explicitly asks about this section → don't suppress
            flags[suppress_key] = False
            logger.debug(f"ThreadMemory: {section_name} protected by intent {current_intent}")
        elif config['change_invalidates']:
            # CRM field changed → must re-communicate
            flags[suppress_key] = False
            logger.debug(f"ThreadMemory: {section_name} has CRM changes → not suppressed")
        else:
            # Section was communicated, no changes, not asked about → suppress
            flags[suppress_key] = True
            logger.debug(f"ThreadMemory: {section_name} → suppressed (already communicated, no changes)")

    return flags


def _compute_progression(records: List[MetaRecord], current_deal_data: dict) -> dict:
    """Detect CRM field changes between last META record and current state."""
    last = records[-1]

    current_evalbox = current_deal_data.get('Evalbox', 'N/A')
    current_date_exam = _extract_current_date_exam(current_deal_data)

    evalbox_changed = (
        last.evalbox
        and last.evalbox != 'N/A'
        and current_evalbox != 'N/A'
        and current_evalbox != last.evalbox
    )

    date_exam_changed = (
        last.date_exam
        and last.date_exam != 'N/A'
        and current_date_exam != 'N/A'
        and current_date_exam != last.date_exam
    )

    return {
        'evalbox_changed': evalbox_changed,
        'evalbox_previous': last.evalbox if evalbox_changed else '',
        'evalbox_current': current_evalbox if evalbox_changed else '',
        'date_exam_changed': date_exam_changed,
        'date_exam_previous': last.date_exam if date_exam_changed else '',
        'date_exam_current': current_date_exam if date_exam_changed else '',
    }


def _detect_relance(records: List[MetaRecord], ticket_threads: list = None) -> dict:
    """Detect if the candidate is following up without receiving a response.

    A relance is detected when:
    - We have at least one previous META record (we responded before)
    - The candidate sent messages after our last response
    """
    result = {
        'is_relance': False,
        'days_since_last': 0,
        'unanswered_count': 0,
    }

    if not records:
        return result

    last = records[-1]

    # Calculate days since last interaction
    if last.timestamp:
        delta = datetime.now() - last.timestamp
        result['days_since_last'] = delta.days

    # Count candidate messages after our last response
    if ticket_threads and last.timestamp:
        unanswered = 0
        for thread in ticket_threads:
            if not isinstance(thread, dict):
                continue

            # Check if this is an incoming message (from candidate)
            direction = thread.get('direction', thread.get('channel', ''))
            is_incoming = direction in ('in', 'incoming')

            # Also check by looking at isForward/fromEmail patterns
            if not is_incoming:
                is_forward = thread.get('isForward', False)
                from_email = thread.get('fromEmailAddress', '')
                # If it has a fromEmailAddress and is not a forward, likely incoming
                if from_email and not is_forward and '@cabformation' not in from_email.lower():
                    is_incoming = True

            if not is_incoming:
                continue

            # Parse thread creation time
            thread_time = _parse_thread_time(thread)
            if thread_time and thread_time > last.timestamp:
                unanswered += 1

        result['unanswered_count'] = unanswered
        result['is_relance'] = unanswered >= 1 and result['days_since_last'] >= 1

    return result


def _extract_current_date_exam(deal_data: dict) -> str:
    """Extract current exam date from deal data, handling lookup format."""
    date_exam = deal_data.get('Date_examen_VTC', '')

    if isinstance(date_exam, dict):
        # Lookup format: {"name": "34_2026-03-31", "id": "..."}
        name = date_exam.get('name', '')
        # Extract date part after underscore
        if '_' in name:
            return name.split('_', 1)[1]
        return name

    if isinstance(date_exam, str) and '_' in date_exam:
        return date_exam.split('_', 1)[1]

    return str(date_exam) if date_exam else 'N/A'


def _parse_thread_time(thread: dict) -> Optional[datetime]:
    """Parse thread creation time from various possible fields."""
    for time_field in ('createdTime', 'modifiedTime', 'Created_Time'):
        val = thread.get(time_field)
        if not val:
            continue

        if isinstance(val, datetime):
            return val

        if isinstance(val, str):
            # Try ISO format variations
            for fmt in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S.%f%z'):
                try:
                    return datetime.strptime(val, fmt)
                except ValueError:
                    continue
            # Try simple date
            try:
                return datetime.strptime(val[:19], '%Y-%m-%dT%H:%M:%S')
            except (ValueError, IndexError):
                pass

    return None


def _log_summary(result: ThreadMemoryResult):
    """Log a summary of ThreadMemory analysis."""
    suppressed = []
    if result.suppress_identifiants:
        suppressed.append('identifiants')
    if result.suppress_dates:
        suppressed.append('dates')
    if result.suppress_sessions:
        suppressed.append('sessions')
    if result.suppress_elearning:
        suppressed.append('elearning')
    if result.suppress_statut:
        suppressed.append('statut')
    if result.suppress_paiement:
        suppressed.append('paiement')

    parts = [f"history={len(result.previous_records)} records"]

    if suppressed:
        parts.append(f"suppress=[{','.join(suppressed)}]")

    if result.evalbox_changed:
        parts.append(f"evalbox: {result.evalbox_previous} → {result.evalbox_current}")

    if result.date_exam_changed:
        parts.append(f"date_exam: {result.date_exam_previous} → {result.date_exam_current}")

    if result.is_relance:
        parts.append(f"RELANCE (days={result.days_since_last}, unanswered={result.unanswered_count})")

    if result.human_intervention_detected:
        parts.append(f"HUMAN_INTERVENTION ({result.human_intervention_actor})")

    if result.field_changes_since_last:
        parts.append(f"timeline_changes={len(result.field_changes_since_last)}")

    logger.info(f"ThreadMemory summary: {' | '.join(parts)}")
