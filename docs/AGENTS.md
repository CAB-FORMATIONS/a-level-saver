# Agents Reference

## Vue d'ensemble

Les agents sont des composants IA spécialisés utilisant Claude (Anthropic).

| Agent | Modèle | Rôle |
|-------|--------|------|
| TriageAgent | claude-sonnet-4-6 (`MODEL_TRIAGE`) | Triage tickets (GO/ROUTE/SPAM) + détection intention |
| CRMUpdateAgent | - | Mises à jour CRM avec validation |
| DealLinkingAgent | - | Liaison ticket↔deal |
| ExamT3PAgent | - | Extraction données ExamT3P |
| RelationsTriageAgent | claude-haiku-4-5 (`MODEL_EXTRACTION`) | Triage B2B Relations entreprises (15 intentions) |
| RelationsResponseAgent | claude-sonnet-4-6 (`MODEL_HUMANIZER`) | Rédaction sécurisée des brouillons B2B |
| TicketDispatcherAgent | - | ⚠️ Exemple uniquement (non branché en prod) |

---

## 1. TriageAgent

**Fichier :** `src/agents/triage_agent.py`
**Premier dans le workflow** - Agent IA pour triage intelligent des tickets.

### Signature
```python
from src.agents.triage_agent import TriageAgent

agent = TriageAgent()

# SIGNATURE CORRECTE (NE PAS passer ticket_id seul!)
result = agent.triage_ticket(
    ticket_subject="Re: Test de sélection réussi",
    thread_content="Je souhaiterais la session du matin...",
    deal_data=deal_data,  # Optionnel, dict CRM
    current_department="DOC"
)
```

### Structure retournée
```python
{
    'action': 'GO' | 'ROUTE' | 'SPAM' | 'DUPLICATE_UBER' | 'NEEDS_CLARIFICATION',
    'target_department': 'DOC' | 'Contact' | 'Comptabilité' | etc,
    'detected_intent': 'DEMANDE_DATES_FUTURES',     # Intention principale
    'primary_intent': 'DEMANDE_DATES_FUTURES',      # Alias
    'secondary_intents': ['QUESTION_SESSION'],       # Intentions secondaires
    'reason': 'Explication du choix',
    'confidence': 0.95,
    'intent_context': {
        'is_urgent': bool,
        'mentions_force_majeure': bool,
        'force_majeure_type': 'medical' | 'death' | 'accident' | 'childcare' | 'other' | None,
        'force_majeure_details': 'description courte' | None,
        'wants_earlier_date': bool,
        'session_preference': 'jour' | 'soir' | None
    }
}
```

### Actions possibles
| Action | Comportement |
|--------|--------------|
| `GO` | Ticket DOC valide, continuer le workflow |
| `ROUTE` | Transférer vers autre département |
| `SPAM` | Spam/pub, clôturer automatiquement |
| `DUPLICATE_UBER` | Doublon offre Uber 20€ |
| `NEEDS_CLARIFICATION` | Besoin de clarification |

### Extraction automatique de contexte
- `session_preference` : Extrait "jour" ou "soir" si le candidat le mentionne
- `force_majeure_type` : Détecte "medical", "death", "accident", "childcare"
- `wants_earlier_date` : Détecte si le candidat veut une date plus tôt

### ATTENTION - Extraction de l'intention
```python
# CORRECT
intention = result.get("detected_intent")
session_pref = result.get("intent_context", {}).get("session_preference")

# FAUX (ne pas utiliser)
# intention = result.get("intent_context", {}).get("intention")  # N'EXISTE PAS!
```

---

## 2. CRMUpdateAgent

**Fichier :** `src/agents/crm_update_agent.py`
**Recommandé** - Agent spécialisé pour TOUTES les mises à jour CRM.

### Fonctionnalités
- Mapping automatique string → ID pour les champs lookup
- Respect des règles de blocage (VALIDE CMA + clôture passée)
- Note CRM optionnelle

### Signature
```python
from src.agents.crm_update_agent import CRMUpdateAgent

agent = CRMUpdateAgent()

result = agent.update_from_ticket_response(
    deal_id="123456",
    ai_updates={
        'Date_examen_VTC': '2026-03-31',
        'Session_choisie': 'Cours du soir'
    },
    deal_data=deal_data,
    session_data=session_data,  # Sessions proposées par session_helper
    ticket_id="789012",
    auto_add_note=False  # Note consolidée gérée par le workflow
)
```

### Mappings automatiques
| Champ | Entrée | Transformation |
|-------|--------|----------------|
| `Date_examen_VTC` | Date string ("2026-03-31") | ID session via `find_exam_session_by_date_and_dept()` |
| `Session_choisie` | Nom ("Cours du soir") | ID en cherchant dans sessions proposées |
| `Preference_horaire` | Texte ("soir") | Pas de mapping |

### Règles de blocage
**Refuse de modifier `Date_examen_VTC` si :**
- Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"}
- ET `Date_Cloture_Inscription` < aujourd'hui

---

## 3. DealLinkingAgent

**Fichier :** `src/agents/deal_linking_agent.py`
Lie les tickets Zoho Desk aux deals CRM.

### Signature
```python
from src.agents.deal_linking_agent import DealLinkingAgent

agent = DealLinkingAgent()
result = agent.process({"ticket_id": "123456"})
```

### Structure retournée
```python
{
    'deal_id': '123456789',
    'deal_data': { ... },           # Données complètes du deal
    'all_deals': [ ... ],           # Tous les deals du contact
    'has_duplicate_uber_offer': bool,  # Doublon détecté
    'duplicate_deals': [ ... ],     # Deals en doublon si applicable
    'routing_info': {
        'should_route': bool,
        'target_department': 'Contact' | None,
        'reason': '...'
    }
}
```

### Détection de doublon Uber 20€
```python
# L'agent détecte automatiquement les doublons
deals_20_won = [d for d in all_deals if d.get("Amount") == 20 and d.get("Stage") == "GAGNÉ"]
if len(deals_20_won) > 1:
    result["has_duplicate_uber_offer"] = True
    result["duplicate_deals"] = deals_20_won
```

---

## 4. ExamT3PAgent

**Fichier :** `src/agents/examt3p_agent.py`
Extrait les données de la plateforme ExamT3P.

### Signature
```python
from src.agents.examt3p_agent import ExamT3PAgent

agent = ExamT3PAgent()
data = agent.extract_data(identifiant, mot_de_passe)
```

### Structure retournée
```python
{
    'compte_existe': True,
    'connection_test_success': True,
    'statut_dossier': 'En cours de composition',
    'num_dossier': '00038886',
    'documents': [
        {'name': 'CNI', 'status': 'validé'},
        {'name': 'Photo', 'status': 'en attente'}
    ],
    'paiements': [
        {'date': '2026-01-15', 'montant': 241, 'status': 'payé'}
    ],
    'examens': [ ... ],
    'departement': '75'
}
```

### Gestion des erreurs
- Si connexion échoue → `connection_test_success = False`
- Si compte n'existe pas → `compte_existe = False`
- Workflow continue même si ExamT3P indisponible

---

## 5. TicketDispatcherAgent

> ⚠️ **`src/agents/dispatcher_agent.py` n'existe pas.** Le seul fichier lié est `examples/ticket_dispatcher.py`, un script d'exemple **non branché en production**. Le routage réel est fait par `DOCTicketWorkflow._run_triage()` + `BusinessRules.determine_department_from_deals_and_ticket()` (business_rules.py:215).

### Départements disponibles
Voir `desk_departments.json` pour la liste complète :
- DOC, DOCS CAB, Contact, Comptabilité, Refus CMA, etc.

---

## 6. RelationsTriageAgent

**Fichier :** `src/agents/relations_triage_agent.py`
Triage des emails B2B du département Relations entreprises (LLM + fallback déterministe par mots-clés).

### Signature
```python
from src.agents.relations_triage_agent import RelationsTriageAgent

agent = RelationsTriageAgent()
result = agent.process({
    "subject": "Demande de devis CACES R489",
    "message": "...",
    "email": "contact@entreprise.fr",
    "crm_context": {...}
})
```

### 15 intentions B2B
`DEMANDE_DEVIS_FORMATION`, `DEMANDE_DISPONIBILITE_SESSION`, `INSCRIPTION_CANDIDATS`, `COMMANDE_FORMALOGISTICS`, `ANNULATION_REPORT_ABSENCE`, `CONVENTION_CONTRAT_DOSSIER`, `BON_DE_COMMANDE`, `CONVOCATION_CONFIRMATION`, `ATTESTATION_FIN_FORMATION`, `DOCUMENTS_SIGNATURES_MANQUANTS`, `FACTURE_FINANCEMENT_PEC`, `BILAN_FORMATEUR`, `PROSPECTION_PARTENARIAT`, `CV_PROFILS_INTERVENANTS`, `AUTRE_A_QUALIFIER`

### Actions possibles
| Action | Comportement |
|--------|--------------|
| `DRAFT` | Brouillon client possible (jamais d'envoi direct) |
| `IGNORE_NOISE` | Spam, newsletter, no-reply, notification automatique |
| `ROUTE_COMPTA` | Litige facture / relance comptable |
| `ROUTE_HUMAN` | Demande sensible ou ambiguïté forte |

### Extraction
`formation_type`, `centre`, `start_date`/`end_date`, `nb_candidates`, `categories` (CACES), `type_ir`, `financement`, `missing_fields`, etc.

**Modèle :** `MODEL_EXTRACTION` (claude-haiku-4-5)

---

## 7. RelationsResponseAgent

**Fichier :** `src/agents/relations_response_agent.py`

Rédige une réponse courte à partir du dernier message externe, de la conversation, du triage, du contexte CRM autorisé et d'une base déterministe. Il retourne aussi `requires_human_action` et `human_action_reason` lorsque le conseiller doit vérifier une inscription, une disponibilité, un tarif ou un document.

Garde-fous principaux : aucun montant inventé, aucune pièce jointe annoncée, aucune confirmation d'action sans preuve, HTML limité et fallback déterministe en cas d'échec IA.

Pour les demandes de formation incomplètes, `1 candidat` et `initial` peuvent être utilisés comme hypothèses par défaut pour PlanBot. `defaulted_fields` oblige alors l'agent à demander leur confirmation dans le dernier paragraphe.

---

## 8. BaseAgent (Classe abstraite)

**Fichier :** `src/agents/base_agent.py`
Classe de base pour tous les agents.

### Méthode principale
```python
class BaseAgent:
    def ask(self, prompt: str, system_prompt: str = None) -> str:
        """Appelle Claude avec le prompt donné."""
        pass
```

### Modèle utilisé
`settings.agent_model` de `config.py` (legacy). Les agents du workflow utilisent les modèles centralisés dans `src/constants/models.py` (`MODEL_TRIAGE`, `MODEL_HUMANIZER`, `MODEL_EXTRACTION`...).

---

## Bonnes Pratiques

### 1. Ne pas passer ticket_id seul au TriageAgent
```python
# FAUX
result = agent.triage_ticket(ticket_id)

# CORRECT
result = agent.triage_ticket(
    ticket_subject=ticket['subject'],
    thread_content=threads_content,
    deal_data=deal_data
)
```

### 2. Toujours utiliser CRMUpdateAgent pour les mises à jour
```python
# FAUX - mapping manuel
crm_client.update_deal(deal_id, {'Date_examen_VTC': '2026-03-31'})

# CORRECT - mapping automatique + validation
agent.update_from_ticket_response(
    deal_id=deal_id,
    ai_updates={'Date_examen_VTC': '2026-03-31'},
    deal_data=deal_data
)
```

### 3. Vérifier le résultat du DealLinkingAgent
```python
result = agent.process({"ticket_id": ticket_id})

if result.get('has_duplicate_uber_offer'):
    # Traitement spécial doublon Uber
    pass

if result.get('routing_info', {}).get('should_route'):
    # Router vers autre département
    pass
```
