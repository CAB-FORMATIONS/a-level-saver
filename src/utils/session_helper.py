"""
Helper pour gérer les sessions de formation et leur association avec les dates d'examen.

Logique métier:
1. Les sessions de formation doivent se terminer AVANT la date d'examen
2. On privilégie les sessions dont la Date_fin est la plus proche de la date d'examen
3. Convention de nommage: cdj-* = Cours Du Jour, cds-* = Cours Du Soir
4. On propose toujours une option CDJ et une option CDS sauf si préférence connue
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any, Tuple

logger = logging.getLogger(__name__)

from src.constants.sessions import SESSION_TYPE_JOUR, SESSION_TYPE_SOIR, is_uber_visio_session
from src.utils.date_utils import parse_date_flexible, parse_datetime_flexible
from src.constants.thresholds import SESSION_MIN_DAYS_BEFORE_EXAM as MIN_DAYS_BEFORE_EXAM, SESSION_MAX_DAYS_BEFORE_EXAM as MAX_DAYS_BEFORE_EXAM


def get_sessions_for_exam_date(
    crm_client,
    exam_date: str,
    session_type: Optional[str] = None,
    limit: int = 2
) -> List[Dict[str, Any]]:
    """
    Récupère les sessions de formation adaptées pour une date d'examen donnée.

    La session doit se terminer AVANT la date d'examen, idéalement proche.

    Args:
        crm_client: Client Zoho CRM
        exam_date: Date d'examen au format YYYY-MM-DD
        session_type: Type de session souhaité ('cdj', 'cds', ou None pour les deux)
        limit: Nombre de sessions à retourner par type

    Returns:
        Liste des sessions avec leurs infos
    """
    from config import settings

    logger.info(f"🔍 Recherche des sessions pour l'examen du {exam_date}")

    try:
        # Parser la date d'examen
        exam_date_obj = parse_datetime_flexible(exam_date, "exam_date")
        if exam_date_obj is None:
            logger.warning(f"Impossible de parser la date d'examen: '{exam_date}'")
            return []
        today = datetime.now()
        today_str = today.strftime('%Y-%m-%d')

        # Calculer la plage de dates pour la fin de formation
        # La session doit se terminer entre (exam - MAX_DAYS) et (exam - MIN_DAYS)
        min_end_date = exam_date_obj - timedelta(days=MAX_DAYS_BEFORE_EXAM)
        max_end_date = exam_date_obj - timedelta(days=MIN_DAYS_BEFORE_EXAM)

        # Permettre les sessions commencées depuis moins de 3 jours (confirmation tardive)
        three_days_ago = today - timedelta(days=3)
        three_days_ago_str = three_days_ago.strftime('%Y-%m-%d')

        logger.info(f"  Recherche sessions se terminant entre {min_end_date.strftime('%Y-%m-%d')} et {max_end_date.strftime('%Y-%m-%d')}")
        logger.info(f"  Filtrage: Date_debut >= {three_days_ago_str} (sessions non commencées ou commencées < 3j)")
        logger.info(f"  Filtrage: Lieu_de_formation = VISIO Zoom VTC (sessions Uber uniquement)")

        # Rechercher les sessions planifiées
        url = f"{settings.zoho_crm_api_url}/Sessions1/search"

        # Critère:
        # - Date_fin dans la plage (proche de l'examen)
        # - Date_debut >= (aujourd'hui - 3 jours) pour inclure sessions récemment commencées
        # Note: Filtrage Lieu_de_formation = VISIO Zoom VTC fait en Python après récupération
        criteria = (
            f"((Date_fin:greater_equal:{min_end_date.strftime('%Y-%m-%d')})"
            f"and(Date_fin:less_equal:{max_end_date.strftime('%Y-%m-%d')})"
            f"and(Date_d_but:greater_equal:{three_days_ago_str}))"
        )

        # Pagination - augmentée pour couvrir tous les cas
        all_sessions = []
        page = 1
        max_pages = 20  # 20 pages × 200 = 4000 sessions max

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
            logger.info(f"  Page {page}: {len(sessions)} session(s) récupérée(s)")

            if len(sessions) < 200:
                break

            page += 1

        if not all_sessions:
            logger.warning(f"Aucune session trouvée pour l'examen du {exam_date}")
            return []

        logger.info(f"  Total: {len(all_sessions)} session(s) trouvée(s) (avant filtrage Lieu)")

        # Filtrer par Lieu_de_formation = VISIO Zoom VTC (sessions Uber uniquement)
        uber_sessions = []
        for session in all_sessions:
            lieu = session.get('Lieu_de_formation')
            lieu_name = ""
            if isinstance(lieu, dict):
                lieu_name = lieu.get('name', '')
            elif lieu:
                lieu_name = str(lieu)

            # Garder uniquement les sessions VISIO Zoom VTC
            if is_uber_visio_session(lieu_name):
                uber_sessions.append(session)
                logger.debug(f"  Session Uber: {session.get('Name')} - Lieu: {lieu_name}")
            else:
                logger.debug(f"  Session ignorée (lieu={lieu_name}): {session.get('Name')}")

        if not uber_sessions:
            # Debug: lister les lieux trouvés
            lieux_trouves = set()
            for s in all_sessions[:10]:  # Limiter à 10 pour le log
                lieu = s.get('Lieu_de_formation')
                if isinstance(lieu, dict):
                    lieux_trouves.add(lieu.get('name', 'N/A'))
                elif lieu:
                    lieux_trouves.add(str(lieu))
            logger.warning(f"Aucune session Uber (VISIO Zoom VTC) trouvée pour l'examen du {exam_date}")
            logger.warning(f"  Lieux trouvés dans les {len(all_sessions)} sessions: {lieux_trouves}")
            return []

        logger.info(f"  ✅ {len(uber_sessions)} session(s) Uber (VISIO Zoom VTC)")

        # Filtrer et catégoriser par type (CDJ/CDS)
        sessions_jour = []
        sessions_soir = []

        for session in uber_sessions:
            session_name = session.get('Name', '').lower()
            date_fin = session.get('Date_fin', '')

            # Calculer la distance avec l'examen
            if date_fin:
                date_fin_obj = parse_datetime_flexible(date_fin, "date_fin")
                if date_fin_obj is not None:
                    days_before_exam = (exam_date_obj - date_fin_obj).days
                    session['days_before_exam'] = days_before_exam
                else:
                    logger.warning(f"Erreur parsing date_fin '{date_fin}'")
                    session['days_before_exam'] = 999

            # Marquer les sessions déjà commencées (Date_debut < aujourd'hui)
            session_start = session.get('Date_d_but', '')
            if session_start and session_start < today_str:
                session['already_started'] = True

            # Catégoriser par type
            if session_name.startswith(SESSION_TYPE_JOUR):
                session['session_type'] = 'jour'
                session['session_type_label'] = 'Cours du jour'
                sessions_jour.append(session)
            elif session_name.startswith(SESSION_TYPE_SOIR):
                session['session_type'] = 'soir'
                session['session_type_label'] = 'Cours du soir'
                sessions_soir.append(session)

        # Trier par proximité avec l'examen (Date_fin la plus proche de l'examen)
        sessions_jour.sort(key=lambda x: x.get('days_before_exam', 999))
        sessions_soir.sort(key=lambda x: x.get('days_before_exam', 999))

        # Retourner selon le type demandé
        result = []

        if session_type == SESSION_TYPE_JOUR or session_type == 'jour':
            result = sessions_jour[:limit]
            if not result and sessions_soir:
                # Aucune session jour disponible → proposer le soir comme alternative
                result = sessions_soir[:limit]
                logger.info(f"⚠️ Aucune session jour → fallback sur {len(result)} session(s) soir comme alternative")
        elif session_type == SESSION_TYPE_SOIR or session_type == 'soir':
            result = sessions_soir[:limit]
            if not result and sessions_jour:
                # Aucune session soir disponible → proposer le jour comme alternative
                result = sessions_jour[:limit]
                logger.info(f"⚠️ Aucune session soir → fallback sur {len(result)} session(s) jour comme alternative")
        else:
            # Retourner les deux types
            if sessions_jour:
                result.append(sessions_jour[0])
            if sessions_soir:
                result.append(sessions_soir[0])

        logger.info(f"✅ {len(result)} session(s) sélectionnée(s) pour l'examen du {exam_date}")
        return result

    except Exception as e:
        logger.error(f"❌ Erreur lors de la recherche des sessions: {e}")
        return []


def get_sessions_for_multiple_exam_dates(
    crm_client,
    exam_dates: List[Dict[str, Any]],
    session_type: Optional[str] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Récupère les sessions de formation pour plusieurs dates d'examen.

    Args:
        crm_client: Client Zoho CRM
        exam_dates: Liste des dates d'examen (retournées par get_next_exam_dates)
        session_type: Type de session souhaité ('jour', 'soir', ou None pour les deux)

    Returns:
        Dict avec date_examen comme clé et liste de sessions comme valeur
    """
    result = {}

    for exam_info in exam_dates:
        exam_date = exam_info.get('Date_Examen')
        if exam_date:
            sessions = get_sessions_for_exam_date(crm_client, exam_date, session_type)
            result[exam_date] = {
                'exam_info': exam_info,
                'sessions': sessions
            }

    return result


def format_session_for_display(session: Dict[str, Any]) -> str:
    """
    Formate une session pour affichage au candidat.

    Args:
        session: Données de la session

    Returns:
        Texte formaté pour le candidat
    """
    name = session.get('Name', 'Session inconnue')
    date_debut = session.get('Date_d_but', '')
    date_fin = session.get('Date_fin', '')
    type_cours = session.get('Type_de_cours', '')
    session_type_label = session.get('session_type_label', '')
    days_before = session.get('days_before_exam', 0)

    # Formater les dates
    date_debut_formatted = ""
    date_fin_formatted = ""

    if date_debut:
        date_obj = parse_date_flexible(date_debut, "date_debut")
        if date_obj is not None:
            date_debut_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            date_debut_formatted = date_debut

    if date_fin:
        date_obj = parse_date_flexible(date_fin, "date_fin")
        if date_obj is not None:
            date_fin_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            date_fin_formatted = date_fin

    result = f"**{session_type_label}** : du {date_debut_formatted} au {date_fin_formatted}"
    if type_cours and type_cours != '-None-':
        result += f" ({type_cours})"

    return result


def format_exam_with_sessions(
    exam_info: Dict[str, Any],
    sessions: List[Dict[str, Any]]
) -> str:
    """
    Formate une date d'examen avec ses sessions associées.

    Args:
        exam_info: Infos sur la date d'examen
        sessions: Sessions de formation associées

    Returns:
        Texte formaté pour le candidat
    """
    # Formater la date d'examen
    exam_date = exam_info.get('Date_Examen', '')
    exam_date_formatted = ""

    if exam_date:
        date_obj = parse_date_flexible(exam_date, "exam_date")
        if date_obj is not None:
            exam_date_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            exam_date_formatted = exam_date

    # Formater la date de clôture
    date_cloture = exam_info.get('Date_Cloture_Inscription', '')
    cloture_formatted = ""

    if date_cloture:
        cloture_obj = parse_date_flexible(date_cloture, "date_cloture")
        if cloture_obj is not None:
            cloture_formatted = cloture_obj.strftime("%d/%m/%Y")

    result = f"📅 **Examen du {exam_date_formatted}**"
    if cloture_formatted:
        result += f" (clôture inscriptions: {cloture_formatted})"
    result += "\n"

    if sessions:
        result += "   Sessions de formation disponibles :\n"
        for session in sessions:
            result += f"   • {format_session_for_display(session)}\n"
    else:
        result += "   ⚠️ Pas de session de formation disponible pour cette date\n"

    return result


def detect_session_preference_from_deal(
    deal_data: Dict[str, Any],
    enriched_lookups: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Détecte la préférence de session (jour/soir) à partir des données du deal.

    Args:
        deal_data: Données du deal CRM
        enriched_lookups: Lookups enrichis depuis crm_lookup_helper (optionnel, recommandé)

    Returns:
        'jour', 'soir', ou None si pas de préférence détectée
    """
    # Méthode préférée: utiliser les lookups enrichis
    if enriched_lookups and enriched_lookups.get('session_type'):
        session_type = enriched_lookups['session_type']
        if session_type in ('jour', 'soir'):
            return session_type

    # Vérifier le champ Session existant (fallback)
    session = deal_data.get('Session')
    if session:
        if isinstance(session, dict):
            # Fallback: parser le name (legacy)
            session_name = session.get('name', '').lower()
        else:
            session_name = str(session).lower()

        if session_name.startswith(SESSION_TYPE_JOUR):
            return 'jour'
        elif session_name.startswith(SESSION_TYPE_SOIR):
            return 'soir'

    # Vérifier le champ Session_souhait_e
    session_souhaitee = deal_data.get('Session_souhait_e', '')
    if session_souhaitee:
        session_lower = str(session_souhaitee).lower()
        if 'jour' in session_lower or 'cdj' in session_lower:
            return 'jour'
        elif 'soir' in session_lower or 'cds' in session_lower:
            return 'soir'

    return None


def detect_session_preference_from_threads(threads: List[Dict]) -> Optional[str]:
    """
    Détecte la préférence de session (jour/soir) à partir des messages du candidat.

    Args:
        threads: Liste des threads du ticket

    Returns:
        'jour', 'soir', ou None si pas de préférence détectée
    """
    import re

    # Patterns plus spécifiques - éviter les faux positifs
    patterns_jour = [
        r"cours du jour",
        r"en journée",
        r"la journée",
        r"formation.{0,20}jour",  # "formation en jour", "formation du jour"
        r"préfère.{0,20}jour",
        r"choisis.{0,20}jour",
    ]

    patterns_soir = [
        r"cours du soir",
        r"le soir",
        r"en soirée",
        r"formation.{0,20}soir",  # "formation du soir"
        r"préfère.{0,20}soir",
        r"choisis.{0,20}soir",
        r"après.{0,10}travail",
    ]

    found_jour = False
    found_soir = False

    for thread in threads:
        if thread.get('direction') != 'in':
            continue

        content = thread.get('content', '') or thread.get('plainText', '')
        content_lower = content.lower()

        for pattern in patterns_jour:
            if re.search(pattern, content_lower):
                logger.info(f"Pattern 'jour' trouvé: '{pattern}'")
                found_jour = True
                break

        for pattern in patterns_soir:
            if re.search(pattern, content_lower):
                logger.info(f"Pattern 'soir' trouvé: '{pattern}'")
                found_soir = True
                break

    # Si les deux sont trouvés, c'est ambigu (peut-être email quoté)
    if found_jour and found_soir:
        logger.warning("Préférence ambiguë: patterns jour ET soir trouvés")
        return None

    if found_soir:
        logger.info("Préférence 'soir' détectée")
        return 'soir'

    if found_jour:
        logger.info("Préférence 'jour' détectée")
        return 'jour'

    return None


def analyze_session_situation(
    deal_data: Dict[str, Any],
    exam_dates: List[Dict[str, Any]],
    threads: List[Dict] = None,
    crm_client = None,
    triage_session_preference: Optional[str] = None,
    allow_change: bool = False,
    enriched_lookups: Optional[Dict[str, Any]] = None,
    is_explicit_session_change: bool = False
) -> Dict[str, Any]:
    """
    Analyse la situation et propose les sessions appropriées pour les dates d'examen.

    Args:
        deal_data: Données du deal CRM
        exam_dates: Liste des prochaines dates d'examen
        threads: Threads du ticket (pour détecter préférence)
        crm_client: Client Zoho CRM
        triage_session_preference: Préférence détectée par TriageAgent ('jour'/'soir')
                                   Si fournie, override la détection automatique
        allow_change: Si True, permet de proposer de nouvelles sessions même si une
                      session est déjà assignée (utilisé pour CONFIRMATION_SESSION
                      quand le candidat veut changer de session)
        enriched_lookups: Lookups enrichis (pour récupérer session_type actuelle)
        is_explicit_session_change: Si True, le candidat demande explicitement un changement
                                    de session → ne pas optimiser même si session actuelle est optimale

    Returns:
        {
            'session_preference': 'jour' | 'soir' | None,
            'current_session': Dict or None,
            'current_session_is_past': bool,
            'refresh_session_available': bool,
            'refresh_session': Dict or None,
            'proposed_options': List of {exam_date, sessions},
            'message': str (message à inclure dans la réponse)
        }
    """
    result = {
        'session_preference': None,
        'current_session': None,
        'current_session_is_past': False,
        'refresh_session_available': False,
        'refresh_session': None,
        'proposed_options': [],
        'message': None
    }

    logger.info("🔍 Analyse de la situation session de formation...")

    # 1. Vérifier si une session est déjà assignée
    current_session = deal_data.get('Session')
    if current_session:
        result['current_session'] = current_session
        logger.info(f"  Session actuelle: {current_session}")

        # Vérifier si la session actuelle est passée
        session_end_date = None
        if isinstance(current_session, dict):
            # Si c'est un lookup, on a besoin de récupérer les détails
            session_id = current_session.get('id')
            session_name = current_session.get('name', '')

            # Extraire la date de fin du nom si possible (format: xxx - DD mois - DD mois YYYY)
            # ou récupérer via API
            if crm_client and session_id:
                try:
                    from config import settings
                    url = f"{settings.zoho_crm_api_url}/Sessions1/{session_id}"
                    response = crm_client._make_request("GET", url)
                    session_data = response.get("data", [])
                    if session_data:
                        session_end_date = session_data[0].get('Date_fin')
                        logger.info(f"  Date fin session actuelle: {session_end_date}")
                except Exception as e:
                    logger.warning(f"  Erreur récupération session: {e}")

        if session_end_date:
            session_end_obj = parse_date_flexible(session_end_date, "session_end_date")
            if session_end_obj is not None:
                if session_end_obj < datetime.now().date():
                    result['current_session_is_past'] = True
                    logger.info("  ⚠️ Session actuelle TERMINÉE (dans le passé)")

    # 2. Détecter la préférence jour/soir
    # Priorité: 1) TriageAgent 2) Deal CRM 3) Threads
    if triage_session_preference:
        preference = triage_session_preference
        logger.info(f"  Préférence TriageAgent: {preference}")
    else:
        preference = detect_session_preference_from_deal(deal_data)
        if not preference and threads:
            preference = detect_session_preference_from_threads(threads)

    result['session_preference'] = preference
    logger.info(f"  Préférence finale: {preference or 'aucune'}")

    # 3. Si pas de dates d'examen, pas de proposition
    if not exam_dates:
        logger.info("  Pas de dates d'examen, pas de proposition de session")
        return result

    # 3.5. Si session DÉJÀ ASSIGNÉE et PAS dans le passé → NE PAS proposer de nouvelles sessions
    # SAUF si allow_change=True (DEMANDE_CHANGEMENT_SESSION ou CONFIRMATION_SESSION avec changement)
    if current_session and not result['current_session_is_past']:
        session_name = current_session.get('name', str(current_session)) if isinstance(current_session, dict) else str(current_session)

        # Si allow_change=True → TOUJOURS proposer des sessions alternatives
        # (le candidat veut explicitement changer de session)
        if allow_change:
            current_type = enriched_lookups.get('session_type') if enriched_lookups else None
            logger.info(f"  🔄 Changement de session demandé (allow_change=True, type actuel: {current_type})")
            # Continuer pour proposer des sessions
        else:
            logger.info(f"  ✅ Session déjà assignée ({session_name}) et valide → Pas de proposition")
            result['message'] = f"Votre session de formation est déjà programmée : {session_name}"
            return result

    # 4. Récupérer les sessions pour chaque date d'examen UNIQUE (cache pour éviter doublons)
    if crm_client:
        # Cache: date_string -> sessions
        sessions_cache = {}

        for exam_info in exam_dates:
            exam_date = exam_info.get('Date_Examen')
            if exam_date:
                # Utiliser le cache si on a déjà cherché cette date
                if exam_date not in sessions_cache:
                    sessions_cache[exam_date] = get_sessions_for_exam_date(
                        crm_client,
                        exam_date,
                        session_type=preference
                    )

                result['proposed_options'].append({
                    'exam_info': exam_info,
                    'sessions': sessions_cache[exam_date]
                })

    # 4.5 Si allow_change=True (repositionnement auto) mais session actuelle est DÉJÀ la plus proche
    # → pas besoin de proposer d'alternatives, on confirme la session actuelle
    # SAUF si is_explicit_session_change=True (le candidat veut changer de session)
    if allow_change and not is_explicit_session_change and current_session and not result['current_session_is_past'] and result['proposed_options']:
        current_session_end_str = enriched_lookups.get('session_date_fin') if enriched_lookups else None
        current_type = enriched_lookups.get('session_type') if enriched_lookups else None
        if current_session_end_str and current_type:
            current_end = parse_datetime_flexible(current_session_end_str, "current_session_end")
            if current_end is not None:
                is_optimal = True
                for option in result['proposed_options']:
                    exam_date_str = option['exam_info'].get('Date_Examen')
                    if not exam_date_str:
                        continue
                    exam_date_obj = parse_datetime_flexible(exam_date_str, "exam_date")
                    if exam_date_obj is None:
                        continue
                    # Session actuelle se termine avant l'examen ?
                    if current_end >= exam_date_obj:
                        is_optimal = False
                        break
                    # Parmi les sessions du même type, y en a-t-il une plus proche de l'examen ?
                    same_type = [s for s in option.get('sessions', []) if s.get('session_type') == current_type]
                    if same_type:
                        # sessions[0] = la plus proche (triée par days_before_exam asc)
                        closest = same_type[0]
                        closest_end_str = closest.get('Date_fin')
                        if closest_end_str:
                            closest_end = parse_datetime_flexible(closest_end_str, "closest_end")
                            if closest_end is not None and closest_end > current_end:
                                # Il existe une session plus proche de l'examen → proposer
                                is_optimal = False
                                break
                if is_optimal:
                    session_name = current_session.get('name', str(current_session)) if isinstance(current_session, dict) else str(current_session)
                    logger.info(f"  ✅ Session actuelle ({session_name}) est la plus proche de la nouvelle date → confirmation")
                    result['proposed_options'] = []
                    result['current_session_valid_for_new_date'] = True
                    result['message'] = f"Votre session de formation est déjà programmée : {session_name}"
                    return result

    # 5. CAS SPÉCIAL: Session passée + Examen futur = Proposer rafraîchissement
    if result['current_session_is_past'] and result['proposed_options']:
        # Chercher la meilleure session de rafraîchissement (la plus proche de l'examen)
        for option in result['proposed_options']:
            sessions = option.get('sessions', [])
            if sessions:
                # Prendre la session la plus proche de l'examen
                best_session = sessions[0]  # Déjà triée par proximité
                result['refresh_session_available'] = True
                result['refresh_session'] = {
                    'session': best_session,
                    'exam_info': option.get('exam_info')
                }
                logger.info(f"  ✅ Session de rafraîchissement disponible: {best_session.get('Name')}")
                break

    # 6. Générer le message
    result['message'] = generate_session_proposal_message(
        result['proposed_options'],
        preference,
        refresh_available=result['refresh_session_available'],
        refresh_session=result['refresh_session']
    )

    return result


def generate_session_proposal_message(
    options: List[Dict],
    preference: Optional[str] = None,
    refresh_available: bool = False,
    refresh_session: Optional[Dict] = None
) -> str:
    """
    Génère le message proposant les sessions de formation avec les dates d'examen.

    Args:
        options: Liste des options {exam_info, sessions}
        preference: Préférence jour/soir du candidat
        refresh_available: Si une session de rafraîchissement est disponible
        refresh_session: Infos sur la session de rafraîchissement proposée

    Returns:
        Message formaté pour le candidat
    """
    if not options:
        return ""

    lines = []

    # CAS SPÉCIAL: Formation terminée mais examen à venir = proposer rafraîchissement
    if refresh_available and refresh_session:
        lines.append(generate_refresh_session_message(refresh_session))
        lines.append("")  # Ligne vide de séparation

    for option in options:
        exam_info = option.get('exam_info', {})
        sessions = option.get('sessions', [])

        lines.append(format_exam_with_sessions(exam_info, sessions))

    message = "\n".join(lines)

    if not preference:
        message += "\nMerci de nous indiquer votre préférence (cours du jour ou cours du soir) ainsi que la date d'examen qui vous convient."
    else:
        pref_label = "cours du jour" if preference == 'jour' else "cours du soir"
        message += f"\nMerci de nous confirmer la date d'examen qui vous convient pour votre formation en {pref_label}."

    return message


def generate_refresh_session_message(refresh_session: Dict) -> str:
    """
    Génère le message proposant une session de rafraîchissement.

    Ce cas se produit quand:
    - Le candidat a déjà suivi une formation (session terminée)
    - Son examen est dans le futur
    - Une nouvelle session est disponible avant l'examen

    On lui propose de rejoindre cette session GRATUITEMENT pour rafraîchir
    ses connaissances et maximiser ses chances de réussite.
    """
    session = refresh_session.get('session', {})
    exam_info = refresh_session.get('exam_info', {})

    # Formater les dates de la session de rafraîchissement
    date_debut = session.get('Date_d_but', '')
    date_fin = session.get('Date_fin', '')
    type_cours = session.get('Type_de_cours', '')
    session_type_label = session.get('session_type_label', 'Formation')

    date_debut_formatted = ""
    date_fin_formatted = ""

    if date_debut:
        date_obj = parse_date_flexible(date_debut, "date_debut")
        if date_obj is not None:
            date_debut_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            date_debut_formatted = date_debut

    if date_fin:
        date_obj = parse_date_flexible(date_fin, "date_fin")
        if date_obj is not None:
            date_fin_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            date_fin_formatted = date_fin

    # Formater la date d'examen
    exam_date = exam_info.get('Date_Examen', '')
    exam_date_formatted = ""
    if exam_date:
        date_obj = parse_date_flexible(exam_date, "exam_date")
        if date_obj is not None:
            exam_date_formatted = date_obj.strftime("%d/%m/%Y")
        else:
            exam_date_formatted = exam_date

    message = f"""📚 **PROPOSITION DE RAFRAÎCHISSEMENT (sans frais supplémentaires)**

Nous avons constaté que vous avez déjà suivi votre formation, mais votre examen est prévu pour le {exam_date_formatted}.

**Pour nous, votre réussite est notre priorité.** Plus vos connaissances sont fraîches au moment de l'examen, plus vos chances de succès sont élevées.

C'est pourquoi nous vous proposons, **sans aucun coût additionnel**, de rejoindre la prochaine session de formation pour rafraîchir vos acquis :

• **{session_type_label}** : du {date_debut_formatted} au {date_fin_formatted}"""

    if type_cours and type_cours != '-None-':
        message += f" ({type_cours})"

    message += """

Si vous souhaitez bénéficier de ce rafraîchissement gratuit, merci de nous le confirmer et nous vous ajouterons à cette session."""

    return message


def match_sessions_by_date_range(
    crm_client,
    requested_dates: Dict[str, Any],
    session_type: Optional[str] = None
) -> Dict[str, Any]:
    """
    Trouve les sessions correspondant à une plage de dates demandée par le candidat.

    Args:
        crm_client: Client Zoho CRM
        requested_dates: Dict avec start_date, end_date, month (depuis intent_context)
        session_type: 'jour' ou 'soir' (optionnel, pour filtrer)

    Returns:
        {
            'match_type': 'EXACT' | 'OVERLAP' | 'CLOSEST' | 'NO_MATCH',
            'exact_matches': [sessions avec dates exactes],
            'overlap_matches': [sessions qui chevauchent],
            'closest_before': session la plus proche avant | None,
            'closest_after': session la plus proche après | None,
            'all_in_month': [toutes les sessions du mois],
            'sessions_proposees': [sessions formatées pour template]
        }
    """
    from config import settings

    result = {
        'match_type': 'NO_MATCH',
        'exact_matches': [],
        'overlap_matches': [],
        'closest_before': None,
        'closest_after': None,
        'closest_before_jour': None,  # Pour proposer les deux types si pas de préférence
        'closest_before_soir': None,
        'closest_after_jour': None,
        'closest_after_soir': None,
        'all_in_month': [],
        'sessions_proposees': []
    }

    if not requested_dates:
        logger.warning("match_sessions_by_date_range: pas de dates demandées")
        return result

    start_date_str = requested_dates.get('start_date')
    end_date_str = requested_dates.get('end_date')
    month = requested_dates.get('month')

    if not start_date_str:
        logger.warning("match_sessions_by_date_range: start_date manquant")
        return result

    start_date = parse_datetime_flexible(start_date_str, "start_date")
    if start_date is None:
        logger.error(f"Erreur parsing start_date: '{start_date_str}'")
        return result
    end_date = parse_datetime_flexible(end_date_str, "end_date") if end_date_str else start_date
    if end_date is None:
        logger.error(f"Erreur parsing end_date: '{end_date_str}'")
        return result

    logger.info(f"🔍 Recherche sessions pour dates demandées: {start_date_str} à {end_date_str}")

    # Rechercher toutes les sessions du mois (ou période étendue)
    today = datetime.now()
    today_str = today.strftime('%Y-%m-%d')

    # Recherche sur une période élargie autour des dates demandées
    search_start = (start_date - timedelta(days=30)).strftime('%Y-%m-%d')
    search_end = (end_date + timedelta(days=30)).strftime('%Y-%m-%d')

    # Permettre les sessions commencées depuis moins de 3 jours (confirmation tardive)
    three_days_ago_str = (today - timedelta(days=3)).strftime('%Y-%m-%d')

    try:
        url = f"{settings.zoho_crm_api_url}/Sessions1/search"

        # Critères: sessions dans la période de recherche
        criteria = (
            f"((Date_d_but:greater_equal:{three_days_ago_str})"
            f"and(Date_d_but:less_equal:{search_end})"
            f"and(Date_fin:greater_equal:{search_start}))"
        )

        all_sessions = []
        for page_num in range(1, 15):  # Max 14 pages (~2800 sessions)
            params = {
                "criteria": criteria,
                "page": page_num,
                "per_page": 200
            }
            response = crm_client._make_request("GET", url, params=params)
            page_data = response.get("data", [])
            if not page_data:
                break
            all_sessions.extend(page_data)
            if len(page_data) < 200:
                break

        logger.info(f"  {len(all_sessions)} session(s) trouvée(s) dans la période")

    except Exception as e:
        logger.error(f"Erreur API sessions: {e}")
        return result

    if not all_sessions:
        return result

    # Filtrer par lieu (VISIO uniquement pour Uber)
    visio_sessions = []
    for s in all_sessions:
        lieu = s.get('Lieu_de_formation', {})
        lieu_name = lieu.get('name', '') if isinstance(lieu, dict) else str(lieu)
        if is_uber_visio_session(lieu_name):
            visio_sessions.append(s)

    logger.info(f"  {len(visio_sessions)} session(s) VISIO après filtrage lieu")

    # Filtrer par type si demandé
    if session_type:
        type_prefix = 'cdj' if session_type == 'jour' else 'cds'
        visio_sessions = [s for s in visio_sessions if s.get('Name', '').lower().startswith(type_prefix)]
        logger.info(f"  {len(visio_sessions)} session(s) après filtrage type '{session_type}'")

    # Catégoriser les sessions par type de correspondance
    for session in visio_sessions:
        session_start_str = session.get('Date_d_but', '')
        session_end_str = session.get('Date_fin', '')

        if not session_start_str or not session_end_str:
            continue

        session_start = parse_datetime_flexible(session_start_str, "session_start")
        session_end = parse_datetime_flexible(session_end_str, "session_end")
        if session_start is None or session_end is None:
            continue

        # Enrichir la session avec des infos formatées
        session_name = session.get('Name', '').lower()
        session['session_type'] = 'jour' if session_name.startswith('cdj') else 'soir'
        session['session_type_label'] = 'Cours du jour' if session['session_type'] == 'jour' else 'Cours du soir'
        session['date_debut'] = session_start.strftime('%d/%m/%Y')
        session['date_fin'] = session_end.strftime('%d/%m/%Y')

        # Vérifier correspondance exacte
        # Cas 1: Dates début ET fin correspondent exactement
        # Cas 2: La session est CONTENUE dans la plage demandée (ex: "semaine du 16/02" = 7j,
        #         session 16-20/02 = 5j → la session tombe exactement dans la semaine demandée)
        # Cas 3: Candidat n'a donné qu'une date de début → session commence à cette date
        session_contained_in_range = (session_start.date() >= start_date.date() and session_end.date() <= end_date.date())
        no_explicit_end = (end_date_str is None) or (start_date.date() == end_date.date())
        starts_on_date = session_start.date() == start_date.date()
        if session_contained_in_range or (starts_on_date and no_explicit_end):
            result['exact_matches'].append(session)
            logger.info(f"  ✅ EXACT MATCH: {session.get('Name')} ({session_start_str} - {session_end_str})")

        # Vérifier chevauchement
        elif session_start <= end_date and session_end >= start_date:
            result['overlap_matches'].append(session)
            logger.info(f"  ⚡ OVERLAP: {session.get('Name')} ({session_start_str} - {session_end_str})")

        # Sinon, garder pour alternatives
        result['all_in_month'].append(session)

        # Trouver la session la plus proche avant les dates demandées (par type)
        if session_end < start_date:
            s_type = session['session_type']
            key_before = f'closest_before_{s_type}'
            prev_end = parse_datetime_flexible(result[key_before].get('Date_fin', '1900-01-01'), "prev_end") if result[key_before] else None
            if not result[key_before] or (prev_end is not None and session_end > prev_end):
                result[key_before] = session
            # Aussi mettre à jour closest_before global (le plus proche tous types)
            global_prev_end = parse_datetime_flexible(result['closest_before'].get('Date_fin', '1900-01-01'), "global_prev_end") if result['closest_before'] else None
            if not result['closest_before'] or (global_prev_end is not None and session_end > global_prev_end):
                result['closest_before'] = session

        # Trouver la session la plus proche après les dates demandées (par type)
        if session_start > end_date:
            s_type = session['session_type']
            key_after = f'closest_after_{s_type}'
            prev_start = parse_datetime_flexible(result[key_after].get('Date_d_but', '2100-01-01'), "prev_start") if result[key_after] else None
            if not result[key_after] or (prev_start is not None and session_start < prev_start):
                result[key_after] = session
            # Aussi mettre à jour closest_after global (le plus proche tous types)
            global_prev_start = parse_datetime_flexible(result['closest_after'].get('Date_d_but', '2100-01-01'), "global_prev_start") if result['closest_after'] else None
            if not result['closest_after'] or (global_prev_start is not None and session_start < global_prev_start):
                result['closest_after'] = session

    # Déterminer le type de match
    if result['exact_matches']:
        result['match_type'] = 'EXACT'
        result['sessions_proposees'] = result['exact_matches']
        logger.info(f"  🎯 Match type: EXACT ({len(result['exact_matches'])} session(s))")
    elif result['overlap_matches']:
        result['match_type'] = 'OVERLAP'
        result['sessions_proposees'] = result['overlap_matches']
        logger.info(f"  🎯 Match type: OVERLAP ({len(result['overlap_matches'])} session(s))")
    elif result['closest_before'] or result['closest_after']:
        result['match_type'] = 'CLOSEST'
        logger.info(f"  🎯 Match type: CLOSEST (before={result['closest_before'] is not None}, after={result['closest_after'] is not None})")
    else:
        result['match_type'] = 'NO_MATCH'
        logger.info("  🎯 Match type: NO_MATCH")

    # ================================================================
    # FALLBACK: Si NO_MATCH avec un type spécifique, chercher l'autre type
    # On ne doit JAMAIS laisser le candidat sans alternatives
    # ================================================================
    if result['match_type'] == 'NO_MATCH' and session_type:
        other_type = 'soir' if session_type == 'jour' else 'jour'
        logger.info(f"  🔄 Aucune session '{session_type}' disponible, recherche sessions '{other_type}' comme alternative...")

        # Refaire la recherche sans filtre de type pour trouver les alternatives
        fallback_result = match_sessions_by_date_range(crm_client, requested_dates, session_type=None)

        # Récupérer les sessions de l'autre type comme alternatives
        other_type_key_before = f'closest_before_{other_type}'
        other_type_key_after = f'closest_after_{other_type}'

        if fallback_result.get(other_type_key_before) or fallback_result.get(other_type_key_after):
            result['no_sessions_of_requested_type'] = True
            result['requested_type'] = session_type
            result['alternative_type'] = other_type
            result['alternative_type_label'] = 'Cours du soir' if other_type == 'soir' else 'Cours du jour'

            # Proposer les sessions de l'autre type comme fallback
            result['fallback_closest_before'] = fallback_result.get(other_type_key_before)
            result['fallback_closest_after'] = fallback_result.get(other_type_key_after)

            # Mettre aussi dans closest_before/after globaux pour le template
            if not result['closest_before']:
                result['closest_before'] = fallback_result.get(other_type_key_before)
            if not result['closest_after']:
                result['closest_after'] = fallback_result.get(other_type_key_after)

            # Mettre à jour le match_type
            if result['closest_before'] or result['closest_after']:
                result['match_type'] = 'CLOSEST_FALLBACK'
                logger.info(f"  ✅ Alternatives '{other_type}' trouvées comme fallback")

    return result


def verify_session_complaint(
    crm_client,
    claimed_session: Dict[str, Any],
    assigned_session: Dict[str, Any],
    enriched_lookups: Dict[str, Any],
    session_preference: Optional[str] = None,
    exam_date: Optional[str] = None
) -> Dict[str, Any]:
    """
    Vérifie si la plainte du candidat concernant une erreur d'inscription est justifiée.

    Compare la session réclamée (extraite du message) avec la session assignée (CRM)
    et vérifie si la session demandée existe vraiment.

    Args:
        crm_client: Client Zoho CRM
        claimed_session: Session que le candidat affirme avoir demandée
            {claimed_type: "jour"|"soir", claimed_dates: "YYYY-MM-DD - YYYY-MM-DD", claimed_dates_raw: str}
        assigned_session: Session actuellement assignée dans le CRM (deal_data.Session)
        enriched_lookups: Lookups enrichis (contient session_type, session_date_debut, etc.)
        session_preference: Préférence de session du candidat (backup si claimed_type absent)

    Returns:
        {
            'is_cab_error': bool,           # True si erreur CAB confirmée
            'error_type': str,              # 'TYPE_MISMATCH', 'DATE_MISMATCH', 'BOTH', 'NO_ERROR'
            'matched_session': dict|None,   # Session correspondant à la demande (si existe)
            'alternatives': list,           # Sessions alternatives si demande impossible
            'verification_details': str,    # Explication de la vérification
            'assigned_session_info': dict,  # Infos sur la session actuellement assignée
            'claimed_session_info': dict    # Infos sur ce que le candidat a demandé
        }
    """
    result = {
        'is_cab_error': False,
        'error_type': 'NO_ERROR',
        'matched_session': None,
        'alternatives': [],
        'verification_details': '',
        'assigned_session_info': {},
        'claimed_session_info': {}
    }

    logger.info("🔍 Vérification de la plainte session...")

    # 1. Extraire les infos de la session assignée depuis enriched_lookups
    assigned_type = enriched_lookups.get('session_type')  # 'jour' ou 'soir'
    assigned_start = enriched_lookups.get('session_date_debut')
    assigned_end = enriched_lookups.get('session_date_fin')
    assigned_name = enriched_lookups.get('session_name', '')

    # Inférer le type depuis le nom si non défini (cds = soir, cdj = jour)
    if not assigned_type and assigned_name:
        name_lower = assigned_name.lower()
        if name_lower.startswith('cds'):
            assigned_type = 'soir'
        elif name_lower.startswith('cdj'):
            assigned_type = 'jour'

    result['assigned_session_info'] = {
        'type': assigned_type,
        'start': assigned_start,
        'end': assigned_end,
        'name': assigned_name
    }

    logger.info(f"  📋 Session assignée: {assigned_type} du {assigned_start} au {assigned_end}")

    if not assigned_type:
        logger.warning("  ⚠️ Pas de session assignée dans le CRM - impossible de vérifier")
        result['verification_details'] = "Aucune session assignée dans le CRM"
        return result

    # 2. Extraire les infos réclamées par le candidat
    claimed_type = claimed_session.get('claimed_type') if claimed_session else None
    claimed_dates_raw = claimed_session.get('claimed_dates_raw', '') if claimed_session else ''
    claimed_dates = claimed_session.get('claimed_dates', '') if claimed_session else ''

    # IMPORTANT: On garde trace si claimed_type vient du candidat ou d'un fallback
    # Si c'est un fallback sur session_preference, on ne peut PAS conclure à une erreur CAB
    claimed_type_from_candidate = claimed_type is not None

    # Détecter si le candidat a des contraintes horaires qui empêchent TOUT type de session
    # Indices: "18h ne convient pas", "après 19h", "rentre du travail à", etc.
    has_time_constraints = False
    if claimed_dates_raw:
        constraint_indicators = ['ne convient pas', 'après 19h', 'rentre du travail', '18h', '19h']
        for indicator in constraint_indicators:
            if indicator.lower() in claimed_dates_raw.lower():
                has_time_constraints = True
                logger.info(f"  ⚠️ Contraintes horaires détectées dans: '{claimed_dates_raw}'")
                break

    # Fallback sur session_preference si claimed_type non spécifié
    if not claimed_type and session_preference:
        claimed_type = session_preference
        logger.info(f"  ℹ️ Utilisation session_preference comme claimed_type: {claimed_type} (fallback, pas de réclamation explicite)")

    result['claimed_session_info'] = {
        'type': claimed_type,
        'dates': claimed_dates,
        'dates_raw': claimed_dates_raw
    }

    logger.info(f"  📋 Session réclamée: {claimed_type} - {claimed_dates_raw or claimed_dates or 'dates non spécifiées'}")

    # 3. Comparer type (jour/soir)
    type_mismatch = False
    if claimed_type and assigned_type and claimed_type != assigned_type:
        type_mismatch = True
        logger.info(f"  ❌ TYPE MISMATCH: réclamé={claimed_type}, assigné={assigned_type}")

    # 4. Comparer dates si spécifiées
    date_mismatch = False
    if claimed_dates:
        # Parser les dates réclamées
        try:
            parts = claimed_dates.split(' - ')
            if len(parts) == 2:
                claimed_start = parts[0].strip()
                claimed_end = parts[1].strip()

                # Comparer avec les dates assignées
                if assigned_start and assigned_end:
                    if claimed_start != assigned_start or claimed_end != assigned_end:
                        date_mismatch = True
                        logger.info(f"  ❌ DATE MISMATCH: réclamé={claimed_start}-{claimed_end}, assigné={assigned_start}-{assigned_end}")
        except Exception as e:
            logger.warning(f"  ⚠️ Erreur parsing dates réclamées: {e}")

    # 5. Déterminer le type d'erreur
    if type_mismatch and date_mismatch:
        result['error_type'] = 'BOTH'
    elif type_mismatch:
        result['error_type'] = 'TYPE_MISMATCH'
    elif date_mismatch:
        result['error_type'] = 'DATE_MISMATCH'
    else:
        result['error_type'] = 'NO_ERROR'
        result['verification_details'] = "La session assignée correspond à la demande"
        logger.info("  ✅ Pas de différence détectée - session assignée semble correcte")

        # CAS SPÉCIAL: Le candidat veut changer de session (dates) mais pas de type mismatch
        # → Proposer les sessions selon la préférence du candidat
        if exam_date:
            # Si le candidat a une préférence claire, ne montrer que ce type
            preferred_type = claimed_type or None
            if preferred_type:
                logger.info(f"  🔍 Demande de changement de dates (préférence: {preferred_type}) → recherche sessions {preferred_type} avant l'examen du {exam_date}...")
            else:
                logger.info(f"  🔍 Demande de changement de dates → recherche de TOUTES les sessions avant l'examen du {exam_date}...")

            all_sessions = []
            sessions_jour = []
            sessions_soir = []

            # Si préférence jour ou pas de préférence → chercher les sessions jour
            if not preferred_type or preferred_type == 'jour':
                sessions_jour = get_sessions_for_exam_date(
                    crm_client=crm_client,
                    exam_date=exam_date,
                    session_type='jour',
                    limit=3
                )
                if sessions_jour:
                    logger.info(f"  ✅ {len(sessions_jour)} session(s) JOUR trouvée(s)")
                    all_sessions.extend(sessions_jour)

            # Si préférence soir ou pas de préférence → chercher les sessions soir
            if not preferred_type or preferred_type == 'soir':
                sessions_soir = get_sessions_for_exam_date(
                    crm_client=crm_client,
                    exam_date=exam_date,
                    session_type='soir',
                    limit=3
                )
                if sessions_soir:
                    logger.info(f"  ✅ {len(sessions_soir)} session(s) SOIR trouvée(s)")
                    all_sessions.extend(sessions_soir)

            if all_sessions:
                result['alternatives'] = all_sessions
                result['all_sessions_jour'] = sessions_jour
                result['all_sessions_soir'] = sessions_soir
                result['has_all_sessions'] = True
                result['verification_details'] = f"Session correcte mais le candidat souhaite d'autres dates. Sessions disponibles: {len(sessions_jour)} jour, {len(sessions_soir)} soir"
                logger.info(f"  ✅ Total: {len(all_sessions)} session(s) alternatives proposée(s)")

        return result

    # 6. Vérifier si la session réclamée EXISTE dans les sessions disponibles
    if crm_client and claimed_type:
        logger.info(f"  🔍 Recherche de la session réclamée ({claimed_type})...")

        # Construire les dates pour la recherche
        search_dates = None
        if claimed_dates:
            try:
                parts = claimed_dates.split(' - ')
                if len(parts) == 2:
                    search_dates = {
                        'start_date': parts[0].strip(),
                        'end_date': parts[1].strip()
                    }
            except:
                pass

        if search_dates:
            # Recherche par dates spécifiques
            match_result = match_sessions_by_date_range(
                crm_client=crm_client,
                requested_dates=search_dates,
                session_type=claimed_type
            )

            if match_result.get('exact_matches'):
                # Session réclamée EXISTE → erreur CAB confirmée
                result['is_cab_error'] = True
                result['matched_session'] = match_result['exact_matches'][0]
                result['verification_details'] = f"Erreur confirmée: la session {claimed_type} du {claimed_dates_raw or claimed_dates} existe"
                logger.info(f"  ✅ ERREUR CAB CONFIRMÉE: session réclamée existe!")

            elif match_result.get('overlap_matches'):
                # Session avec chevauchement → probablement erreur CAB
                result['is_cab_error'] = True
                result['matched_session'] = match_result['overlap_matches'][0]
                result['alternatives'] = match_result['overlap_matches']
                result['verification_details'] = f"Erreur probable: session similaire trouvée (dates légèrement différentes)"
                logger.info(f"  ⚠️ ERREUR CAB PROBABLE: session similaire trouvée")

            else:
                # Session réclamée N'EXISTE PAS
                result['is_cab_error'] = False
                result['verification_details'] = f"La session {claimed_type} aux dates {claimed_dates_raw or claimed_dates} n'existe pas"
                logger.info(f"  ❌ Session réclamée n'existe pas - pas d'erreur CAB")

                # Proposer des alternatives
                if match_result.get('closest_before'):
                    result['alternatives'].append(match_result['closest_before'])
                if match_result.get('closest_after'):
                    result['alternatives'].append(match_result['closest_after'])

        else:
            # Pas de dates spécifiques
            # CAS SPÉCIAL: Si contraintes horaires détectées → proposer TOUTES les sessions
            # Le candidat a des difficultés avec les deux types (jour ET soir)
            if has_time_constraints and exam_date:
                logger.info(f"  🕐 Contraintes horaires → proposer TOUTES les sessions")
                result['is_cab_error'] = False
                result['error_type'] = 'TIME_CONSTRAINTS'

                all_sessions = []
                sessions_jour = get_sessions_for_exam_date(crm_client, exam_date, 'jour', limit=3)
                sessions_soir = get_sessions_for_exam_date(crm_client, exam_date, 'soir', limit=3)

                if sessions_jour:
                    logger.info(f"  ✅ {len(sessions_jour)} session(s) JOUR trouvée(s)")
                    all_sessions.extend(sessions_jour)
                if sessions_soir:
                    logger.info(f"  ✅ {len(sessions_soir)} session(s) SOIR trouvée(s)")
                    all_sessions.extend(sessions_soir)

                if all_sessions:
                    result['alternatives'] = all_sessions
                    result['all_sessions_jour'] = sessions_jour
                    result['all_sessions_soir'] = sessions_soir
                    result['has_all_sessions'] = True
                    result['verification_details'] = f"Contraintes horaires détectées. Sessions disponibles: {len(sessions_jour)} jour, {len(sessions_soir)} soir"
                    logger.info(f"  ✅ Total: {len(all_sessions)} session(s) proposée(s)")

            elif type_mismatch and claimed_type_from_candidate and not has_time_constraints:
                # SEULEMENT si le candidat a EXPLICITEMENT réclamé un type différent
                # ET pas de contraintes horaires détectées
                # → C'est une vraie erreur CAB
                result['is_cab_error'] = True
                result['verification_details'] = f"Type de session différent: réclamé {claimed_type}, assigné {assigned_type}"
                logger.info(f"  ⚠️ ERREUR CAB (type): {claimed_type} ≠ {assigned_type}")

                # Chercher des sessions du type demandé comme alternatives
                if exam_date:
                    logger.info(f"  🔍 Recherche de sessions {claimed_type} avant l'examen du {exam_date}...")
                    sessions = get_sessions_for_exam_date(
                        crm_client=crm_client,
                        exam_date=exam_date,
                        session_type=claimed_type,
                        limit=3
                    )
                    if sessions:
                        # Proposer la première session comme correction, les autres comme alternatives
                        result['matched_session'] = sessions[0]
                        result['alternatives'] = sessions[:3]
                        logger.info(f"  ✅ {len(sessions)} session(s) {claimed_type} trouvée(s) avant l'examen")
                    else:
                        logger.warning(f"  ⚠️ Aucune session {claimed_type} disponible avant l'examen")

            elif exam_date and (not claimed_type_from_candidate or not claimed_type):
                # CAS: Le candidat veut changer de session SANS avoir réclamé un type spécifique
                # → Ce n'est PAS une erreur CAB, juste une demande de changement
                # → Proposer TOUTES les sessions disponibles
                # CAS SPÉCIAL: Pas de type réclamé mais le candidat veut changer de session
                # → Proposer TOUTES les sessions (jour ET soir) pour que le candidat choisisse
                logger.info(f"  🔍 Pas de type spécifié → recherche de TOUTES les sessions avant l'examen du {exam_date}...")

                all_sessions = []
                sessions_jour = []
                sessions_soir = []

                # Récupérer les sessions jour
                sessions_jour = get_sessions_for_exam_date(
                    crm_client=crm_client,
                    exam_date=exam_date,
                    session_type='jour',
                    limit=3
                )
                if sessions_jour:
                    logger.info(f"  ✅ {len(sessions_jour)} session(s) JOUR trouvée(s)")
                    all_sessions.extend(sessions_jour)

                # Récupérer les sessions soir
                sessions_soir = get_sessions_for_exam_date(
                    crm_client=crm_client,
                    exam_date=exam_date,
                    session_type='soir',
                    limit=3
                )
                if sessions_soir:
                    logger.info(f"  ✅ {len(sessions_soir)} session(s) SOIR trouvée(s)")
                    all_sessions.extend(sessions_soir)

                if all_sessions:
                    # Pas d'erreur CAB, juste une demande de changement
                    result['is_cab_error'] = False
                    result['error_type'] = 'NO_ERROR'
                    result['alternatives'] = all_sessions
                    result['all_sessions_jour'] = sessions_jour
                    result['all_sessions_soir'] = sessions_soir
                    result['has_all_sessions'] = True  # Flag pour le template
                    result['verification_details'] = f"Toutes les sessions disponibles avant l'examen: {len(sessions_jour)} jour, {len(sessions_soir)} soir"
                    logger.info(f"  ✅ Total: {len(all_sessions)} session(s) proposée(s) (jour + soir)")
                else:
                    result['verification_details'] = "Aucune session disponible avant la date d'examen"
                    logger.warning("  ⚠️ Aucune session disponible avant l'examen")

    return result
