---
name: pre-commit-check
description: Vérification automatique de la cohérence du projet avant commit. Détecte les 8 catégories de bugs les plus fréquents.
---

# Skill: pre-commit-check

## Description
Vérification automatique de la cohérence du projet avant commit. Détecte les 8 catégories de bugs les plus fréquents.

## Instructions

Quand l'utilisateur invoque `/pre-commit-check`, exécuter les étapes suivantes :

### ÉTAPE 1 : Récupérer les fichiers modifiés

```bash
git diff --name-only HEAD   # unstaged + staged changes
git diff --name-only --cached  # staged only
git diff --name-only --diff-filter=D HEAD  # deleted files
```

Combiner les résultats (union) → `modified_files`.

### ÉTAPE 2 : Déterminer quels checks exécuter

| Check | Condition de déclenchement |
|-------|---------------------------|
| CHECK 1 | Un fichier parmi : `src/agents/triage_agent.py`, `states/state_intention_matrix.yaml`, `src/state_engine/template_engine.py`, `states/templates/response_master.html`, ou `states/templates/partials/intentions/*.html` |
| CHECK 2 | `src/workflows/doc_ticket_workflow.py` OU `src/state_engine/template_engine.py` |
| CHECK 3 | Un `.html` dans `states/templates/` OU un partial supprimé |
| CHECK 4 | Un `.html` dans `states/templates/` |
| CHECK 5 | `src/state_engine/template_engine.py` OU `states/templates/response_master.html` |
| CHECK 6 | `src/state_engine/template_engine.py` |
| CHECK 7 | Un `.html` dans `states/templates/` OU `src/state_engine/template_engine.py` |
| CHECK 8 | `states/state_intention_matrix.yaml` OU `src/state_engine/template_engine.py` |

Pour les checks non déclenchés, afficher `⏭️ CHECK N: <nom> — Skipped (<raison>)`.

### ÉTAPE 3 : Exécuter les checks

---

#### CHECK 1: Intention Pipeline Consistency

**But** : Vérifier que les 6 points de synchronisation sont cohérents pour chaque intention.

**Sources à cross-checker :**

| # | Source | Comment extraire les intentions |
|---|--------|--------------------------------|
| A | `src/agents/triage_agent.py` — SYSTEM_PROMPT | Grep les lignes `- INTENTION_NAME:` (pattern: `- ([A-Z][A-Z_]+):` — sans ancrage `^` car le texte est dans un string Python). Chaque intention est listée comme `- NOM_INTENTION: description`. **Filtrage** : exclure les noms courts (≤4 chars) comme `DOC`, `CRM` et les noms contenant des espaces — seuls les `ALL_CAPS_WITH_UNDERSCORES` de 5+ chars sont des intentions. |
| B | `states/state_intention_matrix.yaml` — section `intentions:` | Grep les clés de niveau 2 sous `intentions:` (pattern: `^  ([A-Z][A-Z_]+):` au début du fichier, avant la section `matrix:`). |
| C | `states/state_intention_matrix.yaml` — wildcards | Grep `\"\*:([A-Z_]+)\"` (avec double-quotes échappées) ou lire le fichier et chercher les lignes contenant `"*:` — extraire le nom d'intention après `*:`. |
| D | `src/state_engine/template_engine.py` — `INTENTION_FLAG_MAP` | Lire le dict `INTENTION_FLAG_MAP` (ligne ~1418). Extraire les clés (noms d'intention) et les valeurs (noms de flags `intention_*`). |
| E | `src/state_engine/template_engine.py` — `_auto_map_intention_flags()` | Lire le dict `flags = {` (ligne ~1490). Extraire les clés du dict de flags initialisés. |
| F | `states/templates/response_master.html` | Grep `{{#if (intention_[a-z_]+)}}` — extraire les noms de flags. |
| G | `states/templates/partials/intentions/` | Lister les fichiers `.html`. Le nom de fichier (sans extension) = nom du partial. |

**Cross-checks à effectuer :**

1. **Triage → Matrix** : Chaque intention dans A doit avoir soit une entrée dans B (définition), soit un wildcard dans C (`"*:INTENTION"`).
2. **Matrix wildcards → Flag Map** : Chaque intention avec wildcard dans C devrait avoir un mapping dans D (sauf celles qui utilisent un template custom != `response_master.html`).
3. **Flag Map → Auto-init** : Chaque valeur unique de D (flag `intention_*`) doit être initialisée dans E.
4. **Flag Map → Master Template** : Chaque valeur unique de D doit avoir un `{{#if flag}}` dans F (sauf les flags couverts par section0_overrides comme `intention_report_date`, `intention_resultat_examen`, `intention_demande_identifiants`).
5. **Master Template → Partial** : Chaque `{{> partials/intentions/xxx}}` dans response_master.html doit avoir le fichier correspondant dans G.

**Tolérance connue** : Certaines intentions dans le triage sont mappées vers le même flag (ex: `DEMANDE_DATE_EXAMEN`, `DEMANDE_AUTRES_DATES`, `DEMANDE_DATES_FUTURES` → tous vers `intention_demande_date`). C'est normal — ne pas signaler comme erreur.

**Tolérance connue** : `REFUS_PARTAGE_CREDENTIALS` utilise un template custom (`credentials_refused_security.html`) et n'a pas besoin d'être dans response_master.html.

**Tolérance connue** : Certaines wildcards utilisent un template custom (pas `response_master.html`) et n'ont donc pas besoin d'être dans `INTENTION_FLAG_MAP` ni dans response_master.html. Exemples : `REMERCIEMENT`, `SALUTATION`, `MESSAGE_CONFUS`, `DATE_LOINTAINE_EXAMT3P`. Vérifier le champ `template:` de l'entrée wildcard — si != `response_master.html`, ne pas exiger la présence dans D/E/F.

**Tolérance connue** : Des intentions dans le triage (source A) peuvent ne pas avoir de wildcard `*:INTENTION` dans C si elles sont couvertes par des entrées spécifiques `STATE:INTENTION` dans la matrice. Ne signaler que les intentions totalement absentes de la matrice (ni wildcard, ni entrée spécifique).

---

#### CHECK 2: Template Variable Whitelist (workflow → engine)

**But** : Détecter les variables passées dans `context_data` mais absentes de `_prepare_placeholder_data()`.

**Méthode :**

1. **Extraire les clés de context_data** dans `src/workflows/doc_ticket_workflow.py` :
   - Pattern `context_data\['([a-z_]+)'\]` et `context_data\.update\({` suivi des clés du dict
   - Grep `context_data\[` et `context_data.update` pour trouver toutes les assignations

2. **Extraire les clés du whitelist** dans `src/state_engine/template_engine.py` :
   - Lire `_prepare_placeholder_data()` (ligne ~657-945)
   - Extraire toutes les clés de `result = {` et les `result['xxx'] = ` assignations

3. **Comparer** : Signaler les clés dans context_data absentes du whitelist.

**Exclusions connues** (objets internes, pas des variables template) :
```
deal_data, contact_data, examt3p_data, threads, enriched_lookups,
date_examen_vtc_data, session_data, uber_eligibility_data,
training_exam_consistency_data, cab_proposals, thread_memory,
conversation_state, crm_notes, primary_intent, secondary_intents,
detected_intent, intent_context, customer_message, ticket_subject,
section0_overrides, triage_full_result, cross_dept_data,
deal_notes, timeline_data, v3_response_mode, ticket_id, department
```

---

#### CHECK 3: Partial Reference Integrity

**But** : Aucune référence `{{> partials/...}}` ne pointe vers un fichier inexistant.

**Méthode :**

**Direction A (référence → fichier)** :
1. Dans chaque fichier `.html` modifié sous `states/templates/`, grep `{{>\s*partials/([^}]+)}}`.
2. Pour chaque match, résoudre le chemin : `partials/intentions/foo` → `states/templates/partials/intentions/foo.html`.
3. Vérifier que le fichier existe sur disque. Signaler les manquants.

**Direction B (suppression → références)** :
1. Parmi les fichiers supprimés (via `git diff --diff-filter=D`), filtrer ceux sous `states/templates/partials/`.
2. Pour chaque partial supprimé, extraire son nom de référence : `states/templates/partials/intentions/foo.html` → `partials/intentions/foo`.
3. Grep ce pattern dans TOUS les fichiers `states/templates/**/*.html` pour trouver les références orphelines.

---

#### CHECK 4: Handlebars Syntax Balance

**But** : Aucun bloc Handlebars n'est mal fermé.

**Méthode** : Pour chaque fichier `.html` modifié sous `states/templates/` :

1. Compter les occurrences de :
   - `{{#if` (ouvertures if)
   - `{{/if}}` (fermetures if)
   - `{{#unless` (ouvertures unless)
   - `{{/unless}}` (fermetures unless)
   - `{{#each` (ouvertures each)
   - `{{/each}}` (fermetures each)
   - `{{else}}` (juste pour info)

2. Pour chaque paire, vérifier que ouvertures == fermetures.
3. Signaler les déséquilibres : `fichier.html — {{#if}} x12 vs {{/if}} x11 — manque 1 fermeture`.

---

#### CHECK 5: Section0 Override Sync

**But** : Les flags Section 0 qui couvrent une intention sont bien listés dans `section0_overrides`.

**Méthode :**

1. Lire le dict `section0_overrides` dans `template_engine.py` (ligne ~1525). Extraire la structure :
   ```python
   section0_overrides = {
       'intention_report_date': ['report_possible', 'report_bloque', 'report_force_majeure'],
       'intention_resultat_examen': ['resultat_admis', 'resultat_non_admis', ...],
       'intention_demande_identifiants': ['credentials_invalid', 'credentials_inconnus'],
   }
   ```

2. Dans `response_master.html`, chercher les blocs Section 0 (les blocs `{{#if xxx}}` qui apparaissent AVANT la Section 1 "intentions"). Extraire les flags utilisés.

3. Pour chaque intention couverte par `section0_overrides`, vérifier que TOUS les flags Section 0 listés existent bien dans response_master.html (section 0).

4. Inversement : si un nouveau flag `resultat_*` ou `report_*` ou `credentials_*` apparaît dans response_master.html Section 0, vérifier qu'il est listé dans `section0_overrides` pour l'intention correspondante.

---

#### CHECK 6: Matrix Rule 11 Compliance

**But** : Le code ne recalcule pas un flag que la matrice peut définir.

**Flags protégés :** `show_dates_section`, `show_sessions_section`, `show_statut_section`, `show_session_info`.

**Méthode** : Dans `template_engine.py`, pour chaque flag protégé :

1. Grep toutes les lignes qui assignent le flag : `result\['show_dates_section'\]\s*=`.
2. Pour chaque assignation, vérifier que le code a bien une garde :
   - Pattern correct : `if 'show_dates_section' in context:` ou `if 'show_dates_section' not in context:` AVANT l'assignation, OU l'assignation est elle-même `result['show_dates_section'] = context.get('show_dates_section', ...)` ou `result['show_dates_section'] = context['show_dates_section']`.
   - Pattern incorrect : assignation directe sans vérification du contexte matrice.

3. Signaler les assignations sans garde.

**Tolérance** : La première initialisation dans le dict `result = {...}` est OK si elle utilise `context.get(...)`.

---

#### CHECK 7: Template Variable Ghost (template → whitelist)

**But** : Aucune variable dans les templates HTML ne référence un flag inexistant (silencieusement `false`).

**Méthode :**

1. Pour chaque `.html` modifié dans `states/templates/` :
   - Extraire les variables avec regex :
     - `{{#if\s+([a-z_][a-z0-9_]*)}}` → flag conditionnel
     - `{{#unless\s+([a-z_][a-z0-9_]*)}}` → flag conditionnel inversé
     - `{{([a-z_][a-z0-9_]*)}}` → variable de remplacement (PAS les partials `{{> ...}}`)
   - Exclure les variables spéciales Handlebars : `this`, `@index`, `@first`, `@last`, `@key`
   - Exclure les variables de contexte `{{#each}}` : si la variable apparaît dans un bloc `{{#each items}}...{{/each}}`, les `this.xxx` et accès simples sont des propriétés de l'item itéré → les exclure. Concrètement, si une variable est précédée de `this.` ou est à l'intérieur d'un `{{#each}}` bloc, l'exclure.

2. Pour chaque variable extraite, vérifier qu'elle existe dans au moins UN des endroits suivants :
   - `_prepare_placeholder_data()` dans `template_engine.py` — dans le dict `result` ou via `result['xxx'] = ...`
   - `_auto_map_intention_flags()` — dans le dict `flags` (pour les flags `intention_*`)
   - `context_flags` dans `state_intention_matrix.yaml` (flags injectés par la matrice)

3. Signaler les variables fantômes.

**Exclusions connues** (variables générées dynamiquement, pas dans le code statique) :
```
prenom, nom, email, date_examen, date_examen_formatted, departement,
session_choisie, statut_actuel, identifiant_examt3p, mot_de_passe_examt3p,
lien_examt3p, lien_elearning, uber_offre_montant
```
Ces variables basiques sont TOUJOURS présentes dans le whitelist — ne pas les signaler.

---

#### CHECK 8: Matrix Context Flags → Whitelist

**But** : Les `context_flags` de la matrice arrivent bien aux templates.

**Méthode :**

1. Parser `state_intention_matrix.yaml` : extraire tous les `context_flags` définis dans les entrées de la matrice.
   - Pattern YAML : après `context_flags:`, chaque ligne `flag_name: true/false` est un flag.
   - Collecter l'ensemble unique de tous les noms de flags.

2. Séparer en deux catégories :
   - **Flags `intention_*`** → vérifiés par CHECK 1 (auto-mappés). Les ignorer ici.
   - **Autres flags** (ex: `show_dates_section`, `show_sessions_section`, `dossier_termine`, etc.) → vérifier qu'ils sont dans `_prepare_placeholder_data()`.

3. Pour les flags non-intention : grep chaque nom dans `_prepare_placeholder_data()`. S'il n'est pas trouvé, c'est un flag fantôme de la matrice.

4. Signaler les flags matrice qui ne sont jamais exposés aux templates.

**Tolérance** : Les flags `show_*` sont traités spécialement (Rule 11) — ils sont assignés via des blocs conditionnels, pas dans le dict `result` initial. Vérifier qu'ils apparaissent au moins dans un `result['show_xxx']` assignation quelque part dans la méthode.

---

### ÉTAPE 4 : Afficher le rapport

Utiliser ce format exact :

```
PRE-COMMIT CHECK — N fichiers modifiés
═══════════════════════════════════════

✅ CHECK 1: Intention Pipeline — OK
✅ CHECK 2: Variable Whitelist (workflow→engine) — OK
❌ CHECK 3: Partial References — N problème(s)
   → fichier.html:ligne — {{> partials/intentions/foo}} — fichier inexistant
✅ CHECK 4: Handlebars Syntax — OK
⏭️  CHECK 5: Section0 Overrides — Skipped (fichiers non modifiés)
❌ CHECK 6: Rule 11 Compliance — N problème(s)
   → template_engine.py:1234 — result['show_dates_section'] = ... — pas de garde context
✅ CHECK 7: Template Variable Ghost — OK
✅ CHECK 8: Matrix Context Flags — OK

Résultat: N problème(s) trouvé(s). Corriger avant de committer.
```

Ou si tout est OK :
```
Résultat: ✅ Aucun problème détecté. Prêt à committer.
```

### Règles importantes

- **Paralléliser les lectures** : Lire tous les fichiers nécessaires en parallèle au début.
- **Ne PAS modifier de fichiers** : Ce skill est en lecture seule.
- **Faux positifs** : En cas de doute, mentionner `⚠️ (possible faux positif)` à côté du problème.
- **Performance** : Si aucun fichier modifié ne déclenche aucun check, afficher directement "Aucun check pertinent — fichiers modifiés hors périmètre."
