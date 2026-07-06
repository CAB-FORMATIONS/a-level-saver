import json

with open('doc_tickets_processed.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

today = [t for t in data if t.get('processed_at', '').startswith('2026-02-1')]
d10 = [t for t in today if t.get('state_id') == 'D-10']

print(f"D-10 (pas de deal CRM): {len(d10)} tickets")
print()

# Subjects - check patterns
subjects = {}
for t in d10:
    subj = (t.get('subject') or '')[:80]
    subjects[subj] = subjects.get(subj, 0) + 1

print("SUJETS (top 20):")
for s, c in sorted(subjects.items(), key=lambda x: -x[1])[:20]:
    print(f"  [{c}x] {s}")

print()

# Emails - check for patterns
emails = {}
for t in d10:
    email = t.get('email') or 'N/A'
    domain = email.split('@')[-1] if '@' in email else 'N/A'
    emails[domain] = emails.get(domain, 0) + 1

print("DOMAINES EMAIL (top 10):")
for e, c in sorted(emails.items(), key=lambda x: -x[1])[:10]:
    print(f"  {e}: {c}")

print()

# Draft created?
drafts = sum(1 for t in d10 if t.get('draft_created'))
no_drafts = sum(1 for t in d10 if not t.get('draft_created'))
print(f"Drafts créés: {drafts} | Sans draft: {no_drafts}")

# Sample ticket IDs
print()
print("ÉCHANTILLON (10 premiers):")
for t in d10[:10]:
    subj = (t.get('subject') or '')[:60]
    print(f"  {t.get('id')} | {t.get('email', 'N/A')[:30]} | {subj}")
