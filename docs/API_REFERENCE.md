# Reference API & Integrations

Documentation exhaustive de toutes les integrations API du projet A-Level Saver.

---

## Vue d'Ensemble des Integrations

| Service | Base URL | Authentification | Rate Limits | Fichiers principaux |
|---------|----------|------------------|-------------|---------------------|
| **Zoho Desk** | `https://desk.zoho.{dc}/api/v1` | OAuth2 (Bearer token) | 300ms entre appels, 429 retry | `src/zoho_client.py` |
| **Zoho CRM v3** | `https://www.zohoapis.{dc}/crm/v3` | OAuth2 (Bearer token) | 300ms entre appels, 429 retry | `src/zoho_client.py` |
| **Zoho CRM v8** | `https://www.zohoapis.{dc}/crm/v8` | OAuth2 (Bearer token) | Idem v3 | `src/zoho_client.py` |
| **Zoho OAuth** | `https://accounts.zoho.{dc}/oauth/v2` | Client credentials | 2s min entre refreshes | `src/zoho_token_manager.py` |
| **Anthropic (Claude)** | `https://api.anthropic.com` | API Key (Bearer) | Geree par le SDK | `src/agents/*.py`, `src/utils/*.py` |
| **ExamT3P** | `https://www.exament3p.fr` | Login/Password (HTTP POST) | 10s timeout | `src/utils/exament3p_playwright.py` (classe `ExamT3PHttpClient`) |

> **Note** : `{dc}` = datacenter Zoho, configure via `settings.zoho_datacenter` (defaut: `com`).

---

## 1. Webhook Endpoints (Notre API)

Serveur Flask defini dans `webhook_server.py`, deploye sur Render.

### GET /health

Endpoint de verification de sante.

**Reponse** :
```json
{
    "status": "healthy",
    "service": "a-level-saver-webhook",
    "timestamp": "2026-02-16T10:30:00",
    "active_threads": 3
}
```

**Exemple curl** :
```bash
curl https://a-level-saver.onrender.com/health
```

### POST /webhook/zoho-desk

Endpoint principal appele par les Deluge Workflow Rules de Zoho Desk. Traitement asynchrone en background thread.

**Authentification** : Header `X-Webhook-Secret` (secret partage configure via `ZOHO_WEBHOOK_SECRET`).

**Requete** :
```json
{
    "ticket_id": "198709000449479828"
}
```

> Accepte aussi `"ticketId"` comme cle alternative.

**Reponse (200 immediate)** :
```json
{
    "success": true,
    "ticket_id": "198709000449479828",
    "message": "Processing in background"
}
```

**Erreurs** :
- `401` : Header `X-Webhook-Secret` absent ou invalide
- `400` : JSON invalide ou `ticket_id` manquant

**Exemple curl** :
```bash
curl -X POST https://a-level-saver.onrender.com/webhook/zoho-desk \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: votre_secret" \
  -d '{"ticket_id": "198709000449479828"}'
```

**Traitement interne** : Lance `DOCTicketWorkflow.process_ticket()` dans un `threading.Thread` daemon avec les options :
```python
workflow.process_ticket(
    ticket_id=ticket_id,
    auto_create_draft=True,
    auto_update_crm=True,
    auto_update_ticket=True,
    auto_send=True   # Garde-fous via _can_auto_send()
)
```

### POST /webhook/test

Endpoint de test synchrone, sans authentification. Retourne le resultat complet du workflow.

**Requete** :
```json
{
    "ticket_id": "198709000438366101",
    "auto_create_draft": true,
    "auto_update_crm": true,
    "auto_update_ticket": true,
    "auto_send": true
}
```

**Reponse (200)** :
```json
{
    "success": true,
    "ticket_id": "198709000438366101",
    "result": {
        "workflow_stage": "COMPLETE",
        "delivery_method": "draft",
        "draft_created": true,
        "reply_sent": false,
        "crm_updated": true,
        "ticket_updated": true,
        "skip_reason": null,
        "errors": []
    }
}
```

### GET /webhook/stats

Statistiques et configuration du webhook.

**Reponse** :
```json
{
    "service": "a-level-saver-webhook",
    "status": "running",
    "configuration": {
        "auth": "X-Webhook-Secret header",
        "auth_enabled": true,
        "processing": "async (background thread)",
        "auto_send": "guarded by _can_auto_send()"
    },
    "active_threads": 2,
    "timestamp": "2026-02-16T10:30:00"
}
```

### GET /logs

Logs applicatifs recents (buffer memoire, 2000 lignes max). **Protege par le header `X-Webhook-Secret`** (`webhook_server.py:218`).

**Query params** :
- `?lines=200` — nombre de lignes (defaut 200, max 2000)
- `?level=ERROR` — filtre par niveau (INFO, WARNING, ERROR)
- `?q=keyword` — filtre par mot-cle (insensible a la casse)
- `?format=text` — texte brut au lieu de JSON

```bash
curl -H "X-Webhook-Secret: $SECRET" "https://a-level-saver.onrender.com/logs?lines=100&level=ERROR"
```

### GET /logs/ticket/{ticket_id}

Logs filtres pour un ticket specifique. **Protege par le header `X-Webhook-Secret`** (`webhook_server.py:257`). Supporte `?format=text`.

```bash
curl -H "X-Webhook-Secret: $SECRET" "https://a-level-saver.onrender.com/logs/ticket/198709000449479828"
```

---

## 2. Zoho Desk API

**Client** : `ZohoDeskClient` dans `src/zoho_client.py`
**Base URL** : `https://desk.zoho.{dc}/api/v1`
**Header commun** : `Authorization: Zoho-oauthtoken {access_token}`, `Content-Type: application/json`
**Param commun** : `orgId={zoho_desk_org_id}` sur chaque appel

### 2.1 Tickets

#### `get_ticket(ticket_id)` — Recuperer un ticket

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets/{ticket_id}` |
| Params | `orgId` |
| Retour | Dict complet du ticket |

```python
desk_client = ZohoDeskClient()
ticket = desk_client.get_ticket("198709000449479828")
# {'id': '198709000449479828', 'subject': '...', 'status': 'Open', 'departmentId': '...', ...}
```

#### `list_tickets(status, limit, from_index)` — Lister les tickets (page unique)

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets` |
| Params | `orgId`, `status` (optionnel), `limit` (defaut: 50), `from` |
| Retour | Dict avec `data: [...]` |

#### `list_all_tickets(status, limit_per_page)` — Lister TOUS les tickets (pagination auto)

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets` (paginee) |
| Params | `orgId`, `status`, `from`, `limit` (max 100) |
| Retour | `List[Dict]` — tous les tickets concatenes |

Utilise `_get_all_pages()` pour la pagination automatique. Incremente `from` de `limit_per_page` a chaque page, s'arrete quand `len(items) < limit_per_page`.

#### `update_ticket(ticket_id, data)` — Mettre a jour un ticket

| Propriete | Valeur |
|-----------|--------|
| Methode API | `PATCH /api/v1/tickets/{ticket_id}` |
| Params | `orgId` |
| Body | Dict avec les champs a modifier |

```python
desk_client.update_ticket("198709000449479828", {"status": "Closed"})
```

#### `move_ticket_to_department(ticket_id, department_name)` — Deplacer un ticket

| Propriete | Valeur |
|-----------|--------|
| Methode API | `POST /api/v1/tickets/{ticket_id}/move` |
| Params | `orgId` |
| Body | `{"departmentId": "..."}` |

Utilise l'endpoint dedie `/move` (POST) et non PATCH. Resout le departement par nom via `get_department_info()`.

```python
desk_client.move_ticket_to_department("198709000449479828", "Contact")
```

### 2.2 Departements

#### `list_departments()` — Lister tous les departements

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/departments` |
| Retour | `List[Dict]` avec `id`, `name`, `layoutId`, etc. |

#### `get_department_id_by_name(name)` — Trouver l'ID par nom

Appelle `list_departments()` et filtre par nom (case-insensitive).

#### `get_department_info(name)` — Info complete d'un departement

Meme principe, retourne le Dict complet du departement.

### 2.3 Threads (Emails)

#### `get_ticket_threads(ticket_id)` — Liste des threads (resume)

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets/{ticket_id}/threads` |
| Retour | Dict avec `data: [...]` |

> **Attention** : Retourne des resumes, pas le contenu complet. Utiliser `get_all_threads_with_full_content()` pour le contenu complet.

#### `get_thread_details(ticket_id, thread_id)` — Contenu complet d'un thread

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets/{ticket_id}/threads/{thread_id}` |
| Retour | Dict avec le contenu complet (champ `content`) |

#### `get_all_threads_with_full_content(ticket_id)` — Tous les threads avec contenu complet

Methode recommandee. Appelle `get_ticket_threads()` puis `get_thread_details()` pour chaque thread individuellement. Fallback sur le resume en cas d'erreur.

```python
threads = desk_client.get_all_threads_with_full_content("198709000449479828")
for thread in threads:
    direction = thread.get('direction')  # 'in' ou 'out'
    content = thread.get('content')      # Contenu HTML complet
    plain = thread.get('plainText')      # Texte brut (si dispo)
```

### 2.4 Conversations et Historique

#### `get_ticket_conversations(ticket_id)` — Toutes les conversations

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets/{ticket_id}/conversations` |

#### `get_ticket_history(ticket_id)` — Historique complet

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets/{ticket_id}/history` |

#### `get_ticket_complete_context(ticket_id)` — Contexte complet pour analyse IA

Methode composite qui appelle :
1. `get_ticket()` — infos de base
2. `get_all_threads_with_full_content()` — emails complets
3. `get_ticket_conversations()` — conversations
4. `get_ticket_history()` — historique

Retourne :
```python
{
    "ticket": {...},
    "threads": [...],       # Contenu complet par thread
    "conversations": [...],
    "history": [...]
}
```

### 2.5 Commentaires

#### `get_ticket_comments(ticket_id, include_public, include_private)` — Commentaires

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /api/v1/tickets/{ticket_id}/comments` |
| Filtres | `include_public`, `include_private` (filtrage cote client) |

#### `add_ticket_comment(ticket_id, content, is_public)` — Ajouter un commentaire

| Propriete | Valeur |
|-----------|--------|
| Methode API | `POST /api/v1/tickets/{ticket_id}/comments` |
| Body | `{"content": "...", "isPublic": true}` |

### 2.6 Brouillons et Reponses

#### `create_ticket_reply_draft(ticket_id, content, content_type, from_email, to_email)` — Creer un brouillon

| Propriete | Valeur |
|-----------|--------|
| Methode API | `POST /api/v1/tickets/{ticket_id}/draftReply` |
| Body | `{"channel": "EMAIL", "contentType": "plainText", "content": "...", "isForward": false}` |
| Doc officielle | https://desk.zoho.com/DeskAPIDocument#Threads#Threads_CreateDraft |

```python
desk_client.create_ticket_reply_draft(
    ticket_id="198709000449479828",
    content="<b>Bonjour...</b>",
    content_type="html",
    from_email="doc@cab-formations.fr"
)
```

#### `send_ticket_reply(ticket_id, content, content_type, from_email, to_email)` — Envoyer une reponse

| Propriete | Valeur |
|-----------|--------|
| Methode API | `POST /api/v1/tickets/{ticket_id}/sendReply` |
| Body | `{"channel": "EMAIL", "contentType": "html", "content": "...", "isForward": false}` |

Utilise par le workflow en mode `auto_send=True` quand les garde-fous `_can_auto_send()` sont satisfaits. Fallback vers `create_ticket_reply_draft()` si les gardes echouent.

#### `has_existing_draft(ticket_id)` — Verifier si un brouillon existe

Appelle `get_ticket_threads()` et cherche un thread avec `status == "DRAFT"`.

---

## 3. Zoho CRM API

**Client** : `ZohoCRMClient` dans `src/zoho_client.py`
**Base URL v3** : `https://www.zohoapis.{dc}/crm/v3`
**Base URL v8** : `https://www.zohoapis.{dc}/crm/v8` (Timeline uniquement)
**Credentials** : Peut utiliser des OAuth credentials separees (`zoho_crm_client_id`, etc.) ou fallback sur celles de Desk.

### 3.1 Deals (Potentiels)

#### `get_deal(deal_id)` — Recuperer un deal

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v3/Deals/{deal_id}` |
| Retour | Dict du deal (premier element de `data`) |

**Champs personnalises importants du module Deals** :
| Champ | Type | Description |
|-------|------|-------------|
| `Evalbox` | Texte | Statut dossier ExamT3P (Dossier cree, Pret a payer, Dossier Synchronise, VALIDE CMA, etc.) |
| `Date_examen_VTC` | Lookup | Vers `Dates_Examens_VTC_TAXI` (retourne `{name, id}`) |
| `Session` | Lookup | Vers `Sessions1` (retourne `{name, id}`) |
| `IDENTIFIANT_EVALBOX` | Texte | Email ExamT3P du candidat |
| `MDP_EVALBOX` | Texte | Mot de passe ExamT3P |
| `NUM_DOSSIER_EVALBOX` | Texte | Numero de dossier CMA |
| `CMA_de_depot` | Texte | Departement CMA (ex: "75") |
| `Resultat` | Texte | ADMIS, NON ADMIS, ADMISSIBLE, ABSENT, etc. |
| `Stage` | Texte | Etape du pipeline (EN ATTENTE, GAGNE, etc.) |
| `Date_Dossier_recu` | Date | Date de reception du dossier initial |
| `Preference_horaire` | Texte | Preference de session du candidat (jour/soir) |
| `Session_souhait_e` | Texte | Session souhaitee |
| `Date_test_selection` | Date | **READ-ONLY** — ne jamais modifier via workflow |

#### `update_deal(deal_id, data)` — Mettre a jour un deal

| Propriete | Valeur |
|-----------|--------|
| Methode API | `PUT /crm/v3/Deals/{deal_id}` |
| Body | `{"data": [{...champs...}]}` |

```python
crm_client.update_deal("5678901234567890", {"Evalbox": "VALIDE CMA"})
```

> **Attention** : Les champs lookup (Date_examen_VTC, Session) attendent un **ID** et non une valeur texte. Utiliser `CRMUpdateAgent` pour le mapping automatique.

#### `search_deals(criteria, page, per_page)` — Rechercher des deals (page unique)

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v3/Deals/search` |
| Params | `criteria`, `page`, `per_page` (max 200) |

**Syntaxe des criteres** :
```python
# Par email
criteria = "(Email:equals:candidat@gmail.com)"

# Par contact
criteria = "(Contact_Name:equals:{contact_id})"

# Combine
criteria = "((Evalbox:equals:VALIDE CMA)and(Departement:equals:75))"
```

#### `search_deals_by_email(email)` — Rechercher par email

Raccourci pour `search_deals("(Email:equals:{email})")`.

#### `search_all_deals(criteria, per_page)` — Rechercher TOUS les deals (pagination auto)

Itere sur toutes les pages via `info.more_records`. Retourne `List[Dict]`.

#### `get_deals_by_contact(contact_id, per_page)` — Deals d'un contact

Utilise `search_deals("(Contact_Name:equals:{contact_id})")`.

### 3.2 Contacts

#### `get_contact(contact_id)` — Recuperer un contact

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v3/Contacts/{contact_id}` |

#### `update_contact(contact_id, data)` — Mettre a jour un contact

| Propriete | Valeur |
|-----------|--------|
| Methode API | `PUT /crm/v3/Contacts/{contact_id}` |
| Body | `{"data": [{...}]}` |

#### `search_contacts(criteria, page, per_page)` — Rechercher des contacts

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v3/Contacts/search` |

### 3.3 Notes (sur Deals)

#### `get_deal_notes(deal_id)` — Notes d'un deal

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v3/Deals/{deal_id}/Notes` |
| Params | `fields=Note_Title,Note_Content,Created_Time` |

> **Important** : Le parametre `fields` est obligatoire pour recuperer le contenu des notes.

#### `add_deal_note(deal_id, note_title, note_content)` — Ajouter une note

| Propriete | Valeur |
|-----------|--------|
| Methode API | `POST /crm/v3/Deals/{deal_id}/Notes` |
| Body | `{"data": [{"Note_Title": "...", "Note_Content": "...", "Parent_Id": {"id": deal_id}}]}` |

Utilise pour les notes `[META]` de ThreadMemory et les traces de workflow.

### 3.4 Records generiques

#### `get_record(module, record_id)` — Recuperer un record de n'importe quel module

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v3/{module}/{record_id}` |

Utilise pour les lookups vers les modules custom :
```python
# Recuperer les details d'une session d'examen
exam_session = crm_client.get_record('Dates_Examens_VTC_TAXI', '1234567890')

# Recuperer les details d'une session de formation
training_session = crm_client.get_record('Sessions1', '9876543210')
```

### 3.5 Timeline API (v8)

#### `get_deal_timeline(deal_id)` — Timeline des modifications

| Propriete | Valeur |
|-----------|--------|
| Methode API | `GET /crm/v8/Deals/{deal_id}/__timeline` |
| Note | Utilise v8 (remplace `/v3` par `/v8` dans la base URL) |

Retourne l'historique des modifications de champs et des actions sur le deal. Utilise par ThreadMemory V2 pour detecter les interventions humaines et la progression CRM.

**Champs suivis** (`TRACKED_FIELDS` dans `thread_memory.py`) :
- Evalbox, Session, Session_souhait_e, Date_examen_VTC, Preference_horaire, etc.

### 3.6 Modules Custom CRM

Le projet utilise deux modules custom accessibles via `get_record()` ou des appels directs :

#### Module `Dates_Examens_VTC_TAXI` — Dates d'examen

| Champ | Description |
|-------|-------------|
| `Date_Examen` | Date de l'examen (format YYYY-MM-DD) |
| `Departement` | Numero de departement (ex: "75") |
| `Date_Cloture_Inscription` | Date limite d'inscription |
| `Statut` | "Actif" ou null |
| `Name` | Nom affiche (ex: "75_2026-03-31") |

**Recherche** (dans `date_examen_vtc_helper.py`) :
```python
url = f"{settings.zoho_crm_api_url}/Dates_Examens_VTC_TAXI/search"
criteria = "(((Statut:equals:Actif)or(Statut:equals:null))and(Departement:equals:75))"
response = crm_client._make_request("GET", url, params={"criteria": criteria, "per_page": 200})
```

#### Module `Sessions1` — Sessions de formation

| Champ | Description |
|-------|-------------|
| `Name` | Nom complet de la session |
| `session_type` | "jour" ou "soir" |
| `Date_d_but` | Date de debut (**attention** : pas `Date_debut`) |
| `Date_fin` | Date de fin (**attention** : pas `Date_de_fin`) |

**Recherche** (dans `session_helper.py`) :
```python
url = f"{settings.zoho_crm_api_url}/Sessions1/search"
criteria = f"((Date_examen_VTC:equals:{exam_date_id})and(session_type:equals:{type}))"
response = crm_client._make_request("GET", url, params={"criteria": criteria})
```

---

## 4. Anthropic API (Claude)

**SDK** : `anthropic` (Python)
**Authentification** : API Key via `settings.anthropic_api_key` ou variable d'environnement `ANTHROPIC_API_KEY`
**Fichier constantes** : `src/constants/models.py`

### 4.1 Modeles utilises

| Constante | Model ID | Usage |
|-----------|----------|-------|
| `MODEL_TRIAGE` | `claude-sonnet-4-20250514` | Triage et detection d'intentions |
| `MODEL_HUMANIZER` | `claude-sonnet-4-20250514` | Reformulation des reponses |
| `MODEL_EXTRACTION` | `claude-3-5-haiku-20241022` | Extraction d'identifiants depuis emails |
| `MODEL_CONVERSATION` | `claude-sonnet-4-5-20250929` | Analyse conversationnelle V3 |
| `MODEL_PERSONALIZATION` | `claude-sonnet-4-5-20250929` | Personnalisation avancee |

### 4.2 Appels par composant

#### TriageAgent (`src/agents/triage_agent.py`)

```python
from anthropic import Anthropic

client = Anthropic()
response = client.messages.create(
    model=MODEL_TRIAGE,       # claude-sonnet-4-20250514
    max_tokens=1000,
    system=self.SYSTEM_PROMPT,  # ~3000 tokens de prompt systeme
    messages=[{"role": "user", "content": f"Analyse ce ticket...\n\n{context}"}]
)
```

**System prompt** : Expert de triage CAB Formations. Detecte action (GO/ROUTE/SPAM), departement cible, intention primaire et secondaires, contexte d'intention.
**Retour attendu** : JSON avec `action`, `target_department`, `primary_intent`, `secondary_intents`, `intent_context`, `confidence`.
**Cout estime** : ~$0.001/appel

#### Response Humanizer (`src/utils/response_humanizer.py`)

```python
client = anthropic.Anthropic()
response = client.messages.create(
    model=MODEL_HUMANIZER,    # claude-sonnet-4-20250514
    max_tokens=2000,
    system=HUMANIZE_SYSTEM_PROMPT,  # ~2000 tokens
    messages=[{"role": "user", "content": base_prompt}]
)
```

**System prompt** : Reformulateur d'emails. Fusionne sections, ajoute transitions naturelles, preserve dates/URLs/montants. Interdit d'inventer du contenu.
**Retry** : Max 2 tentatives. Si validation echoue (dates manquantes/inventees), la 2e tentative inclut un rappel explicite des dates.
**Validation** : `_validate_humanized_response()` verifie la preservation des dates DD/MM/YYYY, des CMA, et des horaires.
**Fallback** : Si validation echoue apres 2 tentatives, retourne le template brut.
**Cout estime** : ~$0.036/appel

#### Conversation Analyzer V3 (`src/utils/conversation_analyzer.py`)

```python
from config import settings
client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
response = client.messages.create(
    model=MODEL_CONVERSATION,  # claude-sonnet-4-5-20250929
    max_tokens=600,
    system=ANALYZER_SYSTEM_PROMPT,
    messages=[{"role": "user", "content": user_prompt}],
    timeout=15.0,
)
```

**Short-circuit** : Si <= 1 message entrant, pas d'appel LLM (retourne `ConversationState` par defaut).
**Timeout** : 15 secondes (`anthropic.APITimeoutError` catchee).
**Anti-hallucination** : Les dates extraites sont validees contre le contenu reel des threads.
**Cout estime** : ~$0.01-0.02/appel (uniquement pour tickets multi-thread).

#### Extraction de credentials (`src/utils/examt3p_credentials_helper.py`)

```python
from anthropic import Anthropic
client = Anthropic()
response = client.messages.create(
    model=MODEL_EXTRACTION,   # claude-3-5-haiku-20241022
    max_tokens=200,
    messages=[{"role": "user", "content": prompt}]
)
```

**Usage** : Extrait identifiant + mot de passe ExamT3P depuis les messages du candidat.
**Retour attendu** : JSON avec `identifiant`, `mot_de_passe`, `confidence`.
**Cout estime** : ~$0.001/appel

#### BaseAgent (`src/agents/base_agent.py`)

Classe abstraite utilisee par les agents heritiers. Utilise le modele defini dans `settings.agent_model` (legacy, defaut `claude-sonnet-4-5-20250929`) :

```python
self.client = Anthropic(api_key=settings.anthropic_api_key)
response = self.client.messages.create(
    model=settings.agent_model,
    max_tokens=settings.agent_max_tokens,  # 4096
    temperature=settings.agent_temperature,  # 0.7
    system=self.system_prompt,
    messages=messages
)
```

#### Autres appels Anthropic

| Fichier | Usage | Modele |
|---------|-------|--------|
| `doc_ticket_workflow.py` (ligne ~2399) | Resume de conversation pour notes CRM | `MODEL_EXTRACTION` |
| `doc_ticket_workflow.py` (ligne ~6615) | Analyse direct_answer | `settings.agent_model` |
| `doc_ticket_workflow.py` (ligne ~6987) | Personnalisation avancee | `MODEL_PERSONALIZATION` |
| `crm_updater.py` (ligne ~466) | Extraction de mise a jour CRM | Via `anthropic.Anthropic()` |
| `deal_linking_agent.py` (ligne ~974) | Correspondance deals | Via `client.messages.create()` |

### 4.3 Cout total estime par ticket

| Composant | Modele | Cout approx. |
|-----------|--------|--------------|
| Extraction identifiants | Haiku 4.5 (`MODEL_EXTRACTION`) | ~$0.001 |
| Agent Trieur | Sonnet 4.6 (`MODEL_TRIAGE`) | ~$0.01 |
| Conversation Analyzer V3 | Sonnet 4.5 (`MODEL_CONVERSATION`) | ~$0.01-0.02 |
| Response Humanizer | Sonnet 4.6 (`MODEL_HUMANIZER`) | ~$0.036 |
| Notes CRM next steps | Sonnet 4.6 (`MODEL_TRIAGE`) | ~$0.01 |
| **Total** | | **~$0.06-0.08** |

---

## 5. ExamT3P (HTTP/httpx)

**Fichiers** : `src/utils/exament3p_playwright.py` (classe `ExamT3PHttpClient`, ligne 58), `src/utils/examt3p_credentials_helper.py`, `src/agents/examt3p_agent.py`

### 5.1 Architecture

ExamT3P est un portail web (`https://www.exament3p.fr`) sans API publique. L'extraction se fait via **httpx** (client HTTP) + **BeautifulSoup** (parsing HTML).

```
ExamT3PAgent (orchestrateur)
    └── exament3p_playwright.py (extracteur HTTP)
            └── ExamT3PHttpClient (classe principale, ligne 58)
                    ├── _login()               → POST /Cma/UserAccount/login
                    ├── _extract_dashboard()   → GET /mon-espace (HTML parsing)
                    ├── _extract_messages()     → GET /Cmacandidate/getMessages (JSON)
                    └── Supporte ?dossier={id}  → Multi-dossier
```

### 5.2 Configuration HTTP

```python
client = httpx.Client(
    base_url="https://www.exament3p.fr",
    timeout=10.0,
    follow_redirects=True
)
```

**Timeout** : 10s pour toutes les requetes HTTP.

### 5.3 Flux d'authentification

1. `POST /Cma/UserAccount/login` avec `email` + `password` (form data)
2. Le serveur retourne un cookie de session
3. Verification de la connexion via `GET /mon-espace` (status 200 + contenu attendu)

### 5.4 Pages visitees et donnees extraites

| Page | Methode | Donnees |
|------|---------|---------|
| Vue d'ensemble | `_extract_overview()` | `statut_dossier`, `progression`, `actions_requises`, historique |
| Mes Examens | `_extract_examens()` | `date_examen`, `convocation`, lieu, statut |
| Mes Documents | `_extract_documents()` | Statut de chaque piece, documents refuses, motifs de refus |
| Mon Compte | `_extract_compte()` | Infos personnelles, departement |
| Mes Paiements | `_extract_paiements()` | `paiement_cma`, historique, statut (VALIDE, EN ATTENTE) |
| Messages | `_extract_messages()` | Echanges avec la CMA |

### 5.5 Gestion des erreurs

**Retry global** : 3 tentatives pour l'ensemble de l'extraction (`MAX_RETRIES = 3`), avec `RETRY_DELAY * 2` secondes entre tentatives.

**Retry par operation** : Chaque page est extraite avec `retry_async()` (3 tentatives, 2s entre chaque).

**Fallback** : Si une page echoue apres retries, l'erreur est enregistree dans `data['errors']` et l'extraction continue sur les autres pages.

### 5.6 Test de connexion (`test_examt3p_connection()`)

Fonction separee dans `examt3p_credentials_helper.py` qui teste uniquement la connexion (sans extraction) pour valider des identifiants.

```python
success, error = test_examt3p_connection("candidat@gmail.com", "motdepasse123")
# (True, None) ou (False, "Identifiants invalides")
```

### 5.7 Synchronisation ExamT3P vers CRM

Fichier `src/utils/examt3p_crm_sync.py`. Mapping des statuts :

| ExamT3P (`statut_dossier`) | CRM (`Evalbox`) |
|---------------------------|-----------------|
| En cours de composition | Dossier cree |
| En attente de paiement | Pret a payer |
| En cours d'instruction | Dossier Synchronise |
| Incomplet | Refuse CMA |
| Valide | VALIDE CMA |
| En attente de convocation | Convoc CMA recue |

---

## 5bis. API interne PlanBot (B2B Relations entreprises)

**Fichier** : `src/utils/planbot_api_client.py` (classe `PlanBotAPIClient`)

API interne en lecture seule (exposee par le service Edusign) utilisee par le workflow Relations entreprises pour les disponibilites de sessions.

| Propriete | Valeur |
|-----------|--------|
| Endpoint | `POST {PLANBOT_API_URL}/internal/planbot/availability` |
| Auth | Header `X-PlanBot-Secret` |
| Body | `{"action": "full", "payload": {...}}` |
| Timeout | 90s |

**Configuration** (via `config.py`) : `PLANBOT_API_URL` (`planbot_api_url`), `PLANBOT_API_SECRET` (`planbot_api_secret`).

**Mode degrade** : si non configuree, retourne `{"status": "skipped", "error": "planbot_api_not_configured"}` ; en cas d'erreur HTTP ou reseau, retourne un dict `{"status": "error", ...}` sans lever d'exception (le workflow cree quand meme un brouillon).

---

## 6. Zoho OAuth — Authentification & Tokens

**Fichier** : `src/zoho_token_manager.py`

### 6.1 Architecture

```
TokenManager (Singleton thread-safe)
    ├── _tokens: Dict[cache_key, {access_token, expires_at}]
    ├── .token_cache.json  (persistance disque)
    └── get_token(client_id, client_secret, refresh_token, accounts_url) -> str
```

### 6.2 Flux OAuth2

**Endpoint** : `POST {accounts_url}/oauth/v2/token`

```python
url = f"{accounts_url}/oauth/v2/token"
params = {
    "refresh_token": refresh_token,
    "client_id": client_id,
    "client_secret": client_secret,
    "grant_type": "refresh_token"
}
response = requests.post(url, params=params, timeout=30)
```

**Reponse** :
```json
{
    "access_token": "1000.xxxx.yyyy",
    "expires_in": 3600
}
```

### 6.3 Cache et persistance

- **Cle de cache** : SHA256(`client_id:refresh_token`)[:16]
- **Expiration** : Token rafraichi 5 minutes avant expiration (`EXPIRATION_BUFFER_SECONDS = 300`)
- **Persistance** : Sauvegarde dans `.token_cache.json` a la racine du projet
- **Chargement** : Au demarrage du singleton, restauration depuis le fichier

### 6.4 Rate limiting OAuth

| Parametre | Valeur | Description |
|-----------|--------|-------------|
| `MIN_REFRESH_INTERVAL` | 2.0s | Minimum entre 2 refreshes du meme credential set |
| `MAX_REFRESH_ATTEMPTS` | 3 | Tentatives max par refresh |
| `BACKOFF_MULTIPLIER` | 2 | Backoff exponentiel (2s, 4s, 8s) |
| `RATE_LIMIT_WAIT_SECONDS` | 60s | Attente sur 429 |

### 6.5 Credentials separees Desk vs CRM

```python
# config.py
zoho_client_id: str          # Desk
zoho_client_secret: str      # Desk
zoho_refresh_token: str      # Desk

zoho_crm_client_id: Optional[str]      # CRM (fallback sur Desk)
zoho_crm_client_secret: Optional[str]  # CRM (fallback sur Desk)
zoho_crm_refresh_token: Optional[str]  # CRM (fallback sur Desk)
```

Le `ZohoCRMClient` surcharge `_get_credentials()` pour utiliser les credentials CRM specifiques si definies, sinon fallback sur celles de Desk.

---

## 7. Gestion des Erreurs

### 7.1 Pattern de retry sur les appels Zoho

Implemente dans `ZohoAPIClient._make_request()` :

```python
MAX_RETRIES = 3  # Applique a chaque requete

# 401 Unauthorized → Invalidation token + retry
if response.status_code == 401:
    self._token_manager.invalidate(client_id, refresh_token)
    return self._make_request(..., _retry_count=_retry_count + 1)

# 429 Too Many Requests → Attente Retry-After + retry
if response.status_code == 429:
    retry_after = int(response.headers.get("Retry-After", 60))
    time.sleep(retry_after)
    return self._make_request(..., _retry_count=_retry_count + 1)

# Timeout → Backoff exponentiel (2^n secondes)
except requests.exceptions.Timeout:
    wait_time = 2 ** _retry_count
    time.sleep(wait_time)
    return self._make_request(..., _retry_count=_retry_count + 1)

# Autres erreurs reseau → Backoff exponentiel
except requests.exceptions.RequestException:
    wait_time = 2 ** _retry_count
    time.sleep(wait_time)
    return self._make_request(..., _retry_count=_retry_count + 1)
```

### 7.2 Rate limiting preventif

```python
# ZohoAPIClient (classe de base)
MIN_API_INTERVAL = 0.3  # 300ms minimum entre appels API

def _apply_api_rate_limit(self):
    with self._api_lock:  # Lock au niveau de la classe (partage entre instances)
        elapsed = time.time() - self._last_api_call_time
        if elapsed < self.MIN_API_INTERVAL:
            time.sleep(self.MIN_API_INTERVAL - elapsed)
        ZohoAPIClient._last_api_call_time = time.time()
```

Ce mecanisme est **partage entre toutes les instances** (class-level lock + class-level timestamp), ce qui empeche les appels concurrents de depasser les limites.

### 7.3 Gestion des erreurs Anthropic

| Composant | Gestion |
|-----------|---------|
| TriageAgent | `try/except` complet, fallback sur `GO` avec `method: 'error_fallback'` |
| Humanizer | 2 tentatives + validation, fallback sur template brut |
| ConversationAnalyzer | Timeout 15s, fallback sur `ConversationState` vide |
| Credentials extraction | `try/except`, retourne `None` si echec |

### 7.4 Timeout configuration

| Composant | Timeout |
|-----------|---------|
| Appels Zoho API | 30s (`_make_request`) |
| Appels OAuth | 30s (`requests.post`) |
| ExamT3P HTTP requests | 10s (`httpx.Client timeout`) |
| Conversation Analyzer LLM | 15s (`timeout=15.0`) |

---

## 8. Limites & Quotas

### 8.1 Zoho API

| Limite | Valeur | Source |
|--------|--------|--------|
| Appels par jour (Desk) | 30,000 (plan gratuit) a 200,000+ | Plan Zoho Desk |
| Appels par minute (Desk) | 50-100 selon le plan | Documentation Zoho |
| Appels par jour (CRM) | 5,000+ selon le plan | Plan Zoho CRM |
| Items par page (Desk) | Max 100 | API Desk |
| Items par page (CRM) | Max 200 | API CRM |
| Rate limit local | 300ms entre appels | `MIN_API_INTERVAL` |

### 8.2 Anthropic API

| Limite | Valeur |
|--------|--------|
| Tokens max par requete | 200-4096 selon le composant |
| Rate limit (tier 2) | 4,000 requetes/min |
| Budget par ticket | ~$0.05-0.06 |
| Timeout configure | 15s (ConversationAnalyzer) |

### 8.3 ExamT3P

| Limite | Valeur |
|--------|--------|
| Delai entre actions | 1s (`ACTION_DELAY`) |
| Timeout navigation | 30s |
| Max retries global | 3 |
| Delai entre retries | 4s (2 * `RETRY_DELAY`) |

---

## 9. URLs Externes

Definies dans `src/constants/urls.py` :

| Constante | URL | Usage |
|-----------|-----|-------|
| `EXAMT3P_URL` | `https://www.exament3p.fr` | Base ExamT3P |
| `EXAMT3P_LOGIN_URL` | `https://www.exament3p.fr/id/14` | Page de connexion |
| `CAB_ELEARNING_URL` | `https://elearning.cab-formations.fr` | Plateforme e-learning |
| `CAB_UBER_INSCRIPTION_URL` | `https://cab-formations.fr/uberxcab_welcome` | Page d'inscription Uber 20EUR |
| `CAB_USER_URL` | `https://cab-formations.fr/user` | Espace utilisateur e-learning |
| `CAB_PHONE` | `01 74 90 20 82` | Telephone CAB Formations |

---

## 10. Configuration (config.py)

Gestion via `pydantic_settings.BaseSettings`, chargement depuis `.env` :

```python
class Settings(BaseSettings):
    # Zoho Desk
    zoho_client_id: str
    zoho_client_secret: str
    zoho_refresh_token: str
    zoho_datacenter: str = "com"           # "com", "eu", etc.
    zoho_desk_org_id: str
    zoho_desk_email_doc: Optional[str]     # Email de reponse DOC
    zoho_desk_email_contact: Optional[str]
    zoho_desk_email_compta: Optional[str]
    zoho_desk_email_default: Optional[str]

    # Zoho CRM (optionnel, fallback sur Desk)
    zoho_crm_client_id: Optional[str]
    zoho_crm_client_secret: Optional[str]
    zoho_crm_refresh_token: Optional[str]

    # Anthropic
    anthropic_api_key: str

    # Agent config (legacy)
    agent_model: str = "claude-sonnet-4-5-20250929"
    agent_max_tokens: int = 4096
    agent_temperature: float = 0.7

    # Staff
    escalation_agent_id: str = "198709000096599317"
    escalation_agent_name: str = "Lamia Serbouty"
    rgpd_referent_email: str = "jc@cab-formations.fr"

    @property
    def zoho_accounts_url(self) -> str:
        return f"https://accounts.zoho.{self.zoho_datacenter}"

    @property
    def zoho_desk_api_url(self) -> str:
        return f"https://desk.zoho.{self.zoho_datacenter}/api/v1"

    @property
    def zoho_crm_api_url(self) -> str:
        return f"https://www.zohoapis.{self.zoho_datacenter}/crm/v3"
```

**Variables d'environnement webhook** (non-Settings) :
- `ZOHO_WEBHOOK_SECRET` — Secret partage pour l'authentification webhook
- `WEBHOOK_HOST` — Host (defaut: `0.0.0.0`)
- `WEBHOOK_PORT` — Port (defaut: `5000`)
- `FLASK_DEBUG` — Mode debug Flask
