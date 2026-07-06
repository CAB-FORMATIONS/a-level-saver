import json

with open('doc_tickets_processed.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

today = [t for t in data if t.get('processed_at', '').startswith('2026-02-1')]
total = len(today)

actions = {}
for t in today:
    a = t.get('triage_action') or 'N/A'
    actions[a] = actions.get(a, 0) + 1

stages = {}
for t in today:
    s = t.get('workflow_stage') or 'N/A'
    stages[s] = stages.get(s, 0) + 1

states = {}
for t in today:
    s = t.get('state_id') or 'N/A'
    if s != 'N/A':
        states[s] = states.get(s, 0) + 1

ok = sum(1 for t in today if t.get('success'))
err = sum(1 for t in today if not t.get('success'))
drafts = sum(1 for t in today if t.get('draft_created'))
crm = sum(1 for t in today if t.get('crm_updated'))

print(f'BATCH - {total} tickets')
print(f'OK: {ok} | Erreurs: {err} | Drafts: {drafts} | CRM: {crm}')
print()
print('TRIAGE:')
for a, c in sorted(actions.items(), key=lambda x: -x[1]):
    print(f'  {a}: {c}')
print()
print('STAGE:')
for s, c in sorted(stages.items(), key=lambda x: -x[1]):
    print(f'  {s}: {c}')
print()
print('ETATS (top 15):')
for s, c in sorted(states.items(), key=lambda x: -x[1])[:15]:
    print(f'  {s}: {c}')
