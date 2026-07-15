import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.workflows.doc_ticket_workflow import DOCTicketWorkflow


def workflow_without_clients():
    return object.__new__(DOCTicketWorkflow)


def safe_triage(**overrides):
    result = {
        'ticket_subject': 'Re: Safe campaign',
        'incoming_thread_count': 1,
        'customer_message': 'Je confirme la session proposée, merci.',
        'detected_intent': 'CONFIRMATION_SESSION',
        'secondary_intents': [],
        'method': 'ai',
        'confidence': 0.99,
        'has_attachments': False,
        'human_intervention': False,
        'quoted_content_fallback': False,
        'recipient_is_ticket_contact': True,
        'delivery_context_is_current': True,
    }
    result.update(overrides)
    return result


def safe_response(**overrides):
    result = {
        'response_text': 'Bonjour, votre session est confirmée. Bien cordialement.',
        'was_humanized': True,
        'secondary_intents': [],
        'is_blocking': False,
        'state_engine': {'state_id': 'SESSION_ASSIGNED'},
        'validation': {
            'SESSION_ASSIGNED': {
                'compliant': True,
                'errors': [],
                'warnings': [],
            }
        },
    }
    result.update(overrides)
    return result


def enable_safe_scenario(monkeypatch):
    monkeypatch.setattr(DOCTicketWorkflow, 'AUTO_SEND_SCENARIOS', [{
        'subject_equals': 'Safe campaign',
        'intent_equals': 'CONFIRMATION_SESSION',
        'state_equals': 'SESSION_ASSIGNED',
    }])


def test_auto_send_is_disabled_by_default():
    allowed, reason = workflow_without_clients()._can_auto_send(safe_response(), safe_triage())

    assert allowed is False
    assert reason.startswith('scenario_not_eligible')


def test_explicit_safe_scenario_can_pass(monkeypatch):
    enable_safe_scenario(monkeypatch)

    assert workflow_without_clients()._can_auto_send(safe_response(), safe_triage()) == (True, None)


def test_auto_send_matches_exact_intent_state_pair(monkeypatch):
    monkeypatch.setattr(DOCTicketWorkflow, 'AUTO_SEND_SCENARIOS', [
        {
            'subject_equals': 'Safe campaign',
            'intent_equals': 'CONFIRMATION_SESSION',
            'state_equals': 'OTHER_STATE',
        },
        {
            'subject_equals': 'Safe campaign',
            'intent_equals': 'CONFIRMATION_SESSION',
            'state_equals': 'SESSION_ASSIGNED',
        },
    ])

    assert workflow_without_clients()._can_auto_send(safe_response(), safe_triage()) == (True, None)


@pytest.mark.parametrize(
    ('triage_changes', 'response_changes', 'expected_reason'),
    [
        ({'incoming_thread_count': 0}, {}, 'incoming_count_not_one'),
        ({'incoming_thread_count': 2}, {}, 'incoming_count_not_one'),
        ({'customer_message': 'Bonjour'}, {}, 'empty_or_ambiguous_message'),
        ({'detected_intent': 'REPORT_DATE'}, {}, 'intent_not_eligible'),
        ({'secondary_intents': ['REPORT_DATE']}, {}, 'secondary_intents_present'),
        ({'method': 'fallback'}, {}, 'unsafe_triage_method'),
        ({'confidence': 0.5}, {}, 'low_triage_confidence'),
        ({'has_attachments': True}, {}, 'attachments_present'),
        ({'human_intervention': True}, {}, 'human_intervention_present'),
        ({'quoted_content_fallback': True}, {}, 'quoted_content_only'),
        ({'recipient_is_ticket_contact': False}, {}, 'recipient_not_verified'),
        ({'delivery_context_is_current': False}, {}, 'stale_or_unverified_context'),
        ({}, {'validation': {}}, 'validation_missing'),
        ({}, {'validation': {'x': {'compliant': True, 'errors': [], 'warnings': ['review']}}}, 'validation_warnings'),
        ({}, {'response_text': 'Votre mot de passe est secret.'}, 'sensitive_data_present'),
        ({}, {'response_text': 'mot ' * 141}, 'response_too_long'),
        ({}, {'is_blocking': True}, 'blocking_state'),
    ],
)
def test_auto_send_blocks_unsafe_cases(monkeypatch, triage_changes, response_changes, expected_reason):
    enable_safe_scenario(monkeypatch)

    allowed, reason = workflow_without_clients()._can_auto_send(
        safe_response(**response_changes),
        safe_triage(**triage_changes),
    )

    assert allowed is False
    assert reason.startswith(expected_reason)


@pytest.mark.parametrize('sender', [
    '"noreply"<noreply@exament3p.fr>',
    'noreply@notify.aircall.io',
])
def test_direct_system_sender_stops_before_crm(sender):
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.get_ticket.return_value = {
        'subject': 'Vérification du compte',
        'departmentId': '198709000025523146',
    }
    workflow.desk_client.get_all_threads_with_full_content.return_value = [{
        'id': 'thread-1',
        'direction': 'in',
        'status': 'SUCCESS',
        'fromEmailAddress': sender,
        'content': 'Notification automatique de vérification du compte.',
        'createdTime': '2026-07-14T10:00:00.000Z',
        'attachmentCount': '0',
    }]
    workflow.deal_linker = Mock()

    result = workflow._run_triage('ticket-1', auto_transfer=False)

    assert result['action'] == 'NOREPLY_NOTIFICATION'
    workflow.deal_linker.process.assert_not_called()


def test_string_zero_attachment_count_is_not_an_attachment():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.get_ticket.return_value = {
        'subject': 'Question candidat',
        'departmentId': '198709000025523146',
    }
    workflow.desk_client.get_all_threads_with_full_content.return_value = [{
        'id': 'thread-1',
        'direction': 'in',
        'status': 'SUCCESS',
        'fromEmailAddress': 'candidate@example.com',
        'content': 'Je souhaite connaitre la prochaine etape de mon dossier.',
        'createdTime': '2026-07-14T10:00:00.000Z',
        'attachmentCount': '0',
    }]
    workflow.deal_linker = Mock()
    workflow.deal_linker.process.return_value = {
        'all_deals': [],
        'selected_deal': {},
        'needs_clarification': True,
    }

    result = workflow._run_triage('ticket-1', auto_transfer=False)

    assert result['has_attachments'] is False


def test_context_revalidation_rejects_human_reply():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.get_ticket.return_value = {
        'status': 'Open',
        'departmentId': '198709000025523146',
    }
    workflow.desk_client.get_ticket_threads.return_value = {'data': [
        {
            'id': 'human-reply',
            'direction': 'out',
            'status': 'SUCCESS',
            'createdTime': '2026-07-14T10:01:00.000Z',
        },
        {
            'id': 'customer-message',
            'direction': 'in',
            'status': 'SUCCESS',
            'createdTime': '2026-07-14T10:00:00.000Z',
        },
    ]}
    snapshot = {
        'latest_incoming_thread_id': 'customer-message',
        'latest_thread_id': 'customer-message',
        'ticket_status_snapshot': 'open',
        'department_snapshot': '198709000025523146',
    }

    assert workflow._ticket_context_is_current('ticket-1', snapshot) is False


def test_context_revalidation_accepts_unchanged_ticket():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.get_ticket.return_value = {
        'status': 'Open',
        'departmentId': '198709000025523146',
    }
    workflow.desk_client.get_ticket_threads.return_value = {'data': [{
        'id': 'customer-message',
        'direction': 'in',
        'status': 'SUCCESS',
        'createdTime': '2026-07-14T10:00:00.000Z',
    }]}
    snapshot = {
        'latest_incoming_thread_id': 'customer-message',
        'latest_thread_id': 'customer-message',
        'ticket_status_snapshot': 'open',
        'department_snapshot': '198709000025523146',
    }

    assert workflow._ticket_context_is_current('ticket-1', snapshot) is True


def test_intentional_transfer_refreshes_only_ticket_ownership():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.get_ticket.return_value = {
        'status': 'Closed',
        'departmentId': 'target-department',
    }
    snapshot = {
        'latest_incoming_thread_id': 'customer-message',
        'latest_thread_id': 'customer-message',
        'ticket_status_snapshot': 'open',
        'department_snapshot': 'source-department',
    }

    workflow._refresh_ticket_ownership_snapshot('ticket-1', snapshot)

    assert snapshot == {
        'latest_incoming_thread_id': 'customer-message',
        'latest_thread_id': 'customer-message',
        'ticket_status_snapshot': 'open',
        'department_snapshot': 'target-department',
    }


def test_closed_ticket_never_reaches_triage_or_draft():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.has_existing_draft.return_value = False
    workflow.desk_client.get_ticket.return_value = {'status': 'Closed'}

    result = workflow.process_ticket('ticket-1', auto_create_draft=True, auto_send=True)

    assert result['workflow_stage'] == 'SKIPPED_TICKET_CLOSED'
    assert result['draft_created'] is False
    assert result['reply_sent'] is False
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()
    workflow.desk_client.send_ticket_reply.assert_not_called()


def test_noreply_terminal_action_precedes_pending_clarification():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.has_existing_draft.return_value = False
    workflow.desk_client.get_ticket.return_value = {'status': 'Open'}
    workflow._check_pending_duplicate_clarification = Mock(return_value={
        'pending_deal_id': 'deal-1',
        'identity_pending': True,
    })
    workflow._check_agent_hint = Mock(return_value=None)
    workflow._run_triage = Mock(return_value={
        'action': 'NOREPLY_NOTIFICATION',
        'reason': 'Notification ExamT3P',
        'customer_message': '',
        'intent_context': {},
    })
    workflow._add_internal_note = Mock()
    workflow.crm_client = Mock()
    workflow.deal_linker = Mock()

    result = workflow.process_ticket('ticket-1')

    assert result['workflow_stage'] == 'CLOSED_NOREPLY_NOTIFICATION'
    assert workflow.crm_client.method_calls == []
    workflow.deal_linker.process.assert_not_called()
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()


def test_examt3p_notification_never_generates_customer_reply():
    workflow = workflow_without_clients()
    workflow.desk_client = Mock()
    workflow.desk_client.has_existing_draft.return_value = False
    workflow.desk_client.get_ticket.return_value = {
        'status': 'Open',
        'statusType': 'Open',
        'subject': 'Vérification du compte',
        'departmentId': '198709000025523146',
        'source': {},
    }
    workflow.desk_client.get_all_threads_with_full_content.return_value = [{
        'id': 'thread-1',
        'direction': 'in',
        'status': 'SUCCESS',
        'fromEmailAddress': '"noreply"<noreply@exament3p.fr>',
        'content': 'Notification automatique de vérification du compte.',
        'createdTime': '2026-07-14T10:00:00.000Z',
        'attachmentCount': '0',
    }]
    workflow._check_pending_duplicate_clarification = Mock(return_value=None)
    workflow._check_agent_hint = Mock(return_value=None)
    workflow.deal_linker = Mock()
    workflow.crm_client = Mock()

    result = workflow.process_ticket(
        'ticket-1',
        auto_create_draft=True,
        auto_update_crm=True,
        auto_update_ticket=True,
        auto_send=True,
    )

    assert result['workflow_stage'] == 'CLOSED_NOREPLY_NOTIFICATION'
    assert result['draft_created'] is False
    assert result['reply_sent'] is False
    workflow.deal_linker.process.assert_not_called()
    workflow.desk_client.create_ticket_reply_draft.assert_not_called()
    workflow.desk_client.send_ticket_reply.assert_not_called()
    workflow.desk_client.update_ticket.assert_called_once_with('ticket-1', {'status': 'Closed'})
