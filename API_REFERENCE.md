# Référence API - Zoho Desk & CRM

> ⚠️ **OBSOLÈTE** : supplanté par docs/API_REFERENCE.md (référence à jour).

Ce document détaille tous les endpoints API Zoho utilisés par le système.

## 🎫 API Zoho Desk

Base URL: `https://desk.zoho.{datacenter}/api/v1`

### Endpoints implémentés

#### 1. GET /tickets/{ticketId}
**Fonction**: Récupérer les informations de base d'un ticket

**Paramètres**:
- `orgId` (requis): ID de votre organisation Zoho Desk

**Réponse**: Objet ticket avec les champs de base

**Utilisation**:
```python
ticket = desk_client.get_ticket("123456789")
```

---

#### 2. GET /tickets
**Fonction**: Lister les tickets avec filtres

**Paramètres**:
- `orgId` (requis): ID de votre organisation
- `status` (optionnel): Open, Pending, Resolved, Closed
- `limit` (optionnel): Nombre max de résultats (défaut: 50)
- `from` (optionnel): Index de départ pour pagination (défaut: 0)

**Réponse**: Liste de tickets

**Utilisation**:
```python
tickets = desk_client.list_tickets(status="Open", limit=10)
```

---

#### 3. GET /tickets/{ticketId}/threads ⭐ NOUVEAU
**Fonction**: Récupérer l'historique COMPLET des threads (emails)

**Paramètres**:
- `orgId` (requis): ID de votre organisation

**Réponse**: Liste complète de tous les threads avec le contenu intégral des emails

**Champs importants retournés**:
- `direction`: "in" (entrant) ou "out" (sortant)
- `from`: Expéditeur (objet avec emailId, name)
- `to`: Destinataire(s)
- `subject`: Sujet de l'email
- `content`: **Contenu HTML complet de l'email**
- `plainText`: **Contenu texte brut complet**
- `createdTime`: Horodatage
- `isReply`: Boolean - est une réponse
- `isForward`: Boolean - est un transfert
- `channel`: Canal (email, web, phone, etc.)

**Utilisation**:
```python
threads = desk_client.get_ticket_threads("123456789")
for thread in threads.get("data", []):
    print(f"De: {thread['from']['emailId']}")
    print(f"Contenu complet: {thread['plainText']}")
```

**⚠️ Important**: Cet endpoint retourne le **contenu intégral** des emails, pas des résumés !

---

#### 4. GET /tickets/{ticketId}/conversations ⭐ NOUVEAU
**Fonction**: Récupérer toutes les conversations (commentaires, notes)

**Paramètres**:
- `orgId` (requis): ID de votre organisation

**Réponse**: Liste de toutes les conversations

**Champs importants retournés**:
- `type`: Type de conversation (comment, note, etc.)
- `content`: Contenu complet du commentaire
- `author`: Auteur (objet avec name, email)
- `isPublic`: Boolean - visible par le client ou interne
- `createdTime`: Horodatage

**Utilisation**:
```python
conversations = desk_client.get_ticket_conversations("123456789")
for conv in conversations.get("data", []):
    visibility = "Public" if conv['isPublic'] else "Interne"
    print(f"[{visibility}] {conv['author']['name']}: {conv['content']}")
```

---

#### 5. GET /tickets/{ticketId}/history ⭐ NOUVEAU
**Fonction**: Récupérer l'historique des modifications

**Paramètres**:
- `orgId` (requis): ID de votre organisation

**Réponse**: Liste de toutes les modifications apportées au ticket

**Champs importants retournés**:
- `fieldName`: Nom du champ modifié
- `oldValue`: Ancienne valeur
- `newValue`: Nouvelle valeur
- `actor`: Qui a fait la modification (objet avec name, email)
- `modifiedTime`: Quand la modification a été faite

**Utilisation**:
```python
history = desk_client.get_ticket_history("123456789")
for change in history.get("data", []):
    print(f"{change['actor']['name']} a changé {change['fieldName']}")
    print(f"  {change['oldValue']} → {change['newValue']}")
```

---

#### 6. GET /tickets/{ticketId} (contexte complet) ⭐ MÉTHODE HELPER
**Fonction**: Récupérer TOUT le contexte d'un ticket en un seul appel

**Utilisation**:
```python
complete_context = desk_client.get_ticket_complete_context("123456789")

# Retourne un dictionnaire avec :
{
    "ticket": {...},           # Infos de base
    "threads": [...],          # Tous les emails (contenu complet)
    "conversations": [...],    # Tous les commentaires
    "history": [...]          # Tous les changements
}
```

**⭐ C'est cette méthode qui est utilisée par DeskTicketAgent** pour avoir le contexte complet !

---

#### 7. PATCH /tickets/{ticketId}
**Fonction**: Mettre à jour un ticket

**Paramètres**:
- `orgId` (requis): ID de votre organisation
- Body: JSON avec les champs à modifier

**Exemple de body**:
```json
{
    "status": "Resolved",
    "priority": "High",
    "customField": "valeur"
}
```

**Utilisation**:
```python
desk_client.update_ticket("123456789", {
    "status": "Resolved"
})
```

---

#### 8. POST /tickets/{ticketId}/comments
**Fonction**: Ajouter un commentaire à un ticket

**Paramètres**:
- `orgId` (requis): ID de votre organisation
- Body JSON:
  - `content`: Contenu du commentaire
  - `isPublic`: true (visible client) ou false (interne)

**Utilisation**:
```python
# Commentaire public
desk_client.add_ticket_comment(
    ticket_id="123456789",
    content="Votre réponse au client",
    is_public=True
)

# Note interne
desk_client.add_ticket_comment(
    ticket_id="123456789",
    content="Note pour l'équipe",
    is_public=False
)
```

---

## 💼 API Zoho CRM

Base URL: `https://www.zohoapis.{datacenter}/crm/v3`

### Endpoints implémentés

#### 1. GET /Deals/{dealId}
**Fonction**: Récupérer une opportunité

**Utilisation**:
```python
deal = crm_client.get_deal("987654321")
```

---

#### 2. PUT /Deals/{dealId}
**Fonction**: Mettre à jour une opportunité

**Body JSON**:
```json
{
    "data": [{
        "Stage": "Proposal",
        "Probability": 75,
        "Next_Step": "Envoyer proposition"
    }]
}
```

**Utilisation**:
```python
crm_client.update_deal("987654321", {
    "Stage": "Proposal",
    "Probability": 75
})
```

---

#### 3. GET /Deals/search
**Fonction**: Rechercher des opportunités

**Paramètres**:
- `criteria`: Critères de recherche
- `page`: Numéro de page
- `per_page`: Résultats par page (max: 200)

**Exemple de critères**:
```
(Stage:equals:Qualification)
(Contact_Name:equals:john@example.com)
(Stage:equals:Proposal)or(Stage:equals:Negotiation)
```

**Utilisation**:
```python
deals = crm_client.search_deals(
    criteria="(Stage:equals:Qualification)",
    per_page=50
)
```

---

#### 4. GET /Deals/{dealId}/Notes
**Fonction**: Récupérer les notes d'une opportunité

**Utilisation**:
```python
notes = crm_client.get_deal_notes("987654321")
```

---

#### 5. POST /Deals/{dealId}/Notes
**Fonction**: Ajouter une note à une opportunité

**Utilisation**:
```python
crm_client.add_deal_note(
    deal_id="987654321",
    note_title="Analyse IA",
    note_content="Le client est très engagé..."
)
```

---

## 🔐 Authentification

Tous les endpoints utilisent OAuth2 avec refresh token.

### Flow d'authentification

1. **Refresh token** (stocké dans `.env`)
2. **Access token** généré automatiquement via `POST /oauth/v2/token`
3. **Header**: `Authorization: Zoho-oauthtoken {access_token}`
4. **Gestion automatique** du renouvellement (cache de 55 min)

### Scopes requis

**Zoho Desk**:
- `Desk.tickets.ALL`
- `Desk.contacts.READ`

**Zoho CRM**:
- `ZohoCRM.modules.ALL`

---

## 📊 Comparaison : Avant vs Maintenant

### Avant (contexte partiel)

```python
# Récupération basique
ticket = get_ticket(ticket_id)

# L'agent IA ne voyait que :
- subject: "Question sur les A-Levels"
- description: "Première question du client"
```

### Maintenant (contexte complet)

```python
# Récupération complète
complete_context = get_ticket_complete_context(ticket_id)

# L'agent IA voit :
- Email initial du client (texte complet)
- Réponse de l'agent (texte complet)
- Email de suivi du client (texte complet)
- 2ème réponse de l'agent (texte complet)
- Commentaires internes de l'équipe
- Historique : status changé Open → Pending → Open
- Historique : priorité changée Low → High
- Tout le contexte de la conversation sur 2 semaines
```

**Résultat** : L'agent IA peut fournir des réponses vraiment contextualisées !

---

## 🔧 Retry et Gestion d'erreurs

Tous les appels API incluent :
- ✅ Retry automatique (3 tentatives)
- ✅ Backoff exponentiel (2s, 4s, 8s)
- ✅ Gestion des erreurs HTTP
- ✅ Logs détaillés

---

## 📚 Ressources

- [API Zoho Desk](https://desk.zoho.com/support/APIDocument.do)
- [API Zoho CRM](https://www.zoho.com/crm/developer/docs/api/v3/)
- [OAuth2 Zoho](https://www.zoho.com/accounts/protocol/oauth.html)
