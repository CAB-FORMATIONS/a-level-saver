---
name: regression-check
description: Analyse statique du diff pour détecter les risques de régression comportementale avant de coder ou committer.
---

# Skill: regression-check

## Description
Analyse le diff Git en cours et identifie les risques de régression comportementale. Contrairement au pre-commit-check (cohérence structurelle), ce skill détecte les changements qui pourraient **casser un comportement existant**.

## Instructions

Quand l'utilisateur invoque `/regression-check`, exécuter les étapes suivantes :

### ÉTAPE 1 : Récupérer le diff détaillé

```bash
git diff HEAD              # Diff complet (contenu, pas juste noms)
git diff --name-only HEAD  # Liste des fichiers modifiés
```

Si le diff est trop volumineux, lire les fichiers modifiés individuellement avec `git diff HEAD -- <fichier>`.

### ÉTAPE 2 : Classifier les zones impactées

Pour chaque fichier modifié, déterminer la zone :

| Zone | Fichiers | Risque principal |
|------|----------|-----------------|
| Z1 — Template Engine | `src/state_engine/template_engine.py` | Sections cachées/visibles pour les mauvais cas |
| Z2 — Workflow | `src/workflows/doc_ticket_workflow.py` | Routing, CRM updates, context_data cassé |
| Z3 — Triage | `src/agents/triage_agent.py` | Classification d'intent modifiée |
| Z4 — Templates HTML | `states/templates/**/*.html` | Contenu manquant/dupliqué |
| Z5 — Matrice | `states/state_intention_matrix.yaml` | Flags/template modifiés pour cas existants |
| Z6 — Helpers | `src/utils/*.py` | Dates/sessions/routing cassés |
| Z7 — Business Rules | `src/utils/business_rules.py` | Contamination contenu, faux routing |
| Z8 — Humanizer | `src/agents/response_humanizer.py` | Perte de contenu, hallucination |
| Z9 — Conversation Analyzer | `src/utils/conversation_analyzer.py` | Mauvais response_mode, faux mode |
| Z10 — Thread Memory | `src/utils/thread_memory.py` | Suppressions cassées, META parsing |

Afficher les zones impactées et skippées.

### ÉTAPE 3 : Exécuter les checks par zone

---

#### ZONE 1 : Template Engine — Risques de régression

Pour chaque ligne **ajoutée (+)** ou **supprimée (-)** dans `template_engine.py`, appliquer les checks suivants :

**R1.1 — Suppression de section (sur-blocage)**

Chercher dans le diff les patterns :
```python
result['show_dates_section'] = False
result['show_sessions_section'] = False
result['show_statut_section'] = False
result['show_session_info'] = False
result['suppress_elearning'] = True
```

Pour chaque occurrence **ajoutée (+)** :
- Lire le bloc `if/elif/else` complet qui l'entoure
- Lister TOUTES les conditions sous lesquelles la suppression se déclenche
- Vérifier : existe-t-il des cas légitimes où cette section devrait rester visible ?
- ⚠️ ALERTE si la condition est large (ex: `if dossier_termine:` sans exception pour REPORT_DATE)

**R1.2 — Garde dossier_termine modifiée**

Si le diff touche des lignes contenant `dossier_termine` :
- Vérifier que la liste d'exceptions est toujours présente :
  ```python
  if primary_intent in ('REPORT_DATE', 'DEMANDE_REINSCRIPTION'):
  ```
- ⚠️ ALERTE si une exception est supprimée ou si `dossier_termine` est utilisé sans exception

**R1.3 — Variable supprimée ou renommée dans _prepare_placeholder_data()**

Chercher dans les lignes supprimées (-) des patterns :
```python
-    'nom_variable': context.get('nom_variable', ...),
-    result['nom_variable'] = ...
```

Pour chaque variable supprimée :
- Grep cette variable dans `states/templates/**/*.html`
- ⚠️ ALERTE si la variable est utilisée dans un template (= elle deviendra silencieusement False/vide)

**R1.4 — Condition modifiée autour d'un flag existant**

Chercher les blocs `if/elif/else` modifiés qui assignent des flags existants.
Pour chaque bloc modifié :
- Comparer l'ancienne condition vs la nouvelle
- ⚠️ ALERTE si la condition est restreinte (cas existants pourraient ne plus matcher)
- ⚠️ ALERTE si un `elif` est ajouté AVANT un `else` existant (l'else couvrait ces cas avant)

**R1.5 — section0_overrides modifié**

Si `section0_overrides` est dans le diff :
- Vérifier que les flags supprimés de la liste ne sont pas toujours dans response_master.html Section 0
- Vérifier que les flags ajoutés existent bien dans response_master.html Section 0
- ⚠️ ALERTE si désynchronisé

**R1.6 — ThreadMemory/V3 suppression logic modifiée**

Si le diff touche `_extract_thread_memory_flags()` ou le bloc V3 `response_mode` :
- Vérifier que les suppressions ThreadMemory ne s'appliquent PAS quand la matrice a défini le flag (Rule 11)
- ⚠️ ALERTE si suppression sans garde `if 'flag' not in context:`

---

#### ZONE 2 : Workflow — Risques de régression

**R2.1 — Routing modifié (GO/ROUTE)**

Chercher dans le diff les patterns :
```python
+    triage_result['action'] = 'ROUTE'
+    action = 'ROUTE'
+    target_department =
```

Pour chaque routing ajouté :
- Lire les conditions : quels tickets seront routés ?
- ⚠️ ALERTE si la condition est basée sur des keywords sans `strip_forwarded_content()` (risque de contamination par contenu quoté)
- ⚠️ ALERTE si la condition pourrait matcher des cas normaux (faux positif routing)

**R2.2 — Early return ajouté**

Chercher `+ return` dans le diff de `doc_ticket_workflow.py` :
- Pour chaque `return` ajouté, vérifier ce qui est sauté (CRM update ? Draft creation ? Internal note ?)
- ⚠️ ALERTE si le return saute STEP 5 (CRM updates), STEP 6 (draft), ou STEP 7 (validation)

**R2.3 — Guard rail STEP 5 modifié**

Si le diff touche la zone STEP 5 (CRM updates) :
- Vérifier que `dossier_termine` bloque toujours les champs critiques (Date_examen_VTC, Session, Preference_horaire)
- Vérifier que les exceptions (REPORT_DATE, DEMANDE_REINSCRIPTION) sont toujours là
- ⚠️ ALERTE si un nouveau champ CRM est bloqué sans exception documentée

**R2.4 — context_data clé supprimée ou renommée**

Chercher les lignes supprimées contenant `context_data['xxx']` ou `context_data.update`:
- Pour chaque clé supprimée, vérifier si elle est utilisée dans template_engine.py
- ⚠️ ALERTE si la clé est dans `_prepare_placeholder_data()` (= template recevra None/False)

**R2.5 — Insistence/escalation modifiée**

Si le diff touche des lignes contenant `escalat`, `insist`, `Lamia`, `HADDOUCHI` :
- Vérifier que la condition d'escalation vérifie toujours le DERNIER message (pas tout le thread)
- Vérifier que `strip_forwarded_content()` est utilisé avant le check de keywords
- ⚠️ ALERTE si escalation sans vérification du dernier message

**R2.6 — Logique doublon Uber modifiée**

Si le diff touche `has_duplicate`, `doublon`, `uber` (dans workflow) :
- Vérifier que la Rule 17 est respectée (doublon != toutes les demandes)
- ⚠️ ALERTE si réponse doublon sans vérification des keywords non-Uber (CPF, France Travail, etc.)

**R2.7 — Session/date loading modifié**

Si le diff touche la section "session priority" ou "date loading" :
- Vérifier que les cas existants (CAS 1-8, CAS 2-IMPLICITE) ne sont pas court-circuités
- ⚠️ ALERTE si un nouveau `if` est ajouté AVANT les cas existants sans `elif`

---

#### ZONE 3 : Triage Agent — Risques de régression

**R3.1 — Description d'intent modifiée**

Pour chaque intent dont la description est modifiée (lignes `-` et `+` avec `- NOM_INTENT:`) :
- ⚠️ ALERTE avec l'ancienne et la nouvelle description
- Signaler le risque : "Le LLM pourrait classifier différemment des tickets existants"

**R3.2 — Nouvel intent ajouté — collision sémantique**

Pour chaque nouvel intent ajouté :
- Lister les intents existants qui ont un champ sémantique proche
- ⚠️ ALERTE si overlap détecté (ex: `DEMANDE_DATES_FUTURES` vs `DEMANDE_AUTRES_DATES`)

**R3.3 — JSON schema modifié**

Si le schema de sortie JSON est modifié :
- Vérifier que `intent_parser.py` gère encore tous les champs
- ⚠️ ALERTE si un champ est renommé ou supprimé

---

#### ZONE 4 : Templates HTML — Risques de régression

**R4.1 — Bloc {{#if}} / {{#unless}} réordonné**

Pour chaque fichier HTML modifié :
- Comparer l'ordre des blocs `{{#if}}` avant/après
- ⚠️ ALERTE si un bloc est déplacé (l'ordre affecte le rendu — premier match visible en premier)

**R4.2 — Chaîne {{#unless}} modifiée**

Si le diff touche une chaîne de `{{#unless}}...{{/unless}}` imbriqués :
- Vérifier que le fallback (dernier contenu avant les fermetures) est toujours accessible
- ⚠️ ALERTE si un `{{#unless}}` est ajouté sans fermeture correspondante (compter)

**R4.3 — Contenu supprimé d'un partial**

Pour chaque partial modifié, si des lignes de contenu sont supprimées :
- Vérifier que le contenu n'était pas critique (liens, identifiants, instructions CMA)
- ⚠️ ALERTE si suppression de `<a href=`, `exament3p.fr`, `cab-formations.fr`, ou contenu métier

**R4.4 — Nouveau {{#if}} dans response_master.html**

Si response_master.html a de nouveaux `{{#if intention_xxx}}` :
- Vérifier que le flag est bien dans `INTENTION_FLAG_MAP` et `_auto_map_intention_flags()`
- ⚠️ ALERTE si le flag n'existe pas (= bloc jamais rendu)

---

#### ZONE 5 : Matrice — Risques de régression

**R5.1 — context_flags modifié pour entrée existante**

Pour chaque entrée matrice modifiée (pas nouvelle) :
- Lister les flags changés (ajoutés/supprimés/valeur modifiée)
- ⚠️ ALERTE pour chaque flag qui passe de `true` à `false` ou supprimé : "Comportement changé pour STATE:INTENTION"

**R5.2 — Template changé pour entrée existante**

Si le champ `template:` d'une entrée existante change :
- ⚠️ ALERTE CRITIQUE : "Le rendering complet change pour cette combinaison STATE:INTENTION"

**R5.3 — Wildcard supprimé**

Si une ligne `"*:INTENTION"` est supprimée :
- ⚠️ ALERTE CRITIQUE : "Cette intention ne sera plus rendue pour AUCUN état non listé explicitement"

---

#### ZONE 6 : Helpers — Risques de régression

**R6.1 — Filtre de dates modifié**

Si le diff touche `date_examen_vtc_helper.py` ou `get_next_exam_dates` :
- Vérifier que les filtres (département, clôture future, mois) sont toujours appliqués dans le bon ordre
- ⚠️ ALERTE si un filtre est supprimé ou élargi

**R6.2 — Session filtering modifié**

Si le diff touche `session_helper.py` :
- Vérifier que `allow_change` et la comparaison session/date_examen sont toujours cohérents
- ⚠️ ALERTE si le filtre "session se termine AVANT examen" est supprimé (Rule 16)

**R6.3 — Routing keywords modifiés**

Si le diff touche des listes de keywords dans `business_rules.py` ou le workflow :
- ⚠️ ALERTE si un keyword est supprimé d'une liste de détection (potentiel faux négatif)
- ⚠️ ALERTE si un keyword est ajouté qui pourrait matcher du contenu quoté

**R6.4 — Return structure modifiée**

Si un helper modifie la structure de son return (dict keys ajoutées/supprimées/renommées) :
- Grep les callers de cette fonction
- ⚠️ ALERTE si un caller utilise une clé supprimée/renommée

---

#### ZONE 7 : Business Rules — Risques de régression

**R7.1 — strip_forwarded_content() modifié**

Si `strip_forwarded_content()` est modifié :
- ⚠️ ALERTE CRITIQUE : "Tout routing basé sur les keywords du message candidat peut être affecté"
- Lister les endroits dans le workflow qui appellent `strip_forwarded_content()`
- Vérifier que les nouveaux patterns ne strippent pas du contenu légitime

**R7.2 — Keyword lists modifiées**

Si des listes de keywords (contact_keywords, annulation_keywords, etc.) sont modifiées :
- Pour chaque keyword supprimé : ⚠️ "Ce pattern ne sera plus détecté"
- Pour chaque keyword ajouté : ⚠️ "Vérifier que ce pattern ne matche pas dans du contenu quoté"

---

#### ZONE 8 : Humanizer — Risques de régression

**R8.1 — Instructions système modifiées**

Si le SYSTEM_PROMPT ou les instructions du humanizer changent :
- ⚠️ ALERTE : "Le reformattage de TOUTES les réponses peut changer"
- Signaler si des règles de préservation (liens, dates, montants) sont affectées

**R8.2 — MODE_INSTRUCTIONS modifié**

Si les instructions par mode (brief_confirmation, status_update, targeted) changent :
- ⚠️ ALERTE par mode affecté avec ancien vs nouveau comportement

---

#### ZONE 9 : Conversation Analyzer — Risques de régression

**R9.1 — Prompt LLM modifié**

Si le prompt d'analyse conversationnelle change :
- ⚠️ ALERTE : "La classification conversation_mode/response_mode peut changer pour tous les tickets multi-thread"

**R9.2 — Short-circuit modifié**

Si la condition de short-circuit (≤1 thread) change :
- ⚠️ ALERTE si le seuil augmente (plus de tickets sans analyse) ou diminue (coût API augmente)

---

#### ZONE 10 : Thread Memory — Risques de régression

**R10.1 — META parsing modifié**

Si le parsing de `[META]` lines change :
- ⚠️ ALERTE : "Les anciennes notes CRM pourraient ne plus être parsées correctement"
- Vérifier la rétrocompatibilité avec les champs V1/V2/V3

**R10.2 — Suppression logic modifiée**

Si la logique de suppression ThreadMemory change :
- Vérifier que Rule 11 est toujours respectée (matrice > ThreadMemory)
- ⚠️ ALERTE si une suppression s'applique sans vérifier `if 'flag' not in context:`

---

### ÉTAPE 4 : Synthèse — Catalogue de régressions connues

Après les checks par zone, croiser avec le **catalogue de régressions historiques** du projet :

| # | Régression connue | Pattern à détecter | Zones |
|---|-------------------|--------------------|-------|
| H1 | Contamination contenu quoté | Keyword check sans `strip_forwarded_content()` | Z2, Z7 |
| H2 | Guard rail sur-bloquant | Nouvelle condition `= False` sans exception | Z1, Z2 |
| H3 | Flag cascade | Assignation flag avant vérification matrice (Rule 11) | Z1 |
| H4 | Intent collision | Nouvel intent sémantiquement proche d'un existant | Z3 |
| H5 | Early return skip | `return` avant STEP 5/6/7 | Z2 |
| H6 | Suppression sans escape | Suppression section sans exception pour cas légitime | Z1, Z10 |
| H7 | Variable fantôme | Variable template non initialisée dans engine | Z1, Z4 |
| H8 | SalesIQ metadata | Keyword match sur metadata technique (java, etc.) | Z2, Z7 |
| H9 | Insistence faux positif | Escalation basée sur thread complet au lieu du dernier message | Z2 |
| H10 | Session après examen | Session proposée dont la fin est APRÈS la date d'examen | Z1, Z6 |

Pour chaque pattern H1-H10, vérifier si le diff actuel **introduit** ou **corrige** ce pattern. Si introduction → ⚠️ ALERTE.

### ÉTAPE 5 : Afficher le rapport

Utiliser ce format :

```
REGRESSION CHECK — N fichiers modifiés, M zones impactées
═════════════════════════════════════════════════════════

ZONES IMPACTÉES :
  Z1 — Template Engine        ← N lignes modifiées
  Z2 — Workflow               ← N lignes modifiées
  ⏭️ Z3 — Triage              (non modifié)
  ...

RISQUES DÉTECTÉS :

🔴 CRITIQUE — R1.3 Variable supprimée
   template_engine.py:-895 — 'mon_flag': context.get('mon_flag', False)
   → Utilisée dans: partials/intentions/mon_intention.html:12
   → Impact: Le bloc {{#if mon_flag}} ne sera JAMAIS rendu
   → Scénario à tester: Ticket avec intention MON_INTENTION

🟡 ATTENTION — R2.1 Nouveau routing
   doc_ticket_workflow.py:+1534 — action = 'ROUTE' si keyword "pratique" détecté
   → Risque: "examen pratique" dans contenu quoté → faux routing Contact
   → Scénario à tester: Ticket avec réponse CAB mentionnant "examen pratique"

🟢 OK — R1.2 Garde dossier_termine
   → Exceptions REPORT_DATE et DEMANDE_REINSCRIPTION toujours présentes

CATALOGUE HISTORIQUE :
  ✅ H1 Contamination quotée — Pas de nouveau keyword check sans strip
  ⚠️ H2 Guard rail sur-bloquant — Nouveau blocage détecté (voir R1.1)
  ✅ H3 Flag cascade — Rule 11 respectée
  ...

══════════════════════════════════════════════════════
RÉSUMÉ : N risque(s) critique(s), M attention(s)

SCÉNARIOS À TESTER :
  1. [Critique] Ticket avec intention MON_INTENTION → vérifier que le bloc s'affiche
  2. [Attention] Ticket avec contenu quoté "examen pratique" → vérifier pas de faux routing
```

Si aucun risque :
```
══════════════════════════════════════════════════════
RÉSUMÉ : ✅ Aucun risque de régression détecté.
```

### Règles importantes

- **Lire le diff COMPLET** : Les noms de fichiers ne suffisent pas — il faut le contenu des changements.
- **Ne PAS modifier de fichiers** : Ce skill est en lecture seule.
- **Prioriser les critiques** : 🔴 = doit être corrigé, 🟡 = à vérifier, 🟢 = confirmé OK.
- **Scénarios concrets** : Toujours proposer un scénario de test avec le type de ticket à utiliser.
- **Paralléliser les lectures** : Lire tous les diffs de fichiers en parallèle.
- **Pas de faux sentiment de sécurité** : Si le diff est trop complexe pour une analyse statique fiable, le dire clairement.
- **Croiser les zones** : Un changement Z1 peut impacter Z4 (template engine ↔ templates HTML). Toujours vérifier les dépendances croisées.
