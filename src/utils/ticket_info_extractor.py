"""
Extraction d'informations des tickets pour mise à jour CRM.

Ce helper analyse les threads de tickets pour détecter les confirmations
du candidat (date d'examen, session de formation, préférences).

RÈGLES CRITIQUES:
=================

1. JAMAIS MODIFIER Date_examen_VTC automatiquement SI:
   - Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"}
   - ET Date_Cloture_Inscription < aujourd'hui (passée)
   → Seul un humain peut traiter

2. Les confirmations candidat doivent être prises avec précaution:
   - Report de date → vérifier si clôture passée
   - Si clôture passée + validé CMA → demander justificatif force majeure

3. Communication UNIQUEMENT par EMAIL, jamais par téléphone.

PATTERNS DÉTECTÉS:
==================
- Confirmation date examen: "je confirme pour le 15/03", "ok pour le 15 mars"
- Préférence session: "cours du soir", "cours du jour", "en journée"
- Confirmation session: "ok pour la session du 24/02"
- Demande de report: "je souhaite décaler", "reporter mon examen"
"""
import re
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple
from src.constants.amounts import CMA_EXAM_FEE
from src.utils.date_utils import parse_date_flexible, parse_datetime_flexible

logger = logging.getLogger(__name__)

# Patterns de détection des confirmations
CONFIRMATION_PATTERNS = {
    'date_examen': [
        # Confirmation explicite avec date
        r"(?:je\s+)?confirm[ée]?\s+(?:pour\s+)?(?:le\s+)?(\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?)",
        r"(?:ok|d'accord|parfait|c'est\s+bon)\s+pour\s+(?:le\s+)?(\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?)",
        r"(?:je\s+)?choisis?\s+(?:la\s+date\s+)?(?:du\s+)?(\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?)",
        r"examen\s+(?:du\s+)?(\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?)\s+(?:me\s+convient|ok|parfait)",
    ],
    # Note: "Option 1/2" est maintenant géré par l'IA (ResponseGeneratorAgent)
    # qui analyse le contexte complet pour comprendre à quoi ça correspond
    'session_preference': [
        # Cours du jour
        r"(?:je\s+)?(?:préfère|choisis?|veux|souhaite)\s+(?:les?\s+)?cours\s+du\s+(jour)",
        r"cours\s+du\s+(jour)\s+(?:me\s+convient|ok|parfait|svp|s'il vous plait)",
        r"(?:en\s+)?(journée)\s+(?:me\s+convient|pour\s+moi|svp)",
        r"(?:je\s+suis\s+)?disponible\s+(?:en\s+)?(journée|la\s+journée)",
        # Cours du soir
        r"(?:je\s+)?(?:préfère|choisis?|veux|souhaite)\s+(?:les?\s+)?cours\s+du\s+(soir)",
        r"cours\s+du\s+(soir)\s+(?:me\s+convient|ok|parfait|svp|s'il vous plait)",
        r"(?:après\s+le\s+travail|le\s+soir|en\s+soirée)",
        r"(?:je\s+suis\s+)?disponible\s+(?:le\s+)?(soir|en\s+soirée)",
    ],
    'session_confirmation': [
        r"(?:je\s+)?confirm[ée]?\s+(?:la\s+)?session\s+(?:du\s+)?(\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?)",
        r"(?:ok|d'accord|parfait)\s+pour\s+(?:la\s+)?session\s+(?:du\s+)?(\d{1,2}[/.\-]\d{1,2})",
        r"session\s+(?:du\s+)?(\d{1,2}[/.\-]\d{1,2})\s+(?:me\s+convient|ok|parfait)",
    ],
    'report_request': [
        r"(?:je\s+)?(?:souhaite|veux|voudrais|peux)\s+(?:décaler|reporter|changer)\s+(?:ma\s+)?(?:date|l'examen)",
        r"report(?:er)?\s+(?:mon\s+)?examen",
        r"(?:pas|ne\s+peux\s+pas|impossible)\s+(?:le|à\s+cette\s+date)",
        r"changer\s+(?:de\s+)?date",
    ],
}


def parse_date_from_match(date_str: str) -> Optional[str]:
    """
    Parse une date depuis un match regex et la convertit en format YYYY-MM-DD.

    Gère les formats:
    - DD/MM/YYYY ou DD-MM-YYYY ou DD.MM.YYYY
    - DD/MM/YY
    - DD/MM (année courante assumée)
    """
    if not date_str:
        return None

    # Nettoyer
    date_str = date_str.strip()

    # Patterns de parsing
    patterns = [
        (r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", "%d/%m/%Y"),
        (r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2})", "%d/%m/%y"),
        (r"(\d{1,2})[/.\-](\d{1,2})", None),  # Année courante
    ]

    for pattern, date_format in patterns:
        match = re.match(pattern, date_str)
        if match:
            groups = match.groups()
            try:
                if len(groups) == 3:
                    day, month, year = groups
                    if len(year) == 2:
                        year = f"20{year}"
                    date_obj = datetime(int(year), int(month), int(day))
                else:
                    day, month = groups
                    current_year = datetime.now().year
                    date_obj = datetime(current_year, int(month), int(day))
                    # Si la date est passée, on assume l'année prochaine
                    if date_obj.date() < datetime.now().date():
                        date_obj = datetime(current_year + 1, int(month), int(day))

                return date_obj.strftime("%Y-%m-%d")
            except ValueError:
                continue

    return None


# Note: _extract_date_from_option_context supprimée
# La détection de "Option 1/2" est maintenant gérée par l'IA (ResponseGeneratorAgent)


def extract_confirmations_from_threads(
    threads: List[Dict],
    deal_data: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Analyse les threads pour détecter les confirmations du candidat.

    Args:
        threads: Liste des threads du ticket
        deal_data: Données du deal (pour contexte Evalbox, date clôture)

    Returns:
        {
            'date_examen_confirmed': str or None,  # YYYY-MM-DD
            'session_preference': 'jour' or 'soir' or None,
            'session_confirmed': Dict or None,
            'report_requested': bool,
            'raw_confirmations': List[Dict],  # Détails des matchs
            'blocked_updates': List[Dict],  # Mises à jour bloquées
            'changes_to_apply': List[Dict]  # Changements CRM à appliquer
        }
    """
    from src.utils.text_utils import get_clean_thread_content

    result = {
        'date_examen_confirmed': None,
        'session_preference': None,
        'session_confirmed': None,
        'report_requested': False,
        'raw_confirmations': [],
        'blocked_updates': [],
        'changes_to_apply': []
    }

    if not threads:
        return result

    logger.info("🔍 Extraction des confirmations depuis les threads...")

    # Récupérer contexte pour règles critiques
    evalbox_status = deal_data.get('Evalbox', '') if deal_data else ''
    date_cloture = None
    date_examen_vtc = deal_data.get('Date_examen_VTC') if deal_data else None
    if date_examen_vtc and isinstance(date_examen_vtc, dict):
        date_cloture = date_examen_vtc.get('Date_Cloture_Inscription')

    # Analyser chaque thread entrant (du candidat)
    for thread in threads:
        if thread.get('direction') != 'in':
            continue

        content = get_clean_thread_content(thread).lower()
        thread_date = thread.get('createdTime', '')

        # 1. Détecter demande de report
        for pattern in CONFIRMATION_PATTERNS['report_request']:
            if re.search(pattern, content, re.IGNORECASE):
                result['report_requested'] = True
                result['raw_confirmations'].append({
                    'type': 'report_request',
                    'thread_date': thread_date,
                    'pattern_matched': pattern
                })
                logger.info(f"  📋 Demande de report détectée")
                break

        # 2. Détecter confirmation date examen (avec date explicite)
        for pattern in CONFIRMATION_PATTERNS['date_examen']:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                parsed_date = parse_date_from_match(date_str)
                if parsed_date:
                    result['raw_confirmations'].append({
                        'type': 'date_examen',
                        'raw_value': date_str,
                        'parsed_value': parsed_date,
                        'thread_date': thread_date
                    })
                    result['date_examen_confirmed'] = parsed_date
                    logger.info(f"  📅 Confirmation date examen: {parsed_date}")
                break

        # Note: "Option 1/2" est maintenant géré par l'IA (ResponseGeneratorAgent)
        # qui analyse le contexte complet et retourne les updates CRM directement

        # 3. Détecter préférence session (jour/soir)
        for pattern in CONFIRMATION_PATTERNS['session_preference']:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                # Déterminer si c'est jour ou soir
                matched_text = match.group(0).lower()
                if any(x in matched_text for x in ['jour', 'journée']):
                    result['session_preference'] = 'jour'
                elif any(x in matched_text for x in ['soir', 'soirée', 'travail']):
                    result['session_preference'] = 'soir'

                if result['session_preference']:
                    result['raw_confirmations'].append({
                        'type': 'session_preference',
                        'value': result['session_preference'],
                        'thread_date': thread_date
                    })
                    logger.info(f"  📚 Préférence session: {result['session_preference']}")
                break

        # 4. Détecter confirmation session spécifique
        for pattern in CONFIRMATION_PATTERNS['session_confirmation']:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                date_str = match.group(1)
                parsed_date = parse_date_from_match(date_str)
                if parsed_date:
                    result['raw_confirmations'].append({
                        'type': 'session_confirmation',
                        'raw_value': date_str,
                        'parsed_value': parsed_date,
                        'thread_date': thread_date
                    })
                    result['session_confirmed'] = {'date_debut': parsed_date}
                    logger.info(f"  📚 Confirmation session: {parsed_date}")
                break

    # ================================================================
    # VALIDATION DES RÈGLES CRITIQUES
    # ================================================================
    from src.utils.examt3p_crm_sync import can_modify_exam_date

    # Si confirmation de date ou demande de report
    if result['date_examen_confirmed'] or result['report_requested']:
        can_modify, reason = can_modify_exam_date(evalbox_status, date_cloture)

        if not can_modify:
            logger.warning(f"  🔒 BLOCAGE: {reason}")
            result['blocked_updates'].append({
                'field': 'Date_examen_VTC',
                'reason': reason,
                'evalbox': evalbox_status,
                'date_cloture': date_cloture,
                'action_required': 'HUMAN_REVIEW',
                'message_to_candidate': _get_blocked_update_message(evalbox_status, date_cloture)
            })
            # Ne pas ajouter aux changes_to_apply
        else:
            # Modification autorisée
            if result['date_examen_confirmed']:
                result['changes_to_apply'].append({
                    'field': 'Date_examen_VTC',
                    'value': result['date_examen_confirmed'],
                    'source': 'ticket_confirmation',
                    'requires_lookup': True  # Doit chercher l'ID de la date d'examen
                })

    # Préférence session → toujours OK à mettre à jour
    if result['session_preference']:
        result['changes_to_apply'].append({
            'field': 'Session_souhait_e',
            'value': 'Cours du jour' if result['session_preference'] == 'jour' else 'Cours du soir',
            'source': 'ticket_confirmation'
        })

    return result


def _get_blocked_update_message(evalbox_status: str, date_cloture: str) -> str:
    """
    Génère le message à envoyer au candidat quand une mise à jour est bloquée.

    IMPORTANT: Communication par EMAIL uniquement.
    """
    # Formater la date
    date_formatted = ""
    if date_cloture:
        date_obj = parse_date_flexible(str(date_cloture), "date_cloture")
        if date_obj:
            date_formatted = date_obj.strftime("%d/%m/%Y")

    return f"""Votre dossier a été validé par la CMA et les inscriptions sont clôturées.

**Un report de date d'examen n'est possible qu'avec un justificatif de force majeure.**

Pour demander un report, merci de nous transmettre **par email** :
1. Votre justificatif de force majeure (certificat médical ou autre document officiel)
2. Une brève explication de votre situation

Nous soumettrons votre demande à la CMA pour validation du report.

**Sans justificatif valide**, des frais de réinscription de {CMA_EXAM_FEE}€ seront nécessaires pour une nouvelle inscription."""


def apply_ticket_confirmations_to_crm(
    deal_id: str,
    confirmations: Dict[str, Any],
    crm_client,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Applique les confirmations extraites au CRM.

    Args:
        deal_id: ID du deal
        confirmations: Résultat de extract_confirmations_from_threads
        crm_client: Client CRM
        dry_run: Simulation sans modification

    Returns:
        {
            'updates_applied': List[Dict],
            'updates_blocked': List[Dict],
            'crm_updated': bool
        }
    """
    result = {
        'updates_applied': [],
        'updates_blocked': confirmations.get('blocked_updates', []),
        'crm_updated': False
    }

    changes = confirmations.get('changes_to_apply', [])
    if not changes:
        logger.info("  ℹ️ Aucun changement à appliquer depuis le ticket")
        return result

    updates_to_apply = {}

    for change in changes:
        field = change['field']
        value = change['value']

        # Cas spécial: Date_examen_VTC nécessite un lookup
        if field == 'Date_examen_VTC' and change.get('requires_lookup'):
            # TODO: Implémenter la recherche de l'ID de la date d'examen
            # Pour l'instant, on log et on skip
            logger.info(f"  ⚠️ Date_examen_VTC nécessite recherche lookup - non implémenté")
            continue

        updates_to_apply[field] = value
        result['updates_applied'].append(change)

    if updates_to_apply and not dry_run:
        try:
            from config import settings
            url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}"
            payload = {"data": [updates_to_apply]}

            response = crm_client._make_request("PUT", url, json=payload)

            if response.get('data'):
                result['crm_updated'] = True
                logger.info(f"  ✅ CRM mis à jour depuis ticket: {list(updates_to_apply.keys())}")
            else:
                logger.error(f"  ❌ Échec mise à jour CRM: {response}")

        except Exception as e:
            logger.error(f"  ❌ Erreur mise à jour CRM: {e}")

    elif updates_to_apply and dry_run:
        logger.info(f"  🔍 DRY RUN: {list(updates_to_apply.keys())}")

    return result


def extract_cab_proposals_from_threads(threads: List[Dict]) -> Dict[str, Any]:
    """
    Detecte si CAB a deja propose des dates d'examen dans les messages precedents.

    Cette fonction permet d'eviter de repeter les memes dates dans les reponses
    si elles ont deja ete communiquees recemment.

    Args:
        threads: Liste des threads du ticket

    Returns:
        {
            'dates_already_proposed': List[str],  # Liste des dates proposees (DD/MM/YYYY)
            'dates_proposed_recently': bool,  # True si proposees dans les derniers 48h
            'proposal_count': int  # Nombre de fois que des dates ont ete proposees
        }
    """
    from datetime import timedelta
    from src.utils.text_utils import get_clean_thread_content

    result = {
        'dates_already_proposed': [],
        'dates_proposed_recently': False,
        'sessions_proposed_recently': False,
        'proposal_count': 0
    }

    if not threads:
        return result

    logger.info("🔍 Detection des dates deja proposees par CAB...")

    # Marqueurs de proposition de dates dans les reponses CAB
    proposal_markers = [
        "prochaines dates d'examen",
        "prochaines dates disponibles",
        "dates disponibles",
        "voici les dates",
        "merci de nous confirmer la date",
        "date qui vous convient",
        "option 1",
        "option 2",
    ]

    # Pattern pour extraire les dates format DD/MM/YYYY
    date_pattern = r"(\d{1,2}/\d{1,2}/\d{4})"

    found_dates = set()
    now = datetime.now()
    recent_threshold = now - timedelta(hours=48)

    for thread in threads:
        # Seulement les messages sortants (CAB -> candidat) qui ont été envoyés (pas les drafts)
        if thread.get('direction') != 'out':
            continue
        if thread.get('status') == 'DRAFT':
            continue

        # Recuperer le contenu du thread
        content = get_clean_thread_content(thread).lower()
        thread_date_str = thread.get('createdTime', '')

        # Verifier si c'est un message de proposition de dates
        is_proposal = any(marker in content for marker in proposal_markers)

        if not is_proposal:
            continue

        result['proposal_count'] += 1

        # Parser la date du thread
        thread_date = None
        if thread_date_str:
            thread_date = parse_datetime_flexible(thread_date_str, "thread_date")
            if thread_date:
                thread_date = thread_date.replace(tzinfo=None)

        # Verifier si recent (< 48h)
        if thread_date and thread_date > recent_threshold:
            result['dates_proposed_recently'] = True

        # Extraire les dates mentionnees (format DD/MM/YYYY)
        dates_found = re.findall(date_pattern, content)
        found_dates.update(dates_found)

    result['dates_already_proposed'] = list(found_dates)

    if result['dates_already_proposed']:
        logger.info(f"  📋 {len(result['dates_already_proposed'])} date(s) deja proposee(s)")
        if result['dates_proposed_recently']:
            logger.info(f"  ⏰ Dates proposees recemment (< 48h)")

    # Detection des sessions deja proposees
    session_markers = [
        "cours du jour",
        "cours du soir",
        "session de formation",
        "sessions disponibles",
        "merci de nous confirmer votre choix de session",
        "confirmer la session",
    ]

    for thread in threads:
        if thread.get('direction') != 'out':
            continue
        if thread.get('status') == 'DRAFT':
            continue

        content = get_clean_thread_content(thread).lower()
        thread_date_str = thread.get('createdTime', '')

        # Verifier si sessions proposees
        has_sessions = any(marker in content for marker in session_markers)
        if not has_sessions:
            continue

        # Parser la date du thread
        thread_date = None
        if thread_date_str:
            thread_date = parse_datetime_flexible(thread_date_str, "thread_date")
            if thread_date:
                thread_date = thread_date.replace(tzinfo=None)

        # Verifier si recent (< 48h)
        if thread_date and thread_date > recent_threshold:
            result['sessions_proposed_recently'] = True
            logger.info("  📚 Sessions proposees recemment (< 48h)")
            break

    # NOUVEAU: Extraire la derniere date d'examen mentionnee par CAB
    for thread in reversed(threads):
        if thread.get('direction') != 'out':
            continue
        if thread.get('status') == 'DRAFT':
            continue
        content = get_clean_thread_content(thread)

        # Chercher pattern "examen du DD/MM/YYYY" ou "examen le DD/MM/YYYY"
        date_match = re.search(r'examen[^\d]*(\d{1,2}/\d{1,2}/\d{4})', content, re.IGNORECASE)
        if date_match and not result.get('last_proposed_exam_date'):
            result['last_proposed_exam_date'] = date_match.group(1)
            break

    return result


def detect_candidate_references(thread_content: str) -> Dict[str, Any]:
    """
    Detecte si le candidat fait reference a une communication precedente.

    Permet de distinguer:
    - Une demande directe (request)
    - Une demande de clarification (clarification) - "vous m'aviez dit X mais..."
    - Une verification (verification) - "donc si j'ai bien compris..."
    - Un suivi (follow_up) - "suite a votre mail..."

    Args:
        thread_content: Contenu du message du candidat

    Returns:
        {
            'references_previous_communication': bool,
            'mentions_discrepancy': bool,
            'communication_mode': str,  # 'request' | 'clarification' | 'verification' | 'follow_up'
            'discrepancy_details': str | None
        }
    """
    content_lower = thread_content.lower()

    # Patterns de reference a communication precedente
    reference_patterns = [
        r"vous m'?a(vez|viez) dit",
        r"vous m'?a(vez|viez) envoye",
        r"vous m'?a(vez|viez) indique",
        r"vous m'?a(vez|viez) propose",
        r"dans (votre|le) (dernier |precedent )?mail",
        r"dans (votre|le) (dernier |precedent )?message",
        r"suite [aà] votre (mail|message|reponse)",
        r"comme (convenu|indique|mentionne)",
        r"on m'a dit que",
        r"j'ai recu un mail",
        r"selon votre (mail|message)",
    ]

    # Patterns d'incoherence/discordance
    discrepancy_patterns = [
        r"mais (je vois|il y a|c'est|j'ai)",
        r"pourtant",
        r"par contre",
        r"c'est (different|pas pareil|contradictoire)",
        r"(annule|change|modifie)\s*\?",
        r"c'est (encore|toujours) (valable|d'actualite|valide)",
        r"est[- ]ce (que c'est )?toujours",
        r"on maintient",
        r"c'est confirme",
    ]

    # Patterns de verification
    verification_patterns = [
        r"(donc|alors) si j'ai bien compris",
        r"pour (confirmer|verifier|etre sur)",
        r"c'est bien [cç]a",
        r"c'est correct",
        r"j'ai bien compris",
        r"est[- ]ce (bien |correct )?que",
    ]

    references_previous = any(re.search(p, content_lower) for p in reference_patterns)
    mentions_discrepancy = any(re.search(p, content_lower) for p in discrepancy_patterns)
    is_verification = any(re.search(p, content_lower) for p in verification_patterns)

    # Determiner le mode de communication
    communication_mode = _infer_communication_mode(
        references_previous,
        mentions_discrepancy,
        is_verification
    )

    # Extraire les details de la discordance si presente
    discrepancy_details = None
    if mentions_discrepancy:
        # Essayer d'extraire le contexte
        for pattern in discrepancy_patterns:
            match = re.search(f"(.{{0,50}}{pattern}.{{0,50}})", content_lower)
            if match:
                discrepancy_details = match.group(1).strip()
                break

    result = {
        'references_previous_communication': references_previous,
        'mentions_discrepancy': mentions_discrepancy,
        'is_verification': is_verification,
        'communication_mode': communication_mode,
        'discrepancy_details': discrepancy_details
    }

    if references_previous or mentions_discrepancy:
        logger.info(f"  📝 Communication mode: {communication_mode}")
        if references_previous:
            logger.info(f"     → Reference a communication precedente detectee")
        if mentions_discrepancy:
            logger.info(f"     → Discordance/question detectee: {discrepancy_details[:50] if discrepancy_details else 'N/A'}...")

    return result


def detect_dossier_completion_request(threads: List[Dict]) -> Dict[str, Any]:
    """
    Détecte si CAB a déjà demandé au candidat de compléter son dossier ExamT3P.

    Utilisé pour distinguer :
    - Première communication → "Nous nous occupons de votre dossier"
    - Demande déjà faite → "Merci de finaliser pour qu'on prenne le relais"

    Args:
        threads: Liste des threads du ticket

    Returns:
        {
            'previously_asked_to_complete': bool,
            'completion_request_date': str | None  # Date de la demande
        }
    """
    from src.utils.text_utils import get_clean_thread_content

    result = {
        'previously_asked_to_complete': False,
        'completion_request_date': None
    }

    if not threads:
        return result

    # Marqueurs de demande de complétion de dossier
    completion_markers = [
        "compléter votre dossier",
        "completer votre dossier",
        "télécharger vos documents",
        "telecharger vos documents",
        "connectez-vous sur exament3p",
        "connectez vous sur exament3p",
        "finaliser votre dossier",
        "valider votre dossier",
        "compléter vos informations",
        "completer vos informations",
        "→ identifiant :",  # On leur a donné les identifiants pour qu'ils complètent
    ]

    for thread in threads:
        # Seulement les messages sortants (CAB -> candidat) qui ont été envoyés (pas les drafts)
        if thread.get('direction') != 'out':
            continue
        if thread.get('status') == 'DRAFT':
            continue

        content = get_clean_thread_content(thread).lower()

        # Vérifier si c'est une demande de complétion
        if any(marker in content for marker in completion_markers):
            result['previously_asked_to_complete'] = True
            result['completion_request_date'] = thread.get('createdTime', '')[:10] if thread.get('createdTime') else None
            logger.info(f"  📋 Demande de complétion dossier détectée (date: {result['completion_request_date']})")
            break  # On prend la plus récente (threads sont triés)

    return result


def _infer_communication_mode(
    references_previous: bool,
    mentions_discrepancy: bool,
    is_verification: bool
) -> str:
    """
    Determine le mode de communication du candidat.

    Args:
        references_previous: Le candidat mentionne une comm precedente
        mentions_discrepancy: Le candidat note une incoherence
        is_verification: Le candidat verifie sa comprehension

    Returns:
        'clarification' | 'verification' | 'follow_up' | 'request'
    """
    if mentions_discrepancy:
        return 'clarification'
    if is_verification:
        return 'verification'
    if references_previous:
        return 'follow_up'
    return 'request'
