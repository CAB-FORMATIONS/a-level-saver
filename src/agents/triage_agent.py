"""
TriageAgent - Agent IA pour le triage intelligent des tickets.

Remplace le système de keywords par une analyse contextuelle avec Claude.
Comprend le SENS du message, pas juste les mots-clés.
Détecte également l'INTENTION du candidat pour un traitement approprié.

UTILISATION:
    agent = TriageAgent()
    result = agent.triage_ticket(
        ticket_subject="Form submission from: Assistance",
        thread_content="J'ai téléchargé tous les documents...",
        deal_data=deal_data  # Optionnel
    )
    # Retourne: action, target_department, reason, confidence, detected_intent, intent_context
"""
import logging
from typing import Dict, Any, Optional
import json
from pathlib import Path

# Load environment variables for Anthropic API key
from dotenv import load_dotenv
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / ".env")

from .base_agent import BaseAgent

# Import BusinessRules pour la détection d'envoi de documents
try:
    from business_rules import BusinessRules
except ImportError:
    BusinessRules = None

logger = logging.getLogger(__name__)


class TriageAgent(BaseAgent):
    """Agent IA pour le triage intelligent des tickets CAB Formations."""

    SYSTEM_PROMPT = """Tu es un expert du triage de tickets pour CAB Formations, un centre de formation VTC.

CONTEXTE MÉTIER:
- CAB Formations prépare les candidats à l'examen VTC (théorique)
- Partenariat Uber: offre à 20€ pour les chauffeurs Uber
- Processus: Inscription → Formation → Examen CMA → Obtention carte VTC

DÉPARTEMENTS DISPONIBLES:
- DOC: Questions sur formation, examen, dates, sessions, identifiants ExamT3P (département par défaut pour candidats Uber 20€)
- Refus CMA: Si la CMA a REFUSÉ un document OU si le candidat nous TRANSMET des documents (pièces jointes, justificatifs)
- Contact: Demandes commerciales, autres formations (NON Uber 20€), RGPD
- Comptabilité:
  * Candidat DEMANDE EXPLICITEMENT sa facture pour la formation/offre souscrite
  * Demande d'attestation/certificat de formation pour France Travail ou Pôle Emploi

RÈGLES DE TRIAGE:

1. **SPAM** → Messages publicitaires, phishing, sans rapport avec la formation

2. **GO (rester dans DOC)** pour:
   - Candidat qui CONFIRME avoir envoyé ses documents (même s'il dit "document")
   - Candidat qui fournit ses identifiants ExamT3P
   - Questions sur dates d'examen, sessions de formation
   - Demandes de changement de date / report
   - Questions sur le dossier en cours
   - ⚠️ **PROSPECTS UBER 20€ (Stage = EN ATTENTE)**: TOUJOURS GO !
     Ces prospects doivent être poussés à finaliser leur paiement des 20€
     On répond à leurs questions et on les encourage à convertir

3. **ROUTE vers Refus CMA** si:
   - Le candidat signale que la CMA a REFUSÉ son dossier
   - OU deal_data.Evalbox == "Refusé CMA" ou "Documents manquants"
   - OU le candidat nous ENVOIE des documents en pièce jointe (intention TRANSMET_DOCUMENTS)
     → On doit uploader ces documents sur son compte ExamT3P manuellement
   ⚠️ EXCEPTION: Si Date_Dossier_reçu est VIDE → GO + TRANSMET_DOCUMENTS (pas de route)
     → C'est un envoi initial de documents, on traite dans DOC

4. **ROUTE vers Contact** si:
   - Demande d'information sur une formation NON Uber (formation classique, CACES, etc.)
   - ⚠️ "TAXI" n'est PAS un motif de routage vers Contact ! Les candidats VTC mentionnent souvent "taxi" (examen taxi/vtc, erreur inscription)
   - ⚠️ JAMAIS pour les prospects Uber 20€ même en EN ATTENTE - ils restent dans DOC !
   - Demande de suppression de données (RGPD, droit à l'oubli, destruction données)
     → Intention DEMANDE_SUPPRESSION_DONNEES → Note automatique: "Transférer à jc@cab-formations.fr (Référent RGPD)"

IMPORTANT - DISTINCTION DOCUMENTS:
- "J'ai téléchargé mes documents SUR EXAMT3P" = GO (ENVOIE_DOCUMENTS - il l'a fait lui-même)
- "Voici mon passeport en pièce jointe" = ROUTE Refus CMA (TRANSMET_DOCUMENTS - on doit uploader pour lui)
  ⚠️ SAUF si Date_Dossier_reçu est VIDE → GO (envoi initial, on gère dans DOC)
- "Mon document a été refusé" = ROUTE Refus CMA (problème de refus CMA)
- Comprends le CONTEXTE, pas juste les mots-clés
- **PROSPECT UBER 20€ = TOUJOURS DOC** pour les pousser à payer et avancer
- **Date_Dossier_reçu VIDE = TOUJOURS DOC** même pour TRANSMET_DOCUMENTS (envoi initial)

IMPORTANT - DISTINCTION "FACTURE":
- Sujet "Facture" SANS demande explicite = candidat TRANSMET un justificatif de domicile (facture EDF, téléphone...)
  → ROUTE Refus CMA (TRANSMET_DOCUMENTS)
- "Je voudrais ma facture pour la formation" = candidat DEMANDE sa facture de paiement
  → ROUTE Comptabilité
- En cas de doute, si le candidat n'écrit pas explicitement "je veux/demande ma facture", c'est un document transmis

IMPORTANT - RÉFUGIÉS / DEMANDEURS D'ASILE:
⚠️ Les réfugiés politiques et demandeurs d'asile n'ont PAS besoin de passeport !
- Si le candidat mentionne "demande d'asile", "réfugié", "protection subsidiaire", "titre de séjour" + question sur le passeport
  → Ce n'est PAS une demande de remboursement !
  → GO + intention DOCUMENT_QUESTION
  → Le titre de séjour ou récépissé de demande d'asile SUFFIT comme pièce d'identité
- Exemples à traiter dans DOC (pas Contact):
  - "Je suis réfugié, dois-je fournir un passeport ?" → DOCUMENT_QUESTION (réponse: non, titre de séjour suffit)
  - "Je n'ai pas de passeport car demandeur d'asile" → DOCUMENT_QUESTION (réponse: pas besoin, récépissé suffit)
  - "Titre de séjour à la place du passeport ?" → DOCUMENT_QUESTION

---

DÉTECTION D'INTENTIONS (TOUTES, pas seulement la principale):

Quand l'action est GO, tu dois identifier TOUTES les intentions exprimées par le candidat.
Un candidat peut avoir PLUSIEURS intentions dans un même message - c'est très fréquent !

INTENTIONS POSSIBLES (par ordre de spécificité - préfère les intentions spécifiques):

**Intentions liées aux DATES D'EXAMEN:**
- DEMANDE_DATE_EXAMEN: Demande de connaître sa date d'examen (candidat AVEC date assignée)
  Exemples: "quelle est ma date d'examen", "quand est mon examen", "date de l'examen", "je n'ai pas reçu ma date", "c'est quand l'examen"
  ⚠️ Utiliser si "Date examen actuelle" contient une date ET le candidat demande juste à la CONNAÎTRE
  ⚠️ DIFFÉRENT de DEMANDE_DATES_FUTURES: le candidat A DÉJÀ une date (vérifier contexte CRM)
  ⚠️ DIFFÉRENT de DEMANDE_CONVOCATION: le candidat demande la DATE, pas le document officiel de convocation
- DEMANDE_DATES_FUTURES: Demande de dates d'examen disponibles (candidat SANS date assignée)
  Exemples: "Quelles sont les prochaines dates ?", "dates disponibles"
  ⚠️ Utiliser SEULEMENT si "Date examen actuelle" = "Aucune date assignée"
- REPORT_DATE: Veut CHANGER sa date d'examen actuelle vers une date ULTÉRIEURE (candidat AVEC date assignée)
  Exemples: "Je voudrais reporter", "changer ma date", "décaler mon examen", "repousser"
  ⚠️ Si "Date examen actuelle" contient une date ET que le candidat demande une autre date/mois/département → c'est REPORT_DATE !
  ⚠️ PRIORITÉ MAXIMALE: Si le candidat indique qu'il sera ABSENT/INDISPONIBLE à sa date actuelle (voyage, hospitalisation, travail...) → c'est REPORT_DATE même s'il pose aussi une question sur la convocation !
  Exemples avec date existante: "je voudrais juillet au lieu de mars", "dates à Montpellier" (si sa date actuelle est ailleurs), "je ne peux pas en mars"
  Exemples d'indisponibilité: "je serai en voyage le jour de l'examen", "je pars le 15 et l'examen est le 24", "je ne serai pas disponible à cette date"
  ⚠️ CAS PIÈGE: "je n'ai pas reçu ma convocation et je suis en voyage à partir du 15" → Le vrai problème est l'ABSENCE, pas la convocation. primary_intent = REPORT_DATE
  ⚠️ CAS IMPLICITE: Si le candidat demande une formation/session à un MOIS ou une DATE qui est APRÈS sa date d'examen actuelle → c'est REPORT_DATE (pas DEMANDE_CHANGEMENT_SESSION).
  Mettre implicit_date_repositioning: true dans intent_context.
  Exemples (Date examen actuelle = "2026-03-31"):
  - "formation en mai" → REPORT_DATE + implicit_date_repositioning: true + requested_month: 5
  - "disponible en septembre" → REPORT_DATE + implicit_date_repositioning: true + requested_month: 9
  - "je voudrais les cours du soir en juin" → REPORT_DATE + implicit_date_repositioning: true + requested_month: 6
- DEMANDE_DATE_PLUS_TOT: Veut une date PLUS TÔT que sa date actuelle
  Exemples: "date plus tôt", "plus proche", "plus rapide", "au plus vite", "avancer mon examen", "passer avant", "février au lieu de mars"
  ⚠️ DIFFÉRENT de REPORT_DATE: le candidat demande un mois/date AVANT sa date actuelle (pas après)
  ⚠️ Vérifier si le mois demandé < mois de la date actuelle → DEMANDE_DATE_PLUS_TOT
  ⚠️ IMPORTANT: Si le candidat demande "février" et sa date est en "mars" → c'est DEMANDE_DATE_PLUS_TOT
  ⚠️ Réponse attendue: vérifier cross-département, si aucune option → expliquer que c'est impossible et garder date actuelle
- CONFIRMATION_DATE_EXAMEN: Candidat CONFIRME son choix de date d'examen
  Exemples: "je confirme la date du 15 mars", "je choisis le 31/03", "ok pour cette date",
            "je confirme le 28/04/2026", "je veux changer pour le 28 avril"
  ⚠️ Important pour mise à jour CRM (crm_update: true)
  ⚠️ EXTRAIRE la date confirmée dans intent_context.confirmed_new_exam_date au format "YYYY-MM-DD"
- DEMANDE_AUTRES_DEPARTEMENTS: Veut voir des dates dans d'autres villes/départements
  Exemples: "dates ailleurs", "autre département", "dates à Lyon", "d'autres options"

**Intentions liées à la FORMATION:**
- QUESTION_SESSION: POSE UNE QUESTION sur les sessions (veut des informations)
  Exemples: "c'est quoi les cours du soir ?", "quels sont les horaires ?", "comment ça se passe ?"
  ⚠️ SEULEMENT si c'est une vraie QUESTION (interrogatif), PAS un choix/décision
- CONFIRMATION_SESSION: FAIT UN CHOIX / CONFIRME sa session (décision prise)
  Exemples: "je choisis cours du soir", "je prends les cours du jour", "je confirme la formation du jour",
            "je participerai aux sessions du 16/03 au 27/03", "je préfère les cours du jour",
            "je garde les cours du jour", "les cours du jour me conviennent", "OK pour cours du soir"
  ⚠️ PRIORITÉ sur QUESTION_SESSION et DEMANDE_DATE_VISIO: si le candidat EXPRIME UN CHOIX
     ("je choisis", "je prends", "je préfère", "je garde", "OK pour"), c'est CONFIRMATION_SESSION
  ⚠️ Si session DÉJÀ assignée et candidat dit "je choisis/garde [même type]" = CONFIRMATION de sa session actuelle
     Exemple: session actuelle = "cdj-..." (cours du jour), candidat dit "je choisis les cours du jour"
     → C'est CONFIRMATION_SESSION (il confirme sa session), PAS une question ni un changement
  ⚠️ Regarder le contexte CRM "Session formation actuelle" pour savoir le type actuel
- DEMANDE_CHANGEMENT_SESSION: Candidat avec session DÉJÀ assignée veut CHANGER de session
  Exemples: "changer de session", "modifier ma session", "passer en cours du soir", "décaler ma formation", "reporter ma formation", "autres dates"
  ⚠️ DIFFÉRENT de CONFIRMATION_SESSION: le candidat a DÉJÀ une session et veut en CHANGER
  ⚠️ DIFFÉRENT de QUESTION_SESSION: le candidat ne pose pas de question, il veut MODIFIER
  ⚠️ Indicateurs: session déjà assignée + demande de modification/changement
  ⚠️ Si la formation demandée est APRÈS la date d'examen actuelle → REPORT_DATE (pas DEMANDE_CHANGEMENT_SESSION) + implicit_date_repositioning: true

  ⚠️ IMPORTANT - Détecter si c'est une PLAINTE (erreur CAB) ou un changement volontaire:
  - is_complaint: true si le candidat signale une ERREUR d'inscription (on lui a assigné la mauvaise session)
    Indicateurs: "ne correspond pas", "j'avais indiqué", "j'avais choisi", "erreur", "pas mon choix",
                 "contrairement à", "pourtant j'avais demandé", "ce n'est pas ce que j'ai demandé"
  - claimed_session: extraire la session que le candidat AFFIRME avoir demandée initialement
    → claimed_type: "jour" ou "soir" (ce qu'il dit avoir demandé)
    → claimed_dates: dates mentionnées comme demandées initialement (format YYYY-MM-DD)
  - assigned_session_wrong: ce qu'il a reçu par erreur (si mentionné)
    → wrong_type: "jour" ou "soir"
    → wrong_dates: dates de la mauvaise session
- DEMANDE_DATE_VISIO: Demande la date/heure de sa prochaine formation en visio OU accès aux 40 heures
  Exemples: "quand est ma formation ?", "date de la visio", "horaires de la formation", "mes 40 heures", "40h de formation", "accès à mes heures", "heures de formation"
  ⚠️ PRIORITÉ SUR DEMANDE_ELEARNING_ACCESS: si le candidat mentionne "40 heures", "40h", ou "heures de formation" → c'est DEMANDE_DATE_VISIO
  ⚠️ Les 40h = sessions de formation en visioconférence (cours du jour ou du soir), PAS l'e-learning !
  ⚠️ L'e-learning (cab-formations.fr/user) = modules en ligne, DIFFÉRENT des 40h visio
- DEMANDE_LIEN_VISIO: Demande le lien Zoom/Teams pour rejoindre la formation
  Exemples: "lien zoom", "lien de la formation", "comment rejoindre la visio"
- DEMANDE_CERTIFICAT_FORMATION: Demande son certificat/attestation de formation (souvent pour France Travail/Pôle Emploi)
  Exemples: "certificat de formation", "attestation", "justificatif de formation", "France Travail me demande", "Pôle Emploi"
  ⚠️ Action: ROUTE vers Comptabilité - c'est eux qui génèrent les attestations

**Intentions liées au DOSSIER:**
- STATUT_DOSSIER: Question sur l'avancement
  Exemples: "où en est mon dossier", "mon inscription", "avancement", "statut"
- DOCUMENT_QUESTION: Question sur les documents requis ou leur format
  Exemples: "quels documents", "pièces à fournir", "document manquant", "format accepté"
  ⚠️ INCLUT les questions de réfugiés/demandeurs d'asile sur le passeport → réponse: titre de séjour suffit
- ENVOIE_DOCUMENTS: Candidat CONFIRME avoir téléchargé ses documents SUR EXAMT3P lui-même
  Exemples: "j'ai téléchargé mes documents sur ExamT3P", "j'ai mis mes pièces sur le site", "documents ajoutés sur mon espace"
  ⚠️ Action: GO - le candidat a fait l'upload lui-même, on accuse réception
- TRANSMET_DOCUMENTS: Candidat nous ENVOIE des documents en pièce jointe (passeport, permis, etc.)
  Exemples: "voici mon passeport", "ci-joint mes documents", "je vous envoie mon permis", "je vous ai envoyé les photos"
  ⚠️ Action: Si Date_Dossier_reçu remplie → ROUTE Refus CMA (correction/ajout de docs)
  ⚠️ Action: Si Date_Dossier_reçu VIDE → GO + intention TRANSMET_DOCUMENTS (envoi initial, on gère dans DOC)
- SIGNALE_PROBLEME_DOCS: Problème technique lors de l'upload des documents
  Exemples: "erreur lors de l'envoi", "impossible de télécharger", "bug sur le site"
- QUESTION_PERMIS_ETRANGER: Question sur permis de conduire étranger (hors zone Euro)
  Exemples: "j'ai un permis marocain", "permis algérien accepté?", "dois-je échanger mon permis", "permis étranger"
  ⚠️ Seuls permis français ou européens (zone Euro) acceptés. Autres = échange de permis obligatoire
- QUESTION_CARTE_SEJOUR: Question sur carte de séjour (expirée, récépissé)
  Exemples: "ma carte de séjour est expirée", "j'ai un récépissé", "titre de séjour périmé"
  ⚠️ Si expirée, récépissé de renouvellement OBLIGATOIRE pour s'inscrire
- QUESTION_HEBERGEMENT: Question sur justificatif de domicile quand hébergé
  Exemples: "je suis hébergé chez mes parents", "pas de facture à mon nom", "attestation d'hébergement"
  ⚠️ Si facture mobile à son nom sur adresse hébergement = suffit. Sinon = attestation + pièce ID hébergeur
- CONFIRMATION_PAIEMENT: Confirmation ou question sur le paiement
  Exemples: "j'ai payé", "paiement effectué", "facture", "preuve de paiement"

**Intentions liées à la CONVOCATION:**
- DEMANDE_CONVOCATION: Demande de convocation CMA
  Exemples: "où est ma convocation", "quand vais-je recevoir ma convocation", "pas reçu de convocation", "convocation examen"
  ⚠️ NE PAS utiliser si le candidat mentionne qu'il sera ABSENT à l'examen (voyage, maladie, etc.) → utiliser REPORT_DATE à la place

**Intentions liées à l'E-LEARNING:**
- DEMANDE_ELEARNING_ACCESS: Demande d'accès à la formation e-learning
  Exemples: "accès formation", "code e-learning", "connexion formation", "identifiants formation", "comment accéder aux cours"

**Intentions liées aux IDENTIFIANTS:**
- DEMANDE_IDENTIFIANTS: Demande d'identifiants ExamT3P
  Exemples: "mot de passe oublié", "mes identifiants", "connexion ExamT3P"
- ENVOIE_IDENTIFIANTS: Candidat PARTAGE ses identifiants ExamT3P
  Exemples: "voici mes identifiants", "mon login est...", "email: xxx, mdp: yyy"
  ⚠️ Important pour mise à jour CRM des credentials
- REFUS_PARTAGE_CREDENTIALS: Refuse de partager ses identifiants (sécurité)
  Exemples: "je ne veux pas donner mon mot de passe", "données personnelles", "RGPD"
- PROBLEME_CONNEXION_EXAMT3P: Problème de connexion à ExamT3P
  Exemples: "je n'arrive pas à me connecter à examt3p", "erreur de connexion", "mot de passe refusé"
- PROBLEME_CONNEXION_ELEARNING: Problème de connexion à la plateforme e-learning
  Exemples: "je n'arrive pas à accéder aux cours", "erreur sur cab-formations", "connexion e-learning impossible"

**Intentions liées à l'OFFRE UBER:**
- DEMANDE_INFOS_OFFRE: Questions sur l'offre Uber 20€
  Exemples: "comment marche l'offre Uber", "c'est quoi l'offre à 20€", "conditions Uber"

**Intentions liées aux RÉSULTATS:**
- RESULTAT_EXAMEN: Question sur le résultat (candidat demande son résultat)
  Exemples: "résultat de l'examen", "ai-je réussi", "admis ou pas"
- ANNONCE_RESULTAT_POSITIF: Candidat ANNONCE qu'il a réussi
  Exemples: "j'ai réussi !", "je suis admis", "j'ai eu mon examen"
- ANNONCE_RESULTAT_NEGATIF: Candidat ANNONCE qu'il a échoué
  Exemples: "j'ai raté", "je n'ai pas réussi", "recalé", "échec à l'examen"
- DEMANDE_REINSCRIPTION: Candidat veut se réinscrire après échec
  Exemples: "je veux me réinscrire", "repasser l'examen", "nouvelle inscription"

**Intentions liées à la CARTE VTC:**
- QUESTION_CARTE_VTC: Question sur la carte VTC après réussite
  Exemples: "comment obtenir ma carte VTC", "demande de carte", "carte professionnelle"
- QUESTION_EXAMEN_PRATIQUE: Question sur l'examen/formation pratique (hors offre Uber 20€)
  Exemples: "examen pratique", "formation pratique", "partie pratique", "pratique incluse", "théorique et pratique", "conduite", "véhicule double commande"

**Autres intentions:**
- QUESTION_PROCESSUS: Question sur le processus
  Exemples: "comment ça marche", "prochaines étapes", "c'est quoi la suite"
- DEMANDE_SUPPRESSION_DONNEES: Demande RGPD de suppression de compte/données
  Exemples: "supprimer mes données", "droit à l'oubli", "effacer mon compte", "article 17 RGPD",
            "droit à l'effacement", "supprimer mon compte", "destruction de mes données",
            "exercer mon droit RGPD", "suppression de compte"
  ⚠️ PRIORITÉ ABSOLUE: Toujours détecter cette intention, même si DUPLICATE_UBER
  → Action: ROUTE vers Contact (référent RGPD)
- PERMIS_PROBATOIRE: Question sur le permis probatoire (jeune permis < 3 ans)
  Exemples: "permis probatoire", "jeune permis", "moins de 3 ans de permis", "fin de probation", "j'ai atteint 3 ans"
  ⚠️ IMPORTANT: Ajouter dans intent_context.probation_status:
    - "completed": le candidat ANNONCE qu'il a atteint les 3 ans (ex: "j'ai atteint les 3 ans", "j'ai maintenant 3 ans de permis") → il est PRÊT, pas besoin de lui demander la date
    - "pending": le candidat n'a PAS encore 3 ans et DEMANDE quand il pourra s'inscrire
    - "question": question générale sur le permis probatoire
- PERMIS_RENOUVELLEMENT: Permis en cours de renouvellement, abîmé, volé ou perdu
  Exemples: "nouveau permis", "renouvellement permis", "permis abîmé", "permis volé", "permis perdu", "en attente de mon permis", "ANTS", "récépissé"
  ⚠️ Réponse: Ils peuvent utiliser l'ancien permis (même abîmé) + récépissé ANTS pour s'inscrire à l'examen
- DATE_LOINTAINE_EXAMT3P: Le candidat ne peut pas choisir la date qu'il veut sur ExamT3P
  Exemples: "je ne peux pas choisir de date en juillet", "la date n'apparaît pas", "pas de date disponible en août"
  ⚠️ DIFFÉRENT de REPORT_DATE: ici le candidat CONSTATE une impossibilité, il ne DEMANDE pas un changement
- DEMANDE_EXCEPTION: Demande d'exception ou dérogation pour passer l'examen plus tôt
  Exemples: "moyen exceptionnel", "exception possible", "dérogation", "vraiment aucun moyen", "aucune solution"
  ⚠️ DIFFÉRENT de DEMANDE_DATES_FUTURES: le candidat sait que c'est trop tard et demande une EXCEPTION aux règles
  ⚠️ DIFFÉRENT de REPORT_DATE: pas de date existante à changer, il veut contourner les règles de clôture
- DEMANDE_APPEL_TEL: Candidat demande à être appelé
  Exemples: "appelez-moi", "pouvez-vous m'appeler", "je préfère par téléphone"
- RECLAMATION: Candidat mécontent, réclamation
  Exemples: "pas satisfait", "plainte", "je veux me plaindre", "scandaleux"
- ERREUR_PAIEMENT_CMA: Candidat Uber 20€ qui a payé les frais CMA (237€/241€) lui-même par erreur
  Exemples: "j'ai payé les frais", "j'ai été débité de 237€", "on m'a prélevé", "je me suis fait rembourser ?", "j'ai réglé moi-même"
  ⚠️ UNIQUEMENT pour les candidats Uber 20€ qui mentionnent avoir payé les frais CMA
  ⚠️ NE PAS ROUTER vers Comptabilité - reste dans DOC avec réponse explicative
  ⚠️ DIFFÉRENT de DEMANDE_REMBOURSEMENT générale
  Pour ERREUR_PAIEMENT_CMA, détecter si le candidat CONFIRME son choix:
  - remboursement_cma_choice: "remboursement" si le candidat dit "je choisis le remboursement", "option 1", "je préfère demander le remboursement"
  - remboursement_cma_choice: "conserver" si le candidat dit "je garde mon paiement", "option 2", "je préfère conserver"
  - remboursement_cma_choice: null si c'est la première détection (pas encore de choix)
- DEMANDE_ANNULATION: Demande d'annulation, rétractation ou remboursement de l'offre Uber 20€
  Exemples: "je veux annuler", "remboursement", "rétractation", "je veux arrêter", "désistement",
            "annuler mon inscription", "je ne veux plus", "c'est une arnaque", "à mon insu"
  ⚠️ Action: **GO** (rester dans DOC) - NE PAS ROUTER vers Contact ou Comptabilité !
  ⚠️ L'offre Uber 20€ est non remboursable - le template DOC gère la réponse appropriée
  ⚠️ Ne pas utiliser si c'est un candidat Uber qui a payé les frais CMA 241€ → utiliser ERREUR_PAIEMENT_CMA
  Pour DEMANDE_ANNULATION, détecter le motif (cancellation_reason):
  - cancellation_reason: "timing" si indisponibilité/dates ne conviennent pas (ex: "pas disponible", "dates ne me conviennent pas", "pas présent")
  - cancellation_reason: "retractation" si rétractation/désistement/veut arrêter (ex: "remboursement", "rétractation", "désistement", "ne veut plus")
  - cancellation_reason: "contestation" si conteste l'offre/malentendu (ex: "arnaque", "mensonger", "à mon insu", "pensais que ça incluait tout")

**Détection eligibility_concern (valable pour TOUTE intention, pas seulement DEMANDE_ANNULATION) :**
  Mettre eligibility_concern: true si le candidat mentionne un problème d'éligibilité Uber :
  - "compte Uber bloqué", "compte bloqué", "pas éligible", "non éligible"
  - "on m'a dit que je ne peux pas m'inscrire", "inscription refusée", "inscription impossible"
  - "compte Uber Eats bloqué/désactivé/suspendu"
  - "pas le droit de s'inscrire", "interdit inscription"
  Sinon: eligibility_concern: false
- REMERCIEMENT: Simple remerciement sans autre demande
  Exemples: "merci beaucoup", "super merci", "c'est parfait merci"

**Intentions liées aux DOUBLONS (clarification email):**
- CONFIRMATION_DOUBLON: Le candidat CONFIRME qu'il s'agit bien de lui / même personne
  Exemples: "oui c'est bien moi", "c'est mon dossier", "oui c'est le même", "je confirme",
            "c'est effectivement moi", "oui je me suis déjà inscrit", "c'est bien mon ancien dossier"
  ⚠️ Utiliser quand on lui a demandé de confirmer un doublon potentiel (nom+CP identiques)
  ⚠️ Le candidat reconnaît son ancienne inscription / son autre dossier
- REFUS_DOUBLON: Le candidat dit que ce N'EST PAS lui / pas la même personne
  Exemples: "non ce n'est pas moi", "ce n'est pas mon dossier", "je ne connais pas",
            "c'est quelqu'un d'autre", "première inscription", "homonyme", "jamais inscrit"
  ⚠️ Le candidat nie être la même personne que le doublon trouvé
- QUESTION_GENERALE: UNIQUEMENT si aucune intention spécifique ne correspond
  ⚠️ N'utilise QUESTION_GENERALE que si tu ne peux vraiment pas classifier autrement !

**EXEMPLES DE MULTI-INTENTIONS (très fréquent):**
- "Je voudrais les dates de Montpellier pour juillet et des infos sur les cours du soir"
  → SI Date examen actuelle = "Aucune date assignée": primary_intent: DEMANDE_DATES_FUTURES, secondary_intents: ["QUESTION_SESSION"]
  → SI Date examen actuelle = "31/03/2026": primary_intent: REPORT_DATE, secondary_intents: ["QUESTION_SESSION", "DEMANDE_AUTRES_DEPARTEMENTS"]
- "Où en est mon dossier ? Et quand est mon examen ?"
  → primary_intent: STATUT_DOSSIER, secondary_intents: ["DEMANDE_DATES_FUTURES"]
- "Je confirme le cours du soir. C'est quoi les prochaines étapes ?"
  → primary_intent: CONFIRMATION_SESSION, secondary_intents: ["QUESTION_PROCESSUS"]
- "Y a-t-il des dates plus tôt dans d'autres départements ?"
  → primary_intent: DEMANDE_DATES_FUTURES, secondary_intents: ["DEMANDE_AUTRES_DEPARTEMENTS"]

Pour REPORT_DATE, ajoute un contexte supplémentaire:
- is_urgent: true si examen imminent (< 7 jours) ou mention d'urgence
- mentions_force_majeure: true si le candidat mentionne un motif de force majeure
- force_majeure_type: "medical" (maladie, hospitalisation, santé), "death" (décès, deuil), "accident", "other", ou null

MOTIFS DE FORCE MAJEURE:
IMPORTANT: La force majeure doit affecter DIRECTEMENT le candidat ou un membre de sa famille proche.
Si c'est un problème indirect (ex: l'assistante maternelle qui a un décès dans SA famille), ce n'est PAS
une force majeure du candidat mais une contrainte de garde d'enfant → force_majeure_type = "childcare" ou "other"

- Medical: maladie DU CANDIDAT, hospitalisation, problème de santé, opération, certificat médical, douleurs, enceinte, accouchement
- Death: décès d'un PROCHE DU CANDIDAT (parent, conjoint, enfant, frère/sœur) - PAS décès chez la nounou/voisin/etc.
- Accident: accident DU CANDIDAT (voiture, travail, etc.)
- Childcare: problème de garde d'enfant (nounou absente, assistante maternelle indisponible, etc.)
- Other: convocation judiciaire, catastrophe naturelle, autre contrainte personnelle

Pour force_majeure_details, préciser QUI est affecté (le candidat directement ou quelqu'un d'autre).

CONTEXTE SUPPLÉMENTAIRE (pour toutes les intentions):
- wants_earlier_date: true si le candidat demande une date plus tôt, plus proche, plus rapide,
  ou s'il mentionne vouloir un autre département, d'autres options, toutes les dates disponibles,
  ou une urgence particulière (pressé, au plus vite, rapidement, etc.)
- mentioned_month: Mois MENTIONNÉ par le candidat (1-12), MÊME en mode clarification ou vérification
  DIFFÉRENT de requested_month qui implique une DEMANDE explicite de changement
  Exemples:
  - "vous m'aviez dit février vers le 24" → mentioned_month: 2, requested_month: null
  - "je voudrais passer en mars" → mentioned_month: 3, requested_month: 3
  - "c'est toujours le 15 juin ?" → mentioned_month: 6, requested_month: null
  ⚠️ TOUJOURS extraire le mois si mentionné, cela permet de proposer des alternatives
- requested_month: le mois spécifique DEMANDÉ pour un changement (1-12 ou null si non mentionné)
  Exemples: "je voudrais juillet" → 7, "reporter à septembre" → 9
  ⚠️ Ne pas confondre avec mentioned_month: ici c'est une DEMANDE, pas une mention
- confirmed_new_exam_date: Date d'examen CONFIRMÉE par le candidat au format "YYYY-MM-DD"
  Exemples: "je confirme le 28/04/2026" → "2026-04-28", "je choisis la date du 28 avril" → "2026-04-28"
  ⚠️ IMPORTANT: Extraire cette date pour CONFIRMATION_DATE_EXAMEN et REPORT_DATE si le candidat confirme une date précise
  ⚠️ Format: TOUJOURS "YYYY-MM-DD" (année-mois-jour). Convertir les formats FR (28/04/2026) en ISO (2026-04-28)
- requested_location: la ville ou le département demandé tel que mentionné par le candidat
  Exemples: "Montpellier", "Lyon", "Paris", "département 34"
- requested_dept_code: le CODE DÉPARTEMENT (2 chiffres) correspondant à la location demandée
  Tu DOIS convertir les villes en codes département français:
  Paris/Île-de-France → "75", Lyon → "69", Marseille → "13", Toulouse → "31",
  Montpellier → "34", Nantes → "44", Bordeaux → "33", Lille → "59", Nice → "06",
  Strasbourg → "67", Rennes → "35", Rouen → "76", Nîmes → "30", Perpignan → "66"
  Si le candidat mentionne directement un numéro de département, utilise-le.
  null si aucune location mentionnée.

CONTEXTE COMMUNICATION (comment le candidat formule sa demande):
- communication_mode: Le MODE de formulation du message (pas le sujet)
  - "request": Demande directe d'info ou d'action (défaut)
    Exemples: "Quelles sont les dates ?", "Je veux changer de date", "Envoyez-moi mes identifiants"
  - "clarification": Le candidat questionne une INCOHÉRENCE ou demande des éclaircissements
    Exemples: "vous m'aviez dit février mais je vois mars", "c'est annulé ?", "c'est toujours valable ?"
    ⚠️ IMPORTANT: utilisé quand le candidat note une DISCORDANCE entre ce qu'il a compris et ce qu'il voit
  - "verification": Le candidat vérifie sa COMPRÉHENSION (pas un choix)
    Exemples: "donc si j'ai bien compris c'est le 31 mars ?", "pour confirmer...", "c'est bien ça ?"
    ⚠️ DIFFÉRENT de confirmation: il ne CONFIRME pas un choix, il VÉRIFIE une info
  - "follow_up": Suite EXPLICITE à un message précédent
    Exemples: "suite à votre mail", "comme convenu", "vous m'avez demandé de..."

- references_previous_communication: true si le candidat mentionne un email/message PRÉCÉDENT de CAB
  Exemples: "vous m'aviez dit", "dans votre dernier mail", "on m'a dit que", "j'ai reçu un mail"

- mentions_discrepancy: true si le candidat note une INCOHÉRENCE entre 2 sources d'info
  Exemples: "mais je vois", "pourtant", "par contre", "c'est différent", "annulé ?", "toujours valable ?"

---

EXTRACTION DES DATES DE FORMATION DEMANDÉES (requested_training_dates):

⚠️ DISTINCTION CRITIQUE - NE PAS CONFONDRE:
- **current_session_dates**: Dates de la session à laquelle le candidat est DÉJÀ inscrit
  → Extraites des mails de confirmation CAB (noreply@info.zohomeeting.com, etc.)
  → Ces dates sont du CONTEXTE, PAS une demande du candidat
- **requested_training_dates**: Dates que le candidat DEMANDE explicitement
  → Ses disponibilités, congés, périodes souhaitées
  → SEULEMENT si le candidat les mentionne DANS SON MESSAGE (pas dans les mails cités)

EXEMPLE - Le candidat répond à un mail de confirmation "Formation VTC - 16/02/2026 au 20/02/2026":
Message candidat: "Bonjour j'ai repris le travail j'aimerais suivre ma formation le soir"
→ current_session_dates: {start_date: "2026-02-16", end_date: "2026-02-20"} (du mail de confirmation)
→ requested_training_dates: null (le candidat ne demande PAS de dates spécifiques !)
→ session_preference: "soir" (il veut juste changer l'horaire)

EXEMPLE - Le candidat donne SES disponibilités:
Message candidat: "Je serai en congés du 21 au 28 février, je voudrais faire ma formation à ce moment"
→ requested_training_dates: {start_date: "2026-02-21", end_date: "2026-02-28", ...} (SA demande)

Quand le candidat mentionne des dates de DISPONIBILITÉ pour sa formation (dans SON message, pas dans les mails cités):

1. **Plages de dates explicites:**
   - "du 21 au 28 février" → start_date: "2026-02-21", end_date: "2026-02-28", month: 2, is_range: true
   - "entre le 15 et le 20 mars" → start_date: "2026-03-15", end_date: "2026-03-20", month: 3, is_range: true
   - "du 21/02 au 28/02" → start_date: "2026-02-21", end_date: "2026-02-28", month: 2, is_range: true

2. **Semaine:**
   - "la semaine du 10 février" → start_date: "2026-02-10", end_date: "2026-02-16", month: 2, is_range: true (7 jours)
   - "semaine du 15 mars" → start_date: "2026-03-15", end_date: "2026-03-21", month: 3, is_range: true

3. **Date unique:**
   - "à partir du 15 février" → start_date: "2026-02-15", end_date: null, month: 2, is_range: false
   - "disponible le 20 mars" → start_date: "2026-03-20", end_date: "2026-03-20", month: 3, is_range: false

4. **Inférence de préférence horaire (inferred_preference):**
   - "9h-18h", "de 9h à 18h", "journée", "toute la journée" → inferred_preference: "jour"
   - "18h-22h", "après le travail", "le soir", "en soirée" → inferred_preference: "soir"
   - Si session_preference déjà explicite, utiliser celle-ci à la place

5. **Normalisation:**
   - Année: utiliser 2026 (ou 2027 si la date est passée)
   - raw_text: garder le texte original (ex: "du 21 au 28 février")
   - Formats acceptés: "21 février", "21/02", "21 fév", "21 fevrier"

EXEMPLE COMPLET:
Message: "Je serai en congés du 21 au 28 février, disponible de 9h à 18h"
→ requested_training_dates: {
    start_date: "2026-02-21",
    end_date: "2026-02-28",
    month: 2,
    raw_text: "du 21 au 28 février",
    is_range: true,
    inferred_preference: "jour"
  }
→ session_preference: "jour" (copier inferred_preference si pas d'autre indication)

---

Réponds UNIQUEMENT en JSON valide:
{
    "action": "GO" | "ROUTE" | "SPAM",
    "target_department": "DOC" | "Refus CMA" | "Contact" | "Comptabilité" | null,
    "reason": "explication courte",
    "confidence": 0.0-1.0,
    "primary_intent": "REPORT_DATE" | "DEMANDE_IDENTIFIANTS" | "STATUT_DOSSIER" | "CONFIRMATION_SESSION" | "DEMANDE_DATES_FUTURES" | "QUESTION_SESSION" | "PERMIS_PROBATOIRE" | "DATE_LOINTAINE_EXAMT3P" | "QUESTION_GENERALE" | ... | null,
    "secondary_intents": ["QUESTION_SESSION", "DEMANDE_DATES_FUTURES", ...],
    "intent_context": {
        "is_urgent": true | false,
        "mentions_force_majeure": true | false,
        "force_majeure_type": "medical" | "death" | "accident" | "childcare" | "other" | null,
        "force_majeure_details": "description courte si force majeure détectée" | null,
        "wants_earlier_date": true | false,
        "session_preference": "jour" | "soir" | null,
        "mentioned_month": 1-12 | null,
        "requested_month": 1-12 | null,
        "requested_location": "ville ou département tel que mentionné" | null,
        "requested_dept_code": "75" | "34" | ... | null,
        "remboursement_cma_choice": "remboursement" | "conserver" | null,
        "cancellation_reason": "timing" | "retractation" | "contestation" | null,
        "eligibility_concern": true | false,
        "communication_mode": "request" | "clarification" | "verification" | "follow_up",
        "references_previous_communication": true | false,
        "mentions_discrepancy": true | false,
        "discrepancy_details": "description courte si discordance détectée" | null,
        "current_session_dates": {
            "start_date": "YYYY-MM-DD" | null,
            "end_date": "YYYY-MM-DD" | null,
            "raw_text": "texte original des dates" | null,
            "source": "confirmation_email" | "context" | null
        } | null,
        "requested_training_dates": {
            "start_date": "YYYY-MM-DD" | null,
            "end_date": "YYYY-MM-DD" | null,
            "month": 1-12 | null,
            "raw_text": "texte original des dates" | null,
            "is_range": true | false,
            "inferred_preference": "jour" | "soir" | null
        } | null,
        "is_complaint": true | false,
        "claimed_session": {
            "claimed_type": "jour" | "soir" | null,
            "claimed_dates": "YYYY-MM-DD - YYYY-MM-DD" | null,
            "claimed_dates_raw": "texte original" | null
        } | null,
        "assigned_session_wrong": {
            "wrong_type": "jour" | "soir" | null,
            "wrong_dates": "YYYY-MM-DD - YYYY-MM-DD" | null,
            "wrong_dates_raw": "texte original" | null
        } | null,
        "probation_status": "completed" | "pending" | "question" | null,
        "implicit_date_repositioning": true | false
    }
}

IMPORTANT: Si le candidat exprime plusieurs intentions, liste l'intention principale dans primary_intent
et les autres dans secondary_intents (array, peut être vide).

Pour CONFIRMATION_SESSION, extraire dans intent_context:
- session_preference: "jour" ou "soir" si mentionné explicitement
  → "jour" si: cours du jour, formation du jour, journée, matin
  → "soir" si: cours du soir, formation du soir, soirée, après le travail
- confirmed_session_dates: "DD/MM/YYYY-DD/MM/YYYY" si le candidat mentionne une plage de dates
  → Exemples: "du 16/03 au 27/03" → "16/03/2026-27/03/2026"
  → Format: date_debut-date_fin (avec l'année en cours ou l'année suivante si passée)

⚠️ CRITIQUE - Pour DEMANDE_CHANGEMENT_SESSION, TOUJOURS distinguer dans intent_context:

1. **current_session_dates**: Dates extraites du mail de confirmation (contexte, pas une demande)
   → Vient de: "Formation VTC - 16/02/2026 au 20/02/2026" dans un mail CAB
   → {start_date: "2026-02-16", end_date: "2026-02-20", source: "confirmation_email"}

2. **requested_training_dates**: Dates que le candidat DEMANDE dans SON message (null si pas de demande)
   → SEULEMENT si le candidat dit "je voudrais du X au Y", "mes disponibilités sont..."
   → "du 21 au 28 février" → {start_date: "2026-02-21", end_date: "2026-02-28", month: 2, ...}

3. **session_preference**: Préférence horaire (jour/soir)
   → "9h-18h", "journée" → "jour"
   → "le soir", "après travail" → "soir"

EXEMPLE 1 - Candidat veut JUSTE changer d'horaire (cas fréquent):
Contexte: Mail de confirmation "Formation VTC - 16/02/2026 au 20/02/2026"
Message candidat: "Bonjour j'ai repris le travail j'aimerais suivre ma formation le soir"
→ primary_intent: "DEMANDE_CHANGEMENT_SESSION"
→ session_preference: "soir"
→ current_session_dates: {start_date: "2026-02-16", end_date: "2026-02-20", source: "confirmation_email"}
→ requested_training_dates: null (il ne demande PAS de dates spécifiques !)
→ is_complaint: false

EXEMPLE 2 - Candidat donne SES disponibilités:
Message candidat: "Je serai en congés du 21 au 28 février, disponible de 9h à 18h"
→ primary_intent: "DEMANDE_CHANGEMENT_SESSION"
→ session_preference: "jour"
→ current_session_dates: null (pas de contexte de session actuelle)
→ requested_training_dates: {start_date: "2026-02-21", end_date: "2026-02-28", month: 2, raw_text: "du 21 au 28 février", is_range: true, inferred_preference: "jour"}
→ is_complaint: false

EXEMPLE PLAINTE - Message: "J'avais clairement indiqué mon choix pour une formation en cours du jour du 16/02 au 20/02, mais je reçois une confirmation pour cours du soir du 16/03 au 27/03, cela ne correspond pas à ma demande"
→ primary_intent: "DEMANDE_CHANGEMENT_SESSION"
→ is_complaint: true (signale une erreur)
→ claimed_session: {claimed_type: "jour", claimed_dates: "2026-02-16 - 2026-02-20", claimed_dates_raw: "du 16/02 au 20/02"}
→ assigned_session_wrong: {wrong_type: "soir", wrong_dates: "2026-03-16 - 2026-03-27", wrong_dates_raw: "du 16/03 au 27/03"}
→ session_preference: "jour" (ce qu'il veut vraiment)
"""

    def __init__(self):
        super().__init__(
            name="TriageAgent",
            system_prompt=self.SYSTEM_PROMPT
        )

    def process(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Interface standard pour le traitement (requis par BaseAgent).

        Args:
            data: {
                'ticket_subject': str,
                'thread_content': str,
                'deal_data': Dict (optionnel),
                'current_department': str (optionnel)
            }

        Returns:
            Résultat du triage
        """
        return self.triage_ticket(
            ticket_subject=data.get('ticket_subject', ''),
            thread_content=data.get('thread_content', ''),
            deal_data=data.get('deal_data'),
            current_department=data.get('current_department', 'DOC')
        )

    def triage_ticket(
        self,
        ticket_subject: str,
        thread_content: str,
        deal_data: Optional[Dict[str, Any]] = None,
        current_department: str = "DOC",
        conversation_summary: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Analyse un ticket et détermine l'action de triage + intention du candidat.

        Args:
            ticket_subject: Sujet du ticket
            thread_content: Contenu du dernier message du client
            deal_data: Données du deal CRM (optionnel)
            current_department: Département actuel du ticket
            conversation_summary: Résumé de l'historique de conversation (optionnel)

        Returns:
            {
                'action': 'GO' | 'ROUTE' | 'SPAM',
                'target_department': str ou None,
                'reason': str,
                'confidence': float,
                'method': 'ai',
                'detected_intent': str ou None (REPORT_DATE, DEMANDE_IDENTIFIANTS, etc.),
                'intent_context': {
                    'is_urgent': bool,
                    'mentions_force_majeure': bool,
                    'force_majeure_type': str ou None,
                    'force_majeure_details': str ou None
                }
            }
        """
        # Construire le contexte pour l'IA
        context_parts = [
            f"**Sujet du ticket:** {ticket_subject}",
        ]

        # Ajouter le résumé de conversation si disponible (pour le contexte historique)
        if conversation_summary:
            context_parts.append(f"**Historique de la conversation (résumé):**\n{conversation_summary}")

        context_parts.extend([
            f"**Dernier message du client:**\n{thread_content[:2000]}",  # Limiter la taille
            f"**Département actuel:** {current_department}"
        ])

        # Ajouter les infos du deal si disponibles
        if deal_data:
            # Utiliser la vraie date d'examen (enrichie par le workflow depuis le module Sessions_d_examen)
            # Le champ Date_examen_VTC est un lookup qui contient juste {'name': '...', 'id': '...'}
            # La vraie date est dans _real_exam_date (ajoutée par le workflow)
            date_examen_info = "Aucune date assignée"
            real_exam_date = deal_data.get('_real_exam_date')
            if real_exam_date:
                # Format YYYY-MM-DD → affichage plus lisible
                date_examen_info = f"{real_exam_date} (date assignée)"
            elif deal_data.get('Date_examen_VTC'):
                # Fallback: lookup non enrichi, on indique juste qu'une date existe
                date_examen_info = "Date assignée (détails non disponibles)"

            # Récupérer l'info de la session actuelle
            session_info = "Aucune session assignée"
            session = deal_data.get('Session')
            if session:
                session_name = session.get('name', str(session)) if isinstance(session, dict) else str(session)
                # Déterminer le type de session depuis le nom
                session_type = "jour" if session_name.lower().startswith('cdj') else "soir" if session_name.lower().startswith('cds') else "inconnu"
                session_info = f"{session_name} (cours du {session_type})"

            deal_info = [
                f"**Deal trouvé:** {deal_data.get('Deal_Name', 'N/A')}",
                f"**Montant:** {deal_data.get('Amount', 'N/A')}€",
                f"**Stage:** {deal_data.get('Stage', 'N/A')}",
                f"**Evalbox:** {deal_data.get('Evalbox', 'N/A')}",
                f"**Date examen actuelle:** {date_examen_info}",
                f"**Session formation actuelle:** {session_info}"
            ]
            context_parts.append("\n".join(deal_info))

            # Règle automatique: Si Evalbox indique un refus → vérifier l'intention
            # LOGIQUE MÉTIER (modifiée 2026-01-31):
            # - Si Evalbox = "Refusé CMA" ET envoi de documents → Refus CMA (il sait, il corrige)
            # - Si Evalbox = "Refusé CMA" ET fournit identifiants → GO (vérifier compte)
            # - Si Evalbox = "Refusé CMA" SANS envoi de documents → GO (il ne sait pas encore, workflow l'informe)
            evalbox = deal_data.get('Evalbox', '')
            # Evalbox qui déclenchent le routage vers Refus CMA si envoi de documents
            # "Pret a payer" inclus car le candidat peut répondre à une demande de document manquant
            evalbox_needs_doc_routing = ['Refusé CMA', 'Documents manquants', 'Documents refusés', 'Pret a payer']
            if evalbox in evalbox_needs_doc_routing:
                # Vérifier si le dernier message contient des identifiants ExamT3P
                thread_lower = thread_content.lower() if thread_content else ''
                has_credentials = (
                    ('mot de passe' in thread_lower or 'password' in thread_lower or 'mdp' in thread_lower)
                    and ('@' in thread_content)  # Présence d'un email
                )

                if has_credentials:
                    # Le candidat a fourni ses identifiants → on traite le ticket normalement
                    logger.info(f"  🔍 Evalbox = '{evalbox}' MAIS identifiants détectés → GO (vérification compte)")
                    return {
                        'action': 'GO',
                        'target_department': current_department,
                        'reason': f"Evalbox = '{evalbox}' mais le candidat fournit des identifiants - vérification du compte ExamT3P nécessaire",
                        'confidence': 1.0,
                        'method': 'rule_credentials_override',
                        'primary_intent': 'ENVOIE_IDENTIFIANTS',
                        'secondary_intents': [],
                        'detected_intent': 'ENVOIE_IDENTIFIANTS',
                        'intent_context': {'has_credentials': True, 'evalbox_status': evalbox}
                    }

                # Vérifier si le candidat ENVOIE des documents (intention TRANSMET_DOCUMENTS)
                has_document_keywords = False
                if BusinessRules:
                    if ticket_subject and BusinessRules.is_document_submission(ticket_subject):
                        has_document_keywords = True
                    if thread_content and BusinessRules.is_document_submission(thread_content):
                        has_document_keywords = True

                if has_document_keywords:
                    # Le candidat envoie des documents → router vers Refus CMA pour traitement
                    logger.info(f"  🔍 Evalbox = '{evalbox}' ET envoi de documents → Route vers Refus CMA")
                    return {
                        'action': 'ROUTE',
                        'target_department': 'Refus CMA',
                        'reason': f"Evalbox = '{evalbox}' et le candidat envoie des documents",
                        'confidence': 1.0,
                        'method': 'rule_evalbox_with_documents',
                        'primary_intent': 'TRANSMET_DOCUMENTS',
                        'secondary_intents': [],
                        'detected_intent': 'TRANSMET_DOCUMENTS',
                        'intent_context': {'evalbox_status': evalbox}
                    }
                else:
                    # Pas d'envoi de documents → rester en DOC, le workflow informera le candidat
                    logger.info(f"  🔍 Evalbox = '{evalbox}' MAIS pas d'envoi de documents → GO (workflow informera le candidat)")
                    # NE PAS retourner ici - laisser le triage IA détecter l'intention réelle
                    # Le workflow utilisera le template approprié pour informer du refus

            # Règle automatique: Demande d'attestation France Travail / Pôle Emploi → Comptabilité
            thread_lower = thread_content.lower() if thread_content else ''
            subject_lower = ticket_subject.lower() if ticket_subject else ''
            combined_text = f"{subject_lower} {thread_lower}"

            attestation_keywords = ['attestation', 'certificat de formation', 'justificatif de formation']
            france_travail_keywords = ['france travail', 'pôle emploi', 'pole emploi', 'francetravail']

            has_attestation = any(kw in combined_text for kw in attestation_keywords)
            has_france_travail = any(kw in combined_text for kw in france_travail_keywords)

            if has_attestation and has_france_travail:
                logger.info(f"  🔍 Demande d'attestation France Travail détectée → Route vers Comptabilité")
                return {
                    'action': 'ROUTE',
                    'target_department': 'Comptabilité',
                    'reason': "Demande d'attestation/certificat de formation pour France Travail - Comptabilité génère les attestations",
                    'confidence': 1.0,
                    'method': 'rule_attestation_france_travail',
                    'primary_intent': 'DEMANDE_CERTIFICAT_FORMATION',
                    'secondary_intents': [],
                    'detected_intent': 'DEMANDE_CERTIFICAT_FORMATION',
                    'intent_context': {'for_france_travail': True}
                }

        context = "\n\n".join(context_parts)

        # Appeler Claude pour l'analyse
        try:
            from anthropic import Anthropic

            client = Anthropic()
            response = client.messages.create(
                model="claude-sonnet-4-20250514",  # Modèle précis pour ne pas rater les intentions
                max_tokens=800,  # Sonnet peut être plus verbeux
                system=self.SYSTEM_PROMPT,
                messages=[
                    {"role": "user", "content": f"Analyse ce ticket et détermine l'action de triage:\n\n{context}"}
                ]
            )

            response_text = response.content[0].text.strip()
            logger.info(f"  🤖 TriageAgent response: {response_text[:200]}...")

            # Parser la réponse JSON
            # Nettoyer le JSON si nécessaire
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]

            # Extraire uniquement le JSON (ignorer le texte après)
            # Chercher le premier { et le dernier } correspondant
            start_idx = response_text.find('{')
            if start_idx != -1:
                brace_count = 0
                end_idx = start_idx
                for i, char in enumerate(response_text[start_idx:], start_idx):
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_idx = i + 1
                            break
                response_text = response_text[start_idx:end_idx]

            result = json.loads(response_text)

            # Valider et normaliser
            action = result.get('action', 'GO').upper()
            if action not in ['GO', 'ROUTE', 'SPAM']:
                action = 'GO'

            target_dept = result.get('target_department')
            if action == 'GO':
                target_dept = current_department

            # Extraire les intentions (support multi-intentions)
            primary_intent = result.get('primary_intent') or result.get('detected_intent')
            secondary_intents = result.get('secondary_intents', [])
            intent_context = result.get('intent_context', {})

            # Normaliser intent_context et secondary_intents
            if not isinstance(intent_context, dict):
                intent_context = {}
            if not isinstance(secondary_intents, list):
                secondary_intents = []

            # Log les intentions détectées
            if primary_intent:
                logger.info(f"  🎯 Intention principale: {primary_intent}")
            if secondary_intents:
                logger.info(f"  🎯 Intentions secondaires: {secondary_intents}")
            if intent_context.get('mentions_force_majeure'):
                logger.info(f"  ⚠️ Force majeure mentionnée: {intent_context.get('force_majeure_type')} - {intent_context.get('force_majeure_details', 'N/A')}")
            if intent_context.get('is_urgent'):
                logger.info(f"  🚨 Situation urgente détectée")
            if intent_context.get('current_session_dates'):
                logger.info(f"  📅 Session actuelle (contexte): {intent_context.get('current_session_dates')}")
            if intent_context.get('requested_training_dates'):
                logger.info(f"  📅 Dates demandées par le candidat: {intent_context.get('requested_training_dates')}")
            if intent_context.get('session_preference'):
                logger.info(f"  ⏰ Préférence session: {intent_context.get('session_preference')}")
            if intent_context.get('is_complaint'):
                logger.info(f"  ⚠️ PLAINTE détectée: candidat signale une erreur d'inscription")
                if intent_context.get('claimed_session'):
                    logger.info(f"  📋 Session réclamée: {intent_context.get('claimed_session')}")
                if intent_context.get('assigned_session_wrong'):
                    logger.info(f"  ❌ Session erronée reçue: {intent_context.get('assigned_session_wrong')}")

            return {
                'action': action,
                'target_department': target_dept,
                'reason': result.get('reason', 'Analyse IA'),
                'confidence': float(result.get('confidence', 0.8)),
                'method': 'ai',
                # Multi-intentions
                'primary_intent': primary_intent,
                'secondary_intents': secondary_intents,
                # Rétrocompatibilité
                'detected_intent': primary_intent,
                'intent_context': intent_context
            }

        except json.JSONDecodeError as e:
            logger.warning(f"  ⚠️ TriageAgent JSON error: {e}")
            # Fallback: rester dans le département actuel
            return {
                'action': 'GO',
                'target_department': current_department,
                'reason': 'Erreur parsing IA - fallback GO',
                'confidence': 0.5,
                'method': 'fallback',
                'primary_intent': None,
                'secondary_intents': [],
                'detected_intent': None,
                'intent_context': {}
            }

        except Exception as e:
            logger.error(f"  ❌ TriageAgent error: {e}")
            # Fallback: rester dans le département actuel
            return {
                'action': 'GO',
                'target_department': current_department,
                'reason': f'Erreur IA: {str(e)[:50]} - fallback GO',
                'confidence': 0.3,
                'method': 'fallback',
                'primary_intent': None,
                'secondary_intents': [],
                'detected_intent': None,
                'intent_context': {}
            }

    def should_use_ai_triage(
        self,
        ticket_subject: str,
        thread_content: str
    ) -> bool:
        """
        Détermine si on doit utiliser le triage IA ou les règles simples.

        Pour économiser les appels API, on utilise l'IA seulement si:
        - Le contenu contient des mots ambigus (document, etc.)
        - Le sujet n'est pas clairement identifiable

        Returns:
            True si triage IA recommandé
        """
        combined = (ticket_subject + " " + thread_content).lower()

        # Mots ambigus qui nécessitent une analyse contextuelle
        ambiguous_words = [
            'document', 'pièce', 'justificatif', 'fichier',
            'envoyé', 'téléchargé', 'uploadé', 'joint'
        ]

        # Si mots ambigus présents → IA
        if any(word in combined for word in ambiguous_words):
            return True

        # Sinon, les règles simples suffisent
        return False
