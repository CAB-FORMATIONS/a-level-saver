import sys
from pathlib import Path
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.relations_crm_lookup import RelationsCRMLookup  # noqa: E402
from src.zoho_client import ZohoDeskClient  # noqa: E402


def test_lookup_uses_account_owner_not_contact_owner():
    crm = Mock()
    crm.search_contacts.return_value = {'data': [{
        'id': 'contact-1',
        'First_Name': 'Thomas',
        'Last_Name': 'Garnier',
        'Email': 'thomas@example.com',
        'Owner': {
            'id': 'contact-owner',
            'name': 'Contact Owner',
            'email': 'contact-owner@cab-formations.fr',
        },
        'Account_Name': {'id': 'account-1', 'name': 'Entreprise Test'},
    }]}
    crm.get_record.return_value = {
        'id': 'account-1',
        'Account_Name': 'Entreprise Test',
        'Owner': {
            'id': 'account-owner',
            'name': 'Account Manager',
            'email': 'manager@cab-formations.fr',
        },
    }
    crm.get_deals_by_contact.return_value = []

    result = RelationsCRMLookup(crm).lookup_sender('thomas@example.com')

    assert result['account_id'] == 'account-1'
    assert result['account_owner'] == {
        'id': 'account-owner',
        'name': 'Account Manager',
        'email': 'manager@cab-formations.fr',
    }


def test_lookup_does_not_fallback_to_contact_owner_when_account_owner_is_missing():
    crm = Mock()
    crm.search_contacts.return_value = {'data': [{
        'id': 'contact-1',
        'Email': 'thomas@example.com',
        'Owner': {
            'id': 'contact-owner',
            'name': 'Contact Owner',
            'email': 'contact-owner@cab-formations.fr',
        },
        'Account_Name': {'id': 'account-1', 'name': 'Entreprise Test'},
    }]}
    crm.get_record.return_value = {'id': 'account-1', 'Account_Name': 'Entreprise Test'}
    crm.get_deals_by_contact.return_value = []

    result = RelationsCRMLookup(crm).lookup_sender('thomas@example.com')

    assert result['account_owner'] is None


def test_list_agents_deduplicates_pagination_boundary():
    client = object.__new__(ZohoDeskClient)
    client._get_all_pages = Mock(return_value=[
        {'id': 'agent-1', 'emailId': 'one@example.com'},
        {'id': 'agent-1', 'emailId': 'one@example.com'},
        {'id': 'agent-2', 'emailId': 'two@example.com'},
    ])

    agents = client.list_agents()

    assert [agent['id'] for agent in agents] == ['agent-1', 'agent-2']


def test_list_ticket_threads_deduplicates_pagination_boundary():
    client = object.__new__(ZohoDeskClient)
    client._get_all_pages = Mock(return_value=[
        {'id': 'thread-1', 'status': 'SUCCESS'},
        {'id': 'thread-1', 'status': 'SUCCESS'},
        {'id': 'thread-2', 'status': 'DRAFT'},
    ])

    threads = client.list_ticket_threads('ticket-1')

    assert [thread['id'] for thread in threads] == ['thread-1', 'thread-2']
    client._get_all_pages.assert_called_once()


def test_lookup_rejects_same_email_linked_to_different_accounts():
    crm = Mock()
    crm.search_contacts.return_value = {'data': [
        {
            'id': 'contact-1',
            'Email': 'shared@example.com',
            'Account_Name': {'id': 'account-1', 'name': 'Account One'},
        },
        {
            'id': 'contact-2',
            'Email': 'shared@example.com',
            'Account_Name': {'id': 'account-2', 'name': 'Account Two'},
        },
    ]}

    result = RelationsCRMLookup(crm).lookup_sender('shared@example.com')

    assert result['classification'] == 'ambiguous_crm_contact'
    assert result['contact_matches'] == 2
    assert 'comptes differents' in result['lookup_error']
    crm.get_record.assert_not_called()


def test_lookup_accepts_duplicate_contacts_only_when_account_is_identical():
    crm = Mock()
    crm.search_contacts.return_value = {'data': [
        {
            'id': 'contact-2',
            'Email': 'shared@example.com',
            'Account_Name': {'id': 'account-1', 'name': 'Account One'},
        },
        {
            'id': 'contact-1',
            'Email': 'shared@example.com',
            'Account_Name': {'id': 'account-1', 'name': 'Account One'},
        },
    ]}
    crm.get_record.return_value = {
        'id': 'account-1',
        'Account_Name': 'Account One',
        'Owner': {
            'id': 'owner-1',
            'name': 'Account Manager',
            'email': 'manager@cab-formations.fr',
        },
    }
    crm.get_deals_by_contact.return_value = []

    result = RelationsCRMLookup(crm).lookup_sender('shared@example.com')

    assert result['classification'] == 'client_crm'
    assert result['contact']['id'] == 'contact-1'
    assert result['account_owner']['email'] == 'manager@cab-formations.fr'
