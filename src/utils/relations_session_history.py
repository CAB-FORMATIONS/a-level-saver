"""Deterministic session-context reconstruction for B2B email follow-ups."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Any, Iterable


EXACT_AVAILABILITY_OPERATIONS = frozenset({
    "availability_check",
    "reschedule",
    "revert_original",
    "select_session",
})

NO_AVAILABILITY_OPERATIONS = frozenset({
    "absence",
    "cancel",
    "candidate_removal",
    "date_pending",
    "status_confirmation",
})

NUMBER_WORDS = {
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "six": 6,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
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

SESSION_CHANGE_WORDS = (
    "avance",
    "avancer",
    "avancez",
    "change",
    "changer",
    "changez",
    "decale",
    "decaler",
    "decalez",
    "deplace",
    "deplacer",
    "deplacez",
    "modifie",
    "modifier",
    "modifiez",
    "rapproche",
    "rapprocher",
    "rapprochez",
    "replanifie",
    "replanifier",
    "replanifiez",
    "reprogramme",
    "reprogrammer",
    "reprogrammez",
    "reporte",
    "reporter",
    "reportez",
)


def detect_session_operation(message: str) -> str:
    """Classify the current session action without relying on the old subject."""
    text = _normalize(message)
    if not text:
        return "unknown"

    revert_patterns = (
        r"\b(?:retour|revenir|reviens|revient|revenons|repasser|garder|maintenir|reste[rz]?)\b.{0,70}"
        r"\b(?:date|session|periode)\s+(?:initiale|initial|d origine)\b",
        r"\b(?:date|session|periode)\s+(?:initiale|initial|d origine)\b.{0,70}"
        r"\b(?:retour|revenir|reviens|revient|garder|maintenir|conserver)\b",
        r"\bannul(?:e|er|ons|ez)\b.{0,50}\b(?:report|changement|decalage|deplacement|nouvelle date)\b",
        r"\bfinalement\b.{0,80}\b(?:date|session)\s+(?:initiale|d origine)\b",
    )
    if any(re.search(pattern, text) for pattern in revert_patterns):
        return "revert_original"

    if re.search(
        r"\b(?:retir(?:er|e|ez)|enlev(?:er|e|ez)|supprim(?:er|e|ez)|desinscri(?:re|t|vez))\b"
        r".{0,60}\b(?:candidat|stagiaire|participant|collaborateur|salarie)\b",
        text,
    ):
        return "candidate_removal"

    if re.search(r"\b(?:absence|absent|ne (?:pourra|peut) pas (?:venir|participer|assister))\b", text):
        return "absence"

    if re.search(
        r"\b(?:annulation|annuler|annule|annulez)\b.{0,70}\b(?:session|formation|inscription|participation)\b"
        r"|\b(?:session|formation|inscription|participation)\b.{0,70}\b(?:annulation|annuler|annule|annulez)\b",
        text,
    ):
        return "cancel"

    if re.search(
        r"\b(?:je|nous)\s+revien(?:s|drons?)\s+(?:vers\s+(?:toi|vous)\s+)?(?:demain|plus tard)?\s*"
        r"(?:pour|avec|concernant)?\s*(?:la|les|une)?\s*date",
        text,
    ) and not re.search(r"\brevien(?:s|t|ons)\s+(?:a|sur)\s+la\s+date\b", text):
        return "date_pending"

    selection_signal = re.search(
        r"\b(?:c est|ce sera|ok|d accord|je confirme|nous confirmons|je retiens|nous retenons|"
        r"je choisis|nous choisissons|on part|partons|maintenir|gard(?:er|ons))\b",
        text,
    )
    date_or_option = bool(
        _extract_date_mentions(message, _today_reference())
        or re.search(r"\b(?:premiere|deuxieme|seconde|troisieme|derniere)\b", text)
        or re.search(r"\bpour\s+le\s+\d{1,2}\b", text)
    )
    if selection_signal and date_or_option:
        return "select_session"

    change_pattern = "|".join(SESSION_CHANGE_WORDS)
    if re.search(
        rf"\b(?:{change_pattern})\b.{{0,70}}\b(?:date|session|formation|periode)\b"
        rf"|\b(?:date|session|formation|periode)\b.{{0,70}}\b(?:{change_pattern})\b",
        text,
    ) or ("finalement" in text and date_or_option):
        return "reschedule"

    if re.search(r"\b(?:autres?|prochaines?|nouvelles?)\s+(?:dates?|disponibilites?|sessions?)\b", text):
        return "alternative_search"

    if "disponibil" in text and re.search(
        r"\b(?:confirmer|verifier|encore|toujours|reste|restent|avez|auriez|place|places|session)\b",
        text,
    ):
        return "availability_check"

    if re.search(
        r"\b(?:convocation|inscription|participation)\b.{0,80}\b(?:confirmer|confirmation|prise en compte|transmettre|envoyer)\b"
        r"|\b(?:confirmer|confirmation|prise en compte|transmettre|envoyer)\b.{0,80}"
        r"\b(?:convocation|inscription|participation)\b",
        text,
    ):
        return "status_confirmation"

    return "unknown"


def extract_current_training_facts(text: str) -> dict[str, Any]:
    """Extract ordered current-message facts and expose unresolved conflicts."""
    formations = _extract_formations(text)
    type_values = _extract_type_values(text)
    count_values = _extract_candidate_counts(text)
    ambiguous = []
    if len(formations) > 1:
        ambiguous.append("formation_type")
    if len(type_values) > 1:
        ambiguous.append("type_ir")
    if len(count_values) > 1:
        ambiguous.append("nb_candidates")
    return {
        "formation_type": formations[0] if len(formations) == 1 else "",
        "categories": _extract_categories(text),
        "type_ir": type_values[0] if len(type_values) == 1 else "",
        "nb_candidates": count_values[0] if len(count_values) == 1 else None,
        "ambiguous_fields": ambiguous,
    }


def reconstruct_session_context(
    entries: list[dict[str, Any]],
    current_thread_id: str,
    known_centres: Iterable[str],
) -> dict[str, Any]:
    """Resolve one target session from chronological, source-aware messages."""
    ordered = sorted(entries, key=lambda item: (float(item.get("timestamp") or 0), str(item.get("id") or "")))
    current_index = next(
        (index for index, entry in enumerate(ordered) if str(entry.get("id") or "") == str(current_thread_id)),
        len(ordered) - 1,
    )
    if current_index < 0 or not ordered:
        return _empty_resolution("unknown")

    current = ordered[current_index]
    current_text = str(current.get("text") or "")
    operation = detect_session_operation(current_text)
    result = _empty_resolution(operation)
    if operation == "unknown":
        return result

    historical = ordered[:current_index]
    reference = _entry_reference_date(current)
    current_facts = _extract_message_facts(current_text, reference, known_centres)
    candidates, static = _build_historical_candidates(historical, known_centres)
    active_names = _latest_candidate_names(historical)

    target, ambiguity = _resolve_target_candidate(
        operation,
        current_text,
        current_facts,
        candidates,
        active_names,
        reference,
    )

    if target is None and operation in NO_AVAILABILITY_OPERATIONS:
        target = _latest_candidate(candidates, active_names)

    facts: dict[str, Any] = {}
    source_ids: list[str] = []
    if target:
        facts.update({
            key: target.get(key)
            for key in (
                "formation_type",
                "centre",
                "start_date",
                "end_date",
                "nb_candidates",
                "categories",
                "type_ir",
                "candidate_names",
            )
            if target.get(key) not in (None, "", [])
        })
        source_ids.extend(str(value) for value in target.get("source_thread_ids") or [] if value)

    # Explicit current facts always win over inherited values.
    for key in ("formation_type", "centre", "nb_candidates", "categories", "type_ir"):
        value = current_facts.get(key)
        if value not in (None, "", []):
            facts[key] = value
            source_ids.append(str(current.get("id") or ""))

    facts = _inherit_static_facts(facts, static, source_ids)
    if facts.get("categories"):
        facts["categories"] = _filter_categories_for_formation(
            list(facts["categories"]),
            str(facts.get("formation_type") or ""),
        )
    if active_names and not facts.get("candidate_names"):
        facts["candidate_names"] = active_names
    if facts.get("candidate_names") and len(facts["candidate_names"]) == 1 and not facts.get("nb_candidates"):
        facts["nb_candidates"] = 1

    start = _as_date(facts.get("start_date"))
    end = _as_date(facts.get("end_date"))
    if start and end and start <= end:
        facts["nombre_jours_souhaites"] = (end - start).days + 1
    facts["financement"] = "B2B"

    missing = _required_missing_fields(facts)
    if ambiguity:
        status = "ambiguous"
    elif operation in NO_AVAILABILITY_OPERATIONS:
        status = "resolved" if target else "not_required"
    elif target is None:
        status = "incomplete"
        if "dates" not in missing:
            missing.insert(0, "dates")
    elif missing:
        status = "incomplete"
    else:
        status = "resolved"

    result.update({
        "status": status,
        "reason": ambiguity or _resolution_reason(status, operation, missing),
        "facts": facts,
        "missing_fields": list(dict.fromkeys(missing)),
        "verified_fields": [
            key for key, value in facts.items()
            if key != "financement" and value not in (None, "", [])
        ],
        "source_thread_ids": list(dict.fromkeys(value for value in source_ids if value)),
        "candidate_count": len(candidates),
        "should_check_availability": operation in EXACT_AVAILABILITY_OPERATIONS and status == "resolved",
    })
    return result


def _empty_resolution(operation: str) -> dict[str, Any]:
    return {
        "operation": operation,
        "status": "not_applicable",
        "reason": "",
        "facts": {},
        "missing_fields": [],
        "verified_fields": [],
        "source_thread_ids": [],
        "candidate_count": 0,
        "should_check_availability": False,
    }


def _build_historical_candidates(
    entries: list[dict[str, Any]],
    known_centres: Iterable[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    active: dict[str, Any] = {}
    active_sources: dict[str, str] = {}
    active_names: list[str] = []

    for entry in entries:
        text = str(entry.get("text") or "")
        facts = _extract_message_facts(text, _entry_reference_date(entry), known_centres)
        if facts.get("formation_type"):
            previous_formation = active.get("formation_type")
            if previous_formation and not _formations_compatible(previous_formation, facts["formation_type"]):
                active = {}
                active_sources = {}
                active_names = []
            active["formation_type"] = _prefer_specific_formation(
                active.get("formation_type"), facts["formation_type"]
            )
            active_sources["formation_type"] = str(entry.get("id") or "")
        if facts.get("categories"):
            active["categories"] = facts["categories"]
            active_sources["categories"] = str(entry.get("id") or "")
        if facts.get("type_ir"):
            active["type_ir"] = facts["type_ir"]
            active_sources["type_ir"] = str(entry.get("id") or "")
        if facts.get("nb_candidates"):
            active["nb_candidates"] = facts["nb_candidates"]
            active_sources["nb_candidates"] = str(entry.get("id") or "")
        if facts.get("candidate_names"):
            active_names = list(facts["candidate_names"])

        date_mentions = facts.get("date_mentions") or []
        for mention in date_mentions:
            if not _date_has_session_evidence(
                text,
                mention,
                facts,
                bool(active.get("formation_type")),
            ):
                continue
            centre = _nearest_value(mention["position"], facts.get("centre_mentions") or [])
            names_before_date = [
                name
                for name_position, name in (facts.get("candidate_name_mentions") or [])
                if name_position <= mention["position"]
            ]
            if len(date_mentions) == 1 and len(names_before_date) > 1:
                names = list(dict.fromkeys(names_before_date))
            else:
                names = _nearest_names(mention["position"], facts.get("candidate_name_mentions") or [])
            if not names and len(active_names) == 1:
                names = list(active_names)
            candidate = {
                "start_date": mention["start_date"],
                "end_date": mention["end_date"],
                "centre": centre or (facts.get("centre") if len(facts.get("centres") or []) == 1 else ""),
                "formation_type": facts.get("formation_type") or active.get("formation_type") or "",
                "categories": facts.get("categories") or active.get("categories") or [],
                "type_ir": facts.get("type_ir") or active.get("type_ir") or "",
                "nb_candidates": facts.get("nb_candidates") or active.get("nb_candidates"),
                "candidate_names": names,
                "count_explicit": facts.get("nb_candidates") is not None,
                "role": entry.get("role"),
                "timestamp": float(entry.get("timestamp") or 0),
                "source_thread_ids": [str(entry.get("id") or "")],
                "selected": _message_selects_session(text, str(entry.get("role") or "")),
            }
            if names and not facts.get("nb_candidates"):
                candidate["nb_candidates"] = len(names)
            candidates.append(candidate)

    _enrich_duplicate_candidates(candidates)
    static = {
        "facts": {
            key: value for key, value in active.items()
            if key in {"formation_type", "categories", "type_ir", "nb_candidates"}
        },
        "sources": active_sources,
    }
    return candidates, static


def _resolve_target_candidate(
    operation: str,
    current_text: str,
    current_facts: dict[str, Any],
    candidates: list[dict[str, Any]],
    active_names: list[str],
    reference: date,
) -> tuple[dict[str, Any] | None, str]:
    current_mentions = current_facts.get("date_mentions") or []
    ordinal = _extract_ordinal(current_text)

    if operation == "reschedule" and len(current_mentions) > 1:
        normalized = _normalize(current_text)
        if not re.search(r"\b(?:vers|a la place|au lieu|plutot|nouvelle date|pour passer)\b", normalized):
            return None, "Plusieurs periodes sont mentionnees sans cible de report explicite"
        current_mentions = [current_mentions[-1]]

    if ordinal is not None:
        last_options = _last_cab_options(candidates)
        if ordinal == -1 and last_options:
            return last_options[-1], ""
        if 0 <= ordinal < len(last_options):
            return last_options[ordinal], ""
        return None, "La session designee par son ordre ne peut pas etre identifiee de facon unique"

    if current_mentions:
        ranked: list[tuple[int, dict[str, Any]]] = []
        current_centres = set(current_facts.get("centres") or [])
        for candidate in candidates:
            score = _candidate_match_score(candidate, current_mentions, current_centres, active_names)
            if score > 0:
                ranked.append((score, candidate))
        if ranked:
            max_score = max(score for score, _ in ranked)
            best = _dedupe_candidates([candidate for score, candidate in ranked if score == max_score])
            if len(best) == 1:
                return best[0], ""
            return None, "Plusieurs sessions historiques correspondent a la date mentionnee"

        # A complete, previously unmentioned range is a valid new target.
        if len(current_mentions) == 1:
            mention = current_mentions[0]
            if mention["explicit_range"]:
                return {
                    "start_date": mention["start_date"],
                    "end_date": mention["end_date"],
                    "centre": current_facts.get("centre") or "",
                    "candidate_names": active_names,
                    "source_thread_ids": [],
                }, ""

    scoped = [
        candidate for candidate in candidates
        if not active_names or not candidate.get("candidate_names")
        or set(candidate.get("candidate_names") or []) & set(active_names)
    ]
    if operation == "revert_original":
        selected = [candidate for candidate in scoped if candidate.get("selected")]
        pool = selected or scoped
        distinct = _dedupe_candidates(pool)
        if not distinct:
            return None, "La session initiale n'apparait pas clairement dans l'historique"
        earliest_time = min(float(candidate.get("timestamp") or 0) for candidate in distinct)
        earliest = _dedupe_candidates([
            candidate for candidate in distinct
            if float(candidate.get("timestamp") or 0) == earliest_time
        ])
        if len(earliest) == 1:
            return earliest[0], ""
        return None, "Plusieurs sessions initiales sont possibles dans l'historique"

    if operation in {"select_session", "availability_check", "reschedule"}:
        latest = _latest_cab_candidates(scoped)
        distinct = _dedupe_candidates(latest)
        if len(distinct) == 1:
            return distinct[0], ""
        if len(distinct) > 1:
            return None, "Plusieurs sessions recentes sont possibles; la date ou le centre doit etre precise"

    return None, ""


def _extract_message_facts(
    text: str,
    reference: date,
    known_centres: Iterable[str],
) -> dict[str, Any]:
    centre_mentions = _extract_centres(text, known_centres)
    name_mentions = _extract_candidate_names(text)
    return {
        "formation_type": _extract_formation(text),
        "categories": _extract_categories(text),
        "type_ir": _extract_type_ir(text),
        "nb_candidates": _extract_candidate_count(text),
        "candidate_names": list(dict.fromkeys(name for _, name in name_mentions)),
        "candidate_name_mentions": name_mentions,
        "centres": list(dict.fromkeys(value for _, value in centre_mentions)),
        "centre": centre_mentions[0][1] if len({value for _, value in centre_mentions}) == 1 else "",
        "centre_mentions": centre_mentions,
        "date_mentions": _extract_date_mentions(text, reference),
    }


def _extract_date_mentions(text: str, reference: date) -> list[dict[str, Any]]:
    normalized = _normalize(text)
    mentions: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []

    patterns = [
        (
            re.compile(
                r"\b(?P<d1>\d{1,2})[/-](?P<m1>\d{1,2})(?:[/-](?P<y1>\d{2,4}))?\s*"
                r"(?:au|a|jusqu au|-)\s*(?P<d2>\d{1,2})[/-](?P<m2>\d{1,2})(?:[/-](?P<y2>\d{2,4}))?\b"
            ),
            "numeric_range",
        ),
        (
            re.compile(
                r"\b(?P<d1>\d{1,2})\s*(?:au|a|-)\s*(?P<d2>\d{1,2})[/-](?P<m2>\d{1,2})"
                r"(?:[/-](?P<y2>\d{2,4}))?\b"
            ),
            "compact_range",
        ),
        (
            re.compile(
                rf"\b(?P<d1>\d{{1,2}})\s*(?:au|a|-)\s*(?P<d2>\d{{1,2}})\s+"
                rf"(?P<month>{'|'.join(MONTHS)})(?:\s+(?P<year>\d{{4}}))?\b"
            ),
            "word_range",
        ),
    ]

    for pattern, kind in patterns:
        for match in pattern.finditer(normalized):
            if _span_overlaps(match.span(), occupied):
                continue
            if kind == "numeric_range":
                start = _make_date(match["d1"], match["m1"], match["y1"], reference)
                end_reference = start or reference
                end = _make_date(match["d2"], match["m2"], match["y2"] or match["y1"], end_reference)
                year_explicit = bool(match["y1"] or match["y2"])
            elif kind == "compact_range":
                start = _make_date(match["d1"], match["m2"], match["y2"], reference)
                end = _make_date(match["d2"], match["m2"], match["y2"], start or reference)
                year_explicit = bool(match["y2"])
            else:
                month = MONTHS[match["month"]]
                start = _make_date(match["d1"], month, match["year"], reference)
                end = _make_date(match["d2"], month, match["year"], start or reference)
                year_explicit = bool(match["year"])
            if start and end and start <= end:
                mentions.append(_date_mention(
                    start,
                    end,
                    match.start(),
                    explicit_range=True,
                    year_explicit=year_explicit,
                ))
                occupied.append(match.span())

    single_patterns = [
        re.compile(r"(?<!\d[/-])\b(?P<d>\d{1,2})[/-](?P<m>\d{1,2})(?:[/-](?P<y>\d{2,4}))?\b"),
        re.compile(
            rf"\b(?P<d>\d{{1,2}})(?:er)?\s+(?P<month>{'|'.join(MONTHS)})"
            rf"(?:\s+(?P<year>\d{{4}}))?\b"
        ),
    ]
    for pattern in single_patterns:
        for match in pattern.finditer(normalized):
            if _span_overlaps(match.span(), occupied):
                continue
            if "m" in match.groupdict():
                value = _make_date(match["d"], match["m"], match.groupdict().get("y"), reference)
                year_explicit = bool(match.groupdict().get("y"))
            else:
                value = _make_date(match["d"], MONTHS[match["month"]], match.groupdict().get("year"), reference)
                year_explicit = bool(match.groupdict().get("year"))
            if value:
                mentions.append(_date_mention(
                    value,
                    value,
                    match.start(),
                    explicit_range=False,
                    year_explicit=year_explicit,
                ))
                occupied.append(match.span())
    return sorted(mentions, key=lambda item: item["position"])


def _date_mention(
    start: date,
    end: date,
    position: int,
    explicit_range: bool,
    year_explicit: bool,
) -> dict[str, Any]:
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "position": position,
        "explicit_range": explicit_range,
        "year_explicit": year_explicit,
    }


def _make_date(day: Any, month: Any, year: Any, reference: date) -> date | None:
    try:
        day_value = int(day)
        month_value = int(month)
        if year not in (None, ""):
            year_value = int(year)
            if year_value < 100:
                year_value += 2000
        else:
            year_value = reference.year
            tentative = date(year_value, month_value, day_value)
            if (reference - tentative).days > 120:
                year_value += 1
        return date(year_value, month_value, day_value)
    except (TypeError, ValueError):
        return None


def _extract_centres(text: str, known_centres: Iterable[str]) -> list[tuple[int, str]]:
    normalized = _normalize(text)
    matches: list[tuple[int, str]] = []
    for centre in known_centres:
        value = _normalize(centre)
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(value)}(?![a-z0-9])", normalized):
            matches.append((match.start(), str(centre)))
    return sorted(matches)


def _extract_candidate_names(text: str) -> list[tuple[int, str]]:
    matches: list[tuple[int, str]] = []
    patterns = (
        r"\b(?i:m(?:me|lle)?\.?)[\s]*(?P<name>[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý' -]{3,60})",
        r"\b(?i:candidat|stagiaire|participant|collaborateur|bdc)\s+(?i:de\s+|: ?)?"
        r"(?P<name>[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý' -]{3,60})",
    )
    stop_words = {"CACES", "CAB", "FORMATION", "INITIAL", "HERBLAY", "VILLABE"}
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            raw = re.split(r"\b(?:PREVU|PRÉVU|POUR|DU|EN|A|À|SI|SESSION)\b", match["name"], maxsplit=1)[0]
            words = re.findall(r"[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý'-]+", raw)
            if len(words) < 2:
                continue
            name = " ".join(words[:4]).strip()
            if name and not set(_normalize(name).upper().split()) & stop_words:
                matches.append((match.start("name"), name))
    return list(dict.fromkeys(matches))


def _extract_formation(text: str) -> str:
    formations = _extract_formations(text)
    return formations[0] if len(formations) == 1 else ""


def _extract_formations(text: str) -> list[str]:
    mentions: list[tuple[int, str]] = []
    offset = 0
    for clause in _relevant_clauses(text, r"\bcaces\b|\br\s?(?:482|484|485|486|489|490)\b|\bsst\b|habilitation|\baipr\b"):
        normalized = _effective_request_segment(
            clause,
            r"\bcaces\b|\br\s?(?:482|484|485|486|489|490)\b|\bsst\b|habilitation|\baipr\b",
        )
        clause_mentions: list[tuple[int, str]] = []
        for match in re.finditer(r"\br\s?(482|484|485|486|489|490)\b", normalized):
            if not _mention_is_negated(normalized, match.start()):
                clause_mentions.append((match.start(), f"CACES R{match.group(1)}"))
        if not any(value.startswith("CACES R") for _, value in clause_mentions):
            caces_match = re.search(r"\bcaces\b", normalized)
            if caces_match and not _mention_is_negated(normalized, caces_match.start()):
                if "nacelle" in normalized or "pemp" in normalized:
                    value = "CACES R486"
                elif "chariot" in normalized or "cariste" in normalized:
                    value = "CACES R489"
                else:
                    value = "CACES"
                clause_mentions.append((caces_match.start(), value))
        for pattern, value in (
            (r"\bsst\b", "SST"),
            (r"\bhabilitation\b.{0,25}\belect", "Habilitation electrique"),
            (r"\baipr\b", "AIPR"),
        ):
            for match in re.finditer(pattern, normalized):
                if not _mention_is_negated(normalized, match.start()):
                    clause_mentions.append((match.start(), value))
        mentions.extend((offset + position, value) for position, value in clause_mentions)
        offset += len(clause) + 1
    return list(dict.fromkeys(value for _, value in sorted(mentions)))


def _extract_categories(text: str) -> list[str]:
    zones: list[str] = []
    token = r"(?:[A-G][1-3]|[1-7][AB]?|[A-G])"
    expression = rf"{token}(?:\s*(?:ET|&|,|/|\+)\s*{token})*"
    for clause in _relevant_clauses(text, r"\bcategor|\bcaces\b|\br\s?(?:482|484|485|486|489|490)\b"):
        effective = _effective_request_segment(
            clause,
            rf"\bcategor|^\s*{expression}(?:\s|$)",
        )
        normalized = effective.upper()
        for pattern in (
            rf"CATEGOR(?:IE|IES)?\s*[: -]?({expression})",
            r"(?:CACES\s+)?R\s?(?:482|484|485|486|489|490)\s+"
            rf"({expression})"
            r"(?=\s+(?:EN\s+)?(?:INITIAL|RECYCLAGE)\b)",
            rf"CACES\s+({expression})\s+(?:NACELLE|CHARIOT|EN\s+INITIAL|EN\s+RECYCLAGE)",
        ):
            match = re.search(pattern, normalized)
            if match:
                zones.append(match.group(1))
        if not zones and effective != _normalize(clause) and re.search(r"\bcategor", _normalize(clause)):
            corrected = re.match(rf"\s*({expression})\b", normalized)
            if corrected:
                zones.append(corrected.group(1))
    categories: list[str] = []
    for zone in zones:
        for category in re.findall(r"\b(?:[A-G][1-3]|[1-7][AB]?|[A-G])\b", zone):
            if category not in categories:
                categories.append(category)
    return _filter_categories_for_formation(categories[:6], _extract_formation(text))


def _filter_categories_for_formation(categories: list[str], formation: str) -> list[str]:
    match = re.search(r"R(482|484|485|486|489|490)", formation, flags=re.IGNORECASE)
    if not match:
        return categories
    allowed = {
        "482": {"A", "B1", "B2", "B3", "C1", "C2", "C3", "D", "E", "F", "G"},
        "484": {"1", "2"},
        "485": {"1", "2"},
        "486": {"A", "B", "C"},
        "489": {"1A", "1B", "2A", "2B", "3", "4", "5", "6", "7"},
    }.get(match.group(1))
    if not allowed:
        return categories
    return [category for category in categories if category in allowed]


def _extract_type_ir(text: str) -> str:
    values = _extract_type_values(text)
    return values[0] if len(values) == 1 else ""


def _extract_type_values(text: str) -> list[str]:
    mentions: list[tuple[int, str]] = []
    offset = 0
    for clause in _relevant_clauses(text, r"\brecyclage\b|\binitiale?\b"):
        normalized = _effective_request_segment(clause, r"\brecyclage\b|\binitiale?\b")
        without_date_initial = re.sub(r"\b(?:date|session|periode)\s+initiale?\b", "", normalized)
        for pattern, value in (
            (r"\b(?:en\s+)?recyclage\b", "recyclage"),
            (r"\b(?:formation\s+initiale?|en\s+initial|initial\s+pour)\b", "initial"),
        ):
            for match in re.finditer(pattern, without_date_initial):
                if not _mention_is_negated(without_date_initial, match.start()):
                    mentions.append((offset + match.start(), value))
        offset += len(clause) + 1
    return list(dict.fromkeys(value for _, value in sorted(mentions)))


def _extract_candidate_count(text: str) -> int | None:
    values = _extract_candidate_counts(text)
    return values[0] if len(values) == 1 else None


def _extract_candidate_counts(text: str) -> list[int]:
    number_pattern = "|".join(NUMBER_WORDS)
    values = []
    for clause in _relevant_clauses(
        text,
        r"\b(?:candidats?|stagiaires?|participants?|collaborateurs?|salaries?|personnes?|interimaires?)\b",
    ):
        normalized = _effective_request_segment(
            clause,
            r"\b(?:candidats?|stagiaires?|participants?|collaborateurs?|salaries?|personnes?|interimaires?)\b",
        )
        for match in re.finditer(
            rf"\b(?P<number>\d{{1,3}}|{number_pattern})\s+"
            r"(?:candidats?|stagiaires?|participants?|collaborateurs?|salaries?|personnes?|interimaires?)\b",
            normalized,
        ):
            if _mention_is_negated(normalized, match.start()):
                continue
            raw = match["number"]
            value = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw)
            if value and 0 < value <= 500 and value not in values:
                values.append(value)
    return values


def _effective_request_segment(text: str, target_pattern: str = "") -> str:
    normalized = _normalize(text)
    cut_positions = []
    for match in re.finditer(r"\b(?:mais|plutot|finalement)\b", normalized):
        if not target_pattern or re.search(target_pattern, normalized[match.end():]):
            cut_positions.append(match.end())
    replacement = re.search(r"\bremplac\w*\b.{0,100}\bpar\b", normalized)
    if replacement and (
        not target_pattern or re.search(target_pattern, normalized[replacement.end():])
    ):
        cut_positions.append(replacement.end())
    return normalized[max(cut_positions):].strip() if cut_positions else normalized


def _mention_is_negated(text: str, position: int) -> bool:
    prefix = text[max(0, position - 45):position]
    return bool(re.search(
        r"\b(?:pas|non|ni|sans|plus)\s+(?:de\s+|d\s+)?$"
        r"|\bne\b.{0,30}\bplus\s+(?:de\s+|d\s+)?$",
        prefix,
    ))


def _relevant_clauses(text: str, pattern: str) -> list[str]:
    normalized = _normalize(text)
    clauses = [part.strip() for part in re.split(r"[.!?;\n]+", normalized) if part.strip()]
    relevant = [clause for clause in clauses if re.search(pattern, clause)]
    return relevant or [normalized]


def _candidate_match_score(
    candidate: dict[str, Any],
    mentions: list[dict[str, Any]],
    current_centres: set[str],
    active_names: list[str],
) -> int:
    start = _as_date(candidate.get("start_date"))
    end = _as_date(candidate.get("end_date"))
    if not start or not end:
        return 0
    score = 0
    for mention in mentions:
        requested_start = _as_date(mention.get("start_date"))
        requested_end = _as_date(mention.get("end_date"))
        if not requested_start or not requested_end:
            continue
        if mention.get("year_explicit"):
            exact_range = start == requested_start and end == requested_end
            same_start = start == requested_start
            same_end = end == requested_start
            contains = start <= requested_start <= end
        else:
            exact_range = (
                (start.month, start.day) == (requested_start.month, requested_start.day)
                and (end.month, end.day) == (requested_end.month, requested_end.day)
            )
            same_start = (start.month, start.day) == (requested_start.month, requested_start.day)
            same_end = (end.month, end.day) == (requested_start.month, requested_start.day)
            if start.year == end.year:
                probe = date(start.year, requested_start.month, requested_start.day)
                contains = start <= probe <= end
            else:
                contains = (start.month, start.day) <= (requested_start.month, requested_start.day) or (
                    requested_start.month,
                    requested_start.day,
                ) <= (end.month, end.day)
        if exact_range:
            score = max(score, 140)
        elif same_start:
            score = max(score, 110)
        elif same_end:
            score = max(score, 45)
        elif contains:
            score = max(score, 30)
    if not score:
        return 0
    if current_centres:
        if candidate.get("centre") in current_centres:
            score += 20
        elif candidate.get("centre"):
            score -= 50
    names = set(candidate.get("candidate_names") or [])
    if active_names and names:
        score += 15 if names & set(active_names) else -40
    return max(score, 0)


def _enrich_duplicate_candidates(candidates: list[dict[str, Any]]) -> None:
    by_range: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for candidate in candidates:
        key = (str(candidate.get("start_date") or ""), str(candidate.get("end_date") or ""))
        by_range.setdefault(key, []).append(candidate)
    for group in by_range.values():
        centres = {str(item.get("centre") or "") for item in group if item.get("centre")}
        names = list(dict.fromkeys(
            name for item in group for name in (item.get("candidate_names") or [])
        ))
        explicit_counts = {
            int(item["nb_candidates"])
            for item in group
            if item.get("count_explicit") and item.get("nb_candidates")
        }
        group_count = next(iter(explicit_counts)) if len(explicit_counts) == 1 else None
        if group_count is None and names and not explicit_counts:
            group_count = len(names)
        for item in group:
            if len(centres) == 1 and not item.get("centre"):
                item["centre"] = next(iter(centres))
            if names:
                item["candidate_names"] = names
            if group_count is not None:
                item["nb_candidates"] = group_count
            source_ids = list(item.get("source_thread_ids") or [])
            for peer in group:
                source_ids.extend(peer.get("source_thread_ids") or [])
            item["source_thread_ids"] = list(dict.fromkeys(source_ids))


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    keys: set[tuple[Any, ...]] = set()
    for candidate in sorted(candidates, key=lambda item: float(item.get("timestamp") or 0)):
        key = (
            candidate.get("start_date"),
            candidate.get("end_date"),
            candidate.get("centre") or "",
            tuple(candidate.get("candidate_names") or []),
        )
        if key not in keys:
            keys.add(key)
            result.append(candidate)
    return result


def _latest_candidate(candidates: list[dict[str, Any]], active_names: list[str]) -> dict[str, Any] | None:
    scoped = [
        item for item in candidates
        if not active_names or not item.get("candidate_names")
        or set(item.get("candidate_names") or []) & set(active_names)
    ]
    return max(scoped, key=lambda item: float(item.get("timestamp") or 0), default=None)


def _latest_cab_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cab = [candidate for candidate in candidates if candidate.get("role") == "CAB"]
    if not cab:
        return []
    latest_time = max(float(candidate.get("timestamp") or 0) for candidate in cab)
    return [candidate for candidate in cab if float(candidate.get("timestamp") or 0) == latest_time]


def _last_cab_options(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cab = [candidate for candidate in candidates if candidate.get("role") == "CAB"]
    if not cab:
        return []
    latest_with_multiple = 0.0
    grouped: dict[float, list[dict[str, Any]]] = {}
    for candidate in cab:
        grouped.setdefault(float(candidate.get("timestamp") or 0), []).append(candidate)
    for timestamp, values in grouped.items():
        if len(_dedupe_candidates(values)) > 1:
            latest_with_multiple = max(latest_with_multiple, timestamp)
    timestamp = latest_with_multiple or max(grouped)
    return _dedupe_candidates(grouped[timestamp])


def _latest_candidate_names(entries: list[dict[str, Any]]) -> list[str]:
    for entry in reversed(entries):
        names = [name for _, name in _extract_candidate_names(str(entry.get("text") or ""))]
        if names:
            return list(dict.fromkeys(names))
    return []


def _inherit_static_facts(
    facts: dict[str, Any],
    static: dict[str, Any],
    source_ids: list[str],
) -> dict[str, Any]:
    result = dict(facts)
    static_facts = static.get("facts") or {}
    static_sources = static.get("sources") or {}
    result_formation = str(result.get("formation_type") or "")
    static_formation = str(static_facts.get("formation_type") or "")
    if (
        result_formation
        and static_formation
        and not _formations_compatible(result_formation, static_formation)
    ):
        return result
    for key in ("formation_type", "categories", "type_ir", "nb_candidates"):
        value = static_facts.get(key)
        if result.get(key) in (None, "", []) and value not in (None, "", []):
            result[key] = list(value) if key == "categories" else value
            if static_sources.get(key):
                source_ids.append(str(static_sources[key]))
    if result.get("categories"):
        result["categories"] = _filter_categories_for_formation(
            list(result["categories"]),
            str(result.get("formation_type") or ""),
        )
    return result


def _prefer_specific_formation(current: Any, new: str) -> str:
    current_value = str(current or "")
    if re.search(r"R\d{3}", new):
        return new
    return current_value or new


def _formations_compatible(first: str, second: str) -> bool:
    first_normalized = _normalize(first)
    second_normalized = _normalize(second)
    if first_normalized == second_normalized:
        return True
    first_code = re.search(r"r\s?(\d{3})", first_normalized)
    second_code = re.search(r"r\s?(\d{3})", second_normalized)
    if first_code and second_code:
        return first_code.group(1) == second_code.group(1)
    return "caces" in first_normalized and "caces" in second_normalized


def _message_has_session_evidence(text: str, facts: dict[str, Any]) -> bool:
    if facts.get("formation_type") or facts.get("candidate_names"):
        return True
    normalized = _normalize(text)
    return bool(re.search(
        r"\b(?:formation|session|caces|sst|habilitation|aipr|nacelle|chariot|"
        r"inscri\w*|convocation|disponibil\w*|prevu\w*|recyclage|initiale?)\b",
        normalized,
    ))


def _date_has_session_evidence(
    text: str,
    mention: dict[str, Any],
    facts: dict[str, Any],
    has_active_formation: bool,
) -> bool:
    normalized = _normalize(text)
    position = int(mention.get("position") or 0)
    window = normalized[max(0, position - 120):position + 140]
    date_prefix = normalized[max(0, position - 70):position]
    if re.search(
        r"\b(?:date\s+d?\s*emission|emise?|emission|echeance|paiement|reglement|"
        r"facturee?|facturation|signee?|signature)\b[^\d]{0,25}$",
        date_prefix,
    ):
        return False
    administrative = re.search(
        r"\b(?:facture|facturation|echeance|paiement|reglement|emise|emission|signature|"
        r"convention|document|bon de commande|bdc)\b",
        window,
    )
    strong_session_evidence = re.search(
        r"\b(?:session|disponibil\w*|inscri\w*|convocation|prevu\w*|programme\w*)\b"
        r"|\bformation\b.{0,35}\b(?:du|au|le|prevu|programme)\b",
        window,
    )
    administrative_override = re.search(
        r"\b(?:session|disponibil\w*|inscri\w*|convocation|prevu\w*|programme\w*)\b",
        window,
    )
    if administrative and not administrative_override:
        return False
    if strong_session_evidence or facts.get("candidate_names"):
        return True
    if mention.get("explicit_range") and (facts.get("formation_type") or has_active_formation):
        return True
    if facts.get("centres") and has_active_formation:
        return True
    return _message_has_session_evidence(text, facts)


def _required_missing_fields(facts: dict[str, Any]) -> list[str]:
    missing = []
    if not facts.get("formation_type"):
        missing.append("formation_type")
    if not facts.get("centre"):
        missing.append("centre")
    if not facts.get("start_date") or not facts.get("end_date"):
        missing.append("dates")
    if not facts.get("nb_candidates"):
        missing.append("nb_candidates")
    formation = str(facts.get("formation_type") or "").lower()
    if "caces" in formation or re.search(r"\br\s?(?:482|484|485|486|489|490)\b", formation):
        if not facts.get("categories"):
            missing.append("categories")
        if facts.get("type_ir") not in {"initial", "recyclage"}:
            missing.append("type_ir")
        if not facts.get("nombre_jours_souhaites"):
            missing.append("nombre_jours_souhaites")
    return missing


def _message_selects_session(text: str, role: str) -> bool:
    if role != "CLIENT":
        return False
    normalized = _normalize(text)
    return bool(re.search(
        r"\b(?:inscri(?:re|vez)|je confirme|nous confirmons|je retiens|nous retenons|"
        r"je choisis|nous choisissons|c est ok|ce sera|maintenir|garder)\b",
        normalized,
    ))


def _extract_ordinal(text: str) -> int | None:
    normalized = _normalize(text)
    for value, words in enumerate((
        ("premiere", "premier", "1ere", "1er"),
        ("deuxieme", "seconde", "second", "2eme"),
        ("troisieme", "3eme"),
    )):
        if any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in words):
            return value
    if re.search(r"\bderniere\b", normalized):
        return -1
    return None


def _nearest_value(position: int, values: list[tuple[int, str]]) -> str:
    if not values:
        return ""
    before = [item for item in values if item[0] <= position]
    after = [item for item in values if item[0] > position]
    preceding = max(before, default=None, key=lambda item: item[0])
    following = min(after, default=None, key=lambda item: item[0])
    if following and following[0] - position <= 80:
        if not preceding or following[0] - position < position - preceding[0]:
            return following[1]
    return preceding[1] if preceding else (following[1] if following else "")


def _nearest_names(position: int, values: list[tuple[int, str]]) -> list[str]:
    if not values:
        return []
    distances = [(abs(item_position - position), name) for item_position, name in values]
    minimum = min(distance for distance, _ in distances)
    return list(dict.fromkeys(name for distance, name in distances if distance == minimum))


def _span_overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < existing[1] and existing[0] < span[1] for existing in occupied)


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _entry_reference_date(entry: dict[str, Any]) -> date:
    raw = str(entry.get("created_time") or "")
    if raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    timestamp = float(entry.get("timestamp") or 0)
    if timestamp > 0:
        return datetime.fromtimestamp(timestamp).date()
    return _today_reference()


def _today_reference() -> date:
    return datetime.now().date()


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value or "")
    without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    without_accents = without_accents.lower().replace("’", "'")
    return re.sub(r"[ \t]+", " ", without_accents)


def _resolution_reason(status: str, operation: str, missing: list[str]) -> str:
    if status == "resolved":
        return "Contexte de session reconstitue depuis des echanges concordants"
    if status == "incomplete":
        return f"Contexte de session incomplet: {', '.join(missing) or 'session cible'}"
    if status == "not_required":
        return f"Aucune verification de disponibilite requise pour l'operation {operation}"
    return ""
