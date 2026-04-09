"""
Helper pour gérer les dates d'examen VTC et leur validation.

Workflow complet :
1. Vérifier si Date_examen_VTC est renseignée dans le Deal
2. Récupérer les infos de la session d'examen (date, date clôture, département)
3. Vérifier le statut Evalbox du candidat
4. Selon les cas, proposer les prochaines dates ou informer du statut

CAS GÉRÉS:
- CAS 1: Date vide → Proposer 2 prochaines dates (CMA du candidat)
- CAS 2: Date passée + Evalbox pré-validation (N/A, Dossier créé, Pret a payer, Dossier Synchronisé)
         → Auto-report sur prochaine date (candidat n'a PAS pu passer l'examen)
- CAS 3: Evalbox = Refusé CMA → Informer du refus + pièces + prochaine date
- CAS 4: Date future + Evalbox = VALIDE CMA → Rassurer (convocation ~10j avant)
- CAS 5: Date future + Evalbox = Dossier Synchronisé → Prévenir (instruction en cours)
- CAS 6: Date future + Evalbox = autre → En attente
- CAS 7: Date passée + Evalbox = VALIDE CMA ou Convoc CMA reçue → Examen probablement passé
         ATTENTION: "Dossier Synchronisé" = en instruction, PAS validé → utiliser CAS 2
- CAS 8: Date future + Date_Cloture passée + Evalbox pré-validation → Deadline ratée, auto-report
- CAS 9: Evalbox = Convoc CMA reçue → Transmettre identifiants, lien plateforme, instructions impression + bonne chance
- CAS 10: Evalbox = Pret a payer → Paiement en cours, surveiller emails, corriger si refus CMA avant clôture
"""
import logging
from datetime import datetime, date
from typing import Dict, Optional, List, Any

from src.utils.date_utils import parse_date_flexible
from src.constants.evalbox import VALIDATED, BLOCKING_RESCHEDULE, READY_TO_PAY, PAID_STATUSES
from src.constants.thresholds import (
    FORCE_MAJEURE_DEADLINE_DAYS, EXAM_IMMINENT_DAYS, CONVOCATION_DAYS_BEFORE_EXAM,
)

logger = logging.getLogger(__name__)


def get_next_exam_dates(
    crm_client,
    departement: str,
    limit: int = 2
) -> List[Dict[str, Any]]:
    """
    Récupère les prochaines dates d'examen disponibles pour un département.

    Filtres appliqués:
    - Date_Cloture_Inscription > aujourd'hui
    - Statut = "Actif"
    - Même département que le candidat

    Args:
        crm_client: Client Zoho CRM
        departement: Département du candidat (ex: "75", "93")
        limit: Nombre de dates à retourner

    Returns:
        Liste des sessions d'examen avec leurs infos
    """
    from config import settings

    logger.info(f"🔍 Recherche des prochaines dates d'examen pour le département {departement}")

    try:
        # Construire la requête de recherche
        # On cherche les sessions actives pour ce département
        # Note: L'API search ne supporte pas sort_by/sort_order sur les modules custom
        url = f"{settings.zoho_crm_api_url}/Dates_Examens_VTC_TAXI/search"

        # Critère: (Statut = Actif OU Statut = vide) AND Departement = X
        criteria = f"(((Statut:equals:Actif)or(Statut:equals:null))and(Departement:equals:{departement}))"

        # Pagination: récupérer toutes les pages
        all_sessions = []
        page = 1
        max_pages = 10  # Sécurité pour éviter boucle infinie

        while page <= max_pages:
            params = {
                "criteria": criteria,
                "page": page,
                "per_page": 200  # Max autorisé par Zoho
            }

            response = crm_client._make_request("GET", url, params=params)
            sessions = response.get("data", [])

            if not sessions:
                break

            all_sessions.extend(sessions)
            logger.info(f"  Page {page}: {len(sessions)} session(s) récupérée(s)")

            # Si moins de 200 résultats, c'est la dernière page
            if len(sessions) < 200:
                break

            page += 1

        if not all_sessions:
            logger.warning(f"Aucune session trouvée pour le département {departement}")
            # Essayer sans filtre département pour avoir au moins des suggestions
            return get_next_exam_dates_any_department(crm_client, limit)

        logger.info(f"  Total: {len(all_sessions)} session(s) récupérée(s) pour le département {departement}")

        # Filtrer les sessions avec clôture suffisamment dans le futur (min 2 jours)
        valid_sessions = []
        today_date = datetime.now().date()
        min_days_before_cloture = 1  # Minimum 1 jour avant la clôture (demain inclus)

        for session in all_sessions:
            date_cloture_str = session.get('Date_Cloture_Inscription')
            if date_cloture_str:
                date_cloture = parse_date_flexible(str(date_cloture_str), "date_cloture")
                if not date_cloture:
                    logger.warning(f"Erreur parsing date clôture {date_cloture_str}")
                    continue

                # Calculer le nombre de jours jusqu'à la clôture
                days_until_cloture = (date_cloture - today_date).days

                # Inclure seulement si clôture dans au moins min_days_before_cloture jours
                if days_until_cloture >= min_days_before_cloture:
                    valid_sessions.append(session)

        # Trier par date d'examen et prendre les N premières
        valid_sessions.sort(key=lambda x: x.get('Date_Examen', '9999-99-99'))

        result = valid_sessions[:limit]

        # Log détaillé des dates retournées pour debug
        for i, session in enumerate(result):
            exam_date = session.get('Date_Examen', 'N/A')
            cloture = session.get('Date_Cloture_Inscription', 'N/A')
            logger.info(f"  📅 Date {i+1}: Examen={exam_date}, Clôture={cloture}")

        logger.info(f"✅ {len(result)} date(s) d'examen valide(s) pour le département {departement} (clôture ≥ {min_days_before_cloture} jours)")

        return result

    except Exception as e:
        logger.error(f"❌ Erreur lors de la recherche des dates d'examen: {e}")
        return []


def get_earlier_dates_other_departments(
    crm_client,
    current_departement: str,
    reference_date: str,
    limit: int = 3
) -> List[Dict[str, Any]]:
    """
    Recherche des dates d'examen plus tôt dans d'autres départements.

    Cette fonction est utilisée quand:
    - Le candidat n'a PAS encore de compte ExamT3P (peut choisir n'importe quel département)
    - Les prochaines dates dans son département sont trop éloignées
    - Le candidat demande explicitement une date plus proche

    Args:
        crm_client: Client Zoho CRM
        current_departement: Département actuel du candidat (à exclure des résultats)
        reference_date: Date de référence (première date du département actuel, format YYYY-MM-DD)
        limit: Nombre maximum de dates à retourner

    Returns:
        Liste des sessions d'examen plus tôt dans d'autres départements,
        triées par date, avec info département incluse
    """
    from config import settings

    logger.info(f"🔍 Recherche de dates plus tôt dans d'autres départements (référence: {reference_date})")

    try:
        # Parser la date de référence
        if not reference_date:
            logger.warning("Pas de date de référence fournie")
            return []

        ref_date = parse_date_flexible(str(reference_date), "reference_date")
        if ref_date is None:
            logger.warning(f"Format de date de référence invalide: {reference_date}")
            return []

        url = f"{settings.zoho_crm_api_url}/Dates_Examens_VTC_TAXI/search"
        # Critère: Statut = Actif OU Statut = vide
        criteria = "((Statut:equals:Actif)or(Statut:equals:null))"

        # Pagination: récupérer toutes les pages
        all_sessions = []
        page = 1
        max_pages = 10

        while page <= max_pages:
            params = {
                "criteria": criteria,
                "page": page,
                "per_page": 200
            }

            response = crm_client._make_request("GET", url, params=params)
            sessions = response.get("data", [])

            if not sessions:
                break

            all_sessions.extend(sessions)

            if len(sessions) < 200:
                break

            page += 1

        if not all_sessions:
            logger.warning("Aucune session trouvée")
            return []

        # Filtrer les sessions:
        # 1. Département différent du département actuel
        # 2. Date de clôture suffisamment dans le futur (min 2 jours)
        # 3. Date d'examen AVANT la date de référence
        valid_sessions = []
        today_date = datetime.now().date()
        min_days_before_cloture = 1  # Minimum 1 jour avant la clôture

        for session in all_sessions:
            # Vérifier le département (zfill pour normaliser int→str: 1→"01")
            session_dept = str(session.get('Departement', '')).zfill(2)
            if session_dept == str(current_departement).zfill(2):
                continue  # Exclure le département actuel

            # Vérifier la date de clôture (doit être dans au moins 2 jours)
            date_cloture_str = session.get('Date_Cloture_Inscription')
            if date_cloture_str:
                date_cloture = parse_date_flexible(str(date_cloture_str), "date_cloture")
                if not date_cloture:
                    continue

                days_until_cloture = (date_cloture - today_date).days
                if days_until_cloture < min_days_before_cloture:
                    continue  # Clôture trop proche ou passée
            else:
                continue  # Pas de date de clôture = invalide

            # Vérifier la date d'examen (doit être AVANT la date de référence)
            date_examen_str = session.get('Date_Examen')
            if date_examen_str:
                date_examen = parse_date_flexible(str(date_examen_str), "date_examen")
                if date_examen is None:
                    continue
                if date_examen >= ref_date:
                    continue  # Pas plus tôt
                valid_sessions.append(session)

        # Trier par date d'examen (plus proche en premier)
        valid_sessions.sort(key=lambda x: x.get('Date_Examen', '9999-99-99'))

        result = valid_sessions[:limit]
        logger.info(f"✅ {len(result)} date(s) plus tôt trouvée(s) dans d'autres départements (clôture ≥ {min_days_before_cloture} jours)")

        return result

    except Exception as e:
        logger.error(f"❌ Erreur recherche dates autres départements: {e}")
        return []


def get_next_exam_dates_any_department(
    crm_client,
    limit: int = 2
) -> List[Dict[str, Any]]:
    """
    Récupère les prochaines dates d'examen sans filtre département (fallback).
    Avec pagination pour récupérer toutes les sessions.
    """
    from config import settings

    logger.info("🔍 Recherche des prochaines dates d'examen (tous départements)")

    try:
        url = f"{settings.zoho_crm_api_url}/Dates_Examens_VTC_TAXI/search"
        # Note: L'API search ne supporte pas sort_by/sort_order sur les modules custom
        # Critère: Statut = Actif OU Statut = vide
        criteria = "((Statut:equals:Actif)or(Statut:equals:null))"

        # Pagination: récupérer toutes les pages
        all_sessions = []
        page = 1
        max_pages = 10  # Sécurité pour éviter boucle infinie

        while page <= max_pages:
            params = {
                "criteria": criteria,
                "page": page,
                "per_page": 200  # Max autorisé par Zoho
            }

            response = crm_client._make_request("GET", url, params=params)
            sessions = response.get("data", [])

            if not sessions:
                break

            all_sessions.extend(sessions)
            logger.info(f"  Page {page}: {len(sessions)} session(s) récupérée(s)")

            # Si moins de 200 résultats, c'est la dernière page
            if len(sessions) < 200:
                break

            page += 1

        if not all_sessions:
            logger.warning("Aucune session active trouvée")
            return []

        logger.info(f"  Total: {len(all_sessions)} session(s) actives récupérée(s)")

        # Filtrer les sessions avec clôture suffisamment dans le futur (min 2 jours)
        # Une clôture demain ou aujourd'hui n'est pas pratique
        valid_sessions = []
        today_date = datetime.now().date()
        min_days_before_cloture = 1  # Minimum 1 jour avant la clôture (demain inclus)

        for session in all_sessions:
            date_cloture_str = session.get('Date_Cloture_Inscription')
            if date_cloture_str:
                date_cloture = parse_date_flexible(str(date_cloture_str), "date_cloture")
                if not date_cloture:
                    continue

                # Calculer le nombre de jours jusqu'à la clôture
                days_until_cloture = (date_cloture - today_date).days

                # Inclure seulement si clôture dans au moins min_days_before_cloture jours
                if days_until_cloture >= min_days_before_cloture:
                    valid_sessions.append(session)
                else:
                    logger.debug(f"  Session exclue: clôture {date_cloture} dans {days_until_cloture} jours (min: {min_days_before_cloture})")

        valid_sessions.sort(key=lambda x: x.get('Date_Examen', '9999-99-99'))
        result = valid_sessions[:limit]

        # Log détaillé des dates retournées pour debug
        for i, session in enumerate(result):
            exam_date = session.get('Date_Examen', 'N/A')
            cloture = session.get('Date_Cloture_Inscription', 'N/A')
            dept = session.get('Departement', 'N/A')
            logger.info(f"  📅 Date {i+1}: Examen={exam_date}, Clôture={cloture}, Dept={dept}")

        logger.info(f"✅ {len(result)} date(s) d'examen valide(s) (tous départements, clôture ≥ {min_days_before_cloture} jours)")
        return result

    except Exception as e:
        logger.error(f"❌ Erreur lors de la recherche des dates d'examen: {e}")
        return []


def format_exam_date_for_display(session: Dict[str, Any], include_department: bool = False) -> str:
    """
    Formate une session d'examen pour affichage au candidat.

    Args:
        session: Données de la session d'examen
        include_department: Si True, inclut le département dans l'affichage

    Returns:
        Texte formaté pour le candidat
    """
    date_examen = session.get('Date_Examen', 'Date inconnue')
    libelle = session.get('Libelle_Affichage', '')
    adresse = session.get('Adresse_Centre', '')
    date_cloture = session.get('Date_Cloture_Inscription', '')

    # Formater la date d'examen
    if date_examen and date_examen != 'Date inconnue':
        date_obj = parse_date_flexible(str(date_examen), "date_examen")
        date_examen_formatted = date_obj.strftime("%d/%m/%Y") if date_obj else date_examen
    else:
        date_examen_formatted = date_examen

    # Formater la date de clôture
    if date_cloture:
        date_cloture_obj = parse_date_flexible(str(date_cloture), "date_cloture")
        date_cloture_formatted = date_cloture_obj.strftime("%d/%m/%Y") if date_cloture_obj else ""
    else:
        date_cloture_formatted = ""

    result = f"- **{date_examen_formatted}**"

    # Ajouter le département si demandé
    if include_department:
        departement = session.get('Departement', '')
        if departement:
            result += f" (Département {departement})"
    elif libelle:
        result += f" ({libelle})"

    if date_cloture_formatted:
        result += f" - Clôture inscriptions: {date_cloture_formatted}"

    return result


def is_date_in_past(date_str: str) -> bool:
    """
    Vérifie si une date est dans le passé.
    """
    if not date_str:
        return False

    date_obj = parse_date_flexible(str(date_str), "is_date_in_past")
    if date_obj is None:
        return False

    return date_obj < datetime.now().date()


def analyze_exam_date_situation(
    deal_data: Dict[str, Any],
    threads: List[Dict] = None,
    crm_client = None,
    examt3p_data: Dict[str, Any] = None,
    session_preference: str = None,
    enriched_lookups: Dict[str, Any] = None,
    deal_timeline: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Analyse la situation de date d'examen VTC du candidat et détermine l'action à prendre.

    LOGIQUE D'AUTO-ASSIGNATION (v2.0):
    - Scénario A: Date vide + pas de session → fixer prochaine date + déduire session compatible
    - Scénario B: Date vide + session confirmée → fixer date juste après fin de session
    - Scénario C: Date fixée + pas de session → proposer sessions compatibles

    Args:
        deal_data: Données du deal CRM
        threads: Threads du ticket (pour détecter indices examen non passé)
        crm_client: Client Zoho CRM (pour récupérer les prochaines dates)
        examt3p_data: Données ExamT3P (pour pièces refusées)
        session_preference: Préférence horaire du candidat ('jour' ou 'soir')
        enriched_lookups: Lookups enrichis avec infos session (session_date_fin, session_type, etc.)

    Returns:
        {
            'case': int (1-10),
            'case_description': str,
            'date_examen_vtc': str or None,
            'date_examen_info': Dict or None,
            'evalbox_status': str or None,
            'should_include_in_response': bool,
            'next_dates': List[Dict],
            'pieces_refusees': List[str] (pour cas 3),
            'date_cloture': str or None,
            'alternative_department_dates': List[Dict] (dates plus tôt dans autres depts),
            'can_choose_other_department': bool (True si pas de compte ExamT3P),
            'current_departement': str or None,
            # Nouveaux champs pour auto-assignation
            'auto_assigned': bool (True si date/session auto-assignée),
            'auto_assigned_exam_date': str or None (date YYYY-MM-DD assignée),
            'auto_assigned_exam_session_id': str or None (ID session examen pour CRM),
            'auto_assigned_session': Dict or None (session formation déduite),
            'crm_updates': Dict (mises à jour CRM à appliquer)
        }
    """
    result = {
        'case': 0,
        'case_description': '',
        'date_examen_vtc': None,
        'date_examen_info': None,
        'evalbox_status': None,
        'should_include_in_response': False,
        'next_dates': [],
        'pieces_refusees': [],
        'date_cloture': None,
        # Dates alternatives dans d'autres départements
        'alternative_department_dates': [],
        'can_choose_other_department': False,
        'current_departement': None,
        # Nouveaux champs pour auto-assignation
        'auto_assigned': False,
        'auto_assigned_exam_date': None,
        'auto_assigned_exam_session_id': None,
        'auto_assigned_session': None,
        'crm_updates': {}
    }

    # Extraire les infos de session depuis enriched_lookups
    session_confirmed = False
    session_date_fin = None
    session_type = None
    session_id = None

    if enriched_lookups:
        session_date_fin = enriched_lookups.get('session_date_fin')
        session_type = enriched_lookups.get('session_type')
        # session_id est dans session_record (pas directement dans enriched_lookups)
        session_record = enriched_lookups.get('session_record', {})
        if session_record:
            session_id = session_record.get('id')
        session_confirmed = bool(session_id and session_date_fin)
        if session_confirmed:
            logger.info(f"  📚 Session déjà confirmée: {enriched_lookups.get('session_name')} (fin: {session_date_fin})")

    logger.info("🔍 Analyse de la situation date d'examen VTC...")

    # Récupérer les données du deal
    date_examen_vtc = deal_data.get('Date_examen_VTC')
    evalbox_status = deal_data.get('Evalbox', '')
    cma_depot = deal_data.get('CMA_de_depot', '')

    result['evalbox_status'] = evalbox_status

    # Extraire le département de la CMA (si format "CMA XX" ou numéro direct)
    departement = extract_departement_from_cma(cma_depot)
    result['current_departement'] = departement

    # Vérifier si le candidat a un compte ExamT3P (peut choisir autre département si non)
    compte_examt3p_existe = examt3p_data.get('compte_existe', False) if examt3p_data else False
    result['can_choose_other_department'] = not compte_examt3p_existe

    logger.info(f"  Date_examen_VTC: {date_examen_vtc}")
    logger.info(f"  Evalbox: {evalbox_status}")
    logger.info(f"  CMA_de_depot: {cma_depot} (département: {departement})")
    logger.info(f"  Compte ExamT3P existe: {compte_examt3p_existe} (peut choisir autre dept: {not compte_examt3p_existe})")

    # Si date_examen_vtc est un lookup, on doit récupérer l'ID et les infos
    if date_examen_vtc:
        if isinstance(date_examen_vtc, dict):
            # C'est un lookup, on a l'ID et le name
            result['date_examen_vtc'] = date_examen_vtc.get('id')
            result['date_examen_info'] = date_examen_vtc
            # Récupérer les infos complètes de la session
            if crm_client and date_examen_vtc.get('id'):
                session_info = get_exam_session_details(crm_client, date_examen_vtc.get('id'))
                if session_info:
                    result['date_examen_info'] = session_info
                    result['date_cloture'] = session_info.get('Date_Cloture_Inscription')
        else:
            result['date_examen_vtc'] = date_examen_vtc

    # ================================================================
    # CORRECTION DÉSYNCHRONISATION CRM ↔ EXAMT3P
    # ================================================================
    # ExamT3P est la source de vérité pour la date d'examen.
    # Si CRM et ExamT3P ont des dates différentes (ex: CRM=31/03, ExamT3P=25/02),
    # on DOIT utiliser la date ExamT3P pour tout le reste de l'analyse,
    # sinon la clôture sera celle de la mauvaise date et le CAS sera faux.
    if examt3p_data and date_examen_vtc:
        from src.utils.examt3p_crm_sync import get_examt3p_exam_date
        examt3p_exam_date_str = get_examt3p_exam_date(examt3p_data)
        if examt3p_exam_date_str:
            # Comparer les dates (normaliser au format date object)
            examt3p_date_obj = parse_date_flexible(examt3p_exam_date_str, "examt3p_exam_date")
            crm_date_str = result.get('date_examen_info', {}).get('Date_Examen') if isinstance(result.get('date_examen_info'), dict) else None
            crm_date_obj = parse_date_flexible(crm_date_str, "crm_exam_date") if crm_date_str else None

            if examt3p_date_obj and crm_date_obj and examt3p_date_obj != crm_date_obj:
                logger.warning(f"  ⚠️ DÉSYNCHRONISATION DATE: CRM={crm_date_str} ≠ ExamT3P={examt3p_exam_date_str}")

                # Essayer de trouver la session CRM correspondant à la date ExamT3P
                if crm_client and departement:
                    from src.utils.examt3p_crm_sync import find_exam_session_by_date_and_dept
                    examt3p_session = find_exam_session_by_date_and_dept(
                        crm_client, examt3p_exam_date_str, departement
                    )
                    if examt3p_session:
                        # Session CRM trouvée pour la date ExamT3P → utiliser ses infos
                        logger.info(f"  📅 ExamT3P confirmé par calendrier CRM → override de la date")
                        result['date_examen_info'] = examt3p_session
                        result['date_cloture'] = examt3p_session.get('Date_Cloture_Inscription')
                        logger.info(f"  ✅ Session CRM trouvée pour ExamT3P date: clôture={result['date_cloture']}")
                        result['date_examen_crm_desync'] = True
                    else:
                        # Pas de session CRM pour la date ExamT3P
                        # La date ExamT3P n'existe pas dans le calendrier Zoho →
                        # c'est probablement une erreur ExamT3P. On garde la date CRM.
                        logger.warning(f"  ⚠️ Date ExamT3P {examt3p_exam_date_str} N'EXISTE PAS dans le calendrier CRM (dept {departement})")
                        logger.info(f"  📅 CRM reste la source de vérité → date {crm_date_str} conservée")
                        result['date_examen_crm_desync'] = True
                        result['examt3p_date_not_in_calendar'] = True
                else:
                    result['date_examen_crm_desync'] = True

    # ================================================================
    # DÉTERMINATION DU CAS
    # ================================================================

    # CAS 1: Date vide → AUTO-ASSIGNATION
    if not date_examen_vtc:
        result['case'] = 1
        result['should_include_in_response'] = True

        if crm_client:
            # ================================================================
            # SCÉNARIO B: Session DÉJÀ confirmée → Fixer date APRÈS fin de session
            # ================================================================
            if session_confirmed and session_date_fin:
                logger.info(f"  📅 SCÉNARIO B: Session confirmée (fin: {session_date_fin}) → recherche date examen après")
                result['case_description'] = "Date vide + session confirmée - Auto-assignation date après session"

                # Chercher la première date d'examen APRÈS la fin de session
                # On récupère plus de dates pour avoir des options après la session
                if departement:
                    all_dates = get_next_exam_dates(crm_client, departement, limit=5)
                else:
                    all_dates = get_next_exam_dates_any_department(crm_client, limit=15)

                # Filtrer: date examen > session_date_fin
                session_end = parse_date_flexible(session_date_fin, "session_date_fin")
                if session_end:
                    valid_dates = []
                    for d in all_dates:
                        exam_date_str = d.get('Date_Examen', '')
                        if exam_date_str:
                            exam_date = parse_date_flexible(exam_date_str, "exam_date")
                            if exam_date and exam_date > session_end:
                                valid_dates.append(d)

                    if valid_dates:
                        # Prendre la première date valide (la plus proche après la session)
                        auto_date = valid_dates[0]
                        result['auto_assigned'] = True
                        result['auto_assigned_exam_date'] = auto_date.get('Date_Examen')
                        result['auto_assigned_exam_session_id'] = auto_date.get('id')
                        result['date_examen_info'] = auto_date
                        result['next_dates'] = [auto_date]  # Juste la date confirmée

                        # CRM updates
                        result['crm_updates'] = {
                            'Date_examen_VTC': auto_date.get('id')
                        }

                        logger.info(f"  ✅ AUTO-ASSIGNATION (Scénario B): Examen {result['auto_assigned_exam_date']} (après session {session_date_fin})")
                    else:
                        logger.warning(f"  ⚠️ Aucune date d'examen trouvée après la fin de session ({session_date_fin})")
                        result['next_dates'] = all_dates
                        result['case_description'] = "Date vide + session confirmée - Pas de date après session"
                else:
                    logger.error(f"  ❌ Erreur parsing session_date_fin: {session_date_fin}")
                    result['next_dates'] = all_dates

            # ================================================================
            # SCÉNARIO A: Pas de session → Fixer prochaine date + déduire session
            # ================================================================
            else:
                logger.info(f"  📅 SCÉNARIO A: Pas de session → auto-assignation date + session")
                result['case_description'] = "Date vide + pas de session - Auto-assignation date et session"

                if departement:
                    result['next_dates'] = get_next_exam_dates(crm_client, departement, limit=2)
                else:
                    logger.info("  ⚠️ Département inconnu - récupération des dates tous départements")
                    result['next_dates'] = get_next_exam_dates_any_department(crm_client, limit=15)

                if result['next_dates']:
                    # Prendre la première date disponible
                    auto_date = result['next_dates'][0]
                    exam_date_str = auto_date.get('Date_Examen')

                    result['auto_assigned'] = True
                    result['auto_assigned_exam_date'] = exam_date_str
                    result['auto_assigned_exam_session_id'] = auto_date.get('id')
                    result['date_examen_info'] = auto_date

                    # CRM updates pour la date
                    result['crm_updates'] = {
                        'Date_examen_VTC': auto_date.get('id')
                    }

                    # Déduire la session de formation compatible
                    if exam_date_str:
                        from src.utils.session_helper import get_sessions_for_exam_date
                        compatible_sessions = get_sessions_for_exam_date(
                            crm_client,
                            exam_date_str,
                            session_type=session_preference,
                            limit=2
                        )

                        if compatible_sessions:
                            # Prendre la session selon la préférence (ou la première si pas de préférence)
                            if session_preference:
                                # Chercher la session du type préféré
                                matching_session = next(
                                    (s for s in compatible_sessions if s.get('session_type') == session_preference),
                                    compatible_sessions[0]
                                )
                            else:
                                matching_session = compatible_sessions[0]

                            result['auto_assigned_session'] = matching_session
                            result['crm_updates']['Session'] = matching_session.get('id')
                            if session_preference:
                                result['crm_updates']['Preference_horaire'] = session_preference

                            logger.info(f"  ✅ AUTO-ASSIGNATION (Scénario A): Examen {exam_date_str} + Session {matching_session.get('Name')}")
                        else:
                            logger.warning(f"  ⚠️ Pas de session compatible trouvée pour l'examen du {exam_date_str}")

                # Si pas de compte ExamT3P, chercher des dates plus tôt dans d'autres départements
                if result['can_choose_other_department'] and result['next_dates'] and departement:
                    first_date = result['next_dates'][0].get('Date_Examen')
                    if first_date:
                        result['alternative_department_dates'] = get_earlier_dates_other_departments(
                            crm_client,
                            departement,
                            first_date,
                            limit=3
                        )
                        if result['alternative_department_dates']:
                            logger.info(f"  📅 {len(result['alternative_department_dates'])} date(s) plus tôt dans d'autres départements")

        logger.info(f"  ➡️ CAS 1: Date vide (auto_assigned={result['auto_assigned']})")
        return result

    # Déterminer si la date est passée
    date_examen_str = None
    if result.get('date_examen_info'):
        if isinstance(result['date_examen_info'], dict):
            date_examen_str = result['date_examen_info'].get('Date_Examen')

    date_is_past = is_date_in_past(date_examen_str) if date_examen_str else False

    # ================================================================
    # CAS 8 PRIORITAIRE: Deadline passée → report automatique
    # ================================================================
    # Statuts où on NE PEUT PAS changer la date (déjà validé par CMA)
    # Pour tous les autres statuts, si la clôture est passée, on redirige vers la prochaine date
    BLOCKED_STATUSES_FOR_RESCHEDULE = BLOCKING_RESCHEDULE

    date_cloture_is_past = is_date_in_past(result['date_cloture']) if result.get('date_cloture') else False

    # ================================================================
    # VÉRIFICATION DATE DE PAIEMENT (avant de déclencher CAS 8)
    # Si le paiement a été fait AVANT la clôture, le candidat est inscrit
    # → Pas de changement de date même si clôture passée
    # ================================================================
    paiement_avant_cloture = False
    if examt3p_data and date_cloture_is_past:
        paiement_cma = examt3p_data.get('paiement_cma', {})
        date_paiement_str = paiement_cma.get('date')  # Format: "03/02/2026" ou "2026-02-03"

        if date_paiement_str and result.get('date_cloture'):
            try:
                date_paiement = parse_date_flexible(date_paiement_str, "date_paiement_cma")
                date_cloture = parse_date_flexible(result['date_cloture'], "date_cloture")

                if date_paiement and date_cloture:
                    paiement_avant_cloture = date_paiement <= date_cloture
                    if paiement_avant_cloture:
                        logger.info(f"  ✅ Paiement CMA fait le {date_paiement_str} (AVANT clôture {result['date_cloture']}) → Candidat inscrit")
                    else:
                        logger.info(f"  ⚠️ Paiement CMA fait le {date_paiement_str} (APRÈS clôture {result['date_cloture']}) → Report nécessaire")
            except Exception as e:
                logger.warning(f"  ⚠️ Erreur parsing dates paiement/clôture: {e}")

    # GUARD RAIL: Dossier Synchronisé sans accès ExamT3P → présumer paiement valide
    # "Dossier Synchronisé" = paiement 241€ effectué par CAB. Si on n'a pas accès à ExamT3P
    # pour vérifier la date de paiement, on ne peut PAS savoir si c'était avant ou après clôture.
    # Par sécurité, on présume que le paiement est valide (avant clôture) et on NE touche PAS
    # la date d'examen dans le CRM. Modifier la date serait dangereux (Mbappé Moudiki incident).
    if evalbox_status in PAID_STATUSES and date_cloture_is_past and not paiement_avant_cloture:
        compte_existe = examt3p_data.get('compte_existe', False) if examt3p_data else False
        if not compte_existe:
            paiement_avant_cloture = True
            logger.info(f"  🛡️ GUARD RAIL: Evalbox={evalbox_status} (paiement fait) + pas d'accès ExamT3P → présume paiement avant clôture, CAS 8 BLOQUÉ")

    # ================================================================
    # VÉRIFICATION REFUS CMA APRÈS CLÔTURE (via Timeline)
    # Même si paiement_avant_cloture=True, un refus CMA survenu APRÈS la clôture
    # rend la date stale → le candidat doit être auto-reporté.
    # Cas typique: Dossier Synchronisé cache un refus résolu (Sync→Refusé→Sync)
    # ================================================================
    if paiement_avant_cloture and date_cloture_is_past and deal_timeline and result.get('date_cloture'):
        try:
            from src.utils.thread_memory import parse_timeline, detect_refus_after_cloture
            timeline_field_changes, _ = parse_timeline(deal_timeline)
            if detect_refus_after_cloture(timeline_field_changes, result['date_cloture']):
                paiement_avant_cloture = False
                logger.info(
                    "  ⚠️ OVERRIDE: paiement_avant_cloture=True MAIS refus CMA après clôture "
                    "détecté dans la timeline → paiement_avant_cloture forcé à False → CAS 8 activé"
                )
        except Exception as e:
            logger.warning(f"  ⚠️ Erreur détection refus après clôture: {e}")

    # CAS 8: Seulement si paiement fait APRÈS clôture (ou pas de paiement trouvé)
    if evalbox_status not in BLOCKED_STATUSES_FOR_RESCHEDULE and date_cloture_is_past and not date_is_past and not paiement_avant_cloture:
        # Date d'examen future mais deadline passée ET paiement après clôture → report automatique
        result['case'] = 8
        result['case_description'] = f"Deadline passée (evalbox: {evalbox_status}) - Report automatique sur prochaine session"
        result['should_include_in_response'] = True
        result['deadline_passed_reschedule'] = True  # Flag pour mise à jour CRM

        # Conserver les dates originales pour le template
        result['original_exam_date'] = date_examen_str
        result['original_date_cloture'] = result['date_cloture']

        # Récupérer la prochaine date disponible
        if crm_client:
            if departement:
                next_dates = get_next_exam_dates(crm_client, departement, limit=2)
            else:
                logger.info("  ⚠️ Département inconnu - récupération des dates tous départements")
                next_dates = get_next_exam_dates_any_department(crm_client, limit=15)

            result['next_dates'] = next_dates

            # Stocker la nouvelle date pour mise à jour CRM
            if next_dates:
                new_session = next_dates[0]
                result['new_exam_session'] = new_session
                result['new_exam_session_id'] = new_session.get('id')
                result['new_exam_date'] = new_session.get('Date_Examen')
                result['new_exam_date_cloture'] = new_session.get('Date_Cloture_Inscription')
                logger.info(f"  📅 Nouvelle date proposée: {result['new_exam_date']} (clôture: {result['new_exam_date_cloture']})")

            # Dates alternatives si pas de compte ExamT3P
            if result['can_choose_other_department'] and next_dates and departement:
                first_date = next_dates[0].get('Date_Examen')
                if first_date:
                    result['alternative_department_dates'] = get_earlier_dates_other_departments(
                        crm_client, departement, first_date, limit=3
                    )

        logger.info(f"  ➡️ CAS 8: Deadline passée + {evalbox_status} → Report automatique")
        return result

    # CAS 3: Evalbox = Refusé CMA (prioritaire car peut arriver avec date passée ou future)
    # Statut "Incomplet" sur ExamT3P = certaines pièces refusées par la CMA
    # En cas de refus, le candidat est automatiquement repositionné sur la PROCHAINE date d'examen
    if evalbox_status == 'Refusé CMA':
        # Récupérer les pièces refusées depuis ExamT3P (noms + détails)
        pieces = []
        pieces_details = []
        if examt3p_data:
            pieces = examt3p_data.get('documents_refuses', [])
            pieces_details = examt3p_data.get('pieces_refusees_details', [])

        # GARDE-FOU: Si Refusé CMA mais 0 pièce REFUSÉ → faux refus (incohérence ExamT3P)
        # Les anciennes pièces refusées n'ont pas été supprimées sur ExamT3P
        # → Traiter comme Dossier Synchronisé (CAS 5) au lieu de CAS 3
        # ⚠️ Seulement si on a pu vérifier le compte (sinon on ne sait pas)
        if not pieces_details and compte_examt3p_existe:
            logger.info("  ⚠️ Refusé CMA mais 0 pièce REFUSÉ → faux refus (incohérence ExamT3P)")
            result['faux_refus_cma'] = True
            result['case'] = 5
            result['case_description'] = "Faux Refusé CMA (0 pièce refusée) - En attente validation CMA"
            result['should_include_in_response'] = True
            logger.info(f"  ➡️ CAS 5 (faux refus): Traitement comme Dossier Synchronisé")
            return result

        # Vrai refus CMA : CAS 3 normal
        result['case'] = 3
        result['case_description'] = "Refusé CMA - Pièces refusées, repositionnement sur prochaine date"
        result['should_include_in_response'] = True
        result['pieces_refusees'] = pieces
        result['pieces_refusees_details'] = pieces_details

        # Récupérer UNE SEULE prochaine date (positionnement automatique)
        next_exam_date = None
        next_date_cloture = None
        if crm_client:
            if departement:
                next_dates = get_next_exam_dates(crm_client, departement, limit=1)
            else:
                # Fallback when department is unknown
                logger.info("  ⚠️ Département inconnu - récupération des dates tous départements")
                next_dates = get_next_exam_dates_any_department(crm_client, limit=1)
            if next_dates:
                next_exam_date = next_dates[0]
                # Utiliser la date de clôture de la PROCHAINE session (pas l'ancienne)
                next_date_cloture = next_exam_date.get('Date_Cloture_Inscription')

                # Repositionner automatiquement sur la prochaine date dans le CRM
                # L'ancienne date est caduque (refus), le CRM doit refléter la prochaine cible
                result['auto_assigned'] = True
                result['auto_assigned_exam_date'] = next_exam_date.get('Date_Examen')
                result['auto_assigned_exam_session_id'] = next_exam_date.get('id')
                result['crm_updates'] = {
                    'Date_examen_VTC': next_exam_date.get('id')
                }
                logger.info(f"  🔄 CAS 3: Repositionnement CRM sur {next_exam_date.get('Date_Examen')} (id={next_exam_date.get('id')})")

            result['next_dates'] = next_dates

        logger.info(f"  ➡️ CAS 3: Refusé CMA - {len(result.get('pieces_refusees', []))} pièce(s) refusée(s)")
        return result

    # CAS avec date dans le passé
    if date_is_past:
        # Statuts validés = dossier vraiment validé par la CMA (candidat a pu passer l'examen)
        # ATTENTION: "Dossier Synchronisé" = en instruction, PAS validé !
        # CAS 7: Date passée + dossier VALIDÉ (examen probablement passé)
        if evalbox_status in VALIDATED:
            result['case'] = 7
            result['case_description'] = "Date passée + dossier validé - Examen probablement passé"

            # Calculer le nombre de jours depuis l'examen
            days_since_exam = None
            if date_examen_str:
                date_obj = parse_date_flexible(str(date_examen_str), "date_examen_cas7")
                if date_obj:
                    today = datetime.now().date()
                    days_since_exam = (today - date_obj).days
                    result['days_since_exam'] = days_since_exam
                    # Force majeure possible uniquement si < 14 jours après l'examen
                    result['force_majeure_possible'] = days_since_exam <= FORCE_MAJEURE_DEADLINE_DAYS
                    logger.info(f"  📅 Jours depuis l'examen: {days_since_exam} → force majeure {'possible' if result['force_majeure_possible'] else 'NON possible (> 14 jours)'}")
                else:
                    logger.warning(f"  ⚠️ Erreur parsing date examen: {date_examen_str}")
                    result['days_since_exam'] = None
                    result['force_majeure_possible'] = False

            # Vérifier s'il y a des indices dans les threads que l'examen n'a pas été passé
            has_indices_not_passed = check_threads_for_exam_not_passed(threads) if threads else False

            # Si Resultat indique un échec THÉORIQUE → charger prochaines dates pour réinscription
            # NON ADMIS / ABSENT PR = échec pratique → dates pratiques non connues (gérées par CMA)
            resultat = deal_data.get('Resultat', '')
            ECHEC_THEORIQUE = {'NON ADMISSIBLE', 'ABSENT TH'}
            if resultat in ECHEC_THEORIQUE and crm_client:
                logger.info(f"  📅 CAS 7 + Resultat={resultat} → chargement prochaines dates pour réinscription")
                dept = extract_departement_from_cma(deal_data.get('CMA_de_depot', ''))
                if dept:
                    next_dates = get_next_exam_dates(crm_client, dept, limit=3)
                    if next_dates:
                        result['next_dates'] = next_dates
                        result['should_include_in_response'] = True
                        logger.info(f"  ✅ {len(next_dates)} date(s) disponibles pour réinscription (dept {dept})")

            if has_indices_not_passed:
                result['should_include_in_response'] = True
            elif not result.get('next_dates'):
                result['should_include_in_response'] = False

            logger.info(f"  ➡️ CAS 7: Date passée + validé (indices non passé: {has_indices_not_passed}, resultat: {resultat or 'N/A'})")
            return result

        # CAS 2: Date passée + statuts pré-validation (N/A, Dossier créé, Pret a payer, Dossier Synchronisé)
        # Le candidat n'a PAS pu passer l'examen car son dossier n'était pas validé
        # → Auto-report automatique par la CMA sur la prochaine date
        else:
            result['case'] = 2
            result['case_description'] = "Date passée + dossier non validé - Auto-report sur prochaine date"
            result['should_include_in_response'] = True
            result['auto_report'] = True  # Flag pour indiquer l'auto-report

            if crm_client:
                if departement:
                    result['next_dates'] = get_next_exam_dates(crm_client, departement, limit=2)
                else:
                    # Fallback when department is unknown
                    logger.info("  ⚠️ Département inconnu - récupération des dates tous départements")
                    result['next_dates'] = get_next_exam_dates_any_department(crm_client, limit=15)  # Many dates for geographic coverage

                # Stocker la première date comme date d'auto-report
                if result['next_dates']:
                    first_next = result['next_dates'][0]
                    result['auto_report_date'] = first_next.get('Date_Examen')
                    result['auto_report_session_id'] = first_next.get('id')
                    # IMPORTANT: Mettre à jour date_cloture avec la NOUVELLE clôture (pas l'ancienne)
                    result['date_cloture'] = first_next.get('Date_Cloture_Inscription')
                    logger.info(f"  ✅ Auto-report détecté: {date_examen_str} → {result['auto_report_date']}")

                # Si pas de compte ExamT3P, chercher des dates plus tôt dans d'autres départements
                if result['can_choose_other_department'] and result['next_dates'] and departement:
                    first_date = result['next_dates'][0].get('Date_Examen')
                    if first_date:
                        result['alternative_department_dates'] = get_earlier_dates_other_departments(
                            crm_client,
                            departement,
                            first_date,
                            limit=3
                        )
                        if result['alternative_department_dates']:
                            logger.info(f"  📅 {len(result['alternative_department_dates'])} date(s) plus tôt dans d'autres départements")

            logger.info(f"  ➡️ CAS 2: Date passée + non validé → auto-report")
            return result

    # CAS avec date dans le futur
    else:
        # CAS 4: Date future + VALIDE CMA
        if evalbox_status == 'VALIDE CMA':
            result['case'] = 4
            result['case_description'] = "Date future + VALIDE CMA - Dossier validé, convocation à venir"
            result['should_include_in_response'] = True

            # Calculer les jours jusqu'à l'examen pour adapter le message
            days_until_exam = None
            if date_examen_str:
                date_obj = parse_date_flexible(str(date_examen_str), "date_examen_cas4")
                if date_obj:
                    today = datetime.now().date()
                    days_until_exam = (date_obj - today).days

            # Si examen dans ≤ 7 jours sans convocation → candidat sera décalé
            # Récupérer la prochaine date d'examen disponible
            next_exam_date = None
            if days_until_exam is not None and days_until_exam <= EXAM_IMMINENT_DAYS:
                if crm_client:
                    if departement:
                        next_dates = get_next_exam_dates(crm_client, departement, limit=2)
                    else:
                        # Fallback when department is unknown
                        logger.info("  ⚠️ Département inconnu - récupération des dates tous départements")
                        next_dates = get_next_exam_dates_any_department(crm_client, limit=15)  # Many dates for geographic coverage
                    # Prendre la 2ème date (la 1ère est celle qui est imminente)
                    if len(next_dates) >= 2:
                        next_exam_date = next_dates[1]
                    elif len(next_dates) == 1:
                        next_exam_date = next_dates[0]
                    result['next_dates'] = next_dates

            logger.info(f"  ➡️ CAS 4: Date future + VALIDE CMA (jours restants: {days_until_exam})")
            return result

        # CAS 5: Date future + Dossier Synchronisé
        if evalbox_status == 'Dossier Synchronisé':
            result['case'] = 5
            result['case_description'] = "Date future + Dossier Synchronisé - Instruction en cours"
            result['should_include_in_response'] = True
            logger.info(f"  ➡️ CAS 5: Date future + Dossier Synchronisé")
            return result

        # CAS 9: Convocation CMA reçue - Informer le candidat et lui donner ses identifiants
        if evalbox_status == 'Convoc CMA reçue':
            result['case'] = 9
            result['case_description'] = "Convocation CMA reçue - Transmettre identifiants et instructions"
            result['should_include_in_response'] = True

            # Récupérer les identifiants ExamT3P du deal
            identifiant = deal_data.get('IDENTIFIANT_EVALBOX', '')
            mot_de_passe = deal_data.get('MDP_EVALBOX', '')

            logger.info(f"  ➡️ CAS 9: Convocation CMA reçue")
            return result

        # CAS 10: Prêt à payer - Paiement en cours, instruction CMA à venir
        if evalbox_status in READY_TO_PAY:
            result['case'] = 10
            result['case_description'] = "Prêt à payer - Paiement en cours, surveiller emails pour instruction CMA"
            result['should_include_in_response'] = True
            logger.info(f"  ➡️ CAS 10: Prêt à payer ({evalbox_status})")
            return result

        # Note: CAS 8 (deadline passée) est maintenant géré en priorité au début de la fonction
        # pour les statuts PRE_PAYMENT_STATUSES. Ce bloc est un fallback pour les cas edge.

        # CAS 6: Date future + autre statut + deadline pas encore passée
        # La date est DÉJÀ ASSIGNÉE → pas besoin de proposer d'autres dates par défaut
        # Les dates alternatives ne sont pertinentes que si le candidat DEMANDE une date plus tôt
        # (intention WANTS_EARLIER_DATE détectée par le triage)
        result['case'] = 6
        result['case_description'] = "Date future + autre statut - En attente"
        result['should_include_in_response'] = False  # Date déjà assignée, rien à proposer par défaut

        # NOTE: Si le candidat demande explicitement une date plus tôt (intent = WANTS_EARLIER_DATE),
        # le workflow peut appeler get_earlier_dates_other_departments() séparément.
        # On ne charge PAS toutes les dates ici car c'est inutile dans 99% des cas.

        logger.info(f"  ➡️ CAS 6: Date future + autre statut ({evalbox_status})")
        return result


def classify_engagement_level(
    evalbox_status: str,
    date_cloture: str = None,
    examt3p_data: Dict[str, Any] = None
) -> Dict[str, Any]:
    """
    Classifie le niveau d'engagement CMA du candidat pour déterminer si un
    repositionnement de date d'examen est possible.

    Niveaux:
    - 0: Pas de compte ExamT3P → 100% libre
    - 1: Compte créé, pas de paiement → repositionnement possible
    - 2: Dossier Synchronisé + clôture future → repositionnement possible
    - 3: Dossier Synchronisé + clôture passée → inscrit, ne peut plus bouger
    - 4: VALIDE CMA / Convoc CMA reçue → force majeure uniquement

    Args:
        evalbox_status: Statut Evalbox du candidat
        date_cloture: Date de clôture d'inscription (YYYY-MM-DD)
        examt3p_data: Données ExamT3P du candidat

    Returns:
        {level, can_reposition, needs_cma_message, description}
    """
    from src.utils.examt3p_crm_sync import is_date_past

    examt3p_data = examt3p_data or {}
    compte_existe = examt3p_data.get('compte_existe', False)
    evalbox = (evalbox_status or '').strip()

    # Level 4: Validé par la CMA
    if evalbox in VALIDATED:
        return {
            'level': 4,
            'can_reposition': False,
            'needs_cma_message': False,
            'description': 'Dossier validé par la CMA — modification impossible sans force majeure'
        }

    # Level 3: Dossier Synchronisé + clôture passée → inscrit
    if evalbox == 'Dossier Synchronisé' and date_cloture and is_date_past(date_cloture):
        return {
            'level': 3,
            'can_reposition': False,
            'needs_cma_message': False,
            'description': 'Dossier synchronisé et clôture passée — candidat inscrit, modification impossible'
        }

    # Level 2: Dossier Synchronisé + clôture future
    if evalbox == 'Dossier Synchronisé':
        return {
            'level': 2,
            'can_reposition': True,
            'needs_cma_message': True,
            'description': 'Dossier synchronisé, clôture pas encore passée — repositionnement possible avec message CMA'
        }

    # Level 1: Compte ExamT3P existe + statuts pré-paiement
    if compte_existe and evalbox in ['Dossier crée', 'Dossier créé', 'Pret a payer', 'Pret a payer par cheque']:
        return {
            'level': 1,
            'can_reposition': True,
            'needs_cma_message': True,
            'description': 'Compte ExamT3P créé, pas de paiement — repositionnement possible avec message CMA'
        }

    # Level 0: Pas de compte ExamT3P ou statut très précoce
    return {
        'level': 0,
        'can_reposition': True,
        'needs_cma_message': False,
        'description': 'Pas de compte ExamT3P — repositionnement libre'
    }


def extract_departement_from_cma(cma_depot: str) -> Optional[str]:
    """
    Extrait le numéro de département depuis le champ CMA_de_depot.

    Args:
        cma_depot: Valeur du champ CMA_de_depot (ex: "CMA 75", "93", "CMA IDF")

    Returns:
        Numéro de département ou None
    """
    import re

    if not cma_depot:
        return None

    cma_str = str(cma_depot).strip()

    # Chercher un numéro à 2-3 chiffres
    match = re.search(r'\b(\d{2,3})\b', cma_str)
    if match:
        return match.group(1)

    # Mappings connus pour les régions
    region_mapping = {
        'IDF': '75',
        'Ile De France': '75',
        'PACA': '13',
        'Rhone': '69',
        'Lyon': '69',
    }

    for key, value in region_mapping.items():
        if key.lower() in cma_str.lower():
            return value

    return None


def get_exam_session_details(crm_client, session_id: str) -> Optional[Dict[str, Any]]:
    """
    Récupère les détails complets d'une session d'examen.
    """
    from config import settings

    try:
        url = f"{settings.zoho_crm_api_url}/Dates_Examens_VTC_TAXI/{session_id}"
        response = crm_client._make_request("GET", url)
        data = response.get("data", [])
        return data[0] if data else None
    except Exception as e:
        logger.error(f"Erreur récupération session {session_id}: {e}")
        return None


def check_threads_for_exam_not_passed(threads: List[Dict]) -> bool:
    """
    Vérifie dans les threads s'il y a des indices que le candidat n'a pas passé l'examen.

    Patterns recherchés:
    - "je n'ai pas pu passer"
    - "je n'ai pas passé"
    - "absent"
    - "pas présenté"
    - "reporté"
    - etc.
    """
    from src.utils.text_utils import get_clean_thread_content
    import re

    if not threads:
        return False

    patterns = [
        r"n'ai pas pu passer",
        r"n'ai pas passé",
        r"pas présenté",
        r"pas pu me présenter",
        r"absent à l'examen",
        r"j'étais absent",
        r"reporté mon examen",
        r"annulé mon examen",
        r"pas encore passé",
        r"quand est.mon examen",
        r"date de.mon examen",
    ]

    for thread in threads:
        if thread.get('direction') != 'in':
            continue

        content = get_clean_thread_content(thread).lower()

        for pattern in patterns:
            if re.search(pattern, content, re.IGNORECASE):
                logger.info(f"Indice trouvé dans thread: pattern '{pattern}'")
                return True

    return False


# NOTE: Legacy generate_*_message() functions (CAS 1-10) removed.
# Responses are now generated by the template engine (response_master.html + partials).


# =============================================================================
# FILTRAGE INTELLIGENT DES DATES PAR RÉGION
# =============================================================================

# Geography mappings loaded from data/geography.json
import json as _json
from pathlib import Path as _Path

_GEOGRAPHY_FILE = _Path(__file__).parent.parent.parent / 'data' / 'geography.json'
with open(_GEOGRAPHY_FILE, 'r', encoding='utf-8') as _f:
    _geo = _json.load(_f)

DEPT_TO_REGION = _geo['dept_to_region']
CITY_TO_REGION = _geo['city_to_region']
REGION_ALIASES = _geo['region_aliases']

# Mapping inverse : région → liste de départements
REGION_TO_DEPTS = {}
for dept, region in DEPT_TO_REGION.items():
    if region not in REGION_TO_DEPTS:
        REGION_TO_DEPTS[region] = []
    REGION_TO_DEPTS[region].append(dept)

del _geo, _f


def detect_candidate_region(
    text: Optional[str] = None,
    department: Optional[str] = None
) -> Optional[str]:
    """
    Détecte la région du candidat à partir du texte ou du département.

    Ordre de priorité:
    1. Département connu (CRM) → région directe
    2. Mention de région dans le texte
    3. Mention de ville dans le texte

    Args:
        text: Message du candidat (optionnel)
        department: Département du candidat depuis le CRM (optionnel)

    Returns:
        Nom de la région ou None si non détectée
    """
    # 1. Si département connu, retourner directement la région
    if department:
        region = DEPT_TO_REGION.get(str(department))
        if region:
            logger.info(f"  🌍 Région détectée depuis département {department}: {region}")
            return region

    # 2. Chercher dans le texte
    if text:
        text_lower = text.lower()

        # 2a. Chercher une mention directe de région
        for alias, region in REGION_ALIASES.items():
            if alias in text_lower:
                logger.info(f"  🌍 Région détectée depuis texte ('{alias}'): {region}")
                return region

        # 2b. Chercher une mention de ville
        for city, region in CITY_TO_REGION.items():
            if city in text_lower:
                logger.info(f"  🌍 Région détectée depuis ville ('{city}'): {region}")
                return region

    logger.info("  🌍 Aucune région détectée")
    return None


def filter_dates_by_region_relevance(
    all_dates: List[Dict[str, Any]],
    candidate_region: Optional[str] = None,
    candidate_message: Optional[str] = None,
    candidate_department: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Filtre intelligent des dates d'examen basé sur la région du candidat.

    Règles:
    1. Si région détectée:
       - Garder TOUTES les dates de la région du candidat
       - Pour les autres régions: ne garder QUE celles avec une date PLUS TÔT
    2. Si pas de région détectée:
       - Retourner toutes les dates (pas de filtrage)

    Args:
        all_dates: Liste complète des dates d'examen
        candidate_region: Région du candidat (si déjà connue)
        candidate_message: Message du candidat (pour détection automatique)
        candidate_department: Département CRM du candidat

    Returns:
        Liste filtrée des dates pertinentes
    """
    if not all_dates:
        return []

    # Détecter la région si non fournie
    region = candidate_region
    if not region:
        region = detect_candidate_region(
            text=candidate_message,
            department=candidate_department
        )

    # Si pas de région détectée, retourner toutes les dates
    if not region:
        logger.info("  📅 Pas de région détectée → retour de toutes les dates")
        return all_dates

    logger.info(f"  📅 Filtrage intelligent pour la région: {region}")

    # Séparer les dates de la région du candidat vs autres régions
    candidate_region_dates = []
    other_region_dates = []

    for date_info in all_dates:
        dept = str(date_info.get('Departement', ''))
        date_region = DEPT_TO_REGION.get(dept)

        if date_region == region:
            candidate_region_dates.append(date_info)
        else:
            other_region_dates.append(date_info)

    logger.info(f"    → {len(candidate_region_dates)} date(s) dans la région du candidat")
    logger.info(f"    → {len(other_region_dates)} date(s) dans d'autres régions")

    # Trouver la première date d'examen dans la région du candidat
    earliest_candidate_date = None
    if candidate_region_dates:
        candidate_region_dates.sort(key=lambda x: x.get('Date_Examen', '9999-99-99'))
        earliest_candidate_date = candidate_region_dates[0].get('Date_Examen')
        logger.info(f"    → Première date dans {region}: {earliest_candidate_date}")

    # Filtrer les autres régions: ne garder que celles avec une date PLUS TÔT
    filtered_other_dates = []
    if earliest_candidate_date:
        for date_info in other_region_dates:
            exam_date = date_info.get('Date_Examen', '9999-99-99')
            if exam_date < earliest_candidate_date:
                filtered_other_dates.append(date_info)
                dept = date_info.get('Departement', '')
                other_region = DEPT_TO_REGION.get(str(dept), 'Inconnue')
                logger.info(f"    → Date antérieure trouvée: {exam_date} ({other_region})")
    else:
        # Si pas de date dans la région du candidat, garder toutes les autres
        filtered_other_dates = other_region_dates

    # Combiner: dates de la région du candidat + dates antérieures d'autres régions
    result = candidate_region_dates + filtered_other_dates

    # Trier par date d'examen
    result.sort(key=lambda x: x.get('Date_Examen', '9999-99-99'))

    logger.info(f"  ✅ Résultat: {len(result)} date(s) après filtrage intelligent")
    logger.info(f"     ({len(candidate_region_dates)} dans {region} + {len(filtered_other_dates)} antérieures d'autres régions)")

    return result


def search_dates_for_month_and_location(
    crm_client,
    requested_month: int = None,
    requested_location: str = None,
    candidate_region: str = None,
    current_exam_date: str = None
) -> Dict[str, Any]:
    """
    Recherche des dates pour un mois et département spécifiques.
    Si pas trouvé, propose des alternatives intelligentes.

    Args:
        crm_client: Client Zoho CRM
        requested_month: Mois demandé (1-12)
        requested_location: Département demandé (ex: "34", "75")
        candidate_region: Région du candidat (optionnel)
        current_exam_date: Date d'examen actuelle à exclure des alternatives (YYYY-MM-DD)

    Returns:
        {
            'exact_match_dates': [],  # Dates pour le mois/lieu exact
            'same_month_other_depts': [],  # Même mois, autres depts de la région
            'same_dept_other_months': [],  # Même dept, autres mois
            'no_date_for_requested_month': bool,
            'requested_month_name': str,
        }
    """
    result = {
        'exact_match_dates': [],
        'same_month_other_depts': [],
        'same_dept_other_months': [],
        'no_date_for_requested_month': False,
        'requested_month_name': '',
    }

    if requested_month:
        # Nom du mois en français
        mois_fr = ['', 'janvier', 'février', 'mars', 'avril', 'mai', 'juin',
                   'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']
        result['requested_month_name'] = mois_fr[requested_month] if 1 <= requested_month <= 12 else ''

    # Récupérer les dates disponibles
    # Si on connaît le département, chercher directement ses dates (plus précis)
    # Sinon, charger toutes les dates tous départements
    if requested_location:
        all_dates = get_next_exam_dates(crm_client, str(requested_location), limit=10)
        logger.info(f"  📅 Recherche ciblée département {requested_location}: {len(all_dates or [])} date(s)")
    else:
        all_dates = get_next_exam_dates_any_department(crm_client, limit=200)

    if not all_dates:
        return result

    # Déterminer la région du département demandé
    region_depts = []
    if requested_location:
        region = DEPT_TO_REGION.get(str(requested_location))
        if region:
            region_depts = REGION_TO_DEPTS.get(region, [])
    elif candidate_region:
        region_depts = REGION_TO_DEPTS.get(candidate_region, [])

    for date_info in all_dates:
        date_str = date_info.get('Date_Examen', '')
        dept = str(date_info.get('Departement', ''))

        # Exclure la date d'examen actuelle des alternatives
        if current_exam_date and date_str[:10] == current_exam_date[:10]:
            continue

        # Parser le mois de la date
        date_obj = parse_date_flexible(date_str[:10], "exam_date_month")
        if date_obj is None:
            continue
        date_month = date_obj.month

        # Catégoriser
        if requested_month and requested_location:
            if date_month == requested_month and dept == str(requested_location):
                result['exact_match_dates'].append(date_info)
            elif date_month == requested_month and dept in region_depts:
                result['same_month_other_depts'].append(date_info)
            elif dept == str(requested_location):
                result['same_dept_other_months'].append(date_info)
        elif requested_month:
            # Seulement le mois demandé
            if date_month == requested_month:
                result['exact_match_dates'].append(date_info)
        elif requested_location:
            # Seulement le département demandé
            if dept == str(requested_location):
                result['exact_match_dates'].append(date_info)

    # Flag si pas de date exacte
    if (requested_month or requested_location) and not result['exact_match_dates']:
        result['no_date_for_requested_month'] = True

    logger.info(f"  🔍 Recherche dates: mois={requested_month}, dept={requested_location}")
    logger.info(f"     → Exact: {len(result['exact_match_dates'])}, Même mois autres depts: {len(result['same_month_other_depts'])}, Même dept autres mois: {len(result['same_dept_other_months'])}")

    return result
