# Système de Templates

## Vue d'ensemble

Le système de templates génère les réponses aux candidats de manière **déterministe**.

### Pipeline de Réponse
```
┌─────────────────────────────────────────────────────────────────────┐
│   1. TEMPLATE ENGINE (Déterministe)                                 │
│      ├── Logique métier, données factuelles                        │
│      └── Structure en sections                                      │
│                               ↓                                     │
│   2. RESPONSE HUMANIZER (IA Sonnet)                                 │
│      ├── Reformule pour rendre naturel                              │
│      └── NE CONTIENT JAMAIS de règles métier                        │
│                               ↓                                     │
│   3. RÉPONSE FINALE                                                 │
└─────────────────────────────────────────────────────────────────────┘
```

### Règle d'Or
| Composant | Responsabilité | Ce qu'il NE FAIT PAS |
|-----------|----------------|----------------------|
| **Template Engine** | Logique métier, données factuelles | Mise en forme naturelle |
| **Response Humanizer** | Reformulation empathique, fluidité | Ajouter des infos métier |

**Si une info métier manque → l'ajouter dans le template, JAMAIS dans le Humanizer.**

---

## Architecture des Templates (v2.0)

### Structure des dossiers
```
states/templates/
├── response_master.html          # Template master universel
├── base_legacy/                  # 66 templates legacy (fallback DÉSACTIVÉ)
│   ├── uber_cas_a.html
│   ├── dossier_synchronise.html
│   └── ...
└── partials/                     # Blocs modulaires réutilisables (94 .html, 17 catégories)
    ├── intentions/               # Réponses aux intentions (36)
    │   ├── statut_dossier.html
    │   ├── demande_date.html
    │   ├── demande_identifiants.html
    │   ├── confirmation_session.html
    │   ├── report_date.html
    │   ├── question_generale.html
    │   └── ... (36 fichiers au total)
    ├── actions/                  # Actions requises (10)
    │   ├── passer_test.html
    │   ├── envoyer_documents.html
    │   ├── completer_dossier.html
    │   ├── choisir_date.html
    │   ├── choisir_session.html
    │   ├── surveiller_paiement.html
    │   ├── attendre_convocation.html
    │   ├── preparer_examen.html
    │   ├── corriger_documents.html
    │   └── contacter_uber.html
    ├── uber/                     # Conditions Uber (10)
    ├── statuts/                  # Affichage statut Evalbox (8)
    │   ├── dossier_cree.html
    │   ├── dossier_synchronise.html
    │   ├── pret_a_payer.html
    │   ├── valide_cma.html
    │   ├── refus_cma.html
    │   ├── convoc_recue.html
    │   ├── documents_refuses.html
    │   └── en_attente.html
    ├── resultats/                # Résultats examen (6)
    ├── report/                   # Report date (4)
    ├── cma/                      # Contact CMA (3)
    ├── documents/                # Documents (3)
    ├── alerts/                   # Alertes (2)
    ├── common/                   # Communs (2)
    ├── context/                  # Contexte (2)
    ├── credentials/              # Identifiants (2)
    ├── warnings/                 # Avertissements (2)
    ├── alternatives/             # Alternatives (1)
    ├── confirmations/            # Confirmations (1)
    ├── dates/                    # Proposition dates (1)
    └── prospect/                 # Prospect (1)
```

---

## Template Master (response_master.html)

### Structure 4 sections
```html
{{> salutation_personnalisee}}

<!-- SECTION 0: CONDITIONS BLOQUANTES -->
{{#if uber_cas_d}}{{> partials/uber/cas_d_compte_non_verifie}}{{/if}}
{{#if uber_cas_e}}{{> partials/uber/cas_e_non_eligible}}{{/if}}
{{#if report_bloque}}{{> partials/report/bloque}}{{/if}}
{{#if resultat_admis}}{{> partials/resultats/admis}}{{/if}}

<!-- SECTION 1: RÉPONDRE À L'INTENTION -->
{{#if intention_statut_dossier}}
{{> partials/intentions/statut_dossier}}
{{/if}}
{{#if intention_demande_date}}
{{> partials/intentions/demande_date}}
{{/if}}
<!-- ... autres intentions ... -->

<!-- SECTION 2: STATUT ACTUEL DU DOSSIER -->
{{#if show_statut_section}}
<b>Statut de votre dossier</b><br>
{{#if evalbox_dossier_synchronise}}
{{> partials/statuts/dossier_synchronise}}
{{/if}}
<!-- ... autres statuts ... -->
{{/if}}

<!-- SECTION 3: ACTION REQUISE POUR AVANCER -->
{{#if has_required_action}}
<b style="color: #d35400;">Prochaine étape pour avancer</b><br>
{{#if action_passer_test}}
{{> partials/actions/passer_test}}
{{/if}}
<!-- ... autres actions ... -->
{{/if}}

<!-- SECTION 4: DATES/SESSIONS SI PERTINENT -->
{{#if show_dates_section}}
{{#each next_dates}}
...
{{/each}}
{{/if}}

{{> acces_elearning_rappel}}
{{> verifier_spams}}
{{> signature}}
```

---

## Syntaxe Handlebars

### Bases
```html
{{variable}}                          <!-- Variable simple -->
{{{variable}}}                        <!-- Variable HTML (non échappée) -->
{{> bloc_name}}                       <!-- Inclusion bloc depuis states/blocks/ -->
{{> partials/intentions/statut}}      <!-- Inclusion partial avec chemin -->
```

### Conditions
```html
{{#if condition}}
  Affiché si condition truthy
{{else}}
  Affiché sinon
{{/if}}

{{#unless condition}}
  Affiché si condition falsy
{{/unless}}
```

### Boucles
```html
{{#each items}}
  {{this.field}}                      <!-- Accès au champ de l'item -->
  {{@index}}                          <!-- Index (0-based) -->
  {{@first}}                          <!-- true si premier élément -->
  {{@last}}                           <!-- true si dernier élément -->
{{/each}}
```

### Boucles avec conditions internes
```html
{{#each sessions_proposees}}
{{#if this.is_first_of_exam}}
<b>Examen du {{this.date_examen_formatted}}</b>
{{/if}}
{{#if this.is_jour}}→ Cours du jour{{/if}}
{{#if this.is_soir}}→ Cours du soir{{/if}}
{{/each}}
```

---

## Context Flags

### Flags d'intention (Section 1)
| Flag | Description |
|------|-------------|
| `intention_statut_dossier` | Question sur l'avancement |
| `intention_demande_date` | Demande de dates d'examen |
| `intention_demande_identifiants` | Demande d'identifiants ExamT3P |
| `intention_confirmation_session` | Choix de session jour/soir |
| `intention_demande_convocation` | Où est ma convocation ? |
| `intention_demande_elearning` | Accès e-learning |
| `intention_report_date` | Demande de report |
| `intention_probleme_documents` | Problème avec les documents |
| `intention_question_generale` | Question générale |
| `intention_resultat_examen` | Question sur résultat d'examen |
| `intention_question_uber` | Question sur l'offre Uber |
| `intention_question_session` | Question sur les sessions |
| `intention_question_processus` | Question sur le processus |
| `intention_autres_departements` | Demande d'autres départements |

### Flags conditions bloquantes (Section 0)
| Flag | Description |
|------|-------------|
| `uber_cas_a` | Documents non envoyés |
| `uber_cas_b` | Test de sélection non passé |
| `uber_cas_d` | Compte Uber non vérifié |
| `uber_cas_e` | Non éligible selon Uber |
| `uber_doublon` | Doublon offre Uber 20€ |
| `resultat_admis` | Résultat d'examen admis |
| `resultat_non_admis` | Résultat d'examen non admis |
| `resultat_absent` | Candidat absent à l'examen |
| `report_bloque` | Report impossible |
| `report_possible` | Report encore possible |
| `report_force_majeure` | Demande avec force majeure |
| `credentials_invalid` | Identifiants invalides |
| `credentials_inconnus` | Identifiants inconnus |

---

## Variables de Session

Voir `states/VARIABLES.md` pour la liste complète.

### Variables principales
```html
{{prenom}}                      <!-- Prénom du candidat -->
{{email}}                       <!-- Email du candidat -->
{{date_examen}}                 <!-- Date d'examen (DD/MM/YYYY) -->
{{date_cloture}}                <!-- Date de clôture -->
{{departement}}                 <!-- Numéro département -->
{{evalbox}}                     <!-- Statut Evalbox -->
{{identifiant_examt3p}}         <!-- Email ExamT3P -->
{{num_dossier}}                 <!-- Numéro dossier CMA -->
```

### Variables de session
```html
{{session_preference}}          <!-- "jour" ou "soir" -->
{{session_preference_jour}}     <!-- true/false -->
{{session_preference_soir}}     <!-- true/false -->
{{session_message}}             <!-- Message pré-formaté -->

{{#each sessions_proposees}}
  {{this.nom}}                  <!-- Nom de la session -->
  {{this.debut}}                <!-- Date début formatée -->
  {{this.fin}}                  <!-- Date fin formatée -->
  {{this.date_examen}}          <!-- Date examen associée -->
  {{this.date_examen_formatted}} <!-- Alias -->
  {{this.date_cloture_formatted}} <!-- Date clôture formatée -->
  {{this.departement}}          <!-- Département -->
  {{this.type}}                 <!-- "jour" ou "soir" -->
  {{this.is_jour}}              <!-- true/false -->
  {{this.is_soir}}              <!-- true/false -->
  {{this.is_first_of_exam}}     <!-- true si première session -->
{{/each}}
```

### Variables booléennes auto-calculées
```html
{{date_examen_vide}}            <!-- true si Date_examen_VTC vide -->
{{session_vide}}                <!-- true si Session vide -->
{{has_sessions_proposees}}      <!-- true si sessions disponibles -->
{{has_next_dates}}              <!-- true si dates disponibles -->
```

---

## Sélection de Template (Template Engine)

### Ordre de priorité (PASS 0-5)
```
PASS 0: Matrice STATE:INTENTION (exact match ou wildcard *:INTENTION)
PASS 1: Templates avec for_intention + for_condition
PASS 1.5: Templates avec for_state
PASS 2: Templates avec for_condition seule
PASS 3: for_uber_case (A, B, D, E)
PASS 4: for_resultat (Admis, Non admis)
PASS 5: for_evalbox (statut dossier) — DÉSACTIVÉ (code commenté, template_engine.py:422-428)
FALLBACK: response_master.html
```

### Exemple matrice
```yaml
# Dans state_intention_matrix.yaml
"UBER_TEST_MISSING:STATUT_DOSSIER":
  template: "response_master.html"
  context_flags:
    intention_statut_dossier: true
    show_statut_section: true

"*:REPORT_DATE":
  template: "report_possible.html"
  context_flags:
    intention_report_date: true
```

---

## Ajout d'un Nouveau Partial

### 1. Créer le fichier
```html
<!-- states/templates/partials/intentions/nouvelle_intention.html -->
<b>Concernant votre demande</b><br>
{{#if some_condition}}
Voici la réponse...
{{/if}}
```

### 2. Ajouter le flag dans template_engine.py
```python
# Dans _prepare_placeholder_data()
'intention_nouvelle_intention': context.get('intention_nouvelle_intention', False),
```

### 3. Ajouter dans response_master.html
```html
{{#if intention_nouvelle_intention}}
{{> partials/intentions/nouvelle_intention}}
{{/if}}
```

### 4. Ajouter dans la matrice
```yaml
"*:NOUVELLE_INTENTION":
  template: "response_master.html"
  context_flags:
    intention_nouvelle_intention: true
```

---

## Response Humanizer

### Ce qu'il fait
- Fusionne "Concernant X" + "Concernant Y" en paragraphes fluides
- Ajoute transitions naturelles ("Par ailleurs", "Concernant...")
- Rend le ton chaleureux et professionnel
- Répond dans l'ordre logique aux questions du candidat
- Préserve 100% des données factuelles

### Ce qu'il ne fait JAMAIS
- Ajouter des explications métier
- Inventer des informations
- Modifier dates, liens, identifiants
- Faire des promesses non présentes dans le template

### Validation automatique
Le Humanizer vérifie que toutes les dates, URLs et emails sont préservés.
Si validation échoue → retourne la réponse template originale.

### Exemple
**Avant (Template Engine)** :
```
Concernant votre session de formation
Nous avons bien noté votre préférence pour les cours du soir.

Concernant votre convocation
Vous n'avez pas encore de date d'examen assignée...
```

**Après (Humanizer)** :
```
Merci pour votre message. Je vais répondre à vos questions.

Nous avons bien noté votre préférence pour les cours du soir.
Votre dossier est actuellement en attente de traitement.

Concernant votre souhait de passer l'examen en février,
malheureusement il n'est plus possible de s'y inscrire
car la date de clôture est passée.
```

---

## Empathie Force Majeure

Dans les templates de report, ajouter l'empathie automatique :

```html
{{#if mentions_force_majeure}}
{{> empathie_force_majeure}}
{{/if}}
```

Le bloc `empathie_force_majeure.md` adapte le message selon `force_majeure_type` :
- `death` → Condoléances sincères
- `medical` → Prompt rétablissement
- `accident` → Soutien et compréhension
- `childcare` → Compréhension de la situation familiale

---

## Transformation session_helper → Template

Le `TemplateEngine` utilise `_flatten_session_options()` pour transformer les données :

```python
# Input (session_helper format):
{
    'proposed_options': [
        {
            'exam_info': {'Date_Examen': '2026-03-31', 'Departement': '75'},
            'sessions': [
                {'Name': 'cds-janvier', 'Date_d_but': '...', 'session_type': 'soir'}
            ]
        }
    ]
}

# Output (template format - aplati):
[
    {
        'date_examen': '31/03/2026',
        'departement': '75',
        'nom': 'Cours du soir',
        'debut': '15/01/2026',
        'fin': '25/01/2026',
        'type': 'soir',
        'is_soir': True,
        'is_first_of_exam': True
    }
]
```

---

## Bonnes Pratiques

### 1. Toujours utiliser .html pour les partials
```
states/templates/partials/*.html  ← CORRECT
states/templates/partials/*.md    ← FAUX (échec silencieux)
```

### 2. Blocs dans states/blocks/ utilisent .md
```
states/blocks/signature.md        ← CORRECT
```

### 3. Préserver les données factuelles
Le template DOIT contenir toutes les infos importantes (dates, liens, emails).
Le Humanizer reformule mais ne doit JAMAIS ajouter d'info.

### 4. Tester les conditionnels
```bash
# Vérifier que tous les {{#if}} ont leur {{/if}}
grep -c "{{#if" states/templates/response_master.html
grep -c "{{/if}}" states/templates/response_master.html
# Les deux nombres doivent être égaux
```
