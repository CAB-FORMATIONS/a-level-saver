# Règles Métier CAB Formations

## Vue d'ensemble

Ce document contient les règles métier critiques du système.
**Ces règles sont implémentées dans le code et les YAML. Ne pas les contourner.**

---

## 1. Mapping ExamT3P → Evalbox

Le statut du dossier ExamT3P est mappé vers le champ CRM `Evalbox` :

| Statut ExamT3P | Evalbox CRM | Signification |
|----------------|-------------|---------------|
| En cours de composition | Dossier crée | Dossier commencé, documents en cours |
| En attente de paiement | Pret a payer | Dossier complet, paiement attendu |
| En cours d'instruction | Dossier Synchronisé | Paiement reçu, CMA instruit |
| Incomplet | Refusé CMA | Documents à corriger |
| Valide | VALIDE CMA | Dossier validé par CMA |
| En attente de convocation | Convoc CMA reçue | Convocation envoyée |

**Implémenté dans :** `src/utils/examt3p_crm_sync.py` → `determine_evalbox_from_examt3p()`

---

## 2. Blocage Modification Date Examen

### Règle
**NE JAMAIS modifier `Date_examen_VTC` automatiquement si :**
- Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"}
- ET `Date_Cloture_Inscription` < aujourd'hui

### Conséquence
Le candidat est **engagé légalement** pour cet examen. Seule solution : justificatif de force majeure (action humaine requise).

### Implémentation
```python
# Dans src/utils/examt3p_crm_sync.py
def can_modify_exam_date(deal_data, exam_session):
    evalbox = deal_data.get('Evalbox')
    if evalbox in ['VALIDE CMA', 'Convoc CMA reçue']:
        cloture = exam_session.get('Date_Cloture_Inscription')
        if cloture and parse_date(cloture) < date.today():
            return False  # BLOQUÉ
    return True
```

### Exception : Force Majeure
Types reconnus :
- `medical` - Maladie, hospitalisation
- `death` - Décès proche
- `accident` - Accident empêchant déplacement
- `childcare` - Garde d'enfant urgente

Détecté par TriageAgent dans `intent_context.force_majeure_type`.
Nécessite validation humaine avant modification.

---

## 3. Cas Uber 20€

### Vue d'ensemble
L'offre Uber 20€ permet aux chauffeurs Uber éligibles de s'inscrire à l'examen VTC pour 20€ au lieu de 241€.

### Les 5 cas + États

| Cas | Condition | État | Action |
|-----|-----------|------|--------|
| PROSPECT | Amount ≠ 20 OU Stage ≠ GAGNÉ | Non Uber | Traitement standard |
| CAS A | Amount = 20 + GAGNÉ + Date_Dossier_recu vide | UBER_DOCS_MISSING | Demander documents |
| CAS D | J+4 passé + Compte_Uber = false | UBER_ACCOUNT_NOT_VERIFIED | Contacter Uber |
| CAS E | J+4 passé + ELIGIBLE = false | UBER_NOT_ELIGIBLE | Contacter Uber |
| CAS B | Date_Dossier_recu > 19/05/2025 + Date_test_selection vide | UBER_TEST_MISSING | Passer le test |
| ÉLIGIBLE | Toutes vérifications OK | UBER_ELIGIBLE | Peut s'inscrire |

### Ordre de vérification
```
PROSPECT → NOT_UBER → CAS A → CAS D → CAS E → CAS B → ÉLIGIBLE
```

### Timing vérification D/E
La vérification `Compte_Uber` et `ELIGIBLE` se fait à `Date_Dossier_recu + 4 jours` (`UBER_VERIFICATION_DELAY_DAYS = 4` dans `src/constants/thresholds.py`).
Avant ce délai, on ne bloque pas le candidat (vérification en attente côté Uber).

### Implémentation
```python
# Dans src/utils/uber_eligibility_helper.py
def analyze_uber_eligibility(deal_data):
    amount = deal_data.get('Amount')
    stage = deal_data.get('Stage', '')

    if amount != 20 or 'GAGN' not in stage.upper():
        return {'case': 'NOT_UBER', 'is_eligible': True}

    # CAS A : Documents non envoyés
    if not deal_data.get('Date_Dossier_recu'):
        return {'case': 'A', 'is_eligible': False}

    # Vérification J+4 pour D/E
    dossier_date = parse_date(deal_data.get('Date_Dossier_recu'))
    if date.today() >= dossier_date + timedelta(days=4):
        if deal_data.get('Compte_Uber') == False:
            return {'case': 'D', 'is_eligible': False}
        if deal_data.get('ELIGIBLE') == False:
            return {'case': 'E', 'is_eligible': False}

    # CAS B : Test non passé (après 19/05/2025)
    if dossier_date > date(2025, 5, 19):
        if not deal_data.get('Date_test_selection'):
            return {'case': 'B', 'is_eligible': False}

    return {'case': 'ELIGIBLE', 'is_eligible': True}
```

---

## 4. Doublon Uber 20€

### Règle
**L'offre Uber 20€ n'est valable qu'UNE SEULE FOIS par candidat.**

### Détection
```python
# Dans DealLinkingAgent.process()
deals_20_won = [
    d for d in all_deals
    if d.get("Amount") == 20 and d.get("Stage") == "GAGNÉ"
]
if len(deals_20_won) > 1:
    result["has_duplicate_uber_offer"] = True
    result["duplicate_deals"] = deals_20_won
```

### Comportement
1. Le `DealLinkingAgent` détecte automatiquement les doublons
2. Le workflow s'arrête à l'étape TRIAGE avec l'action `DUPLICATE_UBER`
3. Une réponse spécifique est générée

### Options proposées au candidat
- **Inscription autonome** : S'inscrire sur ExamT3P et payer les 241€ lui-même
- **Formation avec nous** : Formation VISIO ou présentiel (à ses frais)

### Règle 17 : Bypass doublon pour demandes non-Uber
La logique doublon ne s'applique que pour les demandes **liées à l'offre Uber 20€**.
Si le message contient des mots-clés d'inscription non-Uber (CPF, France Travail/KAIROS, financement personnel, devis, OPCO... — liste `NON_UBER_REGISTRATION` de `config/keywords.yaml`) :
- Demande non-Uber + doublon existant → `action = ROUTE` vers **Contact** (note interne « DEMANDE NON-UBER ») — la logique doublon Uber est ignorée
- Demande non-Uber + aucun deal → `ROUTE` vers **Contact** (prospect)

**Implémenté dans :** `doc_ticket_workflow.py` → `_run_triage()` (méthodes `non_uber_registration_routing` / `non_uber_prospect_routing`)

### Bypass post-examen
Si le champ CRM `Resultat` indique un examen passé ou un dossier clos (catégorie `mid_exam`, `post_exam` ou `closed`), le doublon non récupérable est **bypassé** : le workflow normal s'exécute (ex. intention RESULTAT_EXAMEN) au lieu du template doublon (`method = duplicate_with_resultat_bypass`).

### Flux clarification et doublons récupérables
Quand un doublon **potentiel** est détecté par nom + code postal mais avec email/téléphone différents :

1. **DUPLICATE_CLARIFICATION** (`_generate_duplicate_clarification_response`, `doc_ticket_workflow.py:5740`) :
   - Demande au candidat de confirmer l'email et le téléphone de sa précédente inscription (anti-homonyme)
   - L'intro est adaptée à l'intention détectée (STATUT_DOSSIER, DEMANDE_IDENTIFIANTS, DEMANDE_REINSCRIPTION, dates, e-learning, convocation...)
   - Cas spécial `identity_confirmation_no_deal` : le candidat mentionne un ancien dossier introuvable → réponse « recherche identité »
   - Pas de mise à jour CRM

2. **DUPLICATE_RECOVERABLE** (`_generate_duplicate_recoverable_response`, `doc_ticket_workflow.py:5853`) — doublon confirmé mais récupérable :
   - `RECOVERABLE_REFUS_CMA` : ancien dossier refusé par la CMA (payé) → réinscription possible avec la même offre Uber 20€
   - `RECOVERABLE_PAID` : dossier en cours de traitement CMA → reprise sans frais supplémentaires
   - `RECOVERABLE_NOT_PAID` : inscription jamais finalisée → reprise du dossier existant
   - La réponse demande le renvoi des documents à jour (pièce d'identité, permis, justificatif de domicile)

---

## 5. Date_test_selection

### Règle
**`Date_test_selection` est un champ READ-ONLY** : mis à jour par webhook e-learning uniquement.

### Flow automatique
```
1. Candidat passe le test sur e-learning CAB Formations
2. Webhook externe → Date_test_selection rempli dans Zoho CRM
3. Candidat contacte "j'ai passé le test"
4. Workflow vérifie CRM → Date_test_selection non vide
5. État: CAS B → ELIGIBLE (plus de blocage)
6. Si Date_examen_VTC vide → template propose dates + sessions
```

### Important
- **Ne JAMAIS modifier** ce champ via le workflow
- L'état est basé sur les données CRM, pas sur ce que le candidat dit
- Une fois rempli, le candidat sort automatiquement de CAS B

---

## 6. Priorité Préférence Session

### Ordre de priorité
1. **TriageAgent** : `session_preference` dans `intent_context`
2. **CRM** : `deal_data['Preference_horaire']`
3. **Threads** : Analyse IA du message

### Implémentation
```python
# Dans session_helper.py
def get_session_preference(deal_data, threads, triage_preference):
    # Priorité 1: TriageAgent
    if triage_preference in ['jour', 'soir']:
        return triage_preference

    # Priorité 2: CRM
    crm_pref = deal_data.get('Preference_horaire')
    if crm_pref:
        return crm_pref.lower()

    # Priorité 3: Analyse IA threads
    return analyze_threads_for_preference(threads)
```

---

## 7. Flexibilité Département

### Règle
| Situation | Choix département |
|-----------|-------------------|
| Pas de compte ExamT3P | N'importe quel département |
| Compte ExamT3P existe | Département assigné uniquement* |

*Changement de département = nouveau compte avec nouveaux identifiants.

### Implémentation
```python
# Dans date_examen_vtc_helper.py
can_choose_other_department = not examt3p_data.get('compte_existe', False)
```

---

## 8. Intention Duality

### Règle
**Toute intention ajoutée dans le YAML DOIT être ajoutée dans le prompt du TriageAgent.**

### Pourquoi
Le TriageAgent (Claude) ne peut détecter que les intentions qu'il connaît.
Si une intention existe dans `state_intention_matrix.yaml` mais pas dans le prompt du TriageAgent, elle ne sera **JAMAIS détectée**.

### Vérification
```bash
# Les deux commandes doivent retourner un résultat
grep "NOM_INTENTION" states/state_intention_matrix.yaml
grep "NOM_INTENTION" src/agents/triage_agent.py
```

---

## 9. Template Partials Naming

### Règle
- **Partials** (dans `states/templates/partials/`) → extension `.html`
- **Blocs** (dans `states/blocks/`) → extension `.md`

### Pourquoi
Le `TemplateEngine` cherche les partials avec extension `.html` d'abord.
Utiliser `.md` pour les partials peut causer des échecs silencieux de chargement.

---

## 10. Multi-Severity States

### Règle
| Severity | Comportement |
|----------|--------------|
| BLOCKING | Stoppe le workflow, réponse unique |
| WARNING | Continue, ajoute alerte à la réponse |
| INFO | Combinables, fusionnés dans la réponse |

### Exemples
| Severity | États |
|----------|-------|
| BLOCKING | SPAM, ROUTE_DEPARTMENT, CANDIDATE_NOT_FOUND, EXAMT3P_DOWN, EXAMT3P_ACCESS_LOST, DOUBLE_ACCOUNT_PAID, MISSED_TRAINING_FORCE_MAJEURE |
| WARNING | DUPLICATE_UBER, UBER_DOCS_MISSING, UBER_ACCOUNT_NOT_VERIFIED, UBER_NOT_ELIGIBLE |
| INFO | EXAM_DATE_EMPTY, CREDENTIALS_INVALID, GENERAL |

### Implémentation
```python
# Dans state_detector.py
if blocking_state:
    # Réponse unique, workflow stoppé
    return DetectedStates(blocking_state=blocking_state)
else:
    # Combine warning + info
    return DetectedStates(
        warning_states=warnings,
        info_states=infos
    )
```

---

## 11. Routage départemental (business_rules.py)

### `determine_department_from_deals_and_ticket(all_deals, ticket, last_thread_content)`
Logique complète de routing basée sur les deals CRM et le ticket (`business_rules.py:215`) :

1. Filtrer les deals à 20€
2. Priorité 1 : deal 20€ **GAGNÉ** (le plus récent par `Closing_Date`)
3. Priorité 2 : deal 20€ **EN ATTENTE**
4. Si deal 20€ trouvé : vérifier les conditions Refus CMA vs DOC
5. Si pas de deal 20€ : chercher un autre montant GAGNÉ ou EN ATTENTE → Contact
6. Sinon : fallback sur keywords

### Règle « autre service » (business_rules.py:313-336)
Si un deal 20€ existe MAIS que le sujet/dernier thread (nettoyé du contenu transféré) contient un mot-clé « autre service » (examen pratique, autre formation, location véhicule, CPF/compte formation, ambulance, capacité de transport...) :
- → Routage **Contact** (malgré le deal 20€)
- **SAUF si le candidat a une date d'examen future** (`Date_examen_VTC` > aujourd'hui) → reste en **DOC** (dossier actif)

### `get_department_routing_rules()`
Règles déterministes par mots-clés (`business_rules.py:703`), vérifiées AVANT l'analyse IA : mapping département → `keywords` / `subject_patterns` / `contact_domains` (DOC, DOCS CAB, Contact, etc.).

---

## Résumé des Règles Critiques

| # | Règle | Piège à éviter |
|---|-------|----------------|
| 1 | Mapping ExamT3P → Evalbox | Mauvais statut affiché |
| 2 | Blocage modification date | Modifier date après VALIDE CMA |
| 3 | Cas Uber A/B/D/E | Mauvaise détection = mauvaise réponse |
| 4 | Doublon Uber 20€ | Offre utilisée 2 fois |
| 5 | Date_test_selection READ-ONLY | Modifier via workflow |
| 6 | Priorité session | Ignorer préférence TriageAgent |
| 7 | Flexibilité département | Proposer changement avec compte existant |
| 8 | Intention duality | Intention non détectable |
| 9 | Partials = .html | Échec silencieux chargement |
| 10 | Multi-severity | Combiner états BLOCKING |
