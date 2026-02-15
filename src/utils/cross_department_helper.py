"""
Helper centralise pour la comparaison cross-departement.
Utilise DEPT_TO_REGION et REGION_TO_DEPTS de date_examen_vtc_helper.py
"""

import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from .date_utils import parse_date_flexible
from .date_examen_vtc_helper import (
    DEPT_TO_REGION,
    REGION_TO_DEPTS,
    get_earlier_dates_other_departments
)

logger = logging.getLogger(__name__)


def get_cross_department_alternatives(
    crm_client,
    current_dept: str,
    reference_date: str,
    compte_existe: bool = False,
    limit: int = 5
) -> Dict[str, Any]:
    """
    Recherche intelligente de dates alternatives dans d'autres departements.

    Args:
        crm_client: Client Zoho CRM
        current_dept: Departement actuel (ex: "75")
        reference_date: Date de reference YYYY-MM-DD
        compte_existe: True si compte ExamT3P existe (process changement requis)
        limit: Nombre max de resultats par categorie

    Returns:
        {
            'same_region_options': [...],
            'other_region_options': [...],
            'has_same_region_options': bool,
            'has_other_region_options': bool,
            'earliest_option': {...},
            'days_could_save': int,
            'urgency_level': 'high'|'medium'|'low',
            'closest_closure_days': int,
            'requires_department_change_process': bool,
            'current_region': str,
        }
    """
    logger.info(f"🔍 Cross-department search: dept={current_dept}, ref_date={reference_date}, compte={compte_existe}")

    # Recuperer toutes les dates plus tot (fonction existante)
    all_earlier = get_earlier_dates_other_departments(
        crm_client, current_dept, reference_date, limit=20
    )

    if not all_earlier:
        logger.info("  → Aucune date plus tot trouvee")
        return _empty_result(current_dept, compte_existe)

    # Region actuelle
    current_region = DEPT_TO_REGION.get(current_dept, 'Autre')
    same_region_depts = set(REGION_TO_DEPTS.get(current_region, []))

    # Separer par region
    same_region = []
    other_region = []
    today = datetime.now().date()
    closest_closure_days = 999

    for date_info in all_earlier:
        dept = str(date_info.get('Departement', ''))

        # Calculer jours jusqu'a cloture
        cloture_str = date_info.get('Date_Cloture_Inscription', '')
        days_until_cloture = _calc_days_until(cloture_str, today)

        if days_until_cloture < 7:
            continue  # Cloture < 7 jours, pas assez de temps pour s'inscrire

        closest_closure_days = min(closest_closure_days, days_until_cloture)

        # Enrichir avec infos
        enriched = {
            **date_info,
            'days_until_cloture': days_until_cloture,
            'is_urgent': days_until_cloture < 5,
            'region': DEPT_TO_REGION.get(dept, 'Autre'),
            'date_examen_formatted': _format_date(date_info.get('Date_Examen', '')),
            'date_cloture_formatted': _format_date(cloture_str),
        }

        # Calculer comparison_text
        ref_date = parse_date_flexible(reference_date, "reference_date")
        exam_date = parse_date_flexible(date_info.get('Date_Examen', '')[:10], "Date_Examen")
        if ref_date and exam_date:
            days_earlier = (ref_date - exam_date).days
            enriched['days_earlier'] = days_earlier
            enriched['comparison_text'] = f"{days_earlier} jours plus tot"
        else:
            enriched['days_earlier'] = 0
            enriched['comparison_text'] = ''

        if dept in same_region_depts:
            same_region.append(enriched)
        else:
            other_region.append(enriched)

    # Limiter les resultats
    same_region = same_region[:limit]
    other_region = other_region[:limit]

    # Determiner l'option la plus proche
    all_options = same_region + other_region
    earliest = min(all_options, key=lambda x: x.get('Date_Examen', '')) if all_options else None

    # Niveau d'urgence base sur la cloture la plus proche
    urgency = 'high' if closest_closure_days < 3 else 'medium' if closest_closure_days < 7 else 'low'

    logger.info(f"  → same_region: {len(same_region)}, other_region: {len(other_region)}, urgency: {urgency}")

    return {
        'same_region_options': same_region,
        'other_region_options': other_region,
        'has_same_region_options': bool(same_region),
        'has_other_region_options': bool(other_region),
        'has_earlier_options': bool(same_region or other_region),
        'earliest_option': earliest,
        'days_could_save': earliest['days_earlier'] if earliest else 0,
        'urgency_level': urgency,
        'closest_closure_days': closest_closure_days if closest_closure_days < 999 else None,
        'requires_department_change_process': compte_existe,
        'current_region': current_region,
        'cma_departement': current_dept,
        'compte_existe': compte_existe,
    }


def _empty_result(current_dept: str, compte_existe: bool) -> Dict:
    """Retourne un resultat vide."""
    return {
        'same_region_options': [],
        'other_region_options': [],
        'has_same_region_options': False,
        'has_other_region_options': False,
        'has_earlier_options': False,
        'earliest_option': None,
        'days_could_save': 0,
        'urgency_level': 'low',
        'closest_closure_days': None,
        'requires_department_change_process': compte_existe,
        'current_region': DEPT_TO_REGION.get(current_dept, 'Autre'),
        'cma_departement': current_dept,
        'compte_existe': compte_existe,
    }


def _calc_days_until(date_str: str, today) -> int:
    """Calcule le nombre de jours jusqu'a une date."""
    if not date_str:
        return 999
    date_obj = parse_date_flexible(str(date_str)[:10], "date_str")
    if date_obj:
        return (date_obj - today).days
    return 999


def _format_date(date_str: str) -> str:
    """Formate une date au format DD/MM/YYYY."""
    if not date_str:
        return ''
    from src.utils.date_utils import format_date_for_display
    return format_date_for_display(date_str) or str(date_str)


def get_dates_for_month_other_departments(
    crm_client,
    current_dept: str,
    requested_month: int,
    requested_year: int = None,
    compte_existe: bool = False,
    limit: int = 5
) -> Dict[str, Any]:
    """
    Recherche des dates d'examen pour un mois specifique dans d'autres departements.

    Utilise pour le mode clarification quand le candidat mentionne un mois
    qui n'existe pas dans son departement.

    Args:
        crm_client: Client Zoho CRM
        current_dept: Departement actuel du candidat
        requested_month: Mois demande (1-12)
        requested_year: Annee demandee (defaut: annee courante ou prochaine)
        compte_existe: True si compte ExamT3P existe
        limit: Nombre max de resultats par region

    Returns:
        {
            'same_region_options': [...],
            'other_region_options': [...],
            'has_same_region_options': bool,
            'has_other_region_options': bool,
            'requested_month_name': str,
            'current_region': str,
            ...
        }
    """
    # Noms des mois en francais
    MONTH_NAMES = {
        1: 'janvier', 2: 'février', 3: 'mars', 4: 'avril',
        5: 'mai', 6: 'juin', 7: 'juillet', 8: 'août',
        9: 'septembre', 10: 'octobre', 11: 'novembre', 12: 'décembre'
    }

    month_name = MONTH_NAMES.get(requested_month, f'mois {requested_month}')
    logger.info(f"🔍 Cross-department search for month: {month_name} (dept={current_dept})")

    # Determiner l'annee si non specifiee
    today = datetime.now().date()
    if requested_year is None:
        # Si le mois est passe cette annee, chercher l'annee prochaine
        if requested_month < today.month:
            requested_year = today.year + 1
        else:
            requested_year = today.year

    # Recuperer TOUTES les dates d'examen disponibles
    try:
        from .date_examen_vtc_helper import get_next_exam_dates_any_department
        all_dates = get_next_exam_dates_any_department(crm_client, limit=100)
    except Exception as e:
        logger.warning(f"  → Erreur recuperation dates: {e}")
        return _empty_month_result(current_dept, compte_existe, month_name)

    if not all_dates:
        logger.info("  → Aucune date disponible")
        return _empty_month_result(current_dept, compte_existe, month_name)

    # Region actuelle
    current_region = DEPT_TO_REGION.get(current_dept, 'Autre')
    same_region_depts = set(REGION_TO_DEPTS.get(current_region, []))

    # Filtrer par mois et exclure le departement actuel
    same_region = []
    other_region = []

    for date_info in all_dates:
        dept = str(date_info.get('Departement', ''))

        # Exclure le departement actuel
        if dept == current_dept:
            continue

        # Verifier le mois
        exam_date_str = date_info.get('Date_Examen', '')
        if not exam_date_str:
            continue

        exam_date = parse_date_flexible(str(exam_date_str)[:10], "exam_date")
        if not exam_date:
            continue

        # Filtrer par mois et annee
        if exam_date.month != requested_month:
            continue
        if exam_date.year != requested_year:
            continue

        # Verifier que la cloture n'est pas passee
        cloture_str = date_info.get('Date_Cloture_Inscription', '')
        days_until_cloture = _calc_days_until(cloture_str, today)

        if days_until_cloture < 7:
            continue  # Cloture < 7 jours, pas assez de temps pour s'inscrire

        # Enrichir
        enriched = {
            **date_info,
            'days_until_cloture': days_until_cloture,
            'is_urgent': days_until_cloture < 5,
            'region': DEPT_TO_REGION.get(dept, 'Autre'),
            'date_examen_formatted': _format_date(exam_date_str),
            'date_cloture_formatted': _format_date(cloture_str),
        }

        if dept in same_region_depts:
            same_region.append(enriched)
        else:
            other_region.append(enriched)

    # Trier par date d'examen
    same_region.sort(key=lambda x: x.get('Date_Examen', ''))
    other_region.sort(key=lambda x: x.get('Date_Examen', ''))

    # Limiter
    same_region = same_region[:limit]
    other_region = other_region[:limit]

    logger.info(f"  → {month_name}: same_region={len(same_region)}, other_region={len(other_region)}")

    return {
        'same_region_options': same_region,
        'other_region_options': other_region,
        'has_same_region_options': bool(same_region),
        'has_other_region_options': bool(other_region),
        'has_month_options': bool(same_region or other_region),
        'requested_month': requested_month,
        'requested_month_name': month_name,
        'requested_year': requested_year,
        'current_region': current_region,
        'requires_department_change_process': compte_existe,
        'cma_departement': current_dept,
        'compte_existe': compte_existe,
    }


def _empty_month_result(current_dept: str, compte_existe: bool, month_name: str) -> Dict:
    """Retourne un resultat vide pour la recherche par mois."""
    return {
        'same_region_options': [],
        'other_region_options': [],
        'has_same_region_options': False,
        'has_other_region_options': False,
        'has_month_options': False,
        'requested_month_name': month_name,
        'current_region': DEPT_TO_REGION.get(current_dept, 'Autre'),
        'requires_department_change_process': compte_existe,
        'cma_departement': current_dept,
        'compte_existe': compte_existe,
    }
