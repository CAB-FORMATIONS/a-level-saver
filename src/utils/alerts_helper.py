"""
Helper pour gérer les alertes temporaires.

Les alertes sont stockées dans alerts/active_alerts.yaml et permettent
d'informer l'agent rédacteur de bugs/situations temporaires à prendre
en compte dans les réponses aux candidats.

MODES DE DÉCLENCHEMENT:
1. Par statut Evalbox (applies_to.evalbox)
2. Par mots-clés dans le message du candidat (trigger_keywords)
"""
import logging
import yaml
from datetime import datetime, date
from pathlib import Path

from src.utils.date_utils import parse_date_flexible
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

# Chemin vers le fichier d'alertes
ALERTS_FILE = Path(__file__).parent.parent.parent / "alerts" / "active_alerts.yaml"


def load_alerts() -> List[Dict[str, Any]]:
    """
    Charge toutes les alertes depuis le fichier YAML.

    Returns:
        Liste des alertes (actives et inactives)
    """
    try:
        if not ALERTS_FILE.exists():
            logger.warning(f"Fichier d'alertes non trouvé: {ALERTS_FILE}")
            return []

        with open(ALERTS_FILE, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        return data.get('alerts', []) if data else []

    except Exception as e:
        logger.error(f"Erreur chargement alertes: {e}")
        return []


def check_trigger_keywords(message: str, keywords: List[str]) -> bool:
    """
    Vérifie si le message contient un des mots-clés de déclenchement.

    Args:
        message: Message du candidat (lowercase)
        keywords: Liste de mots-clés ou phrases à détecter

    Returns:
        True si au moins un mot-clé est trouvé
    """
    if not message or not keywords:
        return False

    message_lower = message.lower()

    for keyword in keywords:
        if keyword.lower() in message_lower:
            return True

    return False


def get_active_alerts(
    evalbox_status: Optional[str] = None,
    department: Optional[str] = None,
    customer_message: Optional[str] = None,
    reference_date: Optional[date] = None
) -> List[Dict[str, Any]]:
    """
    Récupère les alertes actives et applicables au contexte.

    Une alerte est déclenchée si:
    - Elle est active ET dans la période de validité
    - ET (evalbox_status correspond OU trigger_keywords trouvés dans le message)

    Args:
        evalbox_status: Statut Evalbox du candidat (pour filtrage)
        department: Département du candidat (pour filtrage)
        customer_message: Message du candidat (pour détection par mots-clés)
        reference_date: Date de référence (défaut: aujourd'hui)

    Returns:
        Liste des alertes actives et applicables
    """
    if reference_date is None:
        reference_date = date.today()

    all_alerts = load_alerts()
    active_alerts = []

    for alert in all_alerts:
        # Vérifier si active
        if not alert.get('active', True):
            continue

        # Vérifier date de début
        start_date_str = alert.get('start_date')
        if start_date_str:
            start_date = parse_date_flexible(start_date_str, f"alert_{alert.get('id')}_start_date")
            if start_date:
                if reference_date < start_date:
                    continue
            else:
                logger.warning(f"Format date invalide pour alerte {alert.get('id')}: {start_date_str}")

        # Vérifier date de fin
        end_date_str = alert.get('end_date')
        if end_date_str:
            end_date = parse_date_flexible(end_date_str, f"alert_{alert.get('id')}_end_date")
            if end_date:
                if reference_date > end_date:
                    continue
            else:
                logger.warning(f"Format date invalide pour alerte {alert.get('id')}: {end_date_str}")

        # === LOGIQUE DE DÉCLENCHEMENT ===
        # L'alerte est déclenchée si:
        # 1. Evalbox correspond (si applies_to.evalbox défini)
        # 2. OU mots-clés trouvés dans le message (si trigger_keywords défini)

        applies_to = alert.get('applies_to', {})
        trigger_keywords = alert.get('trigger_keywords', [])

        # Mode 1: Déclenchement par Evalbox
        evalbox_match = False
        evalbox_filter = applies_to.get('evalbox', [])
        if evalbox_filter:
            if evalbox_status and evalbox_status in evalbox_filter:
                evalbox_match = True
        # Note: Si pas de filtre evalbox, evalbox_match reste False
        # L'alerte ne se déclenche que par mots-clés dans ce cas

        # Mode 2: Déclenchement par mots-clés dans le message
        keyword_match = False
        if trigger_keywords and customer_message:
            keyword_match = check_trigger_keywords(customer_message, trigger_keywords)
            if keyword_match:
                logger.info(f"📢 Alerte '{alert.get('id')}' déclenchée par mot-clé dans le message")

        # Filtre département (si défini, doit correspondre)
        department_ok = True
        if department and applies_to.get('departments'):
            if department not in applies_to['departments']:
                department_ok = False

        # L'alerte est ajoutée si:
        # - (Evalbox correspond OU mot-clé trouvé) ET département OK
        if (evalbox_match or keyword_match) and department_ok:
            # Marquer comment l'alerte a été déclenchée
            alert_copy = alert.copy()
            alert_copy['_triggered_by'] = 'keyword' if keyword_match else 'evalbox'
            active_alerts.append(alert_copy)

    if active_alerts:
        logger.info(f"📢 {len(active_alerts)} alerte(s) active(s) trouvée(s)")
    return active_alerts


def format_alerts_for_prompt(alerts: List[Dict[str, Any]]) -> str:
    """
    Formate les alertes pour inclusion dans le prompt de l'agent rédacteur.

    Args:
        alerts: Liste des alertes actives

    Returns:
        Texte formaté pour le prompt
    """
    if not alerts:
        return ""

    lines = [
        "",
        "=" * 60,
        "🚨 ALERTES TEMPORAIRES - À PRENDRE EN COMPTE ABSOLUMENT",
        "=" * 60,
    ]

    for alert in alerts:
        lines.append("")
        triggered_by = alert.get('_triggered_by', 'evalbox')
        trigger_info = " (détecté dans le message)" if triggered_by == 'keyword' else ""
        lines.append(f"📌 {alert.get('title', 'Alerte')}{trigger_info}")
        lines.append("-" * 40)

        context = alert.get('context', '').strip()
        if context:
            lines.append(f"Contexte: {context}")

        instruction = alert.get('instruction', '').strip()
        if instruction:
            lines.append("")
            lines.append(f"⚠️ INSTRUCTION OBLIGATOIRE: {instruction}")

        lines.append("")

    lines.append("=" * 60)

    return "\n".join(lines)


def get_alerts_for_response(
    deal_data: Dict[str, Any] = None,
    examt3p_data: Dict[str, Any] = None,
    customer_message: str = None,
    threads: List[Dict] = None
) -> str:
    """
    Fonction simplifiée pour récupérer les alertes formatées pour une réponse.

    Args:
        deal_data: Données du deal CRM
        examt3p_data: Données ExamT3P
        customer_message: Message du candidat (pour détection par mots-clés)
        threads: Threads du ticket (alternative pour extraire le message)

    Returns:
        Texte formaté des alertes pour le prompt, ou chaîne vide si aucune
    """
    evalbox_status = None
    department = None

    if deal_data:
        evalbox_status = deal_data.get('Evalbox')
        # Extraire département de CMA_de_depot
        cma = deal_data.get('CMA_de_depot', '')
        if cma:
            import re
            match = re.search(r'\b(\d{2,3})\b', str(cma))
            if match:
                department = match.group(1)

    if examt3p_data and not department:
        department = examt3p_data.get('departement')

    # Extraire le message du candidat des threads si pas fourni
    if not customer_message and threads:
        for thread in threads:
            if thread.get('direction') == 'in':
                customer_message = thread.get('content', '') or thread.get('plainText', '') or ''
                break

    alerts = get_active_alerts(
        evalbox_status=evalbox_status,
        department=department,
        customer_message=customer_message
    )

    return format_alerts_for_prompt(alerts)
