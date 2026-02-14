#!/usr/bin/env python3
"""
Exécution continue du workflow DOC - traite tous les tickets puis boucle sur les nouveaux.

Usage:
    python run_workflow_continuous.py

Le script:
1. Traite tous les tickets dans doc_tickets_pending.json
2. Re-synchronise avec Zoho Desk pour détecter les nouveaux tickets
3. Traite les nouveaux tickets
4. Répète jusqu'à ce qu'il n'y ait plus de nouveaux tickets (ou max 3 cycles)
"""

import json
import os
import sys
import time
from datetime import datetime

# Fix Windows encoding
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.zoho_client import ZohoDeskClient
from src.workflows.doc_ticket_workflow import DOCTicketWorkflow

PENDING_FILE = "doc_tickets_pending.json"
PROCESSED_FILE = "doc_tickets_processed.json"
RESULTS_DIR = "data"
DOC_DEPT_ID = "198709000025523146"

def log(msg):
    """Print avec timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")
    sys.stdout.flush()

def sync_pending_from_zoho():
    """Synchronise doc_tickets_pending.json avec Zoho Desk.

    Critères de sélection :
    - Ticket OUVERT dans département DOC
    - BROUILLON AUTO = non (pas encore traité ou client a répondu)

    Note: On ne vérifie plus processed_ids car le champ BROUILLON AUTO
    est la source de vérité. Si un client répond, le ticket est réouvert
    et BROUILLON AUTO reste décoché → sera re-traité.
    """
    log("Synchronisation avec Zoho Desk...")

    client = ZohoDeskClient()
    all_doc_tickets = []
    from_index = 0

    while True:
        result = client.list_tickets(status='Open', limit=100, from_index=from_index)
        tickets = result.get('data', [])
        if not tickets:
            break
        doc_tickets = [t for t in tickets if t.get('departmentId') == DOC_DEPT_ID]
        all_doc_tickets.extend(doc_tickets)
        from_index += 100
        if len(tickets) < 100 or from_index > 2500:
            break

    # Filtrer: uniquement les tickets où BROUILLON AUTO n'est PAS coché
    pending_tickets = []
    for t in all_doc_tickets:
        # cf = custom fields, cf_brouillon_auto = true/false ou absent
        cf = t.get('cf', {})
        brouillon_auto = cf.get('cf_brouillon_auto', False)

        # Si BROUILLON AUTO est coché (true), on skip
        if brouillon_auto:
            continue

        pending_tickets.append({
            'id': t.get('id'),
            'ticketNumber': t.get('ticketNumber'),
            'subject': t.get('subject'),
            'email': t.get('email'),
            'createdTime': t.get('createdTime'),
            'status': t.get('status'),
        })

    # Sauvegarder
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(pending_tickets, f, ensure_ascii=False, indent=2)

    log(f"Synchronisation terminée: {len(pending_tickets)} tickets en attente")
    return len(pending_tickets)

def load_pending():
    if not os.path.exists(PENDING_FILE):
        return []
    with open(PENDING_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_pending(tickets):
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(tickets, f, ensure_ascii=False, indent=2)

def load_processed():
    if not os.path.exists(PROCESSED_FILE):
        return []
    with open(PROCESSED_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_processed_ticket(ticket_info, result):
    processed = load_processed()

    crm_updates = result.get('response_result', {}).get('crm_updates', {})
    analysis = result.get('analysis_result', {})
    response_result = result.get('response_result', {})
    state_engine = response_result.get('state_engine', {})
    ctx = state_engine.get('context', {})

    processed.append({
        **ticket_info,
        'processed_at': datetime.now().isoformat(),
        'deal_id': analysis.get('deal_id'),
        'success': result.get('success', False),
        'workflow_stage': result.get('workflow_stage'),
        'triage_action': result.get('triage_result', {}).get('action'),
        'primary_intent': analysis.get('primary_intent'),
        'state_id': state_engine.get('state_id'),
        'draft_created': result.get('draft_created', False),
        'crm_updated': result.get('crm_updated', False),
        'crm_updates': crm_updates if crm_updates else None,
        'error': result.get('error'),
    })

    with open(PROCESSED_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed, f, ensure_ascii=False, indent=2)

def save_batch_results(results, cycle_num):
    """Sauvegarde les résultats du batch."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{RESULTS_DIR}/batch_results_{timestamp}_cycle{cycle_num}.json"

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    log(f"Résultats sauvegardés: {filename}")
    return filename

def process_all_pending(workflow, cycle_num, delay_seconds=3.0):
    """Traite tous les tickets pending."""
    pending = load_pending()

    if not pending:
        log("Aucun ticket en attente.")
        return 0, 0

    log(f"Cycle {cycle_num}: Traitement de {len(pending)} tickets...")

    results = []
    success_count = 0
    error_count = 0

    for i, ticket_info in enumerate(pending, 1):
        ticket_id = ticket_info['id']
        subject = (ticket_info.get('subject') or '')[:50]

        log(f"[{i}/{len(pending)}] Ticket {ticket_id}: {subject}")

        try:
            result = workflow.process_ticket(
                ticket_id=ticket_id,
                auto_create_draft=True,
                auto_update_crm=True,
                auto_update_ticket=True,
                auto_send=True
            )

            success = result.get('success', False)
            stage = result.get('workflow_stage', 'UNKNOWN')
            triage_action = result.get('triage_result', {}).get('action', 'N/A')
            intent = result.get('analysis_result', {}).get('primary_intent', 'N/A')

            if success:
                success_count += 1
                log(f"    [OK] {stage} | {triage_action} | {intent}")
            else:
                error_count += 1
                log(f"    [ERREUR] {result.get('error', 'Unknown')}")

            # Sauvegarder dans processed
            save_processed_ticket(ticket_info, result)

            # Collecter pour batch results
            analysis = result.get('analysis_result', {})
            response = result.get('response_result', {})
            triage = result.get('triage_result', {})
            results.append({
                'ticket_id': ticket_id,
                'success': success,
                'stage': stage,
                'triage_action': triage_action,
                'intent': intent,
                'draft_created': result.get('draft_created', False),
                'crm_updated': result.get('crm_updated', False),
                'error': result.get('error'),
                # Contenu original et réponse pour analyse demande/réponse
                # Fallback sur triage_result pour les tickets ROUTE/SPAM (analyse non faite)
                'ticket_subject': analysis.get('ticket_subject', '') or triage.get('ticket_subject', ''),
                'customer_message': analysis.get('customer_message', '') or triage.get('customer_message', ''),
                'draft_content': response.get('final_response', '') or response.get('raw_response', ''),
            })

            # Retirer de pending
            current_pending = load_pending()
            current_pending = [t for t in current_pending if t['id'] != ticket_id]
            save_pending(current_pending)

        except Exception as e:
            error_count += 1
            log(f"    [EXCEPTION] {str(e)}")
            results.append({
                'ticket_id': ticket_id,
                'success': False,
                'error': str(e),
            })

        # Pause entre tickets
        time.sleep(delay_seconds)

    # Sauvegarder les résultats du cycle
    save_batch_results(results, cycle_num)

    log(f"Cycle {cycle_num} terminé: {success_count} OK, {error_count} erreurs")
    return success_count, error_count

def main():
    log("="*60)
    log("WORKFLOW CONTINU - Démarrage (mode infini)")
    log("="*60)
    log("Pour arrêter: Ctrl+C ou 'Stop-Process -Name python' dans PowerShell")

    # Initialiser le workflow une seule fois
    workflow = DOCTicketWorkflow()

    total_success = 0
    total_errors = 0
    cycle = 0
    wait_time_no_tickets = 300  # 5 minutes d'attente si pas de nouveaux tickets

    try:
        while True:
            cycle += 1
            log(f"\n{'='*60}")
            log(f"CYCLE {cycle}")
            log(f"{'='*60}")

            # Traiter les tickets pending
            success, errors = process_all_pending(workflow, cycle, delay_seconds=3.0)
            total_success += success
            total_errors += errors

            # Re-synchroniser avec Zoho pour détecter les nouveaux tickets
            log("\nRecherche de nouveaux tickets...")
            new_count = sync_pending_from_zoho()

            if new_count == 0:
                log(f"Aucun nouveau ticket. Pause de {wait_time_no_tickets//60} minutes...")
                time.sleep(wait_time_no_tickets)
            else:
                log(f"{new_count} nouveaux tickets détectés. Continuation...")
                time.sleep(5)  # Petite pause avant le prochain cycle

    except KeyboardInterrupt:
        log("\n\nArrêt demandé par l'utilisateur (Ctrl+C)")

    log(f"\n{'='*60}")
    log("WORKFLOW CONTINU - Terminé")
    log(f"{'='*60}")
    log(f"Total traités avec succès: {total_success}")
    log(f"Total erreurs: {total_errors}")
    log(f"Cycles effectués: {cycle}")

if __name__ == "__main__":
    main()
