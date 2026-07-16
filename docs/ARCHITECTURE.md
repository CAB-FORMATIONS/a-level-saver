# Architecture Système A-Level Saver

## Vue d'Ensemble

Système d'automatisation des tickets Zoho Desk pour CAB Formations (formation VTC Uber).
Le workflow traite les tickets DOC en utilisant plusieurs agents spécialisés et sources de données.

**Stack technique :**
- Python 3.11+
- Anthropic Claude (Sonnet pour agents, Haiku pour tâches légères)
- Zoho Desk & CRM APIs (OAuth2)
- Playwright/Selenium (scraping ExamT3P)
- YAML (configuration états/intentions)
- Handlebars (templates)

---

## Structure du Projet

```
a-level-saver/
├── src/                              # Code applicatif principal
│   ├── agents/                       # Agents IA spécialisés
│   │   ├── triage_agent.py           # Triage tickets (GO/ROUTE/SPAM)
│   │   ├── crm_update_agent.py       # Mises à jour CRM
│   │   ├── deal_linking_agent.py     # Liaison ticket↔deal
│   │   ├── examt3p_agent.py          # Extraction données ExamT3P
│   │   ├── relations_triage_agent.py # Triage B2B (15 intentions)
│   │   └── base_agent.py             # Classe abstraite agents
│   │
│   ├── state_engine/                 # Moteur d'états déterministe
│   │   ├── state_detector.py         # Détection multi-états
│   │   ├── template_engine.py        # Sélection templates + préparation contexte
│   │   ├── pybars_renderer.py        # Rendu Handlebars (pybars3)
│   │   ├── response_validator.py     # Validation des réponses
│   │   └── crm_updater.py            # Application des mises à jour CRM
│   │
│   ├── utils/                        # Helpers métier
│   │   ├── date_examen_vtc_helper.py # Analyse dates examen (10 cas)
│   │   ├── examt3p_crm_sync.py       # Sync ExamT3P↔CRM
│   │   ├── session_helper.py         # Sélection sessions
│   │   ├── uber_eligibility_helper.py# Cas Uber A/B/D/E
│   │   ├── crm_lookup_helper.py      # Enrichissement lookups
│   │   ├── examt3p_credentials_helper.py # Extraction identifiants
│   │   ├── response_humanizer.py     # Reformulation IA
│   │   ├── thread_memory.py          # Mémoire inter-tickets (V1/V2)
│   │   ├── conversation_analyzer.py  # Analyse conversation LLM (V3)
│   │   ├── date_extractor.py         # Extraction de dates du texte
│   │   ├── intent_parser.py          # Parsing des intentions
│   │   ├── text_utils.py             # Utilitaires texte
│   │   ├── crm_note_logger.py        # Notes CRM consolidées
│   │   ├── alerts_helper.py          # Alertes temporaires
│   │   ├── date_utils.py             # Parsing dates flexible
│   │   ├── planbot_api_client.py     # Client API interne PlanBot (B2B)
│   │   ├── relations_crm_lookup.py   # Lookup CRM B2B (contact + compte)
│   │   ├── relations_response_builder.py  # Construction réponses B2B
│   │   ├── relations_response_validator.py # Validation B2B (FORBIDDEN_TERMS)
│   │   └── training_exam_consistency_helper.py # Cohérence formation/examen
│   │
│   ├── constants/                    # Constantes métier centralisées
│   │   ├── models.py                 # IDs modèles IA
│   │   ├── thresholds.py             # Seuils temporels
│   │   ├── amounts.py                # Montants métier
│   │   ├── evalbox.py                # Statuts Evalbox
│   │   ├── intents.py                # Groupements d'intentions
│   │   ├── keywords.py               # 19 listes de mots-clés
│   │   ├── sessions.py               # Types/horaires sessions
│   │   ├── departments.py            # Départements
│   │   ├── deal_stages.py            # Stages CRM
│   │   ├── emails.py                 # Adresses email
│   │   └── urls.py                   # URLs externes
│   │
│   ├── workflows/                    # Orchestration
│   │   ├── doc_ticket_workflow.py    # Workflow principal 8 étapes
│   │   └── relations_ticket_workflow.py # Workflow B2B (brouillons only)
│   │
│   ├── zoho_client.py                # Clients API Zoho (Desk + CRM)
│   └── ticket_deal_linker.py         # Liaison tickets↔deals (stratégies de base)
│
├── states/                           # Configuration State Engine
│   ├── candidate_states.yaml         # Source vérité états (42)
│   ├── state_intention_matrix.yaml   # Intentions (50) + matrice (143 entrées)
│   ├── blocks/                       # Blocs réutilisables (.md, 53)
│   ├── VARIABLES.md                  # Documentation variables Handlebars
│   └── templates/
│       ├── response_master.html      # Template master universel
│       ├── base_legacy/              # 66 templates legacy (fallback désactivé)
│       └── partials/                 # Fragments modulaires (94 .html, 17 catégories)
│           ├── intentions/           # Réponses intentions (36)
│           ├── actions/              # Actions requises (10)
│           ├── uber/                 # Conditions Uber (10)
│           ├── statuts/              # Affichage statuts (8)
│           ├── resultats/            # Résultats examen (6)
│           ├── report/               # Report date (4)
│           ├── cma/                  # Contact CMA (3)
│           ├── documents/            # Documents (3)
│           ├── alerts/               # Alertes (2)
│           ├── common/               # Communs (2)
│           ├── context/              # Contexte (2)
│           ├── credentials/          # Problèmes identifiants (2)
│           ├── warnings/             # Avertissements (2)
│           ├── alternatives/         # Alternatives (1)
│           ├── confirmations/        # Confirmations (1)
│           ├── dates/                # Proposition dates (1)
│           └── prospect/             # Prospect (1)
│
├── alerts/                           # Alertes temporaires
│   └── active_alerts.yaml            # Alertes actives (éditable)
│
├── examples/                         # Scripts d'exemple
├── tests/                            # Tests unitaires
├── docs/                             # Documentation détaillée
├── config.py                         # Configuration Pydantic
├── business_rules.py                 # Règles de routage départemental
├── webhook_server.py                 # Serveur Flask (webhook Zoho Desk)
├── run_workflow_batch.py             # Point d'entrée batch (CLI)
├── run_workflow_continuous.py        # Point d'entrée traitement continu
├── run_relations_workflow_batch.py   # Batch workflow Relations (B2B)
├── render.yaml                       # Déploiement Render (runtime python)
├── Dockerfile                        # Image Docker (non utilisée par Render)
└── CLAUDE.md                         # Guide projet (règles critiques)
```

---

## Workflow Principal (8 Étapes)

```
┌─────────────────────────────────────────────────────────────────────┐
│                    DOC TICKET WORKFLOW                               │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  STEP 1: TRIAGE AGENT                                               │
│  ├─→ Input: ticket content, deal data                               │
│  ├─→ Output: action (GO/ROUTE/SPAM), intent, session_preference     │
│  └─→ GATES: ROUTE→transfer, SPAM→close, DUPLICATE_UBER→special      │
│                                                                     │
│  STEP 2: ANALYSIS (6 sources)                                       │
│  ├─→ Ticket data extraction                                         │
│  ├─→ Deal linking (DealLinkingAgent)                                │
│  ├─→ ExamT3P credentials + data                                     │
│  ├─→ Date exam analysis (10 cas)                                    │
│  ├─→ Session selection                                              │
│  └─→ Uber eligibility check (A/B/D/E)                               │
│                                                                     │
│  STEP 3: STATE DETECTION (déterministe)                             │
│  ├─→ Évalue candidate_states.yaml par priorité                      │
│  └─→ Retourne: blocking/warning/info states                         │
│                                                                     │
│  STEP 4: TEMPLATE RENDERING                                         │
│  ├─→ Lookup STATE:INTENTION dans matrice                            │
│  ├─→ Charge template + partials                                     │
│  └─→ Remplace variables Handlebars                                  │
│                                                                     │
│  STEP 5: HUMANIZATION (optionnel)                                   │
│  ├─→ Claude Sonnet reformule                                        │
│  ├─→ Valide préservation données                                    │
│  └─→ Retourne original si validation échoue                         │
│                                                                     │
│  STEP 5b: CRM UPDATES                                               │
│  ├─→ Extrait mises à jour suggérées                                 │
│  └─→ Applique règles métier (blocage VALIDE CMA)                    │
│                                                                     │
│  STEP 6: CRM NOTE                                                   │
│  └─→ Crée note consolidée [META] (après les mises à jour CRM)       │
│                                                                     │
│  STEP 7: REPLY DELIVERY                                             │
│  └─→ Brouillon Zoho Desk ou envoi direct (auto-send avec guards)    │
│                                                                     │
│  STEP 8: VALIDATION                                                 │
│  └─→ Vérifie tous les champs requis présents                        │
│                                                                     │
│  STEP 8b: TRANSFERT DOCS CAB (si VTC hors partenariat)              │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Workflow Relations entreprises (B2B)

`src/workflows/relations_ticket_workflow.py` — workflow séparé, en mode brouillon strict, pour le département Relations entreprises :

1. **Triage B2B** : `RelationsTriageAgent` (15 intentions : devis, disponibilités, inscriptions, conventions, factures...) avec actions DRAFT / IGNORE_NOISE / ROUTE_COMPTA / ROUTE_HUMAN
2. **Lookup CRM** : `relations_crm_lookup.py` (contact + compte via l'email expéditeur)
3. **Gestionnaire du compte** : résolution de `Account.Owner.email` vers un agent Desk actif du département Relations entreprises
4. **Disponibilités** : appel en lecture seule de l'API interne PlanBot (`planbot_api_client.py`), dégradé « skipped » si non configurée
5. **Base déterministe** : `relations_response_builder.py` construit un fallback sans prix ni pièce jointe inventés
6. **Rédaction** : `RelationsResponseAgent` adapte la réponse au dernier message et à la conversation
7. **Validation** : `relations_response_validator.py` contrôle HTML, dates, montants, confirmations et disponibilités
8. **Livraison** : revalidation, affectation au gestionnaire, seconde revalidation puis brouillon Zoho Desk — jamais d'envoi automatique, jamais de mise à jour CRM

Point d'entrée batch : `run_relations_workflow_batch.py`.

---

## Sources de Données (6 sources)

| # | Source | Agent/Helper | Données extraites |
|---|--------|--------------|-------------------|
| 1 | Ticket Zoho Desk | `ZohoDeskClient` | Sujet, contenu, threads, pièces jointes |
| 2 | Deal CRM | `DealLinkingAgent` | Champs deal, statut Evalbox, dates, montant |
| 3 | ExamT3P | `ExamT3PAgent` | Statut dossier, documents, paiements, num_dossier |
| 4 | Sessions CRM | `session_helper` | Sessions disponibles (jour/soir) |
| 5 | Dates examen CRM | `date_examen_vtc_helper` | Prochaines dates, départements |
| 6 | Alertes temporaires | `alerts_helper` | Bugs en cours, situations spéciales |

---

## Structures de Données Principales

### crm_updates (extrait par IA)
```python
{
    'Date_examen_VTC': '2026-03-31',      # Date string → À MAPPER vers ID
    'Session_choisie': 'Formation soir...', # Nom → À MAPPER vers ID
    'Preference_horaire': 'soir'           # Texte simple, pas de mapping
}
```

### examt3p_data
```python
{
    'compte_existe': True,
    'connection_test_success': True,
    'identifiant': 'email@example.com',
    'mot_de_passe': '****',
    'credentials_source': 'crm',  # ou 'threads'
    'statut_dossier': 'En cours de composition',
    'num_dossier': '00038886',
    'documents': [...],
    'paiements': [...],
    'departement': '75'
}
```

### TriageAgent result
```python
{
    'action': 'GO' | 'ROUTE' | 'SPAM' | 'DUPLICATE_UBER',
    'target_department': 'DOC' | 'Contact' | etc,
    'detected_intent': 'DEMANDE_DATES_FUTURES',     # Intention principale
    'primary_intent': 'DEMANDE_DATES_FUTURES',      # Alias
    'secondary_intents': ['QUESTION_SESSION'],       # Intentions secondaires
    'intent_context': {
        'is_urgent': bool,
        'mentions_force_majeure': bool,
        'force_majeure_type': 'medical' | 'death' | 'accident' | 'childcare',
        'wants_earlier_date': bool,
        'session_preference': 'jour' | 'soir' | None
    }
}
```

### DetectedStates (multi-états)
```python
{
    'blocking_state': DetectedState | None,   # Si présent, stoppe workflow
    'warning_states': [DetectedState, ...],   # Alertes à inclure
    'info_states': [DetectedState, ...],      # États combinables
    'primary_state': DetectedState,           # Rétrocompatibilité
    'all_states': [DetectedState, ...]        # Tous les états détectés
}
```

---

## Points d'Entrée

### CLI (run_workflow_batch.py)
```bash
python run_workflow_batch.py --status              # Voir le statut de la file
python run_workflow_batch.py --count 5 --dry-run   # Test sur 5 tickets
python run_workflow_batch.py --count 10            # Traiter 10 tickets
python run_workflow_batch.py --ticket <ticket_id>  # Un ticket spécifique
python run_workflow_batch.py --count 10 --auto-send # Envoi direct (guard rails)
```

### Traitement continu
```bash
python run_workflow_continuous.py
```

### Serveur webhook (Zoho Desk)
```bash
python webhook_server.py   # Flask, endpoints /health, /webhook/zoho-desk, etc.
```

### Programmatique
```python
from src.workflows.doc_ticket_workflow import DOCTicketWorkflow

workflow = DOCTicketWorkflow()
result = workflow.process_ticket(ticket_id, auto_create_draft=False)
```

### Scripts d'analyse
```bash
python analyze_lot.py 11 20           # Analyser tickets 11-20
python list_recent_tickets.py         # Lister tickets DOC ouverts
python close_spam_tickets.py data.json # Clôturer SPAM
```

---

## Clients API Zoho

### ZohoDeskClient
```python
from src.zoho_client import ZohoDeskClient

client = ZohoDeskClient()
ticket = client.get_ticket(ticket_id)
threads = client.get_all_threads_with_full_content(ticket_id)
client.create_ticket_reply_draft(ticket_id, content, content_type="html")
client.update_ticket(ticket_id, {"cf": {"cf_opportunite": "..."}})
client.move_ticket_to_department(ticket_id, "Contact")
```

### ZohoCRMClient
```python
from src.zoho_client import ZohoCRMClient

client = ZohoCRMClient()
deal = client.get_deal(deal_id)
client.update_deal(deal_id, {"Field_Name": value})
client.add_deal_note(deal_id, note_title, note_content)
client.search_deals(criteria="(Email:equals:test@example.com)")
client.get_record('Dates_Examens_VTC_TAXI', record_id)  # Enrichir lookup
```

---

## Configuration

### Variables d'environnement (.env)
```
ZOHO_CLIENT_ID=...
ZOHO_CLIENT_SECRET=...
ZOHO_REFRESH_TOKEN=...
ANTHROPIC_API_KEY=...
```

### config.py (Pydantic Settings)
```python
from config import settings

settings.zoho_client_id
settings.anthropic_api_key
settings.agent_model  # Legacy — utiliser src/constants/models.py à la place
```

---

## Diagrammes

Voir `docs/architecture-diagrams.md` pour les diagrammes Mermaid détaillés :
- Workflow complet
- State Engine flow
- Template selection
- Data flow

---

## Coûts API (estimation par ticket)

| Composant | Modèle | Coût |
|-----------|--------|------|
| Extraction identifiants | Haiku 4.5 (`MODEL_EXTRACTION`) | ~$0.001 |
| Agent Trieur | Sonnet 4.6 (`MODEL_TRIAGE`) | ~$0.01 |
| Response Humanizer | Sonnet 4.6 (`MODEL_HUMANIZER`) | ~$0.036 |
| Conversation Analyzer (V3) | Sonnet 4.5 (`MODEL_CONVERSATION`) | ~$0.01-0.02 |
| Next steps note CRM | Sonnet 4.6 (`MODEL_TRIAGE`) | ~$0.01 |
| **Total** | | **~$0.06-0.08** |

Source unique des IDs modèles : `src/constants/models.py`.
