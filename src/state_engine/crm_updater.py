"""
CRMUpdater - Mises à jour CRM déterministes.

Ce module remplace l'extraction [CRM_UPDATES] par l'IA avec une logique
déterministe basée sur l'état détecté et les confirmations explicites.

Principes:
1. Les mises à jour sont définies par l'état, PAS par l'IA
2. Les confirmations candidat sont extraites par matching, PAS par interprétation IA
3. Les règles de blocage (B1) sont toujours respectées
4. Chaque mise à jour est loggée et traceable
"""

import logging
import re
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, date

from .state_detector import DetectedState
from src.utils.date_utils import parse_date_flexible
from src.constants.models import MODEL_EXTRACTION

logger = logging.getLogger(__name__)


class CRMUpdateResult:
    """Résultat des mises à jour CRM."""

    def __init__(self):
        self.updates_applied: Dict[str, Any] = {}
        self.updates_blocked: Dict[str, str] = {}  # field -> reason
        self.updates_skipped: Dict[str, str] = {}  # field -> reason
        self.errors: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            'updates_applied': self.updates_applied,
            'updates_blocked': self.updates_blocked,
            'updates_skipped': self.updates_skipped,
            'errors': self.errors,
            'success': len(self.errors) == 0
        }


class CRMUpdater:
    """
    Gère les mises à jour CRM de manière déterministe.

    Cas de mise à jour:
    1. CONFIRMATION_SESSION: Extraire le choix du candidat → Session + Preference_horaire
    2. CONFIRMATION_DATE_EXAMEN: Extraire la date choisie → Date_examen_VTC
    3. Sync ExamT3P: Identifiants, Evalbox, etc. (géré ailleurs, mais validation ici)

    Règles de blocage:
    - B1: Ne pas modifier Date_examen_VTC si VALIDE CMA + clôture passée
    """

    # Patterns pour extraire les confirmations de session
    SESSION_CHOICE_PATTERNS = {
        'jour': [
            r'cours du jour',
            r'journée',
            r'matin',
            r'option\s*1',
            r'première option',
            r'cdj',
        ],
        'soir': [
            r'cours du soir',
            r'soirée',
            r'soir',
            r'option\s*2',
            r'deuxième option',
            r'seconde option',
            r'cds',
        ]
    }

    # Patterns pour extraire les confirmations de date
    DATE_CHOICE_PATTERNS = [
        r'(\d{2}/\d{2}/\d{4})',  # DD/MM/YYYY
        r'(\d{4}-\d{2}-\d{2})',  # YYYY-MM-DD
        r'(\d{1,2})\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+(\d{4})',
    ]

    # Prompt pour extraction LLM (cas ambigus)
    EXTRACTION_PROMPT = """Analyse le message du candidat et extrait les informations de confirmation.

Message du candidat:
"{message}"

Dates d'examen proposées:
{proposed_dates}

Sessions de formation proposées:
{proposed_sessions}

Extrais les informations suivantes (réponds UNIQUEMENT en JSON valide, sans markdown):
{{
  "date_examen": "YYYY-MM-DD ou null si non confirmée",
  "session_id": "ID de la session choisie ou null",
  "preference_horaire": "jour ou soir ou null si non précisé",
  "confiance": "haute/moyenne/basse",
  "raison": "explication courte"
}}

IMPORTANT:
- date_examen: La date de l'EXAMEN confirmée (PAS la clôture, PAS les dates de cours/session)
- session_id: L'ID de la session si le candidat confirme une session spécifique
- Si le candidat dit juste "ok" ou "je confirme" sans préciser de date, mets null pour date_examen
- Distingue bien: date d'examen (ex: 28/04/2026) vs date de clôture (ex: après "clôture:") vs dates de session (ex: "du 13/04 au 24/04")
"""

    def __init__(self, crm_client=None):
        """
        Initialise le CRMUpdater.

        Args:
            crm_client: Client Zoho CRM (optionnel, injecté si nécessaire)
        """
        self.crm_client = crm_client

    def determine_updates(
        self,
        state: DetectedState,
        candidate_message: str,
        proposed_sessions: Optional[List[Dict]] = None,
        proposed_dates: Optional[List[Dict]] = None
    ) -> CRMUpdateResult:
        """
        Détermine les mises à jour CRM à effectuer.

        IMPORTANT: Cette méthode est maintenant DÉTERMINISTE.
        Elle essaie TOUJOURS d'extraire date/session si les conditions sont réunies,
        indépendamment de l'intention détectée par le LLM.

        Args:
            state: État détecté
            candidate_message: Message du candidat (pour extraction confirmations)
            proposed_sessions: Sessions qui ont été proposées
            proposed_dates: Dates d'examen qui ont été proposées

        Returns:
            CRMUpdateResult avec les mises à jour à appliquer
        """
        result = CRMUpdateResult()
        context = state.context_data

        # ================================================================
        # APPROCHE DÉTERMINISTE: Extraire automatiquement si conditions OK
        # Ne PAS dépendre de l'intention LLM pour les mises à jour CRM
        # ================================================================

        # 1. Extraire la date d'examen si:
        #    - Date actuelle est vide (state = EXAM_DATE_EMPTY ou date_examen is None)
        #    - ET on a des dates proposées
        #    - ET le message semble contenir une confirmation
        date_examen_actuelle = context.get('date_examen')
        has_proposed_dates = bool(proposed_dates)

        if not date_examen_actuelle and has_proposed_dates:
            logger.info("📅 Extraction date: date vide + dates proposées → extraction automatique")
            self._extract_date_choice(
                result, candidate_message, proposed_dates, context
            )

        # 2. Extraire la préférence de session si:
        #    - Session actuelle est vide
        #    - ET on a des sessions proposées
        session_actuelle = context.get('deal_data', {}).get('Session')
        has_proposed_sessions = bool(proposed_sessions)

        if not session_actuelle and has_proposed_sessions:
            logger.info("📚 Extraction session: session vide + sessions proposées → extraction automatique")
            self._extract_session_choice(
                result, candidate_message, proposed_sessions, context
            )

        # 3. Fallback: Si config explicite définie, l'utiliser aussi
        crm_config = state.crm_updates_config
        if crm_config:
            method = crm_config.get('method', '')
            # Ne pas re-extraire si déjà fait ci-dessus
            if method == 'extract_session_choice' and 'Preference_horaire' not in result.updates_applied:
                self._extract_session_choice(
                    result, candidate_message, proposed_sessions, context
                )
            elif method == 'extract_date_choice' and 'Date_examen_VTC' not in result.updates_applied:
                self._extract_date_choice(
                    result, candidate_message, proposed_dates, context
                )

        # Vérifier les règles de blocage
        self._apply_blocking_rules(result, context)

        return result

    def _extract_session_choice(
        self,
        result: CRMUpdateResult,
        message: str,
        proposed_sessions: Optional[List[Dict]],
        context: Dict[str, Any]
    ):
        """
        Extrait le choix de session - Approche hybride.

        1. Tente extraction simple (regex) pour jour/soir
        2. Si ambigu → utilise résultat LLM (si déjà appelé par _extract_date_choice)
        """
        message_lower = message.lower()

        # Étape 1: Extraction simple de la préférence jour/soir
        preference = None
        confidence_jour = 0
        confidence_soir = 0

        for pattern in self.SESSION_CHOICE_PATTERNS['jour']:
            if re.search(pattern, message_lower):
                confidence_jour += 1

        for pattern in self.SESSION_CHOICE_PATTERNS['soir']:
            if re.search(pattern, message_lower):
                confidence_soir += 1

        if confidence_jour > 0 and confidence_soir == 0:
            preference = 'jour'
        elif confidence_soir > 0 and confidence_jour == 0:
            preference = 'soir'
        elif confidence_jour > 0 and confidence_soir > 0:
            # Ambigu par regex - essayer avec LLM
            logger.info("Préférence horaire ambiguë (jour ET soir) → vérification LLM")
            llm_result = context.get('_llm_extraction_result')
            if llm_result and llm_result.get('preference_horaire'):
                preference = llm_result['preference_horaire']
                logger.info(f"Préférence horaire résolue par LLM: {preference}")
            else:
                result.updates_skipped['Preference_horaire'] = "Choix ambigu (jour ET soir mentionnés)"
                logger.warning("Choix de session ambigu - pas de mise à jour")
                return

        if not preference:
            # Essayer avec le résultat LLM si disponible
            llm_result = context.get('_llm_extraction_result')
            if llm_result and llm_result.get('preference_horaire'):
                preference = llm_result['preference_horaire']
                logger.info(f"Préférence horaire depuis LLM: {preference}")
            else:
                result.updates_skipped['Preference_horaire'] = "Aucune préférence détectée"
                return

        # Mettre à jour Preference_horaire
        result.updates_applied['Preference_horaire'] = preference
        logger.info(f"✅ Préférence horaire: {preference}")

        # Trouver la session correspondante dans les propositions
        if proposed_sessions:
            matching_session = None
            for session in proposed_sessions:
                session_type = session.get('session_type', '')
                if session_type == preference:
                    matching_session = session
                    break

            if matching_session:
                session_id = matching_session.get('id')
                if session_id:
                    result.updates_applied['Session'] = session_id
                    logger.info(f"✅ Session sélectionnée: {matching_session.get('Name', session_id)}")
                else:
                    result.updates_skipped['Session'] = "Session trouvée mais sans ID"
            else:
                result.updates_skipped['Session'] = f"Pas de session {preference} dans les propositions"

    def _extract_date_choice(
        self,
        result: CRMUpdateResult,
        message: str,
        proposed_dates: Optional[List[Dict]],
        context: Dict[str, Any]
    ):
        """
        Extrait le choix de date d'examen - Approche hybride.

        1. Tente extraction simple (regex) - rapide, 0 coût
        2. Si ambigu → utilise LLM Haiku (~$0.001)
        """
        # Étape 1: Extraction simple
        simple_result = self._try_simple_extraction(message, proposed_dates)

        if simple_result:
            # Extraction simple réussie - trouver l'ID
            for date_info in proposed_dates or []:
                date_examen = date_info.get('Date_Examen', '')
                if date_examen and date_examen[:10] == simple_result:
                    exam_session_id = date_info.get('id')
                    if exam_session_id:
                        result.updates_applied['Date_examen_VTC'] = exam_session_id
                        logger.info(f"✅ Date d'examen (regex): {simple_result} (ID: {exam_session_id})")
                        return

            result.updates_skipped['Date_examen_VTC'] = f"Date {simple_result} sans ID session"
            return

        # Étape 2: Extraction LLM pour cas ambigu
        logger.info("Extraction simple ambiguë → utilisation LLM Haiku")

        # Récupérer les sessions proposées du contexte
        proposed_sessions = context.get('proposed_sessions', [])

        llm_result = self._extract_with_llm(message, proposed_dates, proposed_sessions)

        # Stocker le résultat LLM dans le contexte pour réutilisation par _extract_session_choice
        context['_llm_extraction_result'] = llm_result

        # Traiter le résultat LLM pour la date
        if llm_result.get('date_examen'):
            chosen_date = llm_result['date_examen']

            # Valider contre proposed_dates
            if proposed_dates:
                for date_info in proposed_dates:
                    date_examen = date_info.get('Date_Examen', '')
                    if date_examen and date_examen[:10] == chosen_date:
                        exam_session_id = date_info.get('id')
                        if exam_session_id:
                            result.updates_applied['Date_examen_VTC'] = exam_session_id
                            logger.info(f"✅ Date d'examen (LLM): {chosen_date} (ID: {exam_session_id})")
                            return

            result.updates_skipped['Date_examen_VTC'] = f"Date LLM {chosen_date} non trouvée dans propositions"
        else:
            result.updates_skipped['Date_examen_VTC'] = (
                f"Extraction échouée: {llm_result.get('raison', 'raison inconnue')}"
            )

    def _apply_blocking_rules(
        self,
        result: CRMUpdateResult,
        context: Dict[str, Any]
    ):
        """Applique les règles de blocage (B1, etc.)."""
        # Règle B1: Ne pas modifier Date_examen_VTC si VALIDE CMA + clôture passée
        if 'Date_examen_VTC' in result.updates_applied:
            if not context.get('can_modify_exam_date', True):
                blocked_value = result.updates_applied.pop('Date_examen_VTC')
                result.updates_blocked['Date_examen_VTC'] = (
                    f"Dossier validé (Evalbox={context.get('evalbox')}) "
                    f"et clôture passée - modification impossible sans force majeure"
                )
                logger.warning(f"Mise à jour Date_examen_VTC bloquée par règle B1")

    def _normalize_date(self, date_str: str) -> Optional[str]:
        """Normalise une date en YYYY-MM-DD."""
        parsed = parse_date_flexible(date_str, "normalize_date")
        if parsed is not None:
            return parsed.strftime('%Y-%m-%d')
        return None

    def _try_simple_extraction(
        self,
        message: str,
        proposed_dates: Optional[List[Dict]]
    ) -> Optional[str]:
        """
        Tente une extraction simple par regex.

        Returns:
            Date YYYY-MM-DD si extraction non-ambiguë, None sinon
        """
        dates_found = []

        for pattern in self.DATE_CHOICE_PATTERNS:
            matches = re.findall(pattern, message, re.IGNORECASE)
            for match in matches:
                if isinstance(match, tuple):
                    # Format texte (jour, mois, année)
                    try:
                        day, month_name, year = match
                        month_map = {
                            'janvier': 1, 'février': 2, 'mars': 3, 'avril': 4,
                            'mai': 5, 'juin': 6, 'juillet': 7, 'août': 8,
                            'septembre': 9, 'octobre': 10, 'novembre': 11, 'décembre': 12
                        }
                        month = month_map.get(month_name.lower(), 0)
                        if month:
                            normalized = f"{int(year):04d}-{month:02d}-{int(day):02d}"
                            if normalized not in dates_found:
                                dates_found.append(normalized)
                    except Exception:
                        pass
                else:
                    normalized = self._normalize_date(match)
                    if normalized and normalized not in dates_found:
                        dates_found.append(normalized)

        # Cas simple: exactement 1 date trouvée
        if len(dates_found) == 1:
            chosen = dates_found[0]
            # Vérifier que c'est bien une date d'examen proposée
            if proposed_dates:
                exam_dates = {d.get('Date_Examen', '')[:10] for d in proposed_dates if d.get('Date_Examen')}
                if chosen in exam_dates:
                    logger.info(f"Extraction simple réussie: 1 date trouvée = {chosen}")
                    return chosen
                else:
                    logger.info(f"Date {chosen} trouvée mais non proposée")
                    return None
            else:
                return chosen

        # Cas ambigu: 0 ou >1 dates
        if len(dates_found) == 0:
            logger.info("Extraction simple: aucune date trouvée")
        else:
            logger.info(f"Extraction simple ambiguë: {len(dates_found)} dates trouvées: {dates_found}")

        return None

    def _extract_with_llm(
        self,
        message: str,
        proposed_dates: Optional[List[Dict]],
        proposed_sessions: Optional[List[Dict]]
    ) -> Dict[str, Any]:
        """
        Extraction structurée via LLM Haiku pour cas ambigus.

        Returns:
            {
                'date_examen': '2026-04-28' ou None,
                'session_id': 'xxx' ou None,
                'preference_horaire': 'jour'/'soir'/None,
                'confiance': 'haute'/'moyenne'/'basse',
                'raison': str
            }
        """
        # Formater les dates proposées pour le prompt
        if not proposed_dates:
            dates_str = "Aucune"
        else:
            dates_str = "\n".join([
                f"- {d.get('Date_Examen', 'N/A')} (clôture: {str(d.get('Date_Cloture_Inscription', 'N/A'))[:10]}, ID: {d.get('id', 'N/A')})"
                for d in proposed_dates
            ])

        # Formater les sessions proposées
        if not proposed_sessions:
            sessions_str = "Aucune"
        else:
            sessions_str = "\n".join([
                f"- {s.get('Name', 'N/A')} (ID: {s.get('id', 'N/A')}, {s.get('Date_debut', 'N/A')} - {s.get('Date_fin', 'N/A')})"
                for s in proposed_sessions
            ])

        prompt = self.EXTRACTION_PROMPT.format(
            message=message,
            proposed_dates=dates_str,
            proposed_sessions=sessions_str
        )

        try:
            import anthropic
            import json

            client = anthropic.Anthropic()

            response = client.messages.create(
                model=MODEL_EXTRACTION,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}]
            )

            # Parser la réponse JSON
            content = response.content[0].text.strip()

            # Nettoyer si wrapped dans ```json
            if content.startswith("```"):
                lines = content.split("\n")
                # Enlever première et dernière ligne (``` markers)
                content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                if content.startswith("json"):
                    content = content[4:].strip()

            result = json.loads(content)
            logger.info(f"LLM extraction réussie: {result}")
            return result

        except Exception as e:
            logger.error(f"LLM extraction failed: {e}")
            return {
                'date_examen': None,
                'session_id': None,
                'preference_horaire': None,
                'confiance': 'basse',
                'raison': f'Erreur extraction: {str(e)}'
            }

    def apply_updates(
        self,
        deal_id: str,
        updates: Dict[str, Any],
        crm_client=None
    ) -> Dict[str, Any]:
        """
        Applique les mises à jour au CRM.

        Args:
            deal_id: ID du deal à mettre à jour
            updates: Dictionnaire des champs à mettre à jour
            crm_client: Client CRM (optionnel, utilise self.crm_client si non fourni)

        Returns:
            Résultat de la mise à jour
        """
        client = crm_client or self.crm_client

        if not client:
            return {'success': False, 'error': 'Pas de client CRM disponible'}

        if not updates:
            return {'success': True, 'message': 'Aucune mise à jour à appliquer'}

        try:
            logger.info(f"Application des mises à jour CRM pour deal {deal_id}: {updates}")
            client.update_deal(deal_id, updates)
            return {
                'success': True,
                'updates_applied': updates
            }
        except Exception as e:
            logger.error(f"Erreur mise à jour CRM: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def format_updates_for_note(self, result: CRMUpdateResult) -> str:
        """Formate les mises à jour pour inclusion dans une note CRM."""
        lines = []

        if result.updates_applied:
            lines.append("**Mises à jour appliquées:**")
            for field, value in result.updates_applied.items():
                lines.append(f"• {field}: {value}")

        if result.updates_blocked:
            lines.append("\n**Mises à jour bloquées:**")
            for field, reason in result.updates_blocked.items():
                lines.append(f"• {field}: {reason}")

        if result.updates_skipped:
            lines.append("\n**Mises à jour ignorées:**")
            for field, reason in result.updates_skipped.items():
                lines.append(f"• {field}: {reason}")

        if result.errors:
            lines.append("\n**Erreurs:**")
            for error in result.errors:
                lines.append(f"• {error}")

        return "\n".join(lines) if lines else "Aucune mise à jour CRM"
