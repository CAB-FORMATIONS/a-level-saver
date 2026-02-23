"""
TemplateEngine - Génération contrôlée des réponses à partir de templates.

Ce module génère les réponses en combinant:
1. Des templates structurés (blocs fixes) depuis states/templates/base/
2. Des blocs réutilisables depuis states/blocks/
3. Des placeholders remplacés par des données réelles
4. Des sections IA contraintes (personnalisation uniquement)

Syntaxe Handlebars supportée:
- {{variable}} : Remplacement de variable
- {{> bloc_name}} : Inclusion de bloc (partial)
- {{#if condition}}...{{else}}...{{/if}} : Conditionnel
- {{#unless condition}}...{{/unless}} : Conditionnel inverse
- {{#each items}}...{{/each}} : Boucle

L'IA n'intervient QUE pour la personnalisation, pas pour le contenu factuel.
"""

import logging
import re
import yaml
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

import gender_guesser.detector as gender_detector

from .state_detector import DetectedState, DetectedStates
from src.constants.thresholds import (
    CONVOCATION_DAYS_BEFORE_EXAM, EXAM_WITHIN_DAYS, SESSION_STARTS_SOON_DAYS,
    MAX_DATES_DISPLAYED, CMA_CONTACT_URGENT_DAYS, UBER_VERIFICATION_DELAY_DAYS,
)
from src.constants.evalbox import (
    VALIDATED, BLOCKING_MODIFICATION, PAID_STATUSES, PAID_EXCLUDING_REFUSED,
    READY_TO_PAY, DOCUMENTS_PROBLEM, DOSSIER_CONSTITUE, STATUT_DISPLAY,
)
from src.constants.intents import FULL_RECAP_INTENTS, STATUT_INTENTS, DATES_INTENTS
from src.constants.amounts import (
    CMA_EXAM_FEE, CMA_DOSSIER_FEE, CMA_ADMISSION_RETAKE_FEE, CMA_MOBILITE_PRO_FEE,
)
from src.constants.emails import COMPANY_SIGNATURE
from src.utils.date_utils import parse_date_flexible

# Détecteur de genre par prénom (singleton)
_gender_detector = gender_detector.Detector()

logger = logging.getLogger(__name__)

# Feature flag for pybars3-based rendering
# Set to True to use pybars3 library instead of regex-based parsing
PYBARS_ENABLED = True

# Chemins vers les ressources
STATES_PATH = Path(__file__).parent.parent.parent / "states"
TEMPLATES_BASE_PATH = STATES_PATH / "templates" / "base_legacy"  # Migrated to partials
BLOCKS_PATH = STATES_PATH / "blocks"
MATRIX_PATH = STATES_PATH / "state_intention_matrix.yaml"


class TemplateEngine:
    """
    Génère les réponses à partir des templates et de l'état détecté.

    Architecture:
    1. Charge state_intention_matrix.yaml pour blocks_registry et base_templates
    2. Sélectionne le template de base selon l'état (via for_evalbox, for_uber_case, etc.)
    3. Charge les blocs depuis states/blocks/
    4. Parse la syntaxe Handlebars ({{> partial}}, {{#if}}, etc.)
    5. Remplace les placeholders par les données réelles
    """

    def __init__(self, states_path: Optional[Path] = None):
        """
        Initialise le TemplateEngine.

        Args:
            states_path: Chemin vers le dossier states (optionnel)
        """
        self.states_path = states_path or STATES_PATH
        self.templates_base_path = self.states_path / "templates" / "base_legacy"  # Migrated to partials
        self.blocks_path = self.states_path / "blocks"
        self.matrix_path = self.states_path / "state_intention_matrix.yaml"

        # Caches
        self.templates_cache: Dict[str, str] = {}
        self.blocks_cache: Dict[str, str] = {}

        # Charger la matrice état×intention
        self.matrix = self._load_matrix()
        self.blocks_registry = self.matrix.get('blocks_registry', {})
        self.base_templates = self.matrix.get('base_templates', {})
        self.state_intention_matrix = self.matrix.get('matrix', {})

        # Initialize pybars3 renderer if enabled
        if PYBARS_ENABLED:
            from .pybars_renderer import PybarsRenderer
            self.pybars_renderer = PybarsRenderer(self.states_path)
            self.pybars_renderer.load_all_partials()
            logger.info("TemplateEngine: Using pybars3 renderer")
        else:
            self.pybars_renderer = None

        logger.info(f"TemplateEngine initialisé: {len(self.blocks_registry)} blocs, {len(self.base_templates)} templates")

    def _load_matrix(self) -> Dict[str, Any]:
        """Charge state_intention_matrix.yaml."""
        try:
            if self.matrix_path.exists():
                with open(self.matrix_path, 'r', encoding='utf-8') as f:
                    return yaml.safe_load(f) or {}
            else:
                logger.warning(f"Matrice non trouvée: {self.matrix_path}")
                return {}
        except Exception as e:
            logger.error(f"Erreur chargement matrice: {e}")
            return {}

    def generate_response_multi(
        self,
        detected_states: DetectedStates,
        triage_result: Dict[str, Any],
        ai_generator: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Génère une réponse composite gérant multi-intentions et multi-états.

        Args:
            detected_states: Tous les états détectés (blocking, warning, info)
            triage_result: Résultat du triage avec primary_intent et secondary_intents
            ai_generator: Fonction pour générer les sections IA (optionnel)

        Returns:
            {
                'response_text': str,
                'template_used': str,
                'states_used': List[str],
                'intents_handled': List[str],
                ...
            }
        """
        # 1. Si état BLOCKING → réponse unique (comportement actuel)
        if detected_states.blocking_state:
            logger.info(f"🚫 État BLOCKING détecté - réponse unique pour {detected_states.blocking_state.name}")
            result = self.generate_response(detected_states.blocking_state, ai_generator)
            result['states_used'] = [detected_states.blocking_state.name]
            result['is_blocking'] = True
            return result

        # 2. Sinon, combiner les flags de tous les états et intentions
        primary_state = detected_states.primary_state
        if not primary_state:
            logger.warning("Aucun état primaire - utilisation de GENERAL")
            primary_state = self._create_default_state()

        # Copier le contexte du primary_state
        combined_context = primary_state.context_data.copy()

        # Ajouter les flags des états WARNING
        warning_flags = self._map_warning_state_flags(detected_states.warning_states)
        combined_context.update(warning_flags)

        # Ajouter les intentions du triage (primary + secondary)
        combined_context['primary_intent'] = triage_result.get('primary_intent')
        combined_context['secondary_intents'] = triage_result.get('secondary_intents', [])

        # Enrichir le primary_state avec le contexte combiné
        primary_state.context_data = combined_context

        # Collecter les alertes de tous les WARNING states
        all_alerts = list(primary_state.alerts)
        for warning_state in detected_states.warning_states:
            all_alerts.extend(warning_state.alerts)
        primary_state.alerts = all_alerts

        # 3. Générer la réponse avec le contexte combiné
        result = self.generate_response(primary_state, ai_generator)

        # 4. Ajouter les métadonnées multi-états
        result['states_used'] = [s.name for s in detected_states.all_states]
        result['warning_states'] = [s.name for s in detected_states.warning_states]
        result['info_states'] = [s.name for s in detected_states.info_states]
        result['primary_intent'] = triage_result.get('primary_intent')
        result['secondary_intents'] = triage_result.get('secondary_intents', [])
        result['is_blocking'] = False

        intents_handled = []
        if triage_result.get('primary_intent'):
            intents_handled.append(triage_result['primary_intent'])
        intents_handled.extend(triage_result.get('secondary_intents', []))
        result['intents_handled'] = intents_handled

        logger.info(f"📝 Réponse multi-états générée: states={result['states_used']}, intents={intents_handled}")

        return result

    def _create_default_state(self) -> DetectedState:
        """Crée un état GENERAL par défaut."""
        return DetectedState(
            id='DEFAULT',
            name='GENERAL',
            priority=999,
            category='default',
            description='État par défaut',
            workflow_action='RESPOND',
            response_config={},
            crm_updates_config=None,
            detection_reason='Fallback vers état GENERAL',
            severity='INFO',
            context_data={},
            alerts=[]
        )

    def generate_response(
        self,
        state: DetectedState,
        ai_generator: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Génère la réponse complète pour un état donné.

        Args:
            state: État détecté du candidat
            ai_generator: Fonction pour générer les sections IA (optionnel)

        Returns:
            {
                'response_text': str,
                'template_used': str,
                'placeholders_replaced': List[str],
                'ai_sections_generated': List[str],
                'alerts_included': List[str],
                'blocks_included': List[str]
            }
        """
        context = state.context_data

        # 1. Sélectionner le template de base approprié
        template_key, template_config = self._select_base_template(state, context)

        if not template_key:
            logger.warning(f"Pas de template pour l'état {state.name}, utilisation fallback")
            return self._generate_fallback_response(state, ai_generator)

        # 2. Charger le template
        template_file = template_config.get('file', f'templates/base/{template_key}.html')
        template_content = self._load_template(template_file)

        if not template_content:
            logger.warning(f"Template {template_file} non trouvé, utilisation fallback")
            return self._generate_fallback_response(state, ai_generator)

        # 3. Préparer les données pour les placeholders et conditions
        placeholder_data = self._prepare_placeholder_data(state)

        # 4. Parser et résoudre le template (partials, conditionnels, boucles)
        blocks_included = []
        response_text = self._parse_template(template_content, placeholder_data, blocks_included)

        # 5. Remplacer les placeholders simples restants
        response_text, replaced = self._replace_placeholders(response_text, placeholder_data)

        # 5.5 Injecter le choix de session si nécessaire (templates legacy)
        # Si session vide + sessions disponibles + pas déjà dans la réponse → injecter
        response_text = self._inject_session_choice_if_needed(response_text, placeholder_data)

        # 6. Générer les sections IA si nécessaire
        ai_sections = []
        response_config = state.response_config
        ai_section_name = response_config.get('ai_section')
        if ai_section_name and ai_generator and f"{{{{{ai_section_name}}}}}" in response_text:
            ai_content = self._generate_ai_section(state, ai_section_name, ai_generator)
            if ai_content:
                response_text = response_text.replace(f"{{{{{ai_section_name}}}}}", ai_content)
                ai_sections.append(ai_section_name)

        # 7. Ajouter les alertes
        alerts_included = []
        for alert in state.alerts:
            alert_content = self._generate_alert_content(alert, context)
            if alert_content:
                response_text = self._insert_alert(
                    response_text, alert_content, alert.get('position', 'before_signature')
                )
                alerts_included.append(alert.get('id', alert.get('type')))

        # 8. Nettoyer
        response_text = self._cleanup_unresolved_placeholders(response_text)
        response_text = self._strip_comments(response_text)

        return {
            'response_text': response_text.strip(),
            'template_used': template_key,
            'template_file': template_file,
            'placeholders_replaced': replaced,
            'ai_sections_generated': ai_sections,
            'alerts_included': alerts_included,
            'blocks_included': blocks_included,
            # CRM updates définis dans la matrice STATE:INTENTION
            # Ces updates spécifiques à la combinaison ont priorité sur ceux de l'état
            'crm_updates_from_matrix': template_config.get('crm_update', []) if template_config else []
        }

    def _select_base_template(
        self,
        state: DetectedState,
        context: Dict[str, Any]
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Sélectionne le template de base approprié selon l'état et le contexte.

        Ordre de priorité:
        0. Matrice STATE:INTENTION (ex: "DATE_EXAMEN_VIDE:CONFIRMATION_SESSION")
        1. Intention + condition (ex: DEMANDE_IDENTIFIANTS + compte_existe)
        2. Condition seule (ex: has_duplicate_uber_offer)
        3. Cas Uber (A, B, D, E)
        4. Résultat examen (Admis, Non admis)
        5. Evalbox (statut du dossier)
        6. Fallback par nom d'état
        """
        evalbox = context.get('evalbox', '')
        uber_case = self._determine_uber_case(context)
        resultat = context.get('deal_data', {}).get('Resultat', '')
        # Standardiser sur primary_intent avec fallback sur detected_intent (rétrocompat)
        intention = context.get('primary_intent') or context.get('detected_intent', '')

        # DEBUG: Log state and intention for debugging
        logger.info(f"🔍 _select_base_template: state={state.name}, intention={intention}, evalbox={evalbox}")

        # PASS 0: Chercher dans la matrice STATE:INTENTION (priorité maximale)
        # Format: "STATE_NAME:INTENTION" -> configuration spécifique
        if intention:
            # 0a. D'abord essayer match exact STATE:INTENTION
            matrix_key = f"{state.name}:{intention}"
            config = self.state_intention_matrix.get(matrix_key)

            # 0b. Si pas de match exact, essayer wildcard *:INTENTION
            if not config:
                wildcard_key = f"*:{intention}"
                if wildcard_key in self.state_intention_matrix:
                    config = self.state_intention_matrix[wildcard_key]
                    matrix_key = wildcard_key  # Pour le logging
                    logger.info(f"✅ Template sélectionné via wildcard: {wildcard_key}")

            if config:
                template_file = config.get('template', '')
                # Extraire le nom du template sans extension
                template_key = template_file.replace('.html', '').replace('.md', '')
                if matrix_key != f"*:{intention}":  # Ne pas doubler le log pour wildcard
                    logger.info(f"✅ Template sélectionné via matrice: {matrix_key} -> {template_file}")

                # Injecter les context_flags dans le contexte global ET dans state.context_data
                # Ces flags permettent aux templates hybrides de savoir quelle intention traiter
                context_flags = config.get('context_flags', {})
                if context_flags:
                    context.update(context_flags)
                    # IMPORTANT: Aussi mettre à jour state.context_data pour _prepare_placeholder_data
                    state.context_data.update(context_flags)
                    logger.info(f"📌 Context flags injectés: {list(context_flags.keys())}")

                # Construire la config au format attendu
                # response_master.html est dans templates/, pas templates/base/
                if template_file == 'response_master.html':
                    file_path = 'templates/response_master.html'
                else:
                    file_path = f'templates/base/{template_file}'

                # Support both 'crm_update' and 'crm_updates' keys for flexibility
                crm_update_config = config.get('crm_updates') or config.get('crm_update', [])
                return template_key, {
                    'file': file_path,
                    'blocks': config.get('blocks', []),
                    'crm_update': crm_update_config,
                    'context_flags': context_flags,
                }

        # PASS 1: Templates avec intention (priorité haute)
        for template_key, config in self.base_templates.items():
            if 'for_intention' in config:
                if intention == config['for_intention']:
                    # Vérifier aussi la condition si elle existe
                    if 'for_condition' in config:
                        if not self._evaluate_condition(config['for_condition'], context):
                            continue  # Condition non satisfaite, passer au suivant
                    # Injecter les context_flags (FIX: manquait dans PASS 1)
                    context_flags = config.get('context_flags', {})
                    if context_flags:
                        context.update(context_flags)
                        state.context_data.update(context_flags)
                    return template_key, config

        # PASS 1.5: Templates avec for_state (état spécifique)
        # Priorité sur les conditions génériques pour éviter que no_compte_examt3p
        # ne match pour des états comme EXAM_DATE_PAST_VALIDATED
        for template_key, config in self.base_templates.items():
            if 'for_state' in config:
                if state.name == config['for_state']:
                    logger.info(f"✅ Template sélectionné via for_state: {state.name} -> {template_key}")
                    self._inject_context_flags(config, context, state, "PASS 1.5")
                    return template_key, config

        # PASS 2: Templates avec condition seule (sans intention et sans for_state)
        for template_key, config in self.base_templates.items():
            if 'for_condition' in config and 'for_intention' not in config and 'for_state' not in config:
                if self._evaluate_condition(config['for_condition'], context):
                    self._inject_context_flags(config, context, state, "PASS 2")
                    return template_key, config

        # PASS 3: Cas Uber
        for template_key, config in self.base_templates.items():
            if 'for_uber_case' in config:
                if uber_case == config['for_uber_case']:
                    self._inject_context_flags(config, context, state, "PASS 3")
                    return template_key, config

        # PASS 4: Résultat examen
        for template_key, config in self.base_templates.items():
            if 'for_resultat' in config:
                if resultat == config['for_resultat']:
                    self._inject_context_flags(config, context, state, "PASS 4")
                    return template_key, config

        # PASS 5: DÉSACTIVÉ - Plus de fallback vers templates legacy (for_evalbox)
        # Règle 14: JAMAIS de fallback legacy - tout doit passer par response_master.html
        # Les anciens templates base_legacy/ sont obsolètes
        # for template_key, config in self.base_templates.items():
        #     if 'for_evalbox' in config:
        #         if evalbox == config['for_evalbox']:
        #             self._inject_context_flags(config, context, state, "PASS 5")
        #             return template_key, config

        # Fallback by name: DÉSACTIVÉ - Plus de fallback vers templates legacy
        # state_name_normalized = state.name.lower().replace('_', '-')
        # for template_key, config in self.base_templates.items():
        #     if template_key.lower() == state_name_normalized:
        #         self._inject_context_flags(config, context, state, "Fallback by name")
        #         return template_key, config

        # FALLBACK FINAL: TOUJOURS utiliser response_master.html
        # Architecture moderne : matrice STATE:INTENTION + response_master.html
        logger.info(f"📝 Utilisation de response_master.html pour {state.name}:{intention}")
        return 'response_master', {
            'file': 'templates/response_master.html',
            'description': f'Template master pour {state.name}',
        }

    def _inject_context_flags(
        self,
        config: Dict[str, Any],
        context: Dict[str, Any],
        state: DetectedState,
        pass_name: str
    ) -> None:
        """
        Injecte les context_flags d'un template dans le contexte et state.context_data.

        Args:
            config: Configuration du template (peut contenir 'context_flags')
            context: Contexte global à modifier
            state: État détecté (state.context_data sera aussi modifié)
            pass_name: Nom du PASS pour le logging (ex: "PASS 1.5")
        """
        context_flags = config.get('context_flags', {})
        if context_flags:
            context.update(context_flags)
            state.context_data.update(context_flags)
            logger.info(f"📌 Context flags injectés ({pass_name}): {list(context_flags.keys())}")

    def _determine_uber_case(self, context: Dict[str, Any]) -> str:
        """
        Récupère le cas Uber depuis le contexte.

        La logique de détection complète (avec vérification J+1, PROSPECT, etc.)
        est dans StateDetector._determine_uber_case() qui est la source de vérité.
        Le résultat est stocké dans context['uber_case'] par _build_context().
        """
        # Priorité: utiliser la valeur calculée par StateDetector
        if 'uber_case' in context:
            return context['uber_case']

        # Fallback pour rétrocompatibilité (si appelé sans contexte enrichi)
        if not context.get('is_uber_20_deal'):
            return 'NOT_UBER'
        if context.get('is_uber_prospect'):
            return 'PROSPECT'
        if not context.get('date_dossier_recu'):
            return 'A'
        if not context.get('compte_uber', True):
            return 'D'
        if not context.get('eligible_uber', True):
            return 'E'
        if not context.get('date_test_selection'):
            return 'B'
        return 'ELIGIBLE'

    def _load_template(self, template_path: str) -> Optional[str]:
        """Charge un template depuis le cache ou le fichier."""
        if template_path in self.templates_cache:
            return self.templates_cache[template_path]

        # Construire le chemin complet - ordre de recherche:
        # 1. Chemin relatif depuis states_path (ex: templates/base/xxx.html)
        # 2. Directement dans templates/ (ex: response_master.html)
        # 3. Dans templates/base/ (fallback)
        full_path = self.states_path / template_path

        if not full_path.exists():
            # Essayer dans states/templates/ directement
            templates_root = self.states_path / "templates"
            full_path = templates_root / Path(template_path).name
            if not full_path.exists():
                # Essayer dans templates/base/
                full_path = self.templates_base_path / Path(template_path).name
                if not full_path.exists():
                    logger.warning(f"Template non trouvé: {template_path}")
                    return None

        try:
            content = full_path.read_text(encoding='utf-8')
            # Nettoyer le contenu: supprimer commentaires HTML et espaces inutiles
            content = self._clean_block_content(content)
            self.templates_cache[template_path] = content
            return content
        except Exception as e:
            logger.error(f"Erreur lecture template {template_path}: {e}")
            return None

    def _load_block(self, block_name: str) -> Optional[str]:
        """Charge un bloc depuis le cache ou le fichier."""
        if block_name in self.blocks_cache:
            return self.blocks_cache[block_name]

        # Chercher dans le registry
        block_config = self.blocks_registry.get(block_name, {})
        block_file = block_config.get('file', f'blocks/{block_name}.md')

        # Construire le chemin
        full_path = self.states_path / block_file

        if not full_path.exists():
            # Essayer avec le path direct dans blocks/
            full_path = self.blocks_path / f"{block_name}.md"
            if not full_path.exists():
                logger.warning(f"Bloc non trouvé: {block_name}")
                return None

        try:
            content = full_path.read_text(encoding='utf-8')
            # Nettoyer le contenu: supprimer commentaires HTML et espaces inutiles
            content = self._clean_block_content(content)
            self.blocks_cache[block_name] = content
            return content
        except Exception as e:
            logger.error(f"Erreur lecture bloc {block_name}: {e}")
            return None

    def _clean_block_content(self, content: str) -> str:
        """Nettoie le contenu d'un bloc en supprimant commentaires et espaces inutiles."""
        import re
        # Supprimer les commentaires HTML (<!-- ... -->)
        content = re.sub(r'<!--.*?-->\s*', '', content, flags=re.DOTALL)
        # Supprimer les lignes vides multiples
        content = re.sub(r'\n\s*\n', '\n', content)
        # Supprimer les espaces en début et fin
        content = content.strip()
        return content

    def _parse_template(
        self,
        template: str,
        context: Dict[str, Any],
        blocks_included: List[str]
    ) -> str:
        """
        Parse le template et résout les partials, conditionnels, boucles.

        Uses pybars3 library for robust Handlebars parsing.
        Supports: {{variable}}, {{> partial}}, {{#if}}, {{#unless}}, {{#each}}
        """
        if not self.pybars_renderer:
            raise RuntimeError("PybarsRenderer not initialized. Ensure PYBARS_ENABLED=True")

        return self.pybars_renderer.render(template, context)

    # =========================================================================
    # REMOVED: Legacy regex-based Handlebars parsing methods
    # The following methods have been removed in favor of pybars3:
    # - _resolve_partials()
    # - _load_partial_path()
    # - _resolve_if_blocks()
    # - _get_context_value_with_path()
    # - _resolve_unless_blocks()
    # - _resolve_each_blocks()
    # - _resolve_if_blocks_in_each_item()
    #
    # pybars3 provides robust, library-based Handlebars parsing that:
    # - Properly handles nested conditionals
    # - Correctly strips HTML/Handlebars comments
    # - Supports all standard Handlebars syntax
    # =========================================================================

    def _get_context_value(self, key: str, context: Dict[str, Any]) -> Any:
        """Récupère une valeur du contexte, avec support des clés imbriquées."""
        # PRIORITÉ 1: Vérifier si la clé existe directement dans le contexte
        # Ceci permet à placeholder_data de surcharger les mappings legacy
        if key in context:
            return context[key]

        # PRIORITÉ 2: Mappings legacy pour rétrocompatibilité
        # (utilisés uniquement si la clé n'existe pas directement)
        if key == 'uber_20':
            return context.get('is_uber_20_deal', False)
        if key == 'can_choose_other_department':
            return not context.get('compte_existe', True)
        if key == 'session_choisie':
            return context.get('session_assigned', False)
        if key == 'compte_existe':
            return context.get('compte_existe', False)
        if key == 'identifiant_examt3p':
            return context.get('examt3p_data', {}).get('identifiant', '')
        if key == 'mot_de_passe_examt3p':
            return context.get('examt3p_data', {}).get('mot_de_passe', '')

        # Mapping prochaines_dates depuis next_dates
        if key == 'prochaines_dates':
            next_dates = context.get('next_dates', [])
            if next_dates:
                formatted_dates = []
                for d in next_dates[:MAX_DATES_DISPLAYED]:  # Limiter à 5 dates
                    date_str = d.get('Date_Examen', '')
                    date_formatted = self._format_date(date_str) if date_str else ''
                    cloture_str = d.get('Date_Cloture_Inscription', '')
                    cloture_formatted = self._format_date(cloture_str) if cloture_str else ''
                    formatted_dates.append({
                        'date': date_formatted,
                        'departement': d.get('Departement', ''),
                        'cloture': cloture_formatted
                    })
                return formatted_dates
            return []

        # Chercher dans deal_data (fallback pour clés non mappées)
        deal_data = context.get('deal_data', {})
        if key in deal_data:
            return deal_data[key]

        # Chercher dans examt3p_data
        examt3p_data = context.get('examt3p_data', {})
        if key in examt3p_data:
            return examt3p_data[key]

        return None

    def _evaluate_condition(self, condition: str, context: Dict[str, Any]) -> bool:
        """Évalue une condition de type 'variable == value'."""
        if '==' in condition:
            parts = condition.split('==')
            if len(parts) == 2:
                var_name = parts[0].strip()
                expected = parts[1].strip().strip("'\"")
                actual = self._get_context_value(var_name, context)

                if expected.lower() == 'true':
                    return actual == True
                if expected.lower() == 'false':
                    return actual == False
                return str(actual) == expected

        return False

    def _prepare_placeholder_data(self, state: DetectedState) -> Dict[str, Any]:
        """Prépare les données pour remplacer les placeholders."""
        context = state.context_data
        deal_data = context.get('deal_data', {})
        contact_data = context.get('contact_data', {})  # Données du Contact lié (First_Name, Last_Name)
        examt3p_data = context.get('examt3p_data', {})
        enriched_lookups = context.get('enriched_lookups') or {}
        intent_context = context.get('intent_context', {})

        # Extraire le prénom et nom depuis Contact (pas Deal)
        prenom = self._extract_prenom_from_contact(contact_data, deal_data)
        nom = contact_data.get('Last_Name', '') or ''

        # Utiliser les lookups enrichis (v2.2) si disponibles, sinon fallback
        if enriched_lookups.get('date_examen'):
            date_examen = enriched_lookups['date_examen']
        else:
            date_examen = context.get('date_examen_vtc_value') or context.get('date_examen')
        date_examen_formatted = self._format_date(date_examen) if date_examen else ''

        # Département depuis lookups enrichis ou fallback
        departement = enriched_lookups.get('departement') or context.get('departement', '')

        # Préparer les dates proposées
        dates_proposees = self._format_dates_list(context.get('next_dates', []))

        # Préparer le statut actuel
        evalbox = context.get('evalbox', '')
        statut_actuel = self._format_statut(evalbox)

        # Calculer la date de convocation (environ 10 jours avant l'examen)
        date_convocation = ''
        if date_examen:
            from datetime import timedelta
            exam_date_parsed = parse_date_flexible(date_examen, 'date_examen_convocation')
            if exam_date_parsed:
                convoc_date = exam_date_parsed - timedelta(days=CONVOCATION_DAYS_BEFORE_EXAM)
                date_convocation = convoc_date.strftime('%d/%m/%Y')

        # Détection du genre via le prénom
        is_female = self._detect_gender_from_name(prenom) == 'female'
        is_male = not is_female  # Par défaut masculin si inconnu

        result = {
            # Infos candidat (depuis Contact, pas Deal)
            'prenom': prenom or 'Bonjour',
            'nom': nom,
            'email': contact_data.get('Email') or deal_data.get('Email', ''),
            'is_female': is_female,
            'is_male': is_male,

            # Identifiants ExamT3P
            'identifiant_examt3p': examt3p_data.get('identifiant', ''),
            'mot_de_passe_examt3p': examt3p_data.get('mot_de_passe', ''),
            'connection_test_success': examt3p_data.get('connection_test_success', False),
            'credentials_login_failed': examt3p_data.get('credentials_login_failed', False),

            # Dates
            'date_examen': date_examen_formatted or '',
            'date_examen_raw': date_examen or '',
            'date_examen_formatted': date_examen_formatted,
            'date_cloture': self._format_date(context.get('date_cloture', '')) if context.get('date_cloture') else '',
            'date_convocation': date_convocation,
            'dates_proposees': dates_proposees,

            # Département
            'departement': departement,

            # Session - utilise les lookups enrichis (v2.2) ou fallback sur legacy
            # Le legacy fournit la logique (quelles sessions proposer), le State Engine formate l'affichage
            # Afficher un nom nettoyé (ex: "Cours du jour") plutôt que le nom CRM brut (ex: "cdj-montreuil- thu1 - ...")
            'session_choisie': self._clean_session_display_name(enriched_lookups) or self._format_session(deal_data.get('Session')),
            'session_message': context.get('session_data', {}).get('message', ''),
            # session_preference: priorité à intent_context (détecté par triage) puis session_data (legacy)
            'session_preference': self._get_session_preference(context),
            'session_preference_soir': self._get_session_preference(context) == 'soir',
            'session_preference_jour': self._get_session_preference(context) == 'jour',
            # Données aplaties pour itération facile dans les templates
            # FILTRER selon la préférence si l'intention est CONFIRMATION_SESSION
            'sessions_proposees': self._flatten_session_options_filtered(context),

            # Session confirmée par le candidat (CONFIRMATION_SESSION)
            # Priorité: matched_session (nouveau matching) > enriched_lookups (session déjà assignée)
            'session_confirmed': context.get('session_confirmed', False) or bool(enriched_lookups.get('session_name')),
            'session_after_exam': context.get('session_after_exam', False),
            'session_after_exam_can_reposition': context.get('session_after_exam_can_reposition', False),
            'session_deja_commencee': context.get('session_already_started', False),
            'matched_session_name': context.get('matched_session_name', '') or ('Cours du soir' if enriched_lookups.get('session_type') == 'soir' else 'Cours du jour' if enriched_lookups.get('session_type') == 'jour' else 'votre session de formation'),
            'matched_session_start': self._format_date(context.get('matched_session_start') or enriched_lookups.get('session_date_debut', '')),
            'matched_session_end': self._format_date(context.get('matched_session_end') or enriched_lookups.get('session_date_fin', '')),
            # Alias pour compatibilité
            'date_debut_formation': self._format_date(context.get('matched_session_start') or enriched_lookups.get('session_date_debut', '')),
            'date_fin_formation': self._format_date(context.get('matched_session_end') or enriched_lookups.get('session_date_fin', '')),

            # Flags temporels session (conscience temporelle générale)
            **self._compute_session_temporal_flags(
                context.get('matched_session_start') or enriched_lookups.get('session_date_debut', ''),
                context.get('matched_session_end') or enriched_lookups.get('session_date_fin', '')
            ),

            # Statut
            'statut_actuel': statut_actuel,
            'evalbox_status': evalbox,
            'num_dossier_cma': examt3p_data.get('num_dossier', ''),

            # Booléens pour les statuts Evalbox (pour templates conditionnels)
            'evalbox_dossier_cree': evalbox == 'Dossier crée',
            'evalbox_dossier_synchronise': evalbox == 'Dossier Synchronisé',
            'evalbox_pret_a_payer': evalbox in READY_TO_PAY,
            'evalbox_valide_cma': evalbox == 'VALIDE CMA',
            'evalbox_refus_cma': evalbox == 'Refusé CMA',
            'evalbox_documents_refuses': evalbox in DOCUMENTS_PROBLEM,
            'evalbox_convoc_recue': evalbox == 'Convoc CMA reçue',
            'no_evalbox_status': not evalbox or evalbox in ['None', '', 'N/A'],
            # Flag pour avertissement modification identifiants (true = paiement non effectué)
            # Payé = Dossier Synchronisé, VALIDE CMA, Convoc CMA reçue, Refusé CMA
            # Non payé = Dossier crée, Pret a payer, Documents refusés/manquants, N/A
            'evalbox_non_paye': evalbox not in PAID_STATUSES,

            # Numéro de dossier
            'num_dossier': examt3p_data.get('num_dossier', '') or context.get('num_dossier', ''),

            # Prochaines étapes
            'prochaines_etapes': self._get_prochaines_etapes(state),

            # Booléens pour les conditions (aussi disponibles comme placeholders)
            # Note: uber_20 et is_uber_20_deal sont synonymes pour supporter les deux notations dans les templates
            'uber_20': context.get('is_uber_20_deal', False),
            'is_uber_20_deal': context.get('is_uber_20_deal', False),
            # uber_eligible = Uber 20€ + Compte_Uber vérifié + ELIGIBLE vérifié
            'uber_eligible': (
                context.get('is_uber_20_deal', False) and
                context.get('compte_uber', False) and
                context.get('eligible_uber', False)
            ),
            # Timeline vérification éligibilité Uber (basée sur Date_Dossier_reçu)
            # Utilisé dans partials/uber/eligibility_status.html
            **self._compute_uber_eligibility_timeline(context, deal_data),

            # Frais d'examen : si "Oui", CAB paye les 241€ (Uber, partenariats, etc.)
            # Champ CRM: EXAM_INCLUS (picklist: Oui/Non/N/A)
            'exam_inclus': deal_data.get('EXAM_INCLUS', '') == 'Oui',
            'cab_paye_examen': deal_data.get('EXAM_INCLUS', '') == 'Oui',
            # Montants métier (variables Handlebars — source: src/constants/amounts.py)
            'frais_examen_complet': CMA_EXAM_FEE,
            'frais_repassage_admission': CMA_ADMISSION_RETAKE_FEE,
            'frais_mobilite_pro': CMA_MOBILITE_PRO_FEE,
            'frais_dossier': CMA_DOSSIER_FEE,
            'can_choose_other_department': context.get('can_choose_other_department', False) or not context.get('compte_existe', True),
            # Session assignée: soit explicitement dans context, soit déduit des enriched_lookups
            'session_assigned': context.get('session_assigned', False) or bool(enriched_lookups.get('session_name')),
            'session_is_jour': enriched_lookups.get('session_type') == 'jour',
            'session_is_soir': enriched_lookups.get('session_type') == 'soir',
            'compte_existe': context.get('compte_existe', False),
            'personal_account_warning': context.get('personal_account_warning', False),
            # Erreur de saisie session (A5)
            'session_assignment_error': context.get('session_assignment_error', False),
            'session_error_dates': context.get('session_error_dates', ''),
            'session_error_correct_year': context.get('session_error_correct_year'),
            # Type de session d'origine (pour erreur de saisie - on garde la préférence)
            'original_session_type': enriched_lookups.get('session_type'),  # 'jour' ou 'soir'
            'original_session_type_text': 'cours du soir' if enriched_lookups.get('session_type') == 'soir' else ('cours du jour' if enriched_lookups.get('session_type') == 'jour' else ''),
            # Correction automatique erreur d'année (session corrigée)
            'session_year_error_corrected': context.get('session_year_error_corrected', False),
            'session_year_error_corrected_name': context.get('session_year_error_corrected_name', ''),
            'session_year_error_corrected_start': context.get('session_year_error_corrected_start', ''),
            'session_year_error_corrected_end': context.get('session_year_error_corrected_end', ''),
            'session_year_error_corrected_start_formatted': self._format_date(context.get('session_year_error_corrected_start', '')) if context.get('session_year_error_corrected_start') else '',
            'session_year_error_corrected_end_formatted': self._format_date(context.get('session_year_error_corrected_end', '')) if context.get('session_year_error_corrected_end') else '',
            'can_modify_exam_date': context.get('can_modify_exam_date', True),
            'cloture_passed': context.get('cloture_passed', False),
            'deadline_passed_reschedule': context.get('deadline_passed_reschedule', False),
            'new_exam_date': self._format_date(context.get('new_exam_date', '')) if context.get('new_exam_date') else '',
            'new_exam_date_cloture': self._format_date(context.get('new_exam_date_cloture', '')) if context.get('new_exam_date_cloture') else '',
            'original_exam_date': self._format_date(context.get('original_exam_date', '')) if context.get('original_exam_date') else '',
            'original_date_cloture': self._format_date(context.get('original_date_cloture', '')) if context.get('original_date_cloture') else '',

            # Flags temporels pour templates (comparateurs Handlebars non supportés)
            'exam_within_7_days': context.get('exam_within_7_days', False),
            'exam_within_10_days': context.get('exam_within_10_days', False),
            'exam_within_30_days': (context.get('days_until_exam') is not None and 0 < context.get('days_until_exam', 999) <= EXAM_WITHIN_DAYS),
            'examen_pas_encore_passe': context.get('examen_pas_encore_passe', False),
            'examen_passe': context.get('examen_passe', False) or context.get('date_examen_passed', False),
            'examen_imminent': context.get('examen_imminent', False),
            'exam_today': context.get('days_until_exam') == 0 if context.get('days_until_exam') is not None else False,
            'convocation_anormale': context.get('convocation_anormale', False),
            'days_until_exam': context.get('days_until_exam'),

            # Pièces refusées (pour templates Refus CMA)
            'pieces_refusees_details': context.get('pieces_refusees_details', []),
            'has_pieces_refusees': context.get('has_pieces_refusees', False) or bool(context.get('pieces_refusees_details', [])),

            # CAS 3 Refusé CMA: date passée + prochaine date (report automatique)
            'date_examen_is_past': self._is_date_passed(date_examen) if date_examen else False,
            'next_exam_date_cas3': self._get_next_exam_date_cas3(context),
            'next_date_cloture_cas3': self._get_next_cloture_cas3(context),

            # Prospect (alias pour templates)
            'is_prospect': context.get('is_prospect', False) or context.get('is_uber_prospect', False),
            # Afficher rappel prospect seulement si PAS uber_prospect (évite doublon)
            'show_prospect_rappel': (context.get('is_prospect', False) or context.get('is_uber_prospect', False)) and not context.get('uber_prospect', False),

            # Booléens pour proposer dates/sessions
            'date_examen_vide': not date_examen,
            'session_vide': not deal_data.get('Session'),
            'has_sessions_proposees': bool(self._flatten_session_options_filtered(context)),
            # Flag: aucune alternative session (ni proposées, ni closest before/after)
            # Utilise _filter_closest_sessions_by_exam pour exclure les sessions après l'examen (Rule 16)
            'no_session_alternatives': self._compute_no_session_alternatives(context),

            # Cascade d'alternatives (DEMANDE_CHANGEMENT_SESSION)
            'session_change_includes_next_date': context.get('session_change_includes_next_date', False),
            'session_change_needs_cma': context.get('session_change_needs_cma', False),

            # Force majeure (pour les templates empathiques)
            'mentions_force_majeure': context.get('mentions_force_majeure', False),
            'force_majeure_type': context.get('force_majeure_type'),
            'force_majeure_details': context.get('force_majeure_details', ''),
            'is_force_majeure_deces': context.get('is_force_majeure_deces', False),
            'is_force_majeure_medical': context.get('is_force_majeure_medical', False),
            'is_force_majeure_accident': context.get('is_force_majeure_accident', False),
            'is_force_majeure_childcare': context.get('is_force_majeure_childcare', False),
            'is_force_majeure_other': context.get('is_force_majeure_other', False),

            # Repositionnement implicite de date d'examen
            'implicit_date_repositioning': context.get('implicit_date_repositioning', False),
            'engagement_level': context.get('engagement_level', {}).get('level', -1),
            'engagement_can_reposition': context.get('engagement_level', {}).get('can_reposition', False),
            'engagement_needs_cma_message': context.get('engagement_level', {}).get('needs_cma_message', False),
            'repositioning_month_name': context.get('repositioning_month_name', ''),
            'repositioning_target_date': self._format_date(context.get('repositioning_target_date', '')) if context.get('repositioning_target_date') else '',

            # Context flags pour templates hybrides
            # AUTO-MAPPING: Génère automatiquement les flags depuis primary_intent et secondary_intents
            # Priorité: context_flags de la matrice > auto-mapping depuis intentions
            **self._log_and_return_intention_flags(context),

            # Context flags pour conditions bloquantes (Section 0 de response_master)
            # Ces flags sont définis via context_flags dans la matrice STATE:INTENTION
            # ou via _map_warning_state_flags pour les états WARNING
            'uber_cas_a': context.get('uber_cas_a', False),
            'uber_cas_b': context.get('uber_cas_b', False),
            'uber_cas_d': context.get('uber_cas_d', False) and not context.get('uber_cas_d_email_received', False),
            'uber_cas_d_email_received': context.get('uber_cas_d_email_received', False),
            'uber_alternative_email': context.get('uber_alternative_email', ''),
            'uber_cas_e': context.get('uber_cas_e', False),
            'uber_remboursement_accepte': False,  # Set to True in CAS D/E block when DEMANDE_ANNULATION
            'uber_doublon': context.get('uber_doublon', False),
            'uber_doublon_clarification': context.get('uber_doublon_clarification', False),
            'uber_doublon_recoverable': context.get('uber_doublon_recoverable', False),
            # Candidat mentionne ancien dossier mais aucun deal trouvé
            'identity_confirmation_no_deal': context.get('identity_confirmation_no_deal', False),
            'uber_prospect': context.get('uber_prospect', False),
            # Infos pour clarification doublon
            'duplicate_deal_name': context.get('duplicate_deal_name', ''),
            'duplicate_type_recoverable': context.get('duplicate_type_recoverable', False),
            'duplicate_type_refus_cma': context.get('duplicate_type_refus_cma', False),
            'already_paid_to_cma': context.get('already_paid_to_cma', False),

            # Résultats d'examen
            'resultat_admis': context.get('resultat_admis', False),
            'resultat_non_admis': context.get('resultat_non_admis', False),
            'resultat_non_admissible': context.get('resultat_non_admissible', False),
            'resultat_admissible': context.get('resultat_admissible', False),
            'resultat_absent': context.get('resultat_absent', False),
            'resultat_convoc_pas_recu': context.get('resultat_convoc_pas_recu', False),
            'resultat_plus_interesse': context.get('resultat_plus_interesse', False),
            'dossier_termine': context.get('dossier_termine', False),
            'resultat_category': context.get('resultat_category', 'pre_exam'),
            'demande_attestation_resultat': context.get('demande_attestation_resultat', False),

            # Report de date - Générer les flags depuis can_modify_exam_date et intention
            # NOTE: Ces flags sont générés en premier car ils affectent show_sessions_section
            **self._generate_report_flags(context),

            # Problèmes d'identifiants
            'credentials_invalid': context.get('credentials_invalid', False),
            'credentials_inconnus': context.get('credentials_inconnus', False),
            'candidat_envoie_identifiants': (context.get('primary_intent') or context.get('detected_intent', '')) == 'ENVOIE_IDENTIFIANTS',

            # Blocage confirmation session (documents manquants ou credentials invalides)
            # NOTE: La clôture passée (CAS 8) n'est PAS un blocage - on redirige vers la nouvelle date
            'session_confirmation_blocked': context.get('session_confirmation_blocked', False),
            'session_blocking_reason': context.get('session_blocking_reason'),
            'session_blocked_documents_manquants': context.get('session_blocked_documents_manquants', False),
            'session_blocked_credentials_invalides': context.get('session_blocked_credentials_invalides', False),

            # Données supplémentaires pour templates hybrides
            'has_next_dates': bool(context.get('next_dates', [])),
            'next_dates': self._format_next_dates_for_template(
                context.get('next_dates', []),
                context.get('session_data'),
                self._get_session_preference(context)
            ),
            'preference_horaire_text': 'cours du soir' if self._get_session_preference(context) == 'soir' else 'cours du jour',

            # Préférence de session (depuis intent_context ou deal)
            'session_preference': self._get_session_preference(context),
            'session_preference_jour': self._get_session_preference(context) == 'jour',
            'session_preference_soir': self._get_session_preference(context) == 'soir',

            # Filtrage par mois demandé (REPORT_DATE)
            'no_date_for_requested_month': context.get('no_date_for_requested_month', False),
            'requested_month_name': context.get('requested_month_name', ''),
            'requested_location': context.get('requested_location', ''),
            'same_month_other_depts': self._format_next_dates_for_template(
                context.get('same_month_other_depts', []),
                context.get('session_data'),
                self._get_session_preference(context)
            ),
            'same_dept_other_months': self._format_next_dates_for_template(
                context.get('same_dept_other_months', []),
                context.get('session_data'),
                self._get_session_preference(context)
            ),

            # Dates alternatives dans d'autres departements (plus tot que la reference)
            'alternative_department_dates': self._format_next_dates_for_template(
                context.get('alternative_department_dates', []),
                context.get('session_data'),
                self._get_session_preference(context)
            ),
            'has_alternative_department_dates': bool(context.get('alternative_department_dates', [])),

            # Cross-department comparison (dates plus proches dans d'autres CMA)
            **self._prepare_cross_department_comparison(
                current_date=date_examen,
                current_dept=departement,
                alternative_dates=context.get('alternative_department_dates', []),
                compte_existe=context.get('compte_existe', False)
            ),

            # Cross-department data enrichie (si disponible via cross_department_helper)
            **self._extract_cross_department_data(context),
            'cross_department_data': context.get('cross_department_data', {}),

            # Cross-department fallback for REPORT_DATE when dept has no alternatives
            'no_dates_in_own_dept': context.get('no_dates_in_own_dept', False),

            # Early date request flags (DEMANDE_DATE_PLUS_TOT)
            'has_earlier_options': context.get('has_earlier_options', False),
            'no_earlier_dates_available': context.get('no_earlier_dates_available', False),
            'suppress_next_dates': context.get('suppress_next_dates', False),

            # Month-based cross-department data (mode clarification - mois demandé sans date locale)
            'month_cross_department': context.get('month_cross_department', {}),
            'has_month_in_other_depts': context.get('has_month_in_other_depts', False),

            # CMA contact flags (pour le partial contact_cma.html)
            **self._prepare_cma_contact_flags(context),

            # Dates deja communiquees (anti-repetition)
            'dates_already_communicated': context.get('dates_already_communicated', False),
            'dates_proposed_recently': context.get('dates_proposed_recently', False),

            # ===== AUTO-ASSIGNATION DATE/SESSION =====
            # Quand la date d'examen était vide, on auto-assigne:
            # - Scénario A: prochaine date + session compatible selon préférence
            # - Scénario B: date après fin de session confirmée
            'auto_assigned': context.get('auto_assigned', False),
            'auto_assigned_exam_date': self._format_date(context.get('auto_assigned_exam_date', '')),
            'auto_assigned_exam_date_raw': context.get('auto_assigned_exam_date', ''),
            'auto_assigned_session': context.get('auto_assigned_session'),
            'auto_assigned_session_name': context.get('auto_assigned_session', {}).get('Name', '') if context.get('auto_assigned_session') else '',
            'auto_assigned_session_start': self._format_date(context.get('auto_assigned_session', {}).get('Date_d_but', '')) if context.get('auto_assigned_session') else '',
            'auto_assigned_session_end': self._format_date(context.get('auto_assigned_session', {}).get('Date_fin', '')) if context.get('auto_assigned_session') else '',
            'auto_assigned_session_type': context.get('auto_assigned_session', {}).get('session_type', '') if context.get('auto_assigned_session') else '',

            # Date precedemment communiquee (pour mode clarification)
            'previously_communicated_date': context.get('cab_proposals', {}).get('last_proposed_exam_date', ''),
            'date_changed_since_last_comm': self._check_date_changed(
                date_examen_formatted,
                context.get('cab_proposals', {}).get('last_proposed_exam_date', '')
            ),

            # direct_answer désactivé — le contenu est géré par les partials
            # d'intention et de statut (pipeline déterministe)
            'direct_answer': '',

            # Mode de communication du candidat (clarification vs request)
            'communication_mode': context.get('communication_mode', 'request'),
            'references_previous_communication': context.get('references_previous_communication', False),
            'mentions_discrepancy': context.get('mentions_discrepancy', False),
            'is_clarification_mode': context.get('is_clarification_mode', False),
            'is_verification_mode': context.get('is_verification_mode', False),
            'is_follow_up_mode': context.get('is_follow_up_mode', False),
            # Demande de complétion dossier précédente (pour Uber 20€)
            'previously_asked_to_complete': context.get('previously_asked_to_complete', False),

            # Choix remboursement CMA (pour ERREUR_PAIEMENT_CMA)
            'remboursement_cma_choice_remboursement': intent_context.get('remboursement_cma_choice') == 'remboursement',
            'remboursement_cma_choice_conserver': intent_context.get('remboursement_cma_choice') == 'conserver',

            # Motif annulation (pour DEMANDE_ANNULATION)
            'cancellation_is_timing': intent_context.get('cancellation_reason') == 'timing',
            'cancellation_is_retractation': intent_context.get('cancellation_reason') == 'retractation',
            'cancellation_is_contestation': intent_context.get('cancellation_reason') == 'contestation',
            # CMA déjà payée (Dossier Synchronisé, VALIDE CMA, Convoc CMA reçue, Refusé CMA)
            # Note: Refusé CMA = CAB a payé 241€ puis la CMA a refusé des documents
            'cma_already_paid': evalbox in PAID_STATUSES,
            # Clôture passée ou non (pour DEMANDE_ANNULATION avec CMA payée)
            # Refusé CMA a son propre flag (repositionné auto, pas de remboursement à mentionner)
            'cma_paid_cloture_open': evalbox in PAID_EXCLUDING_REFUSED and not context.get('cloture_passed', False),
            'cma_paid_cloture_passed': evalbox in PAID_EXCLUDING_REFUSED and context.get('cloture_passed', False),
            # Refusé CMA = 241€ engagés, candidat repositionné sur prochaine date, peut encore décaler
            'cma_refused_repositioned': evalbox == 'Refusé CMA',

            # Préoccupation éligibilité (détectée par triage - toute intention)
            'eligibility_concern': intent_context.get('eligibility_concern', False),

            # Permis probatoire (pour PERMIS_PROBATOIRE)
            'probation_completed': intent_context.get('probation_status') == 'completed',
            'probation_eligible_for_exam': intent_context.get('probation_status') == 'eligible',
            'probation_pending': intent_context.get('probation_status') == 'pending',

            # ===== THREAD MEMORY (mémoire persistante) =====
            **self._extract_thread_memory_flags(context),

            # ===== MATCHING DATES SPÉCIFIQUES (DEMANDE_CHANGEMENT_SESSION) =====
            # Variables pour le template intelligent qui gère les demandes avec dates précises
            'has_date_range_request': context.get('has_date_range_request', False),
            'requested_dates_raw': context.get('requested_dates_raw', ''),
            'is_exact_match': context.get('is_exact_match', False),
            'is_overlap_match': context.get('is_overlap_match', False),
            'is_no_match': context.get('is_no_match', False),
            **self._filter_closest_sessions_by_exam(context),
            # Dates de la session choisie (pour compatibilité)
            'session_date_debut': self._format_date(enriched_lookups.get('session_date_debut', '')),
            'session_date_fin': self._format_date(enriched_lookups.get('session_date_fin', '')),
            # Flag pour indiquer si les dates de session sont passées (pour éviter affichage obsolète)
            'session_dates_passed': self._is_date_passed(enriched_lookups.get('session_date_fin', '')) if enriched_lookups.get('session_date_fin') else False,

            # ===== PLAINTE SESSION (erreur CAB) =====
            # Variables pour gérer les réclamations d'erreur d'inscription
            'is_complaint': context.get('is_complaint', False),
            'is_cab_error': context.get('is_cab_error', False),
            'complaint_error_type': context.get('complaint_error_type', ''),
            'complaint_verification': context.get('complaint_verification', ''),
            'corrected_session': self._format_session_for_template(context.get('corrected_session')),
            'complaint_alternatives': [self._format_session_for_template(s) for s in context.get('complaint_alternatives', [])],
            'has_complaint_alternatives': len(context.get('complaint_alternatives', [])) > 0,
            'assigned_session_info': context.get('assigned_session_info', {}),
            'claimed_session_info': context.get('claimed_session_info', {}),
            # Toutes les sessions (jour + soir) quand le candidat a des contraintes sur les deux types
            'has_all_sessions': context.get('has_all_sessions', False),
            'all_sessions_jour': [self._format_session_for_template(s) for s in context.get('all_sessions_jour', [])],
            'all_sessions_soir': [self._format_session_for_template(s) for s in context.get('all_sessions_soir', [])],

            # ===== FORMATION MANQUÉE (repositionnement) =====
            # Variables pour gérer le cas où le candidat a manqué sa formation
            'formation_manquee': context.get('training_exam_consistency_data', {}).get('has_consistency_issue', False),
            'formation_manquee_needs_refresh': context.get('training_exam_consistency_data', {}).get('needs_refresh_session', False),
            'session_manquee_dates': self._format_session_dates_from_name(enriched_lookups.get('session_name', '')),

            # ===== FORMATION MANQUÉE + FORCE MAJEURE (FM-1) =====
            'missed_training_force_majeure': context.get('missed_training_force_majeure', False),

            # ===== CONFIRMATION DATE (variables pour confirmation_date_examen.html) =====
            'confirmed_exam_date_valid': context.get('confirmed_exam_date_valid', False),
            'confirmed_exam_date_unavailable': context.get('confirmed_exam_date_unavailable', False),
            'available_exam_dates_for_dept': context.get('available_exam_dates_for_dept', []),

            # ===== CONFIRMATION SESSION (variables pour confirmation_session.html) =====
            'no_sessions_of_requested_type': context.get('no_sessions_of_requested_type', False),
            'alternative_type_label': context.get('alternative_type_label', ''),

            # ===== CONVOCATION (variables pour demande_convocation.html) =====
            'deadline_missed': context.get('deadline_missed', False),
            'examen_probablement_passe': context.get('examen_probablement_passe', False),

            # Flags pour le template master (architecture modulaire)
            # Sections à afficher (peuvent être désactivées via context_flags de la matrice)
            'show_statut_section': context.get('show_statut_section', True),  # Par défaut True, sauf si désactivé
            # NOTE: show_dates_section et show_sessions_section sont calculés après
            # car ils dépendent de l'intention (CONFIRMATION_SESSION gère ses propres dates/sessions)
            'show_dates_section': False,  # Sera mis à jour ci-dessous
            'show_sessions_section': False,  # Sera mis à jour ci-dessous

            # Actions requises (déterminées par l'état)
            **self._determine_required_actions(context, evalbox),
        }

        # Rule 11: context_flags de la matrice priment sur les calculs dynamiques
        for override_key in ('has_required_action', 'suppress_elearning'):
            if override_key in context:
                result[override_key] = context[override_key]

        # Récupérer l'intention principale
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')

        # report_possible/bloque sont gérés par Section 0 (partials/report/*.html)
        # Ces partials affichent déjà les dates+sessions, donc on désactive Section 4
        report_possible = result.get('report_possible', False)
        report_bloque = result.get('report_bloque', False)
        report_force_majeure = result.get('report_force_majeure', False)
        deadline_passed_auto = result.get('deadline_passed_auto_reschedule', False)
        is_report_intention = report_possible or report_bloque or report_force_majeure or deadline_passed_auto

        # FIX: Section 0 override pour éviter duplication
        # Si report_possible/bloque/force_majeure est actif, désactiver intention_report_date
        # car partials/report/*.html gère déjà l'intention REPORT_DATE
        # NOTE: deadline_passed_auto_reschedule garde intention_report_date=True car le partial l'utilise
        if report_possible or report_bloque or report_force_majeure:
            result['intention_report_date'] = False

        # V3: Si date confirmée → le bloc report_possible V3 gère tout,
        # désactiver les intentions liées aux dates pour éviter duplication
        v3_confirmed = result.get('tm_candidate_confirmed_date', '')
        if v3_confirmed:
            result['intention_demande_date_plus_tot'] = False
            result['intention_demande_date'] = False
            result['intention_report_date'] = False
            result['intention_question_processus'] = False
            logger.info(f"📚 V3: intentions date supprimées (date confirmée {v3_confirmed})")

        # UBER CAS D/E: Candidat non éligible ou compte non vérifié
        # → Supprimer TOUTES les sections (dates, sessions, statut, e-learning, identifiants, credentials)
        # Le partial uber gère tout + propose alternatives CPF/rappel conseiller
        # Rule 11 intentional override: terminal state, returns early
        is_uber_blocked = result.get('uber_cas_d', False) or result.get('uber_cas_e', False)
        if is_uber_blocked:
            result['show_dates_section'] = False
            result['show_sessions_section'] = False
            result['show_statut_section'] = False
            result['show_confirmation_section'] = False
            result['show_convocation_section'] = False
            result['show_paiement_section'] = False
            # Supprimer e-learning (contrôlé par suppress_elearning dans response_master)
            result['suppress_elearning'] = True
            # Supprimer credentials (Section 0 bloc)
            result['credentials_invalid'] = False
            result['credentials_inconnus'] = False
            # Supprimer les intentions qui ajouteraient du contenu redondant
            # Préserver le fait qu'une annulation a été demandée (pour le partial CAS E)
            is_annulation = result.get('intention_demande_annulation', False)
            for key in list(result.keys()):
                if key.startswith('intention_'):
                    result[key] = False
            # Flag dédié pour que le partial CAS E affiche le remboursement
            if is_annulation:
                result['uber_remboursement_accepte'] = True
            logger.info("🚫 Uber CAS D/E: toutes les sections supprimées (seul le bloc Uber + alternatives s'affiche)")
            return result

        # Calculer show_dates_section (CENTRALISÉ - Section 4)
        # Afficher les dates si:
        # - Pas de date d'examen assignée
        # - ET il y a des dates disponibles
        # - ET on n'est pas dans un cas de report (géré par Section 0)
        # - Le candidat n'a PAS déjà confirmé (date OU session)
        # - PAS de suppression explicite (DEMANDE_DATE_PLUS_TOT sans options)
        # ================================================================
        # RÈGLE 11 : MATRICE = SOURCE DE VÉRITÉ
        # Si la matrice a défini un flag, NE PAS le recalculer
        # ================================================================

        # show_dates_section - MATRICE A TOUJOURS PRIORITÉ
        # ================================================================
        # RÈGLE 11 : Si la matrice définit explicitement un flag, le respecter
        # ================================================================
        date_case = context.get('date_case')
        if v3_confirmed:
            # Rule 11 intentional override: V3 confirmed date → report_possible gère tout
            result['show_dates_section'] = False
            result['show_sessions_section'] = False
            logger.info(f"📅 show_dates_section=False, show_sessions_section=False (V3 date confirmée {v3_confirmed})")
        elif 'show_dates_section' in context:
            # La matrice a explicitement défini ce flag → PRIORITÉ ABSOLUE
            result['show_dates_section'] = context['show_dates_section']
            logger.info(f"📅 show_dates_section={context['show_dates_section']} (défini par matrice - priorité absolue)")
        elif date_case == 2:
            # CAS SPÉCIAL: date passée + non validé → proposer nouvelles dates
            # SAUF si auto-report a déjà sélectionné une date ET session déjà assignée
            session_exists_for_auto_report = bool(enriched_lookups.get('session_name')) or bool(deal_data.get('Session'))
            if context.get('auto_report') and session_exists_for_auto_report:
                result['show_dates_section'] = False
                logger.info("📅 show_dates_section=False (CAS 2 + auto_report + session existante → pas besoin de confirmation)")
            else:
                result['show_dates_section'] = bool(context.get('next_dates', []))
                logger.info(f"📅 show_dates_section={result['show_dates_section']} (CAS 2: date passée non validée)")
        elif context.get('suppress_next_dates'):
            result['show_dates_section'] = False
            logger.info("📅 show_dates_section=False (suppress_next_dates)")
        elif context.get('intention_confirmation_date') or context.get('intention_confirmation_session'):
            result['show_dates_section'] = False
            logger.info("📅 show_dates_section=False (confirmation détectée)")
        elif not is_report_intention:
            result['show_dates_section'] = not date_examen and bool(context.get('next_dates', []))

        # Rule 11 intentional override: DEMANDE_ANNULATION show_dates_section dynamique
        # (CMA payment status unknown at matrix time)
        # - CMA payée + next_dates → True (proposer décalage comme alternative)
        # - Sinon → False (pas de dates à proposer pour une annulation simple)
        if context.get('primary_intent') == 'DEMANDE_ANNULATION':
            if result.get('cma_already_paid') and bool(context.get('next_dates', [])):
                result['show_dates_section'] = True
                logger.info("📅 show_dates_section=True (DEMANDE_ANNULATION + CMA payée → proposer décalage)")
            else:
                result['show_dates_section'] = False
                logger.info("📅 show_dates_section=False (DEMANDE_ANNULATION sans CMA payée)")

        # show_sessions_section — runtime overrides before matrix guard
        # Rule 11 intentional overrides: These runtime states (session_assignment_error,
        # session_confirmed, confirmation+session_exists) MUST take priority because
        # the matrix cannot express these dynamic conditions
        session_already_exists = bool(enriched_lookups.get('session_name')) or bool(deal_data.get('Session'))
        is_confirmation_intent = context.get('intention_confirmation_session') or context.get('primary_intent') == 'CONFIRMATION_SESSION'
        has_session_assignment_error = context.get('session_assignment_error', False)

        if has_session_assignment_error and is_confirmation_intent:
            # Le candidat confirme une nouvelle session → l'erreur est résolue
            # Pas besoin de s'excuser ni de re-proposer des sessions
            result['session_assignment_error'] = False
            result['show_sessions_section'] = False
            logger.info("📚 session_assignment_error + CONFIRMATION_SESSION → erreur résolue, sessions supprimées")
        elif has_session_assignment_error:
            # Cas spécial: session erronée
            if context.get('session_year_error_corrected'):
                # Session auto-corrigée → PAS besoin de proposer des alternatives
                result['show_sessions_section'] = False
                logger.info("📚 show_sessions_section=False (session_year_error_corrected → confirmation directe)")
            else:
                # Erreur mais pas de correction auto → proposer les bonnes sessions
                has_sessions = bool(self._flatten_session_options_filtered(context))
                result['show_sessions_section'] = has_sessions
                logger.info(f"📚 show_sessions_section={has_sessions} (session_assignment_error → proposer corrections)")
        elif context.get('session_confirmed'):
            result['show_sessions_section'] = False
            logger.info("📚 show_sessions_section=False (session confirmée → priorité absolue)")
        elif is_confirmation_intent and session_already_exists:
            result['show_sessions_section'] = False
            logger.info("📚 show_sessions_section=False (CONFIRMATION_SESSION + session déjà assignée)")
        elif 'show_sessions_section' in context:
            # La matrice a explicitement défini ce flag → le respecter
            result['show_sessions_section'] = context['show_sessions_section']
            logger.info(f"📚 show_sessions_section={context['show_sessions_section']} (défini par matrice)")
        else:
            # Calcul dynamique (comportement par défaut)
            primary_intent_for_sessions = context.get('primary_intent') or context.get('detected_intent', '')
            is_early_date_intent = primary_intent_for_sessions == 'DEMANDE_DATE_PLUS_TOT'
            sessions_already_proposed = context.get('sessions_proposed_recently', False)

            if context.get('suppress_next_dates'):
                result['show_sessions_section'] = False
                logger.info("📚 show_sessions_section=False (suppress_next_dates)")
            elif is_early_date_intent and sessions_already_proposed:
                result['show_sessions_section'] = False
                logger.info("📚 show_sessions_section=False (sessions déjà proposées + DEMANDE_DATE_PLUS_TOT)")
            elif context.get('intention_confirmation_session'):
                result['show_sessions_section'] = False
                logger.info("📚 show_sessions_section=False (CONFIRMATION_SESSION)")
            elif context.get('session_confirmed'):
                result['show_sessions_section'] = False
                logger.info("📚 show_sessions_section=False (session déjà confirmée)")
            elif not is_report_intention:
                has_sessions = bool(self._flatten_session_options_filtered(context))
                has_proposed_options = bool(context.get('session_data', {}).get('proposed_options'))
                # Exception: formation manquée → proposer sessions même si une session existe (elle est passée)
                training_missed = context.get('training_exam_consistency_data', {}).get('has_consistency_issue', False)
                session_exists_but_can_override = training_missed and deal_data.get('Session')
                result['show_sessions_section'] = (
                    has_sessions and
                    (not deal_data.get('Session') or session_exists_but_can_override) and
                    (bool(date_examen) or has_proposed_options)
                )
                if session_exists_but_can_override:
                    logger.info("📚 show_sessions_section=True (formation manquée → proposer rafraîchissement)")

        # ================================================================
        # THREAD MEMORY: Suppression sections déjà communiquées
        # RÈGLE: Ne s'applique QUE si la matrice n'a PAS défini le flag (Rule 11)
        # EXCEPTION: Si l'intention n'est PAS liée à la section, ThreadMemory
        #   peut supprimer même si la matrice a forcé le flag (éviter répétition)
        # EXCEPTION 2: QUESTION_GENERALE / ENVOIE_IDENTIFIANTS = le candidat pose une
        #   question ou envoie ses accès → lui donner un point complet sur son dossier.
        #   Ne PAS supprimer les sections.
        # ================================================================
        thread_mem = context.get('thread_memory', {})
        if thread_mem.get('has_history'):
            primary_intent = context.get('primary_intent') or context.get('detected_intent', '')
            # QUESTION_GENERALE / ENVOIE_IDENTIFIANTS: bypass toutes les suppressions ThreadMemory
            # Si le candidat pose une question ou envoie ses identifiants, il veut un retour complet
            if primary_intent in FULL_RECAP_INTENTS:
                logger.info(f"🔓 {primary_intent}: bypass ThreadMemory suppressions (point complet dossier)")
            else:
                # STATUT_INTENTS / DATES_INTENTS: intents that REQUIRE a section (never suppress)

                if thread_mem.get('suppress_dates') and result.get('show_dates_section'):
                    if 'show_dates_section' not in context or primary_intent not in DATES_INTENTS:
                        result['show_dates_section'] = False
                        logger.info("📅 show_dates_section=False (ThreadMemory: déjà communiqué, pas de changement)")
                if thread_mem.get('suppress_sessions') and 'show_sessions_section' not in context and result.get('show_sessions_section'):
                    result['show_sessions_section'] = False
                    logger.info("📚 show_sessions_section=False (ThreadMemory: déjà communiqué, pas de changement)")
                if thread_mem.get('suppress_statut') and result.get('show_statut_section'):
                    if 'show_statut_section' not in context or primary_intent not in STATUT_INTENTS:
                        result['show_statut_section'] = False
                        logger.info("📋 show_statut_section=False (ThreadMemory: déjà communiqué, pas de changement)")
                if thread_mem.get('suppress_elearning'):
                    result['suppress_elearning'] = True
                    logger.info("📖 suppress_elearning=True (ThreadMemory: déjà communiqué)")

        # ================================================================
        # CONVERSATION INTELLIGENCE V3: Response mode → section visibility
        # Respects Rule 11: matrix flags (context) always take priority
        # EXCEPTION: QUESTION_GENERALE / ENVOIE_IDENTIFIANTS bypass V3 suppressions
        # ================================================================
        v3_mode = context.get('conversation_state', {})
        if isinstance(v3_mode, dict):
            v3_response_mode = v3_mode.get('response_mode', '')
        elif hasattr(v3_mode, 'response_mode'):
            v3_response_mode = v3_mode.response_mode or ''
        else:
            v3_response_mode = ''

        primary_intent_v3 = context.get('primary_intent') or context.get('detected_intent', '')
        _bypass_v3 = primary_intent_v3 in FULL_RECAP_INTENTS or context.get('session_after_exam', False)
        if _bypass_v3:
            logger.info(f"🔓 bypass V3 response_mode suppressions (intent={primary_intent_v3}, session_after_exam={context.get('session_after_exam', False)})")
        elif v3_response_mode == 'brief_confirmation':
            if 'show_dates_section' not in context:  # Rule 11
                result['show_dates_section'] = False
            if 'show_sessions_section' not in context:
                result['show_sessions_section'] = False
            logger.info(f"📝 V3 brief_confirmation: dates/sessions suppressed (unless matrix override)")
        elif v3_response_mode == 'status_update':
            if 'show_dates_section' not in context:
                result['show_dates_section'] = False
            if 'show_sessions_section' not in context:
                result['show_sessions_section'] = False
            if 'show_statut_section' not in context:
                result['show_statut_section'] = True
            logger.info(f"📝 V3 status_update: statut forced, dates/sessions suppressed (unless matrix override)")

        # ================================================================
        # DÉTECTION FALLBACK SESSION: préférence jour/soir non disponible
        # Si le candidat a demandé un type mais que seul l'autre type est proposé
        # ================================================================
        session_pref = self._get_session_preference(context)
        sessions_proposees = result.get('sessions_proposees', [])
        if session_pref and sessions_proposees:
            pref_types = {s.get('type') for s in sessions_proposees if s.get('type')}
            if session_pref not in pref_types:
                # Fallback: les sessions proposées sont de l'autre type
                result['session_preference_no_match'] = True
                alt_type = 'soir' if session_pref == 'jour' else 'jour'
                result['session_preference_alt_type'] = alt_type
                result['session_preference_requested_text'] = 'cours du jour' if session_pref == 'jour' else 'cours du soir'
                result['preference_horaire_text'] = ''  # Effacer pour ne pas afficher "(cours du jour)" dans le header
                logger.info(f"🔄 Session preference fallback: '{session_pref}' demandé, '{alt_type}' proposé comme alternative")

        # ================================================================
        # FAUX REFUSÉ CMA: Override evalbox flags si faux refus
        # ================================================================
        if context.get('faux_refus_cma'):
            result['evalbox_refus_cma'] = False
            result['evalbox_dossier_synchronise'] = True
            logger.info("📋 Faux Refusé CMA → evalbox flags overridés vers Dossier Synchronisé")

        # ================================================================
        # Flags checklist progression (pour QUESTION_PROCESSUS)
        # ================================================================
        evalbox = result.get('evalbox_status', '')
        # "dossier_constitue" = evalbox au moins à Dossier créé
        result['dossier_constitue'] = evalbox in DOSSIER_CONSTITUE
        # "paiement_effectue" = evalbox au moins à Dossier Synchronisé (CAB a payé les 241€)
        result['paiement_effectue'] = evalbox in PAID_STATUSES
        # "session_type_display" pour la checklist
        if result.get('session_is_soir'):
            result['session_type_display'] = 'Cours du soir'
        elif result.get('session_is_jour'):
            result['session_type_display'] = 'Cours du jour'
        else:
            result['session_type_display'] = ''

        # ================================================================
        # GARDE-FOU: Pièces refusées → toujours afficher statut + action corriger
        # Rule 11 intentional override: candidate MUST see rejected documents
        # regardless of matrix flags (safety net)
        # ================================================================
        if result.get('has_pieces_refusees') and result.get('pieces_refusees_details'):
            if not result.get('show_statut_section'):
                result['show_statut_section'] = True
                logger.info("📋 show_statut_section=True (FORCÉ: pièces refusées CMA)")
            if not result.get('action_corriger_documents'):
                result['action_corriger_documents'] = True
                result['has_required_action'] = True
                logger.info("📋 action_corriger_documents=True (FORCÉ: pièces refusées CMA)")

        # ================================================================
        # DOSSIER TERMINÉ: Supprimer sections pré-examen inutiles
        # ================================================================
        dossier_termine = context.get('dossier_termine', False)
        if dossier_termine:
            # Pas de nouvelles dates/sessions pour un dossier terminé
            if 'show_dates_section' not in context:  # Rule 11: matrice prime
                result['show_dates_section'] = False
            if 'show_sessions_section' not in context:
                result['show_sessions_section'] = False
            # Pas d'actions pré-examen
            result['has_required_action'] = False
            # Pas d'e-learning
            result['suppress_elearning'] = True
            result['examen_passe'] = True
            logger.info(f"📊 Dossier terminé: sections dates/sessions/actions/elearning supprimées")

            # Exception: NON ADMIS / NON ADMISSIBLE / ABSENT → peuvent vouloir se réinscrire
            reinscription_intents = {'DEMANDE_REINSCRIPTION', 'REPORT_DATE'}
            if context.get('primary_intent') in reinscription_intents:
                if 'show_dates_section' not in context:
                    result['show_dates_section'] = True
                logger.info(f"📊 Dossier terminé MAIS intent réinscription → dates réactivées")

        # ================================================================
        # AUTO-ASSIGNATION: Supprimer statut "en attente" (contradictoire)
        # Quand CAS 1 auto-assigne date+session, la confirmation couvre le statut.
        # "En attente de traitement" contredit "inscription confirmée".
        # ================================================================
        if result.get('auto_assigned') and result.get('no_evalbox_status'):
            if 'show_statut_section' not in context:  # Rule 11: matrice prime
                result['show_statut_section'] = False
            result['has_required_action'] = False
            result['action_choisir_date'] = False
            logger.info("📋 show_statut_section=False (auto-assignation → confirmation suffit, 'en attente' serait contradictoire)")

        return result

    # Mapping state → flag pour les états (utilisés par response_master.html)
    # Ces flags permettent aux templates d'afficher les sections appropriées
    STATE_FLAG_MAP = {
        # États Uber (BLOCKING mais peuvent devenir WARNING dans certains contextes)
        'UBER_DOCS_MISSING': 'uber_cas_a',
        'UBER_TEST_MISSING': 'uber_cas_b',
        'UBER_ACCOUNT_NOT_VERIFIED': 'uber_cas_d',
        'UBER_NOT_ELIGIBLE': 'uber_cas_e',
        'DUPLICATE_UBER': 'uber_doublon',
        'DUPLICATE_CLARIFICATION': 'uber_doublon_clarification',
        'DUPLICATE_RECOVERABLE': 'uber_doublon_recoverable',
        'UBER_PROSPECT': 'uber_prospect',

        # États Credentials
        'CREDENTIALS_INVALID': 'credentials_invalid',
        'CREDENTIALS_REFUSED_SECURITY': 'credentials_refused',

        # État blocage
        'DATE_MODIFICATION_BLOCKED': 'report_bloque',
        'PERSONAL_ACCOUNT_WARNING': 'personal_account_warning',

        # État cohérence
        'TRAINING_MISSED_EXAM_IMMINENT': 'training_missed_alert',
        'REFRESH_SESSION_AVAILABLE': 'refresh_session_available',

        # États date examen (pour génération conditionnelle)
        'EXAM_DATE_EMPTY': 'date_examen_vide',
        'CONVOCATION_RECEIVED': 'convocation_recue',
        'DOSSIER_SYNCHRONIZED': 'dossier_synchronise',
        'VALIDE_CMA_WAITING_CONVOC': 'valide_cma',
        'REFUSED_CMA': 'refus_cma',
        'READY_TO_PAY': 'pret_a_payer',
    }

    # Mapping intention → flag
    INTENTION_FLAG_MAP = {
        'STATUT_DOSSIER': 'intention_statut_dossier',
        'DEMANDE_DATE_EXAMEN': 'intention_demande_date',
        'DEMANDE_AUTRES_DATES': 'intention_demande_date',
        'DEMANDE_DATES_FUTURES': 'intention_demande_date',
        'CONFIRMATION_DATE_EXAMEN': 'intention_demande_date',
        'DEMANDE_IDENTIFIANTS': 'intention_demande_identifiants',
        'ENVOIE_IDENTIFIANTS': 'intention_demande_identifiants',
        'CONFIRMATION_SESSION': 'intention_confirmation_session',
        'QUESTION_SESSION': 'intention_question_session',
        'DEMANDE_CONVOCATION': 'intention_demande_convocation',
        'DEMANDE_ELEARNING_ACCESS': 'intention_demande_elearning',
        'DEMANDE_DATE_VISIO': 'intention_demande_date_visio',
        'DEMANDE_LIEN_VISIO': 'intention_demande_lien_visio',
        'DEMANDE_CHANGEMENT_SESSION': 'intention_demande_changement_session',
        'REPORT_DATE': 'intention_report_date',
        'FORCE_MAJEURE_REPORT': 'intention_report_date',
        'DEMANDE_DATE_PLUS_TOT': 'intention_demande_date_plus_tot',
        'SIGNALE_PROBLEME_DOCS': 'intention_probleme_documents',
        'ENVOIE_DOCUMENTS': 'intention_probleme_documents',
        'PROBLEME_CONNEXION_EXAMT3P': 'intention_probleme_documents',  # Problème upload/connexion plateforme
        'QUESTION_PROCESSUS': 'intention_question_processus',
        'DEMANDE_AUTRES_DEPARTEMENTS': 'intention_autres_departements',
        # Intentions fréquentes
        'QUESTION_GENERALE': 'intention_question_generale',
        'DOCUMENT_QUESTION': 'intention_document_question',
        'RESULTAT_EXAMEN': 'intention_resultat_examen',
        'QUESTION_UBER': 'intention_question_uber',
        # Synonymes courants
        'DEMANDE_RESULTAT': 'intention_resultat_examen',
        'NOTE_EXAMEN': 'intention_resultat_examen',
        'UBER_ELIGIBILITE': 'intention_question_uber',
        'UBER_OFFRE': 'intention_question_uber',
        # Nouvelles intentions alignées (v2.2)
        'CONFIRMATION_PAIEMENT': 'intention_confirmation_paiement',
        'REFUS_PARTAGE_CREDENTIALS': 'intention_refus_credentials',
        'DEMANDE_EXCEPTION': 'intention_demande_exception',
        'ERREUR_PAIEMENT_CMA': 'intention_erreur_paiement_cma',
        'QUESTION_EXAMEN_PRATIQUE': 'intention_question_examen_pratique',
        'PERMIS_RENOUVELLEMENT': 'intention_permis_renouvellement',
        # Questions documents spécifiques
        'QUESTION_PERMIS_ETRANGER': 'intention_question_permis_etranger',
        'QUESTION_CARTE_SEJOUR': 'intention_question_carte_sejour',
        'QUESTION_HEBERGEMENT': 'intention_question_hebergement',
        'PERMIS_PROBATOIRE': 'intention_permis_probatoire',
        'RECLAMATION': 'intention_reclamation',
        'DEMANDE_ANNULATION': 'intention_demande_annulation',
        # Rétrocompat: ancien nom
        'DEMANDE_REMBOURSEMENT': 'intention_demande_annulation',
        # Intentions doublon
        'CONFIRMATION_DOUBLON': 'intention_confirmation_doublon',
        'REFUS_DOUBLON': 'intention_refus_doublon',
        # Synonymes résultats
        'ANNONCE_RESULTAT_POSITIF': 'intention_resultat_examen',
        'ANNONCE_RESULTAT_NEGATIF': 'intention_resultat_examen',
        # Documents transmis (candidat envoie des PJ)
        'TRANSMET_DOCUMENTS': 'intention_probleme_documents',
        # Offre Uber
        'DEMANDE_INFOS_OFFRE': 'intention_question_uber',
        # Réinscription
        'DEMANDE_REINSCRIPTION': 'intention_demande_reinscription',
        # Communication / support
        'DEMANDE_APPEL_TEL': 'intention_demande_appel_tel',
        'SIGNALE_PAS_RECU_EMAIL': 'intention_signale_pas_recu_email',
        'PROBLEME_CONNEXION_ELEARNING': 'intention_probleme_connexion_elearning',
        # Autres demandes spécifiques
        'QUESTION_CARTE_VTC': 'intention_question_carte_vtc',
        'DEMANDE_CERTIFICAT_FORMATION': 'intention_demande_certificat',
        'DEMANDE_SUPPRESSION_DONNEES': 'intention_demande_suppression_donnees',
        # Date lointaine ExamT3P
        'DATE_LOINTAINE_EXAMT3P': 'intention_date_lointaine',
        # Meta (templates dédiés, flags pour cohérence)
        'REMERCIEMENT': 'intention_remerciement',
        'SALUTATION': 'intention_salutation',
        'MESSAGE_CONFUS': 'intention_message_confus',
    }

    def _auto_map_intention_flags(self, context: Dict[str, Any]) -> Dict[str, bool]:
        """
        Auto-génère les flags intention_* depuis primary_intent ET secondary_intents.

        Convention: primary_intent est le standard, detected_intent est conservé pour rétrocompat.

        Cela évite de créer ~200 entrées manuelles dans la matrice STATE×INTENTION.
        Le template master (response_master.html) utilise ces flags pour afficher
        la section appropriée selon l'intention du candidat.

        Priorité: context_flags de la matrice > auto-mapping
        Si un flag est déjà défini dans le contexte (via matrice), il est conservé.
        """
        # Initialiser tous les flags à False
        flags = {
            'intention_statut_dossier': False,
            'intention_demande_date': False,
            'intention_confirmation_session': False,
            'intention_question_session': False,
            'intention_demande_identifiants': False,
            'intention_demande_convocation': False,
            'intention_demande_elearning': False,
            'intention_demande_date_visio': False,
            'intention_demande_lien_visio': False,
            'intention_report_date': False,
            'intention_demande_date_plus_tot': False,
            'intention_probleme_documents': False,
            'intention_question_processus': False,
            'intention_autres_departements': False,
            # Intentions fréquentes
            'intention_question_generale': False,
            'intention_resultat_examen': False,
            'intention_question_uber': False,
            # Nouvelles intentions alignées (v2.2)
            'intention_confirmation_paiement': False,
            'intention_refus_credentials': False,
            'intention_question_examen_pratique': False,
            'intention_demande_exception': False,
            'intention_erreur_paiement_cma': False,
            'intention_permis_renouvellement': False,
            'intention_permis_probatoire': False,
            # Intentions doublon
            'intention_confirmation_doublon': False,
            'intention_refus_doublon': False,
            'intention_demande_annulation': False,
            # Intentions ajoutées (complétude FLAG_MAP)
            'intention_demande_reinscription': False,
            'intention_demande_appel_tel': False,
            'intention_signale_pas_recu_email': False,
            'intention_probleme_connexion_elearning': False,
            'intention_question_carte_vtc': False,
            'intention_demande_certificat': False,
            'intention_demande_suppression_donnees': False,
            'intention_remerciement': False,
            'intention_salutation': False,
            'intention_message_confus': False,
            # Flags utilisés par response_master.html (étaient injectés
            # uniquement via matrix context_flags, maintenant aussi auto-mappés)
            'intention_demande_changement_session': False,
            'intention_document_question': False,
            'intention_question_carte_sejour': False,
            'intention_question_hebergement': False,
            'intention_question_permis_etranger': False,
            'intention_reclamation': False,
            'intention_date_lointaine': False,
        }

        # Récupérer l'intention principale (rétrocompatibilité + nouveau format)
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')

        # Flags Section 0 qui couvrent déjà certaines intentions
        # Si ces flags sont actifs, ne pas auto-mapper l'intention correspondante pour éviter la duplication
        section0_overrides = {
            'intention_report_date': ['report_possible', 'report_bloque', 'report_force_majeure'],
            'intention_resultat_examen': ['resultat_admis', 'resultat_non_admis', 'resultat_non_admissible', 'resultat_admissible', 'resultat_absent', 'resultat_convoc_pas_recu', 'resultat_plus_interesse'],
            'intention_demande_identifiants': ['credentials_invalid', 'credentials_inconnus'],
        }

        # Auto-mapper l'intention principale
        if primary_intent in self.INTENTION_FLAG_MAP:
            flag_name = self.INTENTION_FLAG_MAP[primary_intent]
            # Vérifier si un flag Section 0 couvre déjà cette intention
            skip_mapping = False
            if flag_name in section0_overrides:
                for section0_flag in section0_overrides[flag_name]:
                    if context.get(section0_flag):
                        skip_mapping = True
                        logger.debug(f"Skipping auto-map {flag_name} - covered by Section 0 flag {section0_flag}")
                        break
            if not skip_mapping:
                flags[flag_name] = True
                logger.debug(f"Auto-mapped primary_intent {primary_intent} -> {flag_name}")

        # Auto-mapper les intentions secondaires (avec vérification Section 0)
        secondary_intents = context.get('secondary_intents', [])
        for intent in secondary_intents:
            if intent in self.INTENTION_FLAG_MAP:
                flag_name = self.INTENTION_FLAG_MAP[intent]
                # Vérifier si un flag Section 0 couvre déjà cette intention secondaire
                skip_mapping = False
                if flag_name in section0_overrides:
                    for section0_flag in section0_overrides[flag_name]:
                        if context.get(section0_flag):
                            skip_mapping = True
                            logger.debug(f"Skipping secondary_intent {intent} - covered by Section 0 flag {section0_flag}")
                            break
                if not skip_mapping:
                    flags[flag_name] = True
                    logger.debug(f"Auto-mapped secondary_intent {intent} -> {flag_name}")

        # Priorité aux flags déjà définis dans le contexte (via matrice)
        # SAUF si un flag Section 0 couvre déjà cette intention (éviter duplication)
        for flag_name in flags:
            if context.get(flag_name) is True:
                # Vérifier section0_overrides avant de forcer True
                if flag_name in section0_overrides:
                    covered = any(context.get(s0f) for s0f in section0_overrides[flag_name])
                    if covered:
                        continue  # Ne pas forcer True, Section 0 couvre déjà
                flags[flag_name] = True

        return flags

    def _log_and_return_intention_flags(self, context: Dict[str, Any]) -> Dict[str, bool]:
        """Wrapper pour _auto_map_intention_flags avec logging de debug."""
        flags = self._auto_map_intention_flags(context)
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')
        active_flags = [k for k, v in flags.items() if v]
        if active_flags:
            logger.info(f"  🎯 Intention flags: {active_flags} (primary_intent={primary_intent})")
        return flags

    def _map_warning_state_flags(self, warning_states: List[DetectedState]) -> Dict[str, bool]:
        """
        Génère les flags pour les états WARNING.

        Ces flags sont utilisés par response_master.html pour afficher
        les alertes appropriées dans la réponse.
        """
        flags = {}
        for state in warning_states:
            state_flag = self.STATE_FLAG_MAP.get(state.name)
            if state_flag:
                flags[state_flag] = True
                logger.debug(f"Mapped WARNING state {state.name} -> {state_flag}")
        return flags

    def _determine_required_actions(self, context: Dict[str, Any], evalbox: str) -> Dict[str, bool]:
        """Détermine les actions requises selon l'état du candidat."""
        actions = {
            'has_required_action': False,
            'action_passer_test': False,
            'action_envoyer_documents': False,
            'action_completer_dossier': False,
            'action_choisir_date': False,
            'action_choisir_session': False,
            'action_surveiller_paiement': False,
            'action_attendre_convocation': False,
            'action_preparer_examen': False,
            'action_corriger_documents': False,
            'action_contacter_uber': False,
        }

        # Déterminer l'état Uber - UTILISER uber_case (source de vérité) si disponible
        is_uber_20 = context.get('is_uber_20_deal', False)
        uber_case = context.get('uber_case', '')

        # Faux Refusé CMA: traiter comme Dossier Synchronisé pour les actions
        if context.get('faux_refus_cma') and evalbox == 'Refusé CMA':
            evalbox = 'Dossier Synchronisé'
            logger.info("📋 Faux Refusé CMA → evalbox overridé à 'Dossier Synchronisé' pour actions")

        # EXAM_INCLUS = Oui → CAB gère (compte, docs, paiement)
        deal_data = context.get('deal_data', {})
        cab_paye_examen = deal_data.get('EXAM_INCLUS', '') == 'Oui'

        # États bloquants Uber - Utiliser uber_case pour éviter les incohérences
        # uber_case est déterminé par StateDetector qui gère la logique J+1 et verification pending
        if is_uber_20 and uber_case:
            if uber_case == 'A':
                # CAS A: Documents non envoyés
                actions['action_envoyer_documents'] = True
                actions['has_required_action'] = True
                return actions
            if uber_case == 'B':
                # CAS B: Test non passé
                actions['action_passer_test'] = True
                actions['has_required_action'] = True
                return actions
            if uber_case == 'D':
                # CAS D: Compte Uber non vérifié
                actions['action_contacter_uber'] = True
                actions['has_required_action'] = True
                return actions
            if uber_case == 'E':
                # CAS E: Non éligible
                actions['action_contacter_uber'] = True
                actions['has_required_action'] = True
                return actions
            # ELIGIBLE, PROSPECT, NOT_UBER: pas d'action bloquante Uber
            # Continuer vers la logique Evalbox ci-dessous
        elif is_uber_20:
            # Fallback si uber_case pas défini (rétrocompatibilité)
            date_dossier_recu = context.get('date_dossier_recu')
            date_test_selection = context.get('date_test_selection')
            compte_uber = context.get('compte_uber', True)
            eligible_uber = context.get('eligible_uber', True)

            if not date_dossier_recu:
                actions['action_envoyer_documents'] = True
                actions['has_required_action'] = True
                return actions
            if not date_test_selection:
                actions['action_passer_test'] = True
                actions['has_required_action'] = True
                return actions
            if not compte_uber:
                actions['action_contacter_uber'] = True
                actions['has_required_action'] = True
                return actions
            if not eligible_uber:
                actions['action_contacter_uber'] = True
                actions['has_required_action'] = True
                return actions

        # Actions selon Evalbox
        # Si EXAM_INCLUS = Non (candidat gère lui-même), il doit :
        # 1. Créer son compte ExamT3P, 2. Upload docs, 3. Payer, 4. Nous informer
        if not cab_paye_examen and evalbox in [
            '', None, 'N/A', 'None',  # Pas de compte
            'Dossier crée',            # Compte créé, docs en cours
            'Documents refusés',       # Docs refusés
            'Documents manquants',     # Docs incomplets
            'Refusé CMA'               # Refus CMA
        ]:
            actions['action_completer_dossier'] = True
            actions['has_required_action'] = True
        elif evalbox == 'Dossier Synchronisé':
            # Uber ELIGIBLE = CAB a déjà payé, pas de lien de paiement à surveiller
            if not cab_paye_examen:
                actions['action_surveiller_paiement'] = True
                actions['has_required_action'] = True
        elif evalbox in READY_TO_PAY:
            # Uber ELIGIBLE = CAB a déjà payé, pas de paiement attendu
            if not cab_paye_examen:
                actions['action_surveiller_paiement'] = True
                actions['has_required_action'] = True
        elif evalbox == 'VALIDE CMA':
            actions['action_attendre_convocation'] = True
            actions['has_required_action'] = True
        elif evalbox == 'Convoc CMA reçue':
            # Ne pas afficher "préparer examen" si l'examen est déjà passé
            if not context.get('date_examen_passed', False) and not context.get('examen_passe', False):
                actions['action_preparer_examen'] = True
                actions['has_required_action'] = True
        else:
            # Pas de statut Evalbox - vérifier si date/session manquantes
            date_examen = context.get('date_examen')
            session = context.get('deal_data', {}).get('Session')
            primary_intent = context.get('primary_intent') or context.get('detected_intent', '')
            session_preference = context.get('session_preference')
            has_next_dates = bool(context.get('next_dates', []))

            if not date_examen:
                # NOTE: Ne pas afficher action_choisir_date si Section 4 affiche les dates avec CTA
                # Section 4 affiche les dates quand: has_next_dates ET pas de report
                # On désactive action_choisir_date car le CTA est maintenant centralisé dans Section 4
                if not has_next_dates:
                    # Pas de dates disponibles - afficher l'action sans dates
                    actions['action_choisir_date'] = True
                    actions['has_required_action'] = True
                # Sinon: Section 4 gère l'affichage des dates + CTA
            elif not session:
                # Ne pas demander de choisir une session si:
                # 1. On connaît déjà la préférence (session_preference)
                # 2. C'est un REPORT_DATE (on attend d'abord la confirmation de la nouvelle date)
                # 3. Section 4 affiche les sessions avec CTA (éviter duplication)
                has_sessions = bool(self._flatten_session_options_filtered(context))
                if primary_intent == 'REPORT_DATE':
                    # REPORT_DATE: on propose les sessions APRÈS confirmation de la nouvelle date
                    pass  # Pas d'action requise ici
                elif session_preference:
                    # On connaît la préférence, pas besoin de demander
                    pass  # La session sera proposée automatiquement dans Section 4
                elif has_sessions:
                    # Section 4 gère l'affichage des sessions + CTA
                    pass  # Pas d'action requise, Section 4 s'en charge
                else:
                    # Pas de sessions disponibles - afficher l'action
                    actions['action_choisir_session'] = True
                    actions['has_required_action'] = True

        return actions

    def _format_next_dates_for_template(
        self,
        dates: List[Dict],
        session_data: Optional[Dict[str, Any]] = None,
        session_preference: Optional[str] = None
    ) -> List[Dict]:
        """
        Formate les next_dates pour utilisation dans les templates {{#each}}.

        Args:
            dates: Liste des dates d'examen
            session_data: Données de sessions (optionnel) pour enrichir avec les sessions
            session_preference: Préférence jour/soir pour filtrer (optionnel)
        """
        if not dates:
            return []

        # Filtrer les dates dont l'examen est passé
        from datetime import datetime
        today = datetime.now().date()
        filtered_dates = []
        for d in dates:
            exam_str = d.get('Date_Examen', '')
            if exam_str:
                exam_date = parse_date_flexible(exam_str)
                if exam_date is None:
                    filtered_dates.append(d)  # En cas d'erreur de parsing, garder la date
                elif exam_date >= today:
                    filtered_dates.append(d)
                else:
                    logger.debug(f"Date examen passée exclue: {exam_str}")
            else:
                filtered_dates.append(d)

        if not filtered_dates:
            logger.warning("Toutes les dates d'examen sont passées - aucune date à afficher")
            return []

        formatted = []
        seen_depts = set()

        # Construire un mapping date_examen → session depuis session_data
        session_by_exam_date = {}
        if session_data and 'proposed_options' in session_data:
            for option in session_data.get('proposed_options', []):
                exam_info = option.get('exam_info', {})
                exam_date = exam_info.get('Date_Examen', '')
                sessions = option.get('sessions', [])

                # Filtrer par préférence si spécifiée
                if session_preference and sessions:
                    filtered = [s for s in sessions if s.get('session_type') == session_preference]
                    if filtered:
                        sessions = filtered

                if sessions and exam_date:
                    # Prendre la première session correspondante
                    session = sessions[0]
                    session_type = session.get('session_type', '')
                    session_type_label = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else ''
                    session_by_exam_date[exam_date] = {
                        'session_name': session_type_label,
                        'session_debut': self._format_date(session.get('Date_d_but', '')),
                        'session_fin': self._format_date(session.get('Date_fin', '')),
                        'session_type': session_type,
                    }
        # Fallback: sessions_proposees (flat structure from date_range matching)
        if not session_by_exam_date and session_data and session_data.get('sessions_proposees'):
            for s in session_data['sessions_proposees']:
                exam_date = s.get('date_examen', '') or s.get('Date_Examen', '')
                session_type = s.get('session_type', '') or s.get('type', '')
                date_debut = s.get('Date_d_but', '') or s.get('date_debut', '')
                date_fin = s.get('Date_fin', '') or s.get('date_fin', '')
                if exam_date and not session_by_exam_date.get(exam_date):
                    session_type_label = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else ''
                    # Filtrer par préférence si spécifiée
                    if session_preference and session_type != session_preference:
                        continue
                    session_by_exam_date[exam_date] = {
                        'session_name': session_type_label,
                        'session_debut': self._format_date(date_debut) if date_debut else '',
                        'session_fin': self._format_date(date_fin) if date_fin else '',
                        'session_type': session_type,
                    }

        for d in filtered_dates[:MAX_DATES_DISPLAYED]:  # Limiter à 5 dates (après filtrage des dates passées)
            date_str = d.get('Date_Examen', '')
            cloture_str = d.get('Date_Cloture_Inscription', '')
            dept = d.get('Departement', '')

            # Récupérer les infos session si disponibles
            session_info = session_by_exam_date.get(date_str, {})

            formatted.append({
                'date_examen_formatted': self._format_date(date_str) if date_str else '',
                'date_cloture_formatted': self._format_date(cloture_str) if cloture_str else '',
                'Departement': dept,
                'is_first_of_dept': dept not in seen_depts,
                # Conserver les champs originaux aussi
                'Date_Examen': date_str,
                'Date_Cloture_Inscription': cloture_str,
                # Session info (si disponible)
                'session_name': session_info.get('session_name', ''),
                'session_debut': session_info.get('session_debut', ''),
                'session_fin': session_info.get('session_fin', ''),
                'session_type': session_info.get('session_type', ''),
            })
            seen_depts.add(dept)

        return formatted

    def _prepare_cross_department_comparison(
        self,
        current_date: str,
        current_dept: str,
        alternative_dates: List[Dict],
        compte_existe: bool = False
    ) -> Dict[str, Any]:
        """
        Prepare une comparaison claire entre la date actuelle et les alternatives.

        Args:
            current_date: Date actuelle de l'examen (YYYY-MM-DD)
            current_dept: Departement actuel du candidat
            alternative_dates: Dates alternatives dans d'autres departements
            compte_existe: True si le candidat a deja un compte ExamT3P

        Returns:
            Dict avec has_earlier_options, earlier_options, days_could_save, etc.
        """
        if not current_date or not alternative_dates:
            return {
                'has_earlier_options': False,
                'earlier_options': [],
                'earliest_option': None,
                'days_could_save': 0,
            }

        current_date_obj = parse_date_flexible(current_date, 'current_date_cross_dept')
        if not current_date_obj:
            return {
                'has_earlier_options': False,
                'earlier_options': [],
                'earliest_option': None,
                'days_could_save': 0,
            }

        # Trouver les dates plus proches dans d'autres departements
        earlier_options = []
        for date_info in alternative_dates:
            alt_date_str = date_info.get('Date_Examen', '')
            alt_dept = date_info.get('Departement', '')

            if not alt_date_str or alt_dept == current_dept:
                continue

            alt_date_obj = parse_date_flexible(alt_date_str, 'alt_date_cross_dept')
            if not alt_date_obj:
                continue
            if alt_date_obj < current_date_obj:
                days_earlier = (current_date_obj - alt_date_obj).days
                earlier_options.append({
                    'date_examen_formatted': self._format_date(alt_date_str),
                    'Date_Examen': alt_date_str,
                    'dept': alt_dept,
                    'Departement': alt_dept,
                    'days_earlier': days_earlier,
                    'comparison_text': f"{days_earlier} jours plus tot",
                    'Date_Cloture_Inscription': date_info.get('Date_Cloture_Inscription', ''),
                    'date_cloture_formatted': self._format_date(date_info.get('Date_Cloture_Inscription', '')),
                })

        # Trier par date (plus proche en premier)
        earlier_options.sort(key=lambda x: x.get('Date_Examen', ''))

        # Limiter a 5 alternatives
        earlier_options = earlier_options[:MAX_DATES_DISPLAYED]

        return {
            'has_earlier_options': bool(earlier_options),
            'earlier_options': earlier_options,
            'earliest_option': earlier_options[0] if earlier_options else None,
            'days_could_save': earlier_options[0]['days_earlier'] if earlier_options else 0,
            'compte_existe': compte_existe,
            'cma_departement': current_dept,
        }

    def _extract_prenom_from_contact(self, contact_data: Dict[str, Any], deal_data: Dict[str, Any]) -> str:
        """Extrait le prénom du candidat depuis Contact (prioritaire) ou Deal_Name (fallback)."""
        # Priorité 1: First_Name du Contact
        first_name = contact_data.get('First_Name', '')
        if first_name and first_name.strip():
            return first_name.strip().capitalize()

        # Priorité 2: Extraire le prénom du Deal_Name (ex: "Thomas DUPONT" -> "Thomas")
        deal_name = deal_data.get('Deal_Name', '')
        if deal_name and ' ' in deal_name:
            # Prendre le premier mot qui n'est pas tout en majuscules
            parts = deal_name.split()
            for part in parts:
                if not part.isupper() and part.isalpha():
                    return part.capitalize()
            # Sinon prendre le premier mot
            return parts[0].capitalize()

        return ''

    def _detect_gender_from_name(self, prenom: str) -> str:
        """
        Détecte le genre à partir du prénom.

        Utilise gender-guesser pour deviner le genre.
        Prend le premier mot du prénom pour éviter les cas comme "Mohamed Amine".

        Returns:
            'female', 'male', ou 'unknown'
        """
        if not prenom:
            return 'unknown'

        # Prendre le premier mot du prénom
        first_word = prenom.split()[0].strip()
        if not first_word:
            return 'unknown'

        try:
            result = _gender_detector.get_gender(first_word)
            # gender-guesser retourne: male, female, mostly_male, mostly_female, andy, unknown
            if result in ['female', 'mostly_female']:
                return 'female'
            elif result in ['male', 'mostly_male']:
                return 'male'
            else:
                return 'unknown'
        except Exception:
            return 'unknown'

    def _check_date_changed(self, current_date: str, previous_date: str) -> bool:
        """
        Verifie si la date actuelle differe de la date precedemment communiquee.

        Args:
            current_date: Date actuelle formatee (DD/MM/YYYY)
            previous_date: Date precedemment communiquee (DD/MM/YYYY)

        Returns:
            True si les dates sont differentes et non vides
        """
        if not current_date or not previous_date:
            return False
        # Comparer directement (meme format DD/MM/YYYY)
        return current_date.strip() != previous_date.strip()

    def _format_date(self, date_str: str) -> str:
        """Formate une date en DD/MM/YYYY."""
        if not date_str:
            return ''
        from src.utils.date_utils import format_date_for_display
        return format_date_for_display(date_str) or str(date_str)

    def _is_date_passed(self, date_str: str) -> bool:
        """Vérifie si une date est passée (avant aujourd'hui)."""
        if not date_str:
            return False
        date_obj = parse_date_flexible(date_str)
        return date_obj < datetime.now().date() if date_obj else False

    def _compute_session_temporal_flags(self, session_start_str: str, session_end_str: str) -> Dict[str, Any]:
        """Calcule les flags temporels pour la session assignée (vs aujourd'hui).
        Intelligence générale disponible dans TOUS les templates."""
        result = {
            'session_upcoming': False,
            'session_in_progress': False,
            'session_finished': False,
            'session_starts_soon': False,
            'days_until_session_start': None,
        }
        if not session_start_str and not session_end_str:
            return result
        today = datetime.now().date()
        start_date = parse_date_flexible(session_start_str)
        end_date = parse_date_flexible(session_end_str)
        if start_date:
            days_until = (start_date - today).days
            result['days_until_session_start'] = days_until
            if start_date > today:
                result['session_upcoming'] = True
                if days_until <= SESSION_STARTS_SOON_DAYS:
                    result['session_starts_soon'] = True
            elif end_date and today > end_date:
                result['session_finished'] = True
            else:
                result['session_in_progress'] = True
        elif end_date:
            if today > end_date:
                result['session_finished'] = True
        return result

    def _get_next_exam_date_cas3(self, context: Dict) -> str:
        """Retourne la prochaine date d'examen formatée pour CAS 3 (Refusé CMA)."""
        if context.get('date_case') != 3:
            return ''
        next_dates = context.get('next_dates', [])
        if next_dates and len(next_dates) > 0:
            date_str = next_dates[0].get('Date_Examen', '')
            return self._format_date(date_str)
        return ''

    def _get_next_cloture_cas3(self, context: Dict) -> str:
        """Retourne la date de clôture de la prochaine session pour CAS 3 (Refusé CMA)."""
        if context.get('date_case') != 3:
            return ''
        next_dates = context.get('next_dates', [])
        if next_dates and len(next_dates) > 0:
            cloture = next_dates[0].get('Date_Cloture_Inscription', '')
            return self._format_date(cloture)
        return ''

    def _format_dates_list(self, dates: List[Dict]) -> str:
        """Formate une liste de dates d'examen en HTML."""
        if not dates:
            return "<p>Aucune date disponible pour le moment.</p>"

        lines = []
        for i, date_info in enumerate(dates[:MAX_DATES_DISPLAYED], 1):
            date_str = date_info.get('Date_Examen', '')
            formatted = self._format_date(date_str)
            dept = date_info.get('Departement', '')
            cloture = date_info.get('Date_Cloture_Inscription', '')
            cloture_formatted = self._format_date(cloture) if cloture else ''

            line = f"<li><b>{formatted}</b> (département {dept})"
            if cloture_formatted:
                line += f" - clôture : {cloture_formatted}"
            line += "</li>"
            lines.append(line)

        return f"<ul>{''.join(lines)}</ul>"

    def _clean_session_display_name(self, enriched_lookups: Dict[str, Any]) -> str:
        """
        Retourne un nom de session nettoyé pour affichage.
        Ex: "Cours du jour" ou "Cours du soir" au lieu du nom CRM brut
        qui contient des termes internes (CDJ, Montreuil, etc.).
        """
        session_name = enriched_lookups.get('session_name')
        if not session_name:
            return ''
        session_type = enriched_lookups.get('session_type', '')
        if session_type == 'jour':
            return 'Cours du jour'
        elif session_type == 'soir':
            return 'Cours du soir'
        # Fallback: essayer de déduire du nom brut
        name_lower = session_name.lower()
        if 'cdj' in name_lower or 'jour' in name_lower:
            return 'Cours du jour'
        elif 'cds' in name_lower or 'soir' in name_lower:
            return 'Cours du soir'
        return 'Session de formation'

    def _format_session(self, session: Any) -> str:
        """Formate les infos de session."""
        if not session:
            return ''
        if isinstance(session, dict):
            return session.get('name', '')
        return str(session)

    def _format_session_dates_from_name(self, session_name: str) -> str:
        """
        Extrait les dates d'un nom de session.
        Ex: "cds-montreuil- thu2 - 12 janvier - 23 janvier 2026" → "12 au 23 janvier 2026"
        """
        if not session_name:
            return ''
        import re
        # Pattern: "XX janvier/février/... - XX janvier/février/... 2026"
        date_pattern = r'(\d{1,2})\s*(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)(?:\s*-\s*|\s+au\s+)(\d{1,2})\s*(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s*(\d{4})'
        match = re.search(date_pattern, session_name, re.IGNORECASE)
        if match:
            day1, month1, day2, month2, year = match.groups()
            if month1.lower() == month2.lower():
                return f"{day1} au {day2} {month2} {year}"
            else:
                return f"{day1} {month1} au {day2} {month2} {year}"
        return ''

    def _compute_no_session_alternatives(self, context: Dict[str, Any]) -> bool:
        """
        Calcule no_session_alternatives en tenant compte du filtre par date d'examen.
        True si aucune session proposée ET aucune closest session valide (avant examen).
        """
        if bool(self._flatten_session_options_filtered(context)):
            return False

        filtered = self._filter_closest_sessions_by_exam(context)
        return all(not v for v in filtered.values())

    def _session_ends_before_exam(self, session: Optional[Dict[str, Any]], exam_date) -> bool:
        """Vérifie si une session se termine AVANT la date d'examen."""
        if not session or not exam_date:
            return True  # Pas de contrainte → garder
        end_str = session.get('Date_fin', '') or ''
        if not end_str:
            return True
        session_end = parse_date_flexible(end_str)
        return session_end < exam_date if session_end else True

    def _filter_closest_sessions_by_exam(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Filtre les closest_session_before/after :
        1. Par date d'examen (Rule 16) — sessions qui se terminent APRÈS l'examen
        2. Par date du jour — sessions déjà commencées (passées)
        3. Par session actuelle — ne pas re-proposer la session du candidat
        """
        keys = [
            'closest_session_before', 'closest_session_after',
            'closest_session_before_jour', 'closest_session_before_soir',
            'closest_session_after_jour', 'closest_session_after_soir',
        ]

        # Déterminer si le filtre s'applique
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')
        is_session_change = (
            primary_intent == 'DEMANDE_CHANGEMENT_SESSION'
            or 'DEMANDE_CHANGEMENT_SESSION' in context.get('secondary_intents', [])
        )

        enriched_lookups = context.get('enriched_lookups') or {}
        date_examen_raw = enriched_lookups.get('date_examen') or context.get('date_examen_raw', '')
        exam_date = None
        if is_session_change and date_examen_raw:
            exam_date = parse_date_flexible(date_examen_raw)

        # Session actuelle du candidat (pour ne pas la re-proposer)
        current_session_id = context.get('current_session_id') or (enriched_lookups.get('session_record') or {}).get('id', '')
        today = datetime.now().date()

        result = {}
        for key in keys:
            raw_session = context.get(key)
            if not raw_session:
                result[key] = None
                continue

            # Filtre 1: Session se termine après l'examen (Rule 16)
            if exam_date and not self._session_ends_before_exam(raw_session, exam_date):
                logger.info(f"📅 {key} filtré: session se termine après examen {exam_date}")
                result[key] = None
                continue

            # Filtre 2: Session déjà commencée (date_debut <= aujourd'hui)
            start_str = raw_session.get('Date_d_but', '') or raw_session.get('date_debut', '') or ''
            if start_str:
                start_date = parse_date_flexible(start_str)
                if start_date and start_date <= today:
                    logger.info(f"📅 {key} filtré: session déjà commencée ({start_str} <= {today})")
                    result[key] = None
                    continue

            # Filtre 3: C'est la session actuelle du candidat
            session_id = str(raw_session.get('id', ''))
            if current_session_id and session_id and session_id == str(current_session_id):
                logger.info(f"📅 {key} filtré: c'est la session actuelle du candidat")
                result[key] = None
                continue

            result[key] = self._format_session_for_template(raw_session)

        return result

    def _format_session_for_template(self, session: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """
        Formate une session (depuis match_sessions_by_date_range) pour le template.

        Input:
            {
                'Name': 'cdj-montreuil-...',
                'Date_d_but': '2026-02-16',
                'Date_fin': '2026-02-20',
                'session_type': 'jour',
                ...
            }

        Output:
            {
                'name': 'cdj-montreuil-...',
                'date_debut': '16/02/2026',
                'date_fin': '20/02/2026',
                'session_type': 'jour',
                'session_type_label': 'Cours du jour'
            }
        """
        if not session:
            return None

        session_type = session.get('session_type', '')
        return {
            'name': session.get('Name', ''),
            'date_debut': self._format_date(session.get('Date_d_but', '')),
            'date_fin': self._format_date(session.get('Date_fin', '')),
            'session_type': session_type,
            'session_type_label': 'Cours du soir' if session_type == 'soir' else 'Cours du jour'
        }

    def _flatten_session_options(self, session_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Transforme les proposed_options du legacy session_helper en format plat
        utilisable facilement par les templates Handlebars.

        Input (legacy format):
            proposed_options: [
                {
                    'exam_info': {'Date_Examen': '2026-03-31', 'Departement': '75', ...},
                    'sessions': [
                        {'Name': 'cds-janvier', 'Date_d_but': '...', 'Date_fin': '...', 'session_type': 'soir', ...}
                    ]
                }
            ]

        Output (template format):
            [
                {
                    'date_examen': '31/03/2026',
                    'departement': '75',
                    'cloture': '15/03/2026',
                    'nom': 'Cours du soir - Janvier 2026',
                    'debut': '15/01/2026',
                    'fin': '25/01/2026',
                    'type': 'soir',
                    'horaires': '18h-22h'
                }
            ]
        """
        flattened = []
        proposed_options = session_data.get('proposed_options', [])

        for option in proposed_options:
            exam_info = option.get('exam_info', {})
            sessions = option.get('sessions', [])

            # Formater les dates d'examen
            date_examen = exam_info.get('Date_Examen', '')
            date_examen_formatted = self._format_date(date_examen) if date_examen else ''
            cloture = exam_info.get('Date_Cloture_Inscription', '')
            cloture_formatted = self._format_date(cloture) if cloture else ''
            departement = exam_info.get('Departement', '')

            for session in sessions:
                session_type = session.get('session_type', '')
                session_type_label = session.get('session_type_label', '')

                # Extraire les dates de la session
                date_debut = session.get('Date_d_but', '')
                date_fin = session.get('Date_fin', '')

                # Filtrer les sessions dont la date de début est passée
                if date_debut:
                    today = datetime.now().date()
                    debut_date = parse_date_flexible(date_debut)
                    if debut_date and debut_date < today:
                        logger.debug(f"Session passée exclue: {session.get('Name', '')} (début: {date_debut})")
                        continue

                date_debut_formatted = self._format_date(date_debut) if date_debut else ''
                date_fin_formatted = self._format_date(date_fin) if date_fin else ''

                # Horaires fixes (ne pas utiliser les données CRM)
                # Cours du jour: 8h30-17h30 | Cours du soir: 18h-22h
                horaires = '8h30-17h30' if session_type == 'jour' else '18h-22h' if session_type == 'soir' else ''

                # Déterminer si c'est la première session de cette date d'examen
                is_first_of_exam = not any(
                    s.get('date_examen_raw') == date_examen for s in flattened
                )

                # Générer le label du type de session si pas déjà défini
                if not session_type_label and session_type:
                    session_type_label = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else ''

                flattened.append({
                    'date_examen': date_examen_formatted,
                    'date_examen_formatted': date_examen_formatted,
                    'date_examen_raw': date_examen,
                    'departement': departement,
                    'cloture': cloture_formatted,
                    'date_cloture_formatted': cloture_formatted,
                    'nom': session_type_label or session.get('Name', ''),
                    'session_type_label': session_type_label or ('Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else ''),
                    'session_name': session.get('Name', ''),
                    'session_id': session.get('id', ''),
                    'debut': date_debut_formatted,
                    'date_debut': date_debut_formatted,
                    'fin': date_fin_formatted,
                    'date_fin': date_fin_formatted,
                    'type': session_type,
                    'horaires': horaires,
                    'is_jour': session_type == 'jour',
                    'is_soir': session_type == 'soir',
                    'is_first_of_exam': is_first_of_exam,
                })

        return flattened

    def _get_session_preference(self, context: Dict[str, Any]) -> str:
        """
        Récupère la préférence de session (jour/soir).
        Priorité: intent_context (triage) > session_data (legacy)
        """
        # 1. Priorité: intent_context (détecté par le triage depuis le message client)
        intent_context = context.get('intent_context', {})
        if intent_context.get('session_preference'):
            return intent_context['session_preference']

        # 2. Fallback: session_data (legacy helper)
        session_data = context.get('session_data', {})
        if session_data.get('session_preference'):
            return session_data['session_preference']

        return ''

    def _generate_report_flags(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Génère les flags pour les demandes de report de date.
        Ces flags sont utilisés par Section 0 du response_master.
        """
        can_modify = context.get('can_modify_exam_date', True)
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')
        intent_context = context.get('intent_context', {})
        mentions_force_majeure = intent_context.get('mentions_force_majeure', False)

        # CAS SPECIAL: deadline_passed_reschedule (CAS 8)
        # Le candidat n'a pas demandé de report, la deadline est juste passée avant paiement
        # → Pas besoin d'action du candidat, report automatique lors du paiement
        deadline_passed_auto_reschedule = context.get('deadline_passed_reschedule', False)

        report_bloque = False
        report_possible = False
        report_force_majeure = False

        # Repositionnement implicite: le candidat demande une formation après sa date d'examen
        implicit_repositioning = context.get('implicit_date_repositioning', False)
        engagement_can_reposition = context.get('engagement_level', {}).get('can_reposition', False)

        # V3: Si le candidat a déjà confirmé une date, forcer report_possible
        # indépendamment de l'intention détectée (triage non-déterministe)
        v3_confirmed_date = context.get('tm_candidate_confirmed_date', '')
        if v3_confirmed_date and not deadline_passed_auto_reschedule:
            report_possible = True
            logger.info(f"📚 V3: report_possible=True (date confirmée {v3_confirmed_date})")

        # Seulement si l'intention est REPORT_DATE ET PAS de deadline_passed_reschedule
        # (car deadline_passed_reschedule a son propre traitement)
        elif primary_intent == 'REPORT_DATE' and not deadline_passed_auto_reschedule:
            if implicit_repositioning and engagement_can_reposition:
                # Repositionnement implicite avec engagement faible → forcer report_possible
                report_possible = True
            elif can_modify:
                report_possible = True
            elif mentions_force_majeure:
                report_force_majeure = True
            else:
                report_bloque = True

        # show_session_info: afficher les infos session (pas pour REPORT_DATE/V3 car sessions dans report template)
        # Ni pour DEMANDE_DATE_PLUS_TOT si sessions déjà proposées récemment
        sessions_already_proposed = context.get('sessions_proposed_recently', False)
        is_early_date_with_sessions = primary_intent == 'DEMANDE_DATE_PLUS_TOT' and sessions_already_proposed
        show_session_info = primary_intent != 'REPORT_DATE' and not is_early_date_with_sessions and not v3_confirmed_date

        return {
            'report_bloque': report_bloque,
            'report_possible': report_possible,
            'report_force_majeure': report_force_majeure,
            'deadline_passed_auto_reschedule': deadline_passed_auto_reschedule,
            'show_session_info': show_session_info,
        }

    def _prepare_cma_contact_flags(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Prepare les flags pour le partial contact_cma.html.
        Ces flags determinent quand et comment afficher les instructions de contact CMA.
        """
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')
        intent_context = context.get('intent_context', {})
        mentions_force_majeure = intent_context.get('mentions_force_majeure', False)

        # Scenarios necessitant contact CMA
        requires_contact = any([
            context.get('requires_department_change_process'),
            context.get('report_bloque') and mentions_force_majeure,
            context.get('convocation_anormale'),
            context.get('cloture_passed') and primary_intent == 'REPORT_DATE',
        ])

        # Calculer urgence
        days_until_cloture = context.get('days_until_cloture', 999)

        return {
            'requires_cma_contact': requires_contact,
            'cma_contact_urgent': days_until_cloture < CMA_CONTACT_URGENT_DAYS,
            'is_department_change': context.get('requires_department_change_process', False),
            'is_date_change': primary_intent == 'REPORT_DATE',
        }

    def _extract_thread_memory_flags(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Extract ThreadMemory flags for template variables.

        These flags allow templates to:
        - Acknowledge relances (tm_is_relance)
        - Show progression info (tm_evalbox_changed, tm_date_exam_changed)
        - V3: Conversation intelligence (response_mode, conversation_mode, etc.)
        - The suppression flags are applied separately in section visibility logic.
        """
        thread_mem = context.get('thread_memory', {})
        conv_state = context.get('conversation_state', {})
        if isinstance(conv_state, dict):
            conv_dict = conv_state
        elif hasattr(conv_state, 'to_dict'):
            conv_dict = conv_state.to_dict()
        else:
            conv_dict = {}

        base_flags = {
            'tm_has_history': False,
            'tm_is_relance': False,
            'tm_days_since_last': 0,
            'tm_evalbox_changed': False,
            'tm_evalbox_previous': '',
            'tm_evalbox_current': '',
            'tm_date_exam_changed': False,
            'tm_date_exam_previous': '',
            'tm_date_exam_current': '',
            # V3 flags
            'tm_response_mode': conv_dict.get('response_mode', 'full'),
            'tm_conversation_mode': conv_dict.get('conversation_mode', 'initial_contact'),
            'tm_target_date': conv_dict.get('target_date', ''),
            'tm_has_commitment': conv_dict.get('has_commitments', False),
            'tm_human_is_handling': conv_dict.get('human_is_handling', False),
            'tm_candidate_confirmed_date': context.get('tm_candidate_confirmed_date', ''),
            'tm_candidate_confirmed_session': context.get('tm_candidate_confirmed_session', ''),
            'tm_report_in_progress': context.get('tm_report_in_progress', False),
            'tm_report_target_date': context.get('tm_report_target_date', ''),
        }

        if not thread_mem or not thread_mem.get('has_history'):
            return base_flags

        base_flags.update({
            'tm_has_history': True,
            'tm_is_relance': thread_mem.get('is_relance', False),
            'tm_days_since_last': thread_mem.get('days_since_last', 0),
            'tm_evalbox_changed': thread_mem.get('evalbox_changed', False),
            'tm_evalbox_previous': thread_mem.get('evalbox_previous', ''),
            'tm_evalbox_current': thread_mem.get('evalbox_current', ''),
            'tm_date_exam_changed': thread_mem.get('date_exam_changed', False),
            'tm_date_exam_previous': thread_mem.get('date_exam_previous', ''),
            'tm_date_exam_current': thread_mem.get('date_exam_current', ''),
        })

        return base_flags

    def _extract_cross_department_data(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extrait les donnees cross-departement enrichies si disponibles.
        Ces donnees sont generees par cross_department_helper.get_cross_department_alternatives().
        """
        cross_dept_data = context.get('cross_department_data', {})
        if not cross_dept_data:
            return {}

        return {
            # Separation par region
            'same_region_options': cross_dept_data.get('same_region_options', []),
            'other_region_options': cross_dept_data.get('other_region_options', []),
            'has_same_region_options': cross_dept_data.get('has_same_region_options', False),
            'has_other_region_options': cross_dept_data.get('has_other_region_options', False),

            # Flags de changement de departement
            'requires_department_change_process': cross_dept_data.get('requires_department_change_process', False),
            'current_region': cross_dept_data.get('current_region', ''),

            # Urgence
            'urgency_level': cross_dept_data.get('urgency_level', 'low'),
            'closest_closure_days': cross_dept_data.get('closest_closure_days'),
        }

    def _flatten_session_options_filtered(self, context: Dict[str, Any]) -> list:
        """
        Retourne les sessions aplaties, FILTRÉES selon:
        1. deadline_passed_reschedule → uniquement sessions pour new_exam_date
        2. Date d'examen confirmée → uniquement sessions qui se terminent AVANT l'examen
        3. CONFIRMATION_SESSION + préférence → uniquement jour/soir selon préférence
        """
        session_data = context.get('session_data', {})
        all_sessions = self._flatten_session_options(session_data)

        # Si proposed_options est vide mais sessions_proposees existe (cas match_sessions_by_date_range),
        # utiliser directement sessions_proposees comme source
        if not all_sessions and session_data.get('sessions_proposees'):
            raw_sessions = session_data['sessions_proposees']
            for s in raw_sessions:
                session_type = s.get('session_type', '')
                date_debut = s.get('Date_d_but', '') or s.get('date_debut', '')
                date_fin = s.get('Date_fin', '') or s.get('date_fin', '')
                session_type_label = 'Cours du jour' if session_type == 'jour' else 'Cours du soir' if session_type == 'soir' else ''
                all_sessions.append({
                    'date_examen': '',
                    'date_examen_formatted': '',
                    'date_examen_raw': '',
                    'departement': '',
                    'cloture': '',
                    'date_cloture_formatted': '',
                    'nom': session_type_label or s.get('Name', ''),
                    'session_type_label': session_type_label,
                    'session_name': s.get('Name', ''),
                    'session_id': s.get('id', ''),
                    'debut': self._format_date(date_debut) if date_debut else '',
                    'date_debut': self._format_date(date_debut) if date_debut else '',
                    'fin': self._format_date(date_fin) if date_fin else '',
                    'date_fin': self._format_date(date_fin) if date_fin else '',
                    'type': session_type,
                    'horaires': '8h30-17h30' if session_type == 'jour' else '18h-22h' if session_type == 'soir' else '',
                    'is_jour': session_type == 'jour',
                    'is_soir': session_type == 'soir',
                    'is_first_of_exam': True,
                })
            if all_sessions:
                logger.info(f"📚 Sessions injectées depuis sessions_proposees (date_range match): {len(all_sessions)}")

        # FILTRE 1: Si deadline passée et report automatique, ne montrer que les sessions
        # pour la nouvelle date d'examen (pas toutes les dates alternatives)
        if context.get('deadline_passed_reschedule') and context.get('new_exam_date'):
            new_exam_date = context['new_exam_date']
            # Formater pour comparaison (new_exam_date peut être "2026-03-31" ou "31/03/2026")
            new_exam_formatted = self._format_date(new_exam_date) if new_exam_date else ''
            filtered_by_date = [
                s for s in all_sessions
                if s.get('date_examen_formatted') == new_exam_formatted or s.get('date_examen_raw') == new_exam_date
            ]
            if filtered_by_date:
                logger.info(f"📅 Sessions filtrées pour report (deadline passée) - date {new_exam_formatted}: {len(filtered_by_date)}/{len(all_sessions)}")
                all_sessions = filtered_by_date

        # FILTRE 2: Si date d'examen CONFIRMÉE, ne proposer que les sessions qui se terminent
        # AVANT cette date (on ne peut pas faire une formation après l'examen !)
        primary_intent = context.get('primary_intent') or context.get('detected_intent', '')

        # Pour DEMANDE_CHANGEMENT_SESSION ou session_assignment_error, filtrer par la date d'examen du candidat
        has_session_assignment_error = context.get('session_assignment_error', False)
        is_session_change = primary_intent == 'DEMANDE_CHANGEMENT_SESSION' or 'DEMANDE_CHANGEMENT_SESSION' in context.get('secondary_intents', [])
        if is_session_change or has_session_assignment_error:
            # Récupérer la date d'examen confirmée du candidat
            enriched_lookups = context.get('enriched_lookups') or {}
            date_examen_raw = enriched_lookups.get('date_examen') or context.get('date_examen_raw', '')

            if date_examen_raw:
                exam_date = parse_date_flexible(date_examen_raw)
                if exam_date:
                    filtered_by_exam = []
                    for s in all_sessions:
                        # Récupérer la date de fin de session (format DD/MM/YYYY ou YYYY-MM-DD)
                        session_end_str = s.get('date_fin', '') or s.get('fin', '')
                        if session_end_str:
                            session_end = parse_date_flexible(session_end_str)
                            # Garder seulement si la session se termine AVANT l'examen
                            if session_end is None or session_end < exam_date:
                                filtered_by_exam.append(s)
                        else:
                            # Pas de date de fin, garder par prudence
                            filtered_by_exam.append(s)

                    if filtered_by_exam:
                        logger.info(f"📅 Sessions filtrées par date d'examen ({self._format_date(date_examen_raw)}): {len(filtered_by_exam)}/{len(all_sessions)}")
                        all_sessions = filtered_by_exam
                    else:
                        logger.warning(f"⚠️ Aucune session se terminant avant l'examen du {self._format_date(date_examen_raw)}")
                else:
                    logger.warning(f"⚠️ Impossible de parser la date d'examen '{date_examen_raw}'")

        # FILTRE 3: Sessions passées (date_debut <= aujourd'hui) et session actuelle du candidat
        today = datetime.now().date()
        enriched_lookups_f3 = context.get('enriched_lookups') or {}
        current_session_id = str((enriched_lookups_f3.get('session_record') or {}).get('id', ''))
        filtered_past = []
        for s in all_sessions:
            # Filtre sessions passées
            s_debut = s.get('date_debut', '') or s.get('debut', '')
            if s_debut:
                s_date = parse_date_flexible(s_debut)
                if s_date and s_date <= today:
                    logger.info(f"📅 Session filtrée (passée): {s.get('session_name', s.get('nom', ''))} ({s_debut})")
                    continue
            # Filtre session actuelle du candidat
            s_id = str(s.get('session_id', '') or s.get('id', ''))
            if current_session_id and s_id and s_id == current_session_id:
                logger.info(f"📅 Session filtrée (session actuelle): {s.get('session_name', s.get('nom', ''))}")
                continue
            filtered_past.append(s)
        if len(filtered_past) < len(all_sessions):
            logger.info(f"📅 Sessions après filtre passées/actuelle: {len(filtered_past)}/{len(all_sessions)}")
            all_sessions = filtered_past

        # FILTRE 4 (ex-3): Préférence jour/soir pour CONFIRMATION_SESSION
        secondary_intents = context.get('secondary_intents', [])
        session_preference = self._get_session_preference(context)

        is_confirmation_session = (
            primary_intent == 'CONFIRMATION_SESSION' or
            'CONFIRMATION_SESSION' in secondary_intents
        )

        if is_confirmation_session and session_preference:
            filtered = [s for s in all_sessions if s.get('type') == session_preference]
            if filtered:
                logger.info(f"✅ Sessions filtrées selon préférence '{session_preference}': {len(filtered)}/{len(all_sessions)}")
                return filtered
            logger.warning(f"⚠️ Aucune session '{session_preference}' trouvée, affichage de toutes les sessions")

        # FILTRE 5 (ex-4): Pour session_assignment_error, filtrer par le type de session d'origine
        # (la session erronée indique la préférence du candidat)
        if has_session_assignment_error:
            enriched_lookups = context.get('enriched_lookups') or {}
            original_type = enriched_lookups.get('session_type')  # 'jour' ou 'soir'
            if original_type:
                filtered = [s for s in all_sessions if s.get('type') == original_type]
                if filtered:
                    logger.info(f"✅ Sessions filtrées par type d'origine '{original_type}' (erreur saisie): {len(filtered)}/{len(all_sessions)}")
                    return filtered
                logger.warning(f"⚠️ Aucune session '{original_type}' trouvée avant examen, affichage de toutes les sessions")

        return all_sessions

    def _compute_uber_eligibility_timeline(self, context: Dict, deal_data: Dict) -> Dict:
        """
        Calcule les flags de timeline pour la vérification d'éligibilité Uber.

        Basé sur Date_Dossier_reçu :
        - Pas de date → uber_no_docs_yet (soumettez vos documents)
        - Date < 4 jours → uber_eligibility_pending (en cours de vérification)
        - Date >= 4 jours → vérification terminée (résultat dans compte_uber/eligible_uber)

        Returns:
            Dict avec les flags pour le template
        """
        is_uber = context.get('is_uber_20_deal', False)
        if not is_uber:
            return {
                'uber_no_docs_yet': False,
                'uber_eligibility_pending': False,
                'uber_eligibility_known': False,
                'days_until_eligibility_check': 0,
                'days_until_eligibility_text': '',
            }

        date_dossier_recu = context.get('date_dossier_recu') or deal_data.get('Date_Dossier_re_u')
        compte_uber = context.get('compte_uber', False)
        eligible_uber = context.get('eligible_uber', False)

        # Si éligibilité déjà confirmée → pas besoin de timeline
        if compte_uber and eligible_uber:
            return {
                'uber_no_docs_yet': False,
                'uber_eligibility_pending': False,
                'uber_eligibility_known': True,
                'days_until_eligibility_check': 0,
                'days_until_eligibility_text': '',
            }

        # Pas de documents soumis
        if not date_dossier_recu:
            return {
                'uber_no_docs_yet': True,
                'uber_eligibility_pending': False,
                'uber_eligibility_known': False,
                'days_until_eligibility_check': UBER_VERIFICATION_DELAY_DAYS,
                'days_until_eligibility_text': f'{UBER_VERIFICATION_DELAY_DAYS} jours après soumission de vos documents',
            }

        # Documents soumis — calculer le délai
        try:
            dossier_date = parse_date_flexible(date_dossier_recu)
            today = datetime.now().date()
            days_since = (today - dossier_date).days
            days_remaining = max(0, UBER_VERIFICATION_DELAY_DAYS - days_since)

            if days_remaining > 0:
                if days_remaining == 1:
                    text = 'demain'
                else:
                    text = f'dans {days_remaining} jour(s)'
                return {
                    'uber_no_docs_yet': False,
                    'uber_eligibility_pending': True,
                    'uber_eligibility_known': False,
                    'days_until_eligibility_check': days_remaining,
                    'days_until_eligibility_text': text,
                }
            else:
                # J+4 passé mais pas encore coché → on sait (CAS D/E géré ailleurs)
                return {
                    'uber_no_docs_yet': False,
                    'uber_eligibility_pending': False,
                    'uber_eligibility_known': True,
                    'days_until_eligibility_check': 0,
                    'days_until_eligibility_text': '',
                }
        except Exception as e:
            logger.warning(f"Erreur calcul timeline éligibilité Uber: {e}")
            return {
                'uber_no_docs_yet': False,
                'uber_eligibility_pending': False,
                'uber_eligibility_known': False,
                'days_until_eligibility_check': 0,
                'days_until_eligibility_text': '',
            }

    def _format_statut(self, evalbox: str) -> str:
        """Formate le statut Evalbox pour affichage."""
        return STATUT_DISPLAY.get(evalbox, evalbox or "Statut inconnu")

    def _get_prochaines_etapes(self, state: DetectedState) -> str:
        """Génère les prochaines étapes selon l'état."""
        state_steps = {
            'EXAM_DATE_EMPTY': "Choisissez une date d'examen parmi celles proposées.",
            'DOSSIER_SYNCHRONIZED': "Surveillez vos emails pour la validation CMA.",
            'VALIDE_CMA_WAITING_CONVOC': "Votre convocation arrivera environ 10 jours avant l'examen.",
            'CONVOCATION_RECEIVED': "Téléchargez et imprimez votre convocation.",
            'READY_TO_PAY': "Le paiement CMA est en cours de traitement.",
        }
        return state_steps.get(state.name, "")

    def _replace_placeholders(
        self,
        template: str,
        data: Dict[str, Any]
    ) -> Tuple[str, List[str]]:
        """Remplace les placeholders simples {{variable}} dans le template."""
        replaced = []
        result = template

        # Pattern pour les placeholders: {{placeholder_name}} ou {{a.b.c}} (dot notation)
        pattern = r'\{\{([\w.]+)\}\}'

        for match in re.finditer(pattern, template):
            placeholder = match.group(1)
            # Ignorer les blocs spéciaux (personnalisation, etc.)
            if placeholder in ['personnalisation', 'full_response']:
                continue

            # Support dot notation for nested access
            if '.' in placeholder:
                parts = placeholder.split('.')
                value = data.get(parts[0], {})
                for part in parts[1:]:
                    if isinstance(value, dict):
                        value = value.get(part)
                    else:
                        value = None
                        break
            else:
                value = data.get(placeholder)

            if value and not isinstance(value, bool):
                result = result.replace(f"{{{{{placeholder}}}}}", str(value))
                replaced.append(placeholder)

        return result, replaced

    def _inject_session_choice_if_needed(
        self,
        response_text: str,
        placeholder_data: Dict[str, Any]
    ) -> str:
        """
        Injecte le bloc de choix de session si nécessaire.

        Conditions:
        - show_sessions_section est True (session vide + sessions disponibles)
        - La réponse ne contient pas déjà une demande de session
        - Aucune session n'est déjà assignée au candidat

        Permet aux templates legacy de proposer le choix de session.
        """
        # Ne pas injecter si une session est déjà assignée
        if placeholder_data.get('session_assigned'):
            logger.info("📚 Pas d'injection de choix session (session déjà assignée)")
            return response_text

        # Vérifier si on doit afficher les sessions
        if not placeholder_data.get('show_sessions_section'):
            return response_text

        # Vérifier si la réponse contient déjà une demande de session
        markers = [
            'votre choix de session',
            'cours du jour</b> ou <b>cours du soir',
            'sessions de formation disponibles',
            'préférence de session'
        ]
        response_lower = response_text.lower()
        if any(marker.lower() in response_lower for marker in markers):
            return response_text

        # Générer le bloc de sessions
        sessions_block = self._generate_sessions_block(placeholder_data)
        if not sessions_block:
            return response_text

        logger.info("📚 Injection du bloc de choix de session (template legacy)")

        # Injecter avant la signature
        signature_markers = [
            'Bien cordialement',
            'Cordialement',
            "L'équipe CAB"
        ]

        for marker in signature_markers:
            if marker in response_text:
                insert_pos = response_text.find(marker)
                return (
                    response_text[:insert_pos] +
                    sessions_block +
                    '<br>' +
                    response_text[insert_pos:]
                )

        # Pas de signature trouvée - ajouter à la fin
        return response_text + '<br>' + sessions_block

    def _generate_sessions_block(self, placeholder_data: Dict[str, Any]) -> str:
        """
        Génère le HTML du bloc de choix de session.
        """
        sessions = placeholder_data.get('sessions_proposees', [])
        if not sessions:
            # Fallback: demande simple sans liste
            return (
                '<b>Choix de session de formation</b><br>'
                'Merci de nous indiquer votre préférence : '
                '<b>cours du jour</b> ou <b>cours du soir</b>.<br>'
                '<br>'
            )

        # Bloc avec liste des sessions
        preference = placeholder_data.get('session_preference', '')
        pref_text = f" ({placeholder_data.get('preference_horaire_text', '')})" if preference else ""

        html = f'<b>Sessions de formation disponibles{pref_text}</b><br>'

        current_exam = None
        for session in sessions:
            exam_date = session.get('date_examen_formatted', '')
            if exam_date != current_exam:
                current_exam = exam_date
                html += f'Pour l\'examen du {exam_date} :<br>'

            if session.get('is_jour'):
                html += f'&nbsp;&nbsp;→ <b>Cours du jour</b> : du {session.get("date_debut", "")} au {session.get("date_fin", "")}<br>'
            if session.get('is_soir'):
                html += f'&nbsp;&nbsp;→ <b>Cours du soir</b> : du {session.get("date_debut", "")} au {session.get("date_fin", "")}<br>'

        html += '<br><b>Merci de nous confirmer votre choix de session.</b><br><br>'

        return html

    def _generate_ai_section(
        self,
        state: DetectedState,
        section_name: str,
        ai_generator: callable
    ) -> str:
        """Génère une section via l'IA."""
        response_config = state.response_config
        ai_instructions = response_config.get('ai_instructions', '')

        if section_name == 'full_response':
            return ai_generator(
                state=state,
                instructions=ai_instructions,
                max_length=500
            )

        return ai_generator(
            state=state,
            instructions=ai_instructions,
            max_length=100
        )

    def _generate_alert_content(
        self,
        alert: Dict[str, Any],
        context: Dict[str, Any]
    ) -> Optional[str]:
        """Génère le contenu HTML d'une alerte."""
        alert_type = alert.get('type', '')

        if alert_type == 'uber_case_d':
            return """
<hr>
<p><b>Information importante concernant votre compte Uber</b></p>
<p>Nous avons constaté que l'adresse email utilisée pour votre inscription n'est pas reconnue par Uber comme un compte chauffeur actif.</p>
<p>Veuillez vérifier que vous utilisez la même adresse email que votre compte <b>Uber Driver</b> (pas Uber client). Si le problème persiste, contactez le support Uber via l'application.</p>
<hr>"""

        if alert_type == 'uber_case_e':
            return """
<hr>
<p><b>Information importante concernant votre éligibilité Uber</b></p>
<p>Selon les informations d'Uber, votre profil n'est pas éligible à l'offre partenariat. Nous n'avons pas de visibilité sur les raisons de cette décision.</p>
<p>Nous vous invitons à contacter le support Uber via l'application <b>Uber Driver</b> (Compte → Aide) pour comprendre votre situation.</p>
<hr>"""

        if alert_type == 'personal_account_warning':
            # Utiliser pybars_renderer pour gérer le template Handlebars
            if self.pybars_renderer:
                alert_context = {
                    'personal_account_email': alert.get('personal_account_email', ''),
                    'cab_account_email': alert.get('cab_account_email', ''),
                    'cab_payment_date': alert.get('cab_payment_date', '')
                }
                # Le partial est déjà chargé par pybars_renderer, on peut l'appeler via {{> warnings/personal_account_warning}}
                # Mais pour une alerte standalone, on charge et rend directement
                template_content = self._load_template('templates/partials/warnings/personal_account_warning.html')
                if template_content:
                    rendered = self.pybars_renderer.render(template_content, alert_context)
                    return f"<hr>\n{rendered}\n<hr>"
            return None

        if alert_type == 'session_assignment_error':
            # Utiliser pybars_renderer pour gérer le template Handlebars
            if self.pybars_renderer:
                alert_context = {
                    'session_error_dates': alert.get('session_error_dates', ''),
                    'session_end_date': alert.get('session_end_date', ''),
                    'deal_created_date': alert.get('deal_created_date', ''),
                    'correct_year': alert.get('correct_year'),
                    'has_exam_date': bool(alert.get('date_examen_formatted')),
                    'date_examen_formatted': alert.get('date_examen_formatted', '')
                }
                template_content = self._load_template('templates/partials/warnings/session_assignment_error.html')
                if template_content:
                    rendered = self.pybars_renderer.render(template_content, alert_context)
                    return rendered  # Pas de <hr> car c'est le contenu principal
            return None

        return None

    def _insert_alert(
        self,
        response: str,
        alert_content: str,
        position: str = 'before_signature'
    ) -> str:
        """Insère une alerte dans la réponse HTML."""
        if position == 'before_signature':
            # Chercher la signature (bloc signature ou "Bien cordialement")
            signature_patterns = [
                r'(<p[^>]*>.*?(?:cordialement|équipe cab).*?</p>)',
                r'(Bien cordialement)',
                r'(L\'équipe CAB)',
            ]
            for pattern in signature_patterns:
                match = re.search(pattern, response, re.IGNORECASE | re.DOTALL)
                if match:
                    return response[:match.start()] + alert_content + "\n" + response[match.start():]

        # Fallback: ajouter à la fin
        return response.rstrip() + "\n" + alert_content

    def _cleanup_unresolved_placeholders(self, response: str) -> str:
        """Nettoie les placeholders non remplacés."""
        # Supprimer les placeholders vides (sauf personnalisation qu'on garde pour debug)
        cleaned = re.sub(r'\{\{(?!personnalisation)\w+\}\}', '', response)
        # Nettoyer les lignes vides multiples
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        # Nettoyer les paragraphes vides
        cleaned = re.sub(r'<p>\s*</p>', '', cleaned)
        return cleaned

    def _strip_comments(self, response: str) -> str:
        """Supprime les commentaires HTML et Handlebars du texte final."""
        # Supprimer les commentaires Handlebars {{!-- ... --}}
        cleaned = re.sub(r'\{\{!--.*?--\}\}', '', response, flags=re.DOTALL)
        # Supprimer les commentaires HTML <!-- ... -->
        cleaned = re.sub(r'<!--.*?-->', '', cleaned, flags=re.DOTALL)
        # Nettoyer les lignes vides multiples
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    def _generate_fallback_response(
        self,
        state: DetectedState,
        ai_generator: Optional[callable]
    ) -> Dict[str, Any]:
        """Génère une réponse de fallback quand pas de template."""
        placeholder_data = self._prepare_placeholder_data(state)
        prenom = placeholder_data.get('prenom', 'Bonjour')

        fallback_template = f"""<p>Bonjour {prenom},</p>

<p>{{{{personnalisation}}}}</p>

<p>Bien cordialement,<br>
{COMPANY_SIGNATURE}</p>"""

        response_text = fallback_template
        ai_sections = []

        if ai_generator:
            ai_content = ai_generator(
                state=state,
                instructions="Répondre de manière contextuelle au candidat.",
                max_length=300
            )
            if ai_content:
                response_text = response_text.replace("{{personnalisation}}", ai_content)
                ai_sections.append('personnalisation')

        return {
            'response_text': self._cleanup_unresolved_placeholders(response_text),
            'template_used': 'fallback',
            'template_file': None,
            'placeholders_replaced': ['prenom'],
            'ai_sections_generated': ai_sections,
            'alerts_included': [],
            'blocks_included': [],
            'crm_updates_from_matrix': []  # Pas de CRM updates pour fallback
        }
