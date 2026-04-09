# CLAUDE.md - Guide du Projet A-Level Saver

Système d'automatisation des tickets Zoho Desk pour CAB Formations (formation VTC Uber).
Le workflow traite les tickets DOC en utilisant plusieurs agents spécialisés et sources de données.

---

## ARCHITECTURE CRITIQUE : Pipeline de Réponse

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         PIPELINE DE RÉPONSE                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   1. TEMPLATE ENGINE (Déterministe)                                         │
│      ├── Contient TOUTE la logique métier                                   │
│      ├── Données factuelles (dates, liens, identifiants)                    │
│      └── Structure en sections (intentions, statut, dates, actions)         │
│                               ↓                                             │
│   2. RESPONSE HUMANIZER (IA Sonnet)                                         │
│      ├── Reformule pour rendre NATUREL et EMPATHIQUE                        │
│      ├── Fusionne les sections redondantes                                  │
│      └── NE CONTIENT JAMAIS DE RÈGLES MÉTIER                                │
│                               ↓                                             │
│   3. RÉPONSE FINALE                                                         │
│      └── Humaine, structurée, pédagogique, complète                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Règle d'Or : Séparation Métier / Mise en Forme

| Composant | Responsabilité | Ce qu'il NE FAIT PAS |
|-----------|----------------|----------------------|
| **Template Engine** | Logique métier, données factuelles, explications | Mise en forme naturelle |
| **Response Humanizer** | Reformulation empathique, transitions, fluidité | Ajouter des infos métier |

**Si une info métier manque → l'ajouter dans le template, JAMAIS dans le Humanizer.**

---

## 17 RÈGLES CRITIQUES - Ne Jamais Oublier

| # | Règle | Piège à éviter | Détails |
|---|-------|----------------|---------|
| 1 | **Template/Humanizer separation** | Ajouter logique métier dans le Humanizer | `docs/TEMPLATES.md` |
| 2 | **CRM Lookups = appel API extra** | Utiliser `lookup.get('name')` directement | `docs/HELPERS.md` §2 |
| 3 | **Blocage modification date** | Modifier Date_examen_VTC si VALIDE CMA + clôture passée | `docs/BUSINESS_RULES.md` §2 |
| 4 | **Intention duality** | Ajouter intention YAML sans TriageAgent prompt | `docs/STATE_ENGINE.md` |
| 5 | **Uber 20€ one-time** | Ignorer doublon offre Uber (sauf post-examen) | `docs/BUSINESS_RULES.md` §4 |
| 6 | **Multi-severity states** | Combiner états BLOCKING | `docs/STATE_ENGINE.md` |
| 7 | **Mapping ExamT3P→Evalbox** | Mauvais mapping statut | `docs/BUSINESS_RULES.md` §1 |
| 8 | **Date_test_selection READ-ONLY** | Modifier via workflow | `docs/BUSINESS_RULES.md` §5 |
| 9 | **Session preference priority** | Ignorer préférence TriageAgent | `docs/BUSINESS_RULES.md` §6 |
| 10 | **Partials = .html** | Créer partial en .md | `docs/TEMPLATES.md` |
| 11 | **MATRICE = Source de vérité** | Le code Python recalcule les flags de la matrice | Voir §11 ci-dessous |
| 12 | **Anti-répétition = context flags** | Ajouter logique anti-répétition dans Humanizer | Voir §12 ci-dessous |
| 13 | **Wildcard obligatoire** | Intention sans `*:INTENTION` dans matrice | Voir §13 ci-dessous |
| 14 | **JAMAIS de fallback legacy** | Laisser une combinaison STATE:INTENTION tomber sur `base_legacy/` | Voir §14 ci-dessous |
| 15 | **Statuts pré-validation ≠ validés** | Traiter "Dossier Synchronisé" comme validé | Voir §15 ci-dessous |
| 16 | **Sessions filtrées par date examen** | Proposer session septembre pour examen mai | Voir §16 ci-dessous |
| 17 | **Doublon ≠ toutes les demandes** | Répondre "doublon" pour une demande CPF/France Travail | Voir §17 ci-dessous |

---

## PROCESS CHECKLIST : Ajout d'un Nouveau Scénario

| # | Fichier | Action |
|---|---------|--------|
| 1 | `states/candidate_states.yaml` | Définir l'état (id, priority, severity, detection, workflow, response) |
| 2 | `src/utils/<helper>.py` | Poser le flag booléen `True` + données contexte |
| 3 | `src/state_engine/state_detector.py` | `_build_context()` + `_check_condition()` + `_collect_alerts()` |
| 4 | `states/templates/partials/<cat>/<nom>.html` | Créer le template partial (syntaxe Handlebars) |
| 5 | `src/state_engine/template_engine.py` | `_generate_alert_content()` si alerte |

**Détails complets :** `docs/STATE_ENGINE.md`

---

## PROCESS CHECKLIST : Ajout d'une Nouvelle Intention

| # | Fichier | Action |
|---|---------|--------|
| 1 | `src/agents/triage_agent.py` | **OBLIGATOIRE** - Ajouter dans `SYSTEM_PROMPT` + JSON schema |
| 2 | `states/state_intention_matrix.yaml` | Définir l'intention (section `intentions:`) + wildcard `"*:NOM"` |
| 3 | `states/templates/partials/intentions/<nom>.html` | Créer le template partial |
| 4 | `states/templates/response_master.html` | Ajouter `{{#if intention_xxx}}` block |
| 5 | `src/state_engine/template_engine.py` | Ajouter dans `INTENTION_FLAG_MAP` + `_auto_map_intention_flags()` + `_prepare_placeholder_data()` |

**CRITIQUE : Si l'intention n'est pas dans YAML ET TriageAgent → jamais détectée !**

```bash
# Vérification obligatoire AVANT de coder
grep "NOM_INTENTION" states/state_intention_matrix.yaml
grep "NOM_INTENTION" src/agents/triage_agent.py
grep '"\*:NOM_INTENTION"' states/state_intention_matrix.yaml  # Wildcard obligatoire !
```

**RÈGLE 13 : Wildcard obligatoire pour architecture moderne**

| Vérification | Commande | Si absent |
|--------------|----------|-----------|
| Intention définie | `grep "^  NOM:" states/state_intention_matrix.yaml` | Ajouter dans section `intentions:` |
| Wildcard existe | `grep '"\*:NOM"' states/state_intention_matrix.yaml` | Ajouter entrée `"*:NOM_INTENTION"` |
| Triage détecte | `grep "NOM" src/agents/triage_agent.py` | Ajouter dans SYSTEM_PROMPT |

**Si le wildcard `*:INTENTION` n'existe pas → l'intention sera détectée par le triage mais JAMAIS rendue par le template engine !**

**Détails complets :** `docs/STATE_ENGINE.md`

---

## RÈGLE D'OR : Vérification Avant Codage

Avant de coder une fonctionnalité, **TOUJOURS vérifier** si elle existe :

```bash
# Lister états existants (41+)
grep -E "^  [A-Z_]+:" states/candidate_states.yaml | head -50

# Lister intentions existantes (50+)
grep -E "^  [A-Z_]+:" states/state_intention_matrix.yaml | head -60

# Chercher si similaire existe
grep -i "mot_clé" states/candidate_states.yaml
grep -i "mot_clé" states/state_intention_matrix.yaml

# Vérifier helpers existants
ls src/utils/
```

**Si l'intent/état existe (même sous un nom différent) → l'utiliser, NE PAS en créer un nouveau.**

---

## Workflow Principal (8 étapes)

```
0. DRAFT CHECK      → Skip si brouillon existant
1. TRIAGE AGENT     → GO/ROUTE/SPAM/DUPLICATE_UBER + intention + session_preference
2. ANALYSIS         → 7 sources (ticket, deal, ExamT3P, dates, sessions, uber, conversation V3)
3. STATE ENGINE     → STATE×INTENTION → Template + partials (déterministe)
4. HUMANIZER        → Reformulation naturelle (optionnel)
5. CRM UPDATES      → Via CRMUpdateAgent (mapping auto, règles blocage, dossier_termine guard)
6. DRAFT CREATION   → Brouillon Zoho Desk
7. CRM NOTE         → [META] line pour ThreadMemory
8. VALIDATION       → Vérification finale
```

**Détails complets :** `docs/ARCHITECTURE.md`

---

## Sources de Vérité

| Fichier | Contenu |
|---------|---------|
| `states/candidate_states.yaml` | **41 ÉTATS** - severity, detection, workflow |
| `states/state_intention_matrix.yaml` | **50 INTENTIONS** + 125 entrées matrice État×Intention |
| `states/templates/response_master.html` | Template master modulaire (v2.0) |
| `states/templates/partials/**/*.html` | Partials modulaires (intentions, statuts, actions) |
| `states/VARIABLES.md` | Variables Handlebars disponibles |
| `alerts/active_alerts.yaml` | Alertes temporaires (éditable) |

---

## Constants Architecture

Toutes les constantes métier sont centralisées dans `src/constants/` :

| Module | Contenu | Exemple |
|--------|---------|---------|
| `models.py` | IDs modèles IA (5) | `MODEL_TRIAGE`, `MODEL_HUMANIZER` |
| `evalbox.py` | Frozensets statuts (12) + STATUT_DISPLAY | `PAID_STATUSES`, `VALIDATED` |
| `thresholds.py` | Seuils temporels (11) | `EXAM_WITHIN_DAYS`, `RECENT_THREAD_HOURS` |
| `amounts.py` | Montants métier (3) | `UBER_OFFER_AMOUNT=20`, `CMA_EXAM_FEE=241` |
| `sessions.py` | Types/horaires sessions | `SESSION_HOURS`, `is_uber_visio_session()` |
| `intents.py` | Frozensets d'intentions (8) | `DATES_INTENTS`, `SESSION_INTENTS` |
| `keywords.py` | Charge 17 listes depuis `config/keywords.yaml` | `ANNULATION_KEYWORDS` |
| `urls.py` | URLs externes (6) | `EXAMT3P_URL`, `ELEARNING_URL` |

Autres données externalisées :
- `config/keywords.yaml` — 17 listes de mots-clés (source unique)
- `data/geography.json` — DEPT_TO_REGION (94), CITY_TO_REGION (73), REGION_ALIASES (34)
- `config.py` — Staff config : `escalation_agent_id`, `escalation_agent_name`, `rgpd_referent_email`

---

## Resultat CRM et dossier_termine

Le champ `Resultat` du CRM (ADMIS/NON ADMIS/ADMISSIBLE/etc.) est classifié par `_classify_resultat()` :

| Catégorie | Valeurs | dossier_termine |
|-----------|---------|-----------------|
| `pre_exam` | (vide), None | `False` |
| `mid_exam` | ADMISSIBLE | `False` |
| `post_exam` | ADMIS, NON ADMIS, ABSENT | `True` |
| `closed` | CONVOC PAS REÇU, PLUS INTÉRESSÉ | `True` |

**`dossier_termine=True`** → bloque les mises à jour CRM (Date_examen_VTC, Session, Preference_horaire), supprime dates/sessions/actions/elearning dans les templates.

**Exception** : REPORT_DATE/DEMANDE_REINSCRIPTION réactivent les dates pour les NON ADMIS qui veulent repasser.

**Bypass doublon** : Si Resultat indique post-exam (mid_exam/post_exam/closed), le workflow normal s'exécute au lieu du template doublon Uber.

---

## ThreadMemory (V1/V2/V3)

Mémoire persistante inter-tickets via notes CRM `[META]`.

| Version | Source | Données |
|---------|--------|---------|
| V1 | `[META]` lines dans CRM notes | state, intent, evalbox, sections |
| V2 | Timeline API (field changes) | Progression CRM, interventions humaines |
| V3 | `conversation_analyzer.py` (LLM) | conversation_mode, response_mode, commitments |

**Fichiers** : `src/utils/thread_memory.py`, `src/utils/conversation_analyzer.py`

**V3 response_mode** contrôle la visibilité des sections :
- `full` → tout affiché
- `brief_confirmation` → supprime dates/sessions
- `targeted` → réponse ciblée
- `status_update` → force statut

**Règle** : ThreadMemory ne doit JAMAIS écraser un flag défini par la matrice (Rule 11).

---

## Temporal Awareness (Sessions & Exam)

**5 flags session** : `session_upcoming`, `session_in_progress`, `session_finished`, `session_starts_soon`, `days_until_session_start`

**2 flags examen** : `exam_today`, `exam_within_30_days`

Calculés dans `_compute_session_temporal_flags()` de `template_engine.py`. Les templates utilisent ces flags pour adapter le ton (passé/présent/futur).

---

## Pièges Courants (Gotchas)

### 1. Extraction lookups CRM
```python
# FAUX - lookup retourne {name, id}, pas la vraie donnée !
date = deal_data['Date_examen_VTC']['name']  # '34_2026-03-31' ≠ vraie date

# CORRECT - utiliser le helper
from src.utils.crm_lookup_helper import enrich_deal_lookups
enriched = enrich_deal_lookups(crm_client, deal_data, {})
date = enriched['date_examen']  # '2026-03-31' (vraie date)
```

### 2. Extraction intention TriageAgent
```python
# CORRECT
intention = result.get("detected_intent")
session_pref = result.get("intent_context", {}).get("session_preference")

# FAUX (n'existe pas !)
intention = result.get("intent_context", {}).get("intention")
```

### 3. Parsing dates
```python
# FAUX - formats multiples possibles
date = datetime.strptime(date_str, "%Y-%m-%d")

# CORRECT - gère tous les formats
from src.utils.date_utils import parse_date_flexible
date = parse_date_flexible(date_str)
```

### 4. Mise à jour CRM
```python
# FAUX - Date_examen_VTC attend un ID, pas une date string
crm_client.update_deal(deal_id, {'Date_examen_VTC': '2026-03-31'})

# CORRECT - utiliser CRMUpdateAgent qui fait le mapping
from src.agents.crm_update_agent import CRMUpdateAgent
agent = CRMUpdateAgent()
agent.update_from_ticket_response(deal_id, {'Date_examen_VTC': '2026-03-31'}, deal_data)
```

### 5. Blocage modification date
```python
# CRITIQUE - vérifier AVANT de modifier
from src.utils.examt3p_crm_sync import can_modify_exam_date
if not can_modify_exam_date(deal_data, exam_session):
    # BLOQUÉ : Evalbox = VALIDE CMA + clôture passée
    # Nécessite force majeure
```

### 6. Template Engine - pybars3

**Le Template Engine utilise maintenant pybars3** (bibliothèque Handlebars pour Python).

#### Architecture
- `src/state_engine/pybars_renderer.py` - Renderer pybars3 avec cache de partials
- `src/state_engine/template_engine.py` - Orchestration (sélection template, préparation contexte)

#### Syntaxe Handlebars supportée
```html
{{variable}}                    <!-- Remplacement de variable -->
{{> partial_name}}              <!-- Inclusion de partial -->
{{#if condition}}...{{/if}}     <!-- Conditionnel -->
{{#unless condition}}...{{/unless}}  <!-- Conditionnel inversé -->
{{#each items}}{{this.prop}}{{/each}}  <!-- Boucle -->
```

#### 6.1 Comparaison int vs string (départements)
```python
# FAUX - CRM retourne int, mapping utilise string
dept = date_info.get('Departement', '')  # Retourne 11 (int)
DEPT_TO_REGION['11']  # KeyError ou comparaison False

# CORRECT - toujours convertir en string
dept = str(date_info.get('Departement', ''))
```
**Fichiers concernés :** `cross_department_helper.py`

#### 6.2 `next_dates` écrasé à plusieurs endroits
```python
# Le workflow écrit next_dates à 4+ endroits différents !
# Lignes ~2040, ~2121, ~2153 dans doc_ticket_workflow.py

# SOLUTION : Ajouter filtre FINAL juste avant generate_response_multi()
# Voir ligne ~2191 "FILTRE FINAL"
```

#### 6.3 Sauts de ligne dans les templates HTML
```html
<!-- FAUX - Double saut de ligne (trop d'espace) -->
<b>Titre</b><br>
<br>
Contenu du paragraphe.<br>
<br>
Autre paragraphe.<br>

<!-- CORRECT - Un seul <br> entre paragraphes -->
<b>Titre</b><br>
Contenu du paragraphe.<br>
Autre paragraphe.<br>

<!-- CORRECT - <br><br> seulement pour séparer des SECTIONS distinctes -->
<b>Section 1</b><br>
Contenu section 1.<br>
<br>
<b>Section 2</b><br>
Contenu section 2.<br>
```
**Règle :** Un `<br>` = retour à la ligne. Deux `<br><br>` = nouveau paragraphe/section. Ne JAMAIS mettre `<br>` suivi d'une ligne vide dans le template.

#### 6.4 `_prepare_placeholder_data()` = WHITELIST de variables

**CRITIQUE** : Les variables dans `context_data` ne sont PAS automatiquement disponibles dans les templates !

```
doc_ticket_workflow.py          template_engine.py              Template
        │                              │                            │
context_data = {                _prepare_placeholder_data()      {{#if my_var}}
  'my_new_var': True,  ─────►   result = {                        ❌ INVISIBLE !
}                                 'prenom': ...,                 {{/if}}
                                  # my_new_var ABSENT !
                                }
```

**Symptôme** : Variable définie dans le workflow, template ne réagit pas.

**Solution** : Ajouter EXPLICITEMENT dans `_prepare_placeholder_data()` (~ligne 700-945) :
```python
result = {
    ...
    # Ajouter ici
    'my_new_var': context.get('my_new_var', False),
    ...
}
```

**Fichier :** `src/state_engine/template_engine.py`

---

## RÈGLE 11 : MATRICE = Source de Vérité pour les Context Flags

### Le Problème (Bug découvert 2026-01-31)

```
CONFLIT DE SOURCES DE VÉRITÉ :

   MATRICE (YAML)                    CODE (Python)
   ─────────────────                 ─────────────────
   context_flags:                    # _prepare_template_variables()
     show_dates_section: false  ──►  result['show_dates_section'] =
                                        not date_examen and bool(next_dates)
                                     # LE CODE ÉCRASE LA MATRICE !
```

### La Règle

**Si la matrice définit un flag → le code NE DOIT PAS le recalculer.**

```python
# ❌ FAUX - Recalcule toujours, ignore la matrice
result['show_dates_section'] = not date_examen and bool(next_dates)

# ✅ CORRECT - Respecte la matrice si définie, sinon calcul dynamique
if 'show_dates_section' in context:
    result['show_dates_section'] = context['show_dates_section']
else:
    result['show_dates_section'] = not date_examen and bool(next_dates)
```

### Flags concernés

Ces flags peuvent être overridés par la matrice :
- `show_dates_section` - Afficher section dates
- `show_sessions_section` - Afficher section sessions
- `show_statut_section` - Afficher section statut dossier
- `show_session_info` - Afficher infos générales sessions

### Ajouter une nouvelle intention

Quand tu ajoutes une intention qui doit SUPPRIMER une section :

```yaml
# Dans state_intention_matrix.yaml
"*:MA_NOUVELLE_INTENTION":
  template: "response_master.html"
  context_flags:
    intention_ma_nouvelle_intention: true
    show_dates_section: false      # ← Sera respecté par le code
    show_sessions_section: false   # ← Sera respecté par le code
```

---

## RÈGLE 12 : Anti-Répétition = Context Flags, PAS Humanizer

### Le Problème

Le Humanizer ne peut pas fiablement supprimer du contenu redondant car :
1. Il peut échouer la validation (dates manquantes)
2. Il ajoute de la latence et du coût
3. Le résultat est non-déterministe

### La Solution

Détecter la répétition EN AMONT et poser des flags dans le contexte :

```python
# Dans ticket_info_extractor.py
result['sessions_proposed_recently'] = True  # Si sessions déjà envoyées < 48h
result['dates_proposed_recently'] = True     # Si dates déjà envoyées < 48h
result['elearning_shared_recently'] = True   # Si e-learning déjà partagé < 48h

# Dans template_engine.py - respecter ces flags
if context.get('sessions_proposed_recently'):
    result['show_sessions_section'] = False
```

### Flags anti-répétition existants

| Flag | Description | Fichier source |
|------|-------------|----------------|
| `dates_proposed_recently` | Dates proposées < 48h | `ticket_info_extractor.py` |
| `sessions_proposed_recently` | Sessions proposées < 48h | `ticket_info_extractor.py` |
| `dates_already_communicated` | Dates déjà envoyées (any) | `ticket_info_extractor.py` |

### Pattern d'implémentation

```python
# 1. Détection dans ticket_info_extractor.py
session_markers = ["cours du jour", "cours du soir", "session de formation"]
if any(marker in content for marker in session_markers):
    if thread_date > recent_threshold:  # < 48h
        result['sessions_proposed_recently'] = True

# 2. Passage au contexte template dans doc_ticket_workflow.py
'sessions_proposed_recently': analysis_result.get('sessions_proposed_recently', False),

# 3. Utilisation dans template_engine.py
if context.get('sessions_proposed_recently') and is_follow_up_intent:
    result['show_sessions_section'] = False
```

---

## RÈGLE 14 : JAMAIS de Fallback vers Legacy

### Objectif

L'architecture moderne (`response_master.html` + matrice `STATE:INTENTION`) a pour objectif de **supprimer le legacy à terme**. On ne doit JAMAIS revenir vers les templates legacy (`base_legacy/`).

### Le Problème

```
Template Engine - Ordre de sélection :

   PASS 0: Matrice STATE:INTENTION     ✅ Architecture moderne
   PASS 1: for_intention               ⚠️ Transition
   PASS 2: for_condition               ⚠️ Transition
   PASS 3: for_uber_case               ⚠️ Transition
   PASS 4: for_resultat                ⚠️ Transition
   PASS 5: for_evalbox                 ❌ LEGACY ! (base_legacy/)
   Fallback: response_master.html      ✅ Générique moderne
```

Si une combinaison `STATE:INTENTION` n'existe pas dans la matrice, le code peut tomber sur **PASS 5 (Evalbox)** et utiliser un template legacy comme `dossier_cree.html`.

### La Règle

**Si on identifie qu'une entrée tombe sur un template legacy → ALERTER et migrer vers l'architecture moderne.**

### Détection d'un Fallback Legacy

```bash
# Dans les logs du workflow, chercher :
# ❌ LEGACY si le log montre :
#    "Template: dossier_cree" (ou autre template sans "response_master")
#    ET PAS de log "Template sélectionné via matrice"

# ✅ MODERNE si le log montre :
#    "✅ Template sélectionné via matrice: STATE:INTENTION -> response_master.html"
```

### Migration vers Architecture Moderne

Quand on identifie un fallback legacy :

1. **Identifier la combinaison** : quel `STATE` + quelle `INTENTION` ?
2. **Ajouter l'entrée dans la matrice** :

```yaml
# states/state_intention_matrix.yaml
"EXAM_DATE_ASSIGNED_WAITING:CONFIRMATION_PAIEMENT":
  template: "response_master.html"
  context_flags:
    intention_confirmation_paiement: true
    show_statut_section: true
```

3. **Vérifier le partial d'intention** existe : `partials/intentions/<intention>.html`
4. **Tester** et vérifier que le log affiche "Template sélectionné via matrice"

### Templates Legacy à Migrer

| Template Legacy | Evalbox | Migration |
|-----------------|---------|-----------|
| `dossier_cree.html` | Dossier crée | → `response_master.html` + partials |
| `dossier_synchronise.html` | Dossier Synchronisé | → `response_master.html` + partials |
| `pret_a_payer.html` | Pret a payer | → `response_master.html` + partials |
| `docs_refuses.html` | Documents refusés | → `response_master.html` + partials |

### Exceptions (Cas Spéciaux)

Certains templates legacy peuvent temporairement rester actifs pour des cas très spécifiques qui n'ont pas encore de partial moderne. Dans ce cas, **documenter explicitement** pourquoi et créer une issue de migration.

---

## RÈGLE 15 : Statuts Pré-Validation ≠ Validés

### Le Problème

"Dossier Synchronisé" signifie "en cours d'instruction par la CMA", **PAS validé**.

Si date passée + dossier non validé → le candidat a été **auto-reporté** par la CMA, pas "examen passé".

### Classification des Statuts

| Statuts PRÉ-VALIDATION | Statuts VALIDÉS |
|------------------------|-----------------|
| `N/A` | `VALIDE CMA` |
| `Dossier créé` | `Convoc CMA reçue` |
| `Pret a payer` | |
| **`Dossier Synchronisé`** | |

### Impact sur la Logique

```python
# Dans date_examen_vtc_helper.py
VALIDATED_STATUSES = ['VALIDE CMA', 'Convoc CMA reçue']  # PAS Dossier Synchronisé !

if date_is_past:
    if evalbox_status in VALIDATED_STATUSES:
        case = 7  # Examen peut avoir été passé
    else:
        case = 2  # Auto-report sur prochaine date (candidat jamais inscrit réellement)
```

---

## RÈGLE 16 : Sessions Filtrées par Date d'Examen

### Le Problème

On ne peut pas proposer une session de formation en **septembre** pour un examen en **mai**.

### La Règle

Les sessions proposées doivent se terminer **AVANT** la date d'examen du candidat.

### Implémentation

```python
# Dans template_engine.py - _flatten_session_options_filtered()
if primary_intent == 'DEMANDE_CHANGEMENT_SESSION':
    # Filtrer: garder seulement les sessions qui se terminent AVANT l'examen
    filtered = [s for s in sessions if session_end_date < exam_date]
```

### Exemple

- Examen : 26/05/2026
- ✅ Session 13/04 → 24/04 (avant examen)
- ✅ Session 11/05 → 22/05 (avant examen)
- ❌ Session 14/09 → 25/09 (APRÈS examen - filtrer !)

---

## RÈGLE 17 : Doublon ≠ Toutes les Demandes

### Le Problème

Un candidat avec un dossier Uber 20€ peut contacter CAB Formations pour une **autre raison** (formation CPF, France Travail/KAIROS, financement personnel). Dans ce cas, répondre "offre Uber 20€ déjà utilisée" est **complètement hors sujet**.

### La Règle

La logique doublon Uber ne s'applique que pour les demandes **liées à l'offre Uber 20€**. Pour les autres demandes, router vers Contact.

### Keywords Non-Uber

| Catégorie | Keywords |
|-----------|----------|
| CPF | cpf, compte cpf, compte formation, moncompteformation |
| France Travail | france travail, kairos, pole emploi, conseiller |
| Financement perso | 720€, tarif complet, payer moi-même |
| Devis | devis, facture pro forma, proforma |
| Autres | opco, fafcea, agefice, fifpl, fif pl |

### Implémentation

```python
# Dans doc_ticket_workflow.py - _run_triage()
# AVANT la logique doublon, vérifier si demande non-Uber
if is_non_uber_registration and has_duplicate:
    # Router vers Contact (ignorer doublon Uber)
    triage_result['action'] = 'ROUTE'
    triage_result['target_department'] = 'Contact'
    return triage_result

# Continuer avec logique doublon seulement si demande Uber
if linking_result.get('has_duplicate_uber_offer'):
    ...
```

### Exemples

- Candidat A a un dossier Uber 20€ GAGNÉ
- Candidat A écrit : "Je veux m'inscrire avec mon CPF à 720€"
- ❌ FAUX : Répondre "Vous avez déjà utilisé l'offre Uber 20€"
- ✅ CORRECT : Router vers Contact pour traitement formation CPF

- Candidat B a un dossier Uber 20€ GAGNÉ + Resultat = ADMISSIBLE
- Candidat B écrit : "Quand recevrai-je mes résultats ?"
- ❌ FAUX : Répondre "Vous avez déjà utilisé l'offre Uber 20€"
- ✅ CORRECT : Bypass doublon → workflow normal → intention RESULTAT_EXAMEN

**Fichiers :** `docs/GESTION_DOUBLONS_BFS.md`, `src/workflows/doc_ticket_workflow.py`

---

## Cycle de Vie Evalbox et État ExamT3P

### Chronologie des Statuts

```
N/A → Documents manquants → Documents refusés → Dossier créé → Pret a payer → Dossier Synchronisé → VALIDE CMA
 │         │                      │                  │              │                │                    │
 │         │                      │                  │              │                │                    └── Convoc CMA reçue
 │         │                      │                  │              │                │                        (ou Refusé CMA)
 │         │                      │                  │              │                │
 │         └──────────────────────┘                  │              │                └── Paiement 241€ effectué
 │              Pas de compte ExamT3P                │              │                    par CAB Formations
 │              (dossier en cours chez CAB)          │              │
 │                                                   │              └── Compte ExamT3P créé (OUI)
 │                                                   │                  Documents transmis à la CMA
 │                                                   │
 │                                                   └── Compte ExamT3P potentiel
 │                                                       (peut exister mais pas garanti)
 │
 └── Aucun traitement commencé
```

### Tableau de Référence : Evalbox × ExamT3P × Paiement

| Evalbox | Compte ExamT3P | Paiement 241€ | Si clôture passée |
|---------|----------------|---------------|-------------------|
| `N/A` | ❌ Non | ❌ Non | **CAS 8** ✅ (report auto) |
| `Documents manquants` | ❌ Non | ❌ Non | **CAS 8** ✅ (report auto) |
| `Documents refusés` | ❌ Non | ❌ Non | **CAS 8** ✅ (report auto) |
| `Dossier créé` | ⚠️ Potentiel | ❌ Non | **CAS 8** ✅ (report auto) |
| `Pret a payer` | ✅ Oui | ❌ Non | **CAS 8** ✅ (report auto) |
| **`Dossier Synchronisé`** | ✅ Oui | **✅ Oui** | **Vérifier date paiement** |
| `VALIDE CMA` | ✅ Oui | ✅ Oui | **Bloqué** (modification impossible) |
| `Convoc CMA reçue` | ✅ Oui | ✅ Oui | **Bloqué** (modification impossible) |
| `Refusé CMA` | ✅ Oui | ✅ Oui | **CAS 3** (refus définitif) |

### Règles Importantes

1. **CAS 8 (Report automatique)** : Si clôture passée ET pas de paiement → le système reporte automatiquement sur la prochaine date d'examen.

2. **Dossier Synchronisé = Paiement fait** : À ce stade, CAB Formations a payé les 241€ à la CMA. Si clôture passée, on vérifie la date du paiement :
   - Paiement AVANT clôture → dossier était inscrit à temps → **PAS de CAS 8**
   - Paiement APRÈS clôture → dossier inscrit trop tard → **CAS 8**

3. **Statuts bloquants** (`VALIDE CMA`, `Convoc CMA reçue`) : La modification de date n'est plus possible sans force majeure.

### Impact sur le Code

```python
# Dans date_examen_vtc_helper.py
# Vérification AVANT de déclencher CAS 8
if evalbox == 'Dossier Synchronisé' and date_cloture_is_past:
    # Vérifier date paiement dans ExamT3P
    date_paiement = examt3p_data.get('paiement_cma', {}).get('date')
    if date_paiement and date_paiement <= date_cloture:
        # Paiement fait avant clôture → PAS de CAS 8
        pass
    else:
        # Paiement après clôture → CAS 8
        case = 8
```

---

## DEBUGGING : Pistes d'Investigation

### 7. Template non affiché / Partial manquant

**Symptôme:** Une section (ex: confirmation_session) n'apparaît pas dans la réponse.

**Ordre de priorité de sélection du template:**
```
1. MATRICE STATE:INTENTION (state_intention_matrix.yaml)
   Ex: "READY_TO_PAY:CONFIRMATION_SESSION" → response_master.html
   ✅ Architecture moderne avec intentions

2. TEMPLATE_STATE_MAP (template_engine.py ligne ~1007)
   Ex: 'READY_TO_PAY': 'pret_a_payer'
   ❌ Legacy (base_legacy/) - PAS d'intentions !

3. candidate_states.yaml → response.template
   Ex: template: "ready_to_pay.html"

4. Fallback générique
```

**Investigation:**
```bash
# 1. Vérifier si entrée STATE:INTENTION existe
grep "READY_TO_PAY:CONFIRMATION_SESSION" states/state_intention_matrix.yaml

# 2. Vérifier le log "Template sélectionné via matrice"
# Si absent → fallback sur legacy

# 3. Vérifier TEMPLATE_STATE_MAP dans template_engine.py
grep "READY_TO_PAY" src/state_engine/template_engine.py
```

**Solution:** Ajouter l'entrée dans `state_intention_matrix.yaml`:
```yaml
"ETAT:INTENTION":
  template: "response_master.html"
  context_flags:
    intention_xxx: true
```

### 8. Noms d'états incohérents

**Piège:** Le nom d'état diffère entre les fichiers.

| Fichier | Peut utiliser |
|---------|---------------|
| `candidate_states.yaml` | `READY_TO_PAY` |
| `state_intention_matrix.yaml` | `PRET_A_PAYER` (ancien) |
| `TEMPLATE_STATE_MAP` | `'READY_TO_PAY': 'pret_a_payer'` |

**Vérification:**
```bash
# Trouver le vrai nom de l'état
grep -E "^  [A-Z_]+:" states/candidate_states.yaml | grep -i "pay"
```

### 9. Champs CRM Sessions1

**Module Sessions1 - Noms de champs corrects:**
```python
# CORRECT
session_record.get('Date_d_but')   # Date début
session_record.get('Date_fin')      # Date fin (PAS Date_de_fin !)
session_record.get('session_type')  # 'jour' ou 'soir'
session_record.get('Name')          # Nom complet
```

**Fichier:** `src/utils/crm_lookup_helper.py`

### 10. Variables session dans le contexte

**Priorité des données session (fallback chain):**
```python
# 1. Matching nouveau (si CONFIRMATION_SESSION + proposed_options)
matched_session_start = context.get('matched_session_start')
matched_session_end = context.get('matched_session_end')

# 2. Session déjà assignée dans CRM (fallback)
enriched_lookups.get('session_date_debut')
enriched_lookups.get('session_date_fin')
```

**Template Engine (ligne ~725):**
```python
'matched_session_start': context.get('matched_session_start') or enriched_lookups.get('session_date_debut')
```

### 11. Session déjà assignée - Changement bloqué

**Symptôme:** Le candidat veut changer de session (jour→soir) mais le système ne propose rien.

**Cause:** `session_helper.py` retourne immédiatement si session assignée.

**Solution:** Paramètre `allow_change=True` pour bypasser:
```python
session_data = analyze_session_situation(
    ...,
    allow_change=(detected_intent == 'CONFIRMATION_SESSION'),
    enriched_lookups=enriched_lookups
)
```

**Condition de bypass:**
- `allow_change=True` ET
- Préférence exprimée (jour/soir) ET
- Préférence ≠ session actuelle

### 12. Statut "validé" vs "complet"

**Attention au vocabulaire métier:**

| Evalbox | Signification | Terme correct |
|---------|---------------|---------------|
| `Pret a payer` | Documents transmis, en attente paiement | "complet" |
| `Dossier Synchronisé` | CMA a reçu, vérifie | "en cours de vérification" |
| `VALIDE CMA` | CMA a validé après paiement | "validé" |

**Fichier:** `states/templates/partials/statuts/pret_a_payer.html`

### 13. Template ne rend pas (reste en `{{> partials/...}}`)

**Symptôme:** La réponse contient les directives Handlebars brutes au lieu du contenu.

**Cause:** Erreur de compilation d'un partial (syntaxe Handlebars invalide).

**Investigation:**
```bash
# Chercher les erreurs de compilation pybars
grep "Failed to compile partial" logs
```

**Solution:** Corriger la syntaxe du partial - souvent `{{#if}}`/`{{/if}}` mal imbriqués ou `{{#unless}}` mal fermé.

### 14. Sessions incohérentes avec date d'examen

**Symptôme:** Session de septembre proposée pour un examen en mai.

**Cause:** `_flatten_session_options_filtered` ne filtrait pas par date d'examen.

**Règle:** Les sessions proposées doivent se terminer AVANT la date d'examen du candidat.

**Fichier:** `src/state_engine/template_engine.py` - `_flatten_session_options_filtered()`

### 15. Humanizer invente du contenu

**Symptôme:** Réponse finale contient des infos absentes du template brut (ex: dates passées).

**Debug:**
1. Désactiver humanizer temporairement (`use_ai=False` dans `doc_ticket_workflow.py`)
2. Comparer template brut vs réponse finale
3. Si template brut OK → problème humanizer (comportement non-déterministe)

**Note:** Le humanizer peut utiliser le message du candidat comme source de données alors qu'il ne devrait que reformuler le template.

### 16. Ordre de priorité matrice ignoré

**Symptôme:** Un flag défini dans la matrice (`show_dates_section: false`) est ignoré.

**Cause:** Le code vérifie un cas spécial AVANT la matrice.

**Règle:** TOUJOURS vérifier la matrice EN PREMIER :
```python
# ✅ CORRECT
if 'show_dates_section' in context:  # Matrice d'abord
    result['show_dates_section'] = context['show_dates_section']
elif date_case == 2:  # Cas spéciaux ensuite
    result['show_dates_section'] = True

# ❌ FAUX
if date_case == 2:  # Cas spécial écrase la matrice !
    result['show_dates_section'] = True
elif 'show_dates_section' in context:
    ...
```

---

## Fichiers de Référence

| Fichier | Description | Mise à jour |
|---------|-------------|-------------|
| `crm_schema.json` | Schéma modules/champs Zoho CRM | `python extract_crm_schema.py` |
| `desk_departments.json` | Départements Zoho Desk avec IDs | `python list_departments.py` |

---

## Commandes Utiles

```bash
# Lister tickets récents
python list_recent_tickets.py

# Tester workflow complet
python test_doc_workflow_with_examt3p.py <ticket_id>

# Analyser un lot de tickets
python analyze_lot.py 11 20  # Tickets 11-20

# Clôturer tickets SPAM
python close_spam_tickets.py data/lot2_analysis.json --dry-run
python close_spam_tickets.py data/lot2_analysis.json

# Afficher réponse workflow
python show_response.py <ticket_id>
```

---

## Coûts API (estimation par ticket)

| Composant | Modèle | Coût |
|-----------|--------|------|
| Extraction identifiants | Haiku 3.5 | ~$0.001 |
| Agent Trieur | Haiku 3.5 | ~$0.001 |
| Conversation Analyzer (V3) | Sonnet 4.5 | ~$0.01-0.02 |
| Response Humanizer | Sonnet 4.5 | ~$0.036 |
| Next steps note CRM | Haiku 3.5 | ~$0.001 |
| **Total** | | **~$0.05-0.06** |

**Note** : Le Conversation Analyzer ne s'exécute que pour les tickets multi-thread (>1 thread entrant). Les tickets single-thread = $0 (short-circuit).

---

## Documentation Détaillée

### Documentation Handover (complète, autonome)

| Document | Contenu |
|----------|---------|
| `docs/DEPLOYMENT.md` | Dockerfile, variables d'env, Zoho Desk config, monitoring, troubleshooting |
| `docs/FUNCTIONAL_OVERVIEW.md` | Contexte métier, cycle Evalbox, 10 CAS dates, Uber, 50+ intentions, guard rails |
| `docs/TECHNICAL_OVERVIEW.md` | Architecture, pipeline 8 étapes, tous les modules Python, State Engine, ThreadMemory |
| `docs/DEVELOPER_GUIDE.md` | Setup local, checklists intention/état, debugging 12 scénarios, conventions |
| `docs/API_REFERENCE.md` | Webhook endpoints, Zoho Desk/CRM API, Anthropic, ExamT3P HTTP/httpx, OAuth |

### Documentation technique (référence interne)

| Document | Contenu |
|----------|---------|
| `docs/ARCHITECTURE.md` | Structure projet, workflow 8 étapes, data structures |
| `docs/STATE_ENGINE.md` | États, intentions, détection, processes détaillés |
| `docs/AGENTS.md` | Signatures agents, usage, return structures |
| `docs/HELPERS.md` | Tous les helpers avec exemples de code |
| `docs/BUSINESS_RULES.md` | Règles métier complètes (Uber, dates, mappings) |
| `docs/TEMPLATES.md` | Système template, Handlebars, partials, flags |
| `docs/architecture-diagrams.md` | Diagrammes Mermaid |
| `states/VARIABLES.md` | Variables Handlebars disponibles |
