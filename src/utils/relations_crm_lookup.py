"""CRM lookup helpers for Relations entreprises tickets."""

from __future__ import annotations

import logging
import re
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

INTERNAL_DOMAINS = {"cab-formations.fr", "formalogistics.fr", "formalogistics.com", "formalogistics.pro"}
EMAIL_PATTERN = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


def email_domain(email: str) -> str:
    email = normalize_email(email)
    return email.rsplit("@", 1)[1].lower() if "@" in (email or "") else ""


def is_internal_domain(domain: str) -> bool:
    domain = str(domain or "").lower().strip(".")
    return any(domain == internal or domain.endswith(f".{internal}") for internal in INTERNAL_DOMAINS)


def normalize_email(value: str) -> str:
    """Extract bare email from values like 'Name <email@domain.com>'."""
    parsed = parseaddr(value or "")[1]
    email = (parsed or value or "").strip().strip("<>").lower()
    return email if EMAIL_PATTERN.fullmatch(email) else ""


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
            "account_id": "",
            "account_owner": None,
            "contact_matches": 0,
            "lookup_error": "",
        }

        if not email:
            return result
        if is_internal_domain(domain):
            result["classification"] = "internal"
            return result

        try:
            contacts = self._find_contacts_by_email(email)
        except Exception as exc:
            logger.warning("CRM contact lookup failed for %s: %s", email, exc)
            result["lookup_error"] = f"Recherche Contact CRM impossible: {exc}"
            return result
        result["contact_matches"] = len(contacts)

        contact = None
        if len(contacts) == 1:
            contact = contacts[0]
        elif len(contacts) > 1:
            account_ids = {
                str((item.get("Account_Name") or {}).get("id") or "")
                if isinstance(item.get("Account_Name"), dict) else ""
                for item in contacts
            }
            if len(account_ids) != 1 or "" in account_ids:
                result["classification"] = "ambiguous_crm_contact"
                result["lookup_error"] = f"{len(contacts)} Contacts CRM correspondent a cet email avec des comptes differents"
                return result
            contact = sorted(contacts, key=lambda item: str(item.get("id") or ""))[0]

        if contact:
            result["contact"] = contact
            result["classification"] = "client_crm"
            result["contact_name"] = self._format_contact_name(contact)

            account_lookup = contact.get("Account_Name") if isinstance(contact.get("Account_Name"), dict) else None
            if account_lookup and account_lookup.get("id"):
                account = self._get_account(account_lookup["id"])
                result["account"] = account or account_lookup
                result["account_name"] = (account or account_lookup).get("Account_Name") or account_lookup.get("name", "")
                result["account_id"] = str((account or account_lookup).get("id") or account_lookup["id"])
                result["account_owner"] = self._normalize_owner((account or account_lookup).get("Owner"))

            contact_id = contact.get("id")
            if contact_id:
                result["deals"] = self.crm_client.get_deals_by_contact(str(contact_id))[:10]
            return result

        if domain and domain not in FREE_EMAIL_DOMAINS:
            result["classification"] = "prospect_business"
        elif domain in FREE_EMAIL_DOMAINS:
            result["classification"] = "unknown_personal"
        return result

    def _find_contacts_by_email(self, email: str) -> list[dict[str, Any]]:
        response = self.crm_client.search_contacts(f"(Email:equals:{_criteria_safe(email)})")
        records = response.get("data", []) if isinstance(response, dict) else []
        return [record for record in records if isinstance(record, dict)]

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

    @staticmethod
    def _normalize_owner(owner: Any) -> dict[str, str] | None:
        if not isinstance(owner, dict):
            return None
        owner_id = str(owner.get("id") or "").strip()
        name = str(owner.get("name") or "").strip()
        email = normalize_email(owner.get("email") or "")
        if not owner_id or not name or not email:
            return None
        return {"id": owner_id, "name": name, "email": email}
