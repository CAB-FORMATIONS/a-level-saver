"""
Find open DOC tickets with subject "RE: Test de sélection réussi - Examen VTC"
and only 1 thread (single inbound message).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from src.zoho_client import ZohoDeskClient

DOC_DEPT_ID = "198709000025523146"
TARGET_SUBJECT = "Test de sélection réussi - Examen VTC"

def main():
    client = ZohoDeskClient()

    # Step 1: Paginate through all Open tickets
    all_matching = []
    from_index = 0
    total_scanned = 0
    total_doc = 0

    print(f"Scanning open tickets in DOC department...")
    print(f"Target subject: *{TARGET_SUBJECT}*")
    print()

    while True:
        result = client.list_tickets(status='Open', limit=100, from_index=from_index)
        tickets = result.get('data', [])
        if not tickets:
            break

        total_scanned += len(tickets)

        # Filter DOC department + matching subject
        for t in tickets:
            if t.get('departmentId') != DOC_DEPT_ID:
                continue
            total_doc += 1

            subject = t.get('subject', '')
            if TARGET_SUBJECT.lower() in subject.lower():
                all_matching.append(t)

        print(f"  Scanned {total_scanned} tickets ({total_doc} DOC, {len(all_matching)} matching subject)...")

        if len(tickets) < 100:
            break
        from_index += 100
        if from_index > 5000:  # Safety limit
            print("  (Safety limit reached at 5000)")
            break

    print(f"\nTotal: {total_scanned} tickets scanned, {total_doc} in DOC, {len(all_matching)} matching subject")

    if not all_matching:
        print("No matching tickets found.")
        return

    # Step 2: Check thread count for each matching ticket
    print(f"\nChecking thread count for {len(all_matching)} tickets...")
    single_thread = []

    for i, t in enumerate(all_matching):
        ticket_id = t.get('id')
        subject = t.get('subject', '')
        try:
            threads_resp = client.get_ticket_threads(ticket_id)
            threads = threads_resp.get('data', [])
            thread_count = len(threads)
        except Exception as e:
            thread_count = -1
            print(f"  Error getting threads for {ticket_id}: {e}")

        marker = " <<<" if thread_count == 1 else ""
        if (i + 1) % 10 == 0 or thread_count == 1:
            print(f"  [{i+1}/{len(all_matching)}] {ticket_id} — {thread_count} thread(s) — {subject[:60]}{marker}")

        if thread_count == 1:
            single_thread.append({
                'id': ticket_id,
                'subject': subject,
                'createdTime': t.get('createdTime', ''),
                'contactId': t.get('contactId', ''),
                'threadCount': thread_count
            })

    # Step 3: Report
    print(f"\n{'='*70}")
    print(f"RÉSULTAT: {len(single_thread)} tickets avec subject matching ET 1 seul thread")
    print(f"{'='*70}")

    for t in single_thread:
        print(f"  {t['id']} — créé {t['createdTime'][:10]} — {t['subject'][:70]}")

    # Save IDs for batch processing
    if single_thread:
        ids = [t['id'] for t in single_thread]
        print(f"\nIDs (pour batch): {ids}")

        import json
        with open('data/test_selection_single_thread.json', 'w', encoding='utf-8') as f:
            json.dump({
                'description': f'Open DOC tickets with subject "{TARGET_SUBJECT}" and 1 thread',
                'count': len(single_thread),
                'tickets': single_thread,
                'ticket_ids': ids
            }, f, indent=2, ensure_ascii=False)
        print(f"Saved to data/test_selection_single_thread.json")

if __name__ == '__main__':
    main()
