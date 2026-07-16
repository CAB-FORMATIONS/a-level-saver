import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agents.relations_response_agent import RelationsResponseAgent  # noqa: E402
from src.utils.relations_response_validator import validate_relations_response  # noqa: E402


FALLBACK = (
    'Bonjour,<br><br>Votre demande a bien ete recue.<br><br>'
    "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
)


def response_data(**overrides):
    data = {
        'subject': 'Demande de devis CACES R489',
        'message': 'Bonjour, pouvez-vous etablir un devis pour deux personnes ?',
        'conversation': '[CLIENT]\nDemande initiale',
        'triage': {
            'intent': 'DEMANDE_DEVIS_FORMATION',
            'request_mode': 'new_request',
            'confidence': 0.95,
            'extracted': {'formation_type': 'CACES R489', 'nb_candidates': 2},
            'missing_fields': ['centre', 'dates'],
            'internal_secret': 'must-not-leak',
        },
        'crm_context': {
            'classification': 'client_crm',
            'contact_name': 'Alice Martin',
            'account_name': 'Entreprise Test',
            'deals': [{'id': 'secret-deal-id', 'Amount': 999}],
        },
        'attachments': {'has_attachments': False, 'names': []},
        'fallback_response': FALLBACK,
    }
    data.update(overrides)
    return data


def test_agent_returns_structured_contextual_response_and_sanitizes_context():
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
        'response_html': (
            'Bonjour Alice,<br><br>Pour preparer le devis CACES R489, pouvez-vous nous '
            'confirmer le centre et les dates souhaites ?<br><br>Cordialement,<br>'
            "L'equipe Relations entreprises CAB Formations"
        ),
        'requires_human_action': False,
        'human_action_reason': '',
    }))])
    agent = RelationsResponseAgent(client=client)

    result = agent.process(response_data())

    assert result['used_ai'] is True
    assert result['requires_human_action'] is False
    assert 'Bonjour Alice' in result['response_html']
    request = client.messages.create.call_args.kwargs
    payload = json.loads(request['messages'][0]['content'])
    assert payload['triage']['intent'] == 'DEMANDE_DEVIS_FORMATION'
    assert 'internal_secret' not in payload['triage']
    assert 'deals' not in payload['identite_crm']
    assert 'secret-deal-id' not in request['messages'][0]['content']


def test_agent_parses_json_code_fence_and_human_action_flag():
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text=(
        '```json\n'
        '{"response_html":"Bonjour,<br><br>Nous verifions la prise en compte.<br><br>'
        'Cordialement,<br>L equipe Relations entreprises CAB Formations",'
        '"requires_human_action":"true",'
        '"human_action_reason":"Confirmer les inscriptions dans la plateforme interne"}\n'
        '```'
    ))])
    agent = RelationsResponseAgent(client=client)

    result = agent.process(response_data())

    assert result['used_ai'] is True
    assert result['requires_human_action'] is True
    assert result['human_action_reason'] == 'Confirmer les inscriptions dans la plateforme interne'


def test_agent_removes_vague_deadline_from_generated_email():
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
        'response_html': (
            'Bonjour,<br><br>Nous vous repondrons dans les meilleurs delais.<br>'
            'Notre conseiller vous le fera parvenir rapidement.<br><br>'
            'Notre conseiller vous adressera ce document directement.<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        ),
        'requires_human_action': True,
        'human_action_reason': 'Verifier le dossier',
    }))])
    agent = RelationsResponseAgent(client=client)

    result = agent.process(response_data())

    assert 'meilleurs delais' not in result['response_html']
    assert 'Nous vous repondrons.' in result['response_html']
    assert 'fera parvenir' not in result['response_html']
    assert 'vous adressera' not in result['response_html']


def test_agent_uses_deterministic_fallback_on_api_failure():
    client = Mock()
    client.messages.create.side_effect = RuntimeError('Anthropic unavailable')
    agent = RelationsResponseAgent(client=client)

    result = agent.process(response_data())

    assert result['used_ai'] is False
    assert result['response_html'] == FALLBACK
    assert result['requires_human_action'] is True
    assert 'Anthropic unavailable' in result['fallback_reason']


def test_validator_rejects_unverified_amount_and_attachment_claim():
    response = (
        'Bonjour,<br><br>Vous trouverez ci-joint notre devis de 500 EUR.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(response, {'intent': 'DEMANDE_DEVIS_FORMATION'}, None)

    assert validation['valid'] is False
    assert any('Montant chiffre' in error for error in validation['errors'])
    assert any('Piece jointe' in error for error in validation['errors'])


def test_validator_requires_dates_from_deterministic_source():
    source = (
        'Bonjour,<br><br>Session proposee le 21/09/2026.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    response = (
        'Bonjour,<br><br>Une session est disponible en septembre.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
        None,
        source_response_html=source,
    )

    assert validation['valid'] is False
    assert any('2026-09-21' in error for error in validation['errors'])


def test_validator_warns_on_unverified_vague_deadline():
    response = (
        'Bonjour,<br><br>Nous reviendrons vers vous tres prochainement.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(response, {'intent': 'AUTRE_A_QUALIFIER'}, None)

    assert validation['valid'] is True
    assert any('Promesse de delai' in warning for warning in validation['warnings'])


def test_validator_rejects_script_invented_date_and_false_confirmation():
    response = (
        'Bonjour,<br><script>alert(1)</script><br>'
        "L'inscription est bien enregistree pour le 31/12/2030.<br><br>"
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(
        response,
        {'intent': 'INSCRIPTION_CANDIDATS'},
        None,
        allowed_source_text='Demande recue le 21/09/2026.',
    )

    assert validation['valid'] is False
    assert any('HTML' in error for error in validation['errors'])
    assert any('Inscription confirmee' in error for error in validation['errors'])
    assert any('2030-12-31' in error for error in validation['errors'])


def test_validator_requires_complete_relations_signature():
    response = 'Bonjour,<br><br>Merci pour votre message.<br><br>Cordialement,'

    validation = validate_relations_response(response, {'intent': 'AUTRE_A_QUALIFIER'}, None)

    assert validation['valid'] is False
    assert 'Signature Relations entreprises absente' in validation['errors']


def test_validator_requires_confirmation_for_defaulted_training_assumptions():
    triage = {
        'intent': 'DEMANDE_DISPONIBILITE_SESSION',
        'defaulted_fields': ['nb_candidates', 'type_ir'],
    }
    without_confirmation = (
        'Bonjour,<br><br>Nous recherchons une formation initiale pour un candidat.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    with_confirmation = (
        'Bonjour,<br><br>Voici notre proposition.<br><br>'
        "Merci de nous confirmer qu'il s'agit bien d'une formation initiale pour un seul candidat."
        "<br><br>Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    invalid = validate_relations_response(without_confirmation, triage, None)
    valid = validate_relations_response(with_confirmation, triage, None)

    assert invalid['valid'] is False
    assert any('initial' in error for error in invalid['errors'])
    assert any('candidat unique' in error for error in invalid['errors'])
    assert valid['valid'] is True

    alternative_confirmation = (
        'Bonjour,<br><br>Voici notre proposition.<br><br>'
        "Pourriez-vous egalement nous confirmer qu'il s'agit bien d'une formation initiale "
        "pour un seul candidat ?<br><br>Cordialement,<br>"
        "L'equipe Relations entreprises CAB Formations"
    )
    assert validate_relations_response(alternative_confirmation, triage, None)['valid'] is True


def test_validator_rejects_unverified_document_promise_and_new_candidate_requirements():
    response = (
        'Bonjour,<br><br>Notre conseiller va vous faire parvenir le document.<br><br>'
        "Nous aurons besoin des nom et prenom du stagiaire pour etablir la convention."
        "<br><br>Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(response, {'intent': 'DEMANDE_DEVIS_FORMATION'}, None)

    assert validation['valid'] is False
    assert any('Transmission de document' in error for error in validation['errors'])
    assert any('Informations candidat' in error for error in validation['errors'])

    passive_promise = (
        'Bonjour,<br><br>Un devis va etre prepare et vous sera transmis directement.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    passive_validation = validate_relations_response(
        passive_promise,
        {'intent': 'DEMANDE_DEVIS_FORMATION'},
        None,
    )
    assert passive_validation['valid'] is False

    in_progress_promise = (
        'Bonjour,<br><br>La modification est en cours de traitement. Nous revenons vers toi '
        'des que la session est confirmee pour te transmettre la convocation.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    progress_validation = validate_relations_response(
        in_progress_promise,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION', 'session_operation': 'revert_original'},
        {'status': 'skipped'},
    )
    assert progress_validation['valid'] is False
    assert any('en cours' in error or 'Transmission' in error for error in progress_validation['errors'])

    future_verification = (
        'Bonjour,<br><br>Cette modification n\'est pas encore enregistree. Notre equipe va '
        'verifier la disponibilite et revient vers toi pour confirmation.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    assert validate_relations_response(
        future_verification,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION', 'session_operation': 'revert_original'},
        {'status': 'skipped'},
    )['valid'] is False


def test_validator_rejects_convention_or_session_claim_absent_from_sources():
    response = (
        'Bonjour,<br><br>Nous pourrons etablir la convention. Une session disponible est '
        'prevue en septembre.<br><br>Cordialement,<br>'
        "L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DEVIS_FORMATION'},
        {'status': 'error'},
        source_response_html='Bonjour, demande de devis.',
        allowed_source_text='Le client demande un tarif.',
    )

    assert validation['valid'] is False
    assert any('Convention' in error for error in validation['errors'])
    assert any('Disponibilite' in error for error in validation['errors'])


def test_validator_rejects_alternative_false_confirmations():
    responses = [
        "Nous avons enregistre votre inscription.",
        "Nous avons valide votre inscription.",
        "Votre participation est confirmee.",
        "Votre annulation est prise en compte.",
        "Votre report est confirme.",
        "Votre absence a ete enregistree.",
        "Nous avons valide votre convention.",
    ]

    for body in responses:
        response = (
            f'Bonjour,<br><br>{body}<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        )
        validation = validate_relations_response(response, {'intent': 'INSCRIPTION_CANDIDATS'}, None)
        assert validation['valid'] is False, body


def test_validator_requires_complete_planbot_result_for_availability_claim():
    claims = [
        'La session est disponible.',
        'Nous vous confirmons la disponibilite de la session.',
        'Nous avons une place disponible.',
        'Nous pouvons vous proposer une session la semaine prochaine.',
        'Une session est possible le 21/09/2026.',
    ]
    for claim in claims:
        response = (
            f'Bonjour,<br><br>{claim}<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        )
        without_planbot = validate_relations_response(
            response,
            {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
            None,
        )
        with_planbot = validate_relations_response(
            response,
            {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
            {'direct': {'coverage_complete': True, 'verdict': 'disponible'}},
        )

        assert without_planbot['valid'] is False, claim
        assert with_planbot['valid'] is True, claim


def test_validator_rejects_caces_availability_without_valid_sequence():
    response = (
        'Bonjour,<br><br>Nous pouvons vous proposer une session.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    planbot_result = {
        'status': 'ok',
        'formation': 'CACES R486',
        'semaines': [{
            'dispo_reelle': True,
            'jours': [{'date': '2026-09-21', 'formation_theorie': [{}]}],
        }],
    }

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
        planbot_result,
    )

    assert validation['valid'] is False
    assert any('PlanBot' in error for error in validation['errors'])


def test_planbot_dates_are_allowed_in_safe_fallback_validation():
    response = (
        'Bonjour,<br><br>Option disponible le 21/09/2026.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
        {'direct': {'coverage_complete': True, 'verdict': 'disponible'}},
        source_response_html=response,
        allowed_source_text='Le client demande une session en septembre.',
    )

    assert validation['valid'] is True


def test_exact_session_claim_requires_direct_not_alternative_availability():
    response = (
        'Bonjour,<br><br>La session est disponible le 22/07/2026.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    triage = {
        'intent': 'DEMANDE_DISPONIBILITE_SESSION',
        'session_operation': 'revert_original',
    }
    alternatives_only = {
        'direct': {'status': 'ok', 'formation': 'CACES R486', 'verdict': 'complet'},
        'alternative_dates': {
            'status': 'ok',
            'formation': 'CACES R486',
            'sequence_valide': True,
            'sequence_options': [{
                'start': '2026-07-27',
                'end': '2026-07-29',
            }],
        },
    }

    validation = validate_relations_response(
        response,
        triage,
        alternatives_only,
        allowed_source_text='Session demandee le 22/07/2026.',
    )

    assert validation['valid'] is False
    assert any('Disponibilite' in error for error in validation['errors'])


def test_two_day_caces_complete_coverage_is_valid_direct_availability():
    response = (
        'Bonjour,<br><br>La session est disponible du 21/09/2026 au 22/09/2026.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    triage = {
        'intent': 'DEMANDE_DISPONIBILITE_SESSION',
        'session_operation': 'availability_check',
    }
    direct = {
        'status': 'ok',
        'formation': 'CACES R485',
        'coverage_complete': True,
        'periode': '2026-09-21 -> 2026-09-22',
    }

    validation = validate_relations_response(
        response,
        triage,
        direct,
        allowed_source_text='Demande du 21/09/2026 au 22/09/2026.',
    )

    assert validation['valid'] is True


def test_validator_rejects_reverse_order_action_taken_claim():
    response = (
        'Bonjour,<br><br>Nous avons bien pris en compte votre inscription.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )

    validation = validate_relations_response(
        response,
        {'intent': 'INSCRIPTION_CANDIDATS'},
        None,
    )

    assert validation['valid'] is False
    assert any('prise en compte' in error for error in validation['errors'])

    completed = (
        'Bonjour,<br><br>Votre inscription a bien ete effectuee.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    assert validate_relations_response(
        completed,
        {'intent': 'INSCRIPTION_CANDIDATS'},
        None,
    )['valid'] is False

    for claim in (
        'Le candidat est maintenant inscrit.',
        'Nous avons procede a votre inscription.',
        'Votre inscription est terminee.',
        'Le candidat a bien ete ajoute a la session.',
        'Votre inscription est complete.',
        'Jean est bien inscrit.',
    ):
        response = (
            f'Bonjour,<br><br>{claim}<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        )
        assert validate_relations_response(
            response,
            {'intent': 'INSCRIPTION_CANDIDATS'},
            None,
        )['valid'] is False, claim


def test_new_request_alternative_does_not_validate_requested_date_claim():
    response = (
        'Bonjour,<br><br>La session du 21/09/2026 est disponible.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    planbot_result = {
        'direct': {
            'status': 'ok',
            'formation': 'CACES R486',
            'verdict': 'complet',
        },
        'alternative_dates': {
            'status': 'ok',
            'formation': 'CACES R486',
            'sequence_valide': True,
            'sequence_options': [{
                'start': '2026-09-28',
                'end': '2026-09-30',
            }],
        },
    }

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION', 'request_mode': 'new_request'},
        planbot_result,
        allowed_source_text='Demande initiale le 21/09/2026.',
    )

    assert validation['valid'] is False
    assert any('Disponibilite' in error for error in validation['errors'])


def test_caces_explicit_invalid_sequence_overrides_coverage_flag():
    response = (
        'Bonjour,<br><br>La session est disponible du 21/09/2026 au 22/09/2026.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    contradictory = {
        'status': 'ok',
        'formation': 'CACES R485',
        'coverage_complete': True,
        'sequence_valide': False,
    }

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
        contradictory,
        allowed_source_text='Demande du 21/09/2026 au 22/09/2026.',
    )

    assert validation['valid'] is False


def test_date_availability_claim_is_validated_like_session_claim():
    response = (
        'Bonjour,<br><br>La date du 21/09/2026 est disponible.<br><br>'
        "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
    )
    alternatives_only = {
        'direct': {'status': 'ok', 'formation': 'SST', 'verdict': 'complet'},
        'alternative_dates': {
            'status': 'ok',
            'formation': 'SST',
            'coverage_complete': True,
            'periode': '2026-09-28 -> 2026-09-29',
        },
    }

    validation = validate_relations_response(
        response,
        {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
        alternatives_only,
        allowed_source_text='Demande du 21/09/2026.',
    )

    assert validation['valid'] is False
    assert any('Disponibilite' in error for error in validation['errors'])

    for claim in (
        'Nous avons une disponibilite le 21/09/2026.',
        'Il reste des disponibilites le 21/09/2026.',
        'Le 21/09/2026 est libre.',
    ):
        candidate_response = (
            f'Bonjour,<br><br>{claim}<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        )
        candidate_validation = validate_relations_response(
            candidate_response,
            {'intent': 'DEMANDE_DISPONIBILITE_SESSION'},
            alternatives_only,
            allowed_source_text='Demande du 21/09/2026.',
        )
        assert candidate_validation['valid'] is False, claim
