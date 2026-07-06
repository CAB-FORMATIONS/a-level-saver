"""Run 18 tickets in parallel (3 batches of 6)."""
import sys
import io
import os
import json
import logging
import concurrent.futures
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Skip draft check
os.environ['SKIP_DRAFT_CHECK'] = '1'

from src.workflows.doc_ticket_workflow import DOCTicketWorkflow

TICKETS = [
    "198709000447569065", "198709000447766435", "198709000448261767",
    "198709000448587752", "198709000448650742", "198709000448777173",
    "198709000448794284", "198709000448823374", "198709000449792002",
    "198709000450066055", "198709000450080411", "198709000450189575",
    "198709000451175874", "198709000451182484", "198709000451202903",
    "198709000451293832", "198709000451443261", "198709000451467065",
]

BATCH_SIZE = 6

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def process_ticket(ticket_id):
    """Process a single ticket."""
    try:
        workflow = DOCTicketWorkflow()
        result = workflow.process_ticket(
            ticket_id,
            auto_create_draft=True,
            auto_update_crm=True,
            auto_update_ticket=True
        )
        success = result.get('success', False)
        stage = result.get('workflow_stage', 'UNKNOWN')
        draft = result.get('draft_created', False)
        return {
            'ticket_id': ticket_id,
            'success': success,
            'stage': stage,
            'draft_created': draft,
            'error': None
        }
    except Exception as e:
        return {
            'ticket_id': ticket_id,
            'success': False,
            'stage': 'ERROR',
            'draft_created': False,
            'error': str(e)
        }

def main():
    all_results = []
    total = len(TICKETS)

    for batch_idx in range(0, total, BATCH_SIZE):
        batch = TICKETS[batch_idx:batch_idx + BATCH_SIZE]
        batch_num = batch_idx // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        print(f"\n{'='*60}")
        print(f"BATCH {batch_num}/{total_batches} - {len(batch)} tickets")
        print(f"{'='*60}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
            futures = {executor.submit(process_ticket, tid): tid for tid in batch}
            for future in concurrent.futures.as_completed(futures):
                tid = futures[future]
                result = future.result()
                all_results.append(result)
                status = "OK" if result['success'] else "FAIL"
                draft = "DRAFT" if result['draft_created'] else "NO-DRAFT"
                err = f" | ERROR: {result['error'][:60]}" if result['error'] else ""
                print(f"  [{status}] {tid} | {result['stage']} | {draft}{err}")

    # Summary
    print(f"\n{'='*60}")
    print(f"RÉSUMÉ")
    print(f"{'='*60}")
    ok = sum(1 for r in all_results if r['success'])
    drafts = sum(1 for r in all_results if r['draft_created'])
    print(f"Total: {len(all_results)}")
    print(f"Succès: {ok}")
    print(f"Drafts créés: {drafts}")
    print(f"Échecs: {len(all_results) - ok}")

    failed = [r for r in all_results if not r['success']]
    if failed:
        print(f"\nÉchecs:")
        for r in failed:
            print(f"  {r['ticket_id']} | {r['stage']} | {r['error']}")

    # Save results
    output_file = f"data/batch_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}_18tickets.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)
    print(f"\nRésultats sauvegardés: {output_file}")

if __name__ == '__main__':
    main()
