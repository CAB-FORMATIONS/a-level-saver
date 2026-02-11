"""
Business rules for ticket-deal automation.

IMPORTANT: Customize these rules for your business!

See business_rules.example.py for detailed examples and documentation.

Modify the methods below to match your:
- Sales processes
- Deal stages
- Customer segmentation
- Automation preferences
"""
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


# ===== INDICATEURS DE QUESTION (vs envoi) =====
# Si ces patterns sont présents, le candidat POSE UNE QUESTION sur les documents
# et n'est PAS en train d'en envoyer → rester dans DOC
QUESTION_INDICATORS = [
    # Questions directes
    "est-ce que", "est ce que", "est-ce qu'", "est ce qu'",
    "serait-il possible", "serait il possible", "serait-ce possible",
    "peut-on", "peut on", "pourrait-on", "pourrait on",
    "puis-je", "puis je", "pourrais-je", "pourrais je",
    "faut-il", "faut il", "dois-je", "dois je",
    "acceptez-vous", "acceptez vous", "accepteriez-vous",
    "comment faire", "comment puis-je", "comment dois-je",
    "quel format", "quels formats", "quel type", "quels documents",
    "quelle pièce", "quelles pièces",
    # Expression de manque/absence
    "je n'ai pas", "je n ai pas", "pas encore de", "pas de ",
    "je ne possède pas", "je ne dispose pas",
    "n'ai pas encore", "n ai pas encore",
    # Demande de clarification
    "obligatoire", "nécessaire", "requis", "exigé",
    "suffit-il", "suffit il", "suffirait-il",
    "à la place", "en remplacement", "au lieu de",
    "alternative", "autres options",
    # Questions conditionnelles
    "si je n'ai pas", "si je n ai pas",
    "dans le cas où", "au cas où",
    "que faire si", "comment procéder si",
]

# ===== KEYWORDS POUR DÉTECTION D'ENVOI DE DOCUMENTS =====
# ATTENTION: Ces keywords doivent être SPÉCIFIQUES pour éviter les faux positifs
# Le mot "document" seul est trop générique (apparaît dans footers, signatures, HTML)
DOCUMENT_KEYWORDS = [
    # Envoi explicite de pièces jointes
    "ci-joint", "ci joint", "pièce jointe", "piece jointe",
    "fichier joint", "en pièce jointe", "en piece jointe",
    "attachment", "attaché", "attache",
    "je vous envoie ci-joint", "veuillez trouver ci-joint",
    "vous trouverez ci-joint", "je vous transmets",
    # Envoi de documents (formulations courantes)
    "voici mes documents", "voici mes pièces", "voici mes pieces",
    "je vous envoie les pièces", "je vous envoie les pieces",
    "je vous envoie les documents", "les pièces demandées", "les pieces demandees",
    "les pièces demandés", "les pieces demandes",
    # Note: "document" seul retiré - trop générique (faux positifs avec footers/HTML)

    # Identité
    "pièce d'identité", "piece d'identite", "photo d'identité", "photo d'identite",
    "carte d'identité", "carte d'identite", "cni", "passeport",
    "titre de séjour", "titre de sejour",
    "récépissé de titre de séjour", "recepisse de titre de sejour",
    "récépissé de permis", "recepisse de permis",
    "récépissé", "recepisse",

    # Domicile
    "justificatif de domicile", "justificatif domicile",
    "attestation d'hébergement", "attestation d'hebergement", "attestation hebergement",
    "preuve de domicile",

    # Signature de document (pas signature email)
    # Note: "signature" retiré car trop de faux positifs avec signatures email
    "document signé", "contrat signé", "formulaire signé"
]


class BusinessRules:
    """Your custom business rules. Modify these methods!"""

    @staticmethod
    def strip_forwarded_content(content: str) -> str:
        """
        Supprime le contenu transféré/cité d'un email pour éviter les faux positifs.

        Les emails transférés de CMA contiennent souvent des footers avec "TAXI",
        "capacité de transport", etc. qui ne reflètent pas l'intention du candidat.
        Les réponses incluent aussi le message précédent de CAB cité en dessous,
        qui peut contenir "examen pratique" etc.

        Args:
            content: Contenu HTML ou texte de l'email

        Returns:
            Contenu nettoyé sans les blockquotes/forwards/quoted replies
        """
        import re

        if not content:
            return ""

        # 1. Supprimer les <blockquote> HTML (emails transférés)
        content = re.sub(r'<blockquote[^>]*>.*?</blockquote>', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 2. Supprimer les conteneurs Gmail de citation
        content = re.sub(r'<div\s+class="gmail_quote"[^>]*>.*?</div>\s*$', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<div\s+class="gmail_extra"[^>]*>.*?</div>\s*$', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 3. Supprimer les conteneurs Outlook de citation
        content = re.sub(r'<div\s+id="appendonsend"[^>]*>.*', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'<div\s+id="divRplyFwdMsg"[^>]*>.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 4. Supprimer les sections "Begin forwarded message" et après
        content = re.sub(r'Begin forwarded message:.*', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'---------- Forwarded message ---------.*', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'Message transféré.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 5. Supprimer les en-têtes de réponse français (coupent tout après)
        # "Le 08/02/2026 à 10:30, doc@cab-formations.fr a écrit :"
        content = re.sub(r'Le\s+\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\s+[àa]\s+\d{1,2}[h:]\d{2}.*?(?:a\s+[eé]crit|wrote)\s*:.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 5b. Gmail textual date format: "Le mer. 11 févr. 2026 à 11:34, ... a écrit :"
        content = re.sub(r'Le\s+\w+\.?\s+\d{1,2}\s+\w+\.?\s+\d{4}\s+[àa]\s+\d{1,2}[h:]\d{2}.*?(?:a\s+[eé]crit|wrote)\s*:.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 6. Supprimer les en-têtes Outlook FR/EN
        # "De : doc@cab-formations.fr\nEnvoyé : ...\nÀ : ...\nObjet : ..."
        content = re.sub(r'(?:De|From)\s*:.*?(?:Objet|Subject)\s*:.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 7. Supprimer "-----Message d'origine-----" / "-----Original Message-----"
        content = re.sub(r'-{3,}\s*(?:Message d.origine|Original Message)\s*-{3,}.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 8. Supprimer les séparateurs Outlook (ligne de underscores)
        content = re.sub(r'_{10,}.*', '', content, flags=re.DOTALL)

        # 9. Supprimer les lignes citées (commençant par >)
        content = re.sub(r'^>.*$', '', content, flags=re.MULTILINE)

        # 10. Supprimer les signatures email communes
        content = re.sub(r'Sent from my iPhone.*', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'Envoyé depuis mon.*', '', content, flags=re.DOTALL | re.IGNORECASE)
        content = re.sub(r'Envoy[eé] de mon.*', '', content, flags=re.DOTALL | re.IGNORECASE)

        return content

    @staticmethod
    def is_document_question(thread_content: str) -> bool:
        """
        Détecte si le contenu est une QUESTION sur les documents (vs un envoi).

        Args:
            thread_content: Contenu du thread

        Returns:
            True si c'est une question/clarification sur les documents
        """
        if not thread_content:
            return False

        content_lower = thread_content.lower()

        for indicator in QUESTION_INDICATORS:
            if indicator in content_lower:
                logger.info(f"❓ QUESTION_INDICATOR matched: '{indicator}' in content")
                return True

        # Présence de "?" suggère une question
        if "?" in thread_content:
            logger.info("❓ QUESTION_INDICATOR matched: '?' in content")
            return True

        return False

    @staticmethod
    def is_document_submission(thread_content: str) -> bool:
        """
        Détecte si le contenu d'un thread correspond à un envoi de documents.

        IMPORTANT: Retourne False si c'est une QUESTION sur les documents.

        Args:
            thread_content: Contenu du thread (peut contenir du HTML)

        Returns:
            True si envoi de documents détecté (et pas une question)
        """
        if not thread_content:
            return False

        content_lower = thread_content.lower()

        # D'abord vérifier si c'est une question → pas un envoi
        if BusinessRules.is_document_question(thread_content):
            logger.info("📋 Document keyword présent mais contexte = QUESTION → pas un envoi")
            return False

        # Log which keyword matched for debugging
        for keyword in DOCUMENT_KEYWORDS:
            if keyword in content_lower:
                logger.info(f"📄 DOCUMENT_KEYWORD matched: '{keyword}' in content")
                return True

        return False

    @staticmethod
    def determine_department_from_deals_and_ticket(
        all_deals: List[Dict[str, Any]],
        ticket: Dict[str, Any],
        last_thread_content: Optional[str] = None
    ) -> Optional[str]:
        """
        LOGIQUE COMPLÈTE DE ROUTING basée sur les deals CRM et le ticket.

        WORKFLOW:
        1. Filtrer les deals à 20€
        2. Priorité 1: Deal 20€ GAGNÉ (le plus récent closing_date)
        3. Priorité 2: Deal 20€ EN ATTENTE
        4. Si deal 20€ trouvé: vérifier conditions Refus CMA vs DOC
        5. Si pas de deal 20€: chercher autre montant GAGNÉ ou EN ATTENTE → Contact
        6. Sinon: fallback sur keywords

        Args:
            all_deals: TOUS les deals liés au contact
            ticket: Ticket data complet
            last_thread_content: Contenu du dernier thread (optionnel)

        Returns:
            Nom du département ou None (fallback keywords)
        """
        if not all_deals:
            return None

        # Étape 1: Filtrer les deals à 20€
        deals_20 = [d for d in all_deals if d.get("Amount") == 20]

        # Étape 2: Prioriser GAGNÉ (plus récent)
        deals_20_won = [d for d in deals_20 if d.get("Stage") == "GAGNÉ"]

        selected_deal = None

        if deals_20_won:
            # Prendre le plus récent (Closing_Date)
            deals_20_won_sorted = sorted(
                deals_20_won,
                key=lambda d: d.get("Closing_Date", ""),
                reverse=True
            )
            selected_deal = deals_20_won_sorted[0]
            deal_source = "20€ GAGNÉ (plus récent)"

        else:
            # Étape 3: Chercher EN ATTENTE
            deals_20_pending = [d for d in deals_20 if d.get("Stage") == "EN ATTENTE"]
            if deals_20_pending:
                selected_deal = deals_20_pending[0]
                deal_source = "20€ EN ATTENTE"

        # Étape 4: Si deal 20€ trouvé, déterminer DOC ou REFUS CMA
        if selected_deal:
            # RÈGLE PRIORITAIRE: Si deal 20€ existe MAIS candidat demande autre service → Contact
            # Mots-clés à détecter dans le sujet et/ou dernier thread
            other_service_keywords = [
                # Examen pratique (hors partenariat Uber €20 qui ne couvre que le théorique)
                "examen pratique",
                "pratique vtc",
                "convocation pratique",
                "épreuve pratique",
                "epreuve pratique",
                # Autres formations
                "autre formation",
                "formation pratique",
                "double commande",
                # Location véhicule
                "location véhicule",
                "location de véhicule",
                "louer un véhicule",
                "louer véhicule",
                # CPF / Compte Formation
                "cpf",
                "formation cpf",
                "mon compte cpf",
                "compte cpf",
                "compte formation",
                "mon compte formation",
                # Taxi: retiré - les candidats VTC/Uber mentionnent souvent "taxi" (erreur inscription, examen taxi/vtc)
                # Le mot "taxi" seul ne justifie pas un routage vers Contact
                "ambulance",
                "capacité de transport",
                "capacite de transport"
            ]

            # Vérifier sujet et dernier thread
            # IMPORTANT: Nettoyer le contenu des emails transférés/blockquotes
            # pour éviter les faux positifs (ex: footer CMA avec "TAXI")
            combined_content = ""
            if ticket.get("subject"):
                combined_content += ticket["subject"].lower() + " "
            if last_thread_content:
                # Nettoyer le contenu transféré avant vérification
                cleaned_content = BusinessRules.strip_forwarded_content(last_thread_content)
                combined_content += cleaned_content.lower()

            # Si demande autre service détectée → Contact (malgré deal 20€)
            matched_keyword = next((kw for kw in other_service_keywords if kw in combined_content), None)
            if matched_keyword:
                logger.info(f"🚦 Keyword autre service détecté dans contenu nettoyé: '{matched_keyword}' → Contact")
                return "Contact"

            evalbox = selected_deal.get("Evalbox", "")

            # Détecter si le candidat ENVOIE des documents (intention = TRANSMET_DOCUMENTS)
            # via mots-clés dans le sujet ou le contenu du thread
            #
            # LOGIQUE MÉTIER (modifiée 2026-01-31):
            # - Si Evalbox = "Refusé CMA" ET envoi de documents → Refus CMA (il sait, il corrige)
            # - Si Evalbox = "Refusé CMA" SANS envoi de documents → DOC (il ne sait pas encore, on l'informe)
            # - Si Evalbox OK ET envoi de documents → Refus CMA (gérer les uploads)
            # - Sinon → DOC
            #
            # Cela évite de router aveuglément vers Refus CMA quand le candidat
            # pose une question (statut, convocation, etc.) sans savoir que son dossier est refusé.

            has_document_keywords = False

            # Vérifier le sujet du ticket
            ticket_subject = ticket.get("subject", "")
            logger.info(f"🔍 Checking ticket subject for document keywords: '{ticket_subject}'")
            if ticket_subject and BusinessRules.is_document_submission(ticket_subject):
                logger.info(f"⚠️ Document keyword found in SUBJECT")
                has_document_keywords = True

            # Vérifier le contenu du dernier thread
            logger.info(f"🔍 Checking thread content for document keywords (first 200 chars): '{(last_thread_content or '')[:200]}'")
            if last_thread_content and BusinessRules.is_document_submission(last_thread_content):
                logger.info(f"⚠️ Document keyword found in THREAD CONTENT")
                has_document_keywords = True

            # Router vers Refus CMA SEULEMENT si:
            # 1. Envoi de documents détecté
            # 2. ET Date_Dossier_reçu est remplie (dossier déjà soumis)
            # Si Date_Dossier_reçu est vide → le candidat n'a pas encore soumis son dossier
            # → traiter dans DOC (questions sur les documents requis, envoi initial)
            if has_document_keywords:
                date_dossier_recu = selected_deal.get("Date_Dossier_re_u")
                if date_dossier_recu:
                    logger.info(f"🚨 Routing to Refus CMA due to document keywords (Evalbox: {evalbox}, Date_Dossier_reçu: {date_dossier_recu})")
                    return "Refus CMA"
                else:
                    logger.info(f"📋 Document keywords trouvés mais Date_Dossier_reçu vide → DOC (dossier pas encore soumis)")

            # Si Evalbox = Refusé CMA mais PAS d'envoi de documents → rester DOC
            # Le workflow informera le candidat du refus via le template
            is_refus_cma = evalbox in ["Refusé CMA", "Documents refusés", "Documents manquants"]
            if is_refus_cma:
                logger.info(f"📋 Evalbox={evalbox} mais pas d'envoi de documents → DOC (workflow informera le candidat)")
            else:
                logger.info(f"✅ No document keywords found - staying in DOC")

            # Sinon → DOC
            return "DOC"

        # Étape 5: Pas de deal 20€, chercher autre montant
        other_deals_won_or_pending = [
            d for d in all_deals
            if d.get("Amount") != 20 and d.get("Stage") in ["GAGNÉ", "EN ATTENTE"]
        ]

        if other_deals_won_or_pending:
            # Vérifier si le candidat ENVOIE des documents avant de router vers Contact
            # Un candidat formation payante qui envoie ses docs doit aller vers DOCS CAB
            ticket_subject = ticket.get("subject", "") if ticket else ""
            has_doc_keywords = False
            if ticket_subject and BusinessRules.is_document_submission(ticket_subject):
                has_doc_keywords = True
            if last_thread_content and BusinessRules.is_document_submission(last_thread_content):
                has_doc_keywords = True
            # "documents" dans le SUJET est un signal fiable d'envoi de docs
            # (retiré du body car trop générique, mais dans un sujet c'est explicite)
            if ticket_subject and "document" in ticket_subject.lower():
                has_doc_keywords = True

            if has_doc_keywords:
                logger.info(f"📄 Deal non-20€ + envoi documents → DOCS CAB")
                return "DOCS CAB"

            return "Contact"

        # Étape 6: Aucun deal pertinent trouvé
        return None

    @staticmethod
    def should_create_deal_for_ticket(ticket: Dict[str, Any]) -> bool:
        """
        Should we create a deal for this ticket?

        CUSTOMIZE THIS METHOD!

        Default: Only create deals for Sales department tickets
        """
        # Example rule: Create deals only for sales-related tickets
        department = ticket.get("departmentName", "")
        return department == "Sales"

    @staticmethod
    def get_deal_data_from_ticket(ticket: Dict[str, Any]) -> Dict[str, Any]:
        """
        What data should the deal have?

        CUSTOMIZE THIS METHOD!
        """
        contact = ticket.get("contact", {})

        return {
            "Deal_Name": f"Deal - {contact.get('name', 'Unknown')} - A-Level Selection",
            "Stage": "Qualification",
            "Amount": 500,
            "Lead_Source": "Support Ticket",
            "Description": f"Created from ticket: {ticket.get('subject', '')}",
            "Type": "New Business",
        }

    @staticmethod
    def should_link_ticket_to_deal(
        ticket: Dict[str, Any],
        deal: Dict[str, Any]
    ) -> bool:
        """
        Should we link this ticket to this deal?

        CUSTOMIZE THIS METHOD!

        Default: Don't link to closed deals
        """
        stage = deal.get("Stage", "")
        return stage not in ["Closed Won", "Closed Lost"]

    @staticmethod
    def get_preferred_linking_strategies() -> list:
        """
        Which strategies to use and in what order?

        CUSTOMIZE THIS LIST!
        """
        return [
            "custom_field",
            "contact_email",
            "contact_phone",
            "account",
        ]

    @staticmethod
    def get_deal_search_criteria_for_department(
        department: str,
        contact_email: str,
        ticket: Dict[str, Any]
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Get custom search criteria for specific departments.

        This allows department-specific logic for finding deals.

        Args:
            department: Department name
            contact_email: Contact email from ticket
            ticket: Full ticket data

        Returns:
            List of search criteria dictionaries in priority order, or None for default behavior.
            Each dict has:
            - criteria: Zoho CRM search query
            - description: What this searches for
            - max_results: How many to return

        Example return:
        [
            {"criteria": "(Stage:equals:Closed Won)", "description": "Won deals", "max_results": 1},
            {"criteria": "(Stage:equals:Pending)", "description": "Pending deals", "max_results": 1}
        ]
        """
        # ===== DÉPARTEMENTS PRIORITAIRES =====

        # DOC department: Specific Uber deal logic
        if department == "DOC":
            return [
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:Uber)and(Amount:equals:20)and(Stage:equals:Closed Won))",
                    "description": "Uber €20 deals - WON",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                },
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:Uber)and(Amount:equals:20)and(Stage:equals:Pending))",
                    "description": "Uber €20 deals - PENDING",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                },
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:Uber)and(Amount:equals:20)and(Stage:equals:Closed Lost))",
                    "description": "Uber €20 deals - LOST",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                }
            ]

        # DOCS CAB: Search for CAB-related deals
        elif department == "DOCS CAB":
            return [
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:CAB)and(Stage:not_equals:Closed Lost))",
                    "description": "Active CAB deals",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                },
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:Capacité)and(Stage:not_equals:Closed Lost))",
                    "description": "Active Capacité deals",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                }
            ]

        # Inscription CMA: Search for CMA registration deals
        elif department == "Inscription CMA":
            return [
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:CMA)and(Stage:equals:Qualification))",
                    "description": "CMA deals in qualification",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                },
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:CMA)and(Stage:not_equals:Closed Lost))",
                    "description": "Any active CMA deals",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                }
            ]

        # Refus CMA: Search for rejected CMA deals
        elif department == "Refus CMA":
            return [
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:CMA)and(Stage:equals:Closed Lost))",
                    "description": "Rejected CMA deals",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                }
            ]

        # Contact: General contact deals (any recent deal)
        elif department == "Contact":
            return [
                {
                    "criteria": f"((Email:equals:{contact_email})and(Stage:not_equals:Closed Lost)and(Stage:not_equals:Closed Won))",
                    "description": "Any open deals",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                },
                {
                    "criteria": f"((Email:equals:{contact_email}))",
                    "description": "Most recent deal (any stage)",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                }
            ]

        # Uber department: Similar to DOC but dedicated
        elif department == "Uber":
            return [
                {
                    "criteria": f"((Email:equals:{contact_email})and(Deal_Name:contains:Uber)and(Stage:not_equals:Closed Lost))",
                    "description": "Active Uber deals",
                    "max_results": 1,
                    "sort_by": "Modified_Time",
                    "sort_order": "desc"
                }
            ]

        # Default: Return None to use standard strategies
        return None

    @staticmethod
    def should_auto_process_ticket(ticket: Dict[str, Any]) -> bool:
        """
        Should this ticket be auto-processed or need manual review?

        CUSTOMIZE THIS METHOD!

        Default: Auto-process all tickets
        """
        # Don't auto-process urgent tickets
        if ticket.get("priority") == "Urgent":
            return False

        return True

    @staticmethod
    def get_department_from_deal(deal: Dict[str, Any]) -> Optional[str]:
        """
        Détermine le département basé sur le deal CRM (PRIORITAIRE sur keywords).

        WORKFLOW CORRECT:
        1. Deal Linking Agent trouve le deal
        2. Cette méthode détermine le département depuis le deal
        3. Si pas de match → fallback sur keywords (get_department_routing_rules)

        CUSTOMIZE THIS METHOD!

        Args:
            deal: Le deal CRM trouvé

        Returns:
            Nom du département ou None (fallback sur keywords)

        Logique:
        - Uber €20 deals → DOC
        - CAB/Capacité deals → DOCS CAB
        - CMA Closed Lost → Refus CMA
        - CMA autres stages → Inscription CMA
        - Deal trouvé sans règle spécifique → Contact
        """
        if not deal:
            return None

        deal_name = deal.get("Deal_Name", "").lower()
        stage = deal.get("Stage", "")
        amount = deal.get("Amount", 0)

        # ===== RÈGLES BASÉES SUR LE DEAL =====

        # Uber €20 deals → DOC (formation VTC)
        if "uber" in deal_name and amount == 20:
            return "DOC"

        # CAB / Capacité deals → DOCS CAB
        if "cab" in deal_name or "capacité" in deal_name or "capacite" in deal_name:
            return "DOCS CAB"

        # CMA deals : routing selon le stage
        if "cma" in deal_name:
            if stage == "Closed Lost":
                return "Refus CMA"  # Deal refusé
            elif stage in ["Qualification", "Needs Analysis", "Proposal"]:
                return "Inscription CMA"  # En cours d'inscription
            else:
                return "Contact"  # CMA mais stage inconnu

        # A-Level / Educational deals → DOC
        if "a-level" in deal_name or "formation" in deal_name or "vtc" in deal_name:
            return "DOC"

        # Deal trouvé mais pas de règle spécifique → Contact (département généraliste)
        return "Contact"

    @staticmethod
    def get_department_routing_rules() -> Dict[str, Any]:
        """
        Get department routing rules for the TicketDispatcherAgent.

        These rules are checked BEFORE AI analysis for faster routing.
        Rules are deterministic and keyword-based.

        CUSTOMIZE THIS METHOD!

        Returns:
            Dict mapping department names to routing rules.
            Each department can have:
            - keywords: List of keywords to match in subject/description
            - subject_patterns: List of regex patterns for subject
            - contact_domains: List of email domains

        Example:
        {
            "DOC": {
                "keywords": ["uber", "a-level", "student", "education"],
                "contact_domains": ["@university.edu"]
            },
            "Sales": {
                "keywords": ["pricing", "quote", "demo", "purchase", "buy"]
            }
        }
        """
        return {
            # ===== DÉPARTEMENTS PRIORITAIRES =====

            "DOC": {
                "keywords": [
                    # Mots-clés basés sur l'analyse de 100 tickets réels de Fouad (01/11/2025)
                    # Top occurrences dans les sujets de tickets
                    "examen",           # 42 occurrences - #1
                    "inscription",      # 22 occurrences - #2
                    "formation",        # 13 occurrences - #4
                    "convocation",      # 12 occurrences - #5
                    "dossier",          # 11 occurrences
                    "test",             # 10 occurrences
                    "rappel",           # 10 occurrences
                    "demande",          # 10 occurrences
                    "sélection",        # 9 occurrences
                    "admissibilité",    # 8 occurrences
                    "épreuve",          # 7 occurrences
                    "récapitulatif",    # 7 occurrences
                    "uber",             # Historique
                    "a-level",          # Historique
                    "vtc",              # VTC exams
                    "passage",          # Passage d'examen
                    "réussi",           # Test réussi
                    "théorique",        # Examen théorique
                    "pratique"          # Examen pratique
                ],
                "contact_domains": []
            },

            "DOCS CAB": {
                "keywords": [
                    "cab",
                    "capacité",
                    "capacite",
                    "candidat",
                    "candidate",
                    "dossier cab",
                    "dossier de candidat",
                    "certificat",
                    "attestation"
                ],
                "contact_domains": []
            },

            "Contact": {
                "keywords": [
                    "contact",
                    "renseignement",
                    "information",
                    "question",
                    "demande",
                    "inquiry",
                    "general",
                    "assistance",
                    "help"
                ],
                "contact_domains": []
            },

            "Inscription CMA": {
                "keywords": [
                    "inscription",
                    "inscription cma",
                    "cma",
                    "registration",
                    "enregistrement",
                    "register",
                    "s'inscrire",
                    "adhésion",
                    "membership"
                ],
                "contact_domains": []
            },

            "Refus CMA": {
                "keywords": [
                    "refus",
                    "refus cma",
                    "rejet",
                    "declined",
                    "rejection",
                    "denied",
                    "refuse",
                    "non accepté",
                    "non-accepté"
                ],
                "contact_domains": []
            },

            # ===== AUTRES DÉPARTEMENTS =====

            "FACTURATION": {
                "keywords": [
                    "facture",
                    "facturation",
                    "invoice",
                    "payment",
                    "paiement",
                    "billing",
                    "refund",
                    "remboursement"
                ],
                "contact_domains": []
            },

            "Comptabilité": {
                "keywords": [
                    "comptabilité",
                    "comptable",
                    "accounting",
                    "financial",
                    "finance",
                    "trésorerie"
                ],
                "contact_domains": []
            },

            "Pédagogie": {
                "keywords": [
                    "pédagogie",
                    "pedagogie",
                    "enseignement",
                    "teaching",
                    "formation",
                    "training",
                    "learning"
                ],
                "contact_domains": []
            },

            "Uber": {
                "keywords": [
                    "uber",
                    "uber eats",
                    "livraison",
                    "delivery",
                    "chauffeur",
                    "driver"
                ],
                "contact_domains": []
            }
        }


# For detailed examples, see business_rules.example.py
