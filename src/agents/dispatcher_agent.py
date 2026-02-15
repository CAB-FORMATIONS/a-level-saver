"""
Ticket Dispatcher Agent - Routes tickets to the correct department.

This agent analyzes incoming tickets and ensures they are routed to
the appropriate department before any deal linking or automated responses.
"""
import logging
from typing import Dict, Any, Optional
from .base_agent import BaseAgent
from src.zoho_client import ZohoDeskClient
from src.constants.departments import DEPT_DOC

logger = logging.getLogger(__name__)

# Type alias for optional client injection
ZohoDeskClientType = Optional[ZohoDeskClient]

try:
    from business_rules import BusinessRules
    logger.info("Loaded custom business rules for dispatcher")
except ImportError:
    logger.warning("business_rules.py not found. Using default dispatcher rules.")

    class BusinessRules:
        @staticmethod
        def get_department_routing_rules():
            return {}


class TicketDispatcherAgent(BaseAgent):
    """
    Agent that analyzes tickets and routes them to the correct department.

    This agent:
    1. Analyzes ticket content, subject, contact info
    2. Determines the appropriate department based on business rules
    3. Reassigns the ticket if it's in the wrong department
    4. Validates department before allowing further processing
    """

    def __init__(self, desk_client: ZohoDeskClientType = None):
        """
        Initialize TicketDispatcherAgent.

        Args:
            desk_client: Optional ZohoDeskClient instance (creates new one if None)
        """
        system_prompt = """You are a Ticket Dispatcher Agent for Zoho Desk.

Your role is to analyze support tickets and determine which department should handle them.

When analyzing a ticket, consider:
- Subject line keywords
- Ticket description content
- Contact information and history
- Priority and urgency
- Product/service mentioned
- Type of request (sales, support, billing, etc.)

Department routing logic:
- DOC: Educational services, A-Level programs, Uber €20 deals, student questions
- Sales: New business inquiries, pricing questions, quotes, demos
- Support: Technical issues, product help, account problems
- Billing: Payment issues, invoices, refunds
- Customer Success: Renewals, upgrades, satisfaction

You must provide:
1. Recommended department
2. Confidence score (0-100)
3. Reasoning for your recommendation
4. Keywords/signals that influenced your decision

Be precise and consistent in your routing decisions."""

        super().__init__(name="TicketDispatcherAgent", system_prompt=system_prompt)
        self.desk_client = desk_client or ZohoDeskClient()

    def process(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze a ticket and determine correct department routing.

        IMPORTANT: This method should be called AFTER deal linking,
        because the deal determines the department (priority over keywords).

        Args:
            data: Dict with:
                - ticket_id: The ticket to analyze
                - auto_reassign: Whether to automatically reassign (default: False)
                - force_analysis: Force AI analysis even if routing rules match (default: False)
                - linking_result: Optional full result from DealLinkingAgent (NEW)
                - deal: Optional deal data (DEPRECATED - use linking_result instead)

        Returns:
            Dict with:
                - success: bool
                - ticket_id: str
                - current_department: str
                - recommended_department: str
                - should_reassign: bool
                - reassigned: bool (if auto_reassign=True)
                - confidence: int (0-100)
                - reasoning: str
                - signals: list of keywords/patterns found
                - routing_method: "deal" | "business_rules" | "ai_analysis"
        """
        ticket_id = data.get("ticket_id")
        auto_reassign = data.get("auto_reassign", False)
        force_analysis = data.get("force_analysis", False)
        linking_result = data.get("linking_result")  # NEW: Full linking result
        deal = data.get("deal")  # DEPRECATED: For backward compatibility

        if not ticket_id:
            raise ValueError("ticket_id is required")

        logger.info(f"Analyzing ticket {ticket_id} for department routing")

        # Get ticket details
        try:
            ticket = self.desk_client.get_ticket(ticket_id)
        except Exception as e:
            logger.error(f"Could not fetch ticket {ticket_id}: {e}")
            return {
                "success": False,
                "error": str(e),
                "ticket_id": ticket_id
            }

        # Extract current department name from layoutDetails or departmentName
        current_department = "Unknown"
        layout_details = ticket.get("layoutDetails")
        if layout_details and isinstance(layout_details, dict):
            current_department = layout_details.get("layoutName", "Unknown")
        elif ticket.get("departmentName"):
            current_department = ticket.get("departmentName")

        logger.info(f"Current department: {current_department}")

        # Step 1: Check if DealLinkingAgent already determined the department (HIGHEST PRIORITY)
        if linking_result and not force_analysis:
            recommended_dept = linking_result.get("recommended_department")
            if recommended_dept:
                logger.info(f"Using department from DealLinkingAgent: {recommended_dept}")
                logger.info(f"Routing explanation: {linking_result.get('routing_explanation', 'N/A')}")

                should_reassign = (recommended_dept != current_department)

                result = {
                    "success": True,
                    "ticket_id": ticket_id,
                    "current_department": current_department,
                    "recommended_department": recommended_dept,
                    "should_reassign": should_reassign,
                    "confidence": 98,  # Very high confidence for deal-based
                    "method": "deal_linking_agent",
                    "routing_method": "deal",
                    "reasoning": linking_result.get("routing_explanation", "Department determined by DealLinkingAgent"),
                    "signals": [f"Email: {linking_result.get('email', 'N/A')}", f"Deals found: {linking_result.get('deals_found', 0)}"],
                    "deal_id": linking_result.get("deal_id"),
                    "deal_name": linking_result.get("selected_deal", {}).get("Deal_Name") if linking_result.get("selected_deal") else None,
                    "all_deals_count": linking_result.get("deals_found", 0)
                }

                # Auto-reassign if requested
                if should_reassign and auto_reassign:
                    reassign_result = self._reassign_ticket(ticket_id, recommended_dept)
                    result["reassigned"] = reassign_result
                else:
                    result["reassigned"] = False

                return result

        # Step 2: Fallback to old logic if linking_result didn't provide department
        # Check if deal determines the department (BACKWARD COMPATIBILITY)
        if deal and not force_analysis:
            deal_based_department = BusinessRules.get_department_from_deal(deal)
            if deal_based_department:
                logger.info(f"Deal-based routing determined: {deal_based_department}")
                logger.info(f"Deal: {deal.get('Deal_Name', 'Unknown')} (Stage: {deal.get('Stage', 'Unknown')})")

                should_reassign = (deal_based_department != current_department)

                result = {
                    "success": True,
                    "ticket_id": ticket_id,
                    "current_department": current_department,
                    "recommended_department": deal_based_department,
                    "should_reassign": should_reassign,
                    "confidence": 98,  # Very high confidence for deal-based
                    "method": "deal_based_routing",
                    "routing_method": "deal",
                    "reasoning": f"Routed based on CRM deal: {deal.get('Deal_Name', 'Unknown')} (Stage: {deal.get('Stage', 'Unknown')})",
                    "signals": [f"Deal: {deal.get('Deal_Name', 'Unknown')}", f"Stage: {deal.get('Stage', 'Unknown')}"],
                    "deal_id": deal.get("id"),
                    "deal_name": deal.get("Deal_Name")
                }

                # Auto-reassign if requested
                if should_reassign and auto_reassign:
                    reassign_result = self._reassign_ticket(ticket_id, deal_based_department)
                    result["reassigned"] = reassign_result
                else:
                    result["reassigned"] = False

                return result

        # Step 3: Check business rules (keywords) if no deal or deal didn't match
        routing_rules = BusinessRules.get_department_routing_rules()
        rule_based_department = self._check_routing_rules(ticket, routing_rules)

        if rule_based_department and not force_analysis:
            logger.info(f"Rule-based routing determined: {rule_based_department}")

            should_reassign = (rule_based_department != current_department)

            result = {
                "success": True,
                "ticket_id": ticket_id,
                "current_department": current_department,
                "recommended_department": rule_based_department,
                "should_reassign": should_reassign,
                "confidence": 95,  # High confidence for rule-based
                "method": "business_rules",
                "routing_method": "business_rules",
                "reasoning": f"Matched business rule for {rule_based_department} department",
                "signals": self._extract_signals(ticket)
            }

            # Auto-reassign if requested
            if should_reassign and auto_reassign:
                reassign_result = self._reassign_ticket(ticket_id, rule_based_department)
                result["reassigned"] = reassign_result
            else:
                result["reassigned"] = False

            return result

        # Step 4: Use AI analysis if no rule match or forced
        logger.info("Using AI analysis for department routing")
        ai_result = self._analyze_with_ai(ticket)

        recommended_department = ai_result.get("department", current_department)
        should_reassign = (recommended_department != current_department)

        result = {
            "success": True,
            "ticket_id": ticket_id,
            "current_department": current_department,
            "recommended_department": recommended_department,
            "should_reassign": should_reassign,
            "confidence": ai_result.get("confidence", 50),
            "method": "ai_analysis",
            "routing_method": "ai_analysis",
            "reasoning": ai_result.get("reasoning", ""),
            "signals": ai_result.get("signals", [])
        }

        # Auto-reassign if requested
        if should_reassign and auto_reassign:
            reassign_result = self._reassign_ticket(ticket_id, recommended_department)
            result["reassigned"] = reassign_result
        else:
            result["reassigned"] = False

        return result

    def _check_routing_rules(
        self,
        ticket: Dict[str, Any],
        routing_rules: Dict[str, Any]
    ) -> Optional[str]:
        """
        Check business rules for department routing.

        Routing rules format:
        {
            "DOC": {
                "keywords": ["uber", "a-level", "student", "education"],
                "subject_patterns": [".*uber.*", ".*a-level.*"],
                "contact_domains": ["@university.edu"]
            },
            "Sales": {
                "keywords": ["pricing", "quote", "demo", "purchase"],
                ...
            }
        }
        """
        if not routing_rules:
            return None

        subject = ticket.get("subject", "").lower()
        description = ticket.get("description", "").lower()
        contact = ticket.get("contact", {})
        email = (contact.get("email") or contact.get("emailId") or "").lower()

        # Check each department's rules
        for department, rules in routing_rules.items():
            # Check keywords
            keywords = rules.get("keywords", [])
            if keywords:
                for keyword in keywords:
                    if keyword.lower() in subject or keyword.lower() in description:
                        logger.info(f"Keyword '{keyword}' matches {department}")
                        return department

            # Check contact domain
            contact_domains = rules.get("contact_domains", [])
            if contact_domains and email:
                for domain in contact_domains:
                    if domain.lower() in email:
                        logger.info(f"Email domain '{domain}' matches {department}")
                        return department

        return None

    def _analyze_with_ai(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        """
        Use AI to analyze the ticket and recommend a department.
        """
        # Prepare ticket context
        subject = ticket.get("subject", "N/A")
        description = ticket.get("description", "N/A")
        contact = ticket.get("contact", {})
        contact_name = contact.get("name", "N/A")
        contact_email = contact.get("email") or contact.get("emailId", "N/A")
        priority = ticket.get("priority", "N/A")

        # Build analysis prompt
        prompt = f"""Analyze this support ticket and recommend the correct department.

TICKET INFORMATION:
- Subject: {subject}
- Description: {description}
- Contact: {contact_name} ({contact_email})
- Priority: {priority}

AVAILABLE DEPARTMENTS:
- DOC: Educational services, A-Level programs, Uber €20 deals, student questions
- Sales: New business inquiries, pricing questions, quotes, demos
- Support: Technical issues, product help, account problems
- Billing: Payment issues, invoices, refunds
- Customer Success: Renewals, upgrades, satisfaction

Provide your analysis in this exact format:

DEPARTMENT: [department name]
CONFIDENCE: [0-100]
REASONING: [your reasoning]
SIGNALS: [comma-separated keywords/patterns that influenced your decision]
"""

        try:
            response = self.ask(prompt, reset_history=True)

            # Parse the response
            department = None
            confidence = 50
            reasoning = ""
            signals = []

            for line in response.split("\n"):
                line = line.strip()
                if line.startswith("DEPARTMENT:"):
                    department = line.replace("DEPARTMENT:", "").strip()
                elif line.startswith("CONFIDENCE:"):
                    try:
                        confidence = int(line.replace("CONFIDENCE:", "").strip())
                    except ValueError:
                        confidence = 50
                elif line.startswith("REASONING:"):
                    reasoning = line.replace("REASONING:", "").strip()
                elif line.startswith("SIGNALS:"):
                    signals_str = line.replace("SIGNALS:", "").strip()
                    signals = [s.strip() for s in signals_str.split(",")]

            return {
                "department": department,
                "confidence": confidence,
                "reasoning": reasoning,
                "signals": signals
            }

        except Exception as e:
            logger.error(f"AI analysis failed: {e}")
            return {
                "department": ticket.get("departmentName", "Support"),
                "confidence": 0,
                "reasoning": f"AI analysis failed: {e}",
                "signals": []
            }

    def _extract_signals(self, ticket: Dict[str, Any]) -> list:
        """Extract keywords and signals from the ticket."""
        subject = ticket.get("subject", "").lower()
        description = ticket.get("description", "").lower()

        signals = []

        # Common keywords
        keyword_map = {
            "uber": DEPT_DOC,
            "a-level": DEPT_DOC,
            "student": DEPT_DOC,
            "pricing": "Sales",
            "quote": "Sales",
            "demo": "Sales",
            "technical": "Support",
            "error": "Support",
            "invoice": "Billing",
            "payment": "Billing",
            "renewal": "Customer Success"
        }

        for keyword, dept in keyword_map.items():
            if keyword in subject or keyword in description:
                signals.append(f"{keyword} ({dept})")

        return signals

    def _reassign_ticket(self, ticket_id: str, new_department: str) -> bool:
        """
        Reassign ticket to a new department.

        Args:
            ticket_id: Ticket ID to reassign
            new_department: Department name (e.g., "Contact", "DOC", "Refus CMA")

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Reassigning ticket {ticket_id} to department: {new_department}")

            # Use move_ticket_to_department which handles layoutId properly
            self.desk_client.move_ticket_to_department(
                ticket_id=ticket_id,
                department_name=new_department
            )

            logger.info(f"Successfully reassigned ticket {ticket_id} to {new_department}")
            return True

        except Exception as e:
            logger.error(f"Failed to reassign ticket {ticket_id}: {e}")
            return False

    def batch_validate_departments(
        self,
        status: str = "Open",
        limit: Optional[int] = None,
        use_pagination: bool = False
    ) -> Dict[str, Any]:
        """
        Validate department assignments for a batch of tickets.

        This is useful for auditing and identifying misrouted tickets.

        Args:
            status: Ticket status filter (e.g., "Open")
            limit: Maximum number of tickets to check (None for all if use_pagination=True)
            use_pagination: If True, fetches ALL tickets with automatic pagination

        Returns:
            Dict with:
                - total_checked: int
                - correct_department: int
                - should_reassign: int
                - results: list of ticket results
        """
        if use_pagination:
            logger.info(f"Batch validating departments for ALL {status} tickets (with pagination)")
            tickets_data = self.desk_client.list_all_tickets(status=status)
            # Apply limit after fetching all if specified
            if limit:
                tickets_data = tickets_data[:limit]
        else:
            limit = limit or 50  # Default to 50 if not specified
            logger.info(f"Batch validating departments for {status} tickets (limit: {limit})")
            response = self.desk_client.list_tickets(status=status, limit=limit)
            tickets_data = response.get("data", [])

        results = []
        correct_count = 0
        should_reassign_count = 0

        for ticket in tickets_data:
            ticket_id = ticket.get("id")

            # Analyze each ticket
            result = self.process({
                "ticket_id": ticket_id,
                "auto_reassign": False
            })

            if result.get("success"):
                results.append(result)

                if not result.get("should_reassign"):
                    correct_count += 1
                else:
                    should_reassign_count += 1

        return {
            "success": True,
            "total_checked": len(results),
            "correct_department": correct_count,
            "should_reassign": should_reassign_count,
            "results": results
        }

    def close(self):
        """Clean up resources."""
        # No persistent connections to close
        pass
