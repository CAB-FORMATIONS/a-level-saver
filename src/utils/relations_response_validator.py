"""Safety validation for Relations entreprises drafts."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
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

FORBIDDEN_PATTERNS = {
    r"\bXXX\b": "Placeholder XXX present dans le draft",
    r"\b(?:vous trouverez|veuillez trouver)\s+ci[- ]joint": "Piece jointe annoncee mais non ajoutee par le workflow",
    r"\bannulation\b.{0,50}\b(?:confirmee|confirmée|enregistree|enregistrée|effectuee|effectuée)\b": "Annulation confirmee sans preuve d'execution",
    r"\bannulation\b.{0,60}\bprise en compte\b|\bprise en compte\b.{0,60}\bannulation\b": "Annulation declaree prise en compte sans preuve d'execution",
    r"\binscriptions?\b.{0,60}\b(?:confirmees?|confirmées?|enregistrees?|enregistrées?|prises? en compte|validees?|validées?)\b": "Inscription confirmee sans preuve d'execution",
    r"\b(?:enregistree?|enregistrée?|enregistrees?|enregistrées?|validee?|validée?|validees?|validées?)\b.{0,60}\binscriptions?\b": "Inscription declaree traitee sans preuve d'execution",
    r"\bparticipation\b.{0,40}\b(?:confirmee|confirmée|validee|validée)\b": "Participation confirmee sans preuve d'execution",
    r"\b(?:report|absence)\b.{0,50}\b(?:confirme|confirmé|confirmee|confirmée|enregistre|enregistré|enregistree|enregistrée|valide|validé|validee|validée)\b": "Report ou absence confirme sans preuve d'execution",
    r"\b(?:confirme|confirmé|confirmee|confirmée|enregistre|enregistré|enregistree|enregistrée)\b.{0,50}\b(?:report|absence)\b": "Report ou absence confirme sans preuve d'execution",
    r"\b(?:pris|prise|prises)\s+en compte\b.{0,60}\b(?:inscription|annulation|report|absence)\b": "Action declaree prise en compte sans preuve d'execution",
    r"\binscriptions?\b.{0,60}\b(?:effectuees?|effectuées?|traitees?|traitées?|finalisees?|finalisées?)\b": "Inscription declaree executee sans preuve",
    r"\b(?:effectuees?|effectuées?|traitees?|traitées?|finalisees?|finalisées?)\b.{0,60}\binscriptions?\b": "Inscription declaree executee sans preuve",
    r"\b(?:annulation|report|absence)\b.{0,60}\b(?:effectuee?|effectuée?|traitee?|traitée?|finalisee?|finalisée?)\b": "Modification declaree executee sans preuve",
    r"\b(?:candidat|stagiaire|participant)\b.{0,50}\b(?:est|a ete)\s+(?:maintenant\s+)?inscrit\b": "Candidat declare inscrit sans preuve d'execution",
    r"\b(?:avons|a ete)\s+proc[eé]d[eé]\s+[aà]\b.{0,40}\binscription\b": "Inscription declaree executee sans preuve",
    r"\binscription\b.{0,50}\b(?:terminee?|terminée?|faite|realisee?|réalisée?)\b": "Inscription declaree executee sans preuve",
    r"\b(?:candidat|stagiaire|participant)\b.{0,50}\b(?:ajoute|ajouté|integre|intégré)\b.{0,40}\bsession\b": "Candidat declare ajoute sans preuve d'execution",
    r"\binscription\b.{0,40}\bcomplete\b": "Inscription declaree complete sans preuve d'execution",
    r"\b[a-zà-ÿ][a-zà-ÿ' -]{0,40}\s+est\s+bien\s+inscrit\b": "Candidat declare inscrit sans preuve d'execution",
    r"\b(?:document|convention|bon de commande|bdc)\b.{0,50}\b(?:conforme|valide|validé|validee|validée)\b": "Document declare valide sans verification",
    r"\b(?:valide|validé|validee|validée|validons)\b.{0,60}\b(?:document|convention|bon de commande|bdc)\b": "Document declare valide sans verification",
    r"\b(?:nous|notre conseiller)\b.{0,30}\b(?:repondrons|répondrons|transmettrons|enverrons|adresserons|transmettra|enverra|adressera|fera parvenir)\b.{0,80}\b(?:devis|convention|facture|convocation|attestation|document)\b": "Transmission de document promise sans action verifiee",
    r"\b(?:nous|notre conseiller)\b.{0,40}\b(?:va|allons)\b.{0,30}\b(?:faire parvenir|transmettre|envoyer|adresser)\b.{0,60}\b(?:devis|convention|facture|convocation|attestation|document)\b": "Transmission de document promise sans action verifiee",
    r"\b(?:nous|notre conseiller)\b.{0,40}\b(?:va|allons)\b.{0,30}\bvous\s+(?:le|la|les)?\s*faire parvenir\b": "Transmission de document promise sans action verifiee",
    r"\b(?:devis|document|convention|facture|convocation|attestation)\b.{0,80}\b(?:va (?:etre|être)|sera)\b.{0,30}\b(?:prepare|préparé|preparee|préparée|transmis|transmise|envoye|envoyé|envoyee|envoyée)\b": "Preparation ou transmission de document promise sans action verifiee",
    r"\bvous sera\b.{0,30}\b(?:transmis|transmise|envoye|envoyé|envoyee|envoyée|adresse|adressé|adressee|adressée)\b": "Transmission de document promise sans action verifiee",
    r"\bvous le fera parvenir\b": "Transmission de document promise sans action verifiee",
    r"\b(?:modification|changement|report|inscription|demande)\b.{0,50}\ben cours de (?:traitement|mise a jour|mise à jour|enregistrement)\b": "Action annoncee en cours sans preuve d'execution",
    r"\bnous revenons vers (?:toi|vous)\b.{0,80}\bd[eè]s que|\bnous reviendrons vers (?:toi|vous)\b.{0,80}\bd[eè]s que": "Promesse de suivi conditionnelle non verifiee",
    r"\b(?:notre [eé]quipe|nous)\b.{0,20}\b(?:va|allons)\s+v[eé]rifier\b": "Verification future promise sans action executee",
    r"\b(?:notre [eé]quipe\s+)?revient vers (?:toi|vous)\b.{0,50}\bpour confirmation\b|\bnous revenons vers (?:toi|vous)\b.{0,50}\bpour confirmation\b": "Promesse de suivi non verifiee",
    r"\bpour (?:te|vous)\s+(?:transmettre|envoyer|adresser|faire parvenir)\b.{0,60}\b(?:convocation|document|devis|convention|facture|attestation)\b": "Transmission de document promise sans action verifiee",
    r"\bbesoin\b.{0,40}\bnom\b.{0,20}\bpr[eé]nom\b|\bnom et pr[eé]nom\b.{0,60}\b(?:convention|dossier)\b": "Informations candidat supplementaires demandees sans base factuelle",
}

WARNING_PATTERNS = {
    r"\b(?:tres|très) prochainement\b|\bdes que possible\b|\bdès que possible\b|\bdans les meilleurs delais\b|\bdans les meilleurs délais\b": "Promesse de delai vague a verifier",
}

MONTHS = {
    "janvier": 1,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
}


def validate_relations_response(
    response_html: str,
    triage: dict[str, Any],
    planbot_result: dict[str, Any] | None,
    source_response_html: str | None = None,
    allowed_source_text: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    text = response_html or ""
    lower = text.lower()

    allowed_html = re.sub(r"</?b\s*>|<br\s*/?>", "", text, flags=re.IGNORECASE)
    if re.search(r"<[^>]*>", allowed_html):
        errors.append("Balise HTML non autorisee dans le draft")
    if "```" in text:
        errors.append("Bloc Markdown present dans le draft")

    for term in FORBIDDEN_TERMS:
        pattern = r"\b" + re.escape(term.lower()) + r"\b"
        if re.search(pattern, lower):
            errors.append(f"Terme interne interdit dans le draft: {term}")

    for pattern, error in FORBIDDEN_PATTERNS.items():
        if re.search(pattern, lower, flags=re.IGNORECASE | re.DOTALL):
            errors.append(error)

    for pattern, warning in WARNING_PATTERNS.items():
        if re.search(pattern, lower, flags=re.IGNORECASE | re.DOTALL):
            warnings.append(warning)

    amounts = re.findall(r"(?<!X)\b\d+[\s\u00a0]*(?:€|eur|euros?)\b", text, flags=re.IGNORECASE)
    if amounts:
        errors.append(f"Montant chiffre non verifie detecte: {', '.join(amounts)}")

    intent = triage.get("intent")
    if intent in {"DEMANDE_DEVIS_FORMATION", "DEMANDE_DISPONIBILITE_SESSION", "INSCRIPTION_CANDIDATS", "COMMANDE_FORMALOGISTICS"}:
        if _should_have_planbot(triage) and not planbot_result:
            warnings.append("PlanBot non appele malgre donnees de disponibilite presentes")

    if any(word in lower for word in ["inscription confirmee", "inscription confirmée", "place reservee", "place réservée"]):
        errors.append("Le draft confirme une inscription/place reservee alors que le workflow ne fait que proposer")

    availability_claim = re.search(
        r"\b(?:session|place|places|creneau|créneau)\b.{0,60}\b(?:est|sont|reste|restent)\s+disponible"
        r"|\bconfirmons\b.{0,40}\bdisponibilit"
        r"|\bnous avons\b.{0,30}\b(?:une|des)\s+(?:place|places|session|sessions)\s+disponible"
        r"|\bnous pouvons\b.{0,40}\bproposer\b.{0,40}\bsession\b"
        r"|\bnous proposons\b.{0,40}\bsession\b"
        r"|\b(?:une|la|cette)\s+session\b.{0,30}\b(?:est\s+)?possible\b"
        r"|\bsession\s+(?:possible|disponible)\b"
        r"|\b(?:date|periode|période|formation)\b.{0,60}\b(?:est|reste|serait)\s+disponible\b"
        r"|\bnous avons\b.{0,30}\b(?:une|des)\s+disponibilit[eé]s?\b"
        r"|\bil reste\b.{0,30}\b(?:une|des)\s+disponibilit[eé]s?\b"
        r"|\b(?:le\s+)?\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b.{0,30}\b(?:est|reste)\s+libre\b"
        r"|\bcreneaux?\s+disponibles?\b|\bcréneaux?\s+disponibles?\b",
        lower,
        flags=re.DOTALL,
    )
    if availability_claim:
        exact_operation = triage.get("session_operation") in {
            "availability_check",
            "reschedule",
            "revert_original",
            "select_session",
        }
        availability_verified = _availability_claim_is_verified(
            text,
            availability_claim.span(),
            planbot_result,
            exact_operation,
        )
        if not availability_verified:
            errors.append("Disponibilite annoncee sans resultat PlanBot complet")

    factual_sources = f"{source_response_html or ''}\n{allowed_source_text or ''}".lower()
    if "convention" in lower and "convention" not in factual_sources:
        errors.append("Convention mentionnee sans base factuelle")

    if "cordialement" not in lower:
        errors.append("Formule de politesse absente")
    if "relations entreprises" not in lower:
        errors.append("Signature Relations entreprises absente")

    if "bonjour" not in lower:
        errors.append("Salutation absente")

    defaulted_fields = set(triage.get("defaulted_fields") or [])
    body_tail = lower.rsplit("cordialement", 1)[0][-700:]
    confirmation_request = bool(re.search(
        r"(?:merci de (?:bien )?nous confirmer|(?:pouvez|pourriez)-vous (?:(?:egalement|également) )?(?:nous )?confirmer)",
        body_tail,
    ))
    if "type_ir" in defaulted_fields and not (
        confirmation_request and re.search(r"\b(?:formation\s+)?initiale?\b", body_tail)
    ):
        errors.append("Confirmation du type initial par defaut absente")
    if "nb_candidates" in defaulted_fields and not (
        confirmation_request and re.search(r"\b(?:un seul|1)\s+candidat\b", body_tail)
    ):
        errors.append("Confirmation du candidat unique par defaut absente")

    response_dates, invalid_response_dates = extract_dates(text)
    if invalid_response_dates:
        errors.append(f"Dates invalides dans le draft: {', '.join(sorted(invalid_response_dates))}")

    if source_response_html or allowed_source_text:
        source_dates, _ = extract_dates(source_response_html or "")
        raw_source_dates, _ = extract_dates(allowed_source_text or "")
        planbot_status = str((planbot_result or {}).get("status") or "").lower()
        planbot_verified = bool(planbot_result) and planbot_status not in {"error", "skipped"}
        if allowed_source_text is None:
            allowed_dates = source_dates
        else:
            allowed_dates = raw_source_dates | (source_dates if planbot_verified else set())
        required_dates = source_dates & allowed_dates
        missing_dates = sorted(required_dates - response_dates)
        if missing_dates:
            errors.append(f"Dates verifiees absentes du draft: {', '.join(missing_dates)}")
        invented_dates = sorted(response_dates - allowed_dates)
        if invented_dates:
            errors.append(f"Dates absentes des sources: {', '.join(invented_dates)}")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }


def extract_dates(value: str) -> tuple[set[str], set[str]]:
    dates: set[str] = set()
    invalid: set[str] = set()
    text = value or ""

    for pattern, date_format in (
        (r"\b\d{4}-\d{1,2}-\d{1,2}\b", "%Y-%m-%d"),
        (r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", "%d/%m/%Y"),
        (r"\b\d{1,2}-\d{1,2}-\d{2,4}\b", "%d-%m-%Y"),
    ):
        for match in re.findall(pattern, text):
            normalized_match = match
            if date_format != "%Y-%m-%d":
                separator = "/" if "/" in match else "-"
                parts = match.split(separator)
                if len(parts[-1]) == 2:
                    parts[-1] = f"20{parts[-1]}"
                    normalized_match = separator.join(parts)
            try:
                dates.add(datetime.strptime(normalized_match, date_format).strftime("%Y-%m-%d"))
            except ValueError:
                invalid.add(match)

    normalized_text = unicodedata.normalize("NFD", text.lower())
    normalized_text = "".join(ch for ch in normalized_text if unicodedata.category(ch) != "Mn")
    month_names = "|".join(MONTHS)
    range_pattern = rf"\b(\d{{1,2}})\s+(?:au|a)\s+(\d{{1,2}})\s+({month_names})\s+(\d{{4}})\b"
    for start_day, end_day, month_name, year in re.findall(range_pattern, normalized_text):
        for day in (start_day, end_day):
            try:
                dates.add(datetime(int(year), MONTHS[month_name], int(day)).strftime("%Y-%m-%d"))
            except ValueError:
                invalid.add(f"{day} {month_name} {year}")

    single_pattern = rf"\b(\d{{1,2}})(?:er)?\s+({month_names})\s+(\d{{4}})\b"
    for day, month_name, year in re.findall(single_pattern, normalized_text):
        try:
            dates.add(datetime(int(year), MONTHS[month_name], int(day)).strftime("%Y-%m-%d"))
        except ValueError:
            invalid.add(f"{day} {month_name} {year}")
    return dates, invalid


def _should_have_planbot(triage: dict[str, Any]) -> bool:
    if triage.get("request_mode") in {
        "acknowledgement",
        "document_submission",
        "confirmation_request",
    }:
        return False
    extracted = triage.get("extracted") or {}
    return all([
        extracted.get("formation_type"),
        extracted.get("centre"),
        extracted.get("start_date"),
        extracted.get("end_date"),
        extracted.get("nb_candidates"),
    ])


def _has_verified_availability(planbot_result: dict[str, Any] | None) -> bool:
    if not isinstance(planbot_result, dict):
        return False
    if str(planbot_result.get("status") or "").lower() in {"error", "skipped"}:
        return False
    formation = str(planbot_result.get("formation") or "").lower()
    is_caces = "caces" in formation or bool(re.search(r"\br\s?(?:48[24569]|490)\b", formation))
    if any(_verified_planbot_item(week, is_caces) for week in planbot_result.get("semaines") or []):
        return True
    if any(_verified_planbot_item(centre, is_caces) for centre in planbot_result.get("centres") or []):
        return True
    candidates = [
        planbot_result.get("direct"),
        planbot_result.get("alternative_dates"),
        planbot_result.get("alternative_centres"),
    ]
    if any(isinstance(candidate, dict) for candidate in candidates):
        return any(
            _has_verified_availability(candidate)
            for candidate in candidates
            if isinstance(candidate, dict)
        )
    if is_caces:
        if planbot_result.get("sequence_valide") is False:
            return False
        return bool(
            planbot_result.get("coverage_complete") is True
            or (
                planbot_result.get("sequence_valide") is True
                and planbot_result.get("sequence_options")
            )
        )
    return bool(
        planbot_result.get("coverage_complete") is True
        or str(planbot_result.get("verdict") or "").strip().lower()
        in {"dispo", "disponible", "dispo_complete", "disponibilite_complete"}
    )


def _has_verified_direct_availability(planbot_result: dict[str, Any] | None) -> bool:
    if not isinstance(planbot_result, dict):
        return False
    if "direct" in planbot_result:
        direct = planbot_result.get("direct")
        return _has_verified_availability(direct) if isinstance(direct, dict) else False
    return _has_verified_availability(planbot_result)


def has_verified_direct_availability(planbot_result: dict[str, Any] | None) -> bool:
    """Public availability check used by the workflow delivery guard."""
    return _has_verified_direct_availability(planbot_result)


def has_verified_availability(planbot_result: dict[str, Any] | None) -> bool:
    """Return whether a direct session or a safe alternative is available."""
    return _has_verified_availability(planbot_result)


def _availability_claim_is_verified(
    response_html: str,
    claim_span: tuple[int, int],
    planbot_result: dict[str, Any] | None,
    exact_operation: bool,
) -> bool:
    if not isinstance(planbot_result, dict):
        return False
    if "direct" not in planbot_result:
        return _has_verified_availability(planbot_result)

    direct = planbot_result.get("direct")
    if isinstance(direct, dict) and _has_verified_availability(direct):
        return True

    start = max(0, claim_span[0] - 100)
    end = min(len(response_html), claim_span[1] + 180)
    claim_dates, _ = extract_dates(response_html[start:end])
    if not claim_dates:
        return False
    for key in ("alternative_dates", "alternative_centres"):
        alternative = planbot_result.get(key)
        if not isinstance(alternative, dict) or not _has_verified_availability(alternative):
            continue
        alternative_dates, _ = extract_dates(str(alternative))
        if claim_dates.issubset(alternative_dates):
            return True
    return False


def _verified_planbot_item(item: Any, require_sequence: bool = False) -> bool:
    if not isinstance(item, dict) or not item.get("dispo_reelle"):
        return False
    if require_sequence:
        return bool(item.get("sequence_valide") is True and item.get("options"))
    if item.get("sequence_valide") is True:
        return bool(item.get("options"))
    if item.get("sequence_valide") is False:
        return False
    return bool(item.get("jours"))
