"""
Helper centralisé pour la lecture des champs lookup CRM.

Les champs Date_examen_VTC et Session sont des lookups qui retournent
{name, id}. Ce module centralise les appels aux modules Zoho CRM pour
récupérer les vraies données via get_record().

MODULES DE RÉFÉRENCE:
- Date_examen_VTC -> module "Dates_Examens_VTC_TAXI"
  Champs: Date_Examen, Departement, Date_Cloture_Inscription
- Session -> module "Sessions1"
  Champs: Name, session_type, Date_d_but, Date_de_fin
"""
from typing import Dict, Any, Optional
import logging

from src.constants.sessions import SESSION_TYPE_JOUR, SESSION_TYPE_SOIR

logger = logging.getLogger(__name__)

# Mapping des champs lookup vers leurs modules CRM
LOOKUP_MODULE_MAP = {
    'Date_examen_VTC': 'Dates_Examens_VTC_TAXI',
    'Session': 'Sessions1',
}


def enrich_lookup_field(
    crm_client,
    deal_data: Dict[str, Any],
    field_name: str,
    cache: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """
    Enrichit un champ lookup en récupérant les données complètes du module CRM.

    Args:
        crm_client: Instance de ZohoCRMClient
        deal_data: Données du deal contenant le lookup
        field_name: Nom du champ lookup (ex: 'Date_examen_VTC', 'Session')
        cache: Dict optionnel pour cache (clé = f"{module}_{id}")

    Returns:
        Dict avec les données complètes du record ou None si non trouvé
    """
    lookup_value = deal_data.get(field_name)

    if not lookup_value:
        return None

    # Si ce n'est pas un dict avec id, ce n'est pas un lookup standard
    if not isinstance(lookup_value, dict) or 'id' not in lookup_value:
        logger.debug(f"  {field_name} n'est pas un lookup standard: {type(lookup_value)}")
        return None

    lookup_id = lookup_value.get('id')
    module_name = LOOKUP_MODULE_MAP.get(field_name)

    if not module_name:
        logger.warning(f"  Module inconnu pour le champ lookup: {field_name}")
        return None

    # Vérifier le cache
    cache_key = f"{module_name}_{lookup_id}"
    if cache is not None and cache_key in cache:
        logger.debug(f"  Cache hit pour {field_name}: {cache_key}")
        return cache[cache_key]

    # Appeler le module CRM
    try:
        record = crm_client.get_record(module_name, lookup_id)
        if record is not None:
            logger.debug(f"  ✅ {field_name} enrichi depuis {module_name}")
            # Mettre en cache
            if cache is not None:
                cache[cache_key] = record
            return record
        else:
            logger.warning(f"  Record non trouvé: {module_name}/{lookup_id}")
            return None
    except Exception as e:
        logger.warning(f"  Erreur récupération {field_name} depuis {module_name}: {e}")
        return None


def enrich_deal_lookups(
    crm_client,
    deal_data: Dict[str, Any],
    cache: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Enrichit tous les champs lookup d'un deal (Date_examen_VTC et Session).

    Args:
        crm_client: Instance de ZohoCRMClient
        deal_data: Données du deal
        cache: Dict optionnel pour cache partagé

    Returns:
        Dict avec les records enrichis:
        {
            'date_examen_record': {...} ou None,
            'session_record': {...} ou None,
            'date_examen': '2026-03-31' ou None,
            'date_cloture': '2026-03-15' ou None,
            'departement': '75' ou None,
            'session_type': 'jour' ou 'soir' ou None,
        }
    """
    if cache is None:
        cache = {}

    result = {
        'date_examen_record': None,
        'session_record': None,
        'date_examen': None,
        'date_cloture': None,
        'departement': None,
        'session_type': None,
        'session_name': None,
        'session_date_debut': None,
        'session_date_fin': None,
    }

    # Enrichir Date_examen_VTC
    date_examen_record = enrich_lookup_field(crm_client, deal_data, 'Date_examen_VTC', cache)
    if date_examen_record is not None:
        result['date_examen_record'] = date_examen_record
        result['date_examen'] = date_examen_record.get('Date_Examen')
        result['date_cloture'] = date_examen_record.get('Date_Cloture_Inscription')
        result['departement'] = date_examen_record.get('Departement')
        logger.info(f"  📅 Date_Examen: {result['date_examen']}, Clôture: {result['date_cloture']}, Dept: {result['departement']}")

    # Enrichir Session
    session_record = enrich_lookup_field(crm_client, deal_data, 'Session', cache)
    if session_record is not None:
        result['session_record'] = session_record
        result['session_name'] = session_record.get('Name')
        result['session_date_debut'] = session_record.get('Date_d_but')
        result['session_date_fin'] = session_record.get('Date_fin')
        # Session type: d'abord le champ, sinon déduit du nom (cds=soir, cdj=jour)
        session_type = session_record.get('session_type')
        if not session_type and result['session_name']:
            name_lower = result['session_name'].lower()
            if SESSION_TYPE_SOIR in name_lower or 'cours du soir' in name_lower:
                session_type = 'soir'
            elif SESSION_TYPE_JOUR in name_lower or 'cours du jour' in name_lower:
                session_type = 'jour'
        result['session_type'] = session_type
        logger.info(f"  📚 Session: {result['session_name']} ({result['session_type']}) du {result['session_date_debut']} au {result['session_date_fin']}")

    return result


# ============================================================================
# HELPER FUNCTIONS - Accès simplifié aux données enrichies
# ============================================================================

def get_real_exam_date(enriched_lookups: Dict[str, Any]) -> Optional[str]:
    """
    Récupère la vraie date d'examen (format YYYY-MM-DD).

    Args:
        enriched_lookups: Dict retourné par enrich_deal_lookups()

    Returns:
        Date au format YYYY-MM-DD ou None
    """
    return enriched_lookups.get('date_examen')


def get_real_cloture_date(enriched_lookups: Dict[str, Any]) -> Optional[str]:
    """
    Récupère la vraie date de clôture d'inscription (format YYYY-MM-DD).

    Args:
        enriched_lookups: Dict retourné par enrich_deal_lookups()

    Returns:
        Date au format YYYY-MM-DD ou None
    """
    return enriched_lookups.get('date_cloture')


def get_real_departement(enriched_lookups: Dict[str, Any]) -> Optional[str]:
    """
    Récupère le vrai département de l'examen.

    Args:
        enriched_lookups: Dict retourné par enrich_deal_lookups()

    Returns:
        Code département (ex: '75', '93') ou None
    """
    return enriched_lookups.get('departement')


def get_session_details(enriched_lookups: Dict[str, Any]) -> Dict[str, Any]:
    """
    Récupère les détails complets de la session.

    Args:
        enriched_lookups: Dict retourné par enrich_deal_lookups()

    Returns:
        Dict avec session_type, session_name, date_debut, date_fin
    """
    return {
        'session_type': enriched_lookups.get('session_type'),
        'session_name': enriched_lookups.get('session_name'),
        'date_debut': enriched_lookups.get('session_date_debut'),
        'date_fin': enriched_lookups.get('session_date_fin'),
        'record': enriched_lookups.get('session_record'),
    }


def get_session_type(enriched_lookups: Dict[str, Any]) -> Optional[str]:
    """
    Récupère le type de session (jour/soir).

    Args:
        enriched_lookups: Dict retourné par enrich_deal_lookups()

    Returns:
        'jour', 'soir', ou None
    """
    return enriched_lookups.get('session_type')
