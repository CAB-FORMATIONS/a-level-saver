"""
Build comprehensive tracking file for DOC tickets.
Merges data from:
  - data/test_selection_156_tickets.json (all 156 test tickets)
  - data/test_selection_123_classified.json (draft content classification)
  - data/send_91_results.json (91 successfully sent batch tickets)
Plus hardcoded lists for test tickets, INFO_SESSION sent, manually sent, etc.
"""

import json
from pathlib import Path

BASE = Path(r"C:\Users\fouad\Documents\a-level-saver\data")

# --- Load source files ---
with open(BASE / "test_selection_156_tickets.json", encoding="utf-8") as f:
    data_156 = json.load(f)

with open(BASE / "test_selection_123_classified.json", encoding="utf-8") as f:
    data_123 = json.load(f)

with open(BASE / "send_91_results.json", encoding="utf-8") as f:
    data_91 = json.load(f)

# --- Build lookup maps ---
# 156 tickets: ticket_id -> ticket info
tickets_156 = {t["ticket_id"]: t for t in data_156["tickets"]}

# 123 classified: ticket_id -> classification info
classified_123 = {t["ticket_id"]: t for t in data_123["tickets"]}

# 91 batch sent ticket IDs
batch_sent_ids = {r["ticket_id"] for r in data_91["results"]}

# --- Hardcoded sent lists ---
# 3 test CONFIRMATION_SESSION tickets (sent before the batch)
test_confirmation_ids = {
    "198709000451519662",
    "198709000451527006",
    "198709000451526347",
}

# 7 INFO_SESSION tickets sent
info_session_sent_ids = {
    "198709000451507699",
    "198709000450084237",
    "198709000449840526",
    "198709000448780861",
    "198709000448780020",
    "198709000448251348",
    "198709000447831804",
}

# 2 manually sent by user
manually_sent_ids = {
    "198709000450136577",
    "198709000448279890",
}

# All sent IDs combined
all_sent_ids = batch_sent_ids | test_confirmation_ids | info_session_sent_ids | manually_sent_ids

# 2 INFO_SESSION tickets excluded (draft pending review after workflow re-run)
excluded_review_ids = {
    "198709000451305955",
    "198709000451229327",
}

# --- Build output tickets ---
output_tickets = []

for ticket_id, t156 in tickets_156.items():
    record = {
        "ticket_id": t156["ticket_id"],
        "ticket_number": t156["ticket_number"],
        "subject": t156["subject"],
        "original_group": t156["category"],  # 1_thread_with_draft, etc.
        "category": None,            # from classification
        "processing_status": None,
        "sent_date": None,
        "notes": None,
    }

    # Add classification if available (only the 123 with drafts)
    if ticket_id in classified_123:
        record["category"] = classified_123[ticket_id]["category"]

    # Determine processing status
    if ticket_id in all_sent_ids:
        record["processing_status"] = "sent_and_closed"
        record["sent_date"] = "2026-02-08"
        # Add note for manually sent
        if ticket_id in manually_sent_ids:
            record["notes"] = "sent_manually_by_user"
        elif ticket_id in test_confirmation_ids:
            record["notes"] = "test_ticket"
        elif ticket_id in info_session_sent_ids:
            record["notes"] = "info_session_batch"
    elif ticket_id in excluded_review_ids:
        record["processing_status"] = "draft_pending_review"
        record["notes"] = "workflow_rerun_needs_review"
    elif record["category"] is not None:
        # Classified but not sent = has draft, not processed yet
        record["processing_status"] = "not_processed"
    else:
        # Not in 123 classified = no_draft tickets (the 33 without drafts)
        record["processing_status"] = "not_processed"

    # Clean up None notes
    if record["notes"] is None:
        del record["notes"]

    output_tickets.append(record)

# --- Compute summary ---
status_counts = {}
for t in output_tickets:
    s = t["processing_status"]
    status_counts[s] = status_counts.get(s, 0) + 1

# Category breakdown for not_processed
not_processed_by_cat = {}
for t in output_tickets:
    if t["processing_status"] == "not_processed" and t["category"]:
        cat = t["category"]
        not_processed_by_cat[cat] = not_processed_by_cat.get(cat, 0) + 1

# Sort tickets: sent first, then pending review, then not processed
status_order = {"sent_and_closed": 0, "draft_pending_review": 1, "not_processed": 2}
output_tickets.sort(key=lambda t: (status_order.get(t["processing_status"], 9), t["ticket_id"]))

# --- Build output ---
output = {
    "generated": "2026-02-08",
    "description": "Tracking file for DOC ticket processing (test selection subset of 156 tickets)",
    "total_open_doc": 715,
    "test_selection_subset": 156,
    "summary": {
        "sent_and_closed": status_counts.get("sent_and_closed", 0),
        "draft_pending_review": status_counts.get("draft_pending_review", 0),
        "not_yet_processed": status_counts.get("not_processed", 0),
    },
    "sent_breakdown": {
        "confirmation_session_batch": len(batch_sent_ids),
        "confirmation_session_test": len(test_confirmation_ids),
        "info_session": len(info_session_sent_ids),
        "manually_sent": len(manually_sent_ids),
        "total_sent": len(all_sent_ids),
    },
    "not_processed_breakdown": not_processed_by_cat,
    "tickets": output_tickets,
}

# --- Validate ---
print(f"Total tickets in output: {len(output_tickets)}")
print(f"Sent and closed: {status_counts.get('sent_and_closed', 0)}")
print(f"Draft pending review: {status_counts.get('draft_pending_review', 0)}")
print(f"Not yet processed: {status_counts.get('not_processed', 0)}")
print(f"Sum: {sum(status_counts.values())}")
print()

# Cross-check: all 91 batch IDs should be in the 156
batch_not_in_156 = batch_sent_ids - set(tickets_156.keys())
if batch_not_in_156:
    print(f"WARNING: {len(batch_not_in_156)} batch-sent IDs not in 156 tickets!")
    for i in batch_not_in_156:
        print(f"  {i}")
else:
    print("OK: All 91 batch-sent IDs found in 156 tickets")

# Cross-check: all test IDs in 156
test_not_in_156 = (test_confirmation_ids | info_session_sent_ids | manually_sent_ids) - set(tickets_156.keys())
if test_not_in_156:
    print(f"WARNING: {len(test_not_in_156)} hardcoded sent IDs not in 156 tickets!")
    for i in test_not_in_156:
        print(f"  {i}")
else:
    print("OK: All hardcoded sent IDs found in 156 tickets")

print(f"\nNot processed breakdown:")
for cat, count in sorted(not_processed_by_cat.items()):
    print(f"  {cat}: {count}")

# No-draft tickets (not in 123 classified)
no_draft_count = sum(1 for t in output_tickets if t["category"] is None)
print(f"\nTickets without classification (no draft): {no_draft_count}")

# --- Write output ---
out_path = BASE / "doc_tickets_tracking.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(output, f, indent=2, ensure_ascii=False)

print(f"\nWritten to: {out_path}")
