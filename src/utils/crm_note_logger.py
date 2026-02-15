"""
Logger de notes CRM pour traçabilité des actions.

Ce helper crée des notes structurées dans Zoho CRM pour garder
un track record de toutes les actions effectuées sur un deal.

TYPES DE NOTES:
===============

1. SYNC_EXAMT3P - Synchronisation ExamT3P → CRM
   - Changements de statut Evalbox
   - Mise à jour des identifiants
   - Blocages rencontrés

2. TICKET_UPDATE - Mise à jour depuis un ticket
   - Confirmations détectées du candidat
   - Changements CRM appliqués
   - Blocages (règles critiques)

3. RESPONSE_SENT - Réponse envoyée au candidat
   - ID du ticket
   - Résumé de la réponse
   - Cas traité

4. EXAM_DATE_BLOCKED - Tentative de modification bloquée
   - Raison du blocage
   - Statut Evalbox
   - Date de clôture

FORMAT DES NOTES:
=================
📊 [TYPE] - DD/MM/YYYY HH:MM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[Contenu structuré]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# Emojis par type de note
NOTE_TYPE_EMOJIS = {
    'SYNC_EXAMT3P': '🔄',
    'TICKET_UPDATE': '📥',
    'RESPONSE_SENT': '📤',
    'EXAM_DATE_BLOCKED': '🔒',
    'CRM_UPDATE': '✏️',
    'UBER_ELIGIBILITY': '🚗',
    'SESSION_LINKED': '📚',
}


def create_crm_note(
    deal_id: str,
    crm_client,
    note_type: str,
    content_lines: List[str],
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Crée une note structurée dans le CRM Zoho.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        note_type: Type de note (SYNC_EXAMT3P, TICKET_UPDATE, etc.)
        content_lines: Lignes de contenu de la note
        dry_run: Si True, ne crée pas la note (simulation)

    Returns:
        {
            'success': bool,
            'note_id': str or None,
            'note_content': str
        }
    """
    emoji = NOTE_TYPE_EMOJIS.get(note_type, '📝')
    timestamp = datetime.now().strftime('%d/%m/%Y %H:%M')

    # Construire la note formatée
    note_lines = [
        f"{emoji} {note_type} - {timestamp}",
        "━" * 30,
        *content_lines,
        "━" * 30,
    ]
    note_content = "\n".join(note_lines)

    result = {
        'success': False,
        'note_id': None,
        'note_content': note_content
    }

    if dry_run:
        logger.info(f"  🔍 DRY RUN: Note {note_type} non créée")
        logger.debug(f"  Contenu:\n{note_content}")
        return result

    try:
        from config import settings
        url = f"{settings.zoho_crm_api_url}/Notes"
        payload = {
            "data": [{
                "Note_Title": f"{emoji} {note_type}",
                "Note_Content": note_content,
                "Parent_Id": deal_id,
                "se_module": "Deals"
            }]
        }

        response = crm_client._make_request("POST", url, json=payload)

        if response.get('data'):
            note_id = response['data'][0].get('details', {}).get('id')
            result['success'] = True
            result['note_id'] = note_id
            logger.info(f"  ✅ Note {note_type} créée: {note_id}")
        else:
            logger.error(f"  ❌ Échec création note: {response}")

    except Exception as e:
        logger.error(f"  ❌ Erreur création note: {e}")

    return result


def log_examt3p_sync(
    deal_id: str,
    crm_client,
    sync_result: Dict[str, Any],
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Log une synchronisation ExamT3P → CRM.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        sync_result: Résultat de sync_examt3p_to_crm()
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    if not sync_result.get('sync_performed'):
        return {'success': True, 'note_id': None, 'note_content': ''}

    content_lines = []

    # Changements appliqués
    if sync_result.get('changes_made'):
        content_lines.append("✅ CHANGEMENTS APPLIQUÉS:")
        for change in sync_result['changes_made']:
            field = change['field']
            old_val = change.get('old_value', '')
            new_val = change.get('new_value', '')
            # Masquer les mots de passe
            if 'MDP' in field or 'password' in field.lower():
                old_val = '***' if old_val else ''
                new_val = '***'
            content_lines.append(f"  • {field}: '{old_val}' → '{new_val}'")

    # Changements bloqués
    if sync_result.get('blocked_changes'):
        content_lines.append("")
        content_lines.append("🔒 CHANGEMENTS BLOQUÉS:")
        for blocked in sync_result['blocked_changes']:
            content_lines.append(f"  • {blocked['field']}")
            content_lines.append(f"    Raison: {blocked['reason']}")

    if not content_lines:
        content_lines.append("ℹ️ Aucun changement détecté")

    return create_crm_note(deal_id, crm_client, 'SYNC_EXAMT3P', content_lines, dry_run)


def log_ticket_update(
    deal_id: str,
    crm_client,
    ticket_id: str,
    confirmations: Dict[str, Any],
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Log une mise à jour depuis un ticket.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        ticket_id: ID du ticket Zoho Desk
        confirmations: Résultat de extract_confirmations_from_threads()
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    content_lines = [f"Ticket: #{ticket_id}"]

    # Confirmations détectées
    raw_confirmations = confirmations.get('raw_confirmations', [])
    if raw_confirmations:
        content_lines.append("")
        content_lines.append("📋 CONFIRMATIONS DÉTECTÉES:")
        for conf in raw_confirmations:
            conf_type = conf.get('type', '')
            if conf_type == 'date_examen':
                content_lines.append(f"  • Date examen: {conf.get('parsed_value', 'N/A')}")
            elif conf_type == 'session_preference':
                content_lines.append(f"  • Préférence session: {conf.get('value', 'N/A')}")
            elif conf_type == 'session_confirmation':
                content_lines.append(f"  • Session confirmée: {conf.get('parsed_value', 'N/A')}")
            elif conf_type == 'report_request':
                content_lines.append("  • Demande de report détectée")

    # Changements appliqués
    changes_to_apply = confirmations.get('changes_to_apply', [])
    if changes_to_apply:
        content_lines.append("")
        content_lines.append("✅ CHANGEMENTS CRM:")
        for change in changes_to_apply:
            content_lines.append(f"  • {change['field']} → '{change['value']}'")

    # Mises à jour bloquées
    blocked_updates = confirmations.get('blocked_updates', [])
    if blocked_updates:
        content_lines.append("")
        content_lines.append("🔒 MISES À JOUR BLOQUÉES:")
        for blocked in blocked_updates:
            content_lines.append(f"  • {blocked['field']}")
            content_lines.append(f"    Raison: {blocked['reason']}")
            content_lines.append(f"    → Action humaine requise")

    if not raw_confirmations and not changes_to_apply and not blocked_updates:
        content_lines.append("")
        content_lines.append("ℹ️ Aucune confirmation détectée dans le ticket")

    return create_crm_note(deal_id, crm_client, 'TICKET_UPDATE', content_lines, dry_run)


def log_response_sent(
    deal_id: str,
    crm_client,
    ticket_id: str,
    response_summary: str,
    case_handled: Optional[str] = None,
    uber_case: Optional[str] = None,
    evalbox_status: Optional[str] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Log une réponse envoyée au candidat.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        ticket_id: ID du ticket Zoho Desk
        response_summary: Résumé de la réponse (max 200 car)
        case_handled: Cas Date_examen_VTC traité (1-10)
        uber_case: Cas Uber traité (A, B, ELIGIBLE)
        evalbox_status: Statut Evalbox au moment de la réponse
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    content_lines = [f"Ticket: #{ticket_id}"]

    if evalbox_status:
        content_lines.append(f"Evalbox: {evalbox_status}")

    if case_handled:
        content_lines.append(f"Cas Date_examen_VTC: {case_handled}")

    if uber_case:
        content_lines.append(f"Cas Uber 20€: {uber_case}")

    content_lines.append("")
    content_lines.append("📝 RÉSUMÉ RÉPONSE:")

    # Tronquer le résumé si trop long
    if len(response_summary) > 200:
        response_summary = response_summary[:197] + "..."
    content_lines.append(response_summary)

    return create_crm_note(deal_id, crm_client, 'RESPONSE_SENT', content_lines, dry_run)


def log_exam_date_blocked(
    deal_id: str,
    crm_client,
    evalbox_status: str,
    date_cloture: str,
    requested_action: str,
    ticket_id: Optional[str] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Log une tentative de modification de date bloquée.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        evalbox_status: Statut Evalbox
        date_cloture: Date de clôture des inscriptions
        requested_action: Action demandée (report, modification)
        ticket_id: ID du ticket si applicable
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    # Formater la date de clôture
    from src.utils.date_utils import format_date_for_display
    date_formatted = format_date_for_display(date_cloture) or str(date_cloture or '')

    content_lines = [
        "⚠️ TENTATIVE DE MODIFICATION BLOQUÉE",
        "",
        f"Action demandée: {requested_action}",
        f"Evalbox: {evalbox_status}",
        f"Date clôture: {date_formatted} (passée)",
        "",
        "RÈGLE CRITIQUE APPLIQUÉE:",
        "• Evalbox = VALIDE CMA ou Convoc CMA reçue",
        "• + Date de clôture passée",
        "• → Modification automatique INTERDITE",
        "",
        "ACTION REQUISE:",
        "• Demander justificatif de force majeure par EMAIL",
        "• OU frais de réinscription 241€",
    ]

    if ticket_id:
        content_lines.insert(0, f"Ticket: #{ticket_id}")

    return create_crm_note(deal_id, crm_client, 'EXAM_DATE_BLOCKED', content_lines, dry_run)


def log_uber_eligibility_check(
    deal_id: str,
    crm_client,
    eligibility_result: Dict[str, Any],
    ticket_id: Optional[str] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Log une vérification d'éligibilité Uber 20€.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        eligibility_result: Résultat de analyze_uber_eligibility()
        ticket_id: ID du ticket si applicable
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    if not eligibility_result.get('is_uber_20_deal'):
        # Pas un deal Uber, pas de note
        return {'success': True, 'note_id': None, 'note_content': ''}

    case = eligibility_result.get('case', 'N/A')
    case_description = eligibility_result.get('case_description', '')

    content_lines = []

    if ticket_id:
        content_lines.append(f"Ticket: #{ticket_id}")

    content_lines.extend([
        "Opportunité Uber 20€ détectée",
        "",
        f"CAS: {case}",
        f"Description: {case_description}",
        "",
    ])

    if case == 'A':
        content_lines.extend([
            "📋 ÉTAPES MANQUANTES:",
            "  1. Finaliser inscription sur plateforme",
            "  2. Envoyer documents",
            "  3. Passer test de sélection",
        ])
    elif case == 'B':
        date_dossier = eligibility_result.get('date_dossier_recu', 'N/A')
        content_lines.extend([
            f"Date dossier reçu: {date_dossier}",
            "",
            "📋 ÉTAPE MANQUANTE:",
            "  • Passer le test de sélection",
            "  • Email envoyé le jour du dossier reçu",
        ])
    elif case == 'ELIGIBLE':
        content_lines.extend([
            "✅ ÉLIGIBLE",
            "Candidat peut être inscrit à l'examen",
        ])

    return create_crm_note(deal_id, crm_client, 'UBER_ELIGIBILITY', content_lines, dry_run)


def log_session_linked(
    deal_id: str,
    crm_client,
    session_data: Dict[str, Any],
    exam_date: str,
    ticket_id: Optional[str] = None,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Log une liaison session de formation → date d'examen.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        session_data: Données de la session liée
        exam_date: Date d'examen associée
        ticket_id: ID du ticket si applicable
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    content_lines = []

    if ticket_id:
        content_lines.append(f"Ticket: #{ticket_id}")

    session_name = session_data.get('Name', 'N/A')
    session_date = session_data.get('Date_de_d_but', 'N/A')
    session_type = 'Cours du jour' if 'CDJ' in session_name else 'Cours du soir' if 'CDS' in session_name else 'N/A'

    # Formater les dates
    from src.utils.date_utils import format_date_for_display
    exam_date = format_date_for_display(exam_date) or str(exam_date or 'N/A')
    session_date = format_date_for_display(session_date) or str(session_date or 'N/A')

    content_lines.extend([
        "Session de formation liée à l'examen",
        "",
        f"📅 Date examen: {exam_date}",
        f"📚 Session: {session_name}",
        f"   Début: {session_date}",
        f"   Type: {session_type}",
    ])

    return create_crm_note(deal_id, crm_client, 'SESSION_LINKED', content_lines, dry_run)


def create_summary_note(
    deal_id: str,
    crm_client,
    ticket_id: str,
    actions_performed: List[str],
    response_sent: bool = False,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Crée une note récapitulative de toutes les actions d'un traitement de ticket.

    Args:
        deal_id: ID du deal
        crm_client: Client CRM Zoho
        ticket_id: ID du ticket traité
        actions_performed: Liste des actions effectuées
        response_sent: Si une réponse a été envoyée
        dry_run: Si True, ne crée pas la note

    Returns:
        Résultat de create_crm_note()
    """
    content_lines = [
        f"Ticket #{ticket_id} traité",
        "",
        "📋 ACTIONS EFFECTUÉES:",
    ]

    for i, action in enumerate(actions_performed, 1):
        content_lines.append(f"  {i}. {action}")

    if response_sent:
        content_lines.append("")
        content_lines.append("✉️ Réponse envoyée au candidat")

    return create_crm_note(deal_id, crm_client, 'CRM_UPDATE', content_lines, dry_run)
