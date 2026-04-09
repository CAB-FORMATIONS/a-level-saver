"""
Agent pour extraire les données depuis exament3p.fr via HTTP (httpx).

Utilise exament3p_playwright.py (module HTTP, ex-Playwright) pour l'extraction.
"""
import logging
import sys
import os
from typing import Dict, Any, Optional

# Ajouter le chemin utils au PYTHONPATH
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'utils'))

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class ExamT3PAgent(BaseAgent):
    """Agent pour scraper les données depuis exament3p.fr."""

    def __init__(self, max_retries: int = 3):
        """
        Initialize ExamT3P agent.

        Args:
            max_retries: Nombre maximum de tentatives (default: 3)
        """
        super().__init__(
            name="ExamT3PAgent",
            system_prompt="Agent for scraping exament3p.fr candidate data"
        )
        self.max_retries = max_retries

    def extract_data(self, identifiant: str, password: str) -> Dict[str, Any]:
        """
        Extrait toutes les données du compte exament3p.

        Args:
            identifiant: IDENTIFIANT_EVALBOX (email)
            password: MDP_EVALBOX

        Returns:
            Dict avec toutes les données extraites
        """
        try:
            # Import dynamique du module d'extraction HTTP
            from exament3p_playwright import extract_exament3p_sync

            logger.info(f"Extracting data from exament3p.fr for {identifiant}")

            # Extraction avec retry automatique
            data = extract_exament3p_sync(identifiant, password, max_retries=self.max_retries)

            return data

        except ImportError as e:
            logger.error(f"Module exament3p_playwright not found: {e}")
            logger.error("Please place exament3p_playwright.py in src/utils/")
            return {
                "success": False,
                "error": "Module exament3p_playwright not found",
                "extraction_requise": True
            }
        except Exception as e:
            logger.error(f"ExamT3P extraction failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "extraction_requise": True
            }

    def process(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process method requis par BaseAgent.

        Args:
            data: Dict contenant username, password

        Returns:
            Dict avec les données extraites
        """
        username = data.get("username") or data.get("identifiant")
        password = data.get("password")

        if not all([username, password]):
            return {
                "success": False,
                "error": "Missing credentials (username/password)"
            }

        # Extract data
        result = self.extract_data(username, password)

        # Ajouter success flag
        if not result.get("error"):
            result["success"] = True
        else:
            result["success"] = False

        return result

    def get_summary(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Génère un résumé des informations importantes.

        Args:
            data: Données extraites

        Returns:
            Résumé structuré
        """
        if not data or data.get("extraction_requise"):
            return {
                "available": False,
                "reason": "Data extraction required or failed"
            }

        summary = {
            "available": True,
            "num_dossier": data.get("num_dossier"),
            "nom_candidat": data.get("nom_candidat"),
            "statut_dossier": data.get("statut_dossier"),
            "date_examen": data.get("date_examen"),
            "convocation": data.get("convocation", "EN ATTENTE"),
            "statut_documents": data.get("statut_documents"),
            "documents_valides": data.get("documents_valides", "0/0"),
            "paiement_cma": data.get("paiement_cma", {}),
            "actions_requises": data.get("actions_requises", []),
            "document_problematique": data.get("document_problematique"),
            "action_candidat_requise": data.get("action_candidat_requise", False)
        }

        # Identifier les blocages
        blocages = []

        if data.get("statut_dossier") == "En cours de composition":
            blocages.append("documents_manquants")

        if data.get("statut_dossier") == "En attente du paiement":
            blocages.append("paiement_non_effectue")

        if data.get("statut_dossier") == "Incomplet":
            blocages.append("pieces_refusees")

        if data.get("action_candidat_requise"):
            blocages.append("action_candidat_requise")

        summary["blocages"] = blocages
        summary["dossier_complet"] = data.get("statut_dossier") == "Valide"

        return summary
