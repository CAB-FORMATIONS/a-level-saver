"""
Investigate when Compte_Uber was set to true for 3 email addresses.

For each email:
1. Search contacts by email -> get deals via contact
2. Also search deals directly by email (backup)
3. Find the 20 EUR GAGNE deal
4. Get deal timeline (with pagination)
5. Find Compte_Uber field change
"""
import sys
import os
import logging
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(level=logging.WARNING)

from src.zoho_client import ZohoCRMClient
from config import settings

EMAILS = [
    "fsher1@hotmail.com",
    "Jacquescherine@gmail.com",
    "abdulkader.fof@icloud.com",
]


def find_20e_gagne_deal(deals):
    """Find the deal that is 20 EUR GAGNE (won deal with 20 EUR amount)."""
    for deal in deals:
        deal_name = deal.get('Deal_Name', '')
        stage = deal.get('Stage', '')
        amount = deal.get('Amount', 0)
        is_20e = ('20' in str(deal_name) or amount == 20 or amount == 20.0)
        is_gagne = ('GAGN' in str(stage).upper())
        if is_20e and is_gagne:
            return deal

    for deal in deals:
        stage = deal.get('Stage', '')
        if 'GAGN' in str(stage).upper():
            return deal

    for deal in deals:
        deal_name = deal.get('Deal_Name', '')
        if '20' in str(deal_name):
            return deal

    return None


def get_all_timeline_pages(crm, deal_id):
    """Get timeline with pagination to cover full history."""
    all_entries = []
    page = 1
    per_page = 200

    while True:
        base = settings.zoho_crm_api_url.replace('/v3', '/v8')
        url = f"{base}/Deals/{deal_id}/__timeline"
        params = {"page": page, "per_page": per_page}
        try:
            response = crm._make_request("GET", url, params=params)
        except Exception as e:
            print(f"      Timeline page {page} error: {e}")
            break

        entries = response.get('__timeline', []) if response else []
        if not entries:
            break

        all_entries.extend(entries)

        info = response.get('info', {})
        if not info.get('more_records', False):
            break
        page += 1

    return {'__timeline': all_entries}


def parse_timeline_for_compte_uber(timeline_response):
    """Parse raw timeline looking for Compte_Uber field changes."""
    results = []

    if not timeline_response or not isinstance(timeline_response, dict):
        return results

    entries = timeline_response.get('__timeline', [])
    if not entries or not isinstance(entries, list):
        return results

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        action = entry.get('action', '')
        if action != 'updated':
            continue

        done_by = entry.get('done_by', {})
        actor = done_by.get('name', '') if isinstance(done_by, dict) else str(done_by)
        timestamp = entry.get('audited_time') or entry.get('done_time', '')
        source = entry.get('source', '')

        field_history = entry.get('field_history', [])
        if not isinstance(field_history, list):
            continue

        for fh in field_history:
            if not isinstance(fh, dict):
                continue
            api_name = fh.get('api_name', '')

            if 'compte' in api_name.lower() or 'uber' in api_name.lower():
                value_obj = fh.get('_value', {})
                if not isinstance(value_obj, dict):
                    value_obj = {}

                old_val = value_obj.get('old', '')
                new_val = value_obj.get('new', '')

                results.append({
                    'field': api_name,
                    'old_value': old_val,
                    'new_value': new_val,
                    'actor': actor,
                    'timestamp': timestamp,
                    'source': source,
                })

    return results


def get_deals_for_email(crm, email):
    """Try multiple approaches to find deals for an email."""
    all_deals = []
    seen_ids = set()

    # Approach 1: Search contacts first, then get their deals
    print(f"  [1] Searching contacts for {email}...")
    try:
        contact_result = crm.search_contacts(f"(Email:equals:{email})")
        contacts = contact_result.get('data', [])
        print(f"      Found {len(contacts)} contact(s)")

        for contact in contacts:
            contact_id = contact.get('id')
            contact_name = contact.get('Full_Name', contact.get('Last_Name', 'N/A'))
            print(f"      Contact: {contact_name} (ID: {contact_id})")

            deals = crm.get_deals_by_contact(contact_id)
            print(f"      Deals from contact: {len(deals)}")
            for d in deals:
                if d.get('id') not in seen_ids:
                    all_deals.append(d)
                    seen_ids.add(d.get('id'))
    except Exception as e:
        print(f"      Error: {e}")

    # Approach 2: Search deals directly by email field
    print(f"  [2] Searching deals by email field...")
    try:
        deals = crm.search_deals_by_email(email)
        print(f"      Found {len(deals)} deal(s)")
        for d in deals:
            if d.get('id') not in seen_ids:
                all_deals.append(d)
                seen_ids.add(d.get('id'))
    except Exception as e:
        print(f"      Error: {e}")

    # Approach 3: Try lowercase email search on contacts (Zoho can be case-sensitive)
    email_lower = email.lower()
    if email_lower != email:
        print(f"  [3] Trying lowercase: {email_lower}...")
        try:
            contact_result = crm.search_contacts(f"(Email:equals:{email_lower})")
            contacts = contact_result.get('data', [])
            print(f"      Found {len(contacts)} contact(s)")
            for contact in contacts:
                contact_id = contact.get('id')
                deals = crm.get_deals_by_contact(contact_id)
                for d in deals:
                    if d.get('id') not in seen_ids:
                        all_deals.append(d)
                        seen_ids.add(d.get('id'))
        except Exception as e:
            print(f"      Error: {e}")

    return all_deals


def main():
    crm = ZohoCRMClient()

    print("=" * 130)
    print("COMPTE_UBER FIELD CHANGE INVESTIGATION")
    print("=" * 130)

    summary_rows = []

    for email in EMAILS:
        print(f"\n{'=' * 80}")
        print(f"EMAIL: {email}")
        print(f"{'=' * 80}")

        deals = get_deals_for_email(crm, email)

        if not deals:
            print(f"\n  *** NO DEALS FOUND for {email} ***")
            summary_rows.append((email, 'NO DEALS FOUND', '-', '-', '-', '-', '-'))
            continue

        print(f"\n  Total deals found: {len(deals)}")
        for d in deals:
            print(f"    - {d.get('Deal_Name', 'N/A')} | Stage: {d.get('Stage', 'N/A')} | Amount: {d.get('Amount', 'N/A')} | ID: {d.get('id', 'N/A')} | Compte_Uber: {d.get('Compte_Uber', '?')}")

        target_deal = find_20e_gagne_deal(deals)

        if target_deal:
            print(f"\n  >>> Target deal: {target_deal.get('Deal_Name')} (ID: {target_deal.get('id')})")
            deals_to_check = [target_deal]
        else:
            print(f"\n  No specific GAGNE deal found. Checking ALL deals...")
            deals_to_check = deals

        found_any = False
        for deal in deals_to_check:
            deal_id = deal.get('id')
            deal_name = deal.get('Deal_Name', 'N/A')

            print(f"\n  --- Checking timeline for deal: {deal_name} (ID: {deal_id}) ---")

            try:
                timeline = get_all_timeline_pages(crm, deal_id)
                entries = timeline.get('__timeline', [])
                print(f"      Timeline entries: {len(entries)}")
            except Exception as e:
                print(f"      ERROR getting timeline: {e}")
                continue

            changes = parse_timeline_for_compte_uber(timeline)

            if changes:
                found_any = True
                for c in changes:
                    print(f"\n      *** FOUND COMPTE_UBER CHANGE ***")
                    print(f"      Field:      {c['field']}")
                    print(f"      Old value:  {c['old_value']}")
                    print(f"      New value:  {c['new_value']}")
                    print(f"      Changed by: {c['actor']}")
                    print(f"      When:       {c['timestamp']}")
                    print(f"      Source:     {c['source']}")
                    summary_rows.append((email, deal_name, deal_id, c['actor'], str(c['timestamp']), str(c['old_value']), str(c['new_value'])))
            else:
                print(f"      No Compte_Uber/Uber changes found in timeline.")
                all_fields = set()
                for entry in entries:
                    if not isinstance(entry, dict) or entry.get('action') != 'updated':
                        continue
                    for fh in entry.get('field_history', []):
                        all_fields.add(fh.get('api_name', ''))

                if all_fields:
                    print(f"      All fields in timeline ({len(all_fields)}): {sorted(all_fields)}")

        if not found_any:
            dn = deals_to_check[0].get('Deal_Name', 'N/A') if deals_to_check else 'N/A'
            di = deals_to_check[0].get('id', '-') if deals_to_check else '-'
            summary_rows.append((email, dn, di, 'N/A', 'NO CHANGE FOUND', '-', '-'))

    # Summary table
    print(f"\n\n{'=' * 155}")
    print("SUMMARY TABLE")
    print(f"{'=' * 155}")
    print(f"{'EMAIL':<35} | {'DEAL NAME':<30} | {'DEAL ID':<22} | {'CHANGED BY':<20} | {'WHEN':<30} | {'OLD':<10} | {'NEW':<10}")
    print("-" * 155)
    for row in summary_rows:
        print(f"{row[0]:<35} | {row[1]:<30} | {row[2]:<22} | {row[3]:<20} | {row[4]:<30} | {row[5]:<10} | {row[6]:<10}")
    print("=" * 155)


if __name__ == "__main__":
    main()
