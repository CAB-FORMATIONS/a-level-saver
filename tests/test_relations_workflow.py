import sys
from pathlib import Path
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.relations_response_builder import (  # noqa: E402
    _result_has_availability,
    build_relations_response,
)
from src.agents.relations_triage_agent import RelationsTriageAgent  # noqa: E402
from src.workflows.relations_ticket_workflow import RelationsTicketWorkflow  # noqa: E402


EXTERNAL_EMAIL = 'client@example.com'
ACCOUNT_MANAGER_EMAIL = 'manager@cab-formations.fr'
ACCOUNT_MANAGER_DESK_ID = '198709000000000001'


def workflow_without_clients():
    workflow = object.__new__(RelationsTicketWorkflow)
    workflow.desk_client = Mock()
    workflow.crm_lookup = Mock()
    workflow.triage_agent = Mock()
    workflow.response_agent = Mock()
    workflow.planbot_client = Mock()
    return workflow


def inbound_thread(**overrides):
    thread = {
        'id': 'customer-message',
        'direction': 'in',
        'status': 'SUCCESS',
        'createdTime': '2026-07-15T10:00:00.000Z',
        'fromEmailAddress': f'"Client"<{EXTERNAL_EMAIL}>',
        'to': 'relations.entreprises@cab-formations.fr',
        'plainText': 'Bonjour,\n\nVeuillez trouver notre bon de commande.\n\nCordialement,',
        'attachmentCount': '1',
    }
    thread.update(overrides)
    return thread


def safe_triage(**overrides):
    triage = {
        'action': 'DRAFT',
        'intent': 'BON_DE_COMMANDE',
        'confidence': 0.95,
        'reason': 'Bon de commande recu',
        'request_mode': 'document_submission',
        'extracted': {},
        'missing_fields': [],
    }
    triage.update(overrides)
    return triage


def configure_process(workflow, threads):
    workflow.desk_client.get_ticket.return_value = {
        'id': 'ticket-1',
        'subject': 'Bon de commande',
        'departmentId': '198709000027921097',
        'email': EXTERNAL_EMAIL,
        'assigneeId': 'previous-agent',
    }
    workflow.desk_client.has_existing_draft.return_value = False
    workflow.desk_client.has_existing_draft_strict.return_value = False
    workflow.desk_client.get_all_threads_with_full_content.return_value = threads
    workflow.desk_client.list_ticket_threads.return_value = threads
    workflow.crm_lookup.lookup_sender.return_value = {
        'classification': 'client_crm',
        'contact_name': 'Alice Martin',
        'account_name': 'Entreprise Test',
        'account_id': 'crm-account-1',
        'account': {'id': 'crm-account-1', 'Account_Name': 'Entreprise Test'},
        'account_owner': {
            'id': 'crm-owner-1',
            'name': 'Manager Test',
            'email': ACCOUNT_MANAGER_EMAIL,
        },
    }
    workflow.desk_client.list_agents.return_value = [{
        'id': ACCOUNT_MANAGER_DESK_ID,
        'name': 'Manager Test',
        'emailId': ACCOUNT_MANAGER_EMAIL,
        'status': 'ACTIVE',
        'isConfirmed': True,
        'associatedDepartmentIds': ['198709000027921097'],
    }]
    workflow.desk_client.update_ticket.return_value = {'assigneeId': ACCOUNT_MANAGER_DESK_ID}
    workflow.triage_agent.process.return_value = safe_triage()
    workflow.response_agent.process.return_value = {
        'response_html': (
            'Bonjour,<br><br>Merci pour votre bon de commande. '
            'Nous en accusons reception.<br><br>Cordialement,<br>'
            "L'equipe Relations entreprises CAB Formations"
        ),
        'used_ai': True,
        'requires_human_action': False,
        'human_action_reason': '',
    }


def test_latest_customer_message_ignores_internal_mirror_and_keeps_current_reply():
    workflow = workflow_without_clients()
    threads = [
        inbound_thread(
            id='internal-mirror',
            createdTime='2026-07-15T10:02:00.000Z',
            fromEmailAddress='Relations entreprises <relations.entreprises@cab-formations.fr>',
            plainText='Notre propre reponse dupliquee.',
        ),
        inbound_thread(
            plainText=(
                'Bonjour,\n\nVoici la nouvelle convention signee.\n'
                'Le reporting sera envoye ce jour.\n\n'
                'Le mer. 15 juil. 2026 a 10:47, Relations\n Entreprises a ecrit :\n'
                'Ancien message cite.'
            ),
        ),
    ]

    message = workflow._latest_customer_message(threads)

    assert 'nouvelle convention signee' in message
    assert 'reporting sera envoye' in message
    assert 'Ancien message cite' not in message
    assert 'propre reponse' not in message


def test_latest_customer_thread_compares_timezone_offsets_chronologically():
    workflow = workflow_without_clients()
    older = inbound_thread(
        id='older',
        createdTime='2026-07-15T10:00:00+02:00',
        plainText='Ancien message.',
    )
    newer = inbound_thread(
        id='newer',
        createdTime='2026-07-15T09:00:00Z',
        plainText='Nouveau message.',
    )

    assert workflow._latest_customer_thread([older, newer])['id'] == 'newer'


def test_process_skips_when_an_external_reply_is_newer_than_customer_message():
    workflow = workflow_without_clients()
    threads = [
        inbound_thread(),
        {
            'id': 'human-reply',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-15T10:01:00.000Z',
            'fromEmailAddress': 'relations.entreprises@cab-formations.fr',
            'to': EXTERNAL_EMAIL,
            'plainText': 'Bonjour, votre demande a ete traitee.',
        },
        inbound_thread(
            id='internal-mirror',
            createdTime='2026-07-15T10:01:01.000Z',
            fromEmailAddress='relations.entreprises@cab-formations.fr',
            plainText='Bonjour, votre demande a ete traitee.',
        ),
    ]
    configure_process(workflow, threads)

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['workflow_stage'] == 'SKIPPED_ALREADY_REPLIED'
    workflow.crm_lookup.lookup_sender.assert_not_called()
    workflow.response_agent.process.assert_not_called()


def test_reply_to_another_external_recipient_does_not_hide_customer_request():
    workflow = workflow_without_clients()
    threads = [
        inbound_thread(),
        {
            'id': 'unrelated-reply',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-15T10:01:00.000Z',
            'fromEmailAddress': 'relations.entreprises@cab-formations.fr',
            'to': 'other-client@example.com',
            'plainText': 'Message adresse a un autre interlocuteur.',
        },
    ]
    configure_process(workflow, threads)

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['workflow_stage'] == 'DRAFT_DELIVERY'
    workflow.response_agent.process.assert_called_once()


def test_process_passes_clean_current_message_to_response_agent():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread(plainText=(
        'Bonjour,\n\nVoici notre bon de commande.\n\n'
        'Le mer. 15 juil. 2026 a 09:00, CAB a ecrit :\nAncienne reponse.'
    ))])

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['success'] is True
    assert result['workflow_stage'] == 'DRAFT_DELIVERY'
    payload = workflow.response_agent.process.call_args.args[0]
    assert payload['message'] == 'Bonjour,\n\nVoici notre bon de commande.'
    assert payload['attachments']['has_attachments'] is True
    assert result['response_generation']['used_ai'] is True


def test_dry_run_resolves_account_manager_without_assigning_ticket():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['assignment']['ready'] is True
    assert result['assignment']['desk_agent_id'] == ACCOUNT_MANAGER_DESK_ID
    assert result['assignment']['would_change'] is True
    workflow.desk_client.update_ticket.assert_not_called()


def test_missing_account_owner_blocks_response_generation():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])
    workflow.crm_lookup.lookup_sender.return_value['account_owner'] = None

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['workflow_stage'] == 'STOPPED_ACCOUNT_MANAGER_UNRESOLVED'
    assert 'gestionnaire' in result['skip_reason'].lower()
    workflow.response_agent.process.assert_not_called()


def test_inactive_or_unassociated_desk_agent_blocks_draft():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])
    agent = workflow.desk_client.list_agents.return_value[0]
    agent['status'] = 'DISABLED'

    inactive = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert inactive['workflow_stage'] == 'STOPPED_ACCOUNT_MANAGER_UNRESOLVED'
    assert 'inactif' in inactive['skip_reason']

    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])
    workflow.desk_client.list_agents.return_value[0]['associatedDepartmentIds'] = ['other-department']

    unassociated = workflow.process_ticket('ticket-2', ignore_existing_draft=True)

    assert unassociated['workflow_stage'] == 'STOPPED_ACCOUNT_MANAGER_UNRESOLVED'
    assert 'Relations entreprises' in unassociated['skip_reason']


def test_invalid_ai_response_falls_back_to_safe_deterministic_draft():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])
    workflow.response_agent.process.return_value = {
        'response_html': (
            'Bonjour,<br><br>Vous trouverez ci-joint le devis de 500 EUR.<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        ),
        'used_ai': True,
        'requires_human_action': False,
        'human_action_reason': '',
    }

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['success'] is True
    assert result['response_generation']['used_ai'] is False
    assert '500 EUR' not in result['draft_content']
    assert result['validation']['valid'] is True


def test_invalid_ai_response_is_retried_with_validation_feedback():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])
    workflow.response_agent.process.side_effect = [
        {
            'response_html': (
                'Bonjour,<br><br>Vous trouverez ci-joint le devis de 500 EUR.<br><br>'
                "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
            ),
            'used_ai': True,
            'requires_human_action': False,
            'human_action_reason': '',
        },
        {
            'response_html': (
                'Bonjour,<br><br>Nous accusons reception de votre message concernant '
                'le bon de commande.<br><br>Cordialement,<br>'
                "L'equipe Relations entreprises CAB Formations"
            ),
            'used_ai': True,
            'requires_human_action': True,
            'human_action_reason': 'Verifier le document recu',
        },
    ]

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['response_generation']['used_ai'] is True
    assert result['response_generation']['attempts'] == 2
    assert '500 EUR' not in result['draft_content']
    retry_payload = workflow.response_agent.process.call_args_list[1].args[0]
    assert retry_payload['validation_errors']


def test_training_fallback_has_no_placeholders_or_duplicate_date_question():
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'CACES R489'},
        missing_fields=['dates', 'start_date', 'end_date', 'centre'],
    )

    response = build_relations_response(triage, {}, None)

    assert 'XXX' not in response
    assert response.count('la date ou periode souhaitee') == 1
    assert 'finaliser le devis' not in response


def test_confirmation_request_keeps_questions_but_does_not_call_planbot():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='INSCRIPTION_CANDIDATS',
        request_mode='confirmation_request',
        missing_fields=['centre', 'dates', 'nb_candidates'],
    )

    workflow._enforce_planbot_missing_fields(triage)

    assert triage['missing_fields'] == ['centre', 'dates', 'nb_candidates']
    assert workflow._should_call_planbot(triage) is False


def test_caces_planbot_call_requires_categories_and_duration():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R489',
            'centre': 'Tremblay',
            'start_date': '2026-09-21',
            'end_date': '2026-09-23',
            'nb_candidates': 2,
            'categories': [],
            'nombre_jours_souhaites': None,
        },
        missing_fields=[],
    )

    assert workflow._should_call_planbot(triage) is False
    triage['extracted']['categories'] = ['3', '5']
    triage['extracted']['nombre_jours_souhaites'] = 3
    assert workflow._should_call_planbot(triage) is False
    triage['extracted']['type_ir'] = 'initial'
    assert workflow._should_call_planbot(triage) is True


def test_bare_r486_is_treated_as_caces_and_requires_exact_categories_and_type():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'R486',
            'centre': 'Tremblay',
            'start_date': '2026-09-21',
            'end_date': '2026-09-23',
            'nb_candidates': 1,
            'categories': [],
            'nb_categories': 2,
            'type_ir': '',
            'nombre_jours_souhaites': 3,
        },
        missing_fields=[],
    )

    assert workflow._should_call_planbot(triage) is False
    triage['extracted']['categories'] = ['A', 'B']
    assert workflow._should_call_planbot(triage) is False
    triage['extracted']['type_ir'] = 'initial'
    assert workflow._should_call_planbot(triage) is True


def test_deterministic_recommendation_overrides_vague_llm_formation():
    agent = object.__new__(RelationsTriageAgent)

    result = agent._normalize(
        {
            'intent': 'DEMANDE_DEVIS_FORMATION',
            'extracted': {
                'formation_type': 'PEMP',
                'categories': [],
            },
            'missing_fields': [],
        },
        'Demande CACES R486 categories A et B',
        'Nous souhaitons former un interimaire.',
        EXTERNAL_EMAIL,
    )

    assert result['extracted']['formation_type'] == 'CACES R486'
    assert result['extracted']['categories'] == ['A', 'B']


def test_followup_recovers_training_facts_but_respects_date_promised_later():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='follow_up',
        extracted={},
        missing_fields=['dates', 'nb_candidates'],
    )
    message = 'Avez-vous le tarif detaille ? Je reviens vers vous demain pour la date.'
    conversation = (
        '[CLIENT] Nous souhaitons former un interimaire au CACES R486 categories A et B. '
        'Avez-vous des disponibilites ?'
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        message,
        conversation,
    )
    workflow._apply_training_defaults(triage)

    assert triage.get('planbot_search_mode') is None
    assert triage['extracted']['formation_type'] == 'CACES R486'
    assert triage['extracted']['categories'] == ['A', 'B']
    assert triage['extracted']['centre'] == "Bois d'Arcy"
    assert triage['extracted']['nb_candidates'] == 1
    assert triage['extracted']['type_ir'] == 'initial'
    assert triage['defaulted_fields'] == ['type_ir']
    assert triage['date_will_follow'] is True

    workflow._enforce_planbot_missing_fields(triage, has_previous_cab=True)
    response = build_relations_response(triage, {}, None)
    assert 'dates' not in triage['missing_fields']
    assert "Nous restons dans" in response
    assert "attente de votre retour" in response


def test_next_session_search_defaults_to_initial_and_tracks_confirmation():
    workflow = workflow_without_clients()
    message = (
        'Nous souhaitons former un interimaire au CACES R486 categories A et B. '
        'Auriez-vous des disponibilites et un devis ?'
    )
    triage = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': '',
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'type_ir': '',
        },
        missing_fields=['centre', 'dates', 'nombre_jours_souhaites'],
    )
    crm_context = {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"}

    workflow._prepare_planbot_search_context(triage, crm_context, message, '')
    workflow._apply_training_defaults(triage)
    source = f"{message}\n{crm_context['account_name']}"
    workflow._sanitize_extracted_facts(triage, source, date_source_text=message)
    workflow._enforce_planbot_missing_fields(triage)

    assert triage['planbot_search_mode'] == 'next_sessions'
    assert triage['extracted']['centre'] == "Bois d'Arcy"
    assert triage['extracted']['nb_candidates'] == 1
    assert triage['extracted']['type_ir'] == 'initial'
    assert triage['defaulted_fields'] == ['type_ir']
    assert triage['missing_fields'] == []
    assert workflow._select_planbot_action(triage, source) == 'search_alternative_dates'


def test_training_defaults_one_candidate_and_initial_and_requests_confirmation():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': "Bois d'Arcy",
            'categories': ['A', 'B'],
        },
        missing_fields=[],
    )

    workflow._apply_training_defaults(triage)
    response = build_relations_response(triage, {}, None)

    assert triage['extracted']['nb_candidates'] == 1
    assert triage['extracted']['type_ir'] == 'initial'
    assert triage['defaulted_fields'] == ['nb_candidates', 'type_ir']
    assert "formation initiale pour un seul candidat" in response


def test_explicit_candidate_count_and_recycling_are_not_defaulted():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'nb_candidates': 2,
            'type_ir': 'recyclage',
        },
    )

    workflow._apply_training_defaults(triage)

    assert triage.get('defaulted_fields') == []
    assert triage['extracted']['nb_candidates'] == 2
    assert triage['extracted']['type_ir'] == 'recyclage'


def test_vague_requested_period_is_not_replaced_by_next_sessions_search():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'SST'},
        missing_fields=[],
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        'Avez-vous des disponibilites en septembre ?',
        '',
    )

    assert triage.get('planbot_search_mode') is None

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        'Avez-vous des disponibilites dans deux semaines ?',
        '',
    )
    assert triage.get('planbot_search_mode') is None


def test_negative_or_historical_centre_does_not_override_verified_crm_centre():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'SST', 'centre': 'Tremblay'},
        missing_fields=[],
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        'Nous cherchons les prochaines disponibilites mais pas Tremblay.',
        '[CLIENT] Ancienne demande pour Tremblay.',
    )

    assert triage['extracted']['centre'] == "Bois d'Arcy"

    triage['extracted']['centre'] = "Bois d'Arcy"
    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        "Nous cherchons des disponibilites, mais pas Bois d'Arcy.",
        '',
    )
    assert triage['extracted']['centre'] == ''

    triage['extracted']['centre'] = 'Tremblay'
    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        'Nous cherchons des disponibilites, mais pas sur Tremblay.',
        '',
    )
    assert triage['extracted']['centre'] == "Bois d'Arcy"


def test_next_session_search_calls_alternative_dates_without_client_period_or_duration():
    workflow = workflow_without_clients()
    message = (
        'Nous souhaitons une formation initiale pour un interimaire au CACES R486 '
        'categories A et B. Quelles sont vos prochaines disponibilites ?'
    )
    triage = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': '',
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'type_ir': 'initial',
            'nombre_jours_souhaites': None,
        },
        missing_fields=[],
    )
    crm_context = {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"}

    workflow._prepare_planbot_search_context(triage, crm_context, message, '')
    workflow._apply_training_defaults(triage)
    source = f"{message}\n{crm_context['account_name']}"
    workflow._sanitize_extracted_facts(triage, source, date_source_text=message)
    workflow._enforce_planbot_missing_fields(triage)
    action = workflow._select_planbot_action(triage, source)
    payload = workflow._build_planbot_payload(triage, action=action)

    assert action == 'search_alternative_dates'
    assert payload['centre'] == "Bois d'Arcy"
    assert payload['categories'] == ['A', 'B']
    assert payload['type_ir'] == 'initial'
    assert payload['direction'] == 'after'
    assert payload['nb_weeks'] == 6
    assert 'start_date' not in payload
    assert 'end_date' not in payload
    assert 'nombre_jours_souhaites' not in payload


def test_alternative_planbot_weeks_are_rendered_as_session_proposals():
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': "Bois d'Arcy",
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'type_ir': 'initial',
        },
        missing_fields=[],
    )
    planbot_result = {
        'status': 'ok',
        'semaines': [{
            'start': '2026-08-24',
            'end': '2026-08-28',
            'dispo_reelle': True,
            'sequence_valide': True,
            'options': [{
                'start': '2026-08-24',
                'end': '2026-08-26',
                'dates': ['2026-08-24', '2026-08-25', '2026-08-26'],
            }],
        }],
    }

    response = build_relations_response(triage, {}, planbot_result)

    assert 'Sessions identifiees' in response
    assert 'du 24/08/2026 au 26/08/2026' in response
    assert 'Merci de nous confirmer la session' in response


def test_multi_candidate_planbot_lots_are_presented_as_one_required_plan():
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': "Bois d'Arcy",
            'nb_candidates': 5,
            'categories': ['A', 'B'],
            'type_ir': 'initial',
        },
        missing_fields=[],
    )
    planbot_result = {
        'status': 'ok',
        'formation': 'CACES R486',
        'semaines': [{
            'dispo_reelle': True,
            'sequence_valide': True,
            'options': [
                {
                    'start': '2026-09-07',
                    'end': '2026-09-09',
                    'nb_candidates': 3,
                    'lot_required': True,
                },
                {
                    'start': '2026-09-14',
                    'end': '2026-09-16',
                    'nb_candidates': 2,
                    'lot_required': True,
                },
            ],
        }],
    }

    response = build_relations_response(triage, {}, planbot_result)

    assert 'tous les lots ci-dessous sont necessaires' in response
    assert 'Lot requis de 3 candidats' in response
    assert 'Lot requis de 2 candidats' in response
    assert 'confirmer si ce plan de repartition vous convient' in response


def test_workflow_calls_alternative_dates_and_uses_planbot_proposal():
    workflow = workflow_without_clients()
    message = (
        'Nous souhaitons une formation initiale pour un interimaire au CACES R486 '
        'categories A et B. Quelles sont vos prochaines disponibilites ?'
    )
    configure_process(workflow, [inbound_thread(plainText=message, attachmentCount='0')])
    workflow.crm_lookup.lookup_sender.return_value['account_name'] = "ENTREPRISE TEST 78390 BOIS-D'ARCY"
    workflow.triage_agent.process.return_value = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': '',
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'nb_categories': 2,
            'type_ir': 'initial',
            'nombre_jours_souhaites': None,
        },
        missing_fields=[],
    )
    workflow.planbot_client.check_availability.return_value = {
        'status': 'ok',
        'semaines': [{
            'start': '2026-08-24',
            'end': '2026-08-28',
            'dispo_reelle': True,
            'sequence_valide': True,
            'options': [{
                'start': '2026-08-24',
                'end': '2026-08-26',
                'dates': ['2026-08-24', '2026-08-25', '2026-08-26'],
            }],
        }],
    }
    workflow.response_agent.process.side_effect = lambda data: {
        'response_html': data['fallback_response'],
        'used_ai': False,
        'requires_human_action': False,
        'human_action_reason': '',
    }

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['planbot_action'] == 'search_alternative_dates'
    call = workflow.planbot_client.check_availability.call_args
    assert call.kwargs['action'] == 'search_alternative_dates'
    assert call.args[0]['centre'] == "Bois d'Arcy"
    assert 'start_date' not in call.args[0]
    assert 'nombre_jours_souhaites' not in call.args[0]
    assert 'du 24/08/2026 au 26/08/2026' in result['draft_content']
    assert result['validation']['valid'] is True


def test_workflow_uses_initial_default_for_planbot_and_confirms_it_at_email_end():
    workflow = workflow_without_clients()
    message = (
        'Nous souhaitons former un interimaire au CACES R486 categories A et B. '
        'Quelles sont vos prochaines disponibilites ?'
    )
    configure_process(workflow, [inbound_thread(plainText=message, attachmentCount='0')])
    workflow.crm_lookup.lookup_sender.return_value['account_name'] = "ENTREPRISE TEST 78390 BOIS-D'ARCY"
    workflow.triage_agent.process.return_value = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'nb_categories': 2,
            'type_ir': '',
        },
        missing_fields=['type_ir'],
    )
    workflow.planbot_client.check_availability.return_value = {
        'status': 'ok',
        'formation': 'CACES R486',
        'semaines': [{
            'dispo_reelle': True,
            'sequence_valide': True,
            'options': [{
                'start': '2026-08-24',
                'end': '2026-08-26',
                'dates': ['2026-08-24', '2026-08-25', '2026-08-26'],
            }],
        }],
    }
    workflow.response_agent.process.side_effect = lambda data: {
        'response_html': data['fallback_response'],
        'used_ai': False,
        'requires_human_action': False,
        'human_action_reason': '',
    }

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['triage_result']['defaulted_fields'] == ['type_ir']
    assert result['planbot_action'] == 'search_alternative_dates'
    assert workflow.planbot_client.check_availability.call_args.args[0]['type_ir'] == 'initial'
    assert 'du 24/08/2026 au 26/08/2026' in result['draft_content']
    assert "confirmer qu&#x27;il s&#x27;agit bien d&#x27;une formation initiale" in result['draft_content']
    assert result['validation']['valid'] is True


def test_workflow_falls_back_to_nearby_centres_when_requested_centre_has_no_sequence():
    workflow = workflow_without_clients()
    message = (
        'Nous souhaitons une formation initiale pour un interimaire au CACES R486 '
        'categories A et B. Quelles sont vos prochaines disponibilites ?'
    )
    configure_process(workflow, [inbound_thread(plainText=message, attachmentCount='0')])
    workflow.crm_lookup.lookup_sender.return_value['account_name'] = "ENTREPRISE TEST 78390 BOIS-D'ARCY"
    workflow.triage_agent.process.return_value = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'nb_categories': 2,
            'type_ir': 'initial',
        },
        missing_fields=[],
    )
    workflow.planbot_client.check_availability.side_effect = [
        {
            'status': 'ok',
            'semaines': [{
                'start': '2026-07-20',
                'end': '2026-07-24',
                'dispo_reelle': False,
            }],
        },
        {
            'status': 'ok',
            'centres': [{
                'centre': 'Villabe',
                'dispo_reelle': True,
                'sequence_valide': True,
                'options': [{
                    'start': '2026-07-20',
                    'end': '2026-07-22',
                    'dates': ['2026-07-20', '2026-07-21', '2026-07-22'],
                }],
            }],
        },
    ]
    workflow.response_agent.process.side_effect = lambda data: {
        'response_html': data['fallback_response'],
        'used_ai': False,
        'requires_human_action': False,
        'human_action_reason': '',
    }

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert workflow.planbot_client.check_availability.call_count == 2
    second_call = workflow.planbot_client.check_availability.call_args_list[1]
    assert second_call.kwargs['action'] == 'search_alternative_centres'
    assert second_call.args[0]['exclude_centre'] == "Bois d'Arcy"
    assert 'Villabe : du 20/07/2026 au 22/07/2026' in result['draft_content']
    assert result['validation']['valid'] is True


def test_dates_list_is_detected_as_new_request_not_document_submission():
    agent = object.__new__(RelationsTriageAgent)

    mode = agent._detect_request_mode(
        'Bonjour, voici la suite des dates pour les CACES : du 27 au 29 juillet.'
    )

    assert mode == 'new_request'


def test_triage_drops_confirmation_pseudo_fields_and_normalizes_year():
    agent = object.__new__(RelationsTriageAgent)

    result = agent._normalize(
        {
            'intent': 'DEMANDE_DISPONIBILITE_SESSION',
            'missing_fields': ['confirmation_valentin_dates', 'annee_sessions_demandees'],
            'extracted': {},
        },
        'Dates CACES',
        'Voici la suite des dates pour les CACES.',
        EXTERNAL_EMAIL,
    )

    assert result['missing_fields'] == ['year']


def test_formalogistics_platform_notification_has_no_external_recipient():
    workflow = workflow_without_clients()
    thread = inbound_thread(
        fromEmailAddress='Formalogistics Pro <contact@formalogistics.pro>',
        plainText='Nouvelle commande recue sur la plateforme.',
    )
    configure_process(workflow, [thread])
    workflow.desk_client.get_ticket.return_value['email'] = 'contact@formalogistics.pro'

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['workflow_stage'] == 'STOPPED_NO_EXTERNAL_RECIPIENT'
    workflow.response_agent.process.assert_not_called()


def test_string_zero_attachment_count_is_not_an_attachment():
    workflow = workflow_without_clients()

    context = workflow._attachment_context(inbound_thread(attachmentCount='0'))

    assert context == {'has_attachments': False, 'names': []}


def test_thread_without_valid_sender_is_never_matched_to_ticket_contact():
    workflow = workflow_without_clients()
    thread = inbound_thread(fromEmailAddress='', plainText='Copie interne sans expediteur.')
    configure_process(workflow, [thread])

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['workflow_stage'] == 'STOPPED_NO_EXTERNAL_RECIPIENT'
    workflow.crm_lookup.lookup_sender.assert_not_called()


def test_new_human_reply_during_generation_blocks_draft_creation():
    workflow = workflow_without_clients()
    customer = inbound_thread()
    configure_process(workflow, [customer])
    workflow.desk_client.list_ticket_threads.return_value = [
        {
            **customer,
            'content': None,
        },
        {
            'id': 'new-human-reply',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-15T10:05:00.000Z',
            'fromEmailAddress': 'relations.entreprises@cab-formations.fr',
            'to': EXTERNAL_EMAIL,
        },
    ]

    result = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        ignore_existing_draft=True,
    )

    assert result['workflow_stage'] == 'SKIPPED_STALE_CONTEXT'
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()


def test_unchanged_context_allows_draft_creation():
    workflow = workflow_without_clients()
    customer = inbound_thread()
    configure_process(workflow, [customer])
    workflow.desk_client.list_ticket_threads.return_value = [customer]
    initial_ticket = workflow.desk_client.get_ticket.return_value
    workflow.desk_client.get_ticket.side_effect = [
        initial_ticket,
        initial_ticket,
        {**initial_ticket, 'assigneeId': ACCOUNT_MANAGER_DESK_ID},
    ]

    result = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        ignore_existing_draft=True,
    )

    assert result['draft_created'] is True
    assert result['assignment']['assigned'] is True
    assert result['assignment']['changed'] is True
    method_names = [call[0] for call in workflow.desk_client.method_calls]
    assignment_index = next(
        index for index, call in enumerate(workflow.desk_client.method_calls)
        if call[0] == 'update_ticket' and call.args[1] == {'assigneeId': ACCOUNT_MANAGER_DESK_ID}
    )
    assert assignment_index < method_names.index('create_ticket_reply_draft')
    workflow.desk_client.create_ticket_reply_draft.assert_called_once()


def test_assignment_failure_blocks_draft_creation():
    workflow = workflow_without_clients()
    customer = inbound_thread()
    configure_process(workflow, [customer])
    workflow.desk_client.list_ticket_threads.return_value = [customer]
    workflow.desk_client.update_ticket.side_effect = RuntimeError('Desk unavailable')

    result = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        ignore_existing_draft=True,
    )

    assert result['workflow_stage'] == 'STOPPED_ACCOUNT_ASSIGNMENT_FAILED'
    assert result['assignment']['assigned'] is False
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()


def test_concurrent_reassignment_after_patch_blocks_draft_creation():
    workflow = workflow_without_clients()
    customer = inbound_thread()
    configure_process(workflow, [customer])
    workflow.desk_client.list_ticket_threads.return_value = [customer]
    initial_ticket = workflow.desk_client.get_ticket.return_value
    workflow.desk_client.get_ticket.side_effect = [
        initial_ticket,
        initial_ticket,
        {**initial_ticket, 'assigneeId': 'concurrent-agent'},
    ]

    result = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        ignore_existing_draft=True,
    )

    assert result['workflow_stage'] == 'SKIPPED_STALE_CONTEXT'
    assert result['assignment']['assigned'] is True
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()


def test_strict_draft_check_failure_or_existing_draft_blocks_creation():
    customer = inbound_thread()

    workflow = workflow_without_clients()
    configure_process(workflow, [customer])
    workflow.desk_client.has_existing_draft_strict.return_value = True
    existing = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        ignore_existing_draft=True,
    )
    assert existing['workflow_stage'] == 'SKIPPED_STALE_CONTEXT'
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()

    workflow = workflow_without_clients()
    configure_process(workflow, [customer])
    workflow.desk_client.has_existing_draft_strict.side_effect = RuntimeError('Thread API unavailable')
    unavailable = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        ignore_existing_draft=True,
    )
    assert unavailable['workflow_stage'] == 'SKIPPED_STALE_CONTEXT'
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()


def test_ticket_already_assigned_to_account_manager_is_not_reassigned():
    workflow = workflow_without_clients()
    configure_process(workflow, [inbound_thread()])
    ticket = {'assigneeId': ACCOUNT_MANAGER_DESK_ID}
    assignment = workflow._resolve_account_manager(
        workflow.crm_lookup.lookup_sender.return_value,
        ticket,
    )

    result = workflow._assign_ticket_to_account_manager('ticket-1', ticket, assignment)

    assert result['assigned'] is True
    assert result['changed'] is False
    workflow.desk_client.update_ticket.assert_not_called()


def test_wrong_department_and_closed_ticket_are_blocked_before_processing():
    workflow = workflow_without_clients()
    workflow.desk_client.get_ticket.return_value = {
        'departmentId': 'another-department',
        'statusType': 'Open',
    }

    wrong_department = workflow.process_ticket('ticket-1')

    assert wrong_department['workflow_stage'] == 'STOPPED_WRONG_DEPARTMENT'
    workflow.desk_client.get_all_threads_with_full_content.assert_not_called()

    workflow.desk_client.reset_mock()
    workflow.desk_client.get_ticket.return_value = {
        'departmentId': '198709000027921097',
        'statusType': 'Closed',
    }

    closed = workflow.process_ticket('ticket-2')

    assert closed['workflow_stage'] == 'STOPPED_TICKET_CLOSED'
    workflow.desk_client.get_all_threads_with_full_content.assert_not_called()

    workflow.desk_client.reset_mock()
    workflow.desk_client.get_ticket.return_value = {'departmentId': '', 'statusType': 'Open'}

    missing_department = workflow.process_ticket('ticket-3')

    assert missing_department['workflow_stage'] == 'STOPPED_WRONG_DEPARTMENT'


def test_route_human_is_not_overridden_by_keyword_intent():
    agent = object.__new__(RelationsTriageAgent)

    result = agent._normalize(
        {
            'action': 'ROUTE_HUMAN',
            'intent': 'AUTRE_A_QUALIFIER',
            'reason': 'Reclamation sensible',
            'extracted': {},
            'missing_fields': [],
        },
        'Ancien objet devis CACES',
        "Reclamation grave concernant l'attestation de fin de formation.",
        EXTERNAL_EMAIL,
    )

    assert result['action'] == 'ROUTE_HUMAN'
    assert result['intent'] == 'ATTESTATION_FIN_FORMATION'


def test_fallback_training_request_rebuilds_required_missing_fields():
    agent = object.__new__(RelationsTriageAgent)

    result = agent._fallback(
        'Demande de devis CACES R489',
        'Bonjour, pouvez-vous nous preparer un devis ?',
        EXTERNAL_EMAIL,
    )

    assert result['intent'] == 'DEMANDE_DEVIS_FORMATION'
    assert result['extracted']['formation_type'] == 'CACES R489'
    assert {'centre', 'dates', 'nb_candidates', 'categories'}.issubset(result['missing_fields'])


def test_mixed_thanks_attachment_and_registration_is_a_new_request():
    agent = object.__new__(RelationsTriageAgent)

    mode = agent._detect_request_mode(
        'Merci pour votre retour. Nous souhaitons inscrire deux candidats, CV ci-joints.'
    )

    assert mode == 'new_request'


def test_invalid_calendar_date_is_not_extracted():
    agent = object.__new__(RelationsTriageAgent)

    assert agent._extract_first_date('Session demandee le 31/02/2026') is None


def test_numeric_date_range_keeps_both_bounds():
    agent = object.__new__(RelationsTriageAgent)

    assert agent._extract_date_range(
        'Formation demandee du 21/09/2026 au 23/09/2026.'
    ) == ('2026-09-21', '2026-09-23')
    assert agent._extract_date_range(
        'Formation demandee du 21 au 23/09/2026.'
    ) == ('2026-09-21', '2026-09-23')
    assert agent._extract_date_range(
        'Formation demandee du 21/09 au 23/09/2026.'
    ) == ('2026-09-21', '2026-09-23')
    assert agent._extract_date_range(
        'Periode erronee du 23 au 21/09/2026.'
    ) == (None, None)


def test_partial_planbot_coverage_is_not_presented_as_available():
    result = {
        'verdict': 'indisponible',
        'coverage_complete': False,
        'jours': [{'date': '2026-09-21', 'formation_theorie': True}],
    }

    assert _result_has_availability(result) is False


def test_caces_week_without_explicit_valid_sequence_is_not_available():
    result = {
        'formation': 'CACES R486',
        'status': 'ok',
        'semaines': [{
            'dispo_reelle': True,
            'jours': [{'date': '2026-09-21', 'formation_theorie': [{}]}],
        }],
    }

    assert _result_has_availability(result) is False


def test_unverified_extracted_date_and_centre_are_removed_before_response():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DEVIS_FORMATION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R489',
            'centre': 'Lyon',
            'start_date': '2030-12-31',
            'end_date': '2030-12-31',
            'categories': [],
        },
        missing_fields=[],
    )

    workflow._sanitize_extracted_facts(
        triage,
        'Bonjour, je souhaite un devis CACES R489 a Paris pour le 21/09/2026.',
    )

    assert triage['extracted']['formation_type'] == 'CACES R489'
    assert triage['extracted']['centre'] == ''
    assert triage['extracted']['start_date'] is None
    assert {'centre', 'dates'}.issubset(triage['missing_fields'])


def test_unverified_counts_unknown_fields_and_financing_are_sanitized():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R489',
            'nb_candidates': 99,
            'categories': ['3', '5'],
            'nb_categories': 9,
            'financement': 'B2C',
            'iban': 'FR761234',
        },
        missing_fields=[],
    )

    workflow._sanitize_extracted_facts(
        triage,
        'Demande CACES R489 categories 3 et 5 pour deux candidats.',
    )

    assert triage['extracted']['nb_candidates'] is None
    assert triage['extracted']['nb_categories'] is None
    assert triage['extracted']['financement'] == 'B2B'
    assert 'iban' not in triage['extracted']


def test_only_historical_dates_referenced_by_current_message_are_authorized():
    workflow = workflow_without_clients()
    source = workflow._build_validation_source(
        'Je retiens la periode du 19 au 21 octobre.',
        'Options: 07/09/2026 puis du 19 au 21 octobre 2026.',
    )

    assert '2026-10-19' in source
    assert '2026-10-21' in source
    assert '2026-09-07' not in source


def test_explicit_current_year_does_not_authorize_same_day_from_old_year():
    workflow = workflow_without_clients()
    source = workflow._build_validation_source(
        'Je souhaite la session du 21/09/2026.',
        'Ancienne proposition: 21/09/2025.',
    )

    assert '21/09/2026' in source
    assert '2025-09-21' not in source


def test_availability_confirmation_and_date_transmission_are_new_requests():
    agent = object.__new__(RelationsTriageAgent)

    assert agent._detect_request_mode(
        'Pouvez-vous nous confirmer vos disponibilites pour deux candidats ?'
    ) == 'new_request'
    assert agent._detect_request_mode(
        'Je vous transmets les dates demandees pour verifier les disponibilites.'
    ) == 'new_request'


def test_missing_field_schema_drops_sensitive_and_maps_known_aliases():
    agent = object.__new__(RelationsTriageAgent)

    result = agent._normalize(
        {
            'intent': 'DEMANDE_DEVIS_FORMATION',
            'extracted': {'iban': 'FR761234'},
            'missing_fields': ['iban', 'mot_de_passe', 'centre_formation', 'dates_sessions'],
        },
        'Devis',
        'Pouvez-vous etablir un devis ?',
        EXTERNAL_EMAIL,
    )

    assert result['missing_fields'] == ['centre', 'dates']
    assert 'iban' not in result['extracted']


def test_internal_subdomain_and_invalid_timestamp_are_rejected():
    workflow = workflow_without_clients()

    assert workflow._latest_customer_thread([
        inbound_thread(fromEmailAddress='Agent <user@mail.cab-formations.fr>')
    ]) is None
    assert workflow._latest_customer_thread([
        inbound_thread(createdTime='not-a-date')
    ]) is None


def test_current_contract_message_beats_old_quote_subject():
    agent = object.__new__(RelationsTriageAgent)

    intent = agent._detect_business_intent(
        'Ancien objet devis CACES',
        'Merci de relire le contrat et la signature avant validation.',
    )

    assert intent == 'CONVENTION_CONTRAT_DOSSIER'


def test_candidate_count_is_not_accepted_as_caces_category_and_centre_needs_word_boundary():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R489',
            'centre': 'Paris',
            'categories': ['3'],
            'nb_candidates': 3,
        },
        missing_fields=[],
    )

    workflow._sanitize_extracted_facts(
        triage,
        'Une entreprise parisienne demande un CACES R489 pour 3 candidats.',
    )

    assert triage['extracted']['centre'] == ''
    assert triage['extracted']['categories'] == []
    assert triage['extracted']['nb_candidates'] == 3


def test_partial_training_extraction_rebuilds_required_missing_fields_without_history():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'SST'},
        missing_fields=[],
    )

    workflow._enforce_planbot_missing_fields(triage, has_previous_cab=False)

    assert {'centre', 'dates', 'nb_candidates'}.issubset(triage['missing_fields'])


def test_inverted_period_is_removed_before_planbot():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'SST',
            'centre': 'Paris',
            'start_date': '2026-09-23',
            'end_date': '2026-09-21',
            'nb_candidates': 2,
        },
        missing_fields=[],
    )
    source = 'Formation SST a Paris pour 2 candidats du 21/09/2026 au 23/09/2026.'

    workflow._sanitize_extracted_facts(triage, source)

    assert triage['extracted']['start_date'] is None
    assert triage['extracted']['end_date'] is None
    assert workflow._should_call_planbot(triage, source) is False


def test_workflow_reconstructs_revert_history_and_rechecks_exact_session():
    workflow = workflow_without_clients()
    threads = [
        inbound_thread(
            id='request',
            createdTime='2026-07-08T10:00:00.000Z',
            plainText=(
                'Formation CACES A&B nacelle en initial pour deux collaborateurs '
                'a Villabe ou Herblay.'
            ),
            attachmentCount='0',
        ),
        {
            'id': 'options',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-08T11:00:00.000Z',
            'to': EXTERNAL_EMAIL,
            'plainText': (
                'CACES R486 A et B en initial. VILLABE: du 22/07 au 24/07, '
                'puis du 27/07 au 29/07.'
            ),
        },
        inbound_thread(
            id='registration',
            createdTime='2026-07-09T10:00:00.000Z',
            plainText='Inscrire M. ALPHA TESTEUR du 22 au 24 juillet 2026 a Villabe.',
            attachmentCount='0',
        ),
        {
            'id': 'purchase-order',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-16T07:50:00.000Z',
            'to': EXTERNAL_EMAIL,
            'plainText': 'Merci de transmettre le BDC de ALPHA TESTEUR prevu du 22/07 au 24/07.',
        },
        {
            'id': 'change-proposal',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-16T08:00:00.000Z',
            'to': EXTERNAL_EMAIL,
            'plainText': "J'ai de la disponibilite du 20/07 au 22/07 sur Villabe.",
        },
        inbound_thread(
            id='accepted-change',
            createdTime='2026-07-16T10:00:00.000Z',
            plainText="C'est OK pour le 20, peux-tu transmettre la nouvelle convocation ?",
            attachmentCount='0',
        ),
        inbound_thread(
            id='current',
            createdTime='2026-07-16T10:10:00.000Z',
            plainText='Finalement on revient a la date initiale stp 22/07.',
            attachmentCount='0',
        ),
    ]
    configure_process(workflow, threads)
    workflow.triage_agent.process.return_value = safe_triage(
        intent='AUTRE_A_QUALIFIER',
        request_mode='follow_up',
        extracted={'type_ir': 'initial'},
        missing_fields=['dates', 'formation_type'],
    )
    workflow.planbot_client.check_availability.return_value = {
        'status': 'ok',
        'formation': 'CACES R486',
        'centre': 'Villabe',
        'periode': '2026-07-22 -> 2026-07-24',
        'sequence_valide': True,
        'sequence_options': [{
            'start': '2026-07-22',
            'end': '2026-07-24',
            'dates': ['2026-07-22', '2026-07-23', '2026-07-24'],
        }],
    }
    workflow.response_agent.process.side_effect = lambda data: {
        'response_html': data['fallback_response'],
        'used_ai': False,
        'requires_human_action': False,
        'human_action_reason': '',
    }

    result = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert result['session_context']['status'] == 'resolved'
    assert result['triage_result']['session_operation'] == 'revert_original'
    assert result['planbot_action'] == 'check_availability'
    call = workflow.planbot_client.check_availability.call_args
    assert call.kwargs['action'] == 'check_availability'
    assert call.args[0] == {
        'centre': 'Villabe',
        'formation_type': 'CACES R486',
        'start_date': '2026-07-22',
        'end_date': '2026-07-24',
        'nb_candidates': 1,
        'categories': ['A', 'B'],
        'nb_categories': 2,
        'type_ir': 'initial',
        'financement': 'B2B',
        'nombre_jours_souhaites': 3,
    }
    assert 'Disponibilite verifiee' in result['draft_content']
    assert 'du 22/07/2026 au 24/07/2026' in result['draft_content']
    assert 'doit encore etre enregistre' in result['draft_content']
    assert result['validation']['valid'] is True

    workflow.planbot_client.check_availability.return_value = {
        'status': 'skipped',
        'error': 'planbot_api_not_configured',
    }
    workflow.response_agent.process.side_effect = None
    workflow.response_agent.process.return_value = {
        'response_html': (
            'Bonjour,<br><br>La modification est en cours, je reviens vers vous.<br><br>'
            "Cordialement,<br>L'equipe Relations entreprises CAB Formations"
        ),
        'used_ai': True,
        'requires_human_action': False,
        'human_action_reason': '',
    }

    skipped = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert skipped['response_generation']['used_ai'] is False
    assert skipped['response_generation']['fallback_reason'] == 'planbot_skipped'
    assert 'verification humaine reste necessaire' in skipped['draft_content']
    assert 'modification est en cours' not in skipped['draft_content']

    workflow.planbot_client.check_availability.return_value = {
        'status': 'ok',
        'formation': 'CACES R486',
        'centre': 'Villabe',
        'verdict': 'aucun_scenario_zoho_rules',
        'coverage_complete': False,
    }
    unavailable = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert unavailable['response_generation']['used_ai'] is False
    assert unavailable['response_generation']['fallback_reason'] == 'planbot_no_direct_availability'
    assert "Aucune disponibilite complete n'a ete identifiee" in unavailable['draft_content']

    workflow.planbot_client.check_availability.reset_mock()
    workflow.planbot_client.check_availability.side_effect = [
        {
            'status': 'ok',
            'formation': 'CACES R486',
            'centre': 'Villabe',
            'verdict': 'aucun_scenario_zoho_rules',
            'coverage_complete': False,
        },
        {
            'status': 'ok',
            'formation': 'CACES R486',
            'centre': 'Villabe',
            'semaines': [{
                'dispo_reelle': True,
                'sequence_valide': True,
                'options': [{
                    'start': '2026-07-27',
                    'end': '2026-07-29',
                    'dates': ['2026-07-27', '2026-07-28', '2026-07-29'],
                }],
            }],
        },
    ]

    alternatives = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert workflow.planbot_client.check_availability.call_count == 2
    assert workflow.planbot_client.check_availability.call_args_list[0].kwargs['action'] == 'check_availability'
    assert workflow.planbot_client.check_availability.call_args_list[1].kwargs['action'] == 'search_alternative_dates'
    alternative_payload = workflow.planbot_client.check_availability.call_args_list[1].args[0]
    assert alternative_payload['around_date'] == '2026-07-22'
    assert alternative_payload['min_start_date'] == '2026-07-25'
    assert alternative_payload['direction'] == 'after'
    assert alternative_payload['nb_weeks'] == 12
    assert alternatives['response_generation']['fallback_reason'] == 'planbot_alternatives_only'
    assert 'Alternatives identifiees' in alternatives['draft_content']
    assert 'du 27/07/2026 au 29/07/2026' in alternatives['draft_content']
    assert alternatives['validation']['valid'] is True

    workflow.planbot_client.check_availability.reset_mock()
    workflow.planbot_client.check_availability.side_effect = [
        {
            'status': 'ok',
            'formation': 'CACES R486',
            'verdict': 'aucun_scenario_zoho_rules',
            'coverage_complete': False,
        },
        {'status': 'ok', 'formation': 'CACES R486', 'semaines': []},
        {
            'status': 'ok',
            'formation': 'CACES R486',
            'centres': [{
                'centre': 'Herblay',
                'dispo_reelle': True,
                'sequence_valide': True,
                'options': [{
                    'start': '2026-07-22',
                    'end': '2026-07-24',
                    'dates': ['2026-07-22', '2026-07-23', '2026-07-24'],
                }],
            }],
        },
    ]

    nearby = workflow.process_ticket('ticket-1', ignore_existing_draft=True)

    assert workflow.planbot_client.check_availability.call_count == 3
    assert workflow.planbot_client.check_availability.call_args_list[2].kwargs['action'] == 'search_alternative_centres'
    assert 'Herblay : du 22/07/2026 au 24/07/2026' in nearby['draft_content']
    assert nearby['validation']['valid'] is True


def test_current_training_facts_override_stale_history_before_planbot():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R489',
            'categories': ['3', '5'],
            'type_ir': 'initial',
        },
        missing_fields=[],
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': 'Entreprise Test'},
        'Nouvelle demande SST a Herblay du 21/09/2026 au 22/09/2026 pour 2 candidats.',
        'Ancienne demande CACES R489 categories 3 et 5 en initial a Villabe.',
    )

    assert triage['extracted']['formation_type'] == 'SST'
    assert triage['extracted']['categories'] == []
    assert triage['extracted']['centre'] == 'Herblay'
    assert triage['extracted']['nb_candidates'] == 2
    assert triage['extracted']['type_ir'] == ''


def test_current_corrections_use_replacement_values_not_negated_values():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='follow_up',
        extracted={
            'formation_type': 'CACES R489',
            'type_ir': 'recyclage',
            'nb_candidates': 2,
        },
        missing_fields=[],
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': 'Entreprise Test'},
        'Pas de CACES R489 mais une formation SST. Pas en recyclage mais en initial. '
        'Merci de remplacer 2 candidats par 3 candidats a Herblay.',
        'Ancienne demande CACES R489 en recyclage pour 2 candidats.',
    )

    assert triage['extracted']['formation_type'] == 'SST'
    assert triage['extracted']['type_ir'] == 'initial'
    assert triage['extracted']['nb_candidates'] == 3
    assert triage['extracted']['centre'] == 'Herblay'

    stale_formation = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='follow_up',
        extracted={'formation_type': 'CACES R482', 'nb_candidates': 2},
        missing_fields=[],
    )
    workflow._prepare_planbot_search_context(
        stale_formation,
        {'account_name': 'Entreprise Test'},
        'CACES R486 categories A et B en initial, mais pour 3 candidats a Herblay.',
        'Ancienne demande CACES R482 categories B1 et C1 pour 2 candidats.',
    )
    assert stale_formation['extracted']['formation_type'] == 'CACES R486'
    assert stale_formation['extracted']['categories'] == ['A', 'B']
    assert stale_formation['extracted']['nb_candidates'] == 3

    corrected_type = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='follow_up',
        extracted={'formation_type': 'CACES R486', 'type_ir': 'recyclage'},
        missing_fields=[],
    )
    workflow._prepare_planbot_search_context(
        corrected_type,
        {'account_name': 'Entreprise Test'},
        'Nous ne voulons plus de recyclage, ce sera une formation initiale.',
        'CACES R486 categories A et B en recyclage pour un candidat.',
    )
    assert corrected_type['extracted']['type_ir'] == 'initial'


def test_multiple_current_formations_are_marked_ambiguous():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'SST'},
        missing_fields=[],
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': 'Entreprise Test'},
        'Nouvelle demande SST et habilitation electrique a Herblay pour 2 candidats.',
        '',
    )
    workflow._enforce_planbot_missing_fields(triage, has_previous_cab=True)

    assert triage['extracted']['formation_type'] == ''
    assert 'formation_type' in triage['ambiguous_fields']
    assert 'formation_type' in triage['missing_fields']


def test_semantic_new_request_beats_followup_wording():
    agent = object.__new__(RelationsTriageAgent)

    assert agent._detect_request_mode(
        'Je reviens vers vous pour une nouvelle demande SST.'
    ) == 'new_request'
    assert agent._detect_request_mode(
        'Je reviens vers vous avec un nouveau besoin SST.'
    ) == 'new_request'


def test_multiple_current_centres_remain_ambiguous_instead_of_using_crm_default():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'SST'},
        missing_fields=[],
    )

    workflow._prepare_planbot_search_context(
        triage,
        {'account_name': "ENTREPRISE TEST 78390 BOIS-D'ARCY"},
        'Avez-vous une session SST a Villabe ou Herblay pour 2 candidats ?',
        '',
    )
    workflow._enforce_planbot_missing_fields(triage, has_previous_cab=False)

    assert triage['extracted']['centre'] == ''
    assert 'centre' in triage['ambiguous_fields']
    assert 'centre' in triage['missing_fields']


def test_new_request_rebuilds_required_fields_even_with_previous_cab_message():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'SST'},
        missing_fields=[],
    )

    workflow._enforce_planbot_missing_fields(triage, has_previous_cab=True)

    assert {'centre', 'dates', 'nb_candidates'}.issubset(triage['missing_fields'])


def test_expired_session_is_never_checked_with_planbot():
    workflow = workflow_without_clients()
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='follow_up',
        session_operation='reschedule',
        session_context_status='resolved',
        history_verified_fields=['start_date', 'end_date'],
        extracted={
            'formation_type': 'SST',
            'centre': 'Herblay',
            'start_date': '2025-09-21',
            'end_date': '2025-09-22',
            'nb_candidates': 2,
        },
        missing_fields=[],
    )

    assert workflow._select_planbot_action(triage, '') == ''


def test_full_planbot_result_prefers_same_centre_dates_before_other_centres():
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={
            'formation_type': 'CACES R486',
            'centre': 'Villabe',
            'nb_candidates': 1,
            'categories': ['A', 'B'],
            'type_ir': 'initial',
        },
        missing_fields=[],
    )
    planbot_result = {
        'direct': {'status': 'ok', 'formation': 'CACES R486', 'verdict': 'complet'},
        'alternative_dates': {
            'status': 'ok',
            'formation': 'CACES R486',
            'semaines': [{
                'dispo_reelle': True,
                'sequence_valide': True,
                'options': [{
                    'start': '2026-09-01',
                    'end': '2026-09-03',
                    'dates': ['2026-09-01', '2026-09-02', '2026-09-03'],
                }],
            }],
        },
        'alternative_centres': {
            'status': 'ok',
            'formation': 'CACES R486',
            'centres': [{
                'centre': 'Herblay',
                'dispo_reelle': True,
                'sequence_valide': True,
                'options': [{
                    'start': '2026-09-08',
                    'end': '2026-09-10',
                }],
            }],
        },
    }

    response = build_relations_response(triage, {}, planbot_result)

    assert 'du 01/09/2026 au 03/09/2026' in response
    assert 'Herblay' not in response


def test_same_date_required_lots_are_all_rendered():
    triage = safe_triage(
        intent='DEMANDE_DISPONIBILITE_SESSION',
        request_mode='new_request',
        extracted={'formation_type': 'CACES R486'},
        missing_fields=[],
    )
    planbot_result = {
        'status': 'ok',
        'formation': 'CACES R486',
        'semaines': [{
            'dispo_reelle': True,
            'sequence_valide': True,
            'options': [
                {
                    'start': '2026-09-07',
                    'end': '2026-09-09',
                    'nb_candidates': 2,
                    'lot_required': True,
                },
                {
                    'start': '2026-09-07',
                    'end': '2026-09-09',
                    'nb_candidates': 3,
                    'lot_required': True,
                },
            ],
        }],
    }

    response = build_relations_response(triage, {}, planbot_result)

    assert response.count('Lot requis') == 2
    assert 'Lot requis de 2 candidats' in response
    assert 'Lot requis de 3 candidats' in response


def test_cross_year_range_is_parsed_without_merging_separate_alternatives():
    agent = object.__new__(RelationsTriageAgent)

    assert agent._extract_date_range(
        'Formation du 29/12 au 03/01/2027.'
    ) == ('2026-12-29', '2027-01-03')
    assert agent._extract_date_range(
        'Deux options: le 21/09/2026 ou le 28/09/2026.'
    ) == (None, None)
