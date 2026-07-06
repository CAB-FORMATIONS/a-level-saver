# State Engine - Documentation Complète

## Principe Fondamental

Le State Engine génère les réponses de manière **déterministe** :

```
ÉTAT × INTENTION = TEMPLATE
```

- **ÉTAT** = situation factuelle du candidat (détecté depuis CRM/ExamT3P)
- **INTENTION** = ce que le candidat demande (détecté par TriageAgent via IA)
- **TEMPLATE** = réponse adaptée à la combinaison

---

## Sources de Vérité

| Fichier | Contenu | Priorité |
|---------|---------|----------|
| `states/candidate_states.yaml` | **42 ÉTATS** avec severity | SOURCE UNIQUE |
| `states/state_intention_matrix.yaml` | **50 INTENTIONS** + matrice | Intentions + mapping |
| `states/templates/response_master.html` | Template master modulaire | Architecture v2.0 |
| `states/templates/partials/**/*.html` | Partials modulaires | Fragments réutilisables |
| `states/templates/base_legacy/*.html` | 66 templates legacy | Fallback DÉSACTIVÉ (PASS 5 commenté) |

---

## Architecture Multi-États (v2.1)

Le State Engine détecte **plusieurs états simultanément** via classification par sévérité :

| Severity | Comportement | Exemples |
|----------|--------------|----------|
| **BLOCKING** | Stoppe le workflow, réponse unique | SPAM, ROUTE_DEPARTMENT, CANDIDATE_NOT_FOUND, EXAMT3P_DOWN, EXAMT3P_ACCESS_LOST, DOUBLE_ACCOUNT_PAID, MISSED_TRAINING_FORCE_MAJEURE |
| **WARNING** | Continue + ajoute alerte | DUPLICATE_UBER, UBER_DOCS_MISSING, UBER_ACCOUNT_NOT_VERIFIED, UBER_NOT_ELIGIBLE |
| **INFO** | Combinables entre eux | EXAM_DATE_EMPTY, CREDENTIALS_INVALID, GENERAL |

### Comportement
- Si `blocking_state` présent → réponse unique, workflow stoppé
- Sinon → combine tous les `warning_states` et `info_states` dans la réponse

---

## Catégories d'États

| Catégorie | Priority | ID Pattern | Exemples |
|-----------|----------|------------|----------|
| Triage | 1-4 | T1-T4 | SPAM, ROUTE, DUPLICATE_UBER, CANDIDATE_NOT_FOUND |
| Analysis | 95-104 | A0-A6 | CREDENTIALS_INVALID, EXAMT3P_DOWN, EXAMT3P_ACCESS_LOST, DOUBLE_ACCOUNT |
| Uber | 200-204 | U-* | PROSPECT, CAS_A, CAS_B, CAS_D, CAS_E |
| Force Majeure + Cohérence | 290 | FM-1 | MISSED_TRAINING_FORCE_MAJEURE |
| Date Examen | 300-310 | D-1 à D-11 | DATE_EMPTY, DATE_PAST, VALIDE_CMA, etc. |
| Intention | 400-408 | I1-I9 | REPORT_DATE, CONFIRMATION_SESSION |
| Cohérence | 500-502 | C1-C3 | TRAINING_MISSED, DOSSIER_NOT_RECEIVED |
| Blocage | 600 | B1 | DATE_MODIFICATION_BLOCKED |
| Défaut | 999 | - | GENERAL |

---

## Détection d'États (`state_detector.py`)

### Méthode principale
```python
from src.state_engine.state_detector import StateDetector

detector = StateDetector()
detected_states = detector.detect_all_states(
    deal_data=deal_data,
    examt3p_data=examt3p_data,
    triage_result=triage_result,
    linking_result=linking_result,
    session_data=session_data,
    training_exam_consistency_data=consistency_data
)

# Structure retournée:
{
    "blocking_state": None,                    # Si présent, stoppe tout
    "warning_states": [DetectedState, ...],   # Alertes à inclure
    "info_states": [DetectedState, ...],      # États combinables
    "primary_state": DetectedState,            # État principal (rétrocompat)
    "all_states": [DetectedState, ...]         # Tous les états détectés
}
```

### Contexte enrichi automatiquement (v2.2)
- `uber_case`: calculé automatiquement depuis deal_data (A/B/D/E/ELIGIBLE)
- `extraction_failed`: True si ExamT3P indisponible
- `error_type`: type d'erreur (connection_failed, etc.)

---

## Détection d'Intentions

### Multi-Intentions (v2.1)
Le TriageAgent détecte **plusieurs intentions simultanément** :

```python
result = triage_agent.triage_ticket(...)

# Structure:
{
    "action": "GO" | "ROUTE" | "SPAM",
    "primary_intent": "DEMANDE_DATES_FUTURES",       # Intention principale
    "secondary_intents": ["QUESTION_SESSION"],       # Intentions secondaires
    "detected_intent": "DEMANDE_DATES_FUTURES",      # Alias rétrocompat
    "intent_context": {
        "is_urgent": True | False,
        "mentions_force_majeure": True | False,
        "force_majeure_type": "medical" | "death" | "accident" | "childcare" | None,
        "force_majeure_details": "description courte" | None,
        "wants_earlier_date": True | False,
        "session_preference": "jour" | "soir" | None
    }
}
```

### Intentions disponibles pour multi-détection
- `DEMANDE_DATES_FUTURES` - Demande de dates d'examen futures
- `QUESTION_SESSION` - Question sur les sessions jour/soir
- `QUESTION_PROCESSUS` - Question sur le processus global
- `DEMANDE_AUTRES_DEPARTEMENTS` - Veut voir d'autres départements
- `STATUT_DOSSIER`, `REPORT_DATE`, `CONFIRMATION_SESSION`, etc.

---

## Sélection de Template (`template_engine.py`)

### Ordre de priorité (PASS 0-5)

```
PASS 0: Matrice STATE:INTENTION
        0a. Match exact "STATE:INTENTION"
        0b. Wildcard "*:INTENTION" si pas de match

PASS 1: Templates avec intention (for_intention + for_condition)

PASS 1.5: Templates avec for_state (état spécifique)

PASS 2: Templates avec condition seule (for_condition)

PASS 3: Cas Uber (A, B, D, E)

PASS 4: Résultat examen (Admis, Non admis, Absent)

PASS 5: Evalbox (statut dossier) — DÉSACTIVÉ (code commenté, template_engine.py:422-428)
        Plus aucun fallback vers base_legacy/ (Règle 14)

FALLBACK: response_master.html
```

### Wildcards (v2.2)
```yaml
# Dans state_intention_matrix.yaml
"*:REPORT_DATE":
  template: "report_possible.html"
  context_flags:
    intention_report_date: true
```

---

## Context Flags

### Flags d'intention (Section 1 du response_master)
- `intention_statut_dossier` - Question sur l'avancement
- `intention_demande_date` - Demande de dates d'examen
- `intention_demande_identifiants` - Demande d'identifiants ExamT3P
- `intention_confirmation_session` - Choix de session jour/soir
- `intention_demande_convocation` - Où est ma convocation ?
- `intention_demande_elearning` - Accès e-learning
- `intention_report_date` - Demande de report
- `intention_probleme_documents` - Problème avec les documents
- `intention_question_generale` - Question générale
- `intention_resultat_examen` - Question sur résultat d'examen
- `intention_question_uber` - Question sur l'offre Uber
- `intention_question_session` - Question sur les sessions jour/soir
- `intention_question_processus` - Question sur le processus global
- `intention_autres_departements` - Demande d'autres départements

### Flags conditions bloquantes (Section 0)
- `uber_cas_a`, `uber_cas_b`, `uber_cas_d`, `uber_cas_e`, `uber_doublon`
- `resultat_admis`, `resultat_non_admis`, `resultat_absent`
- `report_bloque`, `report_possible`, `report_force_majeure`
- `credentials_invalid`, `credentials_inconnus`

### STATE_FLAG_MAP (v2.2)
```python
STATE_FLAG_MAP = {
    'UBER_DOCS_MISSING': ['uber_cas_a'],
    'UBER_TEST_MISSING': ['uber_cas_b'],
    'UBER_ACCOUNT_NOT_VERIFIED': ['uber_cas_d'],
    'UBER_NOT_ELIGIBLE': ['uber_cas_e'],
    'DUPLICATE_UBER': ['uber_doublon'],
    'CREDENTIALS_INVALID': ['credentials_invalid'],
    'CREDENTIALS_UNKNOWN': ['credentials_inconnus'],
    'DATE_MODIFICATION_BLOCKED': ['report_bloque'],
    'EXAM_PASSED': ['resultat_admis'],
    'EXAM_FAILED': ['resultat_non_admis'],
    'EXAM_ABSENT': ['resultat_absent'],
    # ... (20+ états au total)
}
```

---

## PROCESS : Ajout d'un Nouvel État

### Checklist rapide

| # | Fichier | Action |
|---|---------|--------|
| 1 | `states/candidate_states.yaml` | Définir l'état |
| 2 | `src/utils/<helper>.py` | Poser le flag booléen + données contexte |
| 3 | `src/state_engine/state_detector.py` | Contexte + détection + collecte alerte |
| 4 | `states/templates/partials/<cat>/<nom>.html` | Template partial |
| 5 | `src/state_engine/template_engine.py` | Rendu de l'alerte |

### Détails

#### Étape 1 : Définir l'état dans candidate_states.yaml
```yaml
NOM_DU_SCENARIO:
  id: "XX"
  priority: NNN
  description: "Description claire"
  category: "analysis|credentials|uber|report|result"
  severity: "BLOCKING|WARNING|INFO"
  detection:
    method: "helper_source"
    condition: "flag_name == true"
  workflow:
    action: "RESPOND_WITH_ALERT|RESPOND|BLOCK|ROUTE"
    alert_internal: true|false
  response:
    generate: true
    alert_template: "partials/category/template_name.html"
    alert_position: "before_signature"
```

#### Étape 2 : Ajouter le flag dans le helper
```python
# Dans le helper qui détecte la condition
# Le flag doit être un booléen True (pas une string)
result['my_new_flag'] = True
result['context_data'] = {'email': '...', 'date': '...'}
```

#### Étape 3 : Propager dans state_detector.py
```python
# 1. Contexte: dans _build_context()
context['my_new_flag'] = examt3p_data.get('my_new_flag', False)

# 2. Détection: dans _check_condition()
if condition == 'my_new_flag == true':
    return context.get('my_new_flag') is True

# 3. Alertes: dans _collect_alerts() si alerte client
if state.name == 'MY_NEW_STATE':
    alerts.append({
        'type': 'client',
        'id': state.id,
        'position': 'before_signature',
        'template': 'partials/category/my_new_state.html',
        'context': context
    })
```

#### Étape 4 : Créer le template partial
```html
<!-- states/templates/partials/category/my_new_state.html -->
<b>Concernant votre situation</b><br>
{{#if context_variable}}
Votre {{context_variable}} est en cours de traitement.
{{/if}}
```

#### Étape 5 : Ajouter le rendu dans template_engine.py
```python
# Dans _generate_alert_content()
if alert['id'] == 'XX':
    template = self._load_partial_path(alert['template'])
    content = self._replace_placeholders(template, alert['context'])
    return content
```

---

## PROCESS : Ajout d'une Nouvelle Intention

### RÈGLE CRITIQUE
**Toute intention ajoutée dans le YAML DOIT être ajoutée dans le prompt du TriageAgent !**

Si l'intention existe dans le YAML mais pas dans le prompt → elle ne sera **JAMAIS détectée**.

### Checklist rapide

| # | Fichier | Action |
|---|---------|--------|
| 1 | `states/state_intention_matrix.yaml` | Définir l'intention (section `intentions:`) |
| 2 | **`src/agents/triage_agent.py`** | **OBLIGATOIRE** - Ajouter dans SYSTEM_PROMPT |
| 3 | `states/state_intention_matrix.yaml` | Ajouter entrées matrice État×Intention (+ wildcard `"*:NOM"`) |
| 4 | `states/templates/partials/intentions/<nom>.html` | Template partial (si nécessaire) |
| 5 | `states/templates/response_master.html` | Ajouter section `{{#if intention_xxx}}` |
| 6 | `src/state_engine/template_engine.py` | Ajouter dans `INTENTION_FLAG_MAP` |
| 7 | `src/state_engine/template_engine.py` | Initialiser le flag dans `_auto_map_intention_flags()` |
| 8 | `src/state_engine/template_engine.py` | Ajouter les variables spécifiques dans `_prepare_placeholder_data()` |

(Voir la checklist complète en 6 étapes dans `docs/DEVELOPER_GUIDE.md`.)

### Détails

#### Étape 1 : Définir l'intention
```yaml
# Dans state_intention_matrix.yaml
intentions:
  NOM_INTENTION:
    id: "IXX"
    description: "Description claire"
    triggers:
      - "phrase déclencheuse 1"
      - "phrase déclencheuse 2"
    priority: XX
```

#### Étape 2 : OBLIGATOIRE - Ajouter dans TriageAgent
```python
# Dans src/agents/triage_agent.py, variable SYSTEM_PROMPT
**Catégorie appropriée:**
- NOM_INTENTION: Description claire de quand utiliser cette intention
  Exemples: "exemple 1", "exemple 2"
  Notes importantes si nécessaire
```

#### Étape 3 : Ajouter les entrées matrice
```yaml
# Dans state_intention_matrix.yaml
"*:NOM_INTENTION":
  template: "response_master.html"
  context_flags:
    intention_xxx: true
```

#### Étape 4 : Créer le partial
```html
<!-- states/templates/partials/intentions/nom_intention.html -->
<b>Concernant votre demande</b><br>
Voici la réponse à votre question...
```

#### Étape 5 : Ajouter dans response_master.html
```html
{{#if intention_xxx}}
{{> partials/intentions/nom_intention}}
{{/if}}
```

### Vérification de cohérence
```bash
# Vérifier dans le YAML
grep "NOM_INTENTION" states/state_intention_matrix.yaml

# Vérifier dans le TriageAgent
grep "NOM_INTENTION" src/agents/triage_agent.py
```

**Si l'intention n'est pas dans les DEUX fichiers, elle ne fonctionnera PAS.**

---

## Syntaxe Templates (Handlebars)

```html
{{variable}}                          <!-- Variable simple -->
{{{variable}}}                        <!-- Variable HTML (non échappée) -->
{{> bloc_name}}                       <!-- Inclusion de bloc -->
{{> partials/intentions/statut_dossier}}  <!-- Chemin relatif -->
{{#if condition}}...{{else}}...{{/if}} <!-- Conditionnel -->
{{#unless condition}}...{{/unless}}   <!-- Conditionnel inverse -->
{{#each items}}{{this.field}}{{/each}} <!-- Boucle -->
```

Voir `states/VARIABLES.md` pour la liste complète des variables disponibles.

---

## Commandes de Vérification

```bash
# Lister tous les états existants
grep -E "^  [A-Z_]+:" states/candidate_states.yaml | head -50

# Lister tous les intents existants
grep -E "^  [A-Z_]+:" states/state_intention_matrix.yaml | head -50

# Chercher si un état/intent similaire existe
grep -i "mot_clé" states/candidate_states.yaml
grep -i "mot_clé" states/state_intention_matrix.yaml

# Vérifier couverture templates
ls states/templates/partials/*/

# Vérifier qu'aucun template ne manque
python -c "
import re, os
with open('states/state_intention_matrix.yaml') as f:
    templates = set(re.findall(r'template:\s*[\"']?([\\w_-]+\\.html)', f.read()))
existing = set(os.listdir('states/templates/base_legacy'))
missing = templates - existing - {'response_master.html'}
print(f'Manquants: {len(missing)}')
for t in sorted(missing): print(f'  - {t}')
"
```

---

## Intégration TriageAgent → session_helper

```python
# Le workflow passe la préférence du TriageAgent au session_helper
session_data = analyze_session_situation(
    deal_data=deal_data,
    exam_dates=exam_dates,
    threads=threads_data,
    crm_client=crm_client,
    triage_session_preference=triage_result.get('intent_context', {}).get('session_preference')
)
```

**Priorité de détection de préférence :**
1. TriageAgent (`session_preference` dans `intent_context`)
2. Deal CRM (`Preference_horaire`)
3. Threads (analyse IA du message)
