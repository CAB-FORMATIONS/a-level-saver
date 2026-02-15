"""
StateDetector - Détection déterministe de l'état du candidat.

Ce module remplace la logique dispersée dans les helpers par une détection
centralisée et déterministe basée sur candidate_states.yaml.

L'état est déterminé par:
1. Les données CRM (deal_data)
2. Les données ExamT3P (examt3p_data)
3. Le résultat du triage (triage_result)
4. Le résultat du deal linking (linking_result)

L'IA n'intervient PAS dans la détection d'état.
"""

import logging
import yaml
from pathlib import Path
from datetime import datetime, date
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field

from src.utils.training_exam_consistency_helper import detect_session_assignment_error
from src.utils.date_utils import parse_date_flexible
from src.constants.thresholds import (
    EXAM_IMMINENT_DAYS, CONVOCATION_DAYS_BEFORE_EXAM, UBER_VERIFICATION_DELAY_DAYS,
)

logger = logging.getLogger(__name__)

# Chemin vers le fichier de configuration des états
STATES_CONFIG_PATH = Path(__file__).parent.parent.parent / "states" / "candidate_states.yaml"


@dataclass
class DetectedState:
    """Représente un état détecté pour un candidat."""
    id: str
    name: str
    priority: int
    category: str
    description: str
    workflow_action: str
    response_config: Dict[str, Any]
    crm_updates_config: Optional[Dict[str, Any]]
    detection_reason: str
    # Severity: BLOCKING, WARNING, INFO
    severity: str = "INFO"
    # Données contextuelles pour le template
    context_data: Dict[str, Any] = field(default_factory=dict)
    # Alertes à inclure (Uber D/E, etc.)
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    # Intention détectée (si applicable)
    detected_intent: Optional[str] = None
    intent_context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DetectedStates:
    """Représente TOUS les états détectés pour un candidat (multi-états)."""
    # État BLOCKING le plus prioritaire (stoppe le workflow)
    blocking_state: Optional[DetectedState] = None
    # États WARNING (alertes à inclure mais workflow continue)
    warning_states: List[DetectedState] = field(default_factory=list)
    # États INFO (combinables, informatifs)
    info_states: List[DetectedState] = field(default_factory=list)
    # État principal pour rétrocompatibilité (blocking > premier info)
    primary_state: Optional[DetectedState] = None
    # Tous les états détectés (pour debug)
    all_states: List[DetectedState] = field(default_factory=list)


class StateDetector:
    """
    Détecte l'état du candidat de manière déterministe.

    Ordre d'évaluation (par priorité):
    1. États de triage (T1-T4): SPAM, ROUTE, DUPLICATE_UBER, CANDIDATE_NOT_FOUND
    2. États d'analyse (A1-A3): CREDENTIALS_INVALID, EXAMT3P_DOWN, DOUBLE_ACCOUNT
    3. États Uber (U-*): PROSPECT, CAS A/B/D/E
    4. États date examen (D-1 à D-10)
    5. États intention (I1-I9): selon l'intention détectée par le triage
    6. États cohérence (C1-C3): TRAINING_MISSED, REFRESH_SESSION, DOSSIER_NOT_RECEIVED
    7. États blocage (B1): DATE_MODIFICATION_BLOCKED
    8. État par défaut (GENERAL)
    """

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialise le StateDetector.

        Args:
            config_path: Chemin vers candidate_states.yaml (optionnel)
        """
        self.config_path = config_path or STATES_CONFIG_PATH
        self.states_config = self._load_config()
        self.config = self.states_config.get('config', {})
        self.states = self.states_config.get('states', {})

        # Trier les états par priorité
        self._sorted_states = sorted(
            self.states.items(),
            key=lambda x: x[1].get('priority', 999)
        )

        logger.info(f"StateDetector initialisé avec {len(self.states)} états")

    def _load_config(self) -> Dict[str, Any]:
        """Charge la configuration YAML."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Erreur chargement config états: {e}")
            return {'states': {}, 'config': {}}

    def detect_state(
        self,
        deal_data: Dict[str, Any],
        examt3p_data: Dict[str, Any],
        triage_result: Dict[str, Any],
        linking_result: Dict[str, Any],
        threads_data: Optional[List[Dict]] = None,
        session_data: Optional[Dict[str, Any]] = None,
        training_exam_consistency_data: Optional[Dict[str, Any]] = None
    ) -> DetectedState:
        """
        Détecte l'état principal du candidat (rétrocompatibilité).

        Args:
            deal_data: Données du deal CRM
            examt3p_data: Données ExamT3P
            triage_result: Résultat du triage (action, intent, etc.)
            linking_result: Résultat du deal linking
            threads_data: Threads du ticket (optionnel)
            session_data: Données session (optionnel, pour C2)
            training_exam_consistency_data: Données cohérence formation/examen (optionnel, pour C1/C3)

        Returns:
            DetectedState principal (blocking > premier info)
        """
        # Utiliser detect_all_states et retourner le primary_state
        detected_states = self.detect_all_states(
            deal_data, examt3p_data, triage_result, linking_result, threads_data,
            session_data, training_exam_consistency_data
        )
        return detected_states.primary_state

    def detect_all_states(
        self,
        deal_data: Dict[str, Any],
        examt3p_data: Dict[str, Any],
        triage_result: Dict[str, Any],
        linking_result: Dict[str, Any],
        threads_data: Optional[List[Dict]] = None,
        session_data: Optional[Dict[str, Any]] = None,
        training_exam_consistency_data: Optional[Dict[str, Any]] = None,
        enriched_lookups: Optional[Dict[str, Any]] = None
    ) -> DetectedStates:
        """
        Détecte TOUS les états du candidat (multi-états).

        Contrairement à detect_state() qui retourne le premier match,
        cette méthode collecte TOUS les états applicables classifiés par severity.

        Args:
            deal_data: Données du deal CRM
            examt3p_data: Données ExamT3P
            triage_result: Résultat du triage (action, intent, etc.)
            linking_result: Résultat du deal linking
            threads_data: Threads du ticket (optionnel)
            session_data: Données session (optionnel, pour C2)
            training_exam_consistency_data: Données cohérence formation/examen (optionnel, pour C1/C3)
            enriched_lookups: Lookups CRM enrichis (v2.2) depuis crm_lookup_helper

        Returns:
            DetectedStates avec blocking_state, warning_states, info_states
        """
        logger.info("🔍 Détection multi-états en cours...")

        # Contexte pour l'évaluation des conditions
        context = self._build_context(
            deal_data, examt3p_data, triage_result, linking_result, threads_data,
            session_data, training_exam_consistency_data, enriched_lookups
        )

        # Collecter les alertes (Uber D/E, etc.)
        alerts = self._collect_alerts(context)

        # Collecter tous les états qui matchent
        blocking_state = None
        warning_states = []
        info_states = []
        all_states = []

        for state_name, state_config in self._sorted_states:
            if self._matches_state(state_name, state_config, context):
                state = self._create_detected_state(
                    state_name, state_config, context, alerts
                )
                severity = state_config.get('severity', 'INFO')
                all_states.append(state)

                if severity == 'BLOCKING':
                    if not blocking_state:  # Premier BLOCKING uniquement
                        blocking_state = state
                        logger.info(f"🚫 État BLOCKING détecté: {state_name} (priorité {state_config.get('priority')})")
                        break  # Les BLOCKING stoppent la collecte
                elif severity == 'WARNING':
                    warning_states.append(state)
                    logger.info(f"⚠️ État WARNING détecté: {state_name}")
                else:  # INFO
                    info_states.append(state)
                    logger.info(f"ℹ️ État INFO détecté: {state_name}")

        # Si aucun état, utiliser GENERAL
        if not blocking_state and not info_states:
            general_state = self._create_detected_state(
                'GENERAL', self.states.get('GENERAL', {}), context, alerts
            )
            info_states.append(general_state)
            all_states.append(general_state)
            logger.info("ℹ️ Aucun état spécifique - utilisation de GENERAL")

        # Primary state: blocking > premier info
        primary_state = blocking_state or (info_states[0] if info_states else None)

        logger.info(f"📊 Résumé: blocking={blocking_state is not None}, warnings={len(warning_states)}, info={len(info_states)}")

        return DetectedStates(
            blocking_state=blocking_state,
            warning_states=warning_states,
            info_states=info_states,
            primary_state=primary_state,
            all_states=all_states
        )

    def _build_context(
        self,
        deal_data: Dict[str, Any],
        examt3p_data: Dict[str, Any],
        triage_result: Dict[str, Any],
        linking_result: Dict[str, Any],
        threads_data: Optional[List[Dict]],
        session_data: Optional[Dict[str, Any]] = None,
        training_exam_consistency_data: Optional[Dict[str, Any]] = None,
        enriched_lookups: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Construit le contexte pour l'évaluation des conditions."""
        today = date.today()

        # Utiliser les lookups enrichis si disponibles, sinon fallback sur les méthodes legacy
        if enriched_lookups:
            date_examen = enriched_lookups.get('date_examen')
            date_cloture = enriched_lookups.get('date_cloture')
            departement = enriched_lookups.get('departement')
        else:
            # Fallback: méthodes legacy avec regex sur le champ "name"
            date_examen = self._extract_date_examen(deal_data)
            date_cloture = self._extract_date_cloture(deal_data)
            departement = self._extract_departement(deal_data)

        # Extraire les champs importants du deal
        evalbox = deal_data.get('Evalbox', '')
        amount = deal_data.get('Amount', 0)
        stage = deal_data.get('Stage', '')

        # Calculer les jours jusqu'à l'examen
        days_until_exam = None
        if date_examen:
            exam_date_obj = parse_date_flexible(date_examen, "date_examen")
            if exam_date_obj:
                days_until_exam = (exam_date_obj - today).days

        # Vérifier si la clôture est passée
        cloture_passed = False
        if date_cloture:
            cloture_date_obj = parse_date_flexible(date_cloture, "date_cloture")
            if cloture_date_obj:
                cloture_passed = cloture_date_obj < today

        # Flags temporels pour templates (remplacent les comparateurs Handlebars non supportés)
        exam_within_7_days = days_until_exam is not None and 0 <= days_until_exam <= EXAM_IMMINENT_DAYS
        exam_within_10_days = days_until_exam is not None and 0 <= days_until_exam <= CONVOCATION_DAYS_BEFORE_EXAM
        examen_pas_encore_passe = days_until_exam is not None and days_until_exam > 0
        examen_imminent = days_until_exam is not None and 0 <= days_until_exam <= 3

        # Flag pour convocation anormale (VALIDE CMA + J-7 + pas de convoc)
        convocation_anormale = (
            evalbox == 'VALIDE CMA' and
            exam_within_7_days
        )

        context = {
            # Données brutes
            'deal_data': deal_data,
            'examt3p_data': examt3p_data,
            'triage_result': triage_result,
            'linking_result': linking_result,
            'threads_data': threads_data or [],

            # Champs CRM extraits
            'evalbox': evalbox,
            'date_examen': date_examen,
            'date_cloture': date_cloture,
            'departement': departement,
            'amount': amount,
            'stage': stage,
            'deal_id': linking_result.get('deal_id'),
            'num_dossier': examt3p_data.get('num_dossier', ''),

            # Calculs
            'today': today.isoformat(),
            'days_until_exam': days_until_exam,
            'cloture_passed': cloture_passed,
            'date_examen_passed': days_until_exam is not None and days_until_exam < 0,
            'date_examen_future': days_until_exam is not None and days_until_exam >= 0,

            # Flags temporels pour templates (comparateurs non supportés en Handlebars)
            'exam_within_7_days': exam_within_7_days,
            'exam_within_10_days': exam_within_10_days,
            'examen_pas_encore_passe': examen_pas_encore_passe,
            'examen_imminent': examen_imminent,
            'convocation_anormale': convocation_anormale,

            # Triage - intentions (primary_intent est le standard, detected_intent pour rétrocompat)
            'triage_action': triage_result.get('action', 'GO'),
            'primary_intent': triage_result.get('primary_intent') or triage_result.get('detected_intent'),
            'detected_intent': triage_result.get('detected_intent'),  # Rétrocompat
            'secondary_intents': triage_result.get('secondary_intents', []),
            'intent_context': triage_result.get('intent_context', {}),

            # Force majeure (fusionnée: triage + helper)
            # Source 1: TriageAgent (intent_context)
            # Source 2: training_exam_consistency_helper
            'mentions_force_majeure': (
                triage_result.get('intent_context', {}).get('mentions_force_majeure', False) or
                (training_exam_consistency_data.get('force_majeure_detected', False) if training_exam_consistency_data else False)
            ),
            'force_majeure_type': (
                triage_result.get('intent_context', {}).get('force_majeure_type') or
                (training_exam_consistency_data.get('force_majeure_type') if training_exam_consistency_data else None)
            ),
            'force_majeure_details': triage_result.get('intent_context', {}).get('force_majeure_details', ''),
            # Flags de type (fusionnés)
            'is_force_majeure_deces': (
                triage_result.get('intent_context', {}).get('force_majeure_type') == 'death' or
                (training_exam_consistency_data.get('force_majeure_type') == 'death' if training_exam_consistency_data else False)
            ),
            'is_force_majeure_medical': (
                triage_result.get('intent_context', {}).get('force_majeure_type') == 'medical' or
                (training_exam_consistency_data.get('force_majeure_type') == 'medical' if training_exam_consistency_data else False)
            ),
            'is_force_majeure_accident': (
                triage_result.get('intent_context', {}).get('force_majeure_type') == 'accident' or
                (training_exam_consistency_data.get('force_majeure_type') == 'accident' if training_exam_consistency_data else False)
            ),
            'is_force_majeure_childcare': (
                triage_result.get('intent_context', {}).get('force_majeure_type') == 'childcare' or
                (training_exam_consistency_data.get('force_majeure_type') == 'childcare' if training_exam_consistency_data else False)
            ),
            'is_force_majeure_other': (
                triage_result.get('intent_context', {}).get('force_majeure_type') == 'other' or
                (training_exam_consistency_data.get('force_majeure_type') == 'other' if training_exam_consistency_data else False)
            ),

            # Deal linking
            'has_duplicate_uber_offer': linking_result.get('has_duplicate_uber_offer', False),
            'needs_clarification': linking_result.get('needs_clarification', False),

            # ExamT3P
            'compte_existe': examt3p_data.get('compte_existe', False),
            'connection_test_success': examt3p_data.get('connection_test_success', False),
            'should_respond_to_candidate': examt3p_data.get('should_respond_to_candidate', False),
            'duplicate_payment_alert': examt3p_data.get('duplicate_payment_alert', False),
            'personal_account_warning': examt3p_data.get('personal_account_warning', False),
            'personal_account_email': examt3p_data.get('personal_account_email', ''),
            'cab_account_email': examt3p_data.get('cab_account_email', ''),
            'cab_payment_date': examt3p_data.get('cab_payment_date', ''),
            'statut_dossier_examt3p': examt3p_data.get('statut_dossier', ''),
            # Flags pour EXAMT3P_DOWN (A2)
            'extraction_failed': examt3p_data.get('extraction_failed', False),
            'error_type': examt3p_data.get('error_type'),  # 'technical' ou 'credentials'

            # Pièces refusées (pour templates Refus CMA)
            'pieces_refusees_details': examt3p_data.get('pieces_refusees_details', []),
            'has_pieces_refusees': bool(examt3p_data.get('pieces_refusees_details')),

            # Uber spécifique
            'is_uber_20_deal': amount == 20 and 'GAGN' in str(stage).upper(),
            'is_uber_prospect': amount == 20 and 'ATTENTE' in str(stage).upper(),
            'is_prospect': amount == 20 and 'ATTENTE' in str(stage).upper(),  # Alias pour templates
            'date_dossier_recu': deal_data.get('Date_Dossier_re_u'),
            'date_test_selection': deal_data.get('Date_test_selection'),
            'compte_uber': deal_data.get('Compte_Uber', False),
            'eligible_uber': deal_data.get('ELIGIBLE', False),

            # Session
            'session_assigned': deal_data.get('Session') is not None,
            'preference_horaire': deal_data.get('Preference_horaire'),

            # Données session et cohérence (pour détection C1, C2, C3)
            'session_data': session_data or {},
            'training_exam_consistency_data': training_exam_consistency_data or {},

            # Lookups CRM enrichis (v2.2)
            'enriched_lookups': enriched_lookups or {},
        }

        # Calculer uber_case une seule fois (source de vérité unique)
        context['uber_case'] = self._determine_uber_case(context)

        # Calculer can_modify_exam_date (règle B1)
        context['can_modify_exam_date'] = self._can_modify_exam_date(context)

        # Détecter erreur de saisie session (A5)
        session_error_data = detect_session_assignment_error(deal_data, enriched_lookups or {})
        context['session_assignment_error'] = session_error_data.get('is_assignment_error', False)
        context['session_error_data'] = session_error_data
        if context['session_assignment_error']:
            # Préparer les données pour le template
            context['session_error_dates'] = session_error_data.get('session_name', '')
            context['session_error_correct_year'] = session_error_data.get('correct_year')

        # Cohérence formation/examen (pour FM-1: MISSED_TRAINING_FORCE_MAJEURE)
        context['has_consistency_issue'] = training_exam_consistency_data.get('has_consistency_issue', False) if training_exam_consistency_data else False
        context['consistency_issue_type'] = training_exam_consistency_data.get('issue_type') if training_exam_consistency_data else None

        return context

    def _extract_date_examen(self, deal_data: Dict[str, Any]) -> Optional[str]:
        """Extrait la date d'examen au format YYYY-MM-DD."""
        import re

        date_examen_vtc = deal_data.get('Date_examen_VTC')
        if not date_examen_vtc:
            return None

        # Si c'est un lookup, extraire la date du nom
        # Format: "93_2026-02-24" ou "75_2026-03-15"
        if isinstance(date_examen_vtc, dict):
            name = date_examen_vtc.get('name', '')
            # Chercher une date au format YYYY-MM-DD dans le nom
            match = re.search(r'(\d{4}-\d{2}-\d{2})', name)
            if match:
                return match.group(1)
            return None

        return str(date_examen_vtc)[:10] if date_examen_vtc else None

    def _extract_date_cloture(self, deal_data: Dict[str, Any]) -> Optional[str]:
        """Extrait la date de clôture au format YYYY-MM-DD."""
        import re

        # 1. Chercher Date_Cloture_Inscription directement dans deal_data
        date_cloture = deal_data.get('Date_Cloture_Inscription')
        if date_cloture:
            if isinstance(date_cloture, str):
                # Si c'est déjà une date string (YYYY-MM-DD)
                return date_cloture[:10]
            return str(date_cloture)[:10]

        # 2. Chercher dans le lookup Date_examen_VTC (données enrichies)
        date_examen_vtc = deal_data.get('Date_examen_VTC')
        if isinstance(date_examen_vtc, dict):
            # Le lookup peut contenir Date_Cloture_Inscription
            cloture = date_examen_vtc.get('Date_Cloture_Inscription')
            if cloture:
                return str(cloture)[:10]

        return None

    def _extract_departement(self, deal_data: Dict[str, Any]) -> Optional[str]:
        """Extrait le département depuis le deal ou le lookup Date_examen_VTC."""
        import re

        # D'abord essayer CMA_de_depot
        cma_depot = deal_data.get('CMA_de_depot')
        if cma_depot:
            # Peut être un string "93" ou un lookup
            if isinstance(cma_depot, dict):
                return cma_depot.get('name', '')
            return str(cma_depot)

        # Sinon extraire du nom du lookup Date_examen_VTC
        # Format: "93_2026-02-24"
        date_examen_vtc = deal_data.get('Date_examen_VTC')
        if isinstance(date_examen_vtc, dict):
            name = date_examen_vtc.get('name', '')
            match = re.match(r'^(\d+)_', name)
            if match:
                return match.group(1)

        return None

    def _can_modify_exam_date(self, context: Dict[str, Any]) -> bool:
        """
        Vérifie si la date d'examen peut être modifiée.

        Règle B1: NE PAS modifier si:
        - Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"}
        - ET Date_Cloture_Inscription < aujourd'hui
        """
        evalbox = context.get('evalbox', '')
        blocking_statuses = {'VALIDE CMA', 'Convoc CMA reçue'}

        if evalbox not in blocking_statuses:
            return True

        if context.get('cloture_passed'):
            return False

        return True

    def _collect_alerts(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Collecte les alertes à inclure dans la réponse.

        NOTE: Les cas Uber D/E sont désormais gérés via les templates (Section 0 de response_master.html)
        et non plus via l'injection d'alertes pour éviter la duplication.
        """
        alerts = []

        # NOTE: uber_case_d et uber_case_e SUPPRIMÉS - gérés par templates Section 0
        # {{#if uber_cas_d}}{{> partials/uber/cas_d_compte_non_verifie}}{{/if}}
        # {{#if uber_cas_e}}{{> partials/uber/cas_e_non_eligible}}{{/if}}

        # Alerte compte personnel potentiel
        if context['examt3p_data'].get('potential_personal_account'):
            alerts.append({
                'type': 'personal_account',
                'title': 'Compte personnel potentiel détecté',
                'priority': 'info'
            })

        # Alerte A4: Compte personnel détecté (CRM payé, perso non payé)
        # Avertissement client pour utiliser le bon compte
        # Vérifier que c'est True (booléen) et pas une string descriptive (cas potential_personal_account)
        if context.get('personal_account_warning') is True:
            alerts.append({
                'type': 'personal_account_warning',
                'id': 'A4',
                'title': 'Compte personnel détecté - utiliser compte CAB',
                'template': 'partials/warnings/personal_account_warning.html',
                'position': 'before_signature',
                'priority': 'warning',
                'personal_account_email': context.get('personal_account_email', ''),
                'cab_account_email': context.get('cab_account_email', ''),
                'cab_payment_date': context.get('cab_payment_date', '')
            })

        # Alerte A5: Erreur de saisie session (session passée impossible)
        if context.get('session_assignment_error') is True:
            session_error_data = context.get('session_error_data', {})
            alerts.append({
                'type': 'session_assignment_error',
                'id': 'A5',
                'title': 'Erreur de saisie session détectée',
                'template': 'partials/warnings/session_assignment_error.html',
                'position': 'main_content',
                'priority': 'warning',
                'session_error_dates': session_error_data.get('session_name', ''),
                'session_end_date': session_error_data.get('session_end_date_formatted', ''),
                'deal_created_date': session_error_data.get('deal_created_date_formatted', ''),
                'correct_year': session_error_data.get('correct_year'),
                'error_type': session_error_data.get('error_type', 'unknown')
            })

        return alerts

    def _is_uber_case_d(self, context: Dict[str, Any]) -> bool:
        """Vérifie si c'est le CAS D Uber (compte non vérifié)."""
        if not context.get('is_uber_20_deal'):
            return False

        date_dossier = context.get('date_dossier_recu')
        if not date_dossier:
            return False

        # Vérification faite si J+4 passé (délai pour laisser le temps à la vérification manuelle)
        from datetime import timedelta
        dossier_date = parse_date_flexible(str(date_dossier)[:10], "date_dossier_recu")
        if not dossier_date:
            return False
        verification_date = dossier_date + timedelta(days=UBER_VERIFICATION_DELAY_DAYS)
        today = date.today()

        if today < verification_date:
            return False  # Vérification pas encore faite

        return not context.get('compte_uber', False)

    def _is_uber_case_e(self, context: Dict[str, Any]) -> bool:
        """Vérifie si c'est le CAS E Uber (non éligible)."""
        if not context.get('is_uber_20_deal'):
            return False

        if self._is_uber_case_d(context):
            return False  # D est prioritaire sur E

        date_dossier = context.get('date_dossier_recu')
        if not date_dossier:
            return False

        # Vérification faite si J+4 passé (délai pour laisser le temps à la vérification manuelle)
        from datetime import timedelta
        dossier_date = parse_date_flexible(str(date_dossier)[:10], "date_dossier_recu")
        if not dossier_date:
            return False
        verification_date = dossier_date + timedelta(days=UBER_VERIFICATION_DELAY_DAYS)
        today = date.today()

        if today < verification_date:
            return False

        return not context.get('eligible_uber', False)

    def _matches_state(
        self,
        state_name: str,
        state_config: Dict[str, Any],
        context: Dict[str, Any]
    ) -> bool:
        """
        Vérifie si le contexte correspond à un état donné.

        Chaque état a une méthode de détection spécifique.
        """
        detection = state_config.get('detection', {})
        method = detection.get('method', '')

        # Dispatch selon la méthode de détection
        if method == 'intent':
            return self._match_intent_state(state_name, detection, context)
        elif method == 'triage_agent':
            return self._match_triage_state(state_name, detection, context)
        elif method == 'deal_linking_agent':
            return self._match_linking_state(state_name, detection, context)
        elif method == 'credentials_helper':
            return self._match_credentials_state(state_name, detection, context)
        elif method == 'examt3p_agent':
            return self._match_examt3p_state(state_name, detection, context)
        elif method == 'uber_eligibility_helper':
            return self._match_uber_state(state_name, detection, context)
        elif method == 'date_examen_helper':
            return self._match_date_examen_state(state_name, detection, context)
        elif method == 'training_exam_consistency_helper':
            return self._match_consistency_state(state_name, detection, context)
        elif method == 'session_helper':
            return self._match_session_state(state_name, detection, context)
        elif method == 'crm_update_agent':
            return self._match_blocking_state(state_name, detection, context)
        elif method == 'workflow':
            return self._match_workflow_state(state_name, detection, context)
        elif method == 'combined':
            # États combinés (plusieurs conditions simultanées)
            return self._match_force_majeure_consistency_state(state_name, detection, context)
        elif method == 'fallback':
            return True  # État par défaut

        return False

    def _match_intent_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """
        Match les états basés sur l'intention détectée par le Triage Agent.

        Vérifie à la fois primary_intent (ou detected_intent pour rétrocompat)
        ET secondary_intents pour supporter le multi-intentions.
        """
        condition = detection.get('condition', '')

        if 'detected_intent' in condition:
            # Format: "detected_intent == 'REFUS_PARTAGE_CREDENTIALS'"
            expected_intent = condition.split('==')[1].strip().strip("'\"")

            # Vérifier primary_intent (avec fallback sur detected_intent pour rétrocompat)
            primary_intent = context.get('primary_intent') or context.get('detected_intent')
            if primary_intent == expected_intent:
                return True

            # Vérifier aussi les secondary_intents (multi-intentions v2.1)
            secondary_intents = context.get('secondary_intents', [])
            if expected_intent in secondary_intents:
                logger.debug(f"Intent {expected_intent} matched via secondary_intents for state {state_name}")
                return True

        return False

    def _match_triage_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états basés sur le triage (action ou intention)."""
        triage_action = detection.get('triage_action')
        condition = detection.get('condition', '')

        if triage_action:
            return context.get('triage_action') == triage_action

        if 'detected_intent' in condition:
            expected_intent = condition.split('==')[1].strip().strip("'\"")
            # Standardiser sur primary_intent avec fallback sur detected_intent
            primary_intent = context.get('primary_intent') or context.get('detected_intent')
            if primary_intent == expected_intent:
                return True
            # Vérifier aussi secondary_intents
            secondary_intents = context.get('secondary_intents', [])
            if expected_intent in secondary_intents:
                return True

        return False

    def _match_linking_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états basés sur le deal linking."""
        condition = detection.get('condition', '')

        if 'has_duplicate_uber_offer' in condition:
            return context.get('has_duplicate_uber_offer', False)

        if 'needs_clarification' in condition or 'deal_id == null' in condition:
            return context.get('needs_clarification', False)

        return False

    def _match_credentials_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états basés sur les credentials."""
        conditions = detection.get('conditions', {})
        condition = detection.get('condition', '')

        # État A1: Identifiants invalides
        # EXCEPTION: Pour les candidats Uber ÉLIGIBLES (Compte_Uber=true, ELIGIBLE=true)
        # CAB gère le compte pour eux, donc on ne bloque PAS sur les identifiants
        if state_name == 'CREDENTIALS_INVALID':
            # Vérifier si c'est un Uber éligible
            is_uber_eligible = (
                context.get('is_uber_20_deal') and
                context.get('compte_uber') and
                context.get('eligible_uber')
            )
            has_exam_date = bool(context.get('date_examen'))

            # Si Uber éligible ou date assignée → pas de blocage sur credentials
            if is_uber_eligible or has_exam_date:
                return False

            if not context.get('compte_existe') and context.get('should_respond_to_candidate'):
                return True
            if not context.get('connection_test_success') and context.get('should_respond_to_candidate'):
                return True

        # État A3: Double compte payé
        if 'duplicate_payment_alert' in condition:
            return context.get('duplicate_payment_alert', False)

        # État A4: Compte personnel détecté (CRM payé, perso non payé)
        if 'personal_account_warning' in condition:
            return context.get('personal_account_warning') is True

        # État A5: Erreur de saisie session (session passée impossible)
        if 'session_assignment_error' in condition:
            return context.get('session_assignment_error') is True

        return False

    def _match_examt3p_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états basés sur ExamT3P."""
        # État A2: ExamT3P down
        if state_name == 'EXAMT3P_DOWN':
            examt3p = context.get('examt3p_data', {})
            return examt3p.get('extraction_failed') and examt3p.get('error_type') == 'technical'

        return False

    def _match_uber_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états Uber."""
        condition = detection.get('condition', '')

        # Extraire le cas attendu
        if 'case ==' in condition:
            expected_case = condition.split('==')[1].strip().strip("'\"")
            actual_case = self._determine_uber_case(context)
            return actual_case == expected_case

        return False

    def _determine_uber_case(self, context: Dict[str, Any]) -> str:
        """Détermine le cas Uber (PROSPECT, A, B, D, E, ELIGIBLE, NOT_UBER)."""
        # PROSPECT
        if context.get('is_uber_prospect'):
            return 'PROSPECT'

        # NOT_UBER
        if not context.get('is_uber_20_deal'):
            return 'NOT_UBER'

        # CAS A: Documents non envoyés
        if not context.get('date_dossier_recu'):
            return 'A'

        # CAS D et E (après vérification J+1)
        if self._is_uber_case_d(context):
            return 'D'
        if self._is_uber_case_e(context):
            return 'E'

        # CAS B: Test sélection non passé (si obligatoire)
        if not context.get('date_test_selection'):
            # Vérifier si test obligatoire (dossier après 19/05/2025)
            date_dossier = context.get('date_dossier_recu')
            if date_dossier:
                dossier_date = parse_date_flexible(str(date_dossier)[:10], "date_dossier_recu")
                if dossier_date:
                    test_mandatory_from = date(2025, 5, 19)
                    if dossier_date > test_mandatory_from:
                        return 'B'

        return 'ELIGIBLE'

    def _match_date_examen_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états date examen (D-1 à D-10)."""
        condition = detection.get('condition', '')

        if 'case ==' not in condition:
            return False

        expected_case = int(condition.split('==')[1].strip())
        actual_case = self._determine_date_examen_case(context)

        return actual_case == expected_case

    def _determine_date_examen_case(self, context: Dict[str, Any]) -> int:
        """
        Détermine le cas date examen (1-10).

        CAS 1: Date vide
        CAS 2: Date passée + non validé
        CAS 3: Refusé CMA
        CAS 4: VALIDE CMA + date future
        CAS 5: Dossier Synchronisé
        CAS 6: Date future + autre statut
        CAS 7: Date passée + validé
        CAS 8: Deadline ratée
        CAS 9: Convoc reçue
        CAS 10: Prêt à payer
        """
        evalbox = context.get('evalbox', '')
        date_examen = context.get('date_examen')
        date_examen_passed = context.get('date_examen_passed', False)
        date_examen_future = context.get('date_examen_future', False)
        cloture_passed = context.get('cloture_passed', False)

        # CAS 1: Date vide
        if not date_examen:
            return 1

        # CAS 3: Refusé CMA
        if evalbox == 'Refusé CMA':
            return 3

        # CAS 9: Convoc reçue (seulement si examen pas encore passé)
        if evalbox == 'Convoc CMA reçue' and not date_examen_passed:
            return 9

        # CAS 11: Convoc reçue + examen passé = examen passé, attente résultats
        if evalbox == 'Convoc CMA reçue' and date_examen_passed:
            return 11

        # CAS 10: Prêt à payer
        if evalbox in ['Pret a payer', 'Pret a payer par cheque']:
            return 10

        # CAS 4: VALIDE CMA + date future
        if evalbox == 'VALIDE CMA' and date_examen_future:
            return 4

        # CAS 5: Dossier Synchronisé
        if evalbox == 'Dossier Synchronisé' and date_examen_future:
            return 5

        # CAS 7: Date passée + validé (sans convoc = report d'office probable)
        if date_examen_passed and evalbox in ['VALIDE CMA', 'Dossier Synchronisé']:
            return 7

        # CAS 8: Deadline ratée (date future mais clôture passée + non validé)
        if date_examen_future and cloture_passed:
            if evalbox not in ['VALIDE CMA', 'Dossier Synchronisé', 'Convoc CMA reçue']:
                return 8

        # CAS 2: Date passée + non validé
        if date_examen_passed:
            return 2

        # CAS 6: Date future + autre statut
        if date_examen_future:
            return 6

        return 1  # Fallback

    def _match_consistency_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """
        Match les états de cohérence formation/examen.

        Utilise training_exam_consistency_data du contexte (fourni par
        training_exam_consistency_helper.py).
        """
        consistency_data = context.get('training_exam_consistency_data', {})

        # C1: Formation manquée + examen imminent
        if state_name == 'TRAINING_MISSED_EXAM_IMMINENT':
            if consistency_data.get('training_missed_exam_imminent') is True:
                logger.debug(f"État {state_name} détecté via training_exam_consistency_data")
                return True

        # C3: Dossier not received (documents non reçus par la CMA)
        if state_name == 'DOSSIER_NOT_RECEIVED':
            if consistency_data.get('dossier_not_received') is True:
                logger.debug(f"État {state_name} détecté via training_exam_consistency_data")
                return True

        # A5: Erreur de saisie session (session passée impossible)
        if state_name == 'SESSION_ASSIGNMENT_ERROR':
            if context.get('session_assignment_error') is True:
                logger.info(f"⚠️ État {state_name} détecté: erreur de saisie session")
                return True

        return False

    def _match_force_majeure_consistency_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """
        Match les états combinant force majeure + cohérence.

        FM-1: Formation manquée pour force majeure
        Priorité: Ces états doivent prendre le dessus sur les états date_examen
        car ils représentent une situation exceptionnelle.

        Sources de détection force majeure:
        1. TriageAgent: mentions_force_majeure (dans intent_context)
        2. training_exam_consistency_helper: force_majeure_detected
        """
        if state_name == 'MISSED_TRAINING_FORCE_MAJEURE':
            has_consistency_issue = context.get('has_consistency_issue', False)
            issue_type = context.get('consistency_issue_type')

            # Source 1: TriageAgent
            mentions_force_majeure = context.get('mentions_force_majeure', False)

            # Source 2: training_exam_consistency_helper
            consistency_data = context.get('training_exam_consistency_data', {})
            force_majeure_from_helper = consistency_data.get('force_majeure_detected', False)

            # Utiliser l'une ou l'autre source
            has_force_majeure = mentions_force_majeure or force_majeure_from_helper

            # Debug
            logger.info(f"  🔍 FM-1 check: has_consistency_issue={has_consistency_issue}, issue_type={issue_type}")
            logger.info(f"  🔍 FM-1 check: mentions_force_majeure={mentions_force_majeure}, force_majeure_from_helper={force_majeure_from_helper}")

            if (has_consistency_issue and
                issue_type == 'MISSED_TRAINING_IMMINENT_EXAM' and
                has_force_majeure):
                source = "triage" if mentions_force_majeure else "helper"
                logger.info(f"  ⚠️ État {state_name} détecté: formation manquée + force majeure (source: {source})")
                return True

        return False

    def _match_session_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """
        Match les états session.

        Utilise session_data du contexte (fourni par session_helper.py).
        """
        session_data = context.get('session_data', {})

        # C2: Refresh session available (session de rattrapage disponible)
        if state_name == 'REFRESH_SESSION_AVAILABLE':
            if session_data.get('refresh_session_available') is True:
                logger.debug(f"État {state_name} détecté via session_data")
                return True

        return False

    def _match_blocking_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états de blocage."""
        # B1: Date modification blocked
        if state_name == 'DATE_MODIFICATION_BLOCKED':
            return not context.get('can_modify_exam_date', True)

        return False

    def _match_workflow_state(
        self, state_name: str, detection: Dict, context: Dict
    ) -> bool:
        """Match les états basés sur conditions workflow."""
        conditions = detection.get('conditions', {})
        all_of = conditions.get('all_of', [])

        # Évaluer toutes les conditions
        for condition in all_of:
            if not self._evaluate_condition(condition, context):
                return False

        return len(all_of) > 0

    def _evaluate_condition(self, condition: str, context: Dict) -> bool:
        """Évalue une condition simple."""
        # Parser la condition (format: "field == value" ou "field NOT IN [...]")
        if '==' in condition:
            parts = condition.split('==')
            field = parts[0].strip()
            value = parts[1].strip()

            # Évaluer
            actual = context.get(field)

            if value == 'null' or value == 'None':
                return actual is None
            elif value == 'true' or value == 'True':
                return actual == True
            elif value == 'false' or value == 'False':
                return actual == False
            elif value.isdigit():
                return actual == int(value)
            else:
                return str(actual) == value.strip("'\"")

        if 'NOT IN' in condition:
            parts = condition.split('NOT IN')
            field = parts[0].strip()
            values_str = parts[1].strip()
            # Parser la liste
            values = [v.strip().strip("'\"") for v in values_str.strip('[]').split(',')]
            actual = context.get(field)
            return actual not in values

        return False

    def _create_detected_state(
        self,
        state_name: str,
        state_config: Dict[str, Any],
        context: Dict[str, Any],
        alerts: List[Dict[str, Any]]
    ) -> DetectedState:
        """Crée un objet DetectedState à partir de la configuration."""
        workflow = state_config.get('workflow', {})
        response = state_config.get('response', {})

        return DetectedState(
            id=state_config.get('id', state_name),
            name=state_name,
            priority=state_config.get('priority', 999),
            category=state_config.get('category', 'default'),
            description=state_config.get('description', ''),
            workflow_action=workflow.get('action', 'RESPOND'),
            response_config=response,
            crm_updates_config=state_config.get('crm_updates'),
            detection_reason=f"État {state_name} détecté",
            severity=state_config.get('severity', 'INFO'),
            context_data=context,
            alerts=alerts,
            detected_intent=context.get('detected_intent'),
            intent_context=context.get('intent_context', {})
        )

    def get_state_by_id(self, state_id: str) -> Optional[Dict[str, Any]]:
        """Récupère un état par son ID."""
        for state_name, state_config in self.states.items():
            if state_config.get('id') == state_id:
                return state_config
        return None

    def get_forbidden_terms(self) -> List[str]:
        """Retourne la liste des termes interdits."""
        return self.config.get('forbidden_terms', [])

    def get_required_blocks_global(self) -> List[str]:
        """Retourne les blocs obligatoires globaux."""
        return self.config.get('required_blocks_global', [])
