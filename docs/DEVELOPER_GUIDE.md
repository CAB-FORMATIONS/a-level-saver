# Guide du Developpeur -- A-Level Saver

Guide pratique pour le developpement au quotidien. Pour l'architecture globale, voir `TECHNICAL_OVERVIEW.md`. Pour le deploiement, voir `DEPLOYMENT.md`.

---

## Setup Environnement Local

### Pre-requis

- Python 3.11+
- Acces API Zoho (Desk + CRM) et Anthropic

### Installation

```bash
# Cloner le repo
git clone <repo-url>
cd a-level-saver

# Installer les dependances
pip install -r requirements.txt

# Installer Playwright (scraping ExamT3P)
playwright install chromium
```

### Configuration `.env`

Copier `.env.example` et remplir les valeurs :

```bash
cp .env.example .env
```

Variables requises :

| Variable | Description |
|----------|-------------|
| `ZOHO_CLIENT_ID` | Client ID OAuth Zoho |
| `ZOHO_CLIENT_SECRET` | Client secret OAuth Zoho |
| `ZOHO_REFRESH_TOKEN` | Refresh token OAuth Zoho |
| `ZOHO_DESK_ORG_ID` | ID organisation Zoho Desk |
| `ANTHROPIC_API_KEY` | Cle API Anthropic (Claude) |

Variables optionnelles (emails de reponse par departement) :

| Variable | Defaut |
|----------|--------|
| `ZOHO_DESK_EMAIL_DOC` | `doc@cab-formations.fr` |
| `ZOHO_DESK_EMAIL_CONTACT` | `contact@cab-formations.fr` |
| `ZOHO_DESK_EMAIL_COMPTA` | `compta@cab-formations.fr` |
| `ZOHO_WEBHOOK_SECRET` | Secret pour le webhook Flask |

### Verifier le setup

```bash
# Test rapide sur un ticket (mode dry-run = aucune modification)
python test_doc_workflow_with_examt3p.py 198709000447309732 --dry-run
```

---

## Ajouter une Nouvelle Intention (Checklist Complete)

C'est l'operation la plus frequente. Il y a 6 fichiers a modifier, dans cet ordre exact.

### Etape 1 : `src/agents/triage_agent.py` -- SYSTEM_PROMPT

Ajouter l'intention dans la liste du prompt. Exemple existant :

```python
# Dans SYSTEM_PROMPT, section "INTENTIONS POSSIBLES"
- QUESTION_EXAMEN_PRATIQUE: Candidat demande des infos sur l'examen pratique
  Exemples: "examen pratique", "conduite", "épreuve pratique"
```

L'intention doit aussi apparaitre dans le schema JSON de reponse (plus bas dans le meme fichier) pour que le triage puisse la retourner.

### Etape 2 : `states/state_intention_matrix.yaml` -- Definition + Wildcard

Deux ajouts dans le meme fichier :

**a) Definition de l'intention** (section `intentions:`, debut du fichier) :

```yaml
intentions:
  # ...
  MA_NOUVELLE_INTENTION:
    id: "I99"
    description: "Description courte"
    triggers:
      - "mot cle 1"
      - "mot cle 2"
```

**b) Wildcard** (section `matrix:`) -- permet a l'intention de fonctionner dans TOUS les etats :

```yaml
  "*:MA_NOUVELLE_INTENTION":
    template: "response_master.html"
    description: "Description pour le debug"
    context_flags:
      intention_ma_nouvelle_intention: true
      show_dates_section: false      # Optionnel: supprimer section dates
      show_sessions_section: false   # Optionnel: supprimer section sessions
```

**Verification obligatoire** :
```bash
grep "MA_NOUVELLE_INTENTION" states/state_intention_matrix.yaml
grep '"*:MA_NOUVELLE_INTENTION"' states/state_intention_matrix.yaml
```

### Etape 3 : `states/templates/partials/intentions/<nom>.html` -- Partial

Creer le fichier HTML du partial. Exemple reel (`question_examen_pratique.html`) :

```html
<!-- Partial: Reponse intention QUESTION_EXAMEN_PRATIQUE -->
<b>Concernant l'examen pratique</b><br>
La formation que vous suivez avec CAB Formations porte sur l'<b>examen theorique VTC</b>.<br>
<br>
L'examen pratique de conduite est une etape distincte, geree directement par la CMA
apres obtention de l'admissibilite a l'examen theorique.<br>
```

Regles de formatage :
- Extension `.html` (jamais `.md`)
- Un `<br>` = retour a la ligne, deux `<br><br>` = nouveau paragraphe
- Utiliser `{{variable}}` pour les donnees dynamiques, `{{#if flag}}...{{/if}}` pour les conditions

### Etape 4 : `states/templates/response_master.html` -- Bloc conditionnel

Ajouter le bloc `{{#if}}` dans la Section 1 (intentions), entre `{{#unless uber_cas_a}}` et `{{/unless}}` :

```html
{{#if intention_ma_nouvelle_intention}}
{{> partials/intentions/ma_nouvelle_intention}}
{{/if}}
```

### Etape 5 : `src/state_engine/template_engine.py` -- 3 ajouts

**a) INTENTION_FLAG_MAP** (~ligne 1522) -- mapper le nom triage vers le flag template :

```python
INTENTION_FLAG_MAP = {
    # ...
    'MA_NOUVELLE_INTENTION': 'intention_ma_nouvelle_intention',
}
```

**b) `_auto_map_intention_flags()`** (~ligne 1612) -- initialiser le flag a False :

```python
flags = {
    # ...
    'intention_ma_nouvelle_intention': False,
}
```

**c) `_prepare_placeholder_data()`** (~ligne 671) -- si le partial utilise des variables specifiques, les ajouter dans le dict `result` :

```python
result = {
    # ...
    'ma_variable_specifique': context.get('ma_variable_specifique', ''),
}
```

### Etape 6 : Tester

```bash
python test_doc_workflow_with_examt3p.py <ticket_id> --dry-run
```

Verifier dans la sortie :
- Le triage detecte bien l'intention : `detected_intent: MA_NOUVELLE_INTENTION`
- Le template est selectionne via matrice : `Template selectionne via matrice: *:MA_NOUVELLE_INTENTION`
- Le partial s'affiche correctement dans la reponse

---

## Ajouter un Nouvel Etat

Les etats representent la situation factuelle du candidat (deterministe, pas d'IA).

### Etape 1 : `states/candidate_states.yaml`

```yaml
states:
  MON_NOUVEL_ETAT:
    id: "S99"
    priority: 250        # Plus le numero est bas, plus c'est prioritaire
    severity: "WARNING"  # BLOCKING, WARNING, ou INFO
    description: "Description de la situation"
    category: "exam"     # triage, uber, exam, formation, etc.

    detection:
      method: "condition"
      conditions:
        - "mon_flag_boolean == True"

    workflow:
      action: "CONTINUE"

    response:
      template: "response_master.html"
```

### Etape 2 : Helper -- poser le flag boolean

Dans le helper concerne (ex: `src/utils/date_examen_vtc_helper.py`) :

```python
result['mon_flag_boolean'] = True
result['donnee_contextuelle'] = valeur  # Donnees pour le template
```

### Etape 3 : `src/state_engine/state_detector.py`

Ajouter dans `_build_context()` pour rendre le flag disponible, puis ajouter la condition dans `_check_condition()`.

### Etape 4 : Template partial

Si l'etat genere une alerte (Section 0), creer le partial dans `states/templates/partials/<categorie>/<nom>.html` et l'ajouter dans `response_master.html`.

### Etape 5 : `src/state_engine/template_engine.py`

Si l'etat a du contenu d'alerte, l'ajouter dans `_generate_alert_content()`.

---

## Modifier une Regle Metier

### Ou vivent les regles metier

| Type de regle | Fichier |
|---------------|---------|
| Seuils temporels | `src/constants/thresholds.py` (ex: `EXAM_WITHIN_DAYS = 30`) |
| Montants | `src/constants/amounts.py` (ex: `UBER_OFFER_AMOUNT = 20`) |
| Statuts Evalbox | `src/constants/evalbox.py` (frozensets `PAID_STATUSES`, `VALIDATED`, etc.) |
| Mots-cles | `config/keywords.yaml` (17 listes) |
| URLs externes | `src/constants/urls.py` |
| Modeles IA | `src/constants/models.py` |
| Types de sessions | `src/constants/sessions.py` |
| Groupes d'intentions | `src/constants/intents.py` |
| Geographie | `data/geography.json` |
| Staff/escalation | `config.py` |

### Exemples concrets

**Changer un seuil temporel** :
```python
# src/constants/thresholds.py
EXAM_WITHIN_DAYS = 30      # Jours avant examen pour alerter
RECENT_PROPOSAL_HOURS = 48  # Heures avant re-proposition de dates
```

**Ajouter un mot-cle** :
```yaml
# config/keywords.yaml
annulation_keywords:
  - "annuler"
  - "résilier"
  - "mon_nouveau_keyword"  # <- ajouter ici
```

**Changer le modele IA du triage** :
```python
# src/constants/models.py
MODEL_TRIAGE = "claude-sonnet-4-20250514"  # Changer ici
```

---

## Modifier un Template

### Syntaxe Handlebars (pybars3)

```html
{{variable}}                         <!-- Remplacement de variable -->
{{> partials/intentions/foo}}        <!-- Inclusion de partial -->
{{#if condition}}...{{/if}}          <!-- Conditionnel -->
{{#unless condition}}...{{/unless}}  <!-- Conditionnel inverse -->
{{#each items}}
  {{this.prop}}                      <!-- Boucle sur une liste -->
{{/each}}
{{else}}                             <!-- Branche else (dans if/unless/each) -->
```

### Partials -- categories existantes

```
states/templates/partials/
  actions/           # Prochaine etape pour le candidat
  alerts/            # Alertes temporaires
  alternatives/      # Dates alternatives
  cma/               # Informations CMA
  common/            # Partials reutilisables (exam_date_line, etc.)
  confirmations/     # Auto-assignation confirmee
  context/           # Communication precedente, alternatives mois
  credentials/       # Identifiants ExamT3P
  dates/             # Informations dates
  documents/         # Questions sur les documents
  intentions/        # Reponses aux intentions (~40 fichiers)
  prospect/          # Rappel inscription prospect
  report/            # Report de date (possible/bloque/force majeure)
  resultats/         # Resultats d'examen (admis, non admis, etc.)
  statuts/           # Statut Evalbox (dossier cree, pret a payer, etc.)
  uber/              # Cas Uber (A, B, D, E, doublon, prospect)
  warnings/          # Avertissements
```

### Ajouter une section dans `response_master.html`

Le template master suit un ordre precis :
1. Salutation
2. `direct_answer` (reponse directe optionnelle)
3. Section 0 : Alertes prioritaires (Uber, resultats, report, credentials)
4. Section 1 : Reponse a l'intention (`{{#if intention_xxx}}`)
5. Section 2 : Statut du dossier (`{{#if show_statut_section}}`)
6. Section 3 : Action requise (`{{#if has_required_action}}`)
7. Section 4 : Dates et sessions (`{{#if show_dates_section}}`)
8. Section 5 : Ressources (e-learning, verifier spams, signature)

### Erreurs frequentes dans les templates

**Double saut de ligne** :
```html
<!-- FAUX -->
<b>Titre</b><br>
<br>
Contenu.<br>

<!-- CORRECT -->
<b>Titre</b><br>
Contenu.<br>
```

**Variable invisible** -- oubli dans `_prepare_placeholder_data()` :
```
Si {{#if ma_var}} ne reagit pas, verifier que ma_var est bien ajoutee
dans _prepare_placeholder_data() (~ligne 671 de template_engine.py).
```

**Partial introuvable** -- extension `.html` obligatoire :
```
Le fichier doit etre states/templates/partials/intentions/foo.html
La reference dans le template est {{> partials/intentions/foo}}
```

---

## Debugging

### 1. Template non affiche

Le template est selectionne dans cet ordre de priorite :

| Priorite | Source | Type |
|----------|--------|------|
| 1 | Matrice STATE:INTENTION (`state_intention_matrix.yaml`) | Moderne |
| 2 | `TEMPLATE_STATE_MAP` (`template_engine.py`) | Legacy |
| 3 | `candidate_states.yaml` -> `response.template` | Legacy |
| 4 | Fallback generique | -- |

```bash
# Verifier si l'entree existe dans la matrice
grep "MON_ETAT:MON_INTENTION" states/state_intention_matrix.yaml
```

Si le log montre "Template: dossier_cree" sans "Template selectionne via matrice" : c'est un fallback legacy. Migrer vers la matrice (voir Regle 14 dans CLAUDE.md).

### 2. Partial non rendu (Handlebars brut dans la sortie)

Cause : erreur de compilation pybars3 (syntaxe invalide dans le partial).

Verifier :
- `{{#if}}` / `{{/if}}` equilibres
- `{{#unless}}` / `{{/unless}}` equilibres
- `{{#each}}` / `{{/each}}` equilibres

### 3. Variable non disponible dans le template

Le pipeline est : `doc_ticket_workflow.py` -> `context_data` -> `_prepare_placeholder_data()` -> template.

**Les variables dans `context_data` ne sont PAS automatiquement disponibles dans les templates.** Il faut les ajouter explicitement dans `_prepare_placeholder_data()` :

```python
# src/state_engine/template_engine.py, dans _prepare_placeholder_data()
result = {
    # ...
    'ma_nouvelle_variable': context.get('ma_nouvelle_variable', False),
}
```

### 4. Mauvais etat detecte

Les etats sont evalues par ordre de priorite (priority dans `candidate_states.yaml`). Le premier dont les conditions sont remplies est selectionne.

```bash
# Lister tous les etats et leur priorite
grep -E "^  [A-Z_]+:|priority:" states/candidate_states.yaml | head -80
```

### 5. Intention non detectee par le triage

Verifier dans l'ordre :
1. L'intention existe dans le SYSTEM_PROMPT de `triage_agent.py`
2. L'intention a un wildcard `"*:INTENTION"` dans `state_intention_matrix.yaml`
3. L'intention est dans `INTENTION_FLAG_MAP` de `template_engine.py`

```bash
grep "MON_INTENTION" src/agents/triage_agent.py
grep '"*:MON_INTENTION"' states/state_intention_matrix.yaml
grep "MON_INTENTION" src/state_engine/template_engine.py
```

### 6. Sessions inconsistantes avec la date d'examen

Les sessions proposees doivent se terminer AVANT la date d'examen. Filtre dans `_flatten_session_options_filtered()` de `template_engine.py`.

### 7. Humanizer qui invente du contenu

Desactiver temporairement pour comparer :
```python
# Dans doc_ticket_workflow.py, passer use_ai=False
```
Si le template brut est correct mais la reponse finale non, c'est le humanizer.

**Regle d'or** : le humanizer ne fait que reformuler. S'il manque une info metier, l'ajouter dans le template, JAMAIS dans le humanizer.

### 8. Flag de la matrice ignore par le code

Verifier la Regle 11 : si la matrice definit un flag, le code ne doit pas le recalculer.

```python
# CORRECT (respecte la matrice)
if 'show_dates_section' in context:
    result['show_dates_section'] = context['show_dates_section']
else:
    result['show_dates_section'] = calcul_dynamique()

# FAUX (ecrase la matrice)
result['show_dates_section'] = calcul_dynamique()
```

Flags proteges : `show_dates_section`, `show_sessions_section`, `show_statut_section`, `show_session_info`.

### 9. Mise a jour CRM bloquee

Si `dossier_termine=True` (Resultat = ADMIS, NON ADMIS, ABSENT, etc.), les mises a jour CRM sont bloquees (Date_examen_VTC, Session, Preference_horaire).

Exception : les intentions REPORT_DATE et DEMANDE_REINSCRIPTION reactivent les mises a jour pour les NON ADMIS.

### 10. Doublon Uber faux positif

Le candidat a un dossier Uber mais contacte pour une autre raison (CPF, France Travail). Verifier la Regle 17 : la logique doublon ne s'applique que pour les demandes liees a l'offre Uber.

### 11. Contamination metadata SalesIQ

Les threads du chat widget incluent "Informations sur le visiteur" avec des donnees techniques. Les mots-cles comme "prise en charge de java" causent de faux positifs. Le code nettoie ce contenu avant les verifications de mots-cles.

### 12. ThreadMemory non fonctionnel

Pipeline de degradation : V3 (LLM conversation_analyzer) -> V2 (Timeline API) -> V1 (META notes) -> pas de memoire.

Verifier que les notes CRM contiennent des lignes `[META]` et que l'API Timeline est accessible.

---

## Tester un Ticket

### Commande de base

```bash
# Test complet avec dry-run (aucune modification CRM/Desk)
python test_doc_workflow_with_examt3p.py <ticket_id> --dry-run

# Test sans creer de brouillon mais avec mise a jour CRM
python test_doc_workflow_with_examt3p.py <ticket_id> --no-draft

# Test sans mise a jour CRM mais avec brouillon
python test_doc_workflow_with_examt3p.py <ticket_id> --no-crm-update

# Traitement bulk de tous les tickets DOC ouverts
python test_doc_workflow_with_examt3p.py --bulk --dry-run --output results.json
```

### Lire la sortie

Dans la sortie du workflow, chercher :
- **Triage** : `action: GO`, `detected_intent: XXX`, `secondary_intents: [...]`
- **Etat** : `Detected state: MON_ETAT (priority: N)`
- **Template** : `Template selectionne via matrice: ETAT:INTENTION -> response_master.html`
- **Validation** : `ResponseValidator: PASS` ou `FAIL` avec details

### Autres scripts utiles

```bash
# Lister les tickets recents
python list_recent_tickets.py

# Afficher la reponse generee pour un ticket
python show_response.py <ticket_id>

# Analyser un lot de tickets
python analyze_lot.py 11 20
```

---

## Response Validator

Le `ResponseValidator` (`src/state_engine/response_validator.py`) verifie la reponse avant envoi.

### Termes interdits

```python
FORBIDDEN_TERMS = [
    'BFS',           # Nom interne du systeme
    'Evalbox',       # Nom interne ExamT3P
    'CDJ',           # Utiliser "Cours du jour"
    'CDS',           # Utiliser "Cours du soir"
    '20€',           # Ne pas mentionner le prix de l'offre
    'Montreuil',     # Adresse interne
    'lookup', 'CRM', 'deal', 'API',
    'ticket_id', 'deal_id', 'module', 'field',
]
```

### Mecanisme `allowed_amounts`

Par defaut, aucun montant n'est autorise dans la reponse. Pour les intentions qui parlent de prix :

```python
# src/workflows/doc_ticket_workflow.py (~ligne 6103)
allowed_amounts = None
if is_uber_related_intent:
    allowed_amounts = [UBER_OFFER_AMOUNT]  # Autorise "20" dans la reponse
```

Pour ajouter une exception : modifier la logique dans `doc_ticket_workflow.py` avant l'appel `validate()`, en important le montant depuis `src/constants/amounts.py`.

---

## Pre-Commit Checks

Le skill `/pre-commit-check` verifie 8 categories de coherence avant chaque commit.

### Les 8 checks

| Check | Verifie |
|-------|---------|
| 1 | **Intention Pipeline** -- 6 points de synchro (triage, matrice, FLAG_MAP, flags init, master template, partial) |
| 2 | **Variable Whitelist** -- variables dans `context_data` presentes dans `_prepare_placeholder_data()` |
| 3 | **Partial References** -- chaque `{{> partials/...}}` pointe vers un fichier existant |
| 4 | **Handlebars Syntax** -- `{{#if}}` / `{{/if}}` equilibres dans chaque fichier HTML |
| 5 | **Section0 Overrides** -- flags Section 0 listes dans `section0_overrides` |
| 6 | **Rule 11 Compliance** -- flags proteges jamais ecrases sans garde context |
| 7 | **Template Variable Ghost** -- pas de variable fantome dans les templates HTML |
| 8 | **Matrix Context Flags** -- `context_flags` de la matrice arrivent aux templates |

### Executer

Invoquer le skill via Claude Code : `/pre-commit-check`

Le skill analyse uniquement les fichiers modifies (via `git diff`) et n'execute que les checks pertinents.

---

## Couts API par Ticket

| Composant | Modele | Cout estime |
|-----------|--------|-------------|
| Extraction identifiants | `claude-3-5-haiku` | ~$0.001 |
| Agent Trieur | `claude-sonnet-4` | ~$0.001 |
| Conversation Analyzer (V3) | `claude-sonnet-4-5` | ~$0.01-0.02 |
| Response Humanizer | `claude-sonnet-4` | ~$0.036 |
| Note CRM next steps | `claude-3-5-haiku` | ~$0.001 |
| **Total** | | **~$0.05-0.06** |

Le Conversation Analyzer (V3) ne s'execute que pour les tickets multi-thread. Les tickets single-thread coutent ~$0.04.

---

## Conventions de Code

### Nommage fichiers

| Type | Convention | Exemple |
|------|-----------|---------|
| Helper Python | `snake_case.py` | `date_examen_vtc_helper.py` |
| Constante Python | `snake_case.py` | `evalbox.py`, `thresholds.py` |
| Template HTML | `snake_case.html` | `confirmation_session.html` |
| Config YAML | `snake_case.yaml` | `state_intention_matrix.yaml` |

### Nommage variables

| Type | Convention | Exemple |
|------|-----------|---------|
| Variable Python | `snake_case` | `date_examen_formatted` |
| Constante Python | `UPPER_CASE` | `UBER_OFFER_AMOUNT`, `MODEL_TRIAGE` |
| Flag template | `snake_case` | `show_dates_section`, `intention_report_date` |
| Etat candidat | `UPPER_CASE` | `READY_TO_PAY`, `EXAM_DATE_ASSIGNED_WAITING` |
| Intention | `UPPER_CASE` | `DEMANDE_IDENTIFIANTS`, `REPORT_DATE` |

### Imports des constantes

Toujours importer depuis `src/constants/` :

```python
from src.constants.models import MODEL_TRIAGE, MODEL_HUMANIZER
from src.constants.amounts import UBER_OFFER_AMOUNT, CMA_EXAM_FEE
from src.constants.evalbox import PAID_STATUSES, VALIDATED
from src.constants.thresholds import EXAM_WITHIN_DAYS, RECENT_PROPOSAL_HOURS
from src.constants.intents import DATES_INTENTS, SESSION_INTENTS
from src.constants.keywords import ANNULATION_KEYWORDS
from src.constants.urls import EXAMT3P_URL, ELEARNING_URL
from src.constants.sessions import SESSION_HOURS, is_uber_visio_session
```

### Separation metier / mise en forme

| Composant | Responsabilite | Ce qu'il ne fait PAS |
|-----------|---------------|----------------------|
| Template Engine | Logique metier, donnees factuelles | Mise en forme naturelle |
| Response Humanizer | Reformulation empathique, fluidite | Ajouter des infos metier |

**Si une info metier manque, l'ajouter dans le template, JAMAIS dans le Humanizer.**

### Parsing des dates

Toujours utiliser le helper flexible, jamais `strptime` directement :

```python
# CORRECT
from src.utils.date_utils import parse_date_flexible
date = parse_date_flexible(date_str)

# FAUX -- echoue si format inattendu
date = datetime.strptime(date_str, "%Y-%m-%d")
```

### Lookups CRM

Les champs lookup retournent `{name, id}`, pas la valeur directe :

```python
# CORRECT
from src.utils.crm_lookup_helper import enrich_deal_lookups
enriched = enrich_deal_lookups(crm_client, deal_data, {})
date = enriched['date_examen']  # '2026-03-31'

# FAUX
date = deal_data['Date_examen_VTC']['name']  # '34_2026-03-31' != vraie date
```
