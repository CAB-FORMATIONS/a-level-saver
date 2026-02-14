"""
CRMUpdateAgent - Agent spécialisé pour les mises à jour CRM CAB Formations.

Cet agent centralise TOUTE la logique de mise à jour du CRM:
1. Mapping des valeurs string → IDs CRM (lookup fields)
2. Respect des règles de blocage (VALIDE CMA + clôture passée)
3. Logging des updates dans les notes CRM
4. Validation des données avant mise à jour

UTILISATION:
    agent = CRMUpdateAgent()
    result = agent.process({
        'deal_id': '1234567890',
        'ai_updates': {'Date_examen_VTC': '2026-03-31', 'Session_choisie': 'Cours du soir'},
        'deal_data': {...},  # Données actuelles du deal
        'session_data': {...},  # Sessions proposées (de session_helper)
        'source': 'ticket_response',  # Source de la demande
        'auto_add_note': True
    })

RÈGLES CRITIQUES:
- Date_examen_VTC et Session sont des LOOKUP FIELDS → nécessitent IDs
- L'IA envoie 'Session_choisie' mais le champ CRM est 'Session' → mapping automatique
- Si Evalbox ∈ {"VALIDE CMA", "Convoc CMA reçue"} ET clôture passée → BLOCAGE
"""
import logging
import re
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

from .base_agent import BaseAgent
from src.zoho_client import ZohoCRMClient
from src.constants.evalbox import BLOCKING_MODIFICATION

logger = logging.getLogger(__name__)


class CRMUpdateAgent(BaseAgent):
    """Agent spécialisé pour les mises à jour CRM CAB Formations VTC."""

    SYSTEM_PROMPT = """Tu es un assistant spécialisé dans la mise à jour du CRM Zoho pour CAB Formations VTC.

Tu dois:
1. Valider les mises à jour demandées selon les règles métier
2. Convertir les valeurs string en IDs CRM quand nécessaire
3. Respecter les règles de blocage (pas de modification si VALIDE CMA + clôture passée)
4. Documenter chaque mise à jour dans les notes CRM

Règles critiques:
- Date_examen_VTC: LOOKUP vers module Dates_Examens_VTC_TAXI → nécessite ID session
- Session_choisie: LOOKUP vers module Sessions1 → nécessite ID session
- Evalbox: picklist simple, valeur texte acceptée
- Si Evalbox = "VALIDE CMA" ou "Convoc CMA reçue" ET clôture passée → NE PAS modifier Date_examen_VTC

Réponds toujours en JSON avec la structure:
{
    "updates_validated": [...],
    "updates_blocked": [...],
    "reason": "explication"
}
"""

    # Statuts qui bloquent la modification de Date_examen_VTC
    BLOCKING_EVALBOX_STATUSES = BLOCKING_MODIFICATION

    # Champs lookup (nécessitent ID, pas string)
    LOOKUP_FIELDS = ['Date_examen_VTC', 'Session_choisie', 'Session']

    def __init__(self, crm_client: Optional[ZohoCRMClient] = None):
        """
        Initialize CRMUpdateAgent.

        Args:
            crm_client: Optional ZohoCRMClient instance (creates new one if None)
        """
        super().__init__(
            name="CRMUpdateAgent",
            system_prompt=self.SYSTEM_PROMPT
        )
        self.crm_client = crm_client or ZohoCRMClient()

    def process(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Traite une demande de mise à jour CRM.

        Args:
            data: {
                'deal_id': str - ID du deal à mettre à jour
                'ai_updates': Dict - Mises à jour extraites par l'IA
                'deal_data': Dict - Données actuelles du deal
                'session_data': Dict - Sessions proposées (optionnel)
                'source': str - Source de la demande (ticket_response, examt3p_sync, etc.)
                'auto_add_note': bool - Ajouter une note CRM avec le détail (default: True)
                'dry_run': bool - Simulation sans écriture (default: False)
            }

        Returns:
            {
                'success': bool,
                'updates_applied': Dict,  # Mises à jour effectuées
                'updates_blocked': List,  # Mises à jour bloquées
                'note_added': bool,
                'errors': List[str]
            }
        """
        deal_id = data.get('deal_id')
        if not deal_id:
            return {
                'success': False,
                'updates_applied': {},
                'updates_blocked': [],
                'note_added': False,
                'errors': ['deal_id is required']
            }

        ai_updates = data.get('ai_updates', {})
        if not ai_updates:
            return {
                'success': True,
                'updates_applied': {},
                'updates_blocked': [],
                'note_added': False,
                'errors': []
            }

        deal_data = data.get('deal_data', {})
        session_data = data.get('session_data', {})
        source = data.get('source', 'unknown')
        auto_add_note = data.get('auto_add_note', True)
        dry_run = data.get('dry_run', False)

        logger.info(f"CRMUpdateAgent: Processing {len(ai_updates)} updates for deal {deal_id}")
        logger.info(f"  Source: {source}")
        logger.info(f"  Updates requested: {list(ai_updates.keys())}")

        result = {
            'success': True,
            'updates_applied': {},
            'updates_blocked': [],
            'note_added': False,
            'errors': []
        }

        # ================================================================
        # 1. VÉRIFIER LES RÈGLES DE BLOCAGE
        # ================================================================
        blocked_fields = []

        # Règle critique: Date_examen_VTC bloquée si VALIDE CMA + clôture passée
        if 'Date_examen_VTC' in ai_updates:
            can_modify, reason = self._can_modify_exam_date(deal_data)
            if not can_modify:
                blocked_fields.append({
                    'field': 'Date_examen_VTC',
                    'requested_value': ai_updates['Date_examen_VTC'],
                    'reason': reason
                })
                logger.warning(f"  🔒 BLOCAGE Date_examen_VTC: {reason}")

        result['updates_blocked'] = blocked_fields

        # ================================================================
        # 2. PRÉPARER LES MISES À JOUR (mapping string → ID)
        # ================================================================
        final_updates = {}

        for field, value in ai_updates.items():
            # Skip si bloqué
            if any(b['field'] == field for b in blocked_fields):
                continue

            # Mapping pour les champs lookup
            if field == 'Date_examen_VTC':
                # Si la valeur est déjà un ID (nombre long), l'utiliser directement
                # IDs Zoho CRM sont des nombres de 19 chiffres
                if str(value).isdigit() and len(str(value)) > 10:
                    logger.info(f"  📊 Date_examen_VTC: valeur déjà un ID → {value}")
                    final_updates[field] = value
                else:
                    # Sinon, mapper la date vers un ID
                    mapped_value = self._map_date_examen_vtc(value, deal_data)
                    if mapped_value:
                        final_updates[field] = mapped_value
                    else:
                        result['errors'].append(f"Failed to map Date_examen_VTC: {value}")

            elif field in ['Session_choisie', 'Session']:
                mapped_value = self._map_session(value, session_data, deal_data)
                if mapped_value:
                    # IMPORTANT: Le champ CRM s'appelle 'Session' (pas 'Session_choisie')
                    final_updates['Session'] = mapped_value
                else:
                    result['errors'].append(f"Failed to map {field}: {value}")

            else:
                # Champs texte simple - pas de mapping
                final_updates[field] = value

        if not final_updates:
            logger.info("  No valid updates after mapping")
            return result

        logger.info(f"  Final updates to apply: {list(final_updates.keys())}")

        # ================================================================
        # 3. APPLIQUER LES MISES À JOUR
        # ================================================================
        if dry_run:
            logger.info(f"  🔍 DRY RUN: Would update {list(final_updates.keys())}")
            result['updates_applied'] = final_updates
            return result

        try:
            self.crm_client.update_deal(deal_id, final_updates)
            result['updates_applied'] = final_updates
            logger.info(f"  ✅ CRM updated: {list(final_updates.keys())}")
        except Exception as e:
            logger.error(f"  ❌ CRM update failed: {e}")
            result['success'] = False
            result['errors'].append(str(e))
            return result

        # ================================================================
        # 4. AJOUTER NOTE CRM
        # ================================================================
        if auto_add_note and (final_updates or blocked_fields):
            try:
                note_content = self._generate_update_note(
                    source=source,
                    updates_applied=final_updates,
                    updates_blocked=blocked_fields,
                    ai_updates=ai_updates
                )
                self.crm_client.add_deal_note(
                    deal_id=deal_id,
                    note_title=f"CRM Update - {source}",
                    note_content=note_content
                )
                result['note_added'] = True
                logger.info("  ✅ Note CRM added")
            except Exception as e:
                logger.warning(f"  ⚠️ Failed to add CRM note: {e}")

        return result

    def _can_modify_exam_date(self, deal_data: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Vérifie si on peut modifier la date d'examen.

        Utilise la logique de examt3p_crm_sync.can_modify_exam_date()

        Returns:
            (can_modify: bool, reason: str)
        """
        from src.utils.examt3p_crm_sync import can_modify_exam_date

        evalbox_status = deal_data.get('Evalbox', '')

        # Récupérer la date de clôture
        date_cloture = None
        date_examen_vtc = deal_data.get('Date_examen_VTC')
        if date_examen_vtc and isinstance(date_examen_vtc, dict):
            date_cloture = date_examen_vtc.get('Date_Cloture_Inscription')

        return can_modify_exam_date(evalbox_status, date_cloture)

    def _map_date_examen_vtc(
        self,
        date_value: str,
        deal_data: Dict[str, Any]
    ) -> Optional[str]:
        """
        Convertit une date d'examen en ID de session CRM.

        Utilise find_exam_session_by_date_and_dept() de examt3p_crm_sync.py

        Args:
            date_value: Date au format "dd/mm/yyyy" ou "yyyy-mm-dd"
            deal_data: Données du deal (pour le département)

        Returns:
            ID de la session ou None
        """
        from src.utils.examt3p_crm_sync import find_exam_session_by_date_and_dept

        # Extraire le département
        departement = deal_data.get('CMA_de_depot', '')
        if departement:
            match = re.search(r'\b(\d{2,3})\b', str(departement))
            if match:
                departement = match.group(1)

        if not departement:
            logger.warning(f"  Cannot map Date_examen_VTC: department not found")
            return None

        # Rechercher la session
        session = find_exam_session_by_date_and_dept(
            self.crm_client, date_value, departement
        )

        if session and session.get('id'):
            # ================================================================
            # VÉRIFICATION CRITIQUE: Date de clôture passée ?
            # ================================================================
            date_cloture = session.get('Date_Cloture_Inscription')
            if date_cloture:
                try:
                    if isinstance(date_cloture, str):
                        cloture_date = datetime.strptime(date_cloture, "%Y-%m-%d")
                    else:
                        cloture_date = date_cloture

                    if cloture_date.date() < datetime.now().date():
                        logger.warning(f"  🚫 BLOCAGE: Date_examen_VTC {date_value} - clôture passée ({date_cloture})")
                        return None
                except (ValueError, TypeError) as e:
                    logger.warning(f"  ⚠️ Impossible de parser Date_Cloture_Inscription: {date_cloture} - {e}")

            logger.info(f"  📊 Date_examen_VTC: {date_value} → ID {session['id']}")
            return session['id']
        else:
            logger.warning(f"  Session not found: {date_value} / dept {departement}")
            return None

    def _map_session(
        self,
        session_value: str,
        session_data: Dict[str, Any],
        deal_data: Dict[str, Any]
    ) -> Optional[str]:
        """
        Convertit un nom de session en ID CRM.

        Cherche dans:
        1. Les sessions proposées par session_helper (proposed_options)
        2. Recherche directe dans le CRM si non trouvé

        Args:
            session_value: Nom ou description de la session (ou déjà un ID)
            session_data: Données de session_helper
            deal_data: Données du deal

        Returns:
            ID de la session ou None
        """
        # 0. Si la valeur est déjà un ID Zoho (numérique 19 chiffres), retourner directement
        if session_value and session_value.isdigit() and len(session_value) >= 15:
            logger.info(f"  📊 Session: valeur déjà un ID → {session_value}")
            return session_value

        # 1. Chercher dans les sessions proposées
        proposed_options = session_data.get('proposed_options', [])

        for option in proposed_options:
            for sess in option.get('sessions', []):
                sess_id = sess.get('id')
                if not sess_id:
                    continue

                sess_name = sess.get('Name', '')
                sess_debut = sess.get('Date_d_but', '')
                sess_fin = sess.get('Date_fin', '')
                sess_type = sess.get('session_type_label', '').lower()

                # Matching par nom exact
                if sess_name and sess_name.lower() in session_value.lower():
                    logger.info(f"  📊 Session: {session_value} → ID {sess_id} (name match)")
                    return sess_id

                # Matching par dates
                if sess_debut and sess_debut in session_value:
                    logger.info(f"  📊 Session: {session_value} → ID {sess_id} (date match)")
                    return sess_id

                # Matching par type (jour/soir)
                session_value_lower = session_value.lower()
                if 'soir' in session_value_lower and 'soir' in sess_type:
                    logger.info(f"  📊 Session: {session_value} → ID {sess_id} (type soir)")
                    return sess_id
                if 'jour' in session_value_lower and 'jour' in sess_type:
                    logger.info(f"  📊 Session: {session_value} → ID {sess_id} (type jour)")
                    return sess_id

        # 2. Si pas trouvé dans proposed_options, recherche CRM directe
        logger.info(f"  Session not in proposed_options, searching CRM...")
        session_found = self._search_session_in_crm(session_value)
        if session_found:
            return session_found

        logger.warning(f"  Session not found: {session_value}")
        return None

    def _search_session_in_crm(self, session_value: str) -> Optional[str]:
        """
        Recherche une session directement dans le CRM.

        Args:
            session_value: Nom ou description de la session

        Returns:
            ID de la session ou None
        """
        from config import settings

        try:
            # Extraire une date si présente (format dd/mm/yyyy)
            date_match = re.search(r'(\d{2}/\d{2}/\d{4})', session_value)

            if date_match:
                date_str = date_match.group(1)
                # Convertir en yyyy-mm-dd
                date_obj = datetime.strptime(date_str, "%d/%m/%Y")
                date_iso = date_obj.strftime("%Y-%m-%d")

                # Recherche par date de début
                url = f"{settings.zoho_crm_api_url}/Sessions1/search"
                criteria = f"(Date_d_but:equals:{date_iso})"
                params = {"criteria": criteria, "per_page": 10}

                response = self.crm_client._make_request("GET", url, params=params)
                sessions = response.get("data", [])

                # Filtrer par type si mentionné
                for sess in sessions:
                    sess_name = sess.get('Name', '').lower()
                    if 'soir' in session_value.lower() and 'cds' in sess_name:
                        logger.info(f"  📊 Session found in CRM: ID {sess['id']}")
                        return sess['id']
                    if 'jour' in session_value.lower() and 'cdj' in sess_name:
                        logger.info(f"  📊 Session found in CRM: ID {sess['id']}")
                        return sess['id']

                # Si pas de filtre type, prendre le premier
                if sessions:
                    logger.info(f"  📊 Session found in CRM: ID {sessions[0]['id']}")
                    return sessions[0]['id']

        except Exception as e:
            logger.warning(f"  Error searching session in CRM: {e}")

        return None

    def _generate_update_note(
        self,
        source: str,
        updates_applied: Dict[str, Any],
        updates_blocked: List[Dict],
        ai_updates: Dict[str, Any]
    ) -> str:
        """
        Génère le contenu de la note CRM pour documenter les mises à jour.

        Args:
            source: Source de la demande
            updates_applied: Mises à jour effectuées
            updates_blocked: Mises à jour bloquées
            ai_updates: Mises à jour originales demandées par l'IA

        Returns:
            Contenu de la note
        """
        lines = [
            f"📊 CRM UPDATE - {source.upper()}",
            f"Date: {datetime.now().strftime('%d/%m/%Y %H:%M')}",
            ""
        ]

        if updates_applied:
            lines.append("✅ MISES À JOUR APPLIQUÉES:")
            for field, value in updates_applied.items():
                # Masquer les IDs complexes
                if isinstance(value, str) and len(value) > 15 and value.isdigit():
                    display_value = f"ID:{value[:8]}..."
                else:
                    display_value = str(value)
                original = ai_updates.get(field, 'N/A')
                lines.append(f"  • {field}: {original} → {display_value}")
            lines.append("")

        if updates_blocked:
            lines.append("🔒 MISES À JOUR BLOQUÉES (règles métier):")
            for blocked in updates_blocked:
                lines.append(f"  • {blocked['field']}: {blocked['requested_value']}")
                lines.append(f"    Raison: {blocked['reason']}")
            lines.append("")

        return "\n".join(lines)

    def update_from_ticket_response(
        self,
        deal_id: str,
        ai_updates: Dict[str, Any],
        deal_data: Dict[str, Any],
        session_data: Optional[Dict] = None,
        ticket_id: Optional[str] = None,
        auto_add_note: bool = False
    ) -> Dict[str, Any]:
        """
        Méthode simplifiée pour les mises à jour depuis une réponse ticket.

        Args:
            deal_id: ID du deal
            ai_updates: Mises à jour extraites par ResponseGeneratorAgent
            deal_data: Données actuelles du deal
            session_data: Sessions proposées
            ticket_id: ID du ticket (pour la note)
            auto_add_note: Si True, ajoute une note CRM (défaut: False car note consolidée dans workflow)

        Returns:
            Résultat du process()
        """
        source = f"ticket_{ticket_id}" if ticket_id else "ticket_response"

        return self.process({
            'deal_id': deal_id,
            'ai_updates': ai_updates,
            'deal_data': deal_data,
            'session_data': session_data or {},
            'source': source,
            'auto_add_note': auto_add_note
        })

    def update_from_examt3p_sync(
        self,
        deal_id: str,
        sync_updates: Dict[str, Any],
        deal_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Méthode simplifiée pour les mises à jour depuis la sync ExamT3P.

        NOTE: Pour la sync ExamT3P, utiliser sync_examt3p_to_crm() directement
        qui gère déjà tous les cas. Cette méthode est fournie pour cohérence.

        Args:
            deal_id: ID du deal
            sync_updates: Mises à jour de la sync ExamT3P
            deal_data: Données actuelles du deal

        Returns:
            Résultat du process()
        """
        return self.process({
            'deal_id': deal_id,
            'ai_updates': sync_updates,
            'deal_data': deal_data,
            'source': 'examt3p_sync',
            'auto_add_note': True
        })

    def close(self):
        """Clean up resources."""
        if hasattr(self, 'crm_client'):
            self.crm_client.close()
