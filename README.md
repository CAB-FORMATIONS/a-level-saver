# A-Level Saver - Automatisation Zoho Desk & CRM

Système d'agents IA pour automatiser la gestion des tickets Zoho Desk et la mise à jour des opportunités Zoho CRM (CAB Formations - formation VTC Uber).

## 🎯 Fonctionnalités

### Workflow DOC (candidats VTC)
- ✅ Triage automatique des tickets (GO / ROUTE / SPAM / DUPLICATE_UBER)
- ✅ Liaison ticket ↔ opportunité CRM (DealLinkingAgent)
- ✅ State Engine déterministe (42 états × 50 intentions → templates)
- ✅ Génération de réponses personnalisées et empathiques (Humanizer IA)
- ✅ Mise à jour automatique du CRM et création de brouillons Zoho Desk

### Workflow Relations entreprises (B2B)
- ✅ Triage 15 intentions B2B (devis, disponibilités, conventions, factures...)
- ✅ Lookup CRM de l'expéditeur (contact + compte)
- ✅ Affectation du ticket au propriétaire CRM du compte avant création du brouillon
- ✅ Rédaction contextualisée par `RelationsResponseAgent` avec fallback déterministe
- ✅ Fermeture automatique des démarchages hors formation uniquement sur signaux déterministes forts
- ✅ Brouillons uniquement — jamais d'envoi automatique, jamais de mise à jour CRM

### Pipeline

```
Webhook / Batch → DOCTicketWorkflow
  → Triage → Deal Linking → State Engine → Templates → Humanizer
  → Brouillon Zoho Desk (+ note CRM)
```

Le workflow Relations entreprises (`src/workflows/relations_ticket_workflow.py`)
suit le même principe mais reste en mode brouillon strict.

## 🚀 Démarrage rapide

### Installation

```bash
# Cloner le repository
git clone <repository-url>
cd a-level-saver

# Installer les dépendances
pip install -r requirements.txt

# Configurer les variables d'environnement
cp .env.example .env
# Éditez .env avec vos credentials Zoho et Anthropic
```

### Configuration

1. **Obtenir les credentials Zoho** (voir [GUIDE.md](GUIDE.md#configuration))
2. **Obtenir une clé API Anthropic** sur https://console.anthropic.com
3. **Remplir le fichier .env** avec vos credentials

### Premier test

```bash
# Voir le statut de la file de tickets
python run_workflow_batch.py --status

# Test sur 5 tickets en mode dry-run (pas de draft/CRM updates)
python run_workflow_batch.py --count 5 --dry-run

# Traiter un ticket spécifique
python run_workflow_batch.py --ticket <ticket_id> --dry-run

# Serveur webhook (déclenchement par Zoho Desk)
python webhook_server.py
```

## 📖 Documentation

- **[GUIDE.md](GUIDE.md)** - Guide complet d'utilisation
- **[WEBHOOK_QUICKSTART.md](WEBHOOK_QUICKSTART.md)** - 🚀 Démarrer le webhook en 5 minutes
- **[WEBHOOK_SETUP.md](WEBHOOK_SETUP.md)** - Configuration complète du webhook
- **[examples/](examples/)** - Exemples de code

## 🔔 Webhook Automation (Nouveau !)

Le système peut maintenant être déclenché automatiquement via webhook Zoho Desk :

```bash
# 1. Démarrer le serveur webhook
python webhook_server.py

# 2. Tester localement
python test_webhook.py --test simple

# 3. Exposer avec ngrok (pour tests)
ngrok http 5000
```

**Configuration Zoho Desk :**
1. Setup → Automation → Webhooks → Add Webhook
2. URL : `https://votre-domaine.com/webhook/zoho-desk`
3. Events : "Ticket Created", "Ticket Updated"
4. Configurer le secret partagé `ZOHO_WEBHOOK_SECRET` dans `.env` (header `X-Webhook-Secret`)

**Guide rapide :** [WEBHOOK_QUICKSTART.md](WEBHOOK_QUICKSTART.md)

## 🏗️ Architecture

```
a-level-saver/
├── src/
│   ├── agents/
│   │   ├── base_agent.py               # Classe de base pour les agents IA
│   │   ├── triage_agent.py             # Triage GO/ROUTE/SPAM + intentions
│   │   ├── deal_linking_agent.py       # Liaison ticket ↔ deal CRM
│   │   ├── crm_update_agent.py         # Mises à jour CRM (mapping, guards)
│   │   ├── examt3p_agent.py            # Extraction dossier ExamT3P
│   │   ├── relations_triage_agent.py   # Triage B2B (15 intentions)
│   │   └── relations_response_agent.py # Rédaction B2B contextualisée et sécurisée
│   ├── workflows/
│   │   ├── doc_ticket_workflow.py      # Workflow principal DOC (8 étapes)
│   │   └── relations_ticket_workflow.py # Workflow B2B (brouillons only)
│   ├── state_engine/
│   │   ├── state_detector.py           # Détection des 42 états
│   │   ├── template_engine.py          # Sélection template + contexte
│   │   ├── pybars_renderer.py          # Rendu Handlebars (pybars3)
│   │   ├── response_validator.py       # Validation des réponses
│   │   └── crm_updater.py              # Application des updates CRM
│   ├── utils/                          # Helpers (thread_memory, dates, etc.)
│   ├── constants/                      # Constantes métier (models, thresholds...)
│   ├── zoho_client.py                  # Clients API Zoho (Desk & CRM)
│   └── ticket_deal_linker.py           # Stratégies de liaison de base
├── states/
│   ├── candidate_states.yaml           # 42 états
│   ├── state_intention_matrix.yaml     # 50 intentions + matrice État×Intention
│   ├── blocks/                         # 53 blocs de contenu
│   └── templates/                      # response_master.html + partials + base_legacy
├── webhook_server.py                   # Serveur Flask (webhook Zoho Desk)
├── run_workflow_batch.py               # Traitement batch (CLI)
├── run_workflow_continuous.py          # Traitement continu
├── config.py                           # Configuration centralisée
├── business_rules.py                   # Règles de routage départemental
├── render.yaml                         # Déploiement Render (runtime python)
├── Dockerfile                          # Image Docker (non utilisée par Render)
└── requirements.txt                    # Dépendances Python
```

## 💡 Cas d'usage

### 1. Traiter un ticket DOC complet
```python
from src.workflows.doc_ticket_workflow import DOCTicketWorkflow

workflow = DOCTicketWorkflow()
result = workflow.process_ticket(
    ticket_id="198709000438366101",
    auto_create_draft=True,
    auto_update_crm=True,
    auto_update_ticket=True
)
```

### 2. Traiter un ticket Relations entreprises (B2B)
```python
from src.workflows.relations_ticket_workflow import RelationsTicketWorkflow

workflow = RelationsTicketWorkflow()
result = workflow.process_ticket(
    ticket_id="198709000438366101",
    auto_create_draft=True  # Brouillon uniquement, jamais d'envoi auto
)
```

### 3. Traitement par lots (CLI)
```bash
python run_workflow_batch.py --count 10          # Traiter 10 tickets
python run_workflow_batch.py --ticket <id>       # Un ticket spécifique
python run_workflow_batch.py --count 5 --dry-run # Mode test
```

## 🔧 Technologies utilisées

- **Python 3.9+**
- **Anthropic Claude** - Agent IA pour l'analyse et les recommandations
- **Zoho Desk API** - Gestion des tickets de support
- **Zoho CRM API** - Gestion des opportunités
- **OAuth2** - Authentification sécurisée

## 📊 Fonctionnalités avancées

- **Retry automatique** avec backoff exponentiel
- **Gestion du cache de tokens** OAuth2
- **Logs structurés** pour monitoring
- **Historique de conversation** pour contexte IA
- **Traitement par lots** optimisé
- **Workflows personnalisables**

## 🔒 Sécurité

- Authentification OAuth2 avec refresh tokens
- Variables d'environnement pour les secrets
- Validation des entrées
- Gestion sécurisée des erreurs

## 🤝 Contribution

Les contributions sont les bienvenues ! Consultez le guide de contribution pour plus d'informations.

## 📄 Licence

[À définir]

## 📞 Support

Pour plus d'informations, consultez le [GUIDE.md](GUIDE.md) ou ouvrez une issue.
