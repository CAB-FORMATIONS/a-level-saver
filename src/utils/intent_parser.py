"""
IntentParser - Centralise l'extraction des données d'intention du triage.

Remplace les 6 duplications de:
    intent_context = triage_result.get('intent_context', {}) if triage_result else {}
    detected_intent = triage_result.get('detected_intent', '')
    session_preference = intent_context.get('session_preference')
    ...

Usage:
    intent = IntentParser(triage_result)
    print(intent.detected_intent)  # 'CONFIRMATION_SESSION'
    print(intent.session_preference)  # 'jour' ou 'soir'
    print(intent.requested_month)  # 3 (mars)
"""

from typing import Optional, Dict, Any, List


class IntentParser:
    """
    Parse et expose les données d'intention extraites par le TriageAgent.

    Centralise l'accès aux champs de intent_context pour éviter la duplication
    et les erreurs de typos sur les noms de clés.
    """

    def __init__(self, triage_result: Optional[Dict[str, Any]] = None):
        """
        Initialise le parser avec le résultat du triage.

        Args:
            triage_result: Dictionnaire retourné par TriageAgent.run()
                          Peut être None si le triage n'a pas été exécuté.
        """
        self._triage_result = triage_result or {}
        self._intent_context = self._triage_result.get('intent_context', {}) or {}

    @property
    def detected_intent(self) -> str:
        """L'intention principale détectée (ex: 'CONFIRMATION_SESSION', 'REPORT_DATE')."""
        return self._triage_result.get('detected_intent', '') or ''

    @property
    def action(self) -> str:
        """L'action de triage (GO, ROUTE, SPAM, DUPLICATE_UBER, NEEDS_CLARIFICATION)."""
        return self._triage_result.get('action', '') or ''

    @property
    def session_preference(self) -> Optional[str]:
        """Préférence de session exprimée ('jour' ou 'soir'), ou None."""
        return self._intent_context.get('session_preference')

    @property
    def requested_month(self) -> Optional[int]:
        """Mois demandé par le candidat (1-12), ou None."""
        month = self._intent_context.get('requested_month')
        if month and isinstance(month, int) and 1 <= month <= 12:
            return month
        return None

    @property
    def requested_location(self) -> Optional[str]:
        """Lieu/département demandé par le candidat, ou None."""
        return self._intent_context.get('requested_location')

    @property
    def confirmed_session_dates(self) -> Optional[Dict[str, str]]:
        """
        Dates de session confirmées par le candidat.

        Returns:
            Dict avec 'start' et 'end' si confirmées, sinon None.
            Exemple: {'start': '16/03/2026', 'end': '27/03/2026'}
        """
        return self._intent_context.get('confirmed_session_dates')

    @property
    def wants_earlier_date(self) -> bool:
        """True si le candidat demande une date plus tôt."""
        return bool(self._intent_context.get('wants_earlier_date'))

    @property
    def implicit_date_repositioning(self) -> bool:
        """True si le candidat demande implicitement un repositionnement de date (formation après examen)."""
        return bool(self._intent_context.get('implicit_date_repositioning'))

    @property
    def confirmed_new_exam_date(self) -> Optional[str]:
        """
        Date d'examen confirmée par le candidat (format YYYY-MM-DD).

        Returns:
            Date au format "YYYY-MM-DD" ou None.
        """
        return self._intent_context.get('confirmed_new_exam_date')

    @property
    def is_early_date_intent(self) -> bool:
        """True si l'intention est explicitement DEMANDE_DATE_PLUS_TOT."""
        return self.detected_intent == 'DEMANDE_DATE_PLUS_TOT'

    @property
    def is_confirmation_intent(self) -> bool:
        """True si l'intention est une confirmation (session, date, etc.)."""
        return self.detected_intent in [
            'CONFIRMATION_SESSION',
            'CONFIRMATION_DATE',
            'CONFIRMATION_INSCRIPTION'
        ]

    @property
    def is_report_intent(self) -> bool:
        """True si l'intention concerne un report de date."""
        return self.detected_intent in ['REPORT_DATE', 'DEMANDE_REINSCRIPTION']

    @property
    def needs_next_dates(self) -> bool:
        """True si l'intention nécessite de charger les prochaines dates disponibles."""
        return self.detected_intent in ['REPORT_DATE', 'DEMANDE_REINSCRIPTION']

    @property
    def mentioned_month(self) -> Optional[int]:
        """Mois mentionné par le candidat (différent de requested_month)."""
        month = self._intent_context.get('mentioned_month')
        if month and isinstance(month, int) and 1 <= month <= 12:
            return month
        return None

    @property
    def mentions_discrepancy(self) -> bool:
        """True si le candidat mentionne une discordance."""
        return bool(self._intent_context.get('mentions_discrepancy'))

    @property
    def communication_mode(self) -> str:
        """Mode de communication ('request', 'clarification', etc.)."""
        return self._intent_context.get('communication_mode', 'request') or 'request'

    @property
    def requested_dept_code(self) -> Optional[str]:
        """Code département demandé (ex: '34')."""
        return self._intent_context.get('requested_dept_code')

    @property
    def requested_training_dates(self) -> Optional[Dict[str, Any]]:
        """
        Dates de formation demandées par le candidat.

        Returns:
            Dict avec start_date, end_date, month, raw_text, is_range, inferred_preference
            ou None si non spécifié.
        """
        return self._intent_context.get('requested_training_dates')

    @property
    def has_date_range_request(self) -> bool:
        """True si le candidat a spécifié une plage de dates pour sa formation."""
        dates = self.requested_training_dates
        return dates is not None and dates.get('start_date') is not None

    @property
    def effective_session_preference(self) -> Optional[str]:
        """
        Préférence de session avec fallback sur l'inférence des dates.

        Priority:
        1. session_preference explicite
        2. inferred_preference depuis requested_training_dates
        """
        if self.session_preference:
            return self.session_preference
        dates = self.requested_training_dates
        if dates:
            return dates.get('inferred_preference')
        return None

    @property
    def is_complaint(self) -> bool:
        """True si le candidat signale une erreur d'inscription (plainte)."""
        return bool(self._intent_context.get('is_complaint'))

    @property
    def claimed_session(self) -> Optional[Dict[str, Any]]:
        """
        Session que le candidat affirme avoir demandée initialement.

        Returns:
            Dict avec claimed_type, claimed_dates, claimed_dates_raw
            ou None si non spécifié.
        """
        return self._intent_context.get('claimed_session')

    @property
    def assigned_session_wrong(self) -> Optional[Dict[str, Any]]:
        """
        Session erronée que le candidat a reçue (si mentionnée).

        Returns:
            Dict avec wrong_type, wrong_dates, wrong_dates_raw
            ou None si non spécifié.
        """
        return self._intent_context.get('assigned_session_wrong')

    @property
    def raw_context(self) -> Dict[str, Any]:
        """Accès direct au intent_context complet (pour cas non couverts)."""
        return self._intent_context

    @property
    def raw_result(self) -> Dict[str, Any]:
        """Accès direct au triage_result complet (pour cas non couverts)."""
        return self._triage_result

    def __repr__(self) -> str:
        return f"IntentParser(intent={self.detected_intent!r}, action={self.action!r})"

    def __bool__(self) -> bool:
        """True si un résultat de triage existe."""
        return bool(self._triage_result)
