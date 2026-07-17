"""Deterministic B2B response builder for Relations entreprises drafts."""

from __future__ import annotations

import re
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
    "year": "l'annee concernee",
}


def build_relations_response(
    triage: dict[str, Any],
    crm_context: dict[str, Any],
    planbot_result: dict[str, Any] | None,
    attachments: dict[str, Any] | None = None,
) -> str:
    """Build a safe HTML draft for Relations entreprises."""
    intent = triage.get("intent") or "AUTRE_A_QUALIFIER"
    request_mode = triage.get("request_mode") or "new_request"
    extracted = triage.get("extracted") or {}
    missing_fields = _normalize_missing_fields(triage.get("missing_fields") or [])
    has_attachments = bool((attachments or {}).get("has_attachments"))

    lines = ["Bonjour,<br>", "<br>"]
    subject_label = INTENT_LABELS.get(intent, "votre demande")
    if request_mode == "acknowledgement":
        lines.append("Merci pour votre retour.<br>")
    elif request_mode == "document_submission":
        prefix = "votre envoi" if has_attachments else "votre message"
        lines.append(f"Merci pour {prefix} concernant {subject_label}.<br>")
    else:
        lines.append(f"Merci pour {subject_label}.<br>")
    lines.append("<br>")

    if request_mode == "acknowledgement":
        lines.extend(_paragraph("Votre message a bien ete pris en compte."))
    elif intent in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
        lines.extend(_build_training_request_section(
            intent,
            extracted,
            missing_fields,
            planbot_result,
            str(triage.get("session_operation") or ""),
        ))
    elif intent == "ANNULATION_REPORT_ABSENCE":
        lines.extend(_paragraph(
            "Nous accusons reception de votre demande d'annulation, de report ou d'absence. "
            "Son traitement doit etre verifie sur la session concernee avant confirmation."
        ))
    elif intent == "CONVENTION_CONTRAT_DOSSIER":
        receipt = "des pieces jointes" if has_attachments else "de votre message"
        lines.extend(_paragraph(f"Nous accusons reception {receipt} concernant le dossier de formation. Les elements restent a verifier."))
    elif intent == "BON_DE_COMMANDE":
        receipt = "du document joint" if has_attachments else "de votre message concernant le bon de commande"
        lines.extend(_paragraph(f"Nous accusons reception {receipt}. Le contenu reste a verifier avant traitement administratif."))
    elif intent == "CONVOCATION_CONFIRMATION":
        lines.extend(_paragraph(
            "Votre demande de convocation ou de confirmation de participation necessite une verification de la session concernee."
        ))
    elif intent == "ATTESTATION_FIN_FORMATION":
        lines.extend(_paragraph(
            "Votre demande de document de fin de formation necessite une verification de la session et des documents disponibles."
        ))
    elif intent == "DOCUMENTS_SIGNATURES_MANQUANTS":
        lines.extend(_paragraph(
            "Votre message concernant les documents ou signatures manquants a bien ete recu. Les elements restant a regulariser doivent etre verifies."
        ))
    elif intent == "FACTURE_FINANCEMENT_PEC":
        lines.extend(_paragraph(
            "Votre demande administrative ou financiere doit etre verifiee par le service concerne avant reponse."
        ))
    elif intent == "BILAN_FORMATEUR":
        lines.extend(_paragraph(
            "Votre demande relative au bilan formateur a bien ete recue. Les elements disponibles doivent etre verifies avant de pouvoir vous confirmer la suite."
        ))
    elif intent == "CV_PROFILS_INTERVENANTS":
        receipt = "du CV joint" if has_attachments else "de votre message concernant le CV"
        lines.extend(_paragraph(f"Nous accusons reception {receipt}. Le document doit etre verifie avant mise a jour du dossier intervenant."))
    elif intent == "PROSPECTION_PARTENARIAT":
        lines.extend(_paragraph(
            "Merci pour votre proposition. Votre demande doit etre examinee par l'equipe Relations entreprises avant de convenir de la suite."
        ))
    else:
        lines.extend(_paragraph(
            "Votre demande necessite une revue par l'equipe Relations entreprises avant reponse."
        ))

    if triage.get("date_will_follow"):
        lines.extend(_paragraph("Nous restons dans l'attente de votre retour concernant la date souhaitee."))

    default_confirmation = _build_default_confirmation(triage)
    if default_confirmation:
        lines.append(f"<b>{escape(default_confirmation)}</b><br><br>")

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
    defaults = ", ".join(triage.get("defaulted_fields") or []) or "aucun"
    return "\n".join([
        meta,
        "",
        "Brouillon Relations entreprises genere automatiquement.",
        f"Action triage: {triage.get('action')}",
        f"Raison: {triage.get('reason')}",
        f"Champs manquants: {missing}",
        f"Hypotheses a confirmer: {defaults}",
        "Prix: XXX a completer manuellement tant que la grille tarifaire CRM n'est pas active.",
        f"Validation: {'OK' if validation.get('valid') else 'A VERIFIER'}",
        *(f"- {err}" for err in validation.get("errors", [])),
        *(f"- Attention: {warning}" for warning in validation.get("warnings", [])),
    ])


def _build_default_confirmation(triage: dict[str, Any]) -> str:
    defaulted = set(triage.get("defaulted_fields") or [])
    if {"nb_candidates", "type_ir"}.issubset(defaulted):
        return "Merci de nous confirmer qu'il s'agit bien d'une formation initiale pour un seul candidat."
    if "type_ir" in defaulted:
        return "Merci de nous confirmer qu'il s'agit bien d'une formation initiale."
    if "nb_candidates" in defaulted:
        return "Merci de nous confirmer que la demande concerne bien un seul candidat."
    return ""


def _build_training_request_section(
    intent: str,
    extracted: dict[str, Any],
    missing_fields: list[str],
    planbot_result: dict[str, Any] | None,
    session_operation: str = "",
) -> list[str]:
    lines = []
    recap = _request_recap(extracted)
    if recap:
        lines.append("<b>Recapitulatif de la demande</b><br>")
        for item in recap:
            lines.append(f"- {escape(item)}<br>")
        lines.append("<br>")

    if missing_fields:
        lead_by_intent = {
            "DEMANDE_DEVIS_FORMATION": "Pour preparer le devis, pouvez-vous nous confirmer les elements suivants :<br>",
            "DEMANDE_DISPONIBILITE_SESSION": "Pour verifier les disponibilites, pouvez-vous nous confirmer les elements suivants :<br>",
            "INSCRIPTION_CANDIDATS": "Pour traiter la demande d'inscription, pouvez-vous nous confirmer les elements suivants :<br>",
            "COMMANDE_FORMALOGISTICS": "Pour traiter la commande, pouvez-vous nous confirmer les elements suivants :<br>",
        }
        lines.append(lead_by_intent.get(intent, "Pouvez-vous nous confirmer les elements suivants :<br>"))
        for field in missing_fields:
            lines.append(f"- {escape(MISSING_FIELD_LABELS.get(field, field))}<br>")
        lines.append("<br>")
        return lines

    session_lines = _format_planbot_sessions(planbot_result)
    if session_lines:
        exact_follow_up = session_operation in {
            "availability_check",
            "reschedule",
            "revert_original",
            "select_session",
        }
        direct_available = _direct_planbot_available(planbot_result)
        if exact_follow_up and not direct_available:
            lines.append("<b>Alternatives identifiees</b><br>")
        else:
            lines.append("<b>Disponibilite verifiee</b><br>" if exact_follow_up else "<b>Sessions identifiees</b><br>")
        lines.extend(session_lines)
        lines.append("<br>")
        if exact_follow_up and not direct_available:
            lines.extend(_paragraph(
                "La session demandee ne dispose pas d'une disponibilite complete. "
                "Merci de nous indiquer si l'une des alternatives ci-dessus vous convient."
            ))
        elif exact_follow_up:
            follow_up_text = {
                "revert_original": (
                    "La capacite necessaire est disponible sur cette session. Le retour a la date initiale "
                    "doit encore etre enregistre par notre equipe avant confirmation definitive."
                ),
                "reschedule": (
                    "La capacite necessaire est disponible sur cette session. La modification demandee "
                    "doit encore etre enregistree par notre equipe avant confirmation definitive."
                ),
                "select_session": (
                    "La capacite necessaire est disponible sur cette session. Votre choix doit encore etre "
                    "enregistre par notre equipe avant confirmation definitive."
                ),
                "availability_check": (
                    "La capacite necessaire est disponible sur cette session. Cette verification ne reserve "
                    "pas automatiquement la place."
                ),
            }[session_operation]
            lines.extend(_paragraph(follow_up_text))
        elif any("Plan de repartition" in line for line in session_lines):
            lines.append("Merci de nous confirmer si ce plan de repartition vous convient afin que nous puissions finaliser le dossier.<br>")
        else:
            lines.append("Merci de nous confirmer la session que vous souhaitez retenir afin que nous puissions finaliser le dossier.<br>")
        lines.append("<br>")
    else:
        if session_operation in {"availability_check", "reschedule", "revert_original", "select_session"}:
            status = str((planbot_result or {}).get("status") or "").lower()
            if planbot_result and status == "ok":
                lines.append(
                    "Aucune disponibilite complete n'a ete identifiee sur la session demandee. "
                    "Une verification humaine reste necessaire avant toute modification.<br><br>"
                )
            else:
                lines.append(
                    "La disponibilite de la session n'a pas pu etre confirmee automatiquement. "
                    "Une verification humaine reste necessaire avant toute modification.<br><br>"
                )
        elif intent == "DEMANDE_DEVIS_FORMATION":
            if recap:
                lines.append("Votre demande de devis a bien ete recue. Aucun montant n'est confirme a ce stade.<br><br>")
            else:
                lines.append("La demande de devis doit etre completee avant preparation.<br><br>")
        elif intent == "INSCRIPTION_CANDIDATS":
            lines.append("La prise en compte de l'inscription doit etre verifiee avant confirmation.<br><br>")
        else:
            lines.append("Les disponibilites doivent encore etre verifiees avant toute confirmation de session.<br><br>")
    return lines


def _format_planbot_sessions(planbot_result: dict[str, Any] | None) -> list[str]:
    if not planbot_result:
        return []

    # Standalone alternative-date searches expose weeks at the root.
    week_lines = _format_planbot_weeks(planbot_result)
    if week_lines:
        return week_lines

    # The next-session fallback wraps same-centre and nearby-centre searches.
    same_centre = planbot_result.get("same_centre") if isinstance(planbot_result, dict) else None
    if isinstance(same_centre, dict):
        week_lines = _format_planbot_weeks(same_centre)
        if week_lines:
            return week_lines

    # A direct check_availability call returns the summarized result at the root.
    if _result_has_availability(planbot_result):
        direct_lines = _format_available_planbot_result(planbot_result, "Option disponible")
        if direct_lines:
            return direct_lines

    # A full query must prefer direct availability, then the same centre, and
    # only then a different centre.
    direct = planbot_result.get("direct") if isinstance(planbot_result, dict) else None
    if direct and _result_has_availability(direct):
        return _format_available_planbot_result(direct, "Option disponible")[:12]

    alt_dates = planbot_result.get("alternative_dates") if isinstance(planbot_result, dict) else None
    if isinstance(alt_dates, dict) and _result_has_availability(alt_dates):
        alt_date_lines = _format_planbot_weeks(alt_dates)
        alt_date_lines = alt_date_lines or _format_available_planbot_result(alt_dates, "Alternative")
        if alt_date_lines:
            return [
                "Aucune disponibilite directe complete n'a ete confirmee sur la periode demandee. "
                "Alternatives sur le meme centre :<br>",
                *alt_date_lines,
            ][:12]

    alternative_centres = planbot_result.get("alternative_centres") if isinstance(planbot_result, dict) else None
    if isinstance(alternative_centres, dict) and _result_has_availability(alternative_centres):
        centre_lines = _format_planbot_centres(alternative_centres)
        centre_lines = centre_lines or _format_available_planbot_result(alternative_centres, "Alternative")
        if centre_lines:
            return [
                "Aucune sequence complete n'a ete identifiee sur le centre demande. "
                "Voici une possibilite dans un centre proche :<br>",
                *centre_lines,
            ][:12]

    return []


def _format_available_planbot_result(result: dict[str, Any], day_prefix: str) -> list[str]:
    if _is_caces_planbot_result(result):
        lines = _format_sequence_options(result.get("sequence_options") or [])
    else:
        lines = _format_days(result.get("jours") or [], prefix=day_prefix)
    return lines or _format_planbot_period(result)


def _direct_planbot_available(planbot_result: dict[str, Any] | None) -> bool:
    if not isinstance(planbot_result, dict):
        return False
    direct = planbot_result.get("direct")
    if isinstance(direct, dict):
        return _result_has_availability(direct)
    return _result_has_availability(planbot_result)


def _format_planbot_weeks(planbot_result: dict[str, Any]) -> list[str]:
    lines = []
    seen_ranges = set()
    require_sequence = _is_caces_planbot_result(planbot_result)
    for week in planbot_result.get("semaines") or []:
        if not isinstance(week, dict) or not week.get("dispo_reelle"):
            continue
        sequence_valid = week.get("sequence_valide")
        if require_sequence and sequence_valid is not True:
            continue
        if sequence_valid is False:
            continue
        options = week.get("options") or []
        if sequence_valid is True and not options:
            continue
        lot_mode = any(isinstance(option, dict) and option.get("lot_required") for option in options)
        if lot_mode and not lines:
            lines.append("Plan de repartition complet - tous les lots ci-dessous sont necessaires :<br>")
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                continue
            dates = [str(value) for value in option.get("dates") or [] if value]
            start = str(option.get("start") or (dates[0] if dates else ""))
            end = str(option.get("end") or (dates[-1] if dates else ""))
            range_key = (start, end, option_index if option.get("lot_required") else -1)
            if not start or not end or range_key in seen_ranges:
                continue
            seen_ranges.add(range_key)
            label = _format_option_dates(dates, start, end)
            if option.get("lot_required"):
                count = int(option.get("nb_candidates") or 0)
                count_label = f" de {count} candidat{'s' if count > 1 else ''}" if count else ""
                lines.append(f"- Lot requis{count_label} : {escape(label)}<br>")
            else:
                lines.append(f"- Session possible : {escape(label)}<br>")
            if not lot_mode and len(lines) >= 3:
                return lines

        if lot_mode and len(lines) > 1:
            return lines

        if not options and sequence_valid is None:
            day_lines = _format_days(week.get("jours") or [], prefix="Option disponible")
            for line in day_lines:
                if line not in lines:
                    lines.append(line)
                if len(lines) >= 3:
                    return lines
    return lines


def _format_planbot_centres(planbot_result: dict[str, Any]) -> list[str]:
    lines = []
    seen = set()
    require_sequence = _is_caces_planbot_result(planbot_result)
    for centre in planbot_result.get("centres") or []:
        if not isinstance(centre, dict) or not centre.get("dispo_reelle"):
            continue
        sequence_valid = centre.get("sequence_valide")
        if require_sequence and sequence_valid is not True:
            continue
        centre_name = str(centre.get("centre") or "").strip()
        options = centre.get("options") or []
        lot_mode = any(isinstance(option, dict) and option.get("lot_required") for option in options)
        if lot_mode and not lines:
            lines.append("Plan de repartition complet - tous les lots ci-dessous sont necessaires :<br>")
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                continue
            start = str(option.get("start") or "")
            end = str(option.get("end") or "")
            key = (centre_name, start, end, option_index if option.get("lot_required") else -1)
            if not centre_name or not start or not end or key in seen:
                continue
            seen.add(key)
            date_label = _format_option_dates(option.get("dates") or [], start, end)
            if option.get("lot_required"):
                count = int(option.get("nb_candidates") or 0)
                count_label = f" de {count} candidat{'s' if count > 1 else ''}" if count else ""
                lines.append(f"- {escape(centre_name)}, lot requis{count_label} : {escape(date_label)}<br>")
            else:
                lines.append(f"- {escape(centre_name)} : {escape(date_label)}<br>")
            if not lot_mode and len(lines) >= 3:
                return lines
        if lot_mode and len(lines) > 1:
            return lines
    return lines


def _format_sequence_options(options: list[dict[str, Any]]) -> list[str]:
    lines = []
    seen = set()
    lot_mode = any(isinstance(option, dict) and option.get("lot_required") for option in options)
    if lot_mode:
        lines.append("Plan de repartition complet - tous les lots ci-dessous sont necessaires :<br>")
    for option_index, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        start = str(option.get("start") or "")
        end = str(option.get("end") or "")
        key = (start, end, option_index if option.get("lot_required") else -1)
        if not start or not end or key in seen:
            continue
        seen.add(key)
        label = _format_option_dates(option.get("dates") or [], start, end)
        if option.get("lot_required"):
            count = int(option.get("nb_candidates") or 0)
            count_label = f" de {count} candidat{'s' if count > 1 else ''}" if count else ""
            lines.append(f"- Lot requis{count_label} : {escape(label)}<br>")
        else:
            lines.append(f"- Session possible : {escape(label)}<br>")
        if not lot_mode and len(lines) >= 3:
            break
    return lines


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
    verdict = str(result.get("verdict") or "").strip().lower()
    require_sequence = _is_caces_planbot_result(result)
    nested_available = (
        any(_planbot_item_available(week, require_sequence) for week in result.get("semaines") or [])
        or any(_planbot_item_available(centre, require_sequence) for centre in result.get("centres") or [])
    )
    if nested_available:
        return True
    if _is_caces_planbot_result(result):
        if result.get("sequence_valide") is False:
            return False
        return bool(
            result.get("coverage_complete") is True
            or (result.get("sequence_valide") is True and result.get("sequence_options"))
        )
    return bool(
        result.get("coverage_complete") is True
        or verdict in {"dispo", "disponible", "dispo_complete", "disponibilite_complete"}
    )


def _planbot_item_available(item: Any, require_sequence: bool = False) -> bool:
    if not isinstance(item, dict) or not item.get("dispo_reelle"):
        return False
    if require_sequence:
        return bool(item.get("sequence_valide") is True and item.get("options"))
    sequence_valid = item.get("sequence_valide")
    if sequence_valid is True:
        return bool(item.get("options"))
    if sequence_valid is False:
        return False
    return bool(item.get("jours"))


def _is_caces_planbot_result(result: dict[str, Any]) -> bool:
    formation = str(result.get("formation") or "").lower()
    return "caces" in formation or bool(re.search(r"\br\s?(?:48[24569]|490)\b", formation, flags=re.IGNORECASE))


def _request_recap(extracted: dict[str, Any]) -> list[str]:
    items = []
    if extracted.get("formation_type"):
        items.append(f"Formation : {extracted['formation_type']}")
    if extracted.get("centre"):
        items.append(f"Centre : {extracted['centre']}")
    if extracted.get("start_date") or extracted.get("end_date"):
        start = _format_date(extracted.get("start_date"))
        end = _format_date(extracted.get("end_date"))
        if start and end:
            items.append(f"Periode : {start}" if start == end else f"Periode : {start} - {end}")
        else:
            items.append(f"Date : {start or end}")
    if extracted.get("nb_candidates"):
        items.append(f"Nombre de candidats : {extracted['nb_candidates']}")
    categories = extracted.get("categories") or []
    if categories:
        items.append(f"Categories : {', '.join(str(cat) for cat in categories)}")
    if extracted.get("type_ir"):
        items.append(f"Type : {extracted['type_ir']}")
    return items


def _format_planbot_period(result: dict[str, Any]) -> list[str]:
    period = str(result.get("periode") or "")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", period)
    if not dates:
        return []
    start = _format_date(dates[0])
    end = _format_date(dates[-1])
    label = start if start == end else f"du {start} au {end}"
    return [f"- Session possible : {escape(label)}<br>"]


def _normalize_missing_fields(fields: list[str]) -> list[str]:
    normalized = []
    for field in fields:
        value = str(field or "").strip()
        if not value or value not in MISSING_FIELD_LABELS and value not in {"start_date", "end_date", "dates_session", "dates_sessions"}:
            continue
        if value in {"start_date", "end_date", "dates_session", "dates_sessions"}:
            if "dates" not in normalized:
                normalized.append("dates")
            continue
        if value not in normalized:
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


def _format_option_dates(dates: list[Any], start: str, end: str) -> str:
    values = list(dict.fromkeys(str(value)[:10] for value in dates if value))
    if not values:
        values = [value for value in (start, end) if value]
    parsed = []
    for value in values:
        try:
            parsed.append(datetime.strptime(value, "%Y-%m-%d"))
        except ValueError:
            parsed = []
            break
    if parsed and len(parsed) > 1:
        consecutive = all((current - previous).days == 1 for previous, current in zip(parsed, parsed[1:]))
        if not consecutive:
            labels = [_format_date(value) for value in values]
            return "les " + ", ".join(labels[:-1]) + f" et {labels[-1]}"
    first = values[0] if values else start
    last = values[-1] if values else end
    return _format_date(first) if first == last else f"du {_format_date(first)} au {_format_date(last)}"


def _planbot_status(planbot_result: dict[str, Any] | None) -> str:
    if not planbot_result:
        return "not_called"
    return str(planbot_result.get("recommended_status") or planbot_result.get("status") or "unknown")
