"""
Advanced ticket-to-deal linking system for Zoho Desk and CRM integration.

This module provides multiple strategies to link tickets to deals:
1. Direct link via custom fields
2. Department-specific logic (from business rules)
3. Search by contact email
4. Search by contact phone
5. Search by account/organization
6. Recent deal fallback
"""
import logging
from typing import Dict, Any, Optional, List
from src.zoho_client import ZohoDeskClient, ZohoCRMClient
from src.constants.deal_stages import STAGE_CLOSED_WON, STAGE_CLOSED_LOST

logger = logging.getLogger(__name__)

# Try to import business rules for department-specific logic
try:
    from business_rules import BusinessRules
    BUSINESS_RULES_AVAILABLE = True
except ImportError:
    BUSINESS_RULES_AVAILABLE = False
    logger.warning("business_rules.py not available. Department-specific logic disabled.")


class TicketDealLinker:
    """
    Intelligent linking between Zoho Desk tickets and Zoho CRM deals.

    Supports multiple linking strategies with fallback mechanisms.
    """

    def __init__(
        self,
        desk_client: Optional[ZohoDeskClient] = None,
        crm_client: Optional[ZohoCRMClient] = None
    ):
        """
        Initialize TicketDealLinker.

        Args:
            desk_client: Optional ZohoDeskClient instance (creates new one if None)
            crm_client: Optional ZohoCRMClient instance (creates new one if None)
        """
        self.desk_client = desk_client or ZohoDeskClient()
        self.crm_client = crm_client or ZohoCRMClient()

    def find_deal_for_ticket(
        self,
        ticket_id: str,
        strategies: Optional[List[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Find the deal associated with a ticket using multiple strategies.

        Strategies are tried in order until a match is found:
        1. custom_field - Check if ticket has a custom field with deal_id
        2. department_specific - Use department-specific business rules (if configured)
        3. contact_email - Search deals by contact email
        4. contact_phone - Search deals by contact phone
        5. account - Search deals by account/organization
        6. recent_deal - Get most recent deal for the contact

        Args:
            ticket_id: The Zoho Desk ticket ID
            strategies: List of strategies to try (defaults to all)

        Returns:
            Deal dict if found, None otherwise
        """
        if strategies is None:
            strategies = [
                "custom_field",
                "department_specific",  # New: Department-specific logic
                "contact_email",
                "contact_phone",
                "account",
                "recent_deal"
            ]

        logger.info(f"Finding deal for ticket {ticket_id} using strategies: {strategies}")

        # Get ticket details
        try:
            ticket = self.desk_client.get_ticket(ticket_id)
        except Exception as e:
            logger.error(f"Could not fetch ticket {ticket_id}: {e}")
            return None

        # Try each strategy
        for strategy in strategies:
            logger.info(f"Trying strategy: {strategy}")

            if strategy == "custom_field":
                deal = self._find_by_custom_field(ticket)
            elif strategy == "department_specific":
                deal = self._find_by_department_logic(ticket)
            elif strategy == "contact_email":
                deal = self._find_by_contact_email(ticket)
            elif strategy == "contact_phone":
                deal = self._find_by_contact_phone(ticket)
            elif strategy == "account":
                deal = self._find_by_account(ticket)
            elif strategy == "recent_deal":
                deal = self._find_recent_deal(ticket)
            else:
                logger.warning(f"Unknown strategy: {strategy}")
                continue

            if deal:
                logger.info(f"Found deal {deal.get('id')} using strategy: {strategy}")
                return deal

        logger.info(f"No deal found for ticket {ticket_id}")
        return None

    def _find_by_custom_field(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Strategy 1: Check if ticket has a custom field containing deal_id.

        Common custom field names:
        - cf_deal_id
        - cf_zoho_crm_deal_id
        - Deal_ID
        """
        # Check common custom field names
        custom_field_names = [
            "cf_deal_id",
            "cf_zoho_crm_deal_id",
            "Deal_ID",
            "dealId",
            "CRM_Deal_ID"
        ]

        for field_name in custom_field_names:
            deal_id = ticket.get(field_name)
            if deal_id:
                logger.info(f"Found deal_id in custom field {field_name}: {deal_id}")
                try:
                    return self.crm_client.get_deal(deal_id)
                except Exception as e:
                    logger.warning(f"Deal {deal_id} not found: {e}")

        return None

    def _find_by_department_logic(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Strategy 2: Department-specific search logic from business rules.

        Uses custom search criteria defined in BusinessRules.get_deal_search_criteria_for_department()
        This allows department-specific logic with fallback criteria.

        Example for DOC department:
        1. Search Uber €20 deals in "Won" status
        2. If not found, search in "Pending" status
        3. If not found, search in "Lost" status
        """
        if not BUSINESS_RULES_AVAILABLE:
            logger.debug("Business rules not available, skipping department_specific strategy")
            return None

        department = ticket.get("departmentName", "")
        if not department:
            logger.debug("No department in ticket, skipping department_specific strategy")
            return None

        contact = ticket.get("contact", {})
        email = contact.get("email") or contact.get("emailId")

        if not email:
            logger.debug("No contact email, skipping department_specific strategy")
            return None

        # Get department-specific search criteria
        search_criteria_list = BusinessRules.get_deal_search_criteria_for_department(
            department=department,
            contact_email=email,
            ticket=ticket
        )

        if not search_criteria_list:
            logger.debug(f"No department-specific criteria for department: {department}")
            return None

        logger.info(f"Using department-specific logic for department: {department}")
        logger.info(f"Will try {len(search_criteria_list)} search criteria in order")

        # Try each search criteria in order (fallback mechanism)
        for idx, search_config in enumerate(search_criteria_list, 1):
            criteria = search_config.get("criteria")
            description = search_config.get("description", "N/A")
            max_results = search_config.get("max_results", 1)

            logger.info(f"  [{idx}/{len(search_criteria_list)}] Trying: {description}")
            logger.debug(f"  Criteria: {criteria}")

            try:
                result = self.crm_client.search_deals(
                    criteria=criteria,
                    per_page=max_results
                )
                deals = result.get("data", [])

                if deals:
                    deal = deals[0]  # Take the first (most recent if sorted)
                    logger.info(f"  ✅ Found deal via {description}: {deal.get('Deal_Name')} (ID: {deal.get('id')})")
                    return deal
                else:
                    logger.info(f"  ❌ No deal found with: {description}")

            except Exception as e:
                logger.warning(f"  ❌ Search failed for {description}: {e}")
                continue

        logger.info(f"No deal found using any department-specific criteria for {department}")
        return None

    def _find_by_contact_email(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Strategy 3: Search deals by contact email (2-step: Contact → Deals).

        1. Find Contact in CRM by email
        2. Get all Deals associated with that Contact
        3. Return the most recent deal (preferring open deals)
        """
        # Zoho Desk API can return email in two formats:
        # 1. Directly in ticket: ticket["email"]
        # 2. In contact object: ticket["contact"]["email"]
        email = ticket.get("email")
        if not email:
            contact = ticket.get("contact", {})
            email = contact.get("email") or contact.get("emailId")

        if not email:
            logger.debug("No contact email found in ticket")
            return None

        logger.info(f"Step 1: Searching CRM Contact by email: {email}")

        try:
            # Step 1: Search for Contact by email
            result = self.crm_client.search_contacts(
                criteria=f"(Email:equals:{email})",
                per_page=1
            )
            contacts = result.get("data", [])

            if not contacts:
                logger.info(f"  No CRM Contact found with email: {email}")
                return None

            crm_contact = contacts[0]
            contact_id = crm_contact.get("id")
            contact_name = f"{crm_contact.get('First_Name', '')} {crm_contact.get('Last_Name', '')}".strip()

            logger.info(f"  ✅ Found Contact: {contact_name} (ID: {contact_id})")

            # Step 2: Get all Deals for this Contact
            logger.info(f"Step 2: Searching Deals for Contact ID: {contact_id}")
            deals = self.crm_client.get_deals_by_contact(contact_id, per_page=10)

            if not deals:
                logger.info(f"  No deals found for contact {contact_name}")
                return None

            logger.info(f"  Found {len(deals)} deal(s) for {contact_name}")

            # Step 3: Return the most appropriate deal
            # Prioritize: Open deals > Recent deals > Any deal
            open_deals = [d for d in deals if d.get('Stage') not in [STAGE_CLOSED_WON, STAGE_CLOSED_LOST]]

            if open_deals:
                deal = open_deals[0]  # Most recent open deal
                logger.info(f"  ✅ Returning open deal: {deal.get('Deal_Name')} (Amount: {deal.get('Amount')}€)")
                return deal
            else:
                # No open deals, return most recent closed deal
                deal = deals[0]
                logger.info(f"  ✅ Returning closed deal: {deal.get('Deal_Name')} (Amount: {deal.get('Amount')}€)")
                return deal

        except Exception as e:
            logger.warning(f"Error in contact email search: {e}")
            return None

    def _find_by_contact_phone(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Strategy 4: Search deals by contact phone number.
        """
        # Zoho Desk API can return phone in two formats:
        # 1. Directly in ticket: ticket["phone"]
        # 2. In contact object: ticket["contact"]["phone"]
        phone = ticket.get("phone")
        if not phone:
            contact = ticket.get("contact", {})
            phone = contact.get("phone") or contact.get("mobile")

        if not phone:
            logger.debug("No contact phone found in ticket")
            return None

        logger.info(f"Searching deals for contact phone: {phone}")

        try:
            # Clean phone number (remove spaces, dashes, etc.)
            clean_phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

            search_query = f"(Phone:equals:{clean_phone})"
            result = self.crm_client.search_deals(criteria=search_query, per_page=1)
            deals = result.get("data", [])
            if deals:
                return deals[0]

        except Exception as e:
            logger.warning(f"Error searching deals by phone: {e}")

        return None

    def _find_by_account(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Strategy 5: Search deals by account/organization.
        """
        account = ticket.get("accountName") or ticket.get("account", {}).get("name")

        if not account:
            logger.debug("No account found in ticket")
            return None

        logger.info(f"Searching deals for account: {account}")

        try:
            search_query = f"(Account_Name:equals:{account})"
            result = self.crm_client.search_deals(criteria=search_query, per_page=1)
            deals = result.get("data", [])
            if deals:
                return deals[0]

        except Exception as e:
            logger.warning(f"Error searching deals by account: {e}")

        return None

    def _find_recent_deal(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Strategy 6: Get the most recent deal for the contact.

        This is a fallback that gets the most recently modified deal
        for the contact, regardless of stage.
        """
        # Zoho Desk API can return email in two formats:
        # 1. Directly in ticket: ticket["email"]
        # 2. In contact object: ticket["contact"]["email"]
        email = ticket.get("email")
        if not email:
            contact = ticket.get("contact", {})
            email = contact.get("email") or contact.get("emailId")

        if not email:
            logger.debug("No contact email for recent deal search")
            return None

        logger.info(f"Getting most recent deal for contact: {email}")

        try:
            # Search for all deals for this contact, sorted by modified time
            search_query = f"(Email:equals:{email})"
            result = self.crm_client.search_deals(criteria=search_query, per_page=5)
            deals = result.get("data", [])

            if deals:
                # Return the most recently modified deal
                # (API should return them sorted by modified time descending)
                return deals[0]

        except Exception as e:
            logger.warning(f"Error getting recent deal: {e}")

        return None

    def link_ticket_to_deal_bidirectional(
        self,
        ticket_id: str,
        deal_id: str,
        update_ticket_field: str = "cf_deal_id",
        update_deal_field: str = "Ticket_ID"
    ) -> Dict[str, Any]:
        """
        Create a bidirectional link between ticket and deal.

        Updates custom fields on both sides to maintain the link.

        Args:
            ticket_id: Zoho Desk ticket ID
            deal_id: Zoho CRM deal ID
            update_ticket_field: Custom field name in Desk ticket
            update_deal_field: Custom field name in CRM deal

        Returns:
            Dict with status of both updates
        """
        logger.info(f"Creating bidirectional link: Ticket {ticket_id} <-> Deal {deal_id}")

        result = {
            "ticket_updated": False,
            "deal_updated": False,
            "errors": []
        }

        # Update ticket with deal_id
        try:
            self.desk_client.update_ticket(ticket_id, {
                update_ticket_field: deal_id
            })
            result["ticket_updated"] = True
            logger.info(f"Updated ticket {ticket_id} with deal_id {deal_id}")
        except Exception as e:
            error_msg = f"Failed to update ticket: {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

        # Update deal with ticket_id
        try:
            self.crm_client.update_deal(deal_id, {
                update_deal_field: ticket_id
            })
            result["deal_updated"] = True
            logger.info(f"Updated deal {deal_id} with ticket_id {ticket_id}")
        except Exception as e:
            error_msg = f"Failed to update deal: {e}"
            logger.error(error_msg)
            result["errors"].append(error_msg)

        return result

    def auto_link_ticket(
        self,
        ticket_id: str,
        create_bidirectional_link: bool = True
    ) -> Optional[str]:
        """
        Automatically find and link a ticket to its corresponding deal.

        Uses all available strategies to find the deal, then optionally
        creates a bidirectional link.

        Args:
            ticket_id: Zoho Desk ticket ID
            create_bidirectional_link: Whether to update custom fields

        Returns:
            Deal ID if found and linked, None otherwise
        """
        logger.info(f"Auto-linking ticket {ticket_id}")

        # Find the deal
        deal = self.find_deal_for_ticket(ticket_id)

        if not deal:
            logger.info(f"Could not find deal for ticket {ticket_id}")
            return None

        deal_id = deal.get("id")

        # Create bidirectional link if requested
        if create_bidirectional_link:
            link_result = self.link_ticket_to_deal_bidirectional(ticket_id, deal_id)
            if link_result["errors"]:
                logger.warning(f"Errors during bidirectional linking: {link_result['errors']}")

        return deal_id

    def close(self):
        """Clean up resources."""
        self.desk_client.close()
        self.crm_client.close()
