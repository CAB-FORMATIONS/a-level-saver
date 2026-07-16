import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.relations_session_history import (  # noqa: E402
    detect_session_operation,
    reconstruct_session_context,
)


CENTRES = ("Tremblay", "Herblay", "Villabe", "Bois d'Arcy")


def entry(identifier, timestamp, role, text, created_time="2026-07-08T10:00:00+02:00"):
    return {
        "id": identifier,
        "timestamp": timestamp,
        "created_time": created_time,
        "role": role,
        "text": text,
    }


def candidate_history(current_text, current_id="current"):
    return [
        entry(
            "request",
            1,
            "CLIENT",
            "Formation CACES A&B nacelle en initial pour deux collaborateurs a Villabe ou Herblay.",
        ),
        entry(
            "options",
            2,
            "CAB",
            "CACES R486 A et B en initial. VILLABE: du 22/07 au 24/07, puis du 27/07 au 29/07.",
        ),
        entry(
            "registration",
            3,
            "CLIENT",
            "Peux-tu inscrire M. ALPHA TESTEUR du 22 au 24 juillet 2026 a Villabe, "
            "et M. BETA TESTEUR en septembre a Herblay ?",
        ),
        entry(
            "purchase-order",
            4,
            "CAB",
            "Merci de transmettre le BDC de ALPHA TESTEUR, prevu du 22/07 au 24/07.",
        ),
        entry(
            "change-proposal",
            5,
            "CAB",
            "J'ai de la disponibilite du 20/07 au 22/07 sur Villabe. Peut-on rapprocher la formation ?",
        ),
        entry(
            "change-accepted",
            6,
            "CLIENT",
            "C'est OK pour le 20, peux-tu transmettre la nouvelle convocation ?",
        ),
        entry(current_id, 7, "CLIENT", current_text, "2026-07-16T12:29:34+02:00"),
    ]


def test_revert_to_initial_date_reconstructs_complete_planbot_context():
    result = reconstruct_session_context(
        candidate_history("Finalement on revient a la date initiale stp 22/07."),
        "current",
        CENTRES,
    )

    assert result["operation"] == "revert_original"
    assert result["status"] == "resolved"
    assert result["should_check_availability"] is True
    assert result["facts"] == {
        "formation_type": "CACES R486",
        "centre": "Villabe",
        "start_date": "2026-07-22",
        "end_date": "2026-07-24",
        "nb_candidates": 1,
        "categories": ["A", "B"],
        "type_ir": "initial",
        "candidate_names": ["ALPHA TESTEUR"],
        "nombre_jours_souhaites": 3,
        "financement": "B2B",
    }


def test_revert_without_repeating_date_uses_unique_original_selection():
    result = reconstruct_session_context(
        candidate_history("Annule le changement, garde finalement la date initiale."),
        "current",
        CENTRES,
    )

    assert result["status"] == "resolved"
    assert result["facts"]["start_date"] == "2026-07-22"
    assert result["facts"]["end_date"] == "2026-07-24"


def test_accepting_latest_offer_rechecks_that_exact_session():
    history = candidate_history("placeholder")[:-2]
    history.append(entry(
        "current",
        6,
        "CLIENT",
        "C'est OK pour le 20, peux-tu transmettre la nouvelle convocation ?",
        "2026-07-16T12:23:01+02:00",
    ))

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["operation"] == "select_session"
    assert result["status"] == "resolved"
    assert result["facts"]["start_date"] == "2026-07-20"
    assert result["facts"]["end_date"] == "2026-07-22"
    assert result["facts"]["candidate_names"] == ["ALPHA TESTEUR"]


def test_explicit_reschedule_overrides_stale_history_facts():
    history = candidate_history("placeholder")[:4]
    history.append(entry(
        "current",
        5,
        "CLIENT",
        "Decale la formation du 1 au 3 septembre 2026 a Herblay en recyclage pour 3 candidats.",
        "2026-07-16T12:00:00+02:00",
    ))

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["operation"] == "reschedule"
    assert result["status"] == "resolved"
    assert result["facts"]["formation_type"] == "CACES R486"
    assert result["facts"]["centre"] == "Herblay"
    assert result["facts"]["start_date"] == "2026-09-01"
    assert result["facts"]["end_date"] == "2026-09-03"
    assert result["facts"]["nb_candidates"] == 3
    assert result["facts"]["type_ir"] == "recyclage"


def test_revert_preserves_recycling_and_does_not_confuse_date_initiale():
    history = [
        entry("request", 1, "CLIENT", "CACES R486 categories A et B en recyclage pour un candidat."),
        entry("selected", 2, "CLIENT", "Je souhaite inscrire M. JEAN DUPONT du 22 au 24 juillet 2026 a Villabe."),
        entry("current", 3, "CLIENT", "Retour a la date initiale du 22/07.", "2026-07-16T12:00:00+02:00"),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["status"] == "resolved"
    assert result["facts"]["type_ir"] == "recyclage"


def test_multiple_original_sessions_without_selector_are_ambiguous():
    history = [
        entry("request", 1, "CLIENT", "CACES R486 categories A et B en initial pour deux candidats."),
        entry(
            "selected",
            2,
            "CLIENT",
            "Inscrire M. JEAN DUPONT du 22 au 24 juillet 2026 a Villabe et "
            "M. PAUL MARTIN du 27 au 29 juillet 2026 a Villabe.",
        ),
        entry("current", 3, "CLIENT", "Finalement retour a la date initiale.", "2026-07-16T12:00:00+02:00"),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["status"] == "ambiguous"
    assert result["should_check_availability"] is False


def test_same_day_month_across_two_years_is_ambiguous_without_year():
    history = [
        entry("request", 1, "CLIENT", "CACES R486 categories A et B en initial pour un candidat."),
        entry("old", 2, "CAB", "Session du 21/09/2025 au 23/09/2025 a Villabe."),
        entry("new", 3, "CAB", "Session du 21/09/2026 au 23/09/2026 a Villabe."),
        entry("current", 4, "CLIENT", "Je retiens le 21/09.", "2026-07-16T12:00:00+02:00"),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["status"] == "ambiguous"
    assert result["should_check_availability"] is False


def test_ordinal_selection_resolves_one_option_from_latest_proposal():
    history = [
        entry("request", 1, "CLIENT", "CACES R486 categories A et B en initial pour un candidat."),
        entry(
            "options",
            2,
            "CAB",
            "A Villabe: du 22/07/2026 au 24/07/2026 ou du 27/07/2026 au 29/07/2026.",
        ),
        entry("current", 3, "CLIENT", "Je retiens la deuxieme proposition.", "2026-07-16T12:00:00+02:00"),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["status"] == "resolved"
    assert result["facts"]["start_date"] == "2026-07-27"
    assert result["facts"]["end_date"] == "2026-07-29"


def test_cancellation_absence_removal_and_status_confirmation_never_check_capacity():
    messages = {
        "Annule la formation de Jean Dupont.": "cancel",
        "Jean Dupont sera absent a cette session.": "absence",
        "Retire le candidat Jean Dupont de la session.": "candidate_removal",
        "Peux-tu confirmer que son inscription est prise en compte ?": "status_confirmation",
    }
    for message, expected_operation in messages.items():
        history = candidate_history(message)
        result = reconstruct_session_context(history, "current", CENTRES)
        assert result["operation"] == expected_operation, message
        assert result["should_check_availability"] is False, message


def test_reporting_and_date_to_follow_are_not_misread_as_reschedules():
    assert detect_session_operation("Le reporting sera transmis demain.") == "unknown"
    assert detect_session_operation("Je reviens vers vous demain pour la date.") == "date_pending"


def test_unrelated_invoice_date_is_not_reused_as_a_session():
    history = [
        entry("invoice", 1, "CAB", "Votre facture a ete emise le 10/07/2026."),
        entry(
            "current",
            2,
            "CLIENT",
            "Avez-vous des disponibilites SST a Herblay pour deux candidats ?",
            "2026-07-16T12:00:00+02:00",
        ),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["candidate_count"] == 0
    assert result["status"] == "incomplete"
    assert "dates" in result["missing_fields"]
    assert result["should_check_availability"] is False

    training_invoice = [
        entry(
            "invoice",
            1,
            "CAB",
            "Facture de la formation SST emise le 10/07/2026, echeance le 10/08/2026.",
        ),
        history[-1],
    ]
    result = reconstruct_session_context(training_invoice, "current", CENTRES)
    assert result["candidate_count"] == 0
    assert result["should_check_availability"] is False

    session_invoice = [
        entry(
            "invoice",
            1,
            "CAB",
            "Facture de la session SST, date emission : 10/09/2026.",
        ),
        history[-1],
    ]
    result = reconstruct_session_context(session_invoice, "current", CENTRES)
    assert result["candidate_count"] == 0
    assert result["should_check_availability"] is False


def test_reschedule_from_old_range_to_new_range_uses_destination_only():
    history = candidate_history("placeholder")[:4]
    history.append(entry(
        "current",
        5,
        "CLIENT",
        "Decale la formation du 22 au 24 juillet 2026 vers du 1 au 3 septembre 2026 "
        "a Herblay en recyclage pour 3 candidats.",
        "2026-07-16T12:00:00+02:00",
    ))

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["status"] == "resolved"
    assert result["facts"]["start_date"] == "2026-09-01"
    assert result["facts"]["end_date"] == "2026-09-03"
    assert result["facts"]["centre"] == "Herblay"


def test_two_named_candidates_on_same_session_keep_capacity_two():
    history = [
        entry("request", 1, "CLIENT", "CACES R486 categories A et B en initial."),
        entry(
            "selected",
            2,
            "CLIENT",
            "Inscrire M. JEAN DUPONT et M. PAUL MARTIN du 22 au 24 juillet 2026 a Villabe.",
        ),
        entry("current", 3, "CLIENT", "Je confirme le 22/07.", "2026-07-16T12:00:00+02:00"),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["status"] == "resolved"
    assert result["facts"]["candidate_names"] == ["JEAN DUPONT", "PAUL MARTIN"]
    assert result["facts"]["nb_candidates"] == 2


def test_preposition_and_candidate_count_are_not_caces_categories():
    history = [
        entry(
            "current",
            1,
            "CLIENT",
            "Avez-vous toujours de la disponibilite CACES R489 a Herblay du "
            "21/09/2026 au 23/09/2026 en initial pour 2 candidats ?",
            "2026-07-16T12:00:00+02:00",
        ),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["facts"].get("categories") in (None, [])
    assert "categories" in result["missing_fields"]
    assert result["should_check_availability"] is False


def test_categories_stop_before_candidate_count():
    history = [
        entry(
            "current",
            1,
            "CLIENT",
            "Avez-vous de la disponibilite CACES R486 categories A et B en initial "
            "a Herblay du 21/09/2026 au 23/09/2026 pour 2 candidats ?",
            "2026-07-16T12:00:00+02:00",
        ),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["facts"]["categories"] == ["A", "B"]
    assert result["facts"]["nb_candidates"] == 2


def test_static_facts_do_not_cross_into_a_different_formation_epoch():
    history = [
        entry(
            "old-request",
            1,
            "CLIENT",
            "CACES R486 categories A et B en initial pour un candidat.",
        ),
        entry(
            "old-option",
            2,
            "CAB",
            "Session CACES R486 du 22/07/2026 au 24/07/2026 a Villabe.",
        ),
        entry(
            "new-option",
            3,
            "CAB",
            "Session CACES R489 du 01/09/2026 au 03/09/2026 a Herblay pour un candidat.",
        ),
        entry(
            "current",
            4,
            "CLIENT",
            "Je confirme la session du 01/09/2026.",
            "2026-07-16T12:00:00+02:00",
        ),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["facts"]["formation_type"] == "CACES R489"
    assert result["facts"].get("categories") in (None, [])
    assert result["facts"].get("type_ir") in (None, "")
    assert {"categories", "type_ir"}.issubset(result["missing_fields"])
    assert result["should_check_availability"] is False


def test_r482_compound_categories_are_preserved():
    history = [
        entry(
            "current",
            1,
            "CLIENT",
            "Avez-vous toujours de la disponibilite CACES R482 categories B1 et C1 "
            "en initial a Herblay du 21/09/2026 au 23/09/2026 pour un candidat ?",
            "2026-07-16T12:00:00+02:00",
        ),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["facts"]["categories"] == ["B1", "C1"]
    assert result["status"] == "resolved"


def test_revert_does_not_inherit_facts_from_later_formation_epoch():
    history = [
        entry("old-request", 1, "CLIENT", "CACES R486 en initial pour un candidat."),
        entry(
            "old-session",
            2,
            "CLIENT",
            "Inscrire M. JEAN DUPONT du 22/07/2026 au 24/07/2026 a Villabe.",
        ),
        entry(
            "new-request",
            3,
            "CLIENT",
            "CACES R489 categories 3 et 5 en initial pour un candidat.",
        ),
        entry(
            "new-session",
            4,
            "CAB",
            "Session CACES R489 du 01/09/2026 au 03/09/2026 a Herblay.",
        ),
        entry(
            "current",
            5,
            "CLIENT",
            "Finalement retour a la date initiale du 22/07/2026.",
            "2026-07-16T12:00:00+02:00",
        ),
    ]

    result = reconstruct_session_context(history, "current", CENTRES)

    assert result["facts"]["formation_type"] == "CACES R486"
    assert result["facts"].get("categories") in (None, [])
    assert "categories" in result["missing_fields"]
    assert result["should_check_availability"] is False
