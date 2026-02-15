"""
Helper pour gérer la cohérence entre les dates de formation et d'examen.

Cas critiques détectés:
1. Formation manquée + Examen imminent → Proposer 2 options au candidat
2. Formation proposée APRÈS examen → ERREUR LOGIQUE à éviter
3. Session assignée avant création opportunité → ERREUR DE SAISIE ADMIN

Règles métier:
- Report d'examen possible UNIQUEMENT pour force majeure (certificat médical, décès, etc.)
- Ne pas avoir suivi la formation ≠ force majeure
- Si e-learning complété, l'examen peut être maintenu
- En cas de report, la CMA positionne sur la prochaine date disponible

Détection erreur de saisie session:
- Si session_end_date < deal_created_date → ERREUR (impossible d'avoir participé)
- Si session_end_date >= deal_created_date ET session_end_date < today → Formation passée normale
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, List

from src.constants.emails import COMPANY_SIGNATURE
from src.utils.date_utils import parse_date_flexible, parse_datetime_flexible

logger = logging.getLogger(__name__)


def analyze_training_exam_consistency(
    deal_data: Dict,
    threads: List[Dict],
    session_data: Optional[Dict] = None,
    crm_client=None
) -> Dict:
    """
    Analyse la cohérence entre la formation suivie/manquée et la date d'examen.

    Détecte les situations problématiques:
    1. Candidat a manqué sa formation
    2. Examen est imminent
    3. Formation proposée serait après l'examen

    Returns:
        {
            'has_consistency_issue': bool,
            'issue_type': str or None,  # 'MISSED_TRAINING_IMMINENT_EXAM', 'TRAINING_AFTER_EXAM'
            'exam_date': str or None,
            'next_exam_date': str or None,  # Pour option de report
            'force_majeure_detected': bool,
            'force_majeure_type': str or None,  # 'medical', 'death', 'other'
            'should_present_options': bool,
            'response_message': str or None,
            'options': List[Dict]  # Les options à présenter au candidat
        }
    """
    result = {
        'has_consistency_issue': False,
        'issue_type': None,
        'exam_date': None,
        'exam_date_formatted': None,
        'next_exam_date': None,
        'next_exam_date_formatted': None,
        'force_majeure_detected': False,
        'force_majeure_type': None,
        'should_present_options': False,
        'response_message': None,
        'options': []
    }

    # ================================================================
    # 1. DÉTECTER SI LE CANDIDAT A MANQUÉ SA FORMATION
    # ================================================================
    # Méthode 1: Détection via les threads (ce que le candidat dit)
    missed_training = detect_missed_training_in_threads(threads)

    # Méthode 2: Détection via le CRM (session passée + examen futur)
    if not missed_training:
        missed_training = detect_missed_training_from_crm(deal_data)

    if not missed_training:
        logger.info("  ✅ Pas de formation manquée détectée")
        return result

    logger.warning(f"  🚨 Formation manquée détectée: {missed_training.get('reason', 'raison inconnue')}")

    # ================================================================
    # 2. VÉRIFIER SI L'EXAMEN EST IMMINENT
    # ================================================================
    exam_date_raw = deal_data.get('Date_examen_VTC')
    if not exam_date_raw:
        logger.info("  ℹ️ Pas de date d'examen enregistrée")
        return result

    # Extraire la date d'examen (format: {'name': '13_2026-01-27', 'id': '...'} ou string)
    if isinstance(exam_date_raw, dict):
        exam_date_str = exam_date_raw.get('name', '')
        # Format: "13_2026-01-27" → extraire "2026-01-27"
        if '_' in exam_date_str:
            exam_date_str = exam_date_str.split('_')[1]
    else:
        exam_date_str = str(exam_date_raw)

    exam_date = parse_datetime_flexible(exam_date_str, "exam_date")
    if exam_date is None:
        logger.warning(f"  ⚠️ Format de date d'examen invalide: {exam_date_str}")
        return result
    result['exam_date'] = exam_date_str
    result['exam_date_formatted'] = exam_date.strftime("%d/%m/%Y")

    # Vérifier si l'examen est dans les 14 prochains jours (imminent)
    today = datetime.now()
    days_until_exam = (exam_date - today).days
    is_imminent = days_until_exam <= 14

    # Formation manquée = toujours un problème (besoin de rafraîchissement)
    # Même si l'examen n'est pas imminent, le candidat a besoin de nouvelles sessions
    result['has_consistency_issue'] = True
    result['issue_type'] = 'MISSED_TRAINING_IMMINENT_EXAM' if is_imminent else 'MISSED_TRAINING_NEEDS_REFRESH'
    result['needs_refresh_session'] = True  # Toujours proposer des sessions de rafraîchissement

    if is_imminent:
        logger.warning(f"  🚨 EXAMEN IMMINENT: dans {days_until_exam} jours ({result['exam_date_formatted']})")
        result['should_present_options'] = True  # Options A/B seulement si imminent
    else:
        logger.info(f"  ℹ️ Examen dans {days_until_exam} jours - proposer session de rafraîchissement")

    # ================================================================
    # 3. DÉTECTER SI FORCE MAJEURE MENTIONNÉE
    # ================================================================
    force_majeure = detect_force_majeure_in_threads(threads)
    result['force_majeure_detected'] = force_majeure.get('detected', False)
    result['force_majeure_type'] = force_majeure.get('type')

    if result['force_majeure_detected']:
        logger.info(f"  📋 Force majeure détectée: {result['force_majeure_type']}")

    # ================================================================
    # 4. RÉCUPÉRER LA PROCHAINE DATE D'EXAMEN (pour option report)
    # ================================================================
    if crm_client:
        next_exam = get_next_exam_date_after(
            current_exam_date=exam_date,
            departement=deal_data.get('CMA_de_depot'),
            crm_client=crm_client
        )
        if next_exam:
            result['next_exam_date'] = next_exam.get('Date_Examen')
            next_date = parse_date_flexible(result['next_exam_date'], "next_exam_date")
            if next_date:
                result['next_exam_date_formatted'] = next_date.strftime("%d/%m/%Y")
            else:
                result['next_exam_date_formatted'] = result['next_exam_date']
            logger.info(f"  📅 Prochaine date d'examen disponible: {result['next_exam_date_formatted']}")

    # ================================================================
    # 5. PRÉPARER LES OPTIONS POUR LE CANDIDAT (seulement si examen imminent)
    # ================================================================
    if is_imminent:
        result['options'] = [
            {
                'id': 'A',
                'title': 'Maintenir l\'examen',
                'description': f"Passer l'examen le {result['exam_date_formatted']} si le e-learning vous a suffi",
                'action': 'KEEP_EXAM'
            },
            {
                'id': 'B',
                'title': 'Reporter l\'examen',
                'description': f"Demander un report vers le {result['next_exam_date_formatted'] or 'prochaine date disponible'} (justificatif force majeure requis)",
                'action': 'RESCHEDULE_EXAM',
                'requires': 'Certificat médical ou justificatif de force majeure'
            }
        ]

    # ================================================================
    # 6. GÉNÉRER LE MESSAGE DE RÉPONSE
    # ================================================================
    result['response_message'] = generate_training_exam_options_message(
        exam_date=result['exam_date_formatted'],
        next_exam_date=result['next_exam_date_formatted'],
        force_majeure_detected=result['force_majeure_detected'],
        force_majeure_type=result['force_majeure_type'],
        missed_reason=missed_training.get('reason')
    )

    return result


def detect_missed_training_in_threads(threads: List[Dict]) -> Optional[Dict]:
    """
    Détecte si le candidat mentionne avoir manqué sa formation.

    Returns:
        Dict avec 'detected': True et 'reason' si trouvé, None sinon
    """
    from src.utils.text_utils import get_clean_thread_content

    # Patterns indiquant une formation manquée
    # IMPORTANT: Ces patterns doivent matcher UNIQUEMENT le message du candidat,
    # pas les mails de confirmation CAB qui contiennent des liens comme "Rejoindre le webinaire"
    patterns = [
        (r"n'ai\s+pas\s+pu\s+(?:assister|participer|suivre|rejoindre)", "impossibilité"),
        (r"pas\s+pu\s+(?:assister|participer|suivre|rejoindre)", "impossibilité"),
        (r"je\s+n'ai\s+pas\s+(?:pu\s+)?(?:assister|participer|suivre|rejoindre)", "impossibilité"),
        (r"manqu[ée]\s+(?:la\s+)?(?:formation|session|cours)", "formation manquée"),
        (r"j'ai\s+manqu[ée]", "formation manquée"),
        (r"absent[e]?\s+(?:à|de)\s+(?:la\s+)?(?:formation|session)", "absence"),
        (r"(?:état\s+de\s+)?sant[ée].*(?:pas\s+permis|emp[êe]ch[ée])", "raison médicale"),
        (r"hospitalis[ée]", "hospitalisation"),
        # Note: "maladie" seul est trop large, il faut un contexte de formation manquée
        (r"(?:pour\s+cause\s+de\s+|à\s+cause\s+de\s+(?:ma\s+)?)?maladie.*(?:pas\s+pu|manqu|absent)", "maladie"),
        # SUPPRIMÉ: le pattern "rejoindre.*webinaire" qui matchait les liens de confirmation
        # (r"(?:ne\s+)?(?:pas\s+)?(?:pouvoir\s+)?rejoindre.*(?:formation|webinaire)", "impossibilité de rejoindre"),
        (r"dossier\s+m[ée]dical", "dossier médical"),
        (r"certificat\s+m[ée]dical", "certificat médical"),
    ]

    for thread in threads:
        if thread.get('direction') != 'in':
            continue

        content = get_clean_thread_content(thread)
        content_lower = content.lower()

        for pattern, reason in patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                logger.info(f"  🔍 Formation manquée détectée: {reason}")
                return {
                    'detected': True,
                    'reason': reason,
                    'pattern': pattern
                }

    return None


def detect_missed_training_from_crm(deal_data: Dict) -> Optional[Dict]:
    """
    Détecte si la formation est manquée en analysant les données CRM.

    Condition: Session passée + Date d'examen future = Formation manquée

    Returns:
        Dict avec 'detected': True et 'reason' si détecté, None sinon
    """
    today = datetime.now().date()

    # Récupérer la session assignée
    session_raw = deal_data.get('Session')
    if not session_raw:
        return None

    # Extraire la date de fin de session
    session_name = session_raw.get('name', '') if isinstance(session_raw, dict) else str(session_raw)
    session_id = session_raw.get('id') if isinstance(session_raw, dict) else None

    # La date de fin de session doit être récupérée depuis le lookup enrichi ou le nom
    # Format typique: "cds-montreuil- thu2 - 12 janvier - 23 janvier 2026"
    # On doit parser la date de fin
    session_end_date = None

    # Essayer d'extraire la date de fin du nom de session
    # Pattern: "XX janvier/février/... 2026" à la fin
    import re
    date_pattern = r'(\d{1,2})\s*(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s*(\d{4})'
    matches = re.findall(date_pattern, session_name, re.IGNORECASE)
    if len(matches) >= 2:
        # Prendre la dernière date (date de fin)
        day, month_name, year = matches[-1]
        month_map = {
            'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
            'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
            'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12
        }
        month = month_map.get(month_name.lower(), 1)
        try:
            session_end_date = datetime(int(year), month, int(day)).date()
        except ValueError:
            pass

    if not session_end_date:
        return None

    # Récupérer la date d'examen
    exam_date_raw = deal_data.get('Date_examen_VTC')
    if not exam_date_raw:
        return None

    # Extraire la date d'examen (format: {'name': '94_2026-03-31', 'id': '...'} ou string)
    if isinstance(exam_date_raw, dict):
        exam_date_str = exam_date_raw.get('name', '')
        if '_' in exam_date_str:
            exam_date_str = exam_date_str.split('_')[1]
    else:
        exam_date_str = str(exam_date_raw)

    exam_date = parse_date_flexible(exam_date_str)
    if not exam_date:
        return None

    exam_date = exam_date.date() if hasattr(exam_date, 'date') else exam_date

    # Condition: Session passée ET examen futur
    if session_end_date < today and exam_date > today:
        logger.info(f"  🔍 Formation manquée détectée via CRM: session terminée le {session_end_date}, examen le {exam_date}")
        return {
            'detected': True,
            'reason': 'session_terminee',
            'session_end_date': session_end_date.isoformat(),
            'exam_date': exam_date.isoformat()
        }

    return None


def detect_force_majeure_in_threads(threads: List[Dict]) -> Dict:
    """
    Détecte si le candidat mentionne un motif de force majeure.

    Force majeure valide:
    - Certificat médical / hospitalisation / maladie grave
    - Décès d'un proche
    - Accident
    - Convocation judiciaire

    Returns:
        Dict avec 'detected': bool et 'type': str
    """
    from src.utils.text_utils import get_clean_thread_content

    # Patterns de force majeure par type
    medical_patterns = [
        r'certificat\s+m[ée]dical',
        r'hospitalis[ée]',
        r'hospitalisation',
        r'maladie\s+grave',
        r'op[ée]ration',
        r'chirurgie',
        r'accident',
        r'blessure',
        r'arr[êe]t\s+(?:de\s+)?travail',
        r'(?:état\s+de\s+)?sant[ée]',
        r'dossier\s+m[ée]dical',
        r'probl[èe]me\s+(?:de\s+)?sant[ée]',
    ]

    death_patterns = [
        r'd[ée]c[èe]s',
        r'deuil',
        r'enterrement',
        r'fun[ée]railles',
    ]

    other_patterns = [
        r'convocation\s+(?:judiciaire|tribunal)',
        r'force\s+majeure',
        r'catastrophe',
        r'sinistre',
    ]

    for thread in threads:
        if thread.get('direction') != 'in':
            continue

        content = get_clean_thread_content(thread)
        content_lower = content.lower()

        # Vérifier médical
        for pattern in medical_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                return {'detected': True, 'type': 'medical'}

        # Vérifier décès
        for pattern in death_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                return {'detected': True, 'type': 'death'}

        # Vérifier autres
        for pattern in other_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                return {'detected': True, 'type': 'other'}

    return {'detected': False, 'type': None}


def get_next_exam_date_after(
    current_exam_date: datetime,
    departement: str,
    crm_client
) -> Optional[Dict]:
    """
    Récupère la prochaine date d'examen APRÈS la date actuelle.

    En cas de report, la CMA positionne sur la prochaine date disponible,
    même si la date de clôture est passée.
    """
    try:
        # Extraire le numéro de département
        dept_num = str(departement)[:2] if departement else None
        if not dept_num:
            return None

        # Chercher les sessions d'examen pour ce département
        # Date_Examen > current_exam_date
        search_criteria = f"(Departement:equals:{dept_num})and(Date_Examen:greater_than:{current_exam_date.strftime('%Y-%m-%d')})"

        sessions = crm_client.search_records(
            module="Sessions1",
            criteria=search_criteria,
            fields=["Date_Examen", "Date_Cloture_Inscription", "Libelle_Affichage"],
            sort_by="Date_Examen",
            sort_order="asc",
            per_page=5
        )

        if sessions:
            # Retourner la première (prochaine) date
            return sessions[0]

        return None

    except Exception as e:
        logger.error(f"Erreur lors de la recherche de la prochaine date d'examen: {e}")
        return None


def generate_training_exam_options_message(
    exam_date: str,
    next_exam_date: Optional[str],
    force_majeure_detected: bool,
    force_majeure_type: Optional[str],
    missed_reason: Optional[str]
) -> str:
    """
    Génère le message présentant les 2 options au candidat.

    Points clés:
    - Informer le candidat de sa date d'examen (il peut ne pas être au courant)
    - Le justificatif force majeure doit couvrir le jour de l'EXAMEN (pas la formation)
    - La CMA gère les examens, pas la formation
    """
    # Adapter l'introduction selon si force majeure détectée
    if force_majeure_detected and force_majeure_type == 'medical':
        intro = """Nous avons bien pris connaissance de votre situation et comprenons que votre état de santé ne vous a pas permis de suivre la formation en visioconférence.

Nous espérons sincèrement que vous allez mieux."""
    elif force_majeure_detected and force_majeure_type == 'death':
        intro = """Nous avons bien pris connaissance de votre situation et vous présentons nos sincères condoléances.

Nous comprenons que les circonstances ne vous ont pas permis de suivre la formation."""
    else:
        intro = """Nous avons bien pris connaissance de votre message concernant la formation."""

    # Message principal avec les 2 options
    # IMPORTANT: Informer le candidat de sa date d'examen car il peut ne pas être au courant
    next_exam_info = f"le **{next_exam_date}**" if next_exam_date else "la prochaine date disponible"

    message = f"""Bonjour,

{intro}

**⚠️ Information importante : Vous êtes inscrit(e) à l'examen VTC du {exam_date}.**

La formation en visioconférence et le e-learning sont des outils de préparation, mais votre inscription à l'examen est déjà validée auprès de la CMA (Chambre des Métiers et de l'Artisanat).

Vous avez deux possibilités :

---

## Option A : Maintenir votre examen au {exam_date}

Si vous estimez que le **e-learning** (formation à distance) vous a permis d'acquérir les connaissances nécessaires, vous pouvez passer l'examen à la date prévue.

📚 **Rappel** : Vous avez accès aux cours en ligne sur : **https://elearning.cab-formations.fr**

La formation en visioconférence est un complément, mais n'est pas obligatoire pour se présenter à l'examen.

---

## Option B : Reporter votre examen

Si vous souhaitez reporter votre examen, **un justificatif de force majeure couvrant la date du {exam_date} est obligatoire**.

⚠️ **Attention** : Le certificat médical doit couvrir **le jour de l'examen** ({exam_date}), pas seulement la période de la formation.

En cas de report accepté par la CMA, vous serez repositionné(e) sur {next_exam_info}.

**Pour demander un report :**
1. Envoyez-nous un **certificat médical** (ou autre justificatif de force majeure) **couvrant la date du {exam_date}**
2. Nous transmettrons votre demande à la CMA
3. La CMA vous repositionnera sur la prochaine date d'examen disponible

⚠️ **Important** : Le simple fait de ne pas avoir suivi la formation n'est **pas** un motif valable de report auprès de la CMA. Seule la force majeure (maladie le jour de l'examen, accident, décès d'un proche, etc.) permet de reporter.

---

**Merci de nous indiquer votre choix** afin que nous puissions vous accompagner au mieux.

Cordialement,
{COMPANY_SIGNATURE}"""

    return message


def check_session_dates_consistency(
    proposed_sessions: List[Dict],
    exam_date: datetime
) -> Dict:
    """
    Vérifie que les sessions de formation proposées se terminent AVANT l'examen.

    Returns:
        {
            'consistent': bool,
            'valid_sessions': List[Dict],  # Sessions qui se terminent avant l'examen
            'invalid_sessions': List[Dict]  # Sessions qui se terminent après l'examen
        }
    """
    result = {
        'consistent': True,
        'valid_sessions': [],
        'invalid_sessions': []
    }

    for session in proposed_sessions:
        date_fin_str = session.get('Date_fin') or session.get('date_fin')
        if not date_fin_str:
            continue

        date_fin = parse_datetime_flexible(str(date_fin_str), "session_date_fin")
        if date_fin is None:
            continue

        # La formation doit se terminer AU MOINS 3 jours avant l'examen
        if date_fin <= exam_date - timedelta(days=3):
            result['valid_sessions'].append(session)
        else:
            result['invalid_sessions'].append(session)
            result['consistent'] = False
            logger.warning(
                f"  ⚠️ Session invalide: fin le {date_fin.strftime('%d/%m/%Y')} "
                f"mais examen le {exam_date.strftime('%d/%m/%Y')}"
            )

    return result


def detect_session_assignment_error(
    deal_data: Dict,
    enriched_lookups: Dict
) -> Dict:
    """
    Détecte si la session assignée est une ERREUR DE SAISIE.

    Logique:
    - Si la session se termine AVANT la date de création du deal
      → ERREUR DE SAISIE (impossible que le candidat y ait participé)
    - Si la session se termine APRÈS la date de création mais dans le passé
      → Formation passée normale (le candidat a pu y participer)

    Args:
        deal_data: Données du deal CRM (contient Created_Time)
        enriched_lookups: Données enrichies (contient session_date_fin)

    Returns:
        {
            'is_assignment_error': bool,
            'session_name': str or None,
            'session_end_date': str or None,
            'deal_created_date': str or None,
            'days_difference': int or None,  # Nombre de jours entre fin session et création deal
            'correct_year': int or None,  # Année probable correcte (si erreur d'année)
        }
    """
    result = {
        'is_assignment_error': False,
        'session_name': None,
        'session_end_date': None,
        'session_end_date_formatted': None,
        'deal_created_date': None,
        'deal_created_date_formatted': None,
        'days_difference': None,
        'correct_year': None,
        'error_type': None,  # 'wrong_year', 'wrong_session', etc.
    }

    # 1. Vérifier si une session est assignée
    session_end = enriched_lookups.get('session_date_fin')
    session_name = enriched_lookups.get('session_name')

    if not session_end:
        logger.debug("  ℹ️ Pas de session assignée - pas d'erreur possible")
        return result

    result['session_name'] = session_name
    result['session_end_date'] = session_end

    # 2. Récupérer la date de création du deal
    deal_created_raw = deal_data.get('Created_Time')
    if not deal_created_raw:
        logger.warning("  ⚠️ Pas de date de création du deal")
        return result

    # 3. Parser les dates
    try:
        session_end_date = parse_date_flexible(session_end)
        deal_created_date = parse_date_flexible(deal_created_raw)

        if not session_end_date or not deal_created_date:
            logger.warning(f"  ⚠️ Impossible de parser les dates: session={session_end}, deal={deal_created_raw}")
            return result

        result['session_end_date_formatted'] = session_end_date.strftime("%d/%m/%Y")
        result['deal_created_date_formatted'] = deal_created_date.strftime("%d/%m/%Y")
        result['deal_created_date'] = deal_created_date.strftime("%Y-%m-%d")

    except Exception as e:
        logger.error(f"  ❌ Erreur parsing dates: {e}")
        return result

    # 4. Comparer les dates
    days_diff = (deal_created_date - session_end_date).days
    result['days_difference'] = days_diff

    # Si le deal a été créé APRÈS la fin de la session → ERREUR
    if days_diff > 0:
        result['is_assignment_error'] = True
        logger.warning(
            f"  🚨 ERREUR DE SAISIE SESSION détectée: "
            f"Session '{session_name}' terminée le {result['session_end_date_formatted']} "
            f"mais deal créé le {result['deal_created_date_formatted']} "
            f"({days_diff} jours APRÈS)"
        )

        # Déterminer le type d'erreur
        session_year = session_end_date.year
        deal_year = deal_created_date.year

        if deal_year - session_year >= 1:
            # Erreur d'année probable (ex: mars 2024 au lieu de mars 2026)
            result['error_type'] = 'wrong_year'
            result['correct_year'] = deal_year
            # Ou l'année suivante si on est en fin d'année
            if deal_created_date.month >= 10 and session_end_date.month <= 3:
                result['correct_year'] = deal_year + 1
            # Extraire le mois de la session erronée pour trouver l'équivalente
            result['wrong_session_month'] = session_end_date.month
            result['wrong_session_type'] = enriched_lookups.get('session_type')  # 'jour' ou 'soir'
            logger.info(f"  💡 Erreur d'année probable: {session_year} → {result['correct_year']} (mois: {session_end_date.month}, type: {result['wrong_session_type']})")
        else:
            result['error_type'] = 'wrong_session'
    else:
        logger.debug(
            f"  ✅ Session OK: Deal créé {abs(days_diff)} jours AVANT la fin de session"
        )

    return result


def find_corrected_session_for_year_error(
    session_error_data: Dict,
    exam_date: str,
    crm_client
) -> Optional[Dict]:
    """
    Trouve la session corrigée quand l'erreur est une mauvaise année.

    Ex: Session mars 2024 soir assignée → trouver mars 2026 soir

    Args:
        session_error_data: Résultat de detect_session_assignment_error
        exam_date: Date d'examen (format YYYY-MM-DD)
        crm_client: Client CRM pour chercher les sessions

    Returns:
        Dict avec la session corrigée ou None si pas trouvée
        {
            'id': str,
            'Name': str,
            'session_type': str,
            'date_debut': str,
            'date_fin': str,
        }
    """
    if session_error_data.get('error_type') != 'wrong_year':
        logger.debug("  ℹ️ Pas une erreur d'année - pas de correction automatique")
        return None

    correct_year = session_error_data.get('correct_year')
    wrong_month = session_error_data.get('wrong_session_month')
    session_type = session_error_data.get('wrong_session_type')  # 'jour' ou 'soir'

    if not all([correct_year, wrong_month, session_type]):
        logger.warning(f"  ⚠️ Données insuffisantes pour correction: year={correct_year}, month={wrong_month}, type={session_type}")
        return None

    # Parser la date d'examen pour filtrer les sessions qui se terminent avant
    exam_date_parsed = parse_date_flexible(exam_date)
    if not exam_date_parsed:
        logger.warning(f"  ⚠️ Impossible de parser la date d'examen: {exam_date}")
        return None

    logger.info(f"  🔍 Recherche session corrigée: mois={wrong_month}, type={session_type}, année={correct_year}")

    try:
        # Construire la plage de dates pour le mois cible
        # Ex: mars 2026 → chercher sessions dont Date_fin est entre 01/03/2026 et 31/03/2026
        from datetime import date
        import calendar

        last_day = calendar.monthrange(correct_year, wrong_month)[1]
        month_start = date(correct_year, wrong_month, 1)
        month_end = date(correct_year, wrong_month, last_day)

        # Chercher les sessions Uber (VISIO Zoom VTC) du bon type qui se terminent dans le bon mois
        # et AVANT la date d'examen
        sessions = crm_client.get_records(
            'Sessions1',
            fields=['Name', 'Date_d_but', 'Date_fin', 'session_type', 'Lieu_de_formation'],
            per_page=200
        )

        matching_sessions = []
        for s in sessions:
            # Vérifier le type (jour/soir)
            if s.get('session_type') != session_type:
                continue

            # Vérifier que c'est une session Uber (VISIO)
            lieu = s.get('Lieu_de_formation', '')
            if 'VISIO' not in str(lieu).upper():
                continue

            # Vérifier la date de fin
            date_fin_str = s.get('Date_fin')
            if not date_fin_str:
                continue

            date_fin = parse_date_flexible(date_fin_str)
            if not date_fin:
                continue

            # Doit être dans le bon mois ET avant l'examen
            if date_fin.month == wrong_month and date_fin.year == correct_year:
                if date_fin.date() < exam_date_parsed.date():
                    matching_sessions.append({
                        'id': s.get('id'),
                        'Name': s.get('Name'),
                        'session_type': s.get('session_type'),
                        'date_debut': s.get('Date_d_but'),
                        'date_fin': s.get('Date_fin'),
                    })

        if matching_sessions:
            # Prendre la session la plus proche de l'examen (dernière du mois)
            matching_sessions.sort(key=lambda x: x.get('date_fin', ''), reverse=True)
            best_match = matching_sessions[0]
            logger.info(f"  ✅ Session corrigée trouvée: {best_match['Name']} ({best_match['date_debut']} - {best_match['date_fin']})")
            return best_match
        else:
            logger.warning(f"  ⚠️ Aucune session {session_type} trouvée pour {wrong_month}/{correct_year} avant examen {exam_date}")
            return None

    except Exception as e:
        logger.error(f"  ❌ Erreur lors de la recherche de session corrigée: {e}")
        return None
