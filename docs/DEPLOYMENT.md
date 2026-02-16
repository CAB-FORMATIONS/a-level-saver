# Guide de Deploiement & Operations

## Architecture de Deploiement

Le systeme A-Level Saver est deploye sur **Render** en tant que Web Service Docker. Le serveur Flask recoit les webhooks de Zoho Desk et declenche le workflow de traitement des tickets.

```
                          +--------------------------+
                          |      Zoho Desk           |
                          |  (Workflow Rule + Deluge)|
                          +-----------+--------------+
                                      |
                                      | POST /webhook/zoho-desk
                                      | Header: X-Webhook-Secret
                                      v
                          +--------------------------+
                          |   Render Web Service     |
                          |   (Docker container)     |
                          |                          |
                          |   Gunicorn (2 workers)   |
                          |   Flask (webhook_server) |
                          +-----------+--------------+
                                      |
                    +-----------------+-----------------+
                    |                 |                 |
                    v                 v                 v
           +-------------+   +-------------+   +----------------+
           | Zoho CRM    |   | Anthropic   |   | ExamT3P        |
           | Zoho Desk   |   | Claude API  |   | (Playwright)   |
           | (REST API)  |   | (LLM)       |   | (Web scraping) |
           +-------------+   +-------------+   +----------------+
```

### Composants

| Composant | Role | Technologie |
|-----------|------|-------------|
| **Webhook Server** | Recoit les notifications Zoho Desk | Flask + Gunicorn |
| **DOCTicketWorkflow** | Orchestre le traitement complet d'un ticket | Python |
| **Zoho Desk Client** | API REST pour tickets, threads, brouillons | `src/zoho_client.py` |
| **Zoho CRM Client** | API REST pour deals, contacts, notes | `src/zoho_client.py` |
| **Anthropic Client** | Triage, humanisation, analyse conversation | API Claude |
| **ExamT3P Scraper** | Extraction donnees portail CMA | Playwright + Chromium |

---

## Dockerfile (explique)

Fichier : `Dockerfile`

```dockerfile
FROM python:3.11-slim
```
Image de base Python 3.11 minimale. La version slim evite les dependances inutiles.

```dockerfile
# Install Playwright system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
    libcairo2 libasound2 libwayland-client0 \
    && rm -rf /var/lib/apt/lists/*
```
Librairies systeme requises par Chromium (Playwright). Sans ces dependances, le navigateur headless ne peut pas demarrer. Le `rm -rf /var/lib/apt/lists/*` reduit la taille de l'image.

```dockerfile
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
```
Installation des dependances Python. Le `--no-cache-dir` evite de stocker le cache pip dans l'image.

```dockerfile
# Install Playwright Chromium browser
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/render/.cache/ms-playwright
RUN mkdir -p /opt/render/.cache/ms-playwright && playwright install chromium
```
Installe le navigateur Chromium de Playwright. Le chemin `/opt/render/.cache/ms-playwright` est le chemin par defaut utilise par Render pour le cache Playwright. Cette variable d'environnement est **critique** : sans elle, Playwright cherche le navigateur dans un chemin different et echoue au lancement.

**Historique** : Ce chemin a fait l'objet de plusieurs fixes (commits `8da7cb3`, `dfedd9b`, `4b5de26`). La version Docker n'utilise pas `--with-deps` car les dependances systeme sont deja installees manuellement ci-dessus.

```dockerfile
COPY . .
EXPOSE 10000
CMD ["gunicorn", "webhook_server:app", "--bind", "0.0.0.0:10000", "--workers", "2", "--timeout", "120"]
```
- **Port 10000** : port par defaut de Render pour les Web Services
- **2 workers** : suffisant pour le volume de tickets (traitement asynchrone en background thread)
- **Timeout 120s** : les workflows peuvent prendre 30-60s (scraping ExamT3P + appels LLM)

### Dependances critiques dans requirements.txt

| Package | Version | Role |
|---------|---------|------|
| `Flask` | 3.0.0 | Serveur webhook |
| `gunicorn` | 21.2.0 | Serveur WSGI production |
| `playwright` | 1.40.0 | Scraping ExamT3P (navigateur headless) |
| `anthropic` | >=0.40.0 | API Claude (triage, humanisation) |
| `pybars3` | 0.9.7 | Moteur de templates Handlebars |
| `PyMeta3` | ==0.5.1 | Dependance de pybars3 — **version pionnee** |
| `pydantic-settings` | 2.1.0 | Gestion des variables d'environnement |
| `gender-guesser` | 0.4.0 | Detection du genre pour personnaliser les reponses |
| `tenacity` | 8.2.3 | Retry automatique pour les appels API |

**PyMeta3==0.5.1** : La version est pinned exactement car des versions plus recentes cassent la compilation des partials Handlebars dans pybars3 (commit `f86c176`).

---

## Variables d'Environnement

### Variables requises

| Variable | Description | Exemple | Fichier source |
|----------|-------------|---------|----------------|
| `ZOHO_CLIENT_ID` | OAuth Client ID Zoho (Desk) | `1000.XXXX...` | `config.py` |
| `ZOHO_CLIENT_SECRET` | OAuth Client Secret Zoho (Desk) | `abcdef1234...` | `config.py` |
| `ZOHO_REFRESH_TOKEN` | OAuth Refresh Token Zoho (Desk) | `1000.XXXX...` | `config.py` |
| `ZOHO_DESK_ORG_ID` | ID de l'organisation Zoho Desk | `648790851` | `config.py` |
| `ANTHROPIC_API_KEY` | Cle API Anthropic pour Claude | `sk-ant-...` | `config.py` |

### Variables optionnelles (avec valeur par defaut)

| Variable | Description | Defaut | Fichier source |
|----------|-------------|--------|----------------|
| `ZOHO_DATACENTER` | Datacenter Zoho | `com` | `config.py` |
| `ZOHO_CRM_CLIENT_ID` | OAuth Client ID CRM (si different du Desk) | (fallback sur `ZOHO_CLIENT_ID`) | `config.py` |
| `ZOHO_CRM_CLIENT_SECRET` | OAuth Client Secret CRM | (fallback sur `ZOHO_CLIENT_SECRET`) | `config.py` |
| `ZOHO_CRM_REFRESH_TOKEN` | OAuth Refresh Token CRM | (fallback sur `ZOHO_REFRESH_TOKEN`) | `config.py` |
| `ZOHO_DESK_EMAIL_DOC` | Adresse email de reponse dept DOC | `None` | `config.py` |
| `ZOHO_DESK_EMAIL_CONTACT` | Adresse email de reponse dept Contact | `None` | `config.py` |
| `ZOHO_DESK_EMAIL_COMPTA` | Adresse email de reponse dept Comptabilite | `None` | `config.py` |
| `ZOHO_DESK_EMAIL_DEFAULT` | Email de reponse par defaut (fallback) | `None` | `config.py` |
| `AGENT_MODEL` | Modele IA par defaut (legacy) | `claude-sonnet-4-5-20250929` | `config.py` |
| `AGENT_MAX_TOKENS` | Max tokens pour les reponses IA | `4096` | `config.py` |
| `AGENT_TEMPERATURE` | Temperature LLM | `0.7` | `config.py` |
| `LOG_LEVEL` | Niveau de log | `INFO` | `config.py` |
| `ESCALATION_AGENT_ID` | ID agent Zoho Desk pour escalade | `198709000096599317` | `config.py` |
| `ESCALATION_AGENT_NAME` | Nom de l'agent pour escalade | `Lamia Serbouty` | `config.py` |
| `RGPD_REFERENT_EMAIL` | Email du referent RGPD | `jc@cab-formations.fr` | `config.py` |

### Variables webhook (dev local uniquement)

| Variable | Description | Defaut | Fichier source |
|----------|-------------|--------|----------------|
| `ZOHO_WEBHOOK_SECRET` | Secret partage pour authentification webhook | `''` (pas d'auth) | `webhook_server.py` |
| `WEBHOOK_HOST` | Hote d'ecoute Flask | `0.0.0.0` | `webhook_server.py` |
| `WEBHOOK_PORT` | Port d'ecoute Flask | `5000` | `webhook_server.py` |
| `WEBHOOK_TEST_URL` | URL de base pour `test_webhook.py` | `http://localhost:5000` | `test_webhook.py` |
| `FLASK_DEBUG` | Mode debug Flask | `false` | `webhook_server.py` |

### Variables internes (flags de controle)

| Variable | Description | Defaut | Fichier source |
|----------|-------------|--------|----------------|
| `SKIP_DRAFT_CHECK` | Ignorer la verification de brouillon existant | non defini | `doc_ticket_workflow.py` |
| `PLAYWRIGHT_BROWSERS_PATH` | Chemin vers les navigateurs Playwright | `/opt/render/.cache/ms-playwright` | `Dockerfile` |

### Configuration sur Render

Sur Render, les variables d'environnement sont configurees dans le dashboard du service (`Environment` tab). Les variables sensibles (cles API, secrets OAuth) doivent etre ajoutees comme **Secret Environment Variables**.

Le fichier `render.yaml` definit deux variables supplementaires au niveau du build :

```yaml
envVars:
  - key: PYTHON_VERSION
    value: "3.11.8"
  - key: PLAYWRIGHT_BROWSERS_PATH
    value: /opt/render/.cache/ms-playwright
```

---

## Configuration Zoho Desk

### Flux de declenchement

```
Ticket cree/modifie dans Zoho Desk (dept DOC)
        |
        v
Workflow Rule (condition: departement = DOC)
        |
        v
Custom Function (Deluge)
        |
        v
invokeurl → POST https://<render-url>/webhook/zoho-desk
        Header: X-Webhook-Secret: <secret>
        Body: {"ticket_id": "<ticket_id>"}
```

### Configuration de la Workflow Rule

1. Aller dans **Zoho Desk > Setup > Automation > Workflow Rules**
2. Creer une nouvelle regle :
   - **Module** : Tickets
   - **Condition** : Departement = DOC (ID: `198709000025523146`)
   - **Action** : Executer une Custom Function

### Script Deluge (Custom Function)

Le script Deluge envoie un `POST` au webhook Render avec l'ID du ticket. Exemple simplifie :

```deluge
// Recuperer l'ID du ticket
ticket_id = ticket.get("id");

// Headers d'authentification
headers = Map();
headers.put("X-Webhook-Secret", "VOTRE_SECRET");
headers.put("Content-Type", "application/json");

// Corps de la requete
body = Map();
body.put("ticket_id", ticket_id);

// Appel au webhook
response = invokeurl
[
    url: "https://a-level-saver.onrender.com/webhook/zoho-desk"
    type: POST
    parameters: body.toString()
    headers: headers
];

info response;
```

**Note** : Les scripts Deluge complets (historiques, pour Zia Agents) se trouvent dans `zia-agent/deluge/`. Le systeme actuel utilise un appel webhook direct, pas Zia Agents.

### Endpoints du webhook

| Endpoint | Methode | Auth | Description |
|----------|---------|------|-------------|
| `/health` | GET | Non | Health check (utilise par Render) |
| `/webhook/zoho-desk` | POST | `X-Webhook-Secret` | Endpoint principal (Deluge) — traitement asynchrone |
| `/webhook/test` | POST | Non | Endpoint de test — traitement synchrone, retourne le resultat complet |
| `/webhook/stats` | GET | Non | Configuration et statut du webhook |

### Traitement asynchrone

L'endpoint `/webhook/zoho-desk` retourne `200 OK` immediatement et traite le ticket dans un **background thread** (`threading.Thread`, `daemon=True`). Cela evite les timeouts Deluge (limite de 10s).

L'endpoint `/webhook/test` est synchrone et retourne le resultat complet du workflow. Il est utile pour le debug mais ne doit pas etre appele par Deluge en production.

---

## Fichier render.yaml

Fichier : `render.yaml`

```yaml
services:
  - type: web
    name: a-level-saver
    runtime: python
    buildCommand: "pip install -r requirements.txt && playwright install --with-deps chromium"
    startCommand: "gunicorn webhook_server:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120"
    healthCheckPath: /health
    envVars:
      - key: PYTHON_VERSION
        value: "3.11.8"
      - key: PLAYWRIGHT_BROWSERS_PATH
        value: /opt/render/.cache/ms-playwright
```

**Note** : Ce fichier est utilise pour le deploiement natif (sans Docker). Le deploiement actuel utilise le **Dockerfile**, qui est prefere car il donne un controle plus fin sur les dependances systeme de Playwright. Le `render.yaml` est conserve comme reference.

Avec le Dockerfile, Render detecte automatiquement sa presence et l'utilise pour le build. Le `healthCheckPath: /health` dans `render.yaml` configure le health check automatique de Render.

---

## Demarrage Local

### Prerequis

- Python 3.11+
- Playwright et Chromium installes
- Fichier `.env` configure (copier `.env.example`)

### Installation

```bash
# Cloner le repository
git clone <repo-url>
cd a-level-saver

# Installer les dependances
pip install -r requirements.txt

# Installer Chromium pour Playwright
playwright install chromium

# Configurer les variables d'environnement
cp .env.example .env
# Editer .env avec vos cles API reelles
```

### Lancer le serveur webhook (dev)

```bash
# Mode developpement (Flask dev server)
python webhook_server.py

# Le serveur ecoute sur http://localhost:5000
# Health check : GET http://localhost:5000/health
```

En mode production locale :

```bash
gunicorn webhook_server:app --bind 0.0.0.0:5000 --workers 2 --timeout 120
```

### Tester un ticket individuellement

```bash
# Workflow complet (analyse + brouillon + CRM)
python test_doc_workflow_with_examt3p.py <ticket_id>

# Dry run (analyse sans modification)
python test_doc_workflow_with_examt3p.py <ticket_id> --dry-run

# Sans brouillon
python test_doc_workflow_with_examt3p.py <ticket_id> --no-draft

# Sans mise a jour CRM
python test_doc_workflow_with_examt3p.py <ticket_id> --no-crm-update

# Traitement bulk (tous les tickets DOC ouverts)
python test_doc_workflow_with_examt3p.py --bulk --dry-run --output results.json
```

### Tester le webhook localement

```bash
# Lancer le serveur
python webhook_server.py

# Dans un autre terminal, lancer les tests
python test_webhook.py --test all

# Tests individuels
python test_webhook.py --test health
python test_webhook.py --test simple --ticket-id 198709000449714052
python test_webhook.py --test signature --secret "votre_secret"
```

Le script `test_webhook.py` supporte 6 types de tests :
- `health` : Verifie le endpoint /health
- `stats` : Verifie le endpoint /webhook/stats
- `simple` : Envoie un ticket via /webhook/test (synchrone, sans auth)
- `signature` : Envoie un ticket via /webhook/zoho-desk (avec auth X-Webhook-Secret)
- `real` : Utilise des donnees de tickets reels depuis des fichiers JSON
- `invalid` : Teste le error handling avec des payloads invalides

### Scripts batch (traitement en masse)

```bash
# Traitement batch avec compteur
python run_workflow_batch.py --count 10
python run_workflow_batch.py --count 10 --dry-run
python run_workflow_batch.py --ticket 198709000449714052

# Traitement continu (boucle sur les nouveaux tickets)
python run_workflow_continuous.py

# Health check post-batch
python batch_health_check.py data/batch_results_20260215_081031_cycle1.json
python batch_health_check.py --latest          # Dernier fichier de resultats
python batch_health_check.py --latest --json   # Sortie JSON
```

---

## Monitoring & Logs

### Logs applicatifs

Les logs sont configures dans `src/utils/logging_config.py` :

| Destination | Niveau | Format |
|-------------|--------|--------|
| Console (stdout) | INFO | `%(asctime)s - %(name)s - %(levelname)s - %(message)s` |
| Fichier `logs/automation.log` | DEBUG | Idem + `[%(filename)s:%(lineno)d]` |

Le niveau de log est controle par la variable `LOG_LEVEL` (defaut: `INFO`).

### Dashboard Render

- Aller sur [dashboard.render.com](https://dashboard.render.com)
- Selectionner le service `a-level-saver`
- Onglet **Logs** pour les logs en temps reel
- Onglet **Events** pour l'historique des deploiements

### Health check

Render interroge periodiquement `GET /health` pour verifier que le service est en vie. La reponse inclut :

```json
{
  "status": "healthy",
  "service": "a-level-saver-webhook",
  "timestamp": "2026-02-16T10:30:00.000000",
  "active_threads": 3
}
```

Le nombre de `active_threads` permet de surveiller les workflows en cours de traitement.

### Patterns de logs a surveiller

| Pattern | Signification | Action |
|---------|---------------|--------|
| `[BG] Starting workflow for ticket` | Debut du traitement d'un ticket | Normal |
| `[BG] Ticket XXX done` | Fin du traitement | Verifier `stage` et `errors` |
| `[BG] Ticket XXX FAILED` | Erreur fatale | Investiguer immediatement |
| `BROUILLON EXISTANT DETECTE` | Ticket deja traite | Normal (skip) |
| `INSTANT MESSAGE detecte` | Ticket SalesIQ (chat) | Skip automatique |
| `X-Webhook-Secret mismatch` | Tentative d'acces non autorisee | Verifier la configuration |
| `Failed to compile partial` | Erreur de syntaxe Handlebars | Corriger le template |
| `credentials_login_failed` | Mot de passe ExamT3P change par le candidat | Normal (gere par template) |

### Health check post-batch

Apres un traitement batch, le script `batch_health_check.py` analyse automatiquement les resultats pour detecter :

- **CRITICAL** : Brouillon vide ou casse
- **ERROR** : Contenu faux ou dangereux
- **WARNING** : Incoherence detectable
- **INFO** : Patterns cross-ticket, degradation qualite

```bash
python batch_health_check.py --latest
```

---

## Troubleshooting

### 1. Playwright / Chromium ne demarre pas

**Symptome** : `Error: Browser not found` ou `Failed to launch chromium`

**Causes possibles** :
- `PLAYWRIGHT_BROWSERS_PATH` ne pointe pas vers le bon repertoire
- Les dependances systeme manquent

**Solution** :
```bash
# Verifier le chemin
echo $PLAYWRIGHT_BROWSERS_PATH
ls -la /opt/render/.cache/ms-playwright/

# Reinstaller
playwright install chromium
```

**Historique** :
- Commit `4b5de26` : Alignement du chemin avec le defaut Render (`/opt/render/.cache/ms-playwright`)
- Commit `dfedd9b` : Ajout du Dockerfile avec installation manuelle des dependances
- Commit `8da7cb3` : Ajout de `--with-deps` pour le mode natif (render.yaml)
- Commit `b16451c` : Retrait de `--with-deps` (remplace par le Dockerfile)

### 2. PyMeta3 / pybars3 — erreur de compilation des partials

**Symptome** : `Failed to compile partial` ou templates rendus en `{{> partials/...}}` brut

**Cause** : Version de PyMeta3 incompatible avec pybars3.

**Solution** : Verifier que `PyMeta3==0.5.1` est installe (version exacte pionnee dans `requirements.txt`).

```bash
pip show PyMeta3
# Doit afficher: Version: 0.5.1
```

**Historique** : Commit `f86c176` — les versions plus recentes de PyMeta3 cassent la compilation des grammaires Handlebars.

### 3. Health check echoue sur Render

**Symptome** : Le service redemarre en boucle, statut "Unhealthy" sur le dashboard.

**Causes possibles** :
- Le port n'est pas 10000 (defaut Render pour Docker)
- Erreur au demarrage de Flask (variable d'environnement manquante)
- Timeout au demarrage (Playwright download trop lent)

**Verification** :
```bash
# Verifier que le port correspond
# Dockerfile : EXPOSE 10000
# CMD : --bind 0.0.0.0:10000

# Verifier les variables requises dans les logs Render
# Chercher: "pydantic_settings.SettingsError"
```

### 4. Erreur OAuth Zoho "Invalid refresh token"

**Symptome** : `401 Unauthorized` sur les appels API Zoho

**Causes possibles** :
- Le refresh token a expire (tokens Zoho expirent si non utilises pendant ~6 mois)
- Les credentials CRM et Desk sont melangees

**Solution** :
1. Regenerer le refresh token via la console Zoho API
2. Mettre a jour `ZOHO_REFRESH_TOKEN` dans les variables d'environnement Render
3. Si CRM et Desk utilisent des credentials differentes, verifier aussi `ZOHO_CRM_REFRESH_TOKEN`

**Note** : Le `ZohoCRMClient` utilise un fallback automatique :
```python
# src/zoho_client.py ligne 682-684
self._crm_client_id = settings.zoho_crm_client_id or settings.zoho_client_id
self._crm_client_secret = settings.zoho_crm_client_secret or settings.zoho_client_secret
self._crm_refresh_token = settings.zoho_crm_refresh_token or settings.zoho_refresh_token
```

### 5. Webhook non declenche depuis Zoho Desk

**Symptome** : Les tickets arrivent dans le departement DOC mais le webhook n'est pas appele.

**Verification** :
1. Verifier que la Workflow Rule est **active** dans Zoho Desk
2. Verifier que la Custom Function (Deluge) pointe vers la bonne URL
3. Verifier le `X-Webhook-Secret` dans le script Deluge
4. Consulter les logs Deluge dans Zoho Desk (Setup > Automation > Actions log)

### 6. Timeout Gunicorn

**Symptome** : `[CRITICAL] WORKER TIMEOUT` dans les logs

**Cause** : Un workflow prend plus de 120 secondes.

**Note** : Ce n'est normalement pas un probleme car l'endpoint `/webhook/zoho-desk` retourne `200` immediatement et traite le ticket dans un background thread. Le timeout Gunicorn ne s'applique qu'a la requete HTTP, pas au traitement en background.

Si le timeout se produit sur `/webhook/test` (endpoint synchrone), c'est normal pour des tickets complexes. Utiliser plutot `/webhook/zoho-desk` en production.

### 7. Crash lors de l'enrichissement de donnees

**Symptome** : `NoneType has no attribute 'get'` dans `template_engine.py`

**Cause** : Les lookups CRM ou le session_record peuvent etre `None` si le deal n'a pas certains champs.

**Historique** : Plusieurs fixes pour gerer les `None` :
- Commit `02198db` : Tous les crashes `enriched_lookups/session_record None` dans template_engine
- Commit `2366da7` : Crash `session_record None` dans `_flatten_session_options_filtered`
- Commit `b840a10` : Crash `enriched_lookups None` + `rule_go_override UnboundLocalError`
- Commit `867526c` : `rule_go_override UnboundLocalError` quand pas de deal data

### 8. Erreur d'encodage (Windows)

**Symptome** : `UnicodeEncodeError` avec des emojis dans les logs

**Cause** : La console Windows ne supporte pas UTF-8 par defaut.

**Solution** : Les scripts batch incluent deja le fix :
```python
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
```

Ce probleme ne concerne que le developpement local sur Windows, pas le deploiement Render (Linux).

---

## Historique des Fixes Deploiement

Chronologie des commits lies au deploiement (du plus ancien au plus recent) :

| Commit | Description | Probleme resolu |
|--------|-------------|-----------------|
| `76ccf65` | Ajout ExamT3PAgent avec Playwright | Premiere integration Playwright |
| `cc6798e` | Ajout scripts exament3p_playwright | Scripts d'extraction ExamT3P |
| `170c46f` | Fix Playwright Chromium path | Retrait du `executable_path` hardcode |
| `09f320c` | Script de test connexion Playwright | Test minimal de connexion ExamT3P |
| `58f3088` | Migration vers pybars3 | Remplacement du parsing regex Handlebars |
| `73ad7b6` | Webhook server + deploiement Render | Premiere mise en production |
| `b16451c` | Ajout gender-guesser + fix Playwright | Retrait de `--with-deps` (passe au Dockerfile) |
| `4e1b2e0` | Webhook async + auth par header | Traitement background + X-Webhook-Secret |
| `f86c176` | Pin PyMeta3==0.5.1 | Fix compilation pybars3 sur Render |
| `8da7cb3` | Ajout --with-deps a playwright install | Fix dependances Playwright mode natif |
| `dfedd9b` | Ajout Dockerfile | Controle precis des dependances systeme |
| `4b5de26` | Alignement chemin Playwright | Fix `PLAYWRIGHT_BROWSERS_PATH` pour Render |

### Modeles IA utilises

Les modeles sont centralises dans `src/constants/models.py` :

| Constante | Modele | Usage |
|-----------|--------|-------|
| `MODEL_TRIAGE` | `claude-sonnet-4-20250514` | Agent trieur (GO/ROUTE/SPAM) |
| `MODEL_HUMANIZER` | `claude-sonnet-4-20250514` | Reformulation naturelle |
| `MODEL_EXTRACTION` | `claude-3-5-haiku-20241022` | Extraction d'identifiants |
| `MODEL_CONVERSATION` | `claude-sonnet-4-5-20250929` | Analyse conversation (V3) |
| `MODEL_PERSONALIZATION` | `claude-sonnet-4-5-20250929` | Personnalisation reponse |

---

## Structure des Fichiers de Deploiement

```
a-level-saver/
  Dockerfile                    # Build Docker pour Render
  render.yaml                   # Configuration Render (mode natif, reference)
  requirements.txt              # Dependances Python
  config.py                     # Settings Pydantic (env vars)
  .env.example                  # Template des variables d'environnement
  .env                          # Variables reelles (NON commite, .gitignore)
  webhook_server.py             # Serveur Flask (point d'entree Gunicorn)
  test_webhook.py               # Tests du webhook
  test_doc_workflow_with_examt3p.py  # Test unitaire d'un ticket
  run_workflow_batch.py         # Traitement batch
  run_workflow_continuous.py    # Traitement continu (boucle)
  batch_health_check.py         # Validation post-batch
  show_response.py              # Affichage rapide d'une reponse
  src/
    zoho_client.py              # Clients API Zoho (Desk + CRM)
    utils/
      logging_config.py         # Configuration des logs
      exament3p_playwright.py   # Scraping ExamT3P via Playwright
    workflows/
      doc_ticket_workflow.py    # Orchestrateur principal
    constants/
      models.py                 # IDs des modeles IA
      urls.py                   # URLs externes
```
