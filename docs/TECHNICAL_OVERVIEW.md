# Documentation Technique -- A-Level Saver

## Vue d'Ensemble

A-Level Saver est un systeme d'automatisation du traitement des tickets Zoho Desk pour **CAB Formations**, un centre de formation VTC (Voiture de Transport avec Chauffeur) en partenariat avec Uber. Le systeme recoit les tickets du departement DOC via webhook, analyse le contexte CRM du candidat, detecte son etat et son intention, genere une reponse personnalisee via un pipeline deterministe (templates Handlebars + humanisation IA), puis cree un brouillon ou envoie directement la reponse.

**Metriques du code source :**

| Composant | Lignes |
|-----------|--------|
| `src/` (Python) | ~32 500 |
| `states/` (YAML + HTML + MD) | ~9 100 |
| Fichier principal (`doc_ticket_workflow.py`) | 7 200 |
| Template Engine (`template_engine.py`) | 3 223 |
| Deal Linking Agent (`deal_linking_agent.py`) | 2 194 |
| State Detector (`state_detector.py`) | 1 097 |
| Date Exam Helper (`date_examen_vtc_helper.py`) | 1 453 |
| Session Helper (`session_helper.py`) | 1 362 |
| Triage Agent (`triage_agent.py`) | 1 059 |
| ExamT3P Playwright (`exament3p_playwright.py`) | 1 017 |
| Thread Memory (`thread_memory.py`) | 957 |
| Zoho Client (`zoho_client.py`) | 899 |
| Partials HTML | 93 fichiers |

---

## Architecture Systeme

```
                      ZOHO DESK
                    (Workflow Rule)
                         |
                    Deluge invokeurl
                         |
                         v
             +-------------------------+
             |    webhook_server.py     |  Flask / Gunicorn (port 10000)
             |    (227 lignes)          |  Authentification X-Webhook-Secret
             +-------------------------+
                         |
                  threading.Thread
                  (fire-and-forget)
                         |
                         v
  +--------------------------------------------------+
  |         DOCTicketWorkflow.process_ticket()        |
  |           doc_ticket_workflow.py (7200 L)         |
  |                                                    |
  |  STEP 0   Verification brouillon existant          |
  |  STEP 0.1 Skip Instant Messages (SalesIQ)         |
  |  STEP 0.5 Clarification doublon en attente         |
  |  STEP 1   Agent Trieur (TriageAgent + LLM)        |
  |  STEP 2   Agent Analyste (7 sources de donnees)   |
  |  STEP 3   Agent Redacteur (State Engine)           |
  |  STEP 4   Ticket Update (statut, tags)             |
  |  STEP 5   Deal Update (CRMUpdateAgent)             |
  |  STEP 6   CRM Note ([META] line)                   |
  |  STEP 7   Reply Delivery (envoi ou brouillon)      |
  |  STEP 8   Validation finale                        |
  +--------------------------------------------------+
       |              |              |              |
       v              v              v              v
  +---------+   +-----------+  +-----------+  +-----------+
  |  Zoho   |   |  Zoho     |  | Anthropic |  |  ExamT3P  |
  |  Desk   |   |  CRM      |  |  Claude   |  | Playwright|
  |  API    |   |  API v3   |  |  API      |  | Scraping  |
  +---------+   +-----------+  +-----------+  +-----------+
```

### Flux de Donnees Principal

```
Ticket Zoho Desk
     |
     v
[1] TriageAgent (LLM) -----> action: GO / ROUTE / SPAM / DUPLICATE_UBER
     |                        + detected_intent (50 intentions)
     |                        + session_preference
     v
[2] _run_analysis() -------> 7 sources de donnees:
     |                        - CRM Deal + Contact + Enriched Lookups
     |                        - ExamT3P (Playwright scraping)
     |                        - Date d'examen (10 cas)
     |                        - Sessions de formation
     |                        - Uber eligibilite (CAS A/B/D/E)
     |                        - Threads ticket (historique)
     |                        - ThreadMemory V1/V2/V3
     v
[3] StateDetector ----------> DetectedStates (blocking + warning + info)
     |
     v
[4] TemplateEngine ---------> Matrice ETAT x INTENTION -> Template Handlebars
     |                        + context_flags + placeholder data
     |
     v
[5] PybarsRenderer ---------> HTML structure (partials compiles)
     |
     v
[6] ResponseHumanizer ------> Reformulation naturelle (LLM Sonnet)
     |
     v
[7] ResponseValidator ------> Termes interdits, dates coherentes
     |
     v
[8] Zoho Desk Draft/Reply --> Brouillon ou envoi direct
     + CRM Note [META]
     + CRM Deal Updates
```

---

## Pipeline de Traitement (8 Etapes)

### STEP 0 : Verification Prealable

**Methode :** `process_ticket()` (lignes 460-490)

**Ce qui se passe :**
- Verifie si un brouillon existe deja pour ce ticket (`has_existing_draft`)
- Detecte les tickets Instant Message (SalesIQ chat widget) et les cloture
- Verifie si une clarification de doublon est en attente

**Donnees en sortie :**
```python
# Si brouillon existe :
{'workflow_stage': 'SKIPPED_DRAFT_EXISTS', 'success': True}
# Si Instant Message :
{'workflow_stage': 'SKIPPED_INSTANT_MESSAGE', 'success': True}
```

**Gestion d'erreur :** Degradation gracieuse -- en cas d'erreur API, le workflow continue.

---

### STEP 1 : Agent Trieur (Triage)

**Methode :** `_run_triage()` (ligne 1696, ~940 lignes)

**Ce qui se passe :**
1. Recuperation des threads du ticket via API Zoho Desk
2. Extraction de l'email candidat (gestion des forwards internes)
3. Detection SPAM via keywords (`SPAM_KEYWORDS`)
4. Recherche du deal CRM via `DealLinkingAgent.process()`
5. Detection de doublons Uber (deal 20EUR GAGNE existant)
6. Detection de demandes non-Uber (CPF, France Travail, etc.)
7. Appel LLM `TriageAgent.triage_ticket()` pour analyse contextuelle
8. Extraction de l'intention et du `session_preference`

**Modele IA :** `claude-sonnet-4-20250514` (`MODEL_TRIAGE`)

**Structure de retour :**
```python
{
    'action': 'GO' | 'ROUTE' | 'SPAM' | 'DUPLICATE_UBER',
    'target_department': str,          # Si ROUTE
    'detected_intent': str,            # Ex: 'REPORT_DATE'
    'secondary_intents': List[str],    # Intentions secondaires
    'intent_context': {
        'session_preference': 'jour' | 'soir' | None,
        'mentioned_month': str,
        'mentioned_date': str,
        'implicit_date_repositioning': bool,
        'cancellation_reason': str,
    },
    'confidence': float,
    'deal_id': str,
    'deal_data': Dict,
    'linking_result': Dict,
    'email_searched': str,
    'incoming_thread_count': int,
    'ticket_subject': str,
}
```

**Intentions possibles (50+) :** Definies dans `states/state_intention_matrix.yaml` et detectees par le prompt systeme de `TriageAgent` dans `src/agents/triage_agent.py`.

---

### STEP 2 : Agent Analyste (Analyse)

**Methode :** `_run_analysis()` (ligne 2736, ~1780 lignes)

**7 sources de donnees :**

| # | Source | API / Methode | Donnees Extraites |
|---|--------|---------------|-------------------|
| 1 | CRM Zoho | `DealLinkingAgent`, `get_deal()`, `get_contact()` | Deal, Contact, enriched lookups |
| 2 | ExamT3P | `ExamT3PAgent` + `exament3p_playwright.py` (Playwright) | Statut dossier, documents, paiements |
| 3 | Date examen | `analyze_exam_date_situation()` | CAS 1-10, dates futures, clotures |
| 4 | Sessions | `analyze_session_situation()`, `get_sessions_for_exam_date()` | Options CDJ/CDS, filtrage par date |
| 5 | Uber eligibilite | `analyze_uber_eligibility()` | CAS A/B/D/E, prospect, eligible |
| 6 | Threads | `get_all_threads_with_full_content()` | Historique conversation |
| 7 | ThreadMemory | `analyze_thread_memory()` + `analyze_conversation()` (V3) | Memoire inter-tickets, suppressions |

**Sous-etapes critiques :**
- Enrichissement des lookups CRM via `enrich_deal_lookups()` (Date_examen_VTC, Session)
- Classification du Resultat CRM (`_classify_resultat()` : pre_exam/mid_exam/post_exam/closed)
- Detection `dossier_termine` (bloque les mises a jour CRM)
- Verification cross-ticket insistance (DEMANDE_ANNULATION via ThreadMemory META)
- Detection de repositionnement implicite de date (`implicit_date_repositioning`)
- Cascade de changement de session (`_apply_session_change_cascade()`)

**Structure de retour :**
```python
{
    'contact_data': Dict,
    'deal_id': str,
    'deal_data': Dict,
    'enriched_lookups': {
        'date_examen': '2026-05-26',          # Vraie date (pas lookup ID)
        'date_cloture': '2026-05-10',
        'departement': '75',
        'session_name': 'cdj-13/04-24/04',
        'session_type': 'jour',
        'session_date_debut': '2026-04-13',
        'session_date_fin': '2026-04-24',
    },
    'examt3p_data': Dict,                     # Donnees Playwright
    'date_examen_vtc_result': {
        'case': int,                           # CAS 1 a 10
        'case_description': str,
        'next_dates': List[Dict],
        'should_include_in_response': bool,
    },
    'session_data': Dict,                      # Options de session
    'uber_eligibility': Dict,                  # CAS A/B/D/E
    'threads': List[Dict],                     # Threads complets
    'thread_memory': ThreadMemoryResult,       # V1/V2
    'conversation_state': ConversationState,   # V3
    'resultat_info': Dict,                     # Classification Resultat
    'ancien_dossier': bool,
}
```

---

### STEP 3 : Agent Redacteur (Generation de Reponse)

**Methodes :** `_run_response_generation()` (ligne 5271) -> `_run_state_driven_response()` (ligne 5311, ~720 lignes)

**Sous-etapes :**

**3.1 -- Detection d'etat** (`StateDetector.detect_all_states()`)
```python
detected_states = self.state_detector.detect_all_states(
    deal_data=deal_data,
    examt3p_data=examt3p_data,
    triage_result=triage_result,
    linking_result=linking_result,
    enriched_lookups=enriched_lookups,
)
# Retourne: DetectedStates (blocking, warning, info, primary)
```

**3.2 -- Generation du template** (`TemplateEngine.generate_response_multi()`)
- Selection du template via la matrice `STATE:INTENTION` dans `state_intention_matrix.yaml`
- Injection des `context_flags` depuis la matrice
- Preparation des placeholders (`_prepare_placeholder_data()`, ~930 lignes)
- Rendu Handlebars via `PybarsRenderer.render()`

**3.3 -- Humanisation** (`humanize_response()`)
- Modele : `claude-sonnet-4-20250514` (`MODEL_HUMANIZER`)
- Reformulation naturelle et empathique
- Preservation stricte des dates, liens, montants
- Instructions specifiques selon `response_mode` V3 (full/brief/targeted/status_update)

**3.4 -- Validation** (`ResponseValidator.validate()`)
- Termes interdits (BFS, Evalbox, 20EUR, CRM, deal, API, etc.)
- Coherence des dates mentionnees
- Montants autorises via `allowed_amounts`
- Verification des blocs obligatoires (salutation, signature)

**Structure de retour :**
```python
{
    'response_text': str,                  # Reponse finale HTML
    'raw_template_output': str,            # Avant humanisation
    'was_humanized': bool,
    'detected_state': str,                 # Ex: 'EXAM_DATE_ASSIGNED_WAITING'
    'detected_intent': str,                # Ex: 'REPORT_DATE'
    'template_used': str,                  # Ex: 'response_master.html'
    'matrix_entry': str,                   # Ex: '*:REPORT_DATE'
    'validation': Dict,
    'crm_updates': Dict,                   # Mises a jour CRM determinees
    'should_stop_workflow': bool,
}
```

---

### STEP 4 : Ticket Update

**Methode :** `_prepare_ticket_updates()` (ligne 6998)

Met a jour le statut et les tags du ticket Zoho Desk si `auto_update_ticket=True`.

---

### STEP 5 : Deal Update (CRM)

**Methode :** Section du `process_ticket()` (ligne 1299)

**Ce qui se passe :**
1. Guard rail `dossier_termine` : bloque les mises a jour si Resultat = post_exam/closed
2. Appel `CRMUpdateAgent.process()` pour les mises a jour validees
3. Mapping automatique des valeurs string vers les IDs CRM (lookup fields)
4. Respect des regles de blocage (VALIDE CMA + cloture passee)

**Champs mis a jour :**
- `Date_examen_VTC` (lookup vers module `Dates_Examens_VTC_TAXI`)
- `Session` (lookup vers module `Sessions1`)
- `Preference_horaire` (picklist : jour/soir)
- `Evalbox` (sync depuis ExamT3P)
- `IDENTIFIANT_EVALBOX`, `MOTDEPASSE_EVALBOX`

---

### STEP 6 : CRM Note

**Methode :** `_create_crm_note()` (ligne 6682) + `_build_meta_line()` (ligne 6788)

Cree une note CRM sur le deal avec :
- Resume des actions effectuees
- Ligne `[META]` pour ThreadMemory V1 :
```
[META] ticket=198709000... | state=EXAM_DATE_ASSIGNED_WAITING | intent=REPORT_DATE | evalbox=Dossier Synchronise | date_exam=2026-05-26 | date_case=5 | session=cds-13/04-24/04 | sections=dates,sessions,statut | response_mode=full
```

---

### STEP 7 : Reply Delivery

**Methode :** Section du `process_ticket()` (ligne 1479)

**Modes de livraison :**
1. **Auto-send** : Envoi direct via `send_ticket_reply()` si `_can_auto_send()` retourne `True`
   - Conditions : sujet dans whitelist (`AUTO_SEND_SCENARIOS`), reponse humanisee, validation OK
   - Ticket ferme automatiquement apres envoi
2. **Draft** : Creation de brouillon via `create_ticket_reply_draft()`
   - Fallback si auto-send echoue
   - Marque le ticket avec `BROUILLON AUTO = true`
3. **None** : Reponse dans les logs (copier-coller manuel)

**Conversion format :** Markdown -> HTML (liens, gras, headers, sauts de ligne).

---

### STEP 8 : Validation Finale

**Methode :** Section du `process_ticket()` (ligne 1643)

Verification de coherence finale et transfert vers DOCS CAB si le deal est VTC hors partenariat.

---

## Modules Python

### src/workflows/

#### `doc_ticket_workflow.py` (7 200 lignes)

Orchestrateur principal. Classe unique `DOCTicketWorkflow`.

**Methodes principales :**

| Methode | Lignes | Description |
|---------|--------|-------------|
| `__init__()` | 125-155 | Initialise 2 clients Zoho + tous les agents |
| `process_ticket()` | 409-1695 | Pipeline 8 etapes complet |
| `_run_triage()` | 1696-2636 | Etape 1 : triage + detection intention |
| `_run_analysis()` | 2736-4519 | Etape 2 : extraction 7 sources |
| `_run_response_generation()` | 5271-5309 | Etape 3 : delegation au State Engine |
| `_run_state_driven_response()` | 5311-6263 | Coeur : detection etat + template + humanisation + validation |
| `_create_crm_note()` | 6682-6787 | Etape 6 : note CRM avec META |
| `_build_meta_line()` | 6788-6872 | Construction ligne [META] pour ThreadMemory |
| `_prepare_deal_updates()` | 7008-7146 | Etape 5 : preparation mises a jour CRM |
| `_classify_resultat()` | 6464-6490 | Classification Resultat CRM (pre/mid/post_exam) |
| `_apply_session_change_cascade()` | 6359-6463 | Cascade 3 niveaux pour changement de session |
| `_generate_duplicate_uber_response()` | 4923-5056 | Reponse doublon Uber |

**Composants injectes :**
```python
self.desk_client = ZohoDeskClient()        # API Zoho Desk
self.crm_client = ZohoCRMClient()          # API Zoho CRM
self.deal_linker = DealLinkingAgent(...)    # Recherche deal
self.examt3p_agent = ExamT3PAgent()        # Scraping ExamT3P
self.dispatcher = TicketDispatcherAgent(..) # Routage tickets
self.crm_update_agent = CRMUpdateAgent(..) # Mises a jour CRM
self.triage_agent = TriageAgent()          # Triage LLM
self.state_detector = StateDetector()      # Detection etat
self.template_engine = TemplateEngine()    # Rendu templates
self.response_validator = ResponseValidator()
self.state_crm_updater = CRMUpdater(...)
self.anthropic_client = anthropic.Anthropic()  # Pour personnalisation
```

---

### src/agents/

#### `base_agent.py` (108 lignes)

Classe abstraite `BaseAgent` dont heritent tous les agents. Fournit :
- Client Anthropic initialise avec `settings.anthropic_api_key`
- Methode `ask()` pour envoyer un message au LLM avec contexte optionnel
- Gestion de l'historique de conversation
- Methode abstraite `process()` a implementer par chaque agent

#### `triage_agent.py` (1 059 lignes)

`TriageAgent(BaseAgent)` -- Detection d'intention via LLM.

**Modele :** `claude-sonnet-4-20250514`

**Methode principale :** `triage_ticket(ticket_subject, thread_content, deal_data)`

**Retourne :**
```python
{
    'action': 'GO' | 'ROUTE' | 'SPAM',
    'target_department': str,
    'reason': str,
    'confidence': float,
    'detected_intent': str,          # Intention principale
    'secondary_intents': List[str],  # Intentions secondaires
    'intent_context': {
        'session_preference': str,
        'mentioned_month': str,
        'communication_mode': str,   # 'question', 'confirmation', 'clarification'
        'cancellation_reason': str,
        'eligibility_concern': bool,
        'implicit_date_repositioning': bool,
    }
}
```

**Intentions detectees (extrait) :** `DEMANDE_DATE_EXAMEN`, `REPORT_DATE`, `CONFIRMATION_SESSION`, `DEMANDE_CHANGEMENT_SESSION`, `DEMANDE_IDENTIFIANTS`, `DEMANDE_ANNULATION`, `STATUT_DOSSIER`, `QUESTION_GENERALE`, `RESULTAT_EXAMEN`, `DEMANDE_CONVOCATION`, `DEMANDE_REINSCRIPTION`, etc.

#### `crm_update_agent.py` (555 lignes)

`CRMUpdateAgent(BaseAgent)` -- Gestion des mises a jour CRM.

**Responsabilites :**
- Mapping valeurs string -> IDs CRM (lookup fields)
- Validation des regles de blocage (`BLOCKING_MODIFICATION`)
- Logging des mises a jour dans les notes CRM
- Gestion des champs `Date_examen_VTC` (module `Dates_Examens_VTC_TAXI`) et `Session` (module `Sessions1`)

#### `deal_linking_agent.py` (2 194 lignes)

`DealLinkingAgent(BaseAgent)` -- Recherche et liaison du ticket au deal CRM.

**Strategies de recherche (par priorite) :**
1. Champ custom `cf_opportunite` du ticket
2. Email du candidat -> recherche deals CRM
3. Telephone du candidat
4. Extraction email depuis forwards internes
5. Extraction email depuis contenu du message (LLM `MODEL_EXTRACTION`)

**Gestion des doublons :**
- Detection de deals 20EUR GAGNE multiples
- Classification : `DUPLICATE_UBER`, `DUPLICATE_RECOVERABLE`
- Verification `has_paid_formation_after_uber` pour bypass

#### `dispatcher_agent.py` (513 lignes)

`TicketDispatcherAgent(BaseAgent)` -- Routage de tickets entre departements.

#### `examt3p_agent.py` (152 lignes)

`ExamT3PAgent(BaseAgent)` -- Orchestration de l'extraction ExamT3P via Playwright.

#### `desk_agent.py` (278 lignes) / `crm_agent.py` (291 lignes)

Agents legacy pour le traitement generique de tickets et deals. Utilises par `ZohoAutomationOrchestrator` dans `main.py`.

---

### src/state_engine/

#### `state_detector.py` (1 097 lignes)

`StateDetector` -- Detection deterministe de l'etat du candidat.

**Dataclasses :**

```python
@dataclass
class DetectedState:
    id: str                          # Ex: 'T1', 'D-5', 'U-A'
    name: str                        # Ex: 'SPAM', 'EXAM_DATE_ASSIGNED_WAITING'
    priority: int                    # 1 = plus prioritaire
    category: str                    # 'triage', 'uber', 'date_examen', etc.
    description: str
    workflow_action: str             # 'STOP', 'CONTINUE', 'ALERT'
    response_config: Dict[str, Any]
    crm_updates_config: Optional[Dict]
    detection_reason: str
    severity: str                    # 'BLOCKING', 'WARNING', 'INFO'
    context_data: Dict[str, Any]
    alerts: List[Dict[str, Any]]
    detected_intent: Optional[str]
    intent_context: Dict[str, Any]

@dataclass
class DetectedStates:
    blocking_state: Optional[DetectedState]    # Stoppe le workflow
    warning_states: List[DetectedState]        # Alertes a inclure
    info_states: List[DetectedState]           # Informatifs
    primary_state: Optional[DetectedState]     # Pour retrocompatibilite
    all_states: List[DetectedState]
```

**Ordre d'evaluation (par priorite) :**
1. Etats triage (T1-T4) : SPAM, ROUTE, DUPLICATE_UBER, CANDIDATE_NOT_FOUND
2. Etats analyse (A1-A3) : CREDENTIALS_INVALID, EXAMT3P_DOWN, DOUBLE_ACCOUNT
3. Etats Uber (U-*) : PROSPECT, CAS A/B/D/E
4. Etats date examen (D-1 a D-10) : 10 cas selon date + Evalbox
5. Etats intention (I1-I9)
6. Etats coherence (C1-C3) : TRAINING_MISSED, REFRESH_SESSION
7. Etats blocage (B1) : DATE_MODIFICATION_BLOCKED
8. Etat par defaut : GENERAL

**Methodes cles :**
- `detect_all_states()` : Point d'entree, retourne `DetectedStates`
- `_build_context()` : Construit le contexte de detection depuis les donnees brutes
- `_matches_state()` / `_match_*_state()` : Logique de matching par categorie
- `_collect_alerts()` : Collecte des alertes Uber D/E

#### `template_engine.py` (3 223 lignes)

`TemplateEngine` -- Generation controlee des reponses.

**Architecture :**
```
state_intention_matrix.yaml
        |
        v
_select_base_template() --> "response_master.html"
        |                    + context_flags
        v
_prepare_placeholder_data() --> ~200 variables Handlebars
        |                       (whitelist explicite)
        v
PybarsRenderer.render() --> HTML final
```

**Methodes cles :**

| Methode | Lignes | Description |
|---------|--------|-------------|
| `generate_response_multi()` | 119-196 | Point d'entree, gere multi-etats |
| `_select_base_template()` | 304-446 | Selection via matrice ou fallback |
| `_prepare_placeholder_data()` | 671-1598 | ~930 lignes : preparation de ~200 variables |
| `_auto_map_intention_flags()` | 1599-1721 | Mapping intention -> flag boolean |
| `_determine_required_actions()` | 1746-1892 | Logique des actions requises |
| `_format_next_dates_for_template()` | 1893-2003 | Formatage dates pour template |
| `_compute_session_temporal_flags()` | 2160-2190 | 5 flags temporels session |
| `_flatten_session_options()` | 2399-2497 | Aplatissement options session |
| `_generate_report_flags()` | 2515+ | Flags pour report bloque/possible |

**Regle critique (Regle 11) :** Si la matrice definit un flag (`show_dates_section: false`), le code Python ne doit JAMAIS le recalculer.

```python
# CORRECT :
if 'show_dates_section' in context:
    result['show_dates_section'] = context['show_dates_section']
else:
    result['show_dates_section'] = not date_examen and bool(next_dates)
```

#### `pybars_renderer.py` (221 lignes)

`PybarsRenderer` -- Rendu Handlebars via la bibliotheque pybars3.

**Fonctionnement :**
1. `load_all_partials()` : Charge et compile les 93 partials HTML + blocks legacy + templates base
2. `render()` : Compile le template (cache par hash), prepare le contexte (None -> ''), rend avec partials
3. Cache des templates compiles (`_compiled_cache`) pour performances

**Syntaxe Handlebars supportee :**
```html
{{variable}}                          <!-- Remplacement -->
{{> partials/intentions/report_date}} <!-- Inclusion partial -->
{{#if condition}}...{{/if}}           <!-- Conditionnel -->
{{#unless condition}}...{{/unless}}   <!-- Conditionnel inverse -->
{{#each items}}{{this.prop}}{{/each}} <!-- Boucle -->
```

#### `response_validator.py` (567 lignes)

`ResponseValidator` -- Validation stricte des reponses generees.

**Validations effectuees :**
1. Termes interdits : `BFS`, `Evalbox`, `20EUR`, `CRM`, `deal`, `API`, `Montreuil`, etc.
2. Blocs obligatoires : salutation, signature
3. Coherence des dates mentionnees (pas de dates inventees)
4. Montants coherents (via `allowed_amounts` par intention)
5. Format et structure HTML

#### `crm_updater.py` (562 lignes)

`CRMUpdater` -- Mises a jour CRM deterministes basees sur l'etat detecte.

**Cas de mise a jour :**
- `CONFIRMATION_SESSION` : extraction choix candidat -> Session + Preference_horaire
- `CONFIRMATION_DATE_EXAMEN` : extraction date choisie -> Date_examen_VTC
- Sync ExamT3P : identifiants, Evalbox

**Regles de blocage :**
- B1 : Ne pas modifier Date_examen_VTC si Evalbox in `{VALIDE CMA, Convoc CMA recue}` ET cloture passee

---

### src/utils/

#### `zoho_client.py` (899 lignes)

Trois classes pour les API Zoho :

**`ZohoAPIClient`** (base) :
- Rate limiting : 300ms minimum entre appels (`_apply_api_rate_limit()`)
- Token management via `ZohoTokenManager` singleton
- Retry automatique sur 401 (token expire) et 429 (rate limit)

**`ZohoDeskClient(ZohoAPIClient)`** (lignes 173-675) :
- `get_ticket()`, `list_tickets()`, `update_ticket()`
- `get_ticket_threads()`, `get_all_threads_with_full_content()`
- `create_ticket_reply_draft()`, `send_ticket_reply()`
- `has_existing_draft()`, `add_ticket_comment()`
- `move_ticket_to_department()`

**`ZohoCRMClient(ZohoAPIClient)`** (lignes 676-899) :
- `get_deal()`, `update_deal()`, `search_deals()`, `search_deals_by_email()`
- `get_contact()`, `update_contact()`
- `get_record()` (generique, pour modules custom)
- `get_deal_notes()`, `add_deal_note()`
- `get_deal_timeline()` (API v8 pour ThreadMemory V2)
- `get_deals_by_contact()`

#### `crm_lookup_helper.py` (227 lignes)

Enrichissement des champs lookup CRM.

```python
# Les champs Date_examen_VTC et Session retournent {name, id}
# Ce helper resout les IDs vers les vraies donnees
enriched = enrich_deal_lookups(crm_client, deal_data, cache)
# enriched = {
#     'date_examen': '2026-05-26',
#     'date_cloture': '2026-05-10',
#     'departement': '75',
#     'session_name': 'cdj-13/04-24/04',
#     'session_type': 'jour',
#     'session_date_debut': '2026-04-13',
#     'session_date_fin': '2026-04-24',
# }
```

**Modules CRM cibles :**
- `Date_examen_VTC` -> module `Dates_Examens_VTC_TAXI` (champs : `Date_Examen`, `Departement`, `Date_Cloture_Inscription`)
- `Session` -> module `Sessions1` (champs : `Name`, `session_type`, `Date_d_but`, `Date_fin`)

#### `date_examen_vtc_helper.py` (1 453 lignes)

Logique des 10 cas de date d'examen VTC.

**Cas geres :**

| CAS | Condition | Comportement |
|-----|-----------|-------------|
| 1 | Date vide | Proposer 2 prochaines dates (dept candidat) |
| 2 | Date passee + Evalbox pre-validation | Auto-report sur prochaine date |
| 3 | Evalbox = Refuse CMA | Informer du refus + prochaine date |
| 4 | Date future + VALIDE CMA | Rassurer (convocation ~10j avant) |
| 5 | Date future + Dossier Synchronise | Prevenir (instruction en cours) |
| 6 | Date future + autre Evalbox | En attente |
| 7 | Date passee + VALIDE CMA / Convoc | Examen probablement passe |
| 8 | Date future + cloture passee + pre-validation | Deadline ratee, auto-report |
| 9 | Evalbox = Convoc CMA recue | Transmettre identifiants + bonne chance |
| 10 | Evalbox = Pret a payer | Paiement en cours, surveiller emails |

**Fonctions cles :**
- `analyze_exam_date_situation()` : Point d'entree, retourne le cas applicable
- `get_next_exam_dates()` : Recherche dates futures via API CRM (module `Dates_Examens_VTC_TAXI`)
- `classify_engagement_level()` : Niveaux 0-4 d'engagement (pour repositionnement implicite)
- `get_earlier_dates_other_departments()` : Dates plus proches dans d'autres departements

#### `session_helper.py` (1 362 lignes)

Gestion des sessions de formation.

**Logique metier :**
- Les sessions doivent se terminer AVANT la date d'examen
- Convention : `cdj-*` = Cours Du Jour (8h30-17h30), `cds-*` = Cours Du Soir (18h-22h)
- Filtrage : uniquement sessions VISIO Zoom VTC (`is_uber_visio_session()`)

**Fonctions cles :**
- `get_sessions_for_exam_date()` : Recherche sessions adaptees via API CRM (module `Sessions1`)
- `analyze_session_situation()` : Analyse complete avec gestion du `allow_change`
- `match_sessions_by_date_range()` : Matching par plage de dates
- `verify_session_complaint()` : Verification plainte session

#### `thread_memory.py` (957 lignes)

Memoire persistante inter-tickets.

**V1 -- META Lines :**
```python
@dataclass
class MetaRecord:
    ticket_id: str
    timestamp: Optional[datetime]
    state: str
    intent: str
    evalbox: str
    date_exam: str
    date_case: str
    session: str
    sections: List[str]          # ['dates', 'sessions', 'statut']
    secondary_intents: List[str]
    # V3 fields :
    target_date: str
    proposed_dates: List[str]
    proposed_sessions: List[str]
    response_mode: str
```

**V2 -- Timeline API :**
```python
@dataclass
class FieldChange:
    field: str            # Ex: 'Evalbox'
    old_value: str
    new_value: str
    timestamp: Optional[datetime]
    actor: str
    source: str           # 'automation', 'crm_ui', 'manual'

@dataclass
class HumanIntervention:
    actor: str
    timestamp: Optional[datetime]
    action: str           # 'note_added', 'email_sent', 'field_updated'
    details: str
```

**Logique de suppression :**
- Si une section a deja ete communiquee < 48h -> supprimer de la reponse
- Exception : si l'intention du candidat demande explicitement la section (`INTENT_PROTECTS_SECTION`)
- Guard rail : si un humain est intervenu apres le dernier META -> reset toutes les suppressions

**Champs CRM suivis :** `Evalbox`, `Session`, `Session_souhait_e`, `Date_examen_VTC`, `IDENTIFIANT_EVALBOX`, `Stage`, `Date_de_depot_CMA`, `Frais_Examen`, `PAYE_EN_PROD`

#### `conversation_analyzer.py` (494 lignes)

ThreadMemory V3 -- Intelligence conversationnelle via LLM.

**Modele :** `claude-sonnet-4-5-20250929` (`MODEL_CONVERSATION`)

**Short-circuit :** Si le ticket n'a qu'un seul thread entrant, retourne un `ConversationState` vide (0ms, 0 cout).

```python
@dataclass
class ConversationState:
    conversation_mode: str   # 'initial_contact', 'confirmation', 'clarification',
                             # 'status_check', 'insistence', 'new_topic',
                             # 'follow_up', 'complaint', 'gratitude'
    response_mode: str       # 'full', 'brief_confirmation', 'targeted', 'status_update'
    commitments: List[Commitment]          # Engagements pris
    candidate_decisions: List[CandidateDecision]  # Decisions du candidat
    target_date: str
    target_session: str
    human_is_handling: bool
    proposed_dates: List[str]
    proposed_sessions: List[str]
```

**Impact sur le pipeline :**
- `response_mode='brief_confirmation'` -> supprime dates/sessions dans le template
- `response_mode='targeted'` -> reponse ciblee
- `human_is_handling=True` -> le workflow peut s'arreter
- `target_date` -> utilise pour filtrer les sessions

**Degradation :** V3 LLM -> V2 ThreadMemory -> V2 META -> pas de memoire

#### `uber_eligibility_helper.py` (415 lignes)

Verification de l'eligibilite a l'offre Uber 20EUR.

**Cas geres :**
- **PROSPECT** : Stage = EN ATTENTE (pas encore paye)
- **CAS A** : Offre payee + Date_Dossier_recu vide (documents manquants)
- **CAS B** : Documents envoyes + test de selection non passe (si > 19/05/2025)
- **CAS D** : Compte_Uber = false (email discordant, apres J+4)
- **CAS E** : ELIGIBLE = false (non eligible Uber, apres J+4)
- **ELIGIBLE** : Toutes les verifications OK

#### `exament3p_playwright.py` (1 017 lignes)

Extraction automatique des donnees ExamT3P via Playwright (Chromium headless).

**Classe :** `ExamenT3PPlaywright`

**Donnees extraites :**
- Vue d'ensemble : statut dossier, progression, actions requises
- Mes Examens : dates, convocation
- Mes Documents : statut de chaque piece justificative
- Mon Compte : informations personnelles
- Mes Paiements : historique complet
- Messages : echanges avec la CMA

**Features :**
- Retry automatique (3 tentatives, delai 2s)
- Timeouts configurables (page 30s, element 10s)
- Deploye dans Docker avec Chromium (`PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright`)

#### `ticket_info_extractor.py` (716 lignes)

Extraction d'informations depuis les threads du ticket.

**Fonctions principales :**
- `extract_confirmations_from_threads()` : Detection des confirmations candidat (date, session, preference)
- `extract_cab_proposals_from_threads()` : Extraction des propositions precedentes de CAB
- `detect_candidate_references()` : Detection de references a d'autres candidats
- `detect_dossier_completion_request()` : Detection demande de completion de dossier

**Patterns de confirmation :**
```python
CONFIRMATION_PATTERNS = {
    'date_examen': [r"confirm[ee]?\s+pour\s+le\s+(\d{1,2}[/.-]\d{1,2})", ...],
    'session_preference': [r"cours\s+du\s+(jour|soir)", ...],
    'session_confirmation': [r"confirm[ee]?\s+la\s+session", ...],
    'report_request': [r"reporter\s+mon\s+examen", ...],
}
```

#### Autres utils

| Fichier | Lignes | Description |
|---------|--------|-------------|
| `response_humanizer.py` | 452 | Reformulation IA des reponses templates |
| `crm_note_logger.py` | 492 | Generation des notes CRM detaillees |
| `cross_department_helper.py` | 332 | Dates d'examen dans d'autres departements |
| `date_filter.py` | 332 | Filtre final sur les dates proposees |
| `date_utils.py` | 238 | Parsing de dates multi-format |
| `date_confirmation_extractor.py` | 119 | Extraction de date confirmee par le candidat |
| `exament3p_extractor.py` | 105 | Extraction de donnees depuis HTML ExamT3P |
| `examt3p_credentials_helper.py` | 1 038 | Validation des identifiants ExamT3P |
| `examt3p_crm_sync.py` | 776 | Synchronisation ExamT3P -> CRM |
| `intent_parser.py` | 220 | Parsing des intentions du triage |
| `training_exam_consistency_helper.py` | 767 | Coherence formation/examen |
| `alerts_helper.py` | 257 | Gestion des alertes temporaires |
| `text_utils.py` | 75 | Nettoyage HTML et extraction contenu |
| `logging_config.py` | 52 | Configuration centralisee du logging |
| `business_rules.py` | (externe) | Regles metier custom (non versionne) |

---

### src/constants/

Toutes les constantes metier centralisees (externalisees en fevrier 2026).

| Module | Lignes | Contenu | Exemple |
|--------|--------|---------|---------|
| `models.py` | 8 | 5 IDs de modeles IA | `MODEL_TRIAGE = "claude-sonnet-4-20250514"` |
| `evalbox.py` | 58 | 12 frozensets de statuts + mapping display | `PAID_STATUSES`, `BLOCKING_MODIFICATION`, `STATUT_DISPLAY` |
| `thresholds.py` | 16 | 13 seuils temporels | `EXAM_WITHIN_DAYS = 30`, `RECENT_PROPOSAL_HOURS = 48` |
| `amounts.py` | 7 | 5 montants metier | `UBER_OFFER_AMOUNT = 20`, `CMA_EXAM_FEE = 241` |
| `sessions.py` | 31 | Types/horaires sessions + `is_uber_visio_session()` | `SESSION_HOURS = {'jour': '8h30-17h30', 'soir': '18h-22h'}` |
| `intents.py` | 29 | 8 frozensets d'intentions nommees | `FULL_RECAP_INTENTS`, `DATE_CONFIRMATION_INTENTS` |
| `keywords.py` | 28 | Charge 17 listes depuis `config/keywords.yaml` | `ANNULATION_KEYWORDS`, `SPAM_KEYWORDS` |
| `urls.py` | 12 | 6 URLs externes + 2 templates Zoho | `EXAMT3P_URL`, `CAB_ELEARNING_URL` |
| `departments.py` | 7 | 5 noms de departements Zoho Desk | `DEPT_DOC = "DOC"`, `DEPT_CONTACT = "Contact"` |
| `emails.py` | 26 | Emails systeme, domaines internes, signature | `SYSTEM_EMAILS`, `INTERNAL_DOMAINS`, `COMPANY_SIGNATURE` |
| `deal_stages.py` | 5 | Noms des etapes de deal CRM | `STAGE_WON = "GAGNE"` |

**Donnees externalisees :**
- `config/keywords.yaml` -- 17 listes de mots-cles (source unique)
- `data/geography.json` -- `DEPT_TO_REGION` (94 departements), `CITY_TO_REGION` (73 villes), `REGION_ALIASES` (34 alias)

---

## State Engine

### Etats Candidat (`states/candidate_states.yaml`, 1 438 lignes)

Definit **41+ etats** organises par categorie et priorite.

**Categories d'etats :**

| Priorite | Categorie | Exemples d'etats |
|----------|-----------|------------------|
| 1-99 | Triage | SPAM, ROUTE, DUPLICATE_UBER, CANDIDATE_NOT_FOUND |
| 100-199 | Analyse | CREDENTIALS_INVALID, EXAMT3P_DOWN, DOUBLE_ACCOUNT |
| 200-299 | Uber | PROSPECT, CAS_A, CAS_B, CAS_D, CAS_E |
| 300-399 | Date examen | DATE_EMPTY, DATE_PAST_PRE_VALIDATION, DATE_FUTURE_VALIDE_CMA, ... |
| 400-499 | Intention | Specifiques a certaines intentions |
| 500-599 | Coherence | TRAINING_MISSED, REFRESH_SESSION, DOSSIER_NOT_RECEIVED |
| 600-699 | Blocage | DATE_MODIFICATION_BLOCKED |
| 900+ | Defaut | GENERAL |

**Structure d'un etat :**
```yaml
EXAM_DATE_ASSIGNED_WAITING:
    id: "D-5"
    priority: 305
    severity: "INFO"
    description: "Date future, dossier en cours d'instruction CMA"
    category: "date_examen"
    detection:
      method: "date_examen_case"
      case: 5
    workflow:
      action: "CONTINUE"
    response:
      template: "response_master.html"
      generate: true
```

### Systeme de Severite

| Severite | Comportement |
|----------|-------------|
| `BLOCKING` | Stoppe le workflow, seul cet etat est traite |
| `WARNING` | Alertes ajoutees a la reponse, workflow continue |
| `INFO` | Informatif, combinable avec d'autres etats |

---

## Systeme de Templates

### Template Master (`states/templates/response_master.html`, 426 lignes)

Structure modulaire en 6 sections :

```
1. Salutation personnalisee     {{> salutation_personnalisee}}
2. Direct Answer                {{#if direct_answer}}
3. Context                      {{> partials/context/previous_communication}}
---
SECTION 0 : Alertes prioritaires
   - Uber CAS A/B/D/E           {{#if uber_cas_a}}...{{/if}}
   - Resultats examen            {{#if resultat_admis}}...{{/if}}
   - Report bloque/possible      {{#if report_bloque}}...{{/if}}
   - Credentials                 {{#if credentials_invalid}}...{{/if}}
---
SECTION 1 : Reponse a l'intention
   - 36 blocs conditionnels      {{#if intention_xxx}}{{> partials/intentions/xxx}}{{/if}}
---
SECTION 2 : Statut dossier
   - 8 statuts Evalbox            {{#if evalbox_xxx}}{{> partials/statuts/xxx}}{{/if}}
---
SECTION 3 : Action requise
   - 10 actions possibles        {{> partials/actions/xxx}}
---
SECTION 4 : Ressources
   - E-learning, dates, sessions
---
SECTION 5 : Signature
   {{> signature}}
```

### Organisation des Partials (93 fichiers)

```
states/templates/partials/
  actions/          (10)  choisir_date, choisir_session, preparer_examen, ...
  alerts/           (2)   formation_manquee_repositionnement, missed_training_force_majeure
  alternatives/     (?)   options departements alternatifs
  cma/              (?)   informations CMA
  common/           (2)   exam_date_line, liste_documents_requis
  confirmations/    (?)   auto_assigned, etc.
  context/          (?)   previous_communication, month_alternatives
  credentials/      (?)   invalid, inconnus
  dates/            (1)   proposition
  documents/        (?)   permis_etranger, carte_sejour_expiree, hebergement
  intentions/       (36)  toutes les intentions (rapport 1:1 avec les flags)
  prospect/         (?)   rappel_inscription
  report/           (4)   bloque, possible, force_majeure, deja_effectue
  resultats/        (?)   admis, non_admis, absent, admissible, non_admissible
  statuts/          (8)   dossier_cree, pret_a_payer, valide_cma, etc.
  uber/             (10)  cas_a, cas_b, cas_d, cas_e, doublon, prospect, etc.
  warnings/         (2)   session_assignment_error, personal_account_warning
```

### Matrice Etat x Intention (`state_intention_matrix.yaml`, 2 817 lignes)

**Structure :**
```yaml
# Section 1 : Definition des 50+ intentions
intentions:
  REPORT_DATE:
    id: "I08"
    description: "Veut changer sa date d'examen"
    triggers: ["reporter", "changer ma date", ...]

# Section 2 : Matrice (125+ entrees)
matrix:
  "EXAM_DATE_ASSIGNED_WAITING:REPORT_DATE":
    template: "response_master.html"
    context_flags:
      intention_report_date: true
      show_dates_section: true

  "*:REPORT_DATE":           # Wildcard -- s'applique a tout etat
    template: "response_master.html"
    context_flags:
      intention_report_date: true
```

**Logique de selection :**
1. Chercher `"ETAT:INTENTION"` exact
2. Chercher `"*:INTENTION"` wildcard
3. Fallback : template par defaut de l'etat
4. Dernier fallback : `response_master.html` generique

**Regle 13 :** Chaque intention DOIT avoir une entree wildcard `"*:INTENTION"` sinon elle sera detectee par le triage mais jamais rendue par le template engine.

---

## ThreadMemory (V1/V2/V3)

### V1 : META Lines dans les Notes CRM

**Principe :** Chaque execution du workflow ajoute une note CRM avec une ligne `[META]` qui encode l'etat, l'intention, les sections communiquees, etc.

**Format :**
```
[META] ticket=198709000449479828 | state=EXAM_DATE_ASSIGNED_WAITING | intent=REPORT_DATE | evalbox=Dossier Synchronise | date_exam=2026-05-26 | date_case=5 | session=cds-13/04-24/04 | sections=dates,sessions,statut | response_mode=full
```

**Utilisation :** Lors du traitement d'un nouveau ticket, les META precedents sont lus pour :
- Supprimer les sections deja communiquees (anti-repetition)
- Detecter les relances (meme intention repetee)
- Detecter les changements de contexte

### V2 : Timeline API

**API :** Zoho CRM v8 `__timeline` endpoint

**Donnees extraites :**
- `FieldChange` : changements de champs CRM avec ancien/nouveau valeur, acteur, source
- `HumanIntervention` : actions manuelles d'agents humains

**Guard rail :** Si un humain est intervenu apres le dernier META -> reset toutes les suppressions (l'humain a peut-etre change le contexte).

### V3 : Analyse Conversationnelle LLM

**Modele :** `claude-sonnet-4-5-20250929` (~0.01-0.02 USD/ticket, ~3-6s latence)

**Short-circuit :** 1 seul thread entrant -> pas d'appel LLM (ConversationState vide)

**Impact sur les templates :**

| `response_mode` | Comportement |
|-----------------|-------------|
| `full` | Tout affiche |
| `brief_confirmation` | Supprime dates/sessions, reponse courte |
| `targeted` | Reponse ciblee sur la question |
| `status_update` | Force la section statut |

### Chaine de Degradation

```
V3 (LLM Conversation) -> V2 (Timeline) -> V1 (META) -> Pas de memoire
```

Chaque niveau degrade gracieusement si le precedent echoue. Les erreurs ne bloquent jamais le workflow.

---

## Integrations Externes

### Zoho Desk API v1

**Base URL :** `https://desk.zoho.{datacenter}/api/v1`

**Endpoints utilises :**
- `GET /tickets/{id}` : Recuperation ticket
- `GET /tickets` : Liste tickets par statut/departement
- `PATCH /tickets/{id}` : Mise a jour ticket
- `GET /tickets/{id}/threads` : Threads du ticket
- `GET /tickets/{id}/threads/{threadId}` : Detail d'un thread
- `POST /tickets/{id}/draftReply` : Creation brouillon
- `POST /tickets/{id}/sendReply` : Envoi direct
- `POST /tickets/{id}/comments` : Ajout note interne
- `PATCH /tickets/{id}/move` : Deplacement departement

**Authentification :** OAuth2 avec refresh token. Token gere par `ZohoTokenManager` singleton.

### Zoho CRM API v3

**Base URL :** `https://www.zohoapis.{datacenter}/crm/v3`

**Modules utilises :**
- `Potentials` (Deals) : Deal du candidat
- `Contacts` : Donnees du contact
- `Dates_Examens_VTC_TAXI` : Dates d'examen (module custom)
- `Sessions1` : Sessions de formation (module custom)
- `Notes` : Notes CRM (META lines)

**Endpoints specifiques :**
- `GET /crm/v3/{module}/search` : Recherche avec criteres
- `GET /crm/v3/{module}/{id}` : Recuperation record
- `PATCH /crm/v3/{module}/{id}` : Mise a jour record
- `GET /crm/v8/Potentials/{id}/__timeline` : Timeline API (V2)

### Anthropic API (Claude)

**Modeles utilises :**

| Constante | Modele | Usage | Cout estime |
|-----------|--------|-------|-------------|
| `MODEL_TRIAGE` | `claude-sonnet-4-20250514` | Triage + detection intention | ~0.001 USD |
| `MODEL_HUMANIZER` | `claude-sonnet-4-20250514` | Reformulation reponse | ~0.036 USD |
| `MODEL_EXTRACTION` | `claude-3-5-haiku-20241022` | Extraction email, identifiants | ~0.001 USD |
| `MODEL_CONVERSATION` | `claude-sonnet-4-5-20250929` | Analyse conversation V3 | ~0.01-0.02 USD |
| `MODEL_PERSONALIZATION` | `claude-sonnet-4-5-20250929` | Personnalisation IA | ~0.01 USD |

**Cout total estime par ticket :** ~0.05-0.06 USD

### ExamT3P (Playwright)

**URL :** `https://www.exament3p.fr`

**Technologie :** Playwright Chromium headless

**Deploiement :** Docker (`PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright`)

**Donnees extraites :**
- Statut du dossier (progression, actions requises)
- Dates d'examen et convocation
- Statut de chaque piece justificative
- Historique des paiements CMA
- Messages avec la CMA

---

## Configuration et Deploiement

### Configuration (`config.py`, 72 lignes)

Classe `Settings` basee sur `pydantic_settings.BaseSettings`. Chargement depuis `.env`.

**Variables d'environnement requises :**
```
ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN
ZOHO_DESK_ORG_ID
ZOHO_CRM_CLIENT_ID, ZOHO_CRM_CLIENT_SECRET, ZOHO_CRM_REFRESH_TOKEN
ANTHROPIC_API_KEY
ZOHO_DESK_EMAIL_DOC, ZOHO_DESK_EMAIL_CONTACT, ZOHO_DESK_EMAIL_COMPTA
ZOHO_WEBHOOK_SECRET
```

### Deploiement Docker (Render)

```dockerfile
FROM python:3.11-slim
# Dependances Playwright (libnss3, libatk, etc.)
# pip install + playwright install chromium
EXPOSE 10000
CMD ["gunicorn", "webhook_server:app", "--bind", "0.0.0.0:10000",
     "--workers", "2", "--timeout", "120"]
```

### Modes d'execution

| Mode | Script | Description |
|------|--------|-------------|
| Webhook | `webhook_server.py` | Serveur Flask, traitement en arriere-plan |
| Continu | `run_workflow_continuous.py` | Boucle sur tickets DOC ouverts |
| Batch | `run_workflow_batch.py` | Traitement d'une liste de tickets |
| Test unitaire | `test_doc_workflow_with_examt3p.py` | Test d'un ticket specifique |

---

## Structures de Donnees Critiques

### `context_data` -- Porteur de Donnees Principal

Le dict `context_data` est le porteur principal de donnees entre les etapes 2 et 3. Il est construit dans `_run_state_driven_response()` (ligne ~5400) et passe au `TemplateEngine`.

```python
context_data = {
    # Donnees CRM
    'deal_data': Dict,
    'contact_data': Dict,
    'enriched_lookups': Dict,

    # Triage
    'detected_intent': str,
    'secondary_intents': List[str],
    'session_preference': str,
    'intent_context': Dict,

    # ExamT3P
    'examt3p_data': Dict,
    'credentials_source': str,
    'evalbox_status': str,

    # Date examen
    'date_case': int,
    'date_examen': str,
    'next_dates': List[Dict],
    'date_is_past': bool,

    # Sessions
    'session_data': Dict,
    'proposed_sessions': List,
    'matched_session_start': str,
    'matched_session_end': str,

    # Uber
    'uber_case': str,
    'uber_20': bool,
    'is_prospect': bool,
    'uber_eligible': bool,

    # Resultat
    'resultat_info': Dict,
    'dossier_termine': bool,
    'resultat_admis': bool,
    'resultat_non_admis': bool,

    # ThreadMemory
    'thread_memory': ThreadMemoryResult,
    'conversation_state': ConversationState,
    'response_mode': str,

    # Flags matrice (injectes par _inject_context_flags)
    'show_dates_section': bool,
    'show_sessions_section': bool,
    'show_statut_section': bool,
    'intention_report_date': bool,
    ...
}
```

**Point critique :** Les variables du `context_data` ne sont PAS automatiquement disponibles dans les templates. Elles doivent etre explicitement ajoutees dans `_prepare_placeholder_data()` (whitelist).

### `TriageResult` (retour de `_run_triage`)

Voir section STEP 1 ci-dessus.

### `AnalysisResult` (retour de `_run_analysis`)

Voir section STEP 2 ci-dessus.

### `DetectedState` / `DetectedStates`

Voir section `state_detector.py` ci-dessus.

### `MetaRecord` (ThreadMemory V1)

Voir section ThreadMemory V1 ci-dessus.

### `ConversationState` (ThreadMemory V3)

Voir section ThreadMemory V3 ci-dessus.

### `ValidationResult` / `ValidationError`

```python
class ValidationResult:
    valid: bool
    errors: List[ValidationError]       # severity='error' -> invalide
    warnings: List[ValidationError]     # severity='warning' -> attention
    checks_passed: List[str]

class ValidationError:
    error_type: str      # 'forbidden_term', 'date_mismatch', etc.
    message: str
    severity: str        # 'error', 'warning', 'info'
    location: Optional[str]
```

### `CRMUpdateResult`

```python
class CRMUpdateResult:
    updates_applied: Dict[str, Any]   # champ -> nouvelle valeur
    updates_blocked: Dict[str, str]   # champ -> raison du blocage
    updates_skipped: Dict[str, str]   # champ -> raison du skip
    errors: List[str]
```

---

## Regles Metier Critiques

### Separation Template / Humanizer

Le pipeline de reponse suit une separation stricte :
- **Template Engine** : toute la logique metier, les donnees factuelles, les explications
- **Response Humanizer** : uniquement la reformulation, les transitions, le ton

Si une information metier manque dans la reponse, elle doit etre ajoutee dans le template, JAMAIS dans le Humanizer.

### Blocage de Modification de Date

Si `Evalbox` in `{VALIDE CMA, Convoc CMA recue}` ET `Date_Cloture_Inscription` est passee, la modification de `Date_examen_VTC` est bloquee. Seule une force majeure peut debloquer.

### Doublon Uber vs Autres Demandes

Un candidat avec un deal Uber 20EUR GAGNE peut contacter pour une autre raison (CPF, France Travail). La logique doublon ne s'applique que pour les demandes liees a l'offre Uber. Les keywords `NON_UBER_REGISTRATION` declenchent un routage vers Contact.

### Dossier Termine (`dossier_termine`)

Quand `Resultat` CRM indique post_exam (`ADMIS`, `NON ADMIS`, `ABSENT`) ou closed (`CONVOC PAS RECU`, `PLUS INTERESSE`), le flag `dossier_termine=True` :
- Bloque les mises a jour CRM (Date_examen_VTC, Session, Preference_horaire)
- Supprime les sections dates/sessions/actions/elearning dans les templates
- Exception : REPORT_DATE/DEMANDE_REINSCRIPTION reactive les dates pour les NON ADMIS

### Sessions Filtrees par Date d'Examen

Les sessions proposees doivent se terminer AVANT la date d'examen du candidat (Regle 16). Une session de septembre ne peut pas etre proposee pour un examen en mai.

---

## Fichiers de Reference

| Fichier | Description |
|---------|-------------|
| `states/candidate_states.yaml` | 41+ etats candidat (source de verite) |
| `states/state_intention_matrix.yaml` | 50+ intentions + 125+ entrees matrice |
| `states/templates/response_master.html` | Template master modulaire |
| `states/templates/partials/**/*.html` | 93 partials HTML |
| `states/VARIABLES.md` | Variables Handlebars disponibles |
| `config/keywords.yaml` | 17 listes de mots-cles |
| `data/geography.json` | Mapping departements/villes/regions |
| `config.py` | Configuration Pydantic Settings |
| `webhook_server.py` | Serveur Flask webhook |
| `business_rules.py` | Regles metier custom (non versionne) |
