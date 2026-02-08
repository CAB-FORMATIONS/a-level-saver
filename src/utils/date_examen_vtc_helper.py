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
                try:
                    # Parser la date (format ISO ou datetime)
                    if 'T' in str(date_cloture_str):
                        date_cloture = datetime.fromisoformat(date_cloture_str.replace('Z', '+00:00'))
                        date_cloture = date_cloture.replace(tzinfo=None).date()
                    else:
                        date_cloture = datetime.strptime(str(date_cloture_str), "%Y-%m-%d").date()

                    # Calculer le nombre de jours jusqu'à la clôture
                    days_until_cloture = (date_cloture - today_date).days

                    # Inclure seulement si clôture dans au moins min_days_before_cloture jours
                    if days_until_cloture >= min_days_before_cloture:
                        valid_sessions.append(session)
                except Exception as e:
                    logger.warning(f"Erreur parsing date clôture {date_cloture_str}: {e}")
                    continue

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

        try:
            ref_date = datetime.strptime(str(reference_date), "%Y-%m-%d")
        except Exception as e:
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
            # Vérifier le département
            session_dept = session.get('Departement', '')
            if session_dept == current_departement:
                continue  # Exclure le département actuel

            # Vérifier la date de clôture (doit être dans au moins 2 jours)
            date_cloture_str = session.get('Date_Cloture_Inscription')
            if date_cloture_str:
                try:
                    if 'T' in str(date_cloture_str):
                        date_cloture = datetime.fromisoformat(date_cloture_str.replace('Z', '+00:00'))
                        date_cloture = date_cloture.replace(tzinfo=None).date()
                    else:
                        date_cloture = datetime.strptime(str(date_cloture_str), "%Y-%m-%d").date()

                    days_until_cloture = (date_cloture - today_date).days
                    if days_until_cloture < min_days_before_cloture:
                        continue  # Clôture trop proche ou passée
                except Exception as e:
                    continue
            else:
                continue  # Pas de date de clôture = invalide

            # Vérifier la date d'examen (doit être AVANT la date de référence)
            date_examen_str = session.get('Date_Examen')
            if date_examen_str:
                try:
                    date_examen = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
                    if date_examen >= ref_date:
                        continue  # Pas plus tôt
                    valid_sessions.append(session)
                except Exception as e:
                    continue

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
                try:
                    if 'T' in str(date_cloture_str):
                        date_cloture = datetime.fromisoformat(date_cloture_str.replace('Z', '+00:00'))
                        date_cloture = date_cloture.replace(tzinfo=None).date()
                    else:
                        date_cloture = datetime.strptime(str(date_cloture_str), "%Y-%m-%d").date()

                    # Calculer le nombre de jours jusqu'à la clôture
                    days_until_cloture = (date_cloture - today_date).days

                    # Inclure seulement si clôture dans au moins min_days_before_cloture jours
                    if days_until_cloture >= min_days_before_cloture:
                        valid_sessions.append(session)
                    else:
                        logger.debug(f"  Session exclue: clôture {date_cloture} dans {days_until_cloture} jours (min: {min_days_before_cloture})")
                except Exception as e:
                    continue

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
    try:
        if date_examen and date_examen != 'Date inconnue':
            date_obj = datetime.strptime(str(date_examen), "%Y-%m-%d")
            date_examen_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            date_examen_formatted = date_examen
    except Exception as e:
        date_examen_formatted = date_examen

    # Formater la date de clôture
    try:
        if date_cloture:
            if 'T' in str(date_cloture):
                date_cloture_obj = datetime.fromisoformat(str(date_cloture).replace('Z', '+00:00'))
            else:
                date_cloture_obj = datetime.strptime(str(date_cloture), "%Y-%m-%d")
            date_cloture_formatted = date_cloture_obj.strftime("%d/%m/%Y")
        else:
            date_cloture_formatted = ""
    except Exception as e:
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

    try:
        if 'T' in str(date_str):
            date_obj = datetime.fromisoformat(str(date_str).replace('Z', '+00:00'))
        else:
            date_obj = datetime.strptime(str(date_str), "%Y-%m-%d")

        return date_obj.date() < datetime.now().date()
    except Exception as e:
        return False


def analyze_exam_date_situation(
    deal_data: Dict[str, Any],
    threads: List[Dict] = None,
    crm_client = None,
    examt3p_data: Dict[str, Any] = None,
    session_preference: str = None,
    enriched_lookups: Dict[str, Any] = None
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
            'response_message': str or None,
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
        'response_message': None,
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
                try:
                    session_end = datetime.strptime(session_date_fin, "%Y-%m-%d").date()
                    valid_dates = []
                    for d in all_dates:
                        exam_date_str = d.get('Date_Examen', '')
                        if exam_date_str:
                            exam_date = datetime.strptime(exam_date_str, "%Y-%m-%d").date()
                            if exam_date > session_end:
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
                except Exception as e:
                    logger.error(f"  ❌ Erreur parsing session_date_fin: {e}")
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

        result['response_message'] = generate_propose_dates_message(result['next_dates'], departement)
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
    BLOCKED_STATUSES_FOR_RESCHEDULE = [
        'VALIDE CMA',  # Déjà validé par la CMA, trop tard
        'Convoc CMA reçue',  # Convocation reçue, trop tard
        'Refusé CMA',  # Géré par CAS 3 séparément
    ]

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

        result['response_message'] = generate_deadline_missed_message(
            date_examen_str,
            result['date_cloture'],
            evalbox_status,
            result['next_dates']
        )
        logger.info(f"  ➡️ CAS 8: Deadline passée + {evalbox_status} → Report automatique")
        return result

    # CAS 3: Evalbox = Refusé CMA (prioritaire car peut arriver avec date passée ou future)
    # Statut "Incomplet" sur ExamT3P = certaines pièces refusées par la CMA
    # En cas de refus, le candidat est automatiquement repositionné sur la PROCHAINE date d'examen
    if evalbox_status == 'Refusé CMA':
        result['case'] = 3
        result['case_description'] = "Refusé CMA - Pièces refusées, repositionnement sur prochaine date"
        result['should_include_in_response'] = True

        # Récupérer les pièces refusées depuis ExamT3P (noms + détails)
        if examt3p_data:
            result['pieces_refusees'] = examt3p_data.get('documents_refuses', [])
            # Récupérer les détails complets (nom, motif, solution)
            result['pieces_refusees_details'] = examt3p_data.get('pieces_refusees_details', [])

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
            result['next_dates'] = next_dates

        result['response_message'] = generate_refus_cma_message(
            result['pieces_refusees'],
            next_date_cloture,  # Date clôture de la PROCHAINE session
            result['next_dates'],
            pieces_details=result.get('pieces_refusees_details', [])
        )
        logger.info(f"  ➡️ CAS 3: Refusé CMA - {len(result.get('pieces_refusees', []))} pièce(s) refusée(s)")
        return result

    # CAS avec date dans le passé
    if date_is_past:
        # Statuts validés = dossier vraiment validé par la CMA (candidat a pu passer l'examen)
        # ATTENTION: "Dossier Synchronisé" = en instruction, PAS validé !
        VALIDATED_STATUSES = ['VALIDE CMA', 'Convoc CMA reçue']

        # CAS 7: Date passée + dossier VALIDÉ (examen probablement passé)
        if evalbox_status in VALIDATED_STATUSES:
            result['case'] = 7
            result['case_description'] = "Date passée + dossier validé - Examen probablement passé"

            # Calculer le nombre de jours depuis l'examen
            days_since_exam = None
            if date_examen_str:
                try:
                    date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    days_since_exam = (today - date_obj).days
                    result['days_since_exam'] = days_since_exam
                    # Force majeure possible uniquement si < 14 jours après l'examen
                    result['force_majeure_possible'] = days_since_exam <= 14
                    logger.info(f"  📅 Jours depuis l'examen: {days_since_exam} → force majeure {'possible' if result['force_majeure_possible'] else 'NON possible (> 14 jours)'}")
                except Exception as e:
                    logger.warning(f"  ⚠️ Erreur calcul jours depuis examen: {e}")
                    result['days_since_exam'] = None
                    result['force_majeure_possible'] = False

            # Vérifier s'il y a des indices dans les threads que l'examen n'a pas été passé
            has_indices_not_passed = check_threads_for_exam_not_passed(threads) if threads else False

            if has_indices_not_passed:
                result['should_include_in_response'] = True
                result['response_message'] = generate_clarification_exam_message()
            else:
                result['should_include_in_response'] = False
                result['response_message'] = None

            logger.info(f"  ➡️ CAS 7: Date passée + validé (indices non passé: {has_indices_not_passed})")
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

            result['response_message'] = generate_propose_dates_past_message(result['next_dates'], departement)
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
                try:
                    date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    days_until_exam = (date_obj - today).days
                except Exception as e:
                    pass

            # Si examen dans ≤ 7 jours sans convocation → candidat sera décalé
            # Récupérer la prochaine date d'examen disponible
            next_exam_date = None
            if days_until_exam is not None and days_until_exam <= 7:
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

            result['response_message'] = generate_valide_cma_message(
                date_examen_str,
                next_exam_date=next_exam_date
            )
            logger.info(f"  ➡️ CAS 4: Date future + VALIDE CMA (jours restants: {days_until_exam})")
            return result

        # CAS 5: Date future + Dossier Synchronisé
        if evalbox_status == 'Dossier Synchronisé':
            result['case'] = 5
            result['case_description'] = "Date future + Dossier Synchronisé - Instruction en cours"
            result['should_include_in_response'] = True
            result['response_message'] = generate_dossier_synchronise_message(
                date_examen_str,
                result['date_cloture'],
                result['next_dates']
            )
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

            result['response_message'] = generate_convocation_message(
                date_examen_str,
                identifiant,
                mot_de_passe
            )
            logger.info(f"  ➡️ CAS 9: Convocation CMA reçue")
            return result

        # CAS 10: Prêt à payer - Paiement en cours, instruction CMA à venir
        if evalbox_status in ['Pret a payer', 'Pret a payer par cheque']:
            result['case'] = 10
            result['case_description'] = "Prêt à payer - Paiement en cours, surveiller emails pour instruction CMA"
            result['should_include_in_response'] = True
            result['response_message'] = generate_pret_a_payer_message(
                date_examen_str,
                result['date_cloture']
            )
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
        result['response_message'] = None

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
    if evalbox in ['VALIDE CMA', 'Convoc CMA reçue']:
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


# ================================================================
# GÉNÉRATEURS DE MESSAGES
# ================================================================

def generate_propose_dates_message(next_dates: List[Dict], departement: str) -> str:
    """
    Génère le message proposant les prochaines dates d'examen (CAS 1).
    """
    if not next_dates:
        return """Concernant votre inscription à l'examen VTC, nous n'avons pas encore de date d'examen enregistrée pour votre dossier.

Merci de nous indiquer vos disponibilités afin que nous puissions vous proposer les prochaines dates d'examen disponibles dans votre région."""

    dates_formatted = "\n".join([format_exam_date_for_display(d) for d in next_dates])

    return f"""Concernant votre inscription à l'examen VTC, nous n'avons pas encore de date d'examen enregistrée pour votre dossier.

Voici les prochaines dates d'examen disponibles :

{dates_formatted}

Merci de nous confirmer la date qui vous convient le mieux afin que nous puissions procéder à votre inscription."""


def generate_propose_dates_past_message(next_dates: List[Dict], departement: str) -> str:
    """
    Génère le message proposant les prochaines dates quand la date précédente est passée (CAS 2).
    """
    if not next_dates:
        return """Nous constatons que la date d'examen initialement prévue est maintenant passée et votre dossier n'a pas été validé à temps.

Merci de nous contacter pour que nous puissions vous proposer les prochaines dates d'examen disponibles."""

    dates_formatted = "\n".join([format_exam_date_for_display(d) for d in next_dates])

    return f"""Nous constatons que la date d'examen initialement prévue est maintenant passée.

Pour vous permettre de passer votre examen, voici les prochaines dates disponibles :

{dates_formatted}

Merci de nous confirmer la date qui vous convient afin que nous puissions mettre à jour votre inscription."""


def generate_refus_cma_message(
    pieces_refusees: List[str],
    date_cloture: str,
    next_dates: List[Dict],
    pieces_details: List[Dict] = None
) -> str:
    """
    Génère le message pour informer d'un refus CMA (CAS 3 / statut Incomplet).

    Args:
        pieces_refusees: Liste des noms de pièces refusées
        date_cloture: Date de clôture de la PROCHAINE session d'examen
        next_dates: Prochaine date d'examen (1 seule - positionnement automatique)
        pieces_details: Détails des pièces (nom, motif, solution)

    Le message doit:
    1. Expliquer pourquoi le candidat n'est pas convoqué sur l'examen prévu
    2. Indiquer qu'il est automatiquement repositionné sur la prochaine date
    3. Lister les pièces refusées avec le motif de refus et la solution
    4. Indiquer la date limite pour corriger (clôture de la prochaine session)
    """
    # Formater la date de clôture de la PROCHAINE session
    date_cloture_formatted = ""
    if date_cloture:
        try:
            if 'T' in str(date_cloture):
                date_obj = datetime.fromisoformat(str(date_cloture).replace('Z', '+00:00'))
            else:
                date_obj = datetime.strptime(str(date_cloture), "%Y-%m-%d")
            date_cloture_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_cloture_formatted = str(date_cloture)

    # Formater la prochaine date d'examen (UNE SEULE - positionnement automatique)
    next_exam_text = ""
    next_exam_date_formatted = ""
    if next_dates and len(next_dates) > 0:
        next_exam = next_dates[0]
        date_examen = next_exam.get('Date_Examen', '')
        if date_examen:
            try:
                date_obj = datetime.strptime(str(date_examen), "%Y-%m-%d")
                next_exam_date_formatted = date_obj.strftime("%d/%m/%Y")
            except Exception as e:
                next_exam_date_formatted = str(date_examen)

    # Formater les pièces refusées avec détails
    pieces_text = ""
    if pieces_details and len(pieces_details) > 0:
        # Utiliser les détails complets (motif + solution)
        pieces_lines = []
        for piece in pieces_details:
            nom = piece.get('nom', 'Document')
            motif = piece.get('motif', 'Motif non précisé')
            solution = piece.get('solution', 'Veuillez fournir un nouveau document conforme.')

            pieces_lines.append(f"""**📄 {nom}**
   ❌ **Motif du refus** : {motif}
   ✅ **Solution** : {solution}""")

        pieces_list = "\n\n".join(pieces_lines)
        pieces_text = f"""**🔴 Pièce(s) refusée(s) par la CMA :**

{pieces_list}

"""
    elif pieces_refusees and len(pieces_refusees) > 0:
        # Fallback: juste les noms (ancien format)
        pieces_list = "\n".join([f"• {piece}" for piece in pieces_refusees])
        pieces_text = f"""**🔴 Pièce(s) refusée(s) par la CMA :**

{pieces_list}

"""
    else:
        # Aucune pièce identifiée - demander vérification sur ExamT3P
        pieces_text = """**🔴 Des pièces de votre dossier ont été refusées par la CMA.**

Pour connaître les pièces concernées, connectez-vous sur votre espace ExamT3P et consultez la section "Mes Documents".

"""

    # Construire le message selon les informations disponibles
    date_cloture_text = f"**avant le {date_cloture_formatted}**" if date_cloture_formatted else "**dans les plus brefs délais**"
    next_exam_info = f" du **{next_exam_date_formatted}**" if next_exam_date_formatted else ""

    return f"""**⚠️ Information importante concernant votre inscription à l'examen VTC**

Nous vous informons que la CMA (Chambre des Métiers et de l'Artisanat) a refusé certaines pièces de votre dossier. **C'est pour cette raison que vous n'avez pas reçu de convocation** pour l'examen initialement prévu.

{pieces_text}**📅 Votre nouvelle date d'examen :**

Votre inscription a été **automatiquement reportée** sur la prochaine session d'examen{next_exam_info}.

**⏰ Que devez-vous faire maintenant ?**

Pour être convoqué sur cette nouvelle date, vous devez nous transmettre vos documents corrigés {date_cloture_text} (date de clôture des inscriptions).

📧 Vous pouvez :
• Nous envoyer vos documents par **retour de mail**
• Ou les télécharger directement sur votre **espace ExamT3P**

⚠️ **Important** : Si les documents corrigés ne sont pas reçus avant la date de clôture, votre inscription sera à nouveau reportée sur la session suivante.

Nous restons à votre disposition pour toute question."""


def generate_valide_cma_message(date_examen_str: str, next_exam_date: Optional[Dict] = None) -> str:
    """
    Génère le message pour un dossier validé CMA (CAS 4).

    Adapte le message selon la proximité de l'examen:
    - > 10 jours: "vous recevrez la convocation ~10j avant"
    - 7-10 jours: "la convocation devrait être arrivée, vérifiez vos spams"
    - ≤ 7 jours sans convocation: "report automatique sur prochaine date"

    Args:
        date_examen_str: Date d'examen actuelle
        next_exam_date: Prochaine date d'examen si report nécessaire
    """
    date_formatted = ""
    days_until_exam = None

    if date_examen_str:
        try:
            date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
            date_formatted = date_obj.strftime("%d/%m/%Y")
            # Calculer le nombre de jours jusqu'à l'examen
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            days_until_exam = (date_obj - today).days
        except Exception as e:
            date_formatted = str(date_examen_str)

    date_text = f" du {date_formatted}" if date_formatted else ""

    # CAS CRITIQUE: Examen dans ≤ 7 jours = report automatique par la CMA
    if days_until_exam is not None and days_until_exam <= 7:
        # Formater la prochaine date d'examen
        next_date_formatted = ""
        if next_exam_date:
            try:
                next_date_str = next_exam_date.get('Date_Examen', '')
                if next_date_str:
                    next_date_obj = datetime.strptime(str(next_date_str), "%Y-%m-%d")
                    next_date_formatted = next_date_obj.strftime("%d/%m/%Y")
            except Exception as e:
                pass

        next_date_text = f" du **{next_date_formatted}**" if next_date_formatted else " (date à confirmer)"

        return f"""Votre dossier a été validé par la CMA.

**Information importante concernant votre examen :**

La CMA envoie les convocations au minimum **7 jours avant** la date d'examen. Or, l'examen initialement prévu{date_text} est dans moins de 7 jours et vous n'avez pas encore reçu de convocation.

Cela signifie que la CMA, en raison de ses **délais de traitement importants**, n'a pas pu finaliser votre convocation à temps pour cette session.

**Ne vous inquiétez pas !** Votre dossier reste validé et vous serez **automatiquement convoqué(e) pour la prochaine session d'examen**{next_date_text}.

Vous recevrez votre convocation officielle environ 7 à 10 jours avant cette nouvelle date. Pensez à vérifier régulièrement vos spams.

En attendant, nous vous conseillons de continuer à bien préparer votre examen. N'hésitez pas à nous contacter si vous avez des questions."""

    # Examen entre 7 et 10 jours - convocation devrait être arrivée
    if days_until_exam is not None and days_until_exam <= 10:
        return f"""Bonne nouvelle ! Votre dossier a été validé par la CMA pour l'examen{date_text}.

**Concernant votre convocation :**
La convocation officielle est généralement envoyée par la CMA environ 7 à 10 jours avant l'examen. Elle devrait donc **déjà être arrivée** dans votre boîte mail.

📧 **Vérifiez impérativement vos spams et courriers indésirables**, car il arrive fréquemment que les emails de la CMA s'y retrouvent.

Si vous n'avez toujours pas reçu votre convocation après avoir vérifié vos spams, merci de nous le signaler rapidement afin que nous puissions contacter la CMA.

En attendant, nous vous conseillons de bien préparer votre examen. N'hésitez pas à nous contacter si vous avez des questions."""
    else:
        # Examen dans plus de 10 jours
        return f"""Bonne nouvelle ! Votre dossier a été validé par la CMA pour l'examen{date_text}.

Vous recevrez votre convocation officielle environ 10 jours avant la date de l'examen. Cette convocation vous sera envoyée directement par la CMA à l'adresse email que vous avez renseignée.

📧 **Pensez à vérifier régulièrement vos spams et courriers indésirables**, car il arrive que les emails de la CMA s'y retrouvent.

En attendant, nous vous conseillons de bien préparer votre examen. N'hésitez pas à nous contacter si vous avez des questions."""


def generate_dossier_synchronise_message(
    date_examen_str: str,
    date_cloture: str,
    next_dates: List[Dict]
) -> str:
    """
    Génère le message pour un dossier synchronisé (en cours d'instruction) (CAS 5).
    """
    date_formatted = ""
    if date_examen_str:
        try:
            date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
            date_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_formatted = str(date_examen_str)

    date_cloture_formatted = ""
    if date_cloture:
        try:
            if 'T' in str(date_cloture):
                date_obj = datetime.fromisoformat(str(date_cloture).replace('Z', '+00:00'))
            else:
                date_obj = datetime.strptime(str(date_cloture), "%Y-%m-%d")
            date_cloture_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_cloture_formatted = str(date_cloture)

    date_text = f" du {date_formatted}" if date_formatted else ""
    cloture_text = f" avant le {date_cloture_formatted}" if date_cloture_formatted else " rapidement"

    return f"""Votre dossier a bien été transmis à la CMA pour l'examen{date_text} et est actuellement en cours d'instruction.

**Important :** Pendant cette période, la CMA peut vous demander des corrections ou des pièces complémentaires. Nous vous conseillons de surveiller attentivement vos emails (y compris les spams).

Si la CMA refuse certains documents, vous devrez nous transmettre les corrections{cloture_text} pour que votre inscription soit maintenue sur cette date d'examen. Dans le cas contraire, votre dossier sera automatiquement décalé sur la prochaine session disponible.

N'hésitez pas à nous contacter si vous recevez une demande de la CMA."""


def generate_clarification_exam_message() -> str:
    """
    Génère le message demandant clarification sur le passage de l'examen (CAS 7).
    """
    return """Nous constatons que la date de votre examen est passée. Votre dossier avait été validé par la CMA.

Pourriez-vous nous confirmer si vous avez bien pu passer votre examen ?

Si ce n'est pas le cas, merci de nous en informer afin que nous puissions vous proposer une nouvelle date d'inscription."""


def generate_deadline_missed_message(
    date_examen_str: str,
    date_cloture: str,
    evalbox_status: str,
    next_dates: List[Dict]
) -> str:
    """
    Génère le message informant que la deadline est passée et le candidat sera reporté (CAS 8).

    Ce cas se produit quand:
    - La date d'examen est dans le futur
    - MAIS la date de clôture des inscriptions est passée
    - ET le dossier n'a pas été validé (Evalbox ≠ VALIDE CMA/Dossier Synchronisé)

    Conséquence: Le candidat a raté la deadline et sera automatiquement reporté
    sur la prochaine session disponible.
    """
    # Formater la date d'examen
    date_examen_formatted = ""
    if date_examen_str:
        try:
            date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
            date_examen_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_examen_formatted = str(date_examen_str)

    # Formater la date de clôture
    date_cloture_formatted = ""
    if date_cloture:
        try:
            if 'T' in str(date_cloture):
                date_obj = datetime.fromisoformat(str(date_cloture).replace('Z', '+00:00'))
            else:
                date_obj = datetime.strptime(str(date_cloture), "%Y-%m-%d")
            date_cloture_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_cloture_formatted = str(date_cloture)

    date_examen_text = f" du {date_examen_formatted}" if date_examen_formatted else ""
    date_cloture_text = f" (clôturées le {date_cloture_formatted})" if date_cloture_formatted else ""

    # Formater les prochaines dates
    next_dates_text = ""
    if next_dates:
        dates_formatted = "\n".join([format_exam_date_for_display(d) for d in next_dates])
        next_dates_text = f"""

Voici les prochaines dates d'examen disponibles :

{dates_formatted}

Merci de nous confirmer la date qui vous convient afin que nous puissions vous inscrire sur cette nouvelle session."""
    else:
        next_dates_text = """

Nous allons vous recontacter rapidement pour vous proposer les prochaines dates disponibles."""

    return f"""Nous vous informons que les inscriptions pour l'examen{date_examen_text} sont maintenant clôturées{date_cloture_text}.

Votre dossier n'ayant pas été validé avant cette date limite, vous ne pourrez malheureusement pas passer l'examen sur cette session. Votre inscription sera automatiquement reportée sur la prochaine session disponible.{next_dates_text}"""


def generate_convocation_message(
    date_examen_str: str,
    identifiant: str,
    mot_de_passe: str
) -> str:
    """
    Génère le message pour informer que la convocation est disponible (CAS 9).

    Contenu:
    - Convocation disponible sur ExamT3P
    - Lien vers la plateforme
    - Identifiants de connexion
    - Instructions: télécharger, imprimer, pièce d'identité
    - Souhait de bonne chance
    """
    # Formater la date d'examen
    date_formatted = ""
    if date_examen_str:
        try:
            date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
            date_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_formatted = str(date_examen_str)

    date_text = f" du **{date_formatted}**" if date_formatted else ""

    # Construire la section identifiants
    identifiants_text = ""
    if identifiant and mot_de_passe:
        identifiants_text = f"""
**Vos identifiants de connexion :**
- Identifiant : **{identifiant}**
- Mot de passe : **{mot_de_passe}**
"""
    elif identifiant:
        identifiants_text = f"""
**Votre identifiant de connexion :** {identifiant}
(Si vous avez oublié votre mot de passe, utilisez la fonction "Mot de passe oublié" sur la plateforme)
"""
    else:
        identifiants_text = """
(Vos identifiants vous ont été communiqués lors de la création de votre compte. Si vous les avez oubliés, utilisez la fonction "Mot de passe oublié" sur la plateforme)
"""

    return f"""Excellente nouvelle ! Votre convocation pour l'examen VTC{date_text} est maintenant disponible !

**Pour récupérer votre convocation :**

1. Connectez-vous sur la plateforme ExamT3P : **https://www.exament3p.fr**
{identifiants_text}
2. Une fois connecté, téléchargez votre convocation officielle

3. **Imprimez votre convocation** - elle est obligatoire le jour de l'examen

**Le jour de l'examen, présentez-vous avec :**
- Votre convocation imprimée
- Une pièce d'identité en cours de validité (carte d'identité ou passeport)

Nous vous souhaitons bonne chance pour votre examen ! Nous restons à votre disposition si vous avez des questions."""


def generate_pret_a_payer_message(
    date_examen_str: str,
    date_cloture: str
) -> str:
    """
    Génère le message pour informer que le paiement est en cours (CAS 10).

    Contenu:
    - Paiement des frais d'examen en cours (prochaines heures/jours)
    - Une fois payé, la CMA va instruire les pièces
    - Surveiller emails + spams pour notifications CMA
    - Si refus de pièces → corriger avant date clôture
    - Sinon → décalage date examen
    """
    # Formater la date d'examen
    date_examen_formatted = ""
    if date_examen_str:
        try:
            date_obj = datetime.strptime(str(date_examen_str), "%Y-%m-%d")
            date_examen_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_examen_formatted = str(date_examen_str)

    # Formater la date de clôture
    date_cloture_formatted = ""
    if date_cloture:
        try:
            if 'T' in str(date_cloture):
                date_obj = datetime.fromisoformat(str(date_cloture).replace('Z', '+00:00'))
            else:
                date_obj = datetime.strptime(str(date_cloture), "%Y-%m-%d")
            date_cloture_formatted = date_obj.strftime("%d/%m/%Y")
        except Exception as e:
            date_cloture_formatted = str(date_cloture)

    date_examen_text = f" du **{date_examen_formatted}**" if date_examen_formatted else ""
    date_cloture_text = f"**{date_cloture_formatted}**" if date_cloture_formatted else "la date de clôture des inscriptions"

    return f"""Votre dossier est complet et prêt pour le paiement des frais d'examen !

Nous allons procéder au règlement des frais d'inscription dans les **prochaines heures/jours**.

**Ce qui va se passer ensuite :**

1. Une fois le paiement effectué, votre dossier sera transmis à la **CMA (Chambre des Métiers et de l'Artisanat)** pour instruction

2. La CMA va examiner vos pièces justificatives

3. **Important - Surveillez vos emails (et vos spams !)** : Si la CMA refuse certaines pièces, vous recevrez une notification par email vous demandant de les corriger

4. En cas de demande de correction, vous devrez nous transmettre les documents corrigés **avant le {date_cloture_text}**

**Attention :** Si les corrections ne sont pas apportées avant la date de clôture, votre inscription sera automatiquement reportée sur la prochaine session d'examen.

Votre examen est prévu pour le{date_examen_text}. Nous restons à votre disposition pour toute question."""


# =============================================================================
# FILTRAGE INTELLIGENT DES DATES PAR RÉGION
# =============================================================================

# Mapping département → région (pour toute la France métropolitaine)
DEPT_TO_REGION = {
    # Auvergne-Rhône-Alpes
    '01': 'Auvergne-Rhône-Alpes', '03': 'Auvergne-Rhône-Alpes', '07': 'Auvergne-Rhône-Alpes',
    '15': 'Auvergne-Rhône-Alpes', '26': 'Auvergne-Rhône-Alpes', '38': 'Auvergne-Rhône-Alpes',
    '42': 'Auvergne-Rhône-Alpes', '43': 'Auvergne-Rhône-Alpes', '63': 'Auvergne-Rhône-Alpes',
    '69': 'Auvergne-Rhône-Alpes', '73': 'Auvergne-Rhône-Alpes', '74': 'Auvergne-Rhône-Alpes',
    # Bourgogne-Franche-Comté
    '21': 'Bourgogne-Franche-Comté', '25': 'Bourgogne-Franche-Comté', '39': 'Bourgogne-Franche-Comté',
    '58': 'Bourgogne-Franche-Comté', '70': 'Bourgogne-Franche-Comté', '71': 'Bourgogne-Franche-Comté',
    '89': 'Bourgogne-Franche-Comté', '90': 'Bourgogne-Franche-Comté',
    # Bretagne
    '22': 'Bretagne', '29': 'Bretagne', '35': 'Bretagne', '56': 'Bretagne',
    # Centre-Val de Loire
    '18': 'Centre-Val de Loire', '28': 'Centre-Val de Loire', '36': 'Centre-Val de Loire',
    '37': 'Centre-Val de Loire', '41': 'Centre-Val de Loire', '45': 'Centre-Val de Loire',
    # Grand Est
    '08': 'Grand Est', '10': 'Grand Est', '51': 'Grand Est', '52': 'Grand Est',
    '54': 'Grand Est', '55': 'Grand Est', '57': 'Grand Est', '67': 'Grand Est',
    '68': 'Grand Est', '88': 'Grand Est',
    # Hauts-de-France
    '02': 'Hauts-de-France', '59': 'Hauts-de-France', '60': 'Hauts-de-France',
    '62': 'Hauts-de-France', '80': 'Hauts-de-France',
    # Île-de-France
    '75': 'Île-de-France', '77': 'Île-de-France', '78': 'Île-de-France',
    '91': 'Île-de-France', '92': 'Île-de-France', '93': 'Île-de-France',
    '94': 'Île-de-France', '95': 'Île-de-France',
    # Normandie
    '14': 'Normandie', '27': 'Normandie', '50': 'Normandie', '61': 'Normandie', '76': 'Normandie',
    # Nouvelle-Aquitaine
    '16': 'Nouvelle-Aquitaine', '17': 'Nouvelle-Aquitaine', '19': 'Nouvelle-Aquitaine',
    '23': 'Nouvelle-Aquitaine', '24': 'Nouvelle-Aquitaine', '33': 'Nouvelle-Aquitaine',
    '40': 'Nouvelle-Aquitaine', '47': 'Nouvelle-Aquitaine', '64': 'Nouvelle-Aquitaine',
    '79': 'Nouvelle-Aquitaine', '86': 'Nouvelle-Aquitaine', '87': 'Nouvelle-Aquitaine',
    # Occitanie
    '09': 'Occitanie', '11': 'Occitanie', '12': 'Occitanie', '30': 'Occitanie',
    '31': 'Occitanie', '32': 'Occitanie', '34': 'Occitanie', '46': 'Occitanie',
    '48': 'Occitanie', '65': 'Occitanie', '66': 'Occitanie', '81': 'Occitanie', '82': 'Occitanie',
    # Pays de la Loire
    '44': 'Pays de la Loire', '49': 'Pays de la Loire', '53': 'Pays de la Loire',
    '72': 'Pays de la Loire', '85': 'Pays de la Loire',
    # PACA
    '04': 'PACA', '05': 'PACA', '06': 'PACA', '13': 'PACA', '83': 'PACA', '84': 'PACA',
}

# Mapping inverse : région → liste de départements
REGION_TO_DEPTS = {}
for dept, region in DEPT_TO_REGION.items():
    if region not in REGION_TO_DEPTS:
        REGION_TO_DEPTS[region] = []
    REGION_TO_DEPTS[region].append(dept)

# Mapping villes principales → région (pour détection dans le texte)
CITY_TO_REGION = {
    # Pays de la Loire
    'nantes': 'Pays de la Loire', 'angers': 'Pays de la Loire', 'le mans': 'Pays de la Loire',
    'laval': 'Pays de la Loire', 'la roche-sur-yon': 'Pays de la Loire', 'saint-nazaire': 'Pays de la Loire',
    # Île-de-France
    'paris': 'Île-de-France', 'versailles': 'Île-de-France', 'boulogne': 'Île-de-France',
    'montreuil': 'Île-de-France', 'saint-denis': 'Île-de-France', 'argenteuil': 'Île-de-France',
    'creteil': 'Île-de-France', 'créteil': 'Île-de-France', 'bobigny': 'Île-de-France',
    # PACA
    'marseille': 'PACA', 'nice': 'PACA', 'toulon': 'PACA', 'aix-en-provence': 'PACA',
    'avignon': 'PACA', 'cannes': 'PACA', 'antibes': 'PACA',
    # Auvergne-Rhône-Alpes
    'lyon': 'Auvergne-Rhône-Alpes', 'grenoble': 'Auvergne-Rhône-Alpes', 'saint-etienne': 'Auvergne-Rhône-Alpes',
    'clermont-ferrand': 'Auvergne-Rhône-Alpes', 'annecy': 'Auvergne-Rhône-Alpes', 'valence': 'Auvergne-Rhône-Alpes',
    # Occitanie
    'toulouse': 'Occitanie', 'montpellier': 'Occitanie', 'nîmes': 'Occitanie', 'nimes': 'Occitanie',
    'perpignan': 'Occitanie', 'béziers': 'Occitanie', 'beziers': 'Occitanie',
    # Nouvelle-Aquitaine
    'bordeaux': 'Nouvelle-Aquitaine', 'limoges': 'Nouvelle-Aquitaine', 'poitiers': 'Nouvelle-Aquitaine',
    'pau': 'Nouvelle-Aquitaine', 'la rochelle': 'Nouvelle-Aquitaine', 'angoulême': 'Nouvelle-Aquitaine',
    # Grand Est
    'strasbourg': 'Grand Est', 'reims': 'Grand Est', 'metz': 'Grand Est', 'nancy': 'Grand Est',
    'mulhouse': 'Grand Est', 'colmar': 'Grand Est', 'troyes': 'Grand Est',
    # Hauts-de-France
    'lille': 'Hauts-de-France', 'amiens': 'Hauts-de-France', 'roubaix': 'Hauts-de-France',
    'tourcoing': 'Hauts-de-France', 'dunkerque': 'Hauts-de-France',
    # Bretagne
    'rennes': 'Bretagne', 'brest': 'Bretagne', 'quimper': 'Bretagne', 'lorient': 'Bretagne',
    'vannes': 'Bretagne', 'saint-brieuc': 'Bretagne',
    # Normandie
    'rouen': 'Normandie', 'le havre': 'Normandie', 'caen': 'Normandie', 'cherbourg': 'Normandie',
    # Centre-Val de Loire
    'orléans': 'Centre-Val de Loire', 'orleans': 'Centre-Val de Loire', 'tours': 'Centre-Val de Loire',
    'bourges': 'Centre-Val de Loire', 'chartres': 'Centre-Val de Loire',
    # Bourgogne-Franche-Comté
    'dijon': 'Bourgogne-Franche-Comté', 'besançon': 'Bourgogne-Franche-Comté', 'besancon': 'Bourgogne-Franche-Comté',
    'belfort': 'Bourgogne-Franche-Comté', 'auxerre': 'Bourgogne-Franche-Comté',
}

# Alias de régions (pour détection dans le texte)
REGION_ALIASES = {
    'pays de la loire': 'Pays de la Loire',
    'pays-de-la-loire': 'Pays de la Loire',
    'pdl': 'Pays de la Loire',
    'ile de france': 'Île-de-France',
    'ile-de-france': 'Île-de-France',
    'idf': 'Île-de-France',
    'région parisienne': 'Île-de-France',
    'region parisienne': 'Île-de-France',
    'paca': 'PACA',
    'provence': 'PACA',
    'côte d\'azur': 'PACA',
    'cote d\'azur': 'PACA',
    'rhône-alpes': 'Auvergne-Rhône-Alpes',
    'rhone-alpes': 'Auvergne-Rhône-Alpes',
    'auvergne': 'Auvergne-Rhône-Alpes',
    'grand est': 'Grand Est',
    'alsace': 'Grand Est',
    'lorraine': 'Grand Est',
    'champagne': 'Grand Est',
    'occitanie': 'Occitanie',
    'languedoc': 'Occitanie',
    'midi-pyrénées': 'Occitanie',
    'midi-pyrenees': 'Occitanie',
    'nouvelle-aquitaine': 'Nouvelle-Aquitaine',
    'aquitaine': 'Nouvelle-Aquitaine',
    'bretagne': 'Bretagne',
    'normandie': 'Normandie',
    'hauts-de-france': 'Hauts-de-France',
    'nord': 'Hauts-de-France',
    'picardie': 'Hauts-de-France',
    'centre': 'Centre-Val de Loire',
    'bourgogne': 'Bourgogne-Franche-Comté',
    'franche-comté': 'Bourgogne-Franche-Comté',
    'franche-comte': 'Bourgogne-Franche-Comté',
}


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

    # Récupérer toutes les dates disponibles
    all_dates = get_next_exam_dates_any_department(crm_client, limit=50)

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
        try:
            date_obj = datetime.strptime(date_str[:10], '%Y-%m-%d')
            date_month = date_obj.month
        except Exception:
            continue

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
