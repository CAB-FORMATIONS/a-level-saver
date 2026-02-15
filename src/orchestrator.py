"""Orchestrator for coordinating multiple agents in automated workflows."""
import logging
from typing import Dict, Any, List, Optional
from src.agents import DeskTicketAgent, CRMOpportunityAgent, TicketDispatcherAgent, DealLinkingAgent
from src.zoho_client import ZohoDeskClient, ZohoCRMClient
from src.ticket_deal_linker import TicketDealLinker

logger = logging.getLogger(__name__)


class ZohoAutomationOrchestrator:
    """
    Orchestrates automated workflows between Zoho Desk and Zoho CRM.

    This class coordinates multiple agents to handle complex automation scenarios
    such as:
    - Routing tickets to correct departments
    - Processing tickets and updating related opportunities
    - Batch processing of tickets with CRM updates
    - Finding and addressing opportunities that need attention

    Workflow order:
    1. TicketDispatcherAgent - Routes to correct department
    2. DealLinkingAgent - Links ticket to appropriate deal
    3. DeskTicketAgent - Processes and responds to ticket
    4. CRMOpportunityAgent - Updates CRM based on ticket context
    """

    def __init__(self):
        """
        Initialize orchestrator with shared Zoho clients.

        Creates only 2 clients (Desk + CRM) and injects them into all agents
        to share token management and reduce API calls.
        """
        # Create shared clients (TokenManager singleton handles token caching)
        self.desk_client = ZohoDeskClient()
        self.crm_client = ZohoCRMClient()

        # Inject shared clients into all agents
        self.dispatcher_agent = TicketDispatcherAgent(desk_client=self.desk_client)
        self.deal_linking_agent = DealLinkingAgent(
            desk_client=self.desk_client,
            crm_client=self.crm_client
        )
        self.desk_agent = DeskTicketAgent(desk_client=self.desk_client)
        self.crm_agent = CRMOpportunityAgent(crm_client=self.crm_client)
        self.ticket_deal_linker = TicketDealLinker(
            desk_client=self.desk_client,
            crm_client=self.crm_client
        )

    def process_ticket_with_crm_update(
        self,
        ticket_id: str,
        deal_id: str,
        auto_respond: bool = False,
        auto_update_ticket: bool = False,
        auto_update_deal: bool = False,
        auto_add_note: bool = False
    ) -> Dict[str, Any]:
        """
        Process a support ticket and update the related CRM opportunity.

        This is a common workflow where a customer interaction (ticket) should
        influence the sales opportunity status.

        Args:
            ticket_id: Zoho Desk ticket ID
            deal_id: Zoho CRM deal ID
            auto_respond: Auto-respond to the ticket
            auto_update_ticket: Auto-update ticket status
            auto_update_deal: Auto-update deal fields
            auto_add_note: Auto-add notes to the deal

        Returns:
            Combined results from both agents
        """
        logger.info(f"Processing ticket {ticket_id} with CRM update to deal {deal_id}")

        try:
            # Step 1: Process the ticket
            ticket_result = self.desk_agent.process({
                "ticket_id": ticket_id,
                "auto_respond": auto_respond,
                "auto_update": auto_update_ticket
            })

            # Step 2: Update the CRM opportunity based on ticket context
            crm_result = self.crm_agent.process_with_ticket(
                deal_id=deal_id,
                ticket_id=ticket_id,
                ticket_analysis=ticket_result,
                auto_update=auto_update_deal,
                auto_add_note=auto_add_note
            )

            return {
                "success": True,
                "ticket_result": ticket_result,
                "crm_result": crm_result,
                "workflow": "ticket_with_crm_update"
            }

        except Exception as e:
            logger.error(f"Error in ticket-CRM workflow: {e}")
            return {
                "success": False,
                "error": str(e),
                "workflow": "ticket_with_crm_update"
            }

    def process_ticket_complete_workflow(
        self,
        ticket_id: str,
        auto_dispatch: bool = True,
        auto_link: bool = True,
        auto_respond: bool = False,
        auto_update_ticket: bool = False,
        auto_update_deal: bool = False,
        auto_add_note: bool = False
    ) -> Dict[str, Any]:
        """
        Complete end-to-end workflow for processing a ticket.

        This is the RECOMMENDED method for full automation. It:
        1. Links ticket to appropriate deal FIRST (DealLinkingAgent)
        2. Routes ticket to correct department based on DEAL + keywords (TicketDispatcherAgent)
        3. Processes ticket and generates response (DeskTicketAgent)
        4. Updates CRM deal based on ticket context (CRMOpportunityAgent)

        IMPORTANT: Deal linking MUST come before routing because the deal
        determines the department (e.g., Uber €20 → DOC, CMA Closed Lost → Refus CMA).

        Args:
            ticket_id: Zoho Desk ticket ID
            auto_dispatch: Auto-reassign to correct department if needed
            auto_link: Auto-link to deal
            auto_respond: Auto-respond to the ticket
            auto_update_ticket: Auto-update ticket status
            auto_update_deal: Auto-update deal fields
            auto_add_note: Auto-add notes to the deal

        Returns:
            Combined results from all agents in the workflow
        """
        logger.info(f"Starting complete workflow for ticket {ticket_id}")

        workflow_result = {
            "success": True,
            "ticket_id": ticket_id,
            "linking_result": None,
            "dispatch_result": None,
            "ticket_result": None,
            "crm_result": None,
            "workflow": "complete_workflow"
        }

        try:
            # Step 1: Deal linking FIRST (determines department)
            logger.info("Step 1: Linking ticket to deal")
            linking_result = self.deal_linking_agent.process({
                "ticket_id": ticket_id
            })
            workflow_result["linking_result"] = linking_result

            deal_id = None
            deal = None
            if linking_result.get("deal_found"):
                deal_id = linking_result.get("deal_id")
                deal = linking_result.get("deal")  # Get full deal data
                logger.info(f"Linked to deal {deal_id}: {deal.get('Deal_Name', 'Unknown')}")
            else:
                logger.warning(f"No deal found for ticket {ticket_id}")

            # Step 2: Department routing validation (uses deal if available)
            logger.info("Step 2: Validating department routing (with deal context)")
            dispatch_result = self.dispatcher_agent.process({
                "ticket_id": ticket_id,
                "auto_reassign": auto_dispatch,
                "linking_result": linking_result  # Pass full linking result with recommended_department
            })
            workflow_result["dispatch_result"] = dispatch_result

            if dispatch_result.get("should_reassign") and not dispatch_result.get("reassigned"):
                logger.warning(
                    f"Ticket {ticket_id} should be in {dispatch_result['recommended_department']} "
                    f"but is in {dispatch_result['current_department']} (auto_dispatch=False)"
                )

            # Step 3: Process ticket
            logger.info("Step 3: Processing ticket")
            ticket_result = self.desk_agent.process({
                "ticket_id": ticket_id,
                "auto_respond": auto_respond,
                "auto_update": auto_update_ticket
            })
            workflow_result["ticket_result"] = ticket_result

            # Step 4: Update CRM if deal exists
            if deal_id:
                logger.info(f"Step 4: Updating CRM deal {deal_id}")
                crm_result = self.crm_agent.process_with_ticket(
                    deal_id=deal_id,
                    ticket_id=ticket_id,
                    ticket_analysis=ticket_result,
                    auto_update=auto_update_deal,
                    auto_add_note=auto_add_note
                )
                workflow_result["crm_result"] = crm_result
            else:
                logger.info("Step 4: Skipping CRM update (no deal found)")
                workflow_result["crm_result"] = {"skipped": True, "reason": "no_deal_found"}

            logger.info(f"Complete workflow finished for ticket {ticket_id}")
            return workflow_result

        except Exception as e:
            logger.error(f"Error in complete workflow: {e}")
            workflow_result["success"] = False
            workflow_result["error"] = str(e)
            return workflow_result

    def process_ticket_with_auto_crm_link(
        self,
        ticket_id: str,
        auto_respond: bool = False,
        auto_update_ticket: bool = False,
        auto_update_deal: bool = False,
        auto_add_note: bool = False,
        create_bidirectional_link: bool = True
    ) -> Dict[str, Any]:
        """
        Process a ticket and AUTOMATICALLY find and update the related deal.

        NOTE: For full automation including department routing, use
        process_ticket_complete_workflow() instead.

        This method:
        1. Processes the ticket
        2. Automatically finds the related deal using multiple strategies
        3. Creates a bidirectional link (optional)
        4. Updates the deal based on ticket context

        Args:
            ticket_id: Zoho Desk ticket ID
            auto_respond: Auto-respond to the ticket
            auto_update_ticket: Auto-update ticket status
            auto_update_deal: Auto-update deal fields
            auto_add_note: Auto-add notes to the deal
            create_bidirectional_link: Update custom fields to link ticket and deal

        Returns:
            Combined results including deal linking information
        """
        logger.info(f"Processing ticket {ticket_id} with automatic CRM linking")

        try:
            # Step 1: Process the ticket
            ticket_result = self.desk_agent.process({
                "ticket_id": ticket_id,
                "auto_respond": auto_respond,
                "auto_update": auto_update_ticket
            })

            # Step 2: Find the related deal automatically
            deal = self.ticket_deal_linker.find_deal_for_ticket(ticket_id)

            if not deal:
                logger.warning(f"No deal found for ticket {ticket_id}")
                return {
                    "success": True,
                    "ticket_result": ticket_result,
                    "deal_found": False,
                    "crm_result": None,
                    "workflow": "ticket_with_auto_crm_link"
                }

            deal_id = deal.get("id")
            logger.info(f"Found deal {deal_id} for ticket {ticket_id}")

            # Step 3: Create bidirectional link if requested
            if create_bidirectional_link:
                link_result = self.ticket_deal_linker.link_ticket_to_deal_bidirectional(
                    ticket_id, deal_id
                )
                logger.info(f"Bidirectional link result: {link_result}")

            # Step 4: Update the CRM opportunity based on ticket context
            crm_result = self.crm_agent.process_with_ticket(
                deal_id=deal_id,
                ticket_id=ticket_id,
                ticket_analysis=ticket_result,
                auto_update=auto_update_deal,
                auto_add_note=auto_add_note
            )

            return {
                "success": True,
                "ticket_result": ticket_result,
                "deal_found": True,
                "deal_id": deal_id,
                "deal_name": deal.get("Deal_Name"),
                "crm_result": crm_result,
                "workflow": "ticket_with_auto_crm_link"
            }

        except Exception as e:
            logger.error(f"Error in auto-linking workflow: {e}")
            return {
                "success": False,
                "error": str(e),
                "workflow": "ticket_with_auto_crm_link"
            }

    def batch_process_tickets(
        self,
        status: Optional[str] = "Open",
        limit: int = 10,
        auto_respond: bool = False,
        auto_update: bool = False
    ) -> Dict[str, Any]:
        """
        Process multiple tickets in batch.

        Args:
            status: Filter tickets by status
            limit: Maximum number of tickets to process
            auto_respond: Auto-respond to tickets
            auto_update: Auto-update ticket statuses

        Returns:
            Summary of batch processing results
        """
        logger.info(f"Starting batch ticket processing (status={status}, limit={limit})")

        results = self.desk_agent.analyze_ticket_batch(status=status, limit=limit)

        summary = {
            "total_processed": len(results),
            "successful": len([r for r in results if "error" not in r]),
            "failed": len([r for r in results if "error" in r]),
            "escalations_needed": len([
                r for r in results
                if "error" not in r and r.get("agent_analysis", {}).get("should_escalate", False)
            ]),
            "results": results
        }

        # If auto-actions are enabled, process them
        if auto_respond or auto_update:
            for result in results:
                if "error" not in result and not result.get("agent_analysis", {}).get("should_escalate", False):
                    try:
                        self.desk_agent.process({
                            "ticket_id": result["ticket_id"],
                            "auto_respond": auto_respond,
                            "auto_update": auto_update
                        })
                    except Exception as e:
                        logger.error(f"Failed auto-action for ticket {result['ticket_id']}: {e}")

        return summary

    def find_and_update_stale_opportunities(
        self,
        days_stale: int = 14,
        auto_update: bool = False,
        auto_add_note: bool = False
    ) -> Dict[str, Any]:
        """
        Find opportunities that haven't been updated recently and analyze them.

        Args:
            days_stale: Number of days without update to consider stale
            auto_update: Auto-update opportunity fields
            auto_add_note: Auto-add analysis notes

        Returns:
            Summary of stale opportunities and actions taken
        """
        logger.info(f"Finding stale opportunities (>{days_stale} days)")

        # Search for opportunities needing attention
        attention_needed = self.crm_agent.find_opportunities_needing_attention()

        summary = {
            "total_analyzed": len(attention_needed),
            "high_priority": len([a for a in attention_needed if a.get("priority_score", 0) >= 8]),
            "medium_priority": len([a for a in attention_needed if 5 <= a.get("priority_score", 0) < 8]),
            "low_priority": len([a for a in attention_needed if a.get("priority_score", 0) < 5]),
            "opportunities": attention_needed
        }

        # If auto-actions are enabled, process them
        if auto_update or auto_add_note:
            for opportunity in attention_needed:
                try:
                    self.crm_agent.process({
                        "deal_id": opportunity["deal_id"],
                        "auto_update": auto_update,
                        "auto_add_note": auto_add_note
                    })
                except Exception as e:
                    logger.error(f"Failed auto-action for deal {opportunity['deal_id']}: {e}")

        return summary

    def link_ticket_to_deal(
        self,
        ticket_id: str,
        contact_email: str,
    ) -> Optional[str]:
        """
        Find a deal associated with a ticket's contact.

        Args:
            ticket_id: The ticket ID
            contact_email: Contact email to search for in CRM

        Returns:
            Deal ID if found, None otherwise
        """
        logger.info(f"Linking ticket {ticket_id} to deal for contact {contact_email}")

        try:
            # Search for deals associated with this contact
            search_criteria = f"(Contact_Name:equals:{contact_email})"
            search_result = self.crm_client.search_deals(criteria=search_criteria, per_page=1)

            deals = search_result.get("data", [])

            if deals:
                deal_id = deals[0]["id"]
                logger.info(f"Found existing deal {deal_id} for contact {contact_email}")
                return deal_id

            logger.info(f"No deal found for contact {contact_email}")
            return None

        except Exception as e:
            logger.error(f"Error linking ticket to deal: {e}")
            return None

    def run_full_automation_cycle(
        self,
        process_tickets: bool = True,
        update_opportunities: bool = True,
        ticket_status: str = "Open",
        ticket_limit: int = 10,
        auto_actions: bool = False
    ) -> Dict[str, Any]:
        """
        Run a complete automation cycle across both Desk and CRM.

        This is useful for scheduled automation runs.

        Args:
            process_tickets: Whether to process tickets
            update_opportunities: Whether to update opportunities
            ticket_status: Status filter for tickets
            ticket_limit: Max tickets to process
            auto_actions: Whether to auto-execute suggested actions

        Returns:
            Summary of the full automation cycle
        """
        logger.info("Starting full automation cycle")

        results = {
            "timestamp": None,
            "tickets_processed": None,
            "opportunities_updated": None
        }

        # Process tickets
        if process_tickets:
            ticket_results = self.batch_process_tickets(
                status=ticket_status,
                limit=ticket_limit,
                auto_respond=auto_actions,
                auto_update=auto_actions
            )
            results["tickets_processed"] = ticket_results

        # Update opportunities
        if update_opportunities:
            opportunity_results = self.find_and_update_stale_opportunities(
                auto_update=auto_actions,
                auto_add_note=auto_actions
            )
            results["opportunities_updated"] = opportunity_results

        logger.info("Full automation cycle completed")
        return results

    def close(self):
        """Clean up all resources."""
        self.dispatcher_agent.close()
        self.deal_linking_agent.close()
        self.desk_agent.close()
        self.crm_agent.close()
        self.desk_client.close()
        self.crm_client.close()
        self.ticket_deal_linker.close()
