"""Deterministic B2B response builder for Relations entreprises drafts."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any


INTENT_LABELS = {
    "DEMANDE_DEVIS_FORMATION": "votre demande de devis",
    "DEMANDE_DISPONIBILITE_SESSION": "votre demande de disponibilites",
    "INSCRIPTION_CANDIDATS": "votre demande d'inscription",
    "COMMANDE_FORMALOGISTICS": "votre commande Formalogistics",
    "ANNULATION_REPORT_ABSENCE": "votre demande de modification",
    "CONVENTION_CONTRAT_DOSSIER": "votre dossier de formation",
    "BON_DE_COMMANDE": "votre bon de commande",
    "CONVOCATION_CONFIRMATION": "votre demande de convocation",
    "ATTESTATION_FIN_FORMATION": "votre demande de documents de fin de formation",
    "DOCUMENTS_SIGNATURES_MANQUANTS": "les documents ou signatures manquants",
    "FACTURE_FINANCEMENT_PEC": "votre demande administrative/financiere",
    "BILAN_FORMATEUR": "le bilan formateur",
    "PROSPECTION_PARTENARIAT": "notre echange commercial",
    "CV_PROFILS_INTERVENANTS": "le CV transmis",
}

MISSING_FIELD_LABELS = {
    "formation_type": "la formation souhaitee",
    "formation": "la formation souhaitee",
    "centre": "le centre souhaite",
    "start_date": "la date ou periode souhaitee",
    "end_date": "la date ou periode souhaitee",
    "dates": "la date ou periode souhaitee",
    "nb_candidates": "le nombre de candidats",
    "categories": "les categories concernees",
    "type_ir": "initial ou recyclage",
    "nombre_jours_souhaites": "la duree souhaitee en jours",
}


def build_relations_response(
    triage: dict[str, Any],
    crm_context: dict[str, Any],
    planbot_result: dict[str, Any] | None,
) -> str:
    """Build a safe HTML draft for Relations entreprises."""
    intent = triage.get("intent") or "AUTRE_A_QUALIFIER"
    extracted = triage.get("extracted") or {}
    missing_fields = _normalize_missing_fields(triage.get("missing_fields") or [])
    account_name = crm_context.get("account_name") or extracted.get("company") or ""

    lines = ["Bonjour,<br>", "<br>"]
    subject_label = INTENT_LABELS.get(intent, "votre demande")
    if account_name:
        lines.append(f"Merci pour {subject_label} concernant {escape(str(account_name))}.<br>")
    else:
        lines.append(f"Merci pour {subject_label}.<br>")
    lines.append("<br>")

    if intent in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
        lines.extend(_build_training_request_section(extracted, missing_fields, planbot_result))
        lines.extend(_build_quote_block())
    elif intent == "ANNULATION_REPORT_ABSENCE":
        lines.extend(_paragraph(
            "Nous avons bien pris note de votre demande d'annulation, de report ou d'absence. "
            "Nous allons mettre a jour le suivi de la session concernee et reviendrons vers vous si un element complementaire est necessaire pour le repositionnement ou la regularisation du dossier."
        ))
    elif intent == "CONVENTION_CONTRAT_DOSSIER":
        lines.extend(_paragraph(
            "Nous avons bien recu les elements relatifs a la convention, au contrat ou au dossier de formation. "
            "Ils vont etre rapproches de la session concernee et utilises pour le suivi administratif du dossier."
        ))
    elif intent == "BON_DE_COMMANDE":
        lines.extend(_paragraph(
            "Nous avons bien recu votre bon de commande. Nous allons le rapprocher du ou des dossiers concernes et finaliser les elements administratifs correspondants."
        ))
    elif intent == "CONVOCATION_CONFIRMATION":
        lines.extend(_paragraph(
            "Nous allons verifier les convocations ou confirmations de participation liees a la session concernee et vous confirmer le statut."
        ))
    elif intent == "ATTESTATION_FIN_FORMATION":
        lines.extend(_paragraph(
            "Nous allons verifier les documents de fin de formation disponibles pour la session concernee et vous les transmettre ou vous confirmer le delai de mise a disposition."
        ))
    elif intent == "DOCUMENTS_SIGNATURES_MANQUANTS":
        lines.extend(_paragraph(
            "Nous allons verifier les documents ou signatures manquants et vous indiquer les elements restant a regulariser."
        ))
    elif intent == "FACTURE_FINANCEMENT_PEC":
        lines.extend(_paragraph(
            "Nous allons verifier les elements administratifs et financiers lies a votre demande. Si besoin, le service concerne reviendra vers vous avec les informations complementaires."
        ))
    elif intent == "BILAN_FORMATEUR":
        lines.extend(_paragraph(
            "Nous avons bien recu votre demande relative au bilan formateur. Nous la transmettons a l'intervenant concerne afin que les elements attendus soient completes et retournes dans les meilleurs delais."
        ))
    elif intent == "CV_PROFILS_INTERVENANTS":
        lines.extend(_paragraph(
            "Nous avons bien recu le CV mis a jour. Nous allons l'ajouter au dossier de l'intervenant concerne et le prendre en compte pour les prochaines validations administratives ou pedagogiques."
        ))
    else:
        lines.extend(_paragraph(
            "Nous allons verifier les elements de votre demande et revenir vers vous avec une reponse complete."
        ))

    lines.append("<br>")
    lines.append("Cordialement,<br>")
    lines.append("L'equipe Relations entreprises CAB Formations")
    return "".join(lines)


def build_internal_note(
    ticket_id: str,
    triage: dict[str, Any],
    crm_context: dict[str, Any],
    planbot_result: dict[str, Any] | None,
    validation: dict[str, Any],
) -> str:
    planbot_status = _planbot_status(planbot_result)
    meta = (
        f"[REL_META] ticket={ticket_id} | intent={triage.get('intent')} | "
        f"crm={crm_context.get('classification')} | planbot={planbot_status} | "
        f"confidence={triage.get('confidence')}"
    )
    missing = ", ".join(triage.get("missing_fields") or []) or "aucun"
    return "\n".join([
        meta,
        "",
        "Brouillon Relations entreprises genere automatiquement.",
        f"Action triage: {triage.get('action')}",
        f"Raison: {triage.get('reason')}",
        f"Champs manquants: {missing}",
        "Prix: XXX a completer manuellement tant que la grille tarifaire CRM n'est pas active.",
        f"Validation: {'OK' if validation.get('valid') else 'A VERIFIER'}",
        *(f"- {err}" for err in validation.get("errors", [])),
    ])


def _build_training_request_section(extracted: dict[str, Any], missing_fields: list[str], planbot_result: dict[str, Any] | None) -> list[str]:
    lines = []
    recap = _request_recap(extracted)
    if recap:
        lines.append("<b>Recapitulatif de la demande</b><br>")
        for item in recap:
            lines.append(f"- {escape(item)}<br>")
        lines.append("<br>")

    if missing_fields:
        lines.append("Pour vous proposer une session fiable et finaliser le devis, pouvez-vous nous confirmer les elements suivants :<br>")
        for field in missing_fields:
            lines.append(f"- {escape(MISSING_FIELD_LABELS.get(field, field))}<br>")
        lines.append("<br>")
        lines.append("Des reception de ces informations, nous pourrons verifier les disponibilites et completer le devis.<br>")
        lines.append("<br>")
        return lines

    session_lines = _format_planbot_sessions(planbot_result)
    if session_lines:
        lines.append("<b>Sessions identifiees</b><br>")
        lines.extend(session_lines)
        lines.append("<br>")
        lines.append("Merci de nous confirmer la session que vous souhaitez retenir afin que nous puissions finaliser le dossier.<br>")
        lines.append("<br>")
    else:
        lines.append("Nous devons encore verifier les disponibilites avant de vous confirmer une proposition de session.<br><br>")
    return lines


def _build_quote_block() -> list[str]:
    return [
        "<b>Devis a completer</b><br>",
        "- Formation : XXX a completer<br>",
        "- Nombre de candidats : XXX a completer<br>",
        "- Montant HT : XXX EUR a completer<br>",
        "- TVA : XXX a completer<br>",
        "- Montant TTC : XXX EUR a completer<br>",
        "- Validite du devis : XXX jours a completer<br>",
        "- Modalites : XXX a completer<br>",
        "<br>",
    ]


def _format_planbot_sessions(planbot_result: dict[str, Any] | None) -> list[str]:
    if not planbot_result:
        return []

    blocks = []
    direct = planbot_result.get("direct") if isinstance(planbot_result, dict) else None
    if direct and _result_has_availability(direct):
        blocks.extend(_format_days(direct.get("jours") or [], prefix="Option disponible"))

    if not blocks:
        alt_dates = planbot_result.get("alternative_dates") if isinstance(planbot_result, dict) else None
        if alt_dates and _result_has_availability(alt_dates):
            blocks.append("Aucune disponibilite directe complete n'a ete confirmee sur la periode demandee. Alternatives meme centre :<br>")
            blocks.extend(_format_days(alt_dates.get("jours") or [], prefix="Alternative"))

    if not blocks:
        alt_centres = planbot_result.get("alternative_centres") if isinstance(planbot_result, dict) else None
        if alt_centres and _result_has_availability(alt_centres):
            blocks.append("Aucune disponibilite directe complete n'a ete confirmee sur le centre demande. Alternatives proches :<br>")
            blocks.extend(_format_days(alt_centres.get("jours") or [], prefix="Alternative"))

    optimizer = planbot_result.get("optimizer") if isinstance(planbot_result, dict) else None
    if not blocks and optimizer and optimizer.get("best_plan"):
        blocks.append(
            "Une possibilite de reajustement planning semble exister, sous reserve de validation interne. "
            "Nous revenons vers vous apres verification definitive.<br>"
        )

    return blocks[:12]


def _format_days(days: list[dict[str, Any]], prefix: str) -> list[str]:
    lines = []
    for day in days[:6]:
        if not isinstance(day, dict):
            continue
        date = _format_date(day.get("date") or "")
        jour = day.get("jour") or ""
        available_parts = []
        for label, key in [
            ("formation theorie", "formation_theorie"),
            ("formation pratique", "formation_pratique"),
            ("test theorie", "test_theorie"),
            ("test pratique", "test_pratique"),
        ]:
            if day.get(key):
                available_parts.append(label)
        if date and available_parts:
            lines.append(f"- {escape(prefix)} : {escape(str(jour))} {escape(date)} ({escape(', '.join(available_parts))})<br>")
    return lines


def _result_has_availability(result: dict[str, Any]) -> bool:
    verdict = str(result.get("verdict") or "")
    return bool(
        verdict.startswith("dispo")
        or result.get("coverage_complete") is True
        or any(day.get("formation_theorie") or day.get("formation_pratique") or day.get("test_theorie") or day.get("test_pratique") for day in result.get("jours") or [])
    )


def _request_recap(extracted: dict[str, Any]) -> list[str]:
    items = []
    if extracted.get("formation_type"):
        items.append(f"Formation : {extracted['formation_type']}")
    if extracted.get("centre"):
        items.append(f"Centre : {extracted['centre']}")
    if extracted.get("start_date") or extracted.get("end_date"):
        items.append(f"Periode : {_format_date(extracted.get('start_date'))} - {_format_date(extracted.get('end_date'))}")
    if extracted.get("nb_candidates"):
        items.append(f"Nombre de candidats : {extracted['nb_candidates']}")
    categories = extracted.get("categories") or []
    if categories:
        items.append(f"Categories : {', '.join(str(cat) for cat in categories)}")
    if extracted.get("type_ir"):
        items.append(f"Type : {extracted['type_ir']}")
    return items


def _normalize_missing_fields(fields: list[str]) -> list[str]:
    normalized = []
    for field in fields:
        value = str(field or "").strip()
        if not value:
            continue
        if value in {"start_date", "end_date"} and "dates" not in normalized:
            normalized.append("dates")
        elif value not in normalized:
            normalized.append(value)
    return normalized


def _paragraph(text: str) -> list[str]:
    return [escape(text), "<br>", "<br>"]


def _format_date(value: Any) -> str:
    if not value:
        return ""
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return text


def _planbot_status(planbot_result: dict[str, Any] | None) -> str:
    if not planbot_result:
        return "not_called"
    return str(planbot_result.get("recommended_status") or planbot_result.get("status") or "unknown")
