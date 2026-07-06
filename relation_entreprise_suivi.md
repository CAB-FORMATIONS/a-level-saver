# Suivi Projet - Relations Entreprises

## Objectif

Mettre en place une automatisation augmentee par IA pour la boite Zoho Desk `Relations entreprises`, separee du workflow `DOC`, afin de creer automatiquement des brouillons de reponse aux emails B2B.

Le workflow doit :
- identifier l'intention du mail,
- retrouver le contexte CRM client/prospect,
- proposer une reponse adaptee,
- utiliser PlanBot via API pour les demandes de disponibilite/session,
- integrer un bloc devis avec montants `XXX a completer`,
- rester en mode brouillon uniquement.

## Separation Avec DOC

Le workflow `DOC` reste dedie aux candidats VTC/Uber/ExamT3P.

Le workflow `Relations entreprises` est separe :
- nouveau workflow : `RelationsTicketWorkflow`,
- triage dedie : `RelationsTriageAgent`,
- templates/reponses B2B dedies,
- validation B2B dediee,
- aucun auto-send,
- aucune mise a jour CRM automatique pour le moment.

Les seules briques reutilisees sont techniques : clients Zoho, creation de brouillon Desk, parsing threads, runner batch.

## Departement Et Email

- Departement Zoho Desk : `Relations entreprises`
- Department ID : `198709000027921097`
- Email expediteur : `relations.entreprises@cab-formations.fr`

## Fichiers Cotes A-Level Saver

Fichiers ajoutes :
- `src/workflows/relations_ticket_workflow.py`
- `src/agents/relations_triage_agent.py`
- `src/utils/planbot_api_client.py`
- `src/utils/relations_crm_lookup.py`
- `src/utils/relations_response_builder.py`
- `src/utils/relations_response_validator.py`
- `run_relations_workflow_batch.py`

Fichiers modifies :
- `config.py`
- `src/constants/departments.py`

## Fichiers Cotes Edusign

Fichiers modifies :
- `config.py`
- `src/webhook/server.py`

Ajout d'une API interne PlanBot read-only :
- `POST /internal/planbot/availability`

Outils PlanBot exposes via cette API :
- `check_availability`
- `search_alternative_dates`
- `search_alternative_centres`
- `optimize_candidate_placement`

## Variables D'Environnement A Configurer

Cote `Edusign` :
- `PLANBOT_INTERNAL_API_SECRET`

Cote `a-level-saver` :
- `PLANBOT_API_URL`
- `PLANBOT_API_SECRET`

Sans ces variables, le workflow peut quand meme generer des brouillons safe, mais il ne pourra pas interroger PlanBot durablement pour proposer des sessions validees.

## Intents B2B Geres

Intentions principales :
- `DEMANDE_DEVIS_FORMATION`
- `DEMANDE_DISPONIBILITE_SESSION`
- `INSCRIPTION_CANDIDATS`
- `COMMANDE_FORMALOGISTICS`
- `ANNULATION_REPORT_ABSENCE`
- `CONVENTION_CONTRAT_DOSSIER`
- `BON_DE_COMMANDE`
- `CONVOCATION_CONFIRMATION`
- `ATTESTATION_FIN_FORMATION`
- `DOCUMENTS_SIGNATURES_MANQUANTS`
- `FACTURE_FINANCEMENT_PEC`
- `BILAN_FORMATEUR`
- `PROSPECTION_PARTENARIAT`
- `CV_PROFILS_INTERVENANTS`
- `AUTRE_A_QUALIFIER`

## Extraction Actuelle

Le triage extrait ou enrichit :
- type de formation,
- categories CACES,
- date ou periode,
- initial/recyclage,
- nombre de candidats si present,
- centre si present,
- champs manquants.

Limite connue : l'extraction reste perfectible quand les informations sont uniquement dans les pieces jointes.

## Regles De Securite

Le workflow bloque ou evite :
- creation de doublon si un brouillon existe deja,
- spam/no-reply/notifications outils,
- emails internes sans destinataire externe fiable,
- prix inventes,
- termes internes dans les brouillons : `PlanBot`, `Zoho rules`, `UT`, `API`, `simulation`, etc.,
- confirmation ferme d'inscription/place reservee.

Le bloc devis contient uniquement des placeholders :
- `Montant HT : XXX EUR a completer`
- `TVA : XXX a completer`
- `Montant TTC : XXX EUR a completer`
- `Validite du devis : XXX jours a completer`
- `Modalites : XXX a completer`

## Commandes Utiles

Dry-run sur un lot de tickets ouverts :

```bash
python run_relations_workflow_batch.py --count 10
```

Creation de brouillons :

```bash
python run_relations_workflow_batch.py --count 10 --create-draft
```

Ticket specifique :

```bash
python run_relations_workflow_batch.py --ticket <ticket_id> --create-draft
```

Recalculer un ticket meme si un brouillon existe deja, sans toucher Zoho :

```bash
python run_relations_workflow_batch.py --ticket <ticket_id> --ignore-existing-draft --no-save
```

## Tickets Tests

Dry-runs et brouillons testes sur :
- `198709000476086655` - Bilan formateur CACES R489 - intention `BILAN_FORMATEUR`
- `198709000475929798` - BDC GEODIS CL IDF - intention `BON_DE_COMMANDE`
- `198709000475773385` - CEPIM dossier formateur - intention `CONVENTION_CONTRAT_DOSSIER`
- `198709000475148801` - Formation a programmer CACES - intention corrigee `ANNULATION_REPORT_ABSENCE`
- `198709000475954156` - Certalis formation confirmee - intention corrigee `CONVENTION_CONTRAT_DOSSIER`

Derniers brouillons crees volontairement :
- `198709000475148801`
- `198709000475954156`

Verification API : les deux tickets avaient bien un brouillon existant apres creation.

## Calibration Effectuee

Problemes corriges :
- parsing des emails sous format `Nom <email@domaine>` ;
- faux bruit cause par le mot `newsletter` dans les signatures CAB citees ;
- IA qui classait des vrais mails metier en `IGNORE_NOISE` ;
- intention deduite uniquement du sujet alors que le dernier message disait autre chose ;
- categories CACES faussement extraites depuis des dates ou numeros de session.

Regle actuelle : le dernier message utile prime sur le sujet quand il contient une intention claire.

## Limites Connues

- Les pieces jointes ne sont pas encore analysees en profondeur.
- Les brouillons deja crees ne sont pas modifies automatiquement.
- PlanBot n'est durablement exploitable qu'apres configuration des variables d'environnement.
- La grille tarifaire client n'existe pas encore dans Zoho, donc les prix restent a completer manuellement.
- Les demandes avec informations incompletes generent une demande de precision plutot qu'une proposition de session.

## Prochaines Etapes

1. Configurer les variables PlanBot API en environnement permanent.
2. Tester un lot de 10 a 20 tickets en `--create-draft` apres revue humaine.
3. Ajouter une lecture structuree des pieces jointes utiles : BDC, conventions, dossiers formateur.
4. Creer le module Zoho de grille tarifaire par client.
5. Brancher le devis automatique sur cette grille, en gardant `XXX` si aucune regle tarifaire fiable.
6. Ajouter des metriques de suivi : intent, draft_created, validation_failed, missing_fields, planbot_called.
7. Apres validation terrain, envisager uniquement des auto-actions non risquées : cloture spam/no-reply, jamais auto-send client au depart.
