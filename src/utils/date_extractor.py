"""
Date Extractor — extraction fiable des dates depuis les messages candidats.

Post-processing après le triage LLM. Le triage détecte l'intention,
le date extractor rattrape les dates que le triage a oubliées.

Étape 1: Regex déterministe (toujours, 0ms, 0$)
Étape 2: LLM Haiku pour catégoriser (seulement si regex trouve des dates, ~$0.001)
"""
import re
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import anthropic

from src.constants.models import MODEL_EXTRACTION

logger = logging.getLogger(__name__)

# Mois français → numéro
MOIS_FR = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3,
    'avril': 4, 'mai': 5, 'juin': 6, 'juillet': 7,
    'août': 8, 'aout': 8, 'septembre': 9, 'octobre': 10,
    'novembre': 11, 'décembre': 12, 'decembre': 12,
    'janv': 1, 'fév': 2, 'fev': 2, 'avr': 4,
    'juil': 7, 'sept': 9, 'oct': 10, 'nov': 11, 'déc': 12, 'dec': 12,
}

MOIS_PATTERN = '|'.join(sorted(MOIS_FR.keys(), key=len, reverse=True))

# Année par défaut pour les dates sans année
DEFAULT_YEAR = datetime.now().year
if datetime.now().month >= 10:
    DEFAULT_YEAR = datetime.now().year + 1


def _extract_dates_regex(message: str) -> List[Dict]:
    """Étape 1 : extraction déterministe des dates par regex."""
    dates = []
    text = message.lower()

    # Pattern 1: "29 septembre 2026" ou "29 septembre"
    for match in re.finditer(
        rf'(\d{{1,2}})\s+({MOIS_PATTERN})(?:\s+(\d{{4}}))?', text
    ):
        jour = int(match.group(1))
        mois = MOIS_FR[match.group(2)]
        annee = int(match.group(3)) if match.group(3) else DEFAULT_YEAR
        if 1 <= jour <= 31:
            dates.append({
                'date_iso': f'{annee}-{mois:02d}-{jour:02d}',
                'month': mois,
                'raw': match.group(0).strip(),
            })

    # Pattern 2: "29/09/2026" ou "29/09"
    for match in re.finditer(r'(\d{1,2})/(\d{1,2})(?:/(\d{4}))?', text):
        jour = int(match.group(1))
        mois = int(match.group(2))
        annee = int(match.group(3)) if match.group(3) else DEFAULT_YEAR
        if 1 <= jour <= 31 and 1 <= mois <= 12:
            dates.append({
                'date_iso': f'{annee}-{mois:02d}-{jour:02d}',
                'month': mois,
                'raw': match.group(0).strip(),
            })

    # Pattern 3: mois seul — "en septembre", "mois d'avril", "session d'avril"
    for match in re.finditer(
        rf"(?:en|mois d[e' ]|session d[e' ]|d[ée]but|fin|courant)\s*({MOIS_PATTERN})", text
    ):
        mois = MOIS_FR[match.group(1)]
        # Vérifier que ce mois n'est pas déjà dans une date exacte
        if not any(d['month'] == mois for d in dates):
            dates.append({
                'date_iso': None,
                'month': mois,
                'raw': match.group(0).strip(),
            })

    # Pattern 4: mois isolé avec contexte de demande
    for match in re.finditer(
        rf"(?:reporter|changer|passer|inscrire|décaler|repousser).*?(?:en|au|à|pour)\s+({MOIS_PATTERN})", text
    ):
        mois = MOIS_FR[match.group(1)]
        if not any(d['month'] == mois for d in dates):
            dates.append({
                'date_iso': None,
                'month': mois,
                'raw': match.group(0).strip(),
            })

    # Dédupliquer par date_iso ou month
    seen = set()
    unique = []
    for d in dates:
        key = d['date_iso'] or f"month_{d['month']}"
        if key not in seen:
            seen.add(key)
            unique.append(d)

    return unique


def _categorize_with_llm(message: str, regex_dates: List[Dict]) -> Optional[Dict]:
    """Étape 2 : catégorisation LLM des dates extraites."""
    try:
        from config import settings
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        dates_str = json.dumps(regex_dates, ensure_ascii=False)

        response = client.messages.create(
            model=MODEL_EXTRACTION,
            max_tokens=300,
            system="Tu catégorises des dates extraites d'un message de candidat VTC. Réponds UNIQUEMENT en JSON valide.",
            messages=[{"role": "user", "content": f"""Message du candidat :
"{message}"

Dates détectées : {dates_str}

Pour chaque date, détermine :
- type : "examen" (date d'examen demandée/mentionnée) | "formation" (session de formation) | "disponibilite" (période de disponibilité)
- is_request : true si le candidat DEMANDE activement ce changement, false s'il mentionne simplement une date

Puis détermine :
- requested_month : le mois (1-12) que le candidat DEMANDE pour un changement de date (examen OU formation). null si aucune demande.
- confirmed_exam_date : la date exacte d'examen demandée au format YYYY-MM-DD. null si pas de date exacte.

JSON :
{{"dates": [...], "requested_month": int|null, "confirmed_exam_date": "YYYY-MM-DD"|null}}"""}],
            timeout=10.0,
        )

        raw = response.content[0].text.strip()
        # Extraire JSON
        if '```' in raw:
            raw = raw.split('```')[1].replace('json', '').strip()
        return json.loads(raw)

    except anthropic.APITimeoutError:
        logger.warning("DateExtractor: LLM timeout (10s)")
        return None
    except Exception as e:
        logger.warning(f"DateExtractor: LLM error: {str(e)[:80]}")
        return None


def extract_dates_from_message(message: str) -> Dict:
    """
    Extraction complète des dates depuis un message candidat.

    Returns:
        {
            'requested_month': int or None,
            'mentioned_month': int or None,
            'confirmed_new_exam_date': str or None,  # YYYY-MM-DD
            'extracted_dates': list,
        }
    """
    result = {
        'requested_month': None,
        'mentioned_month': None,
        'confirmed_new_exam_date': None,
        'extracted_dates': [],
    }

    if not message or len(message.strip()) < 10:
        return result

    # Étape 1: Regex
    regex_dates = _extract_dates_regex(message)

    if not regex_dates:
        logger.debug("DateExtractor: aucune date détectée par regex")
        return result

    logger.info(f"DateExtractor: {len(regex_dates)} date(s) détectée(s) par regex: {[d['raw'] for d in regex_dates]}")
    result['extracted_dates'] = regex_dates

    # Si une seule date trouvée et c'est un mois, on peut inférer sans LLM
    if len(regex_dates) == 1 and not regex_dates[0]['date_iso']:
        result['mentioned_month'] = regex_dates[0]['month']
        # Heuristique : si le message contient des verbes de demande, c'est requested
        demand_keywords = ['changer', 'reporter', 'passer', 'inscrire', 'décaler',
                           'repousser', 'souhaite', 'voudrais', 'veux', 'possible',
                           'disponible', 'préfère']
        if any(kw in message.lower() for kw in demand_keywords):
            result['requested_month'] = regex_dates[0]['month']
            logger.info(f"DateExtractor: mois demandé inféré sans LLM: {result['requested_month']}")
            return result

    # Si une date exacte et un verbe de demande → inférer sans LLM
    if len(regex_dates) == 1 and regex_dates[0]['date_iso']:
        demand_keywords = ['changer', 'reporter', 'passer', 'inscrire', 'décaler',
                           'repousser', 'souhaite', 'voudrais', 'veux', 'comme convenu',
                           'confirme']
        if any(kw in message.lower() for kw in demand_keywords):
            result['requested_month'] = regex_dates[0]['month']
            result['confirmed_new_exam_date'] = regex_dates[0]['date_iso']
            logger.info(f"DateExtractor: date demandée inférée sans LLM: {result['confirmed_new_exam_date']}")
            return result

    # Étape 2: LLM pour cas ambigus (plusieurs dates, ou contexte flou)
    llm_result = _categorize_with_llm(message, regex_dates)
    if llm_result:
        if llm_result.get('requested_month'):
            result['requested_month'] = int(llm_result['requested_month'])
        if llm_result.get('confirmed_exam_date'):
            result['confirmed_new_exam_date'] = llm_result['confirmed_exam_date']
        # mentioned_month = premier mois trouvé si pas de requested
        if not result['requested_month'] and regex_dates:
            result['mentioned_month'] = regex_dates[0]['month']
        logger.info(f"DateExtractor LLM: requested_month={result['requested_month']}, confirmed={result['confirmed_new_exam_date']}")
    else:
        # Fallback: si LLM échoue, utiliser le premier mois comme mentioned
        result['mentioned_month'] = regex_dates[0]['month']
        logger.info(f"DateExtractor: LLM fallback, mentioned_month={result['mentioned_month']}")

    return result
