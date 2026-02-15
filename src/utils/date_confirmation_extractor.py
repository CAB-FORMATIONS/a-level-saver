"""
Extracteur de date d'examen confirmée par le candidat.

Ce module détecte quand un candidat mentionne une date d'examen dans son message,
par exemple: "mon examen est programmé le 26 mai 2026 à Rennes"

Utilisé dans le cas d'auto-report: quand la date CRM est obsolète (passée + dossier non validé),
le candidat peut confirmer sa nouvelle date d'examen assignée par la CMA.
"""
import re
import logging
from datetime import datetime
from typing import Optional, Dict
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Mapping des mois français vers leur numéro
MONTH_FR = {
    'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3, 'avril': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8, 'aout': 8,
    'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12, 'decembre': 12
}


def extract_confirmed_exam_date(message: str) -> Optional[Dict]:
    """
    Extrait une date d'examen confirmée par le candidat dans son message.

    Args:
        message: Le contenu du message (peut être HTML)

    Returns:
        Dict avec:
            - 'date': Date au format YYYY-MM-DD
            - 'formatted': Date au format DD/MM/YYYY
            - 'raw': Le texte brut qui a matché
        Ou None si aucune date trouvée

    Examples:
        >>> extract_confirmed_exam_date("mon examen est programmé le 26 mai 2026")
        {'date': '2026-05-26', 'formatted': '26/05/2026', 'raw': 'examen est programmé le 26 mai 2026'}

        >>> extract_confirmed_exam_date("passage à l'examen VTC est programmé le 26 mai 2026 à Rennes")
        {'date': '2026-05-26', 'formatted': '26/05/2026', 'raw': "passage à l'examen vtc est programmé le 26 mai 2026"}
    """
    if not message:
        return None

    # Nettoyer le HTML si présent
    if '<' in message and '>' in message:
        try:
            soup = BeautifulSoup(message, 'html.parser')
            message = soup.get_text(separator=' ')
        except Exception:
            pass

    message_lower = message.lower()

    # Patterns de confirmation de date d'examen
    # Ordre: du plus spécifique au moins spécifique
    patterns = [
        # "mon examen est programmé le 26 mai 2026"
        r"(?:mon\s+)?(?:passage\s+(?:à\s+l')?)?examen(?:\s+vtc)?\s+(?:est\s+)?(?:prévu|programmé|fixé|planifié)\s+(?:le\s+)?(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\s+(\d{4})",

        # "inscrit pour l'examen du 26 mai 2026"
        r"inscrit[e]?\s+(?:pour\s+)?(?:l')?examen(?:\s+vtc)?\s+(?:du\s+)?(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\s+(\d{4})",

        # "examen prévu le 26/05/2026" (format numérique)
        r"examen(?:\s+vtc)?\s+(?:est\s+)?(?:prévu|programmé|fixé|planifié)\s+(?:le\s+)?(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})",

        # "passer l'examen le 26 mai 2026"
        r"passer\s+(?:mon\s+)?(?:l')?examen(?:\s+vtc)?\s+(?:le\s+)?(\d{1,2})\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\s+(\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, message_lower)
        if match:
            groups = match.groups()

            # Déterminer si c'est un format numérique (DD/MM/YYYY) ou textuel
            if groups[1].isdigit():
                # Format numérique: DD/MM/YYYY
                day = int(groups[0])
                month = int(groups[1])
                year = int(groups[2])
            else:
                # Format textuel: DD mois YYYY
                day = int(groups[0])
                month_name = groups[1].lower()
                year = int(groups[2])
                month = MONTH_FR.get(month_name)

                if not month:
                    continue

            # Valider la date
            try:
                date_obj = datetime(year, month, day)

                # Vérifier que la date est dans le futur (ou au moins pas trop dans le passé)
                today = datetime.now()
                if date_obj < today.replace(day=1, month=1):  # Pas avant le début de l'année
                    logger.debug(f"Date trouvée mais trop ancienne: {date_obj}")
                    continue

                result = {
                    'date': date_obj.strftime('%Y-%m-%d'),
                    'formatted': date_obj.strftime('%d/%m/%Y'),
                    'raw': match.group(0)
                }
                logger.info(f"  📅 Date confirmée extraite: {result['formatted']} (raw: '{result['raw']}')")
                return result

            except ValueError as e:
                logger.debug(f"Date invalide: {day}/{month}/{year} - {e}")
                continue

    return None
