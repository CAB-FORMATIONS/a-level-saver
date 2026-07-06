"""Quick script to analyze timeline API response."""
import sys
import json

sys.stdout.reconfigure(encoding='utf-8')

with open('data/timeline_debug.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

entries = data['__timeline']

skip_fields = {'Modified_Time', 'Modified_By', 'Last_Activity_Time', 'Tag'}

print('=== FIELD CHANGES (avec api_name) ===')
for e in entries:
    if e.get('action') != 'updated':
        continue
    time = e.get('audited_time', '')[:16]
    actor = (e.get('done_by') or {}).get('name', '?')
    source = e.get('source', '')
    automation = (e.get('automation_details') or {}).get('name', '')
    for fh in (e.get('field_history') or []):
        api_name = fh.get('api_name', '?')
        if api_name in skip_fields:
            continue
        val = fh.get('_value', {})
        if isinstance(val, dict):
            old = val.get('old', '')
            new = val.get('new', '')
        else:
            old = fh.get('_previous_value', '')
            new = val
        auto_tag = f'  (via {automation})' if automation else ''
        print(f'{time} | {actor:15} | {api_name:35} | {str(old)[:35]:35} -> {str(new)[:40]}{auto_tag}')

print()
print('=== ALL NOTES ===')
for e in entries:
    action = e.get('action', '')
    if action not in ('added', 'deleted'):
        continue
    record_module = (e.get('record') or {}).get('module', {}).get('api_name', '')
    if record_module != 'Notes':
        continue
    actor = (e.get('done_by') or {}).get('name', '?')
    time = e.get('audited_time', '')[:16]
    source = e.get('source', '')
    note_name = (e.get('record') or {}).get('name', '')[:80]
    print(f'{time} | {actor:15} | {action:8} | src={source:10} | {note_name}')

print()
print('=== EMAILS SENT ===')
for e in entries:
    if e.get('action') != 'sent':
        continue
    actor = (e.get('done_by') or {}).get('name', '?')
    time = e.get('audited_time', '')[:16]
    source = e.get('source', '')
    record_name = (e.get('record') or {}).get('name', '')[:80]
    print(f'{time} | {actor:15} | src={source:10} | {record_name}')

print()
print('=== TRANSITIONS ===')
for e in entries:
    if e.get('action') not in ('transition', 'automatic_transition'):
        continue
    actor = (e.get('done_by') or {}).get('name', '?')
    time = e.get('audited_time', '')[:16]
    for fh in (e.get('field_history') or []):
        val = fh.get('_value', {})
        if isinstance(val, dict):
            old = val.get('old', '')
            new = val.get('new', '')
            print(f'{time} | {actor:15} | {old} -> {new}')

print()
print('=== INFO (pagination) ===')
info = data.get('info', {})
print(json.dumps(info, indent=2, ensure_ascii=False))
