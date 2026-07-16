#!/usr/bin/env python3
"""Batch runner for Relations entreprises draft workflow.

Default mode is dry-run. Use --create-draft to create Zoho Desk drafts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

load_dotenv()

os.environ["PYTHONIOENCODING"] = "utf-8"
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import settings  # noqa: E402
from src.workflows.relations_ticket_workflow import RelationsTicketWorkflow  # noqa: E402
from src.zoho_client import ZohoDeskClient  # noqa: E402


def fetch_relations_tickets(statuses: list[str], limit: int) -> list[dict[str, Any]]:
    desk = ZohoDeskClient()
    tickets: list[dict[str, Any]] = []
    seen: set[str] = set()

    for status in statuses:
        from_index = 0
        while len(tickets) < limit:
            url = f"{settings.zoho_desk_api_url}/tickets"
            params = {
                "orgId": settings.zoho_desk_org_id,
                "departmentId": settings.zoho_desk_relations_department_id,
                "status": status,
                "from": from_index,
                "limit": min(100, limit - len(tickets)),
            }
            page = desk._make_request("GET", url, params=params).get("data", [])
            if not page:
                break
            for ticket in page:
                ticket_id = str(ticket.get("id") or "")
                if ticket_id and ticket_id not in seen:
                    seen.add(ticket_id)
                    tickets.append(ticket)
                    if len(tickets) >= limit:
                        break
            if len(page) < 100:
                break
            from_index += len(page)
    return tickets


def save_results(results: list[dict[str, Any]]) -> str:
    os.makedirs("data", exist_ok=True)
    path = f"data/relations_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2, default=str)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Relations entreprises workflow")
    parser.add_argument("--ticket", help="Process a specific Zoho Desk ticket ID")
    parser.add_argument("--count", type=int, default=10, help="Number of tickets to process")
    parser.add_argument("--status", action="append", default=["Open"], help="Ticket status to fetch; repeatable")
    parser.add_argument("--create-draft", action="store_true", help="Create Zoho Desk drafts")
    parser.add_argument("--add-notes", action="store_true", help="Add internal notes even in dry-run")
    parser.add_argument("--no-save", action="store_true", help="Do not write a JSON result file")
    parser.add_argument("--ignore-existing-draft", action="store_true", help="Dry-run recalculation even if a draft exists")
    args = parser.parse_args()
    if args.create_draft and args.ignore_existing_draft:
        parser.error("--ignore-existing-draft is only allowed without --create-draft")

    workflow = RelationsTicketWorkflow()
    if args.ticket:
        tickets = [{"id": args.ticket, "subject": "specific ticket"}]
    else:
        tickets = fetch_relations_tickets(args.status, args.count)

    mode = "CREATE_DRAFT" if args.create_draft else "DRY_RUN"
    print(f"Relations entreprises workflow - {mode} - {len(tickets)} ticket(s)")

    results = []
    for index, ticket in enumerate(tickets, 1):
        ticket_id = str(ticket["id"])
        print(f"[{index}/{len(tickets)}] {ticket_id} | {(ticket.get('subject') or '')[:80]}")
        result = workflow.process_ticket(
            ticket_id,
            auto_create_draft=args.create_draft,
            auto_update_ticket=args.create_draft or args.add_notes,
            ignore_existing_draft=args.ignore_existing_draft,
        )
        results.append({
            "ticket_id": ticket_id,
            "subject": ticket.get("subject"),
            "success": result.get("success"),
            "stage": result.get("workflow_stage"),
            "sender_email": result.get("sender_email"),
            "intent": (result.get("triage_result") or {}).get("intent"),
            "action": (result.get("triage_result") or {}).get("action"),
            "draft_created": result.get("draft_created"),
            "draft_content": result.get("draft_content"),
            "response_generation": result.get("response_generation"),
            "assignment": result.get("assignment"),
            "validation": result.get("validation"),
            "skip_reason": result.get("skip_reason"),
            "errors": result.get("errors"),
        })
        generation = result.get("response_generation") or {}
        assignment = result.get("assignment") or {}
        source = "ai" if generation.get("used_ai") else "fallback" if generation else "n/a"
        print(
            f"    -> {result.get('workflow_stage')} | draft={result.get('draft_created')} | "
            f"intent={(result.get('triage_result') or {}).get('intent')} | response={source} | "
            f"manager={assignment.get('desk_agent_name') or 'n/a'}"
        )

    if not args.no_save:
        output = save_results(results)
        print(f"Resultats sauvegardes: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
