"""Analyze and categorize tickets moved from Contact back to DOC."""
import sys
import json
import re
from collections import Counter
from config import settings
from src.zoho_client import ZohoDeskClient

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

client = ZohoDeskClient()

with open('data/contact_tickets_to_move.json', 'r', encoding='utf-8') as f:
    tickets = json.load(f)

active_tickets = [t for t in tickets if t['status'] not in ['Fermé', 'Ferme', 'Closed']]
print(f'Reading threads for {len(active_tickets)} active tickets...')

annul_kw = ['annul', 'rembours', 'retract', 'desist', 'supprim', 'abandon']
contest_kw = ['arnaque', 'escroqu', 'trompe', 'malentendu', 'pas ce que', 'pensais que', 'contestat']

for i, t in enumerate(active_tickets):
    tid = t['id']
    num = t['ticketNumber']
    try:
        thread_list = client.get_all_threads_with_full_content(tid)

        customer_msgs = []
        all_text = ''
        for th in thread_list:
            from_email = th.get('fromEmailAddress', '') or ''
            content = th.get('content', '') or th.get('plainText', '') or ''
            clean = re.sub(r'<[^>]+>', ' ', content)
            clean = re.sub(r'\s+', ' ', clean).strip()
            if 'cab-formations' not in from_email.lower():
                customer_msgs.append(clean[:500])
            all_text += ' ' + clean.lower()

        summary = ' | '.join(customer_msgs)[:600]

        has_annul = any(kw in all_text for kw in annul_kw)
        has_contest = any(kw in all_text for kw in contest_kw)

        category = 'AUTRE'
        if has_annul and has_contest:
            category = 'CONTESTATION_ANNULATION'
        elif has_annul:
            category = 'DEMANDE_ANNULATION'
        elif has_contest:
            category = 'CONTESTATION'
        elif any(kw in all_text for kw in ['reinscri', 'echoue', 'echec', 'rate', 'repass']):
            category = 'REINSCRIPTION'
        elif any(kw in all_text for kw in ['report', 'decal', 'changer date', 'reporter']):
            category = 'REPORT_DATE'
        elif any(kw in all_text for kw in ['dossier incomplet', 'document manquant', 'piece manquante']):
            category = 'DOCUMENTS_CMA'
        elif any(kw in all_text for kw in ['identifiant', 'mot de passe', 'connexion', 'connecter']):
            category = 'IDENTIFIANTS'
        elif any(kw in all_text for kw in ['session', 'visio', 'e-learning', 'cours du jour', 'cours du soir']):
            category = 'SESSION_FORMATION'
        elif any(kw in all_text for kw in ['resultat', 'admissib', 'convocation']):
            category = 'RESULTAT_EXAMEN'
        elif any(kw in all_text for kw in ['carte pro', 'carte professionnelle']):
            category = 'CARTE_PRO'
        elif any(kw in all_text for kw in ['paiement', 'paye', 'payer', '20 euro']):
            category = 'PAIEMENT'
        elif any(kw in all_text for kw in ['inscription', 'inscrire', 'comment faire']):
            category = 'INSCRIPTION'

        t['category'] = category
        t['customer_summary'] = summary
        t['thread_count'] = len(thread_list)
        t['has_annulation_keywords'] = has_annul
        t['has_contestation_keywords'] = has_contest

        print(f'  [{i+1:2d}/{len(active_tickets)}] #{num} -> {category:25s} | {summary[:80]}')

    except Exception as e:
        t['category'] = 'ERROR'
        t['customer_summary'] = f'Error: {str(e)[:100]}'
        t['thread_count'] = 0
        print(f'  [{i+1:2d}/{len(active_tickets)}] #{num} -> ERROR: {str(e)[:60]}')

for t in tickets:
    if t['status'] in ['Fermé', 'Ferme', 'Closed']:
        t['category'] = 'FERME'
        t['customer_summary'] = ''
        t['thread_count'] = 0

with open('data/contact_tickets_to_move.json', 'w', encoding='utf-8') as f:
    json.dump(tickets, f, ensure_ascii=False, indent=2)

cats = Counter()
for t in tickets:
    c = t.get('category', '?')
    if c != 'FERME':
        cats[c] += 1

print(f'\n=== CATEGORIES ({sum(cats.values())} tickets) ===')
for cat, count in cats.most_common():
    print(f'  {cat:30s} : {count}')

annul = [t for t in tickets if 'ANNUL' in t.get('category', '') or 'CONTEST' in t.get('category', '')]
print(f'\n=== ANNULATION / CONTESTATION ({len(annul)}) ===')
for t in annul:
    print(f'  #{t["ticketNumber"]} | {t["category"]:25s} | {t["subject"][:70]}')
