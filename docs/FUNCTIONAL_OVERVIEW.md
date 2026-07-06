# Documentation Fonctionnelle — A-Level Saver

## Contexte Metier

**CAB Formations** est un organisme de formation VTC (Voiture de Transport avec Chauffeur). En partenariat avec **Uber**, ils proposent une offre a 20 EUR permettant aux chauffeurs Uber eligibles de passer l'examen VTC. Cette offre inclut :

- Le paiement des frais d'examen CMA de 241 EUR (pris en charge par CAB Formations)
- 40 heures de formation en visioconference (cours du jour ou du soir)
- Un acces illimite au e-learning pour les revisions
- Un accompagnement personnalise jusqu'a l'obtention de la carte VTC

**A-Level Saver** est le systeme d'automatisation qui traite les tickets email du departement **DOC** dans Zoho Desk. Il analyse chaque message entrant, determine la situation du candidat (etat CRM + statut dossier), detecte l'intention du candidat (ce qu'il demande), puis genere une reponse adaptee en brouillon ou en envoi direct.

Le systeme traite environ 350+ tickets DOC et utilise un pipeline de reponse en 8 etapes, de la reception du ticket jusqu'a la creation du brouillon et la mise a jour CRM.

---

## Cycle de Vie du Candidat

Le parcours d'un candidat Uber 20 EUR suit les etapes suivantes :

```
Prospect (paiement 20 EUR en attente)
    |
    v
Inscription (paiement 20 EUR effectue = Deal GAGNE)
    |
    v
Envoi des documents (Date_Dossier_recu rempli)
    |
    v
Test de selection (obligatoire si inscription apres le 19/05/2025)
    |
    v
Verification Uber (Compte_Uber + ELIGIBLE verifies a J+4)
    |
    v
Creation compte ExamT3P + choix date d'examen + session de formation
    |
    v
Constitution du dossier CMA (paiement 241 EUR par CAB Formations)
    |
    v
Validation CMA --> Convocation --> Examen
    |
    v
Resultat (ADMIS / NON ADMIS / ADMISSIBLE / ABSENT)
    |
    v
Carte VTC (si ADMIS) ou reinscription (si echec)
```

**Declencheurs cles :**
- Le paiement des 20 EUR fait passer le candidat de Prospect a Inscrit
- L'envoi des documents declenche la verification d'eligibilite Uber
- Le passage du test de selection (si obligatoire) debloque l'inscription a l'examen
- Le paiement CMA de 241 EUR par CAB Formations fait passer le dossier en "Dossier Synchronise"
- La validation CMA entraine la reception de la convocation environ 10 jours avant l'examen

---

## Workflow Principal (8 Etapes)

```
STEP 0   DRAFT CHECK      Verifier si un brouillon existe deja (skip si oui)
STEP 0.1 SOURCE CHECK     Ignorer les Instant Messages (SalesIQ chat widget)
STEP 0.5 DOUBLON CHECK    Verifier si clarification doublon en attente
STEP 1   TRIAGE AGENT     GO / ROUTE / SPAM / DUPLICATE_UBER + intention + session_preference
STEP 2   ANALYSIS         7 sources (ticket, deal, ExamT3P, dates, sessions, uber, conversation V3)
STEP 3   STATE ENGINE     ETAT x INTENTION --> Template + partials (deterministe)
STEP 4   HUMANIZER        Reformulation naturelle par Sonnet 4.6 (optionnel)
STEP 5   CRM UPDATES      Via CRMUpdateAgent (mapping auto, regles blocage, guard rail dossier_termine)
STEP 6   CRM NOTE         Ligne [META] pour ThreadMemory (apres les mises a jour CRM)
STEP 7   REPLY DELIVERY   Brouillon Zoho Desk ou envoi direct (auto-send)
STEP 8   VALIDATION       Verification finale (termes interdits, donnees factuelles)
```

Le pipeline de reponse suit une separation stricte entre logique metier et mise en forme :
1. **Template Engine** (deterministe) : contient TOUTE la logique metier, les donnees factuelles
2. **Response Humanizer** (IA Sonnet) : reformule pour rendre la reponse naturelle et empathique
3. **Reponse finale** : humaine, structuree, pedagogique, complete

---

## Cycle de Vie Evalbox (Statut Dossier)

Le champ CRM **Evalbox** reflete la progression du dossier d'inscription a l'examen VTC. Les statuts sont synchronises depuis ExamT3P.

### Chronologie des statuts

```
N/A --> Documents manquants --> Documents refuses --> Dossier cree
    --> Pret a payer --> Dossier Synchronise --> VALIDE CMA --> Convoc CMA recue
                                                            --> Refuse CMA
```

### Tableau de reference

| Statut Evalbox | Statut ExamT3P | Signification | Compte ExamT3P | Paiement 241 EUR |
|----------------|----------------|---------------|-----------------|------------------|
| `N/A` | - | Aucun traitement commence | Non | Non |
| `Documents manquants` | - | Documents incomplets (interne CAB) | Non | Non |
| `Documents refuses` | - | Documents rejetes (interne CAB) | Non | Non |
| `Dossier cree` | En cours de composition | Dossier commence, documents en cours | Potentiel | Non |
| `Pret a payer` | En attente de paiement | Dossier complet, paiement 241 EUR attendu | Oui | Non |
| `Dossier Synchronise` | En cours d'instruction | Paiement recu, CMA instruit le dossier | Oui | **Oui** |
| `VALIDE CMA` | Valide | Dossier valide par la CMA | Oui | Oui |
| `Convoc CMA recue` | En attente de convocation | Convocation disponible | Oui | Oui |
| `Refuse CMA` | Incomplet | Document(s) refuse(s) par la CMA | Oui | Oui |

### Classification des statuts

| Categorie | Statuts | Impact |
|-----------|---------|--------|
| **Pre-validation** | N/A, Dossier cree, Pret a payer, Dossier Synchronise | Date modifiable (sauf exceptions) |
| **Valides** | VALIDE CMA, Convoc CMA recue | Modification bloquee (force majeure requise) |
| **Probleme documents** | Documents refuses, Documents manquants | Necessite correction |
| **Paiement effectue** | Dossier Synchronise, VALIDE CMA, Convoc CMA recue, Refuse CMA | CAB a paye les 241 EUR |

**Point critique** : "Dossier Synchronise" signifie "en cours d'instruction par la CMA", **PAS** valide. Si la date d'examen est passee et le dossier n'est pas valide, le candidat a ete auto-reporte (il n'a pas pu passer l'examen).

---

## Les 10 Cas de Dates d'Examen

Le helper `date_examen_vtc_helper.py` analyse la situation de date d'examen et determine l'un des 10 cas suivants. Le CAS 8 est evalue en priorite.

| CAS | Condition | Description | Action du bot |
|-----|-----------|-------------|---------------|
| **1** | Date vide | Aucune date d'examen assignee | Auto-assignation : fixer prochaine date + deduire session compatible. Proposer dates alternatives si pas de compte ExamT3P |
| **2** | Date passee + Evalbox pre-validation | Date expiree, dossier jamais valide | Auto-report sur prochaine date (le candidat n'a PAS pu passer l'examen). Proposer dates alternatives |
| **3** | Evalbox = Refuse CMA | Pieces refusees par la CMA | Lister pieces refusees avec details + repositionnement automatique sur prochaine date + date limite de correction |
| **4** | Date future + VALIDE CMA | Dossier valide, convocation a venir | Rassurer. Si examen dans 7 jours sans convocation : verifier spams. Si examen imminent : proposer prochaine date en secours |
| **5** | Date future + Dossier Synchronise | CMA instruit le dossier | Informer que l'instruction est en cours, prevenir de surveiller les emails |
| **6** | Date future + autre statut | Date assignee, en attente | Pas de message specifique sur la date (reponse selon l'intention detectee) |
| **7** | Date passee + VALIDE CMA ou Convoc | Examen probablement passe | Verifier indices dans les threads. Si indices de non-passage : demander clarification. Force majeure possible si < 14 jours |
| **8** | Date future + cloture passee + pre-validation | Deadline d'inscription ratee | Report automatique sur prochaine session. Verifier date paiement (si paiement avant cloture : pas de CAS 8) |
| **9** | Convoc CMA recue | Convocation disponible | Transmettre identifiants ExamT3P, lien plateforme, instructions impression + bonne chance |
| **10** | Pret a payer | Dossier pret pour paiement CMA | Paiement en cours par CAB, surveiller emails, corriger si refus CMA avant cloture |

### CAS 11 (extension)

| CAS | Condition | Description |
|-----|-----------|-------------|
| **11** | Convoc recue + date passee | Examen passe, en attente des resultats (delai 2-4 semaines) |

### Niveaux d'engagement CMA

Pour determiner si un repositionnement de date est possible, le systeme evalue le niveau d'engagement :

| Niveau | Condition | Repositionnement |
|--------|-----------|------------------|
| **0** | Pas de compte ExamT3P | Libre (n'importe quel departement) |
| **1** | Compte cree, pas de paiement | Possible (avec message CMA) |
| **2** | Dossier Synchronise + cloture future | Possible (avec message CMA) |
| **3** | Dossier Synchronise + cloture passee | Bloque (candidat inscrit) |
| **4** | VALIDE CMA / Convoc CMA recue | Force majeure uniquement |

---

## Eligibilite Uber (CAS A-E)

Le helper `uber_eligibility_helper.py` verifie les conditions d'eligibilite dans cet ordre :

| CAS | Condition | Etat candidat | Action |
|-----|-----------|---------------|--------|
| **PROSPECT** | Stage = EN ATTENTE, Amount = 20 | Paiement non effectue | Repondre aux questions generales + encourager paiement |
| **NOT_UBER** | Amount != 20 ou Stage != GAGNE | Pas une offre Uber 20 EUR | Traitement standard |
| **A** | Deal GAGNE + Date_Dossier_recu vide | Documents non envoyes | Confirmer paiement + demander de finaliser inscription |
| **D** | J+4 passe + Compte_Uber = false | Email non lie a un compte Uber Driver | Demander de verifier l'email ou contacter Uber via l'app |
| **E** | J+4 passe + ELIGIBLE = false | Non eligible selon Uber | Demander de contacter Uber via l'app pour comprendre |
| **B** | Date_Dossier_recu > 19/05/2025 + Date_test_selection vide | Test de selection non passe | Demander de passer le test (lien envoye par email) |
| **ELIGIBLE** | Toutes verifications OK | Candidat eligible | Processus normal d'inscription a l'examen |

**Ordre de verification** : `PROSPECT --> NOT_UBER --> CAS A --> CAS D --> CAS E --> CAS B --> ELIGIBLE`

**Note** : La verification des CAS D et E se fait a `Date_Dossier_recu + 4 jours`. Avant ce delai, on ne bloque pas le candidat (verification en attente cote Uber).

**Note** : Le test de selection (CAS B) n'est obligatoire que pour les dossiers recus **apres le 19/05/2025**. Les dossiers anterieurs passent directement a ELIGIBLE.

---

## Les Etats Candidat (candidate_states.yaml)

Les etats sont evalues par ordre de priorite (1 = plus prioritaire). Le premier etat dont les conditions sont satisfaites est selectionne.

### Etats de Triage (Priorite 1-99)

| ID | Etat | Severite | Description |
|----|------|----------|-------------|
| T1 | `SPAM` | BLOCKING | Message spam/publicite - cloturer sans reponse |
| T2 | `ROUTE_DEPARTMENT` | BLOCKING | Ticket a transferer vers un autre departement |
| T3 | `DUPLICATE_UBER` | WARNING | Candidat a deja utilise l'offre Uber 20 EUR |
| T4 | `CANDIDATE_NOT_FOUND` | BLOCKING | Aucun deal trouve pour cet email |

### Etats Credentials et ExamT3P (Priorite 95-104)

| ID | Etat | Severite | Description |
|----|------|----------|-------------|
| A0 | `CREDENTIALS_REFUSED_SECURITY` | INFO | Le candidat refuse de partager ses identifiants |
| A1 | `CREDENTIALS_INVALID` | INFO | Identifiants ExamT3P invalides ou manquants |
| A2 | `EXAMT3P_DOWN` | BLOCKING | Erreur technique ExamT3P - intervention manuelle |
| A3 | `DOUBLE_ACCOUNT_PAID` | BLOCKING | Deux comptes ExamT3P payes detectes |
| A4 | `PERSONAL_ACCOUNT_WARNING` | WARNING | Compte CAB paye + compte personnel non paye |
| A5 | `SESSION_ASSIGNMENT_ERROR` | WARNING | Session assignee dans le passe - erreur admin |
| A6 | `EXAMT3P_ACCESS_LOST` | BLOCKING | Identifiants ExamT3P invalides + dossier en statut critique (paiement fait) |

### Etats Eligibilite Uber (Priorite 200-204)

| ID | Etat | Severite | Description |
|----|------|----------|-------------|
| U-PROSPECT | `UBER_PROSPECT` | WARNING | Paiement 20 EUR non effectue |
| U-A | `UBER_DOCS_MISSING` | WARNING | 20 EUR paye mais documents non envoyes |
| U-B | `UBER_TEST_MISSING` | WARNING | Documents envoyes mais test non passe |
| U-D | `UBER_ACCOUNT_NOT_VERIFIED` | WARNING | Compte Uber Driver non verifie apres J+4 |
| U-E | `UBER_NOT_ELIGIBLE` | WARNING | Non eligible selon Uber |

### Etat Force Majeure (Priorite 290)

| ID | Etat | Severite | Description |
|----|------|----------|-------------|
| FM-1 | `MISSED_TRAINING_FORCE_MAJEURE` | BLOCKING | Formation manquee pour raison de force majeure |

### Etats Date Examen (Priorite 300-310)

| ID | Etat | Severite | CAS | Description |
|----|------|----------|-----|-------------|
| D-1 | `EXAM_DATE_EMPTY` | INFO | 1 | Pas de date d'examen - proposer dates |
| D-2 | `EXAM_DATE_PAST_NOT_VALIDATED` | INFO | 2 | Date passee, dossier non valide - auto-report |
| D-3 | `REFUSED_CMA` | INFO | 3 | Refuse par la CMA - pieces a corriger |
| D-4 | `VALIDE_CMA_WAITING_CONVOC` | INFO | 4 | Valide CMA - convocation a venir |
| D-5 | `DOSSIER_SYNCHRONIZED` | INFO | 5 | Transmis a la CMA - instruction en cours |
| D-6 | `EXAM_DATE_ASSIGNED_WAITING` | INFO | 6 | Date assignee, en attente |
| D-7 | `EXAM_DATE_PAST_VALIDATED` | INFO | 7 | Date passee + valide - examen passe? |
| D-8 | `DEADLINE_MISSED` | INFO | 8 | Cloture passee - report automatique |
| D-9 | `CONVOCATION_RECEIVED` | INFO | 9 | Convocation disponible |
| D-10 | `READY_TO_PAY` | INFO | 10 | Dossier pret pour paiement CMA |
| D-11 | `EXAM_PASSED_AWAITING_RESULTS` | INFO | 11 | Examen passe, attente resultats |

### Etats Intention Candidat (Priorite 400-408)

| ID | Etat | Description |
|----|------|-------------|
| I1 | `REPORT_DATE_REQUEST` | Demande de report de date d'examen |
| I2 | `CONFIRMATION_SESSION` | Confirme choix de session de formation |
| I3 | `DEMANDE_IDENTIFIANTS` | Demande identifiants ExamT3P |
| I4 | `STATUT_DOSSIER` | Question sur le statut du dossier |
| I5 | `CONFIRMATION_PAIEMENT` | Question sur le paiement |
| I6 | `DOCUMENT_QUESTION` | Question sur les documents |
| I7 | `RESULTAT_EXAMEN` | Demande resultat d'examen |
| I8 | `QUESTION_GENERALE` | Autre question generale |
| I9 | `CONFIRMATION_DATE_EXAMEN` | Confirme choix de date d'examen |

### Etats Coherence (Priorite 500-502)

| ID | Etat | Severite | Description |
|----|------|----------|-------------|
| C1 | `TRAINING_MISSED_EXAM_IMMINENT` | WARNING | Formation manquee + examen dans < 14 jours |
| C2 | `REFRESH_SESSION_AVAILABLE` | INFO | Formation terminee + examen futur - proposer rafraichissement |
| C3 | `DOSSIER_NOT_RECEIVED` | INFO | Deal 20 EUR sans dossier recu |

### Etats Blocage (Priorite 600)

| ID | Etat | Severite | Description |
|----|------|----------|-------------|
| B1 | `DATE_MODIFICATION_BLOCKED` | WARNING | Modification impossible (VALIDE CMA + cloture passee) |

### Etat par Defaut (Priorite 999)

| ID | Etat | Description |
|----|------|-------------|
| DEFAULT | `GENERAL` | Etat par defaut - reponse contextuelle |

---

## Les Intentions

Les intentions sont detectees par le Triage Agent (IA Sonnet 4.6) et representent ce que le candidat demande. Elles sont definies dans `state_intention_matrix.yaml`.

### Demandes d'information

| ID | Intention | Description |
|----|-----------|-------------|
| I01 | `DEMANDE_IDENTIFIANTS` | Demande identifiants ExamT3P |
| I02 | `DEMANDE_ELEARNING_ACCESS` | Demande acces e-learning |
| I03 | `DEMANDE_DATE_VISIO` | Quand est ma prochaine formation visio (40h) ? |
| I04 | `DEMANDE_LIEN_VISIO` | Demande le lien Zoom/Teams |
| I05 | `DEMANDE_DATE_EXAMEN` | Quelle est ma date d'examen ? |
| I06 | `DEMANDE_CONVOCATION` | Ou est ma convocation ? |
| I07 | `STATUT_DOSSIER` | Ou en est mon dossier ? |
| I08 | `DEMANDE_INFOS_OFFRE` | Questions sur l'offre Uber 20 EUR |
| I09 | `DEMANDE_AUTRES_DATES` | Veut d'autres dates / autre departement |

### Actions et confirmations

| ID | Intention | Description | Mise a jour CRM |
|----|-----------|-------------|-----------------|
| I10 | `REPORT_DATE` | Report/changement de date d'examen | - |
| I10b | `DEMANDE_DATE_PLUS_TOT` | Date plus tot que la date actuelle | - |
| I11 | `FORCE_MAJEURE_REPORT` | Report avec motif de force majeure | - |
| I12 | `CONFIRMATION_DATE_EXAMEN` | Confirme la date du XX/XX | Date_examen_VTC |
| I13 | `CONFIRMATION_SESSION` | Choix de session de formation | Session + Preference_horaire |
| I13b | `DEMANDE_CHANGEMENT_SESSION` | Changement de session deja assignee | - |
| I14 | `ENVOIE_DOCUMENTS` | Confirmation d'upload sur ExamT3P | - |
| I14b | `TRANSMET_DOCUMENTS` | Envoie documents en piece jointe | Route vers Refus CMA |
| I15 | `ENVOIE_IDENTIFIANTS` | Fournit ses identifiants ExamT3P | - |
| I16 | `REFUS_PARTAGE_CREDENTIALS` | Refuse de partager ses identifiants | - |

### Documents et permis

| ID | Intention | Description |
|----|-----------|-------------|
| I17 | `DOCUMENT_QUESTION` | Question sur les documents requis |
| I18 | `SIGNALE_PROBLEME_DOCS` | Probleme avec l'upload de documents |
| I18a | `QUESTION_PERMIS_ETRANGER` | Permis de conduire etranger (hors zone Euro) |
| I18b | `QUESTION_CARTE_SEJOUR` | Carte de sejour expiree ou recepisse |
| I18c | `QUESTION_HEBERGEMENT` | Justificatif de domicile quand heberge |

### Resultats d'examen

| ID | Intention | Description |
|----|-----------|-------------|
| I20 | `RESULTAT_EXAMEN` | Question sur le resultat |
| I21 | `ANNONCE_RESULTAT_POSITIF` | "J'ai reussi mon examen" |
| I22 | `ANNONCE_RESULTAT_NEGATIF` | "J'ai rate mon examen" |
| I23 | `DEMANDE_REINSCRIPTION` | "Je veux me reinscrire" |

### Communication et reclamations

| ID | Intention | Description |
|----|-----------|-------------|
| I24 | `DEMANDE_APPEL_TEL` | Demande d'appel telephonique |
| I25 | `RECLAMATION` | Plainte / insatisfaction (avec escalation) |
| I26 | `DEMANDE_ANNULATION` | Annulation, retractation, remboursement |

### Problemes techniques

| ID | Intention | Description |
|----|-----------|-------------|
| I27 | `SIGNALE_PAS_RECU_EMAIL` | Email non recu |
| I28 | `PROBLEME_CONNEXION_EXAMT3P` | Impossible de se connecter a ExamT3P |
| I29 | `PROBLEME_CONNEXION_ELEARNING` | Probleme acces e-learning |

### Autres intentions

| ID | Intention | Description |
|----|-----------|-------------|
| I19 | `CONFIRMATION_PAIEMENT` | Question sur le paiement / facture |
| I30 | `QUESTION_CARTE_VTC` | Comment obtenir la carte VTC |
| I31 | `QUESTION_EXAMEN_PRATIQUE` | Question sur l'epreuve pratique |
| I32 | `DEMANDE_CERTIFICAT_FORMATION` | Demande attestation/certificat |
| I33 | `REMERCIEMENT` | Simple remerciement |
| I34 | `SALUTATION` | Bonjour sans question |
| I35 | `MESSAGE_CONFUS` | Message incomprehensible |
| I36 | `QUESTION_GENERALE` | Autre question generale (fallback) |
| I37 | `DEMANDE_SUPPRESSION_DONNEES` | Demande RGPD suppression de donnees |
| I38 | `PERMIS_PROBATOIRE` | Question sur permis probatoire |
| I39 | `DATE_LOINTAINE_EXAMT3P` | Date pas encore visible sur ExamT3P |
| I40 | `DEMANDE_EXCEPTION` | Demande d'exception/derogation |
| I40 | `PERMIS_RENOUVELLEMENT` | Permis en renouvellement/vole/perdu |
| I41 | `ERREUR_PAIEMENT_CMA` | Candidat Uber a paye les 241 EUR lui-meme |
| - | `CONFIRMATION_DOUBLON` | Confirme etre le doublon trouve |
| - | `REFUS_DOUBLON` | Nie etre le doublon trouve |

### Architecture Wildcard

Chaque intention possede une entree wildcard `*:INTENTION` dans la matrice qui s'applique a n'importe quel etat si aucune entree specifique `ETAT:INTENTION` n'existe. Il y a **53 wildcards** definis, couvrant toutes les intentions.

La matrice contient egalement des entrees specifiques `ETAT:INTENTION` pour les combinaisons frequentes (environ 80 entrees specifiques + 53 wildcards = 130+ entrees).

### Groupements d'intentions (src/constants/intents.py)

| Groupe | Intentions | Usage |
|--------|-----------|-------|
| `FULL_RECAP_INTENTS` | QUESTION_GENERALE, ENVOIE_IDENTIFIANTS | Bypass ThreadMemory suppressions |
| `STATUT_INTENTS` | STATUT_DOSSIER, QUESTION_PROCESSUS, QUESTION_DOCUMENTS | Ne peut pas supprimer la section statut |
| `DATES_INTENTS` | REPORT_DATE, DEMANDE_DATE_PLUS_TOT, CONFIRMATION_DATE | Ne peut pas supprimer la section dates |
| `SESSION_CHANGE_INTENTS` | CONFIRMATION_SESSION, DEMANDE_CHANGEMENT_SESSION | Intentions liees aux sessions |
| `REINSCRIPTION_INTENTS` | DEMANDE_REINSCRIPTION, REPORT_DATE | Reactive les dates pour NON ADMIS |
| `NEEDS_NEXT_DATES_INTENTS` | REPORT_DATE, DEMANDE_REINSCRIPTION, DEMANDE_ANNULATION | Necessite le chargement de dates alternatives |

---

## Resultat et Dossier Termine

Le champ CRM `Resultat` est classifie par `_classify_resultat()` en 4 categories :

| Categorie | Valeurs CRM | Flag associe | dossier_termine |
|-----------|-------------|--------------|-----------------|
| `pre_exam` | (vide), None | aucun | `False` |
| `mid_exam` | ADMISSIBLE | `resultat_admissible` | `False` |
| `post_exam` | ADMIS, NON ADMIS, NON ADMISSIBLE, ABSENT TH, ABSENT PR, Convoc pas recu | `resultat_admis`, `resultat_non_admis`, etc. | `True` |
| `closed` | NON ADMIS PLUS INTERRESSE, NON ADMISSIBLE PLUS INTERRESSE | `resultat_plus_interesse` | `True` |

### Impact de dossier_termine = True

Quand le dossier est considere comme termine :

- **CRM bloque** : les champs Date_examen_VTC, Session, Preference_horaire ne sont pas mis a jour
- **CAS 8 bloque** : pas d'auto-reschedule
- **Auto-assignation bloquee** : pas de nouvelle date automatique
- **Templates** : les sections dates, sessions, actions, e-learning sont supprimees

### Exceptions

Les intentions `REPORT_DATE` et `DEMANDE_REINSCRIPTION` **re-activent** l'affichage des dates pour les candidats NON ADMIS qui souhaitent repasser l'examen.

### Bypass doublon

Si le champ Resultat indique un resultat d'examen (mid_exam, post_exam, closed), le workflow normal s'execute au lieu du template doublon Uber. Un candidat ADMISSIBLE qui demande ses resultats ne doit pas recevoir "vous avez deja utilise l'offre Uber".

---

## Guard Rails

### Auto-send

Le systeme peut envoyer automatiquement certaines reponses sans passer par un brouillon. Les conditions sont strictes :

- **Scenarios eligibles** : actuellement limite a `"test de selection reussi"` avec 1 thread entrant maximum
- **Qualite requise** : reponse non vide, humanisee, validation passee
- **Fallback** : si les conditions ne sont pas remplies, un brouillon est cree a la place

### Blocage modifications CRM (STEP 5)

Le guard rail `dossier_termine` bloque les mises a jour CRM quand le resultat indique que le cycle d'examen est termine :

```
Si dossier_termine == True :
  - Date_examen_VTC     --> BLOQUE
  - Session             --> BLOQUE
  - Preference_horaire  --> BLOQUE
  - Auto-reschedule     --> BLOQUE
  - Auto-assignation    --> BLOQUE
```

### Blocage modification de date d'examen

La modification de `Date_examen_VTC` est impossible si :
- Evalbox = VALIDE CMA ou Convoc CMA recue
- ET Date_Cloture_Inscription < aujourd'hui

Seule solution : justificatif de force majeure (intervention humaine).

### Detection doublon Uber

L'offre Uber 20 EUR n'est valable qu'une seule fois par candidat. Le `DealLinkingAgent` detecte automatiquement les doublons (2+ deals GAGNE a 20 EUR).

**Exception Regle 17** : un candidat doublon qui ecrit pour une raison **non liee a l'offre Uber** (CPF, France Travail, financement personnel) est route vers Contact au lieu de recevoir la reponse doublon.

**Exception Resultat** : un candidat doublon dont le Resultat indique un examen passe/en cours (ADMISSIBLE, ADMIS, NON ADMIS, ABSENT) passe par le workflow normal.

### Insistance et escalation

Pour l'intention `DEMANDE_ANNULATION`, si le candidat a deja recu une reponse (marqueurs detectes dans l'historique : "non remboursable", "plus de 700") **ET** que son dernier message entrant contient toujours des mots-cles d'annulation, le ticket est escalade a Lamia (agent humain).

### Guard rail paiement avant cloture (CAS 8)

Avant de declencher un CAS 8 (report automatique), le systeme verifie si le paiement CMA a ete fait avant la cloture. Si oui, le candidat etait inscrit a temps et ne doit pas etre reporte.

Si le dossier est "Dossier Synchronise" sans acces ExamT3P pour verifier la date de paiement, le systeme presume que le paiement est valide par securite.

### Guard rail refus CMA apres cloture (Timeline)

Meme si paiement_avant_cloture = True, un refus CMA survenu apres la cloture (detecte via la Timeline API) rend la date obsolete et le candidat est auto-reporte (CAS 8 active).

---

## 17 Regles Critiques

### Regle 1 : Separation Template / Humanizer

Le Template Engine contient la logique metier et les donnees factuelles. Le Humanizer reformule pour rendre la reponse naturelle. **Jamais ajouter d'information metier dans le Humanizer.**

Si une info manque dans la reponse, l'ajouter dans le template, pas dans le Humanizer.

### Regle 2 : CRM Lookups = appel API extra

Les lookups CRM retournent `{name, id}`, pas la vraie donnee. Utiliser `enrich_deal_lookups()` pour obtenir les vraies valeurs (ex: `date_examen` au lieu de `34_2026-03-31`).

### Regle 3 : Blocage modification date

Ne jamais modifier `Date_examen_VTC` si Evalbox = VALIDE CMA ou Convoc CMA recue ET cloture passee. Le candidat est engage legalement.

### Regle 4 : Dualite intention

Une intention ajoutee dans le YAML sans etre dans le prompt du Triage Agent ne sera jamais detectee. Les deux doivent etre synchronises.

### Regle 5 : Uber 20 EUR one-time

L'offre n'est valable qu'une fois. Le doublon est detecte automatiquement (sauf post-examen qui bypass le doublon).

### Regle 6 : Multi-severity states

Les etats BLOCKING stoppent le workflow. Les etats WARNING/INFO peuvent se combiner avec des intentions.

### Regle 7 : Mapping ExamT3P vers Evalbox

Les statuts ExamT3P sont mappes vers Evalbox CRM selon une table precise. "En cours d'instruction" = "Dossier Synchronise", "Incomplet" = "Refuse CMA", etc.

### Regle 8 : Date_test_selection READ-ONLY

Ce champ est rempli uniquement par webhook e-learning. Ne jamais le modifier via le workflow.

### Regle 9 : Priorite preference session

Ordre : 1) Triage Agent, 2) CRM (Preference_horaire), 3) Analyse IA des threads.

### Regle 10 : Partials = .html

Les partials de templates utilisent l'extension `.html` (syntaxe Handlebars), jamais `.md`.

### Regle 11 : Matrice = Source de verite pour les context flags

Si la matrice definit un flag (ex: `show_dates_section: false`), le code Python ne doit PAS le recalculer. Toujours verifier la matrice en premier.

### Regle 12 : Anti-repetition = context flags, PAS Humanizer

La detection de repetition (dates deja envoyees, sessions deja proposees) se fait en amont via des flags comme `dates_proposed_recently`, `sessions_proposed_recently`. Le Humanizer ne doit pas supprimer de contenu.

### Regle 13 : Wildcard obligatoire

Toute intention doit avoir une entree wildcard `*:INTENTION` dans la matrice. Sinon elle sera detectee par le triage mais jamais rendue par le template engine.

### Regle 14 : Jamais de fallback legacy

L'architecture moderne (`response_master.html` + matrice) doit couvrir toutes les combinaisons. Si une combinaison tombe sur un template legacy (`base_legacy/`), alerter et migrer.

### Regle 15 : Statuts pre-validation != valides

"Dossier Synchronise" = en instruction, PAS valide. Si date passee + dossier non valide : auto-report (CAS 2), pas "examen passe" (CAS 7).

### Regle 16 : Sessions filtrees par date d'examen

Les sessions proposees doivent se terminer AVANT la date d'examen. Ne pas proposer une session de septembre pour un examen en mai.

### Regle 17 : Doublon != toutes les demandes

Un candidat doublon Uber qui ecrit pour du CPF, France Travail ou financement personnel doit etre route vers Contact, pas recevoir la reponse doublon Uber.

---

## ThreadMemory (Memoire Persistante)

Le systeme dispose d'une memoire inter-tickets en 3 versions :

| Version | Source | Donnees |
|---------|--------|---------|
| **V1** | Lignes `[META]` dans les notes CRM | Etat, intention, evalbox, sections envoyees |
| **V2** | Timeline API (changements de champs) | Progression CRM, interventions humaines |
| **V3** | `conversation_analyzer.py` (LLM Sonnet) | Mode conversation, mode reponse, engagements |

### V3 Response Mode

Le mode de reponse V3 controle la visibilite des sections :
- `full` : tout affiche
- `brief_confirmation` : supprime dates/sessions (le candidat confirme, pas besoin de tout repeter)
- `targeted` : reponse ciblee
- `status_update` : force la section statut

### Court-circuit V3

Si le ticket n'a qu'un seul thread entrant, le LLM n'est pas appele (economie de cout et latence). Les valeurs par defaut sont utilisees.

### Guard rail humain

Si un humain (interface CRM) est intervenu apres la derniere note META, toutes les suppressions ThreadMemory sont reinitalisees.

---

## Temporal Awareness (Sessions et Examen)

Le systeme calcule des flags temporels pour adapter le ton des reponses :

### Flags session

| Flag | Signification |
|------|--------------|
| `session_upcoming` | Session de formation a venir |
| `session_in_progress` | Session actuellement en cours |
| `session_finished` | Session terminee |
| `session_starts_soon` | Session commence bientot |
| `days_until_session_start` | Nombre de jours avant le debut |

### Flags examen

| Flag | Signification |
|------|--------------|
| `exam_today` | L'examen est aujourd'hui |
| `exam_within_30_days` | L'examen est dans les 30 prochains jours |

Les templates utilisent ces flags pour adapter le vocabulaire (passe/present/futur : "votre session a eu lieu", "votre session est en cours", "votre session est prevue").

---

## Scenarios Types

### Premier contact d'un nouveau candidat

1. Triage : GO + intention detectee (ex: QUESTION_GENERALE)
2. Eligibilite Uber : CAS A (documents non envoyes)
3. Etat : UBER_DOCS_MISSING
4. Reponse : confirmation du paiement + explication des etapes + demande de finaliser inscription
5. Pas de mise a jour CRM

### Candidat qui demande sa date d'examen

1. Triage : GO + intention DEMANDE_DATE_EXAMEN
2. Analyse : CAS 1 (date vide) ou CAS 6 (date assignee)
3. Si CAS 1 : auto-assignation de la prochaine date + proposition de session
4. Si CAS 6 : confirmation de la date existante
5. Mise a jour CRM si auto-assignation

### Candidat qui veut changer de session

1. Triage : GO + intention DEMANDE_CHANGEMENT_SESSION
2. Cascade 3 niveaux :
   - Niveau 1 : meme type, meme date, session differente
   - Niveau 2 : autre type meme date + meme type prochaine date
   - Niveau 3 : prochaine date (tous types)
3. Reponse avec options de session alternatives
4. Mise a jour CRM apres confirmation du candidat

### Candidat avec examen passe (ADMIS)

1. Triage : GO + intention RESULTAT_EXAMEN
2. Classify resultat : post_exam, dossier_termine = True
3. Reponse : felicitations + etapes pour obtenir la carte VTC
4. Guard rail : aucune mise a jour CRM (dossier termine)

### Doublon offre Uber

1. DealLinkingAgent detecte 2+ deals GAGNE a 20 EUR
2. Verification : la demande est-elle liee a Uber ? (Regle 17)
3. Si oui : reponse doublon (inscription autonome + formation payante)
4. Si non (CPF, France Travail) : route vers Contact
5. Si resultat post-exam : bypass doublon, workflow normal

### Demande d'annulation/remboursement

1. Triage : GO + intention DEMANDE_ANNULATION
2. Premiere reponse : accuse reception + politique non remboursable + valeur de l'offre
3. Si insistance detectee (marqueurs deja presents + mots-cles dans dernier message) : escalation a Lamia
4. Si le dernier message ne contient plus de mots-cles d'annulation : pas d'escalation (le candidat a accepte)

---

## Workflow Relations entreprises (B2B)

Un workflow separe traite les emails du departement Relations entreprises (`src/workflows/relations_ticket_workflow.py`). Il est volontairement independant du workflow DOC :

- **Triage B2B** : `RelationsTriageAgent` (LLM + fallback deterministe) classe le message parmi 15 intentions B2B (devis, disponibilite session, inscription candidats, commande Formalogistics, annulation/report/absence, convention/contrat, bon de commande, convocation, attestation fin de formation, documents/signatures manquants, facture/financement, bilan formateur, prospection/partenariat, CV intervenants, autre a qualifier) avec actions DRAFT / IGNORE_NOISE / ROUTE_COMPTA / ROUTE_HUMAN.
- **Lookup CRM** : `RelationsCRMLookup` retrouve le contact et le compte (entreprise) a partir de l'email de l'expediteur.
- **Disponibilites** : si la demande est suffisamment qualifiee, interrogation en lecture seule de l'API interne PlanBot (`planbot_api_client.py`) ; sinon la reponse demande les infos manquantes (`missing_fields`).
- **Brouillon uniquement** : le workflow ne fait JAMAIS d'envoi automatique ni de mise a jour CRM — il cree uniquement des brouillons Zoho Desk (`auto_create_draft=True`).
- **Validation** : `relations_response_validator.py` verifie l'absence de termes interdits (`FORBIDDEN_TERMS`) avant creation du brouillon.

Point d'entree batch : `run_relations_workflow_batch.py`.

---

## Couts API par ticket

| Composant | Modele | Cout estime |
|-----------|--------|-------------|
| Extraction identifiants | Haiku 4.5 (`MODEL_EXTRACTION`) | ~0.001 USD |
| Agent Trieur | Sonnet 4.6 (`MODEL_TRIAGE`) | ~0.01 USD |
| Conversation Analyzer V3 | Sonnet 4.5 (`MODEL_CONVERSATION`) | ~0.01-0.02 USD |
| Response Humanizer | Sonnet 4.6 (`MODEL_HUMANIZER`) | ~0.036 USD |
| Note CRM (next steps) | Sonnet 4.6 (`MODEL_TRIAGE`) | ~0.01 USD |
| **Total** | | **~0.06-0.08 USD** |

Le Conversation Analyzer V3 ne s'execute que pour les tickets multi-thread (>1 thread entrant). Les tickets single-thread = 0 USD (court-circuit).

---

## Fichiers de Reference

| Fichier | Description |
|---------|-------------|
| `states/candidate_states.yaml` | Tous les etats candidat avec conditions et templates |
| `states/state_intention_matrix.yaml` | Intentions + matrice ETAT x INTENTION |
| `src/utils/date_examen_vtc_helper.py` | Logique CAS 1-10 dates d'examen |
| `src/utils/uber_eligibility_helper.py` | Logique CAS A-E eligibilite Uber |
| `src/workflows/doc_ticket_workflow.py` | Workflow principal (8 etapes) |
| `src/state_engine/template_engine.py` | Moteur de templates pybars3 |
| `src/utils/thread_memory.py` | ThreadMemory V1/V2 |
| `src/utils/conversation_analyzer.py` | ThreadMemory V3 (LLM) |
| `src/constants/evalbox.py` | Statuts Evalbox (frozensets) |
| `src/constants/intents.py` | Groupements d'intentions |
| `states/templates/response_master.html` | Template master modulaire |
| `states/templates/partials/**/*.html` | Partials modulaires |
