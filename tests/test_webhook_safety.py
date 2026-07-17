import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import webhook_server  # noqa: E402


class FakeWorkflow:
    calls = []

    def process_ticket(self, **kwargs):
        self.calls.append(kwargs)
        return {'success': True, 'errors': []}


def test_background_processing_defaults_to_draft(monkeypatch):
    FakeWorkflow.calls = []
    webhook_server._PROCESSING_TICKETS.clear()
    webhook_server._PENDING_TICKETS.clear()
    monkeypatch.setattr(
        webhook_server,
        '_resolve_ticket_workflow',
        lambda _ticket_id: (FakeWorkflow(), 'doc'),
    )

    webhook_server.process_ticket_background('ticket-1')

    assert FakeWorkflow.calls[0]['auto_send'] is False
    assert FakeWorkflow.calls[0]['auto_create_draft'] is True


def test_test_endpoint_requires_configured_secret(monkeypatch):
    monkeypatch.setattr(webhook_server, 'WEBHOOK_SECRET', '')

    response = webhook_server.app.test_client().post(
        '/webhook/test',
        json={'ticket_id': 'ticket-1'},
    )

    assert response.status_code == 401


def test_test_endpoint_mutations_default_off(monkeypatch):
    FakeWorkflow.calls = []
    monkeypatch.setattr(webhook_server, 'WEBHOOK_SECRET', 'secret')
    monkeypatch.setattr(webhook_server, 'ENABLE_LIVE_TEST_WEBHOOK', False)
    monkeypatch.setattr(
        webhook_server,
        '_resolve_ticket_workflow',
        lambda _ticket_id: (FakeWorkflow(), 'doc'),
    )

    response = webhook_server.app.test_client().post(
        '/webhook/test',
        headers={'X-Webhook-Secret': 'secret'},
        json={'ticket_id': 'ticket-1'},
    )

    assert response.status_code == 403
    assert FakeWorkflow.calls == []


def test_live_test_requires_explicit_mutation_confirmation(monkeypatch):
    FakeWorkflow.calls = []
    monkeypatch.setattr(webhook_server, 'WEBHOOK_SECRET', 'secret')
    monkeypatch.setattr(webhook_server, 'ENABLE_LIVE_TEST_WEBHOOK', True)
    monkeypatch.setattr(
        webhook_server,
        '_resolve_ticket_workflow',
        lambda _ticket_id: (FakeWorkflow(), 'doc'),
    )

    client = webhook_server.app.test_client()
    response = client.post(
        '/webhook/test',
        headers={'X-Webhook-Secret': 'secret'},
        json={'ticket_id': 'ticket-1'},
    )

    assert response.status_code == 400
    assert FakeWorkflow.calls == []

    response = client.post(
        '/webhook/test',
        headers={'X-Webhook-Secret': 'secret'},
        json={'ticket_id': 'ticket-1', 'confirm_live_mutations': True},
    )

    assert response.status_code == 200
    assert FakeWorkflow.calls[0]['auto_create_draft'] is False
    assert FakeWorkflow.calls[0]['auto_update_crm'] is False
    assert FakeWorkflow.calls[0]['auto_update_ticket'] is False
    assert FakeWorkflow.calls[0]['auto_send'] is False


def test_duplicate_background_processing_queues_rerun(monkeypatch):
    webhook_server._PROCESSING_TICKETS.clear()
    webhook_server._PENDING_TICKETS.clear()

    class ReentrantWorkflow:
        calls = 0

        def process_ticket(self, **kwargs):
            self.__class__.calls += 1
            if self.__class__.calls == 1:
                webhook_server.process_ticket_background('ticket-1')
            return {'success': True, 'errors': []}

    monkeypatch.setattr(
        webhook_server,
        '_resolve_ticket_workflow',
        lambda _ticket_id: (ReentrantWorkflow(), 'doc'),
    )

    webhook_server.process_ticket_background('ticket-1')

    assert ReentrantWorkflow.calls == 2
    assert webhook_server._PROCESSING_TICKETS == set()
    assert webhook_server._PENDING_TICKETS == set()


def test_main_webhook_rejects_missing_secret(monkeypatch):
    monkeypatch.setattr(webhook_server, 'WEBHOOK_SECRET', '')

    response = webhook_server.app.test_client().post(
        '/webhook/zoho-desk',
        json={'ticket_id': 'ticket-1'},
    )

    assert response.status_code == 401


def test_relations_workflow_never_receives_crm_or_send_mutations():
    FakeWorkflow.calls = []

    webhook_server._run_ticket_workflow(
        workflow=FakeWorkflow(),
        workflow_name='relations',
        ticket_id='ticket-1',
        auto_create_draft=True,
        auto_update_crm=True,
        auto_update_ticket=True,
        auto_send=True,
    )

    assert FakeWorkflow.calls == [{
        'ticket_id': 'ticket-1',
        'auto_create_draft': True,
        'auto_update_ticket': True,
    }]


def test_department_dispatch_selects_relations_workflow(monkeypatch):
    class FakeDesk:
        closed = False

        def get_ticket(self, _ticket_id):
            return {'departmentId': '198709000027921097'}

        def close(self):
            self.__class__.closed = True

    monkeypatch.setattr(webhook_server, 'ZohoDeskClient', FakeDesk)
    monkeypatch.setattr(webhook_server, 'RelationsTicketWorkflow', FakeWorkflow)

    workflow, name = webhook_server._resolve_ticket_workflow('ticket-1')

    assert isinstance(workflow, FakeWorkflow)
    assert name == 'relations'
    assert FakeDesk.closed is True
