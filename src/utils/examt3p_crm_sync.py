"""
Synchronisation ExamT3P → Zoho CRM.

Ce helper synchronise les données extraites d'ExamT3P vers le CRM Zoho.
ExamT3P est la SOURCE DE VÉRITÉ pour le statut du dossier candidat.

RÈGLES CRITIQUES DE MODIFICATION:
=================================

1. JAMAIS MODIFIER Date_examen_VTC automatiquement SI:
   - Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"}
   - ET Date_Cloture_Inscription < aujourd'hui (passée)
   → Seul un humain peut traiter (report avec justif ou repayer)

2. Report POSSIBLE automatiquement SI:
   - Date_Cloture_Inscription >= aujourd'hui (pas encore passée)
   → La CMA accepte les reports avant clôture

3. CAS Refusé CMA + Clôture passée:
   - Le candidat sera décalé sur la prochaine session automatiquement
   - SEULEMENT s'il corrige avant la clôture de la nouvelle session

MAPPING EXAMT3P → CRM (Statut du Dossier):
==========================================
- "En cours de composition"     → Evalbox = "Dossier crée"
- "En attente de paiement"      → Evalbox = "Pret a payer"
- "En cours d'instruction"      → Evalbox = "Dossier Synchronisé"
- "Incomplet"                   → Evalbox = "Refusé CMA"
- "Valide"                      → Evalbox = "VALIDE CMA"
- "En attente de convocation"   → Evalbox = "Convoc CMA reçue"

NOTE: "Documents manquants" et "Documents refusés" sont utilisés
      AVANT la création du compte ExamT3P (gestion interne CAB).

Autres champs synchronisés:
- identifiant                   → IDENTIFIANT_EVALBOX (si vide)
- mot_de_passe                  → MDP_EVALBOX (si vide)
- departement                   → CMA_de_depot (si vide ou différent)
- num_dossier                   → NUM_DOSSIER_EVALBOX (numéro de dossier CMA)
- date_examen + departement     → Date_examen_VTC (lookup vers session CRM)
"""
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Tuple

from src.constants.evalbox import BLOCKING_MODIFICATION
from src.constants.amounts import CMA_EXAM_FEE
from src.utils.date_utils import parse_date_flexible, format_date_for_display

logger = logging.getLogger(__name__)

# Mapping ExamT3P "Statut du Dossier" → Evalbox CRM
# Basé sur les valeurs réelles de la plateforme ExamT3P
EXAMT3P_STATUT_DOSSIER_MAPPING = {
    # Statut exact ExamT3P → Evalbox CRM
    'En cours de composition': 'Dossier crée',
    'EN COURS DE COMPOSITION': 'Dossier crée',
    'En attente de paiement': 'Pret a payer',
    'EN ATTENTE DE PAIEMENT': 'Pret a payer',
    "En cours d'instruction": 'Dossier Synchronisé',
    "EN COURS D'INSTRUCTION": 'Dossier Synchronisé',
    'Incomplet': 'Refusé CMA',
    'INCOMPLET': 'Refusé CMA',
    'Valide': 'VALIDE CMA',
    'VALIDE': 'VALIDE CMA',
    'En attente de convocation': 'Convoc CMA reçue',
    'EN ATTENTE DE CONVOCATION': 'Convoc CMA reçue',
}

# Statuts qui bloquent la modification de Date_examen_VTC
BLOCKING_EVALBOX_STATUSES = BLOCKING_MODIFICATION


def is_date_past(date_str: str) -> bool:
    """Vérifie si une date est dans le passé."""
    if not date_str:
        return False
    try:
        if 'T' in str(date_str):
            date_obj = datetime.fromisoformat(str(date_str).replace('Z', '+00:00'))
            date_obj = date_obj.replace(tzinfo=None)
        else:
            date_obj = datetime.strptime(str(date_str), "%Y-%m-%d")
        return date_obj.date() < datetime.now().date()
    except Exception as e:
        return False


def can_modify_exam_date(evalbox_status: str, date_cloture: str) -> Tuple[bool, str]:
    """
    Vérifie si on peut modifier la date d'examen automatiquement.

    RÈGLE CRITIQUE:
    - Si Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"} ET clôture passée
    - → JAMAIS modifier automatiquement

    Returns:
        (can_modify: bool, reason: str)
    """
    if evalbox_status in BLOCKING_EVALBOX_STATUSES:
        if is_date_past(date_cloture):
            return False, (
                f"BLOCAGE: Evalbox={evalbox_status} + clôture passée. "
                "Report uniquement avec justificatif de force majeure. "
                "Action humaine requise."
            )
        else:
            # Clôture pas encore passée, modification possible
            return True, "Clôture future, modification autorisée"

    return True, "Statut permet la modification"


def determine_evalbox_from_examt3p(examt3p_data: Dict[str, Any]) -> Optional[str]:
    """
    Détermine la valeur Evalbox à partir des données ExamT3P.

    Utilise le champ "Statut du Dossier" (statut_dossier ou statut_principal)
    de la plateforme ExamT3P pour déterminer la valeur Evalbox CRM.

    Mapping:
    - "En cours de composition"     → "Dossier crée"
    - "En attente de paiement"      → "Pret a payer"
    - "En cours d'instruction"      → "Dossier Synchronisé"
    - "Incomplet"                   → "Refusé CMA"
    - "Valide"                      → "VALIDE CMA"
    - "En attente de convocation"   → "Convoc CMA reçue"

    Returns:
        Valeur Evalbox ou None si pas de mapping trouvé
    """
    if not examt3p_data:
        return None

    # Récupérer le "Statut du Dossier" de ExamT3P
    # Le champ peut s'appeler statut_dossier ou statut_principal selon l'extraction
    statut_dossier = (
        examt3p_data.get('statut_dossier') or
        examt3p_data.get('statut_principal') or
        ''
    ).strip()

    if not statut_dossier:
        logger.warning("  ⚠️ Pas de statut_dossier dans les données ExamT3P")
        return None

    # Chercher le mapping exact
    for examt3p_value, evalbox_value in EXAMT3P_STATUT_DOSSIER_MAPPING.items():
        if statut_dossier.lower() == examt3p_value.lower():
            logger.info(f"  📊 Mapping ExamT3P '{statut_dossier}' → Evalbox '{evalbox_value}'")
            return evalbox_value

    # Chercher une correspondance partielle (au cas où)
    statut_lower = statut_dossier.lower()
    if 'composition' in statut_lower:
        logger.info(f"  📊 Mapping partiel '{statut_dossier}' → Evalbox 'Dossier crée'")
        return 'Dossier crée'
    elif 'paiement' in statut_lower:
        logger.info(f"  📊 Mapping partiel '{statut_dossier}' → Evalbox 'Pret a payer'")
        return 'Pret a payer'
    elif 'instruction' in statut_lower:
        logger.info(f"  📊 Mapping partiel '{statut_dossier}' → Evalbox 'Dossier Synchronisé'")
        return 'Dossier Synchronisé'
    elif 'incomplet' in statut_lower:
        logger.info(f"  📊 Mapping partiel '{statut_dossier}' → Evalbox 'Refusé CMA'")
        return 'Refusé CMA'
    elif 'valide' in statut_lower and 'convocation' not in statut_lower:
        logger.info(f"  📊 Mapping partiel '{statut_dossier}' → Evalbox 'VALIDE CMA'")
        return 'VALIDE CMA'
    elif 'convocation' in statut_lower:
        logger.info(f"  📊 Mapping partiel '{statut_dossier}' → Evalbox 'Convoc CMA reçue'")
        return 'Convoc CMA reçue'

    logger.warning(f"  ⚠️ Statut ExamT3P non reconnu: '{statut_dossier}'")
    return None


def sync_examt3p_to_crm(
    deal_id: str,
    deal_data: Dict[str, Any],
    examt3p_data: Dict[str, Any],
    crm_client,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Synchronise les données ExamT3P vers le CRM Zoho.

    Args:
        deal_id: ID du deal CRM
        deal_data: Données actuelles du deal
        examt3p_data: Données extraites d'ExamT3P
        crm_client: Client CRM Zoho
        dry_run: Si True, ne fait pas les mises à jour (simulation)

    Returns:
        {
            'sync_performed': bool,
            'changes_made': List[Dict],  # Liste des changements
            'blocked_changes': List[Dict],  # Changements bloqués par règles critiques
            'crm_updated': bool,
            'note_content': str  # Contenu pour note CRM
        }
    """
    logger.info(f"🔄 Synchronisation ExamT3P → CRM pour deal {deal_id}")

    result = {
        'sync_performed': False,
        'changes_made': [],
        'blocked_changes': [],
        'crm_updated': False,
        'note_content': ''
    }

    if not examt3p_data or not examt3p_data.get('compte_existe'):
        logger.info("  ℹ️ Pas de données ExamT3P à synchroniser")
        return result

    updates_to_apply = {}
    current_evalbox = deal_data.get('Evalbox', '')
    current_date_cloture = None

    # Récupérer la date de clôture si on a une date d'examen
    date_examen_vtc = deal_data.get('Date_examen_VTC')
    if date_examen_vtc and isinstance(date_examen_vtc, dict):
        current_date_cloture = date_examen_vtc.get('Date_Cloture_Inscription')

    # ================================================================
    # 1. SYNCHRONISATION EVALBOX
    # ================================================================
    new_evalbox = determine_evalbox_from_examt3p(examt3p_data)

    if new_evalbox and new_evalbox != current_evalbox:
        logger.info(f"  📊 Evalbox: '{current_evalbox}' → '{new_evalbox}'")
        updates_to_apply['Evalbox'] = new_evalbox
        result['changes_made'].append({
            'field': 'Evalbox',
            'old_value': current_evalbox,
            'new_value': new_evalbox,
            'source': 'examt3p'
        })

    # ================================================================
    # 2. SYNCHRONISATION IDENTIFIANTS (si vides dans CRM)
    # ================================================================
    crm_identifiant = deal_data.get('IDENTIFIANT_EVALBOX', '')
    crm_password = deal_data.get('MDP_EVALBOX', '')

    examt3p_identifiant = examt3p_data.get('identifiant', '')
    examt3p_password = examt3p_data.get('mot_de_passe', '')

    if not crm_identifiant and examt3p_identifiant:
        logger.info(f"  🔑 IDENTIFIANT_EVALBOX: vide → '{examt3p_identifiant}'")
        updates_to_apply['IDENTIFIANT_EVALBOX'] = examt3p_identifiant
        result['changes_made'].append({
            'field': 'IDENTIFIANT_EVALBOX',
            'old_value': '',
            'new_value': examt3p_identifiant,
            'source': 'examt3p'
        })

    if not crm_password and examt3p_password:
        logger.info(f"  🔑 MDP_EVALBOX: vide → '***'")
        updates_to_apply['MDP_EVALBOX'] = examt3p_password
        result['changes_made'].append({
            'field': 'MDP_EVALBOX',
            'old_value': '',
            'new_value': '***',  # Masqué pour le log
            'source': 'examt3p'
        })

    # ================================================================
    # 3. SYNCHRONISATION CMA_de_depot (département)
    # ================================================================
    crm_cma_depot = deal_data.get('CMA_de_depot', '')
    examt3p_departement = examt3p_data.get('departement', '')

    if examt3p_departement:
        # Formater le département pour le CRM (format: numéro simple ou "CMA XX")
        # On vérifie si le département ExamT3P est différent de celui du CRM
        import re
        crm_dept_num = None
        if crm_cma_depot:
            match = re.search(r'\b(\d{2,3})\b', str(crm_cma_depot))
            if match:
                crm_dept_num = match.group(1)

        examt3p_dept_num = None
        match = re.search(r'\b(\d{2,3})\b', str(examt3p_departement))
        if match:
            examt3p_dept_num = match.group(1)

        # Mettre à jour si vide OU si différent
        if examt3p_dept_num and (not crm_cma_depot or crm_dept_num != examt3p_dept_num):
            # Utiliser le même format que le CRM s'il existe, sinon juste le numéro
            new_cma_depot = examt3p_dept_num
            logger.info(f"  📍 CMA_de_depot: '{crm_cma_depot or 'VIDE'}' → '{new_cma_depot}'")
            updates_to_apply['CMA_de_depot'] = new_cma_depot
            result['changes_made'].append({
                'field': 'CMA_de_depot',
                'old_value': crm_cma_depot or '',
                'new_value': new_cma_depot,
                'source': 'examt3p'
            })

    # ================================================================
    # 4. SYNCHRONISATION NUM_DOSSIER_EVALBOX (numéro de dossier)
    # ================================================================
    crm_num_dossier = deal_data.get('NUM_DOSSIER_EVALBOX', '')
    examt3p_num_dossier = examt3p_data.get('num_dossier', '')

    if examt3p_num_dossier and str(examt3p_num_dossier) != str(crm_num_dossier):
        logger.info(f"  📋 NUM_DOSSIER_EVALBOX: '{crm_num_dossier or 'VIDE'}' → '{examt3p_num_dossier}'")
        updates_to_apply['NUM_DOSSIER_EVALBOX'] = str(examt3p_num_dossier)
        result['changes_made'].append({
            'field': 'NUM_DOSSIER_EVALBOX',
            'old_value': crm_num_dossier or '',
            'new_value': str(examt3p_num_dossier),
            'source': 'examt3p'
        })

    # ================================================================
    # 6. VÉRIFICATION RÈGLES CRITIQUES POUR DATE EXAMEN
    # ================================================================
    # Note: La modification de Date_examen_VTC est faite par sync_exam_date_from_examt3p()
    # qui est appelée séparément dans le workflow. On vérifie ici si on est dans un état bloqué
    # pour l'indiquer dans les blocked_changes

    effective_evalbox = new_evalbox or current_evalbox
    can_modify, reason = can_modify_exam_date(effective_evalbox, current_date_cloture)

    if not can_modify:
        result['blocked_changes'].append({
            'field': 'Date_examen_VTC',
            'reason': reason,
            'evalbox': effective_evalbox,
            'date_cloture': current_date_cloture
        })
        logger.warning(f"  🔒 {reason}")

    # ================================================================
    # 7. APPLIQUER LES MISES À JOUR
    # ================================================================
    if updates_to_apply and not dry_run:
        try:
            from config import settings
            url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}"
            payload = {"data": [updates_to_apply]}

            response = crm_client._make_request("PUT", url, json=payload)

            if response.get('data'):
                result['crm_updated'] = True
                logger.info(f"  ✅ CRM mis à jour: {list(updates_to_apply.keys())}")
            else:
                logger.error(f"  ❌ Échec mise à jour CRM: {response}")

        except Exception as e:
            logger.error(f"  ❌ Erreur mise à jour CRM: {e}")
    elif updates_to_apply and dry_run:
        logger.info(f"  🔍 DRY RUN: Mises à jour simulées: {list(updates_to_apply.keys())}")
        result['crm_updated'] = False

    # ================================================================
    # 8. GÉNÉRER CONTENU POUR NOTE CRM
    # ================================================================
    if result['changes_made'] or result['blocked_changes']:
        note_lines = ["📊 SYNC EXAMT3P → CRM", f"Date: {datetime.now().strftime('%d/%m/%Y %H:%M')}"]

        if result['changes_made']:
            note_lines.append("\n✅ CHANGEMENTS APPLIQUÉS:")
            for change in result['changes_made']:
                if change['field'] == 'MDP_EVALBOX':
                    note_lines.append(f"  - {change['field']}: *** → ***")
                else:
                    note_lines.append(f"  - {change['field']}: '{change['old_value']}' → '{change['new_value']}'")

        if result['blocked_changes']:
            note_lines.append("\n🔒 CHANGEMENTS BLOQUÉS (règle critique):")
            for blocked in result['blocked_changes']:
                note_lines.append(f"  - {blocked['field']}: {blocked['reason']}")

        result['note_content'] = "\n".join(note_lines)

    result['sync_performed'] = True
    return result


def get_sync_status_message(
    evalbox_status: str,
    date_cloture: str,
    is_report_request: bool = False
) -> Optional[str]:
    """
    Génère un message approprié pour le candidat selon le statut de sync.

    Utilisé quand le candidat demande un report mais qu'on ne peut pas le faire.

    IMPORTANT: Ne jamais dire "nous contacter" - communication par EMAIL uniquement.
    """
    can_modify, reason = can_modify_exam_date(evalbox_status, date_cloture)

    if not can_modify and is_report_request:
        # Formater la date de clôture
        date_formatted = format_date_for_display(date_cloture) or str(date_cloture or '')

        return f"""Votre inscription à l'examen VTC a été validée par la CMA et les inscriptions sont maintenant clôturées.

**Un report n'est possible qu'avec un justificatif de force majeure** (certificat médical ou autre document attestant de l'impossibilité de vous présenter à l'examen).

**Pour demander un report, merci de nous transmettre par email :**
1. Votre justificatif de force majeure (certificat médical, etc.)
2. Une brève explication de votre situation

Nous soumettrons votre demande à la CMA pour validation.

**Important :** Sans justificatif valide, des frais de réinscription de {CMA_EXAM_FEE}€ seront à prévoir pour une nouvelle inscription."""

    return None


def find_exam_session_by_date_and_dept(
    crm_client,
    exam_date: str,
    departement: str
) -> Optional[Dict[str, Any]]:
    """
    Recherche une session d'examen dans le CRM par date et département.

    Args:
        crm_client: Client CRM Zoho
        exam_date: Date d'examen au format "dd/mm/yyyy" ou "yyyy-mm-dd"
        departement: Numéro de département (ex: "75", "93")

    Returns:
        Session trouvée ou None
    """
    from config import settings
    import re

    if not exam_date or not departement:
        return None

    # Normaliser la date au format yyyy-mm-dd pour la recherche CRM
    try:
        if '/' in str(exam_date):
            # Format dd/mm/yyyy
            date_obj = datetime.strptime(str(exam_date), "%d/%m/%Y")
        else:
            # Format yyyy-mm-dd
            date_obj = datetime.strptime(str(exam_date), "%Y-%m-%d")
        date_iso = date_obj.strftime("%Y-%m-%d")
        date_formatted = date_obj.strftime("%d/%m/%Y")
    except ValueError as e:
        logger.warning(f"  ⚠️ Format de date invalide: {exam_date} - {e}")
        return None

    logger.info(f"  🔍 Recherche session: date={date_formatted}, département={departement}")

    try:
        url = f"{settings.zoho_crm_api_url}/Dates_Examens_VTC_TAXI/search"

        # Critères: Date_Examen = date ET Departement = dept
        criteria = f"((Date_Examen:equals:{date_iso})and(Departement:equals:{departement}))"

        params = {
            "criteria": criteria,
            "per_page": 10
        }

        response = crm_client._make_request("GET", url, params=params)
        sessions = response.get("data", [])

        if sessions:
            session = sessions[0]
            logger.info(f"  ✅ Session trouvée: {session.get('Name')} (ID: {session.get('id')})")
            return session
        else:
            logger.warning(f"  ⚠️ Aucune session trouvée pour {date_formatted} / département {departement}")
            return None

    except Exception as e:
        logger.error(f"  ❌ Erreur recherche session: {e}")
        return None


def get_crm_exam_date(
    deal_data: Dict[str, Any],
    enriched_lookups: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Extrait la date d'examen du deal CRM au format dd/mm/yyyy.

    Args:
        deal_data: Données du deal CRM
        enriched_lookups: Lookups enrichis depuis crm_lookup_helper (optionnel, recommandé)

    Returns:
        Date formatée (dd/mm/yyyy) ou None
    """
    # Méthode préférée: utiliser les lookups enrichis
    if enriched_lookups and enriched_lookups.get('date_examen'):
        date_value = enriched_lookups['date_examen']
        # Convertir de YYYY-MM-DD vers dd/mm/yyyy
        parsed = parse_date_flexible(date_value)
        if parsed:
            return format_date_for_display(parsed)
        return None

    # Fallback: méthode legacy avec regex sur le champ "name"
    import re

    date_examen_vtc = deal_data.get('Date_examen_VTC')
    if not date_examen_vtc:
        return None

    if isinstance(date_examen_vtc, dict):
        # Lookup - extraire la date du name
        date_value = date_examen_vtc.get('name', '')

        # Essayer d'extraire une date au format dd/mm/yyyy
        if date_value and '/' in str(date_value):
            match = re.search(r'(\d{2}/\d{2}/\d{4})', str(date_value))
            if match:
                return match.group(1)

        # Essayer format yyyy-mm-dd dans le name
        if date_value:
            match = re.search(r'(\d{4}-\d{2}-\d{2})', str(date_value))
            if match:
                formatted = format_date_for_display(match.group(1))
                if formatted:
                    return formatted

    return None


def get_examt3p_exam_date(examt3p_data: Dict[str, Any]) -> Optional[str]:
    """
    Extrait la date d'examen des données ExamT3P au format dd/mm/yyyy.

    Gère plusieurs formats de date:
    - dd/mm/yyyy (standard)
    - yyyy-mm-dd (ISO)
    - "1 mars 2026" (format français extrait de "À partir du...")

    Returns:
        Date formatée au format dd/mm/yyyy ou None
    """
    date_examen = (
        examt3p_data.get('date_examen') or
        examt3p_data.get('examens', {}).get('date')
    )

    if not date_examen:
        return None

    date_str = str(date_examen).strip()

    # Format dd/mm/yyyy - déjà bon
    if '/' in date_str:
        return date_str

    # Format yyyy-mm-dd (ISO)
    if '-' in date_str and len(date_str) == 10:
        formatted = format_date_for_display(date_str)
        if formatted:
            return formatted

    # Format français "1 mars 2026" ou "15 février 2026"
    # Extraire jour, mois (texte), année
    import re
    match = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})', date_str)
    if match:
        jour = int(match.group(1))
        mois_texte = match.group(2).lower()
        annee = int(match.group(3))

        # Mapping mois français → numéro
        mois_mapping = {
            'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3,
            'avril': 4, 'mai': 5, 'juin': 6, 'juillet': 7,
            'août': 8, 'aout': 8, 'septembre': 9, 'octobre': 10,
            'novembre': 11, 'décembre': 12, 'decembre': 12
        }

        mois = mois_mapping.get(mois_texte)
        if mois:
            return f"{jour:02d}/{mois:02d}/{annee}"
        else:
            logger.warning(f"  ⚠️ Mois non reconnu dans date ExamT3P: {mois_texte}")

    logger.warning(f"  ⚠️ Format de date ExamT3P non reconnu: {date_str}")
    return None


def sync_exam_date_from_examt3p(
    deal_id: str,
    deal_data: Dict[str, Any],
    examt3p_data: Dict[str, Any],
    crm_client,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Synchronise la date d'examen depuis ExamT3P vers le CRM.

    Logique:
    1. Compare la date ExamT3P avec celle du CRM
    2. Si différentes ET pas bloqué → recherche la session correspondante
    3. Met à jour Date_examen_VTC avec l'ID de la session trouvée

    RÈGLE CRITIQUE:
    - Si Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"} ET clôture passée
    - → NE PAS modifier (action humaine requise)

    Args:
        deal_id: ID du deal CRM
        deal_data: Données actuelles du deal
        examt3p_data: Données extraites d'ExamT3P
        crm_client: Client CRM Zoho
        dry_run: Si True, ne fait pas les mises à jour

    Returns:
        {
            'sync_performed': bool,
            'date_changed': bool,
            'old_date': str or None,
            'new_date': str or None,
            'session_id': str or None,
            'blocked': bool,
            'blocked_reason': str or None,
            'error': str or None
        }
    """
    result = {
        'sync_performed': False,
        'date_changed': False,
        'old_date': None,
        'new_date': None,
        'session_id': None,
        'blocked': False,
        'blocked_reason': None,
        'error': None
    }

    if not examt3p_data or not examt3p_data.get('compte_existe'):
        return result

    # ================================================================
    # 1. RÉCUPÉRER LES DATES
    # ================================================================
    crm_date = get_crm_exam_date(deal_data)
    examt3p_date = get_examt3p_exam_date(examt3p_data)

    result['old_date'] = crm_date

    if not examt3p_date:
        logger.debug("  ℹ️ Pas de date d'examen dans ExamT3P")
        return result

    logger.info(f"  📅 Comparaison dates: CRM={crm_date or 'N/A'} vs ExamT3P={examt3p_date}")

    # ================================================================
    # 2. COMPARER LES DATES
    # ================================================================
    if crm_date == examt3p_date:
        logger.info(f"  ✅ Dates synchronisées: {crm_date}")
        result['sync_performed'] = True
        return result

    # Les dates sont différentes
    logger.info(f"  📊 Dates différentes: CRM={crm_date or 'VIDE'} → ExamT3P={examt3p_date}")

    # ================================================================
    # 2b. VÉRIFIER RÈGLE DE BLOCAGE
    # ================================================================
    # Si ExamT3P a une date FUTURE → écraser CRM systématiquement (quel que soit Evalbox).
    # Si ExamT3P a une date PASSÉE → bloquer sauf si Evalbox = "Convoc CMA reçue"
    # (date passée ExamT3P = artefact, CRM est la source de vérité via auto-report).
    current_evalbox = deal_data.get('Evalbox', '')
    examt3p_date_is_past = is_date_past(examt3p_date)

    if crm_date and examt3p_date_is_past and current_evalbox != 'Convoc CMA reçue':
        logger.info(f"  🔒 BLOCAGE SYNC DATE: ExamT3P date passée ({examt3p_date}) + Evalbox={current_evalbox!r} ≠ 'Convoc CMA reçue' → CRM protégé ({crm_date})")
        result['blocked'] = True
        result['blocked_reason'] = (
            f"Date ExamT3P ({examt3p_date}) est dans le passé et "
            f"Evalbox={current_evalbox!r} n'est pas 'Convoc CMA reçue'. "
            f"La date CRM ({crm_date}) est protégée."
        )
        result['sync_performed'] = True
        return result

    if not examt3p_date_is_past:
        logger.info(f"  ✅ Date ExamT3P future ({examt3p_date}) → sync autorisée (Evalbox={current_evalbox!r})")

    # ================================================================
    # 3. RÉCUPÉRER LE DÉPARTEMENT
    # ================================================================
    # Priorité: ExamT3P > CRM
    departement = (
        examt3p_data.get('departement') or
        deal_data.get('CMA_de_depot', '')
    )

    # Extraire le numéro de département
    import re
    if departement:
        match = re.search(r'\b(\d{2,3})\b', str(departement))
        if match:
            departement = match.group(1)
        else:
            # Mappings connus
            dept_mapping = {
                'idf': '75', 'ile de france': '75', 'paris': '75',
                'paca': '13', 'marseille': '13',
                'rhone': '69', 'lyon': '69'
            }
            dept_lower = str(departement).lower()
            for key, value in dept_mapping.items():
                if key in dept_lower:
                    departement = value
                    break

    if not departement:
        logger.warning("  ⚠️ Département non trouvé - impossible de chercher la session")
        result['error'] = "Département non trouvé"
        return result

    logger.info(f"  📍 Département: {departement}")

    # ================================================================
    # 5. RECHERCHER LA SESSION CORRESPONDANTE
    # ================================================================
    session = find_exam_session_by_date_and_dept(crm_client, examt3p_date, departement)

    if not session:
        logger.warning(f"  ⚠️ Session non trouvée pour {examt3p_date} / {departement}")
        result['error'] = f"Session non trouvée: {examt3p_date} / département {departement}"
        return result

    session_id = session.get('id')
    result['session_id'] = session_id
    result['new_date'] = examt3p_date

    # ================================================================
    # 6. METTRE À JOUR LE CRM
    # ================================================================
    if dry_run:
        logger.info(f"  🔍 DRY RUN: Date_examen_VTC serait mis à jour vers {session.get('Name')}")
        result['date_changed'] = True
        result['sync_performed'] = True
        return result

    try:
        from config import settings
        url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}"
        payload = {
            "data": [{
                "Date_examen_VTC": session_id
            }]
        }

        response = crm_client._make_request("PUT", url, json=payload)

        if response.get('data'):
            logger.info(f"  ✅ Date_examen_VTC mis à jour: {crm_date or 'VIDE'} → {examt3p_date}")
            result['date_changed'] = True
            result['sync_performed'] = True
        else:
            logger.error(f"  ❌ Échec mise à jour Date_examen_VTC: {response}")
            result['error'] = f"Échec mise à jour CRM: {response}"

    except Exception as e:
        logger.error(f"  ❌ Erreur mise à jour Date_examen_VTC: {e}")
        result['error'] = str(e)

    return result
