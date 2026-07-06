"""Audit des notes META — analyse exact match vs wildcard vs fallback."""
import json
import re
import yaml
import sys
import io
from datetime import datetime, timedelta
from collections import Counter

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from src.workflows.doc_ticket_workflow import DOCTicketWorkflow

w = DOCTicketWorkflow()
desk = w.desk_client
crm = w.crm_client

# Load matrix
with open('states/state_intention_matrix.yaml', 'r', encoding='utf-8') as f:
    m = yaml.safe_load(f)
matrix = m.get('matrix', {})
exact_entries = {k for k in matrix if ':' in k and not k.startswith('*:')}
wildcard_entries = {k for k in matrix if k.startswith('*:')}
print(f"Matrice: {len(exact_entries)} exact, {len(wildcard_entries)} wildcards")

# 1. Fetch closed tickets (15 days)
cutoff = (datetime.utcnow() - timedelta(days=15)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
all_tickets = []
offset = 0
stop = False
while not stop:
    batch = desk.list_tickets(status='Closed', limit=100, from_index=offset)
    data = batch.get('data', []) if isinstance(batch, dict) else []
    if not data:
        break
    for t in data:
        if t.get('closedTime', '') >= cutoff:
            all_tickets.append(t)
        else:
            stop = True
            break
    offset += len(data)
    if len(data) < 100:
        break

print(f"Tickets fermes (15j): {len(all_tickets)}")

# 2. Get deal IDs from cf_opportunite
deal_map = {}
no_deal = 0
for i, t in enumerate(all_tickets):
    try:
        full = desk.get_ticket(str(t['id']))
        cf_url = full.get('cf', {}).get('cf_opportunite', '')
        if cf_url:
            match = re.search(r'/(\d{16,})', cf_url)
            if match:
                deal_map.setdefault(match.group(1), []).append(str(t['id']))
                continue
        no_deal += 1
    except Exception as e:
        no_deal += 1
    if (i + 1) % 100 == 0:
        print(f"  ... {i+1}/{len(all_tickets)} tickets, {len(deal_map)} deals")

print(f"Deals uniques: {len(deal_map)}, sans deal: {no_deal}")

# 3. Fetch META notes
all_metas = []
for i, did in enumerate(deal_map):
    try:
        resp = crm.get_deal_notes(did)
        notes = resp.get('data', []) if isinstance(resp, dict) else []
        for note in notes:
            content = note.get('Note_Content', '')
            if '[META]' not in content:
                continue
            meta_line = content.split('\n')[0]
            fields = {}
            for part in meta_line.replace('[META] ', '').split(' | '):
                if '=' in part:
                    k, v = part.split('=', 1)
                    fields[k.strip()] = v.strip()
            if fields.get('state') and fields.get('intent'):
                fields['deal_id'] = did
                all_metas.append(fields)
    except:
        pass
    if (i + 1) % 100 == 0:
        print(f"  ... {i+1}/{len(deal_map)} deals, {len(all_metas)} metas")

print(f"Notes META: {len(all_metas)}")

# 4. Classify
results = []
for meta in all_metas:
    state = meta['state']
    intent = meta['intent']
    combo = state + ':' + intent
    if combo in exact_entries:
        mt = 'exact'
    elif ('*:' + intent) in wildcard_entries:
        mt = 'wildcard'
    else:
        mt = 'fallback'
    results.append({
        'state': state, 'intent': intent, 'combo': combo,
        'match_type': mt, 'evalbox': meta.get('evalbox', ''),
        'date_case': meta.get('date_case', ''),
    })

# 5. Stats
total = len(results)
ec = sum(1 for r in results if r['match_type'] == 'exact')
wc_count = sum(1 for r in results if r['match_type'] == 'wildcard')
fc = sum(1 for r in results if r['match_type'] == 'fallback')

print(f"\n{'=' * 70}")
print(f"AUDIT MATRICE - {total} reponses (15 derniers jours)")
print(f"{'=' * 70}")
pct = lambda n: str(100 * n // max(total, 1)) + '%'
print(f"Exact:    {ec} ({pct(ec)})")
print(f"Wildcard: {wc_count} ({pct(wc_count)})")
print(f"Fallback: {fc} ({pct(fc)})")

wc = Counter(r['combo'] for r in results if r['match_type'] == 'wildcard')
fb = Counter(r['combo'] for r in results if r['match_type'] == 'fallback')
ex = Counter(r['combo'] for r in results if r['match_type'] == 'exact')

print(f"\n--- TOP 20 WILDCARDS ---")
for combo, count in wc.most_common(20):
    print(f"  {count:3d}x  {combo}")

if fc:
    print(f"\n--- FALLBACKS ---")
    for combo, count in fb.most_common(10):
        print(f"  {count:3d}x  {combo}")

print(f"\n--- TOP 10 EXACT ---")
for combo, count in ex.most_common(10):
    print(f"  {count:3d}x  {combo}")

# Save
with open('data/meta_audit_15days.json', 'w', encoding='utf-8') as f:
    json.dump({
        'generated': datetime.utcnow().isoformat(),
        'total_tickets': len(all_tickets),
        'unique_deals': len(deal_map),
        'total_metas': len(all_metas),
        'stats': {'exact': ec, 'wildcard': wc_count, 'fallback': fc},
        'top_wildcards': wc.most_common(30),
        'top_fallbacks': fb.most_common(10),
        'top_exact': ex.most_common(20),
    }, f, indent=2, ensure_ascii=False)
print("\nSaved: data/meta_audit_15days.json")
w.close()
