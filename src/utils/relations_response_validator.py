"""Safety validation for Relations entreprises drafts."""

from __future__ import annotations

import re
from typing import Any


FORBIDDEN_TERMS = [
    "PlanBot",
    "Zoho rules",
    "simulation read-only",
    "read-only",
    "UT",
    "API",
    "tool",
    "deal_id",
    "ticket_id",
]


def validate_relations_response(
    response_html: str,
    triage: dict[str, Any],
    planbot_result: dict[str, Any] | None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    text = response_html or ""
    lower = text.lower()

    for term in FORBIDDEN_TERMS:
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        if re.search(pattern, lower):
            errors.append(f"Terme interne interdit dans le draft: {term}")

    amounts = re.findall(r"(?<!X)\b\d+[\s\u00a0]*(?:€|eur|euros?)\b", text, flags=re.IGNORECASE)
    if amounts:
        errors.append(f"Montant chiffre detecte hors placeholder XXX: {', '.join(amounts)}")

    intent = triage.get("intent")
    if intent in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
        if "XXX" not in text:
            errors.append("Bloc devis avec placeholders XXX absent")
        if _should_have_planbot(triage) and not planbot_result:
            warnings.append("PlanBot non appele malgre donnees de disponibilite presentes")

    if any(word in lower for word in ["inscription confirmee", "inscription confirmée", "place reservee", "place réservée"]):
        errors.append("Le draft confirme une inscription/place reservee alors que le workflow ne fait que proposer")

    if "cordialement" not in lower and "relations entreprises" not in lower:
        errors.append("Signature Relations entreprises absente")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def _should_have_planbot(triage: dict[str, Any]) -> bool:
    extracted = triage.get("extracted") or {}
    return all([
        extracted.get("formation_type"),
        extracted.get("centre"),
        extracted.get("start_date"),
        extracted.get("end_date"),
        extracted.get("nb_candidates"),
    ])
