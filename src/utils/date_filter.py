"""
DateFilter - Centralise la logique de filtrage des dates d'examen.

Remplace les 5 écritures dispersées de next_dates dans doc_ticket_workflow.py
par une source de vérité unique et des méthodes de filtrage composables.

Usage:
    from src.utils.date_filter import DateFilter

    # Filtrage simple
    df = DateFilter(next_dates)
    filtered = df.exclude_current('2026-03-31').limit(3).get()

    # Filtrage complexe
    filtered = (DateFilter(next_dates)
        .exclude_current(current_date)
        .filter_by_month(requested_month)
        .exclude_past_deadlines()
        .limit(1)
        .get())
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

from src.utils.date_utils import parse_date_flexible

logger = logging.getLogger(__name__)


class DateFilter:
    """
    Filtre composable pour les dates d'examen.

    Permet de chaîner les opérations de filtrage de manière lisible
    et centralisée, éliminant les 5 écritures dispersées de next_dates.
    """

    def __init__(self, dates: Optional[List[Dict[str, Any]]] = None):
        """
        Initialise le filtre avec une liste de dates.

        Args:
            dates: Liste de dictionnaires représentant les dates d'examen.
                   Chaque dict doit avoir 'Date_Examen' ou 'date_examen'.
        """
        self._dates = list(dates) if dates else []
        self._original_count = len(self._dates)

    def exclude_current(self, current_date: Optional[str]) -> 'DateFilter':
        """
        Exclut la date d'examen actuelle du candidat.

        Args:
            current_date: Date actuelle au format 'YYYY-MM-DD' ou avec timestamp.

        Returns:
            Self pour chaînage.
        """
        if not current_date or not self._dates:
            return self

        # Normaliser la date (extraire les 10 premiers caractères)
        current_date_str = str(current_date)[:10]

        before_count = len(self._dates)
        self._dates = [
            d for d in self._dates
            if self._get_date_str(d) != current_date_str
        ]

        if len(self._dates) != before_count:
            logger.debug(f"DateFilter.exclude_current: exclu {current_date_str}")

        return self

    def filter_by_month(self, month: Optional[int]) -> 'DateFilter':
        """
        Filtre pour garder les dates du mois demandé ou après.

        Args:
            month: Mois demandé (1-12), ou None pour ne pas filtrer.

        Returns:
            Self pour chaînage.
        """
        if not month or not isinstance(month, int) or not 1 <= month <= 12:
            return self

        if not self._dates:
            return self

        filtered = []
        for date_info in self._dates:
            date_str = self._get_date_str(date_info)
            if date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                    # Garder les dates du mois demandé ou après
                    if date_obj.month >= month:
                        filtered.append(date_info)
                except ValueError:
                    # En cas d'erreur de parsing, garder la date
                    filtered.append(date_info)

        before_count = len(self._dates)
        self._dates = filtered

        if len(self._dates) != before_count:
            logger.debug(f"DateFilter.filter_by_month({month}): {before_count} → {len(self._dates)}")

        return self

    def filter_exact_month(self, month: Optional[int]) -> 'DateFilter':
        """
        Filtre pour garder uniquement les dates du mois exact demandé.

        Args:
            month: Mois demandé (1-12), ou None pour ne pas filtrer.

        Returns:
            Self pour chaînage.
        """
        if not month or not isinstance(month, int) or not 1 <= month <= 12:
            return self

        if not self._dates:
            return self

        filtered = []
        for date_info in self._dates:
            date_str = self._get_date_str(date_info)
            if date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                    if date_obj.month == month:
                        filtered.append(date_info)
                except ValueError:
                    pass

        self._dates = filtered
        return self

    def exclude_past_deadlines(self, min_days: int = 0) -> 'DateFilter':
        """
        Exclut les dates dont la clôture est passée ou trop proche.

        Args:
            min_days: Nombre minimum de jours avant la clôture (défaut: 0 = aujourd'hui).

        Returns:
            Self pour chaînage.
        """
        if not self._dates:
            return self

        today = datetime.now().date()
        min_deadline = today + timedelta(days=min_days)

        filtered = []
        for date_info in self._dates:
            deadline_str = date_info.get('Date_Cloture') or date_info.get('date_cloture')
            if deadline_str:
                deadline = parse_date_flexible(str(deadline_str)[:10], "deadline")
                if deadline is not None:
                    if deadline >= min_deadline:
                        filtered.append(date_info)
                else:
                    # En cas d'erreur de parsing, garder la date
                    filtered.append(date_info)
            else:
                # Pas de deadline connue, garder la date
                filtered.append(date_info)

        before_count = len(self._dates)
        self._dates = filtered

        if len(self._dates) != before_count:
            logger.debug(f"DateFilter.exclude_past_deadlines(min={min_days}): {before_count} → {len(self._dates)}")

        return self

    def limit(self, n: int) -> 'DateFilter':
        """
        Limite le nombre de résultats.

        Args:
            n: Nombre maximum de dates à retourner.

        Returns:
            Self pour chaînage.
        """
        if n > 0:
            self._dates = self._dates[:n]
        return self

    def sort_by_date(self, ascending: bool = True) -> 'DateFilter':
        """
        Trie les dates par Date_Examen.

        Args:
            ascending: True pour ordre croissant (plus proche d'abord).

        Returns:
            Self pour chaînage.
        """
        self._dates.sort(
            key=lambda d: self._get_date_str(d) or '',
            reverse=not ascending
        )
        return self

    def get(self) -> List[Dict[str, Any]]:
        """
        Retourne la liste filtrée finale.

        Returns:
            Liste des dates après application de tous les filtres.
        """
        return self._dates

    def has_date_in_month(self, month: int) -> bool:
        """
        Vérifie si au moins une date est dans le mois exact demandé.

        Args:
            month: Mois à vérifier (1-12).

        Returns:
            True si au moins une date est dans ce mois.
        """
        if not month or not isinstance(month, int) or not 1 <= month <= 12:
            return False

        for date_info in self._dates:
            date_str = self._get_date_str(date_info)
            if date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d')
                    if date_obj.month == month:
                        return True
                except ValueError:
                    pass

        return False

    @property
    def count(self) -> int:
        """Nombre de dates actuellement dans le filtre."""
        return len(self._dates)

    @property
    def is_empty(self) -> bool:
        """True si aucune date ne reste après filtrage."""
        return len(self._dates) == 0

    @property
    def original_count(self) -> int:
        """Nombre de dates avant tout filtrage."""
        return self._original_count

    def _get_date_str(self, date_info: Dict[str, Any]) -> str:
        """Extrait la date au format YYYY-MM-DD depuis un dict de date."""
        date_val = date_info.get('Date_Examen') or date_info.get('date_examen') or ''
        return str(date_val)[:10] if date_val else ''

    def __len__(self) -> int:
        return len(self._dates)

    def __bool__(self) -> bool:
        return len(self._dates) > 0

    def __repr__(self) -> str:
        return f"DateFilter({len(self._dates)} dates, original={self._original_count})"


# ============================================================================
# FONCTIONS UTILITAIRES POUR MIGRATION PROGRESSIVE
# ============================================================================

def apply_final_filter(
    dates: List[Dict[str, Any]],
    current_date: Optional[str] = None,
    limit: int = 1
) -> List[Dict[str, Any]]:
    """
    Applique le filtre final standard (exclure date actuelle + limiter).

    Remplace le pattern récurrent:
        filtered = [d for d in dates if d['Date_Examen'][:10] != current][:1]

    Args:
        dates: Liste des dates à filtrer.
        current_date: Date actuelle à exclure.
        limit: Nombre max de résultats.

    Returns:
        Liste filtrée.
    """
    return (DateFilter(dates)
            .exclude_current(current_date)
            .limit(limit)
            .get())


def filter_for_intent(
    dates: List[Dict[str, Any]],
    current_date: Optional[str] = None,
    requested_month: Optional[int] = None,
    is_confirmation: bool = False,
    limit: int = 3
) -> List[Dict[str, Any]]:
    """
    Filtre les dates selon le contexte d'intention.

    Args:
        dates: Liste des dates à filtrer.
        current_date: Date actuelle à exclure.
        requested_month: Mois demandé par le candidat.
        is_confirmation: True si c'est une confirmation (pas d'alternatives).
        limit: Nombre max de résultats.

    Returns:
        Liste filtrée selon le contexte.
    """
    df = DateFilter(dates)

    if is_confirmation:
        # Confirmation = pas d'alternatives, juste la date confirmée
        return dates[:1] if dates else []

    df = df.exclude_current(current_date)

    if requested_month:
        df = df.filter_by_month(requested_month)

    return df.limit(limit).get()
