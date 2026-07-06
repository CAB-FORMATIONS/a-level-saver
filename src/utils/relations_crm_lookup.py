"""CRM lookup helpers for Relations entreprises tickets."""

from __future__ import annotations

import logging
from email.utils import parseaddr
from typing import Any

from src.zoho_client import ZohoCRMClient


logger = logging.getLogger(__name__)

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "hotmail.fr",
    "outlook.com",
    "outlook.fr",
    "yahoo.com",
    "yahoo.fr",
    "icloud.com",
    "live.fr",
    "live.com",
}

INTERNAL_DOMAINS = {"cab-formations.fr", "formalogistics.fr", "formalogistics.com"}


def email_domain(email: str) -> str:
    email = normalize_email(email)
    return email.rsplit("@", 1)[1].lower() if "@" in (email or "") else ""


def normalize_email(value: str) -> str:
    """Extract bare email from values like 'Name <email@domain.com>'."""
    parsed = parseaddr(value or "")[1]
    return (parsed or value or "").strip().strip("<>").lower()


def _criteria_safe(value: str) -> str:
    return str(value or "").replace("(", "").replace(")", "").replace(":", "").strip()


class RelationsCRMLookup:
    """Resolve B2B sender identity in Zoho CRM."""

    def __init__(self, crm_client: ZohoCRMClient | None = None):
        self.crm_client = crm_client or ZohoCRMClient()

    def lookup_sender(self, email: str) -> dict[str, Any]:
        email = normalize_email(email)
        domain = email_domain(email)
        result: dict[str, Any] = {
            "email": email,
            "domain": domain,
            "classification": "unknown",
            "contact": None,
            "account": None,
            "deals": [],
            "contact_name": "",
            "account_name": "",
        }

        if not email:
            return result
        if domain in INTERNAL_DOMAINS:
            result["classification"] = "internal"
            return result

        contact = self._find_contact_by_email(email)
        if contact:
            result["contact"] = contact
            result["classification"] = "client_crm"
            result["contact_name"] = self._format_contact_name(contact)

            account_lookup = contact.get("Account_Name") if isinstance(contact.get("Account_Name"), dict) else None
            if account_lookup and account_lookup.get("id"):
                account = self._get_account(account_lookup["id"])
                result["account"] = account or account_lookup
                result["account_name"] = (account or account_lookup).get("Account_Name") or account_lookup.get("name", "")

            contact_id = contact.get("id")
            if contact_id:
                result["deals"] = self.crm_client.get_deals_by_contact(str(contact_id))[:10]
            return result

        if domain and domain not in FREE_EMAIL_DOMAINS:
            result["classification"] = "prospect_business"
        elif domain in FREE_EMAIL_DOMAINS:
            result["classification"] = "unknown_personal"
        return result

    def _find_contact_by_email(self, email: str) -> dict[str, Any] | None:
        try:
            response = self.crm_client.search_contacts(f"(Email:equals:{_criteria_safe(email)})")
            records = response.get("data", []) if isinstance(response, dict) else []
            return records[0] if records else None
        except Exception as exc:
            logger.warning("CRM contact lookup failed for %s: %s", email, exc)
            return None

    def _get_account(self, account_id: str) -> dict[str, Any] | None:
        try:
            return self.crm_client.get_record("Accounts", account_id)
        except Exception as exc:
            logger.warning("CRM account lookup failed for %s: %s", account_id, exc)
            return None

    @staticmethod
    def _format_contact_name(contact: dict[str, Any]) -> str:
        first = contact.get("First_Name") or ""
        last = contact.get("Last_Name") or ""
        full = f"{first} {last}".strip()
        return full or contact.get("Full_Name") or contact.get("Email") or ""
