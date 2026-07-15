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
from batch_health_check import run_health_check

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
    - Pas de brouillon existant (vérifié via API threads)

    Note: L'API list ne retourne pas les custom fields (cf_brouillon_auto),
    donc on utilise has_existing_draft() comme source de vérité.
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

    log(f"  {len(all_doc_tickets)} tickets DOC ouverts trouvés, vérification des brouillons...")

    # Filtrer: uniquement les tickets SANS brouillon existant
    pending_tickets = []
    skipped_draft = 0
    for i, t in enumerate(all_doc_tickets):
        tid = str(t.get('id', ''))
        try:
            if client.has_existing_draft(tid):
                skipped_draft += 1
                continue
        except Exception:
            pass  # En cas d'erreur, inclure le ticket (le workflow re-vérifiera)

        pending_tickets.append({
            'id': t.get('id'),
            'ticketNumber': t.get('ticketNumber'),
            'subject': t.get('subject'),
            'email': t.get('email'),
            'createdTime': t.get('createdTime'),
            'status': t.get('status'),
        })

        if (i + 1) % 100 == 0:
            log(f"  Vérifié {i + 1}/{len(all_doc_tickets)}... ({len(pending_tickets)} sans draft)")

    # Sauvegarder
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(pending_tickets, f, ensure_ascii=False, indent=2)

    log(f"Synchronisation terminée: {len(pending_tickets)} tickets en attente ({skipped_draft} avec brouillon existant)")
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

    triage = result.get('triage_result', {})
    validation = response_result.get('validation', {})
    first_validation = next(iter(validation.values()), {}) if validation else {}

    processed.append({
        **ticket_info,
        'processed_at': datetime.now().isoformat(),
        'deal_id': analysis.get('deal_id'),
        'success': result.get('success', False),
        'workflow_stage': result.get('workflow_stage'),
        # Triage
        'triage_action': triage.get('action'),
        'target_department': triage.get('target_department'),
        'incoming_thread_count': triage.get('incoming_thread_count'),
        'primary_intent': analysis.get('primary_intent'),
        'secondary_intents': triage.get('secondary_intents', []),
        # State engine
        'state_id': state_engine.get('state_id'),
        'state_name': state_engine.get('state_name'),
        'template_used': state_engine.get('template_used'),
        'evalbox': ctx.get('evalbox'),
        'date_case': ctx.get('date_case'),
        'dossier_termine': ctx.get('dossier_termine', False),
        'resultat_category': ctx.get('resultat_category'),
        # Delivery
        'draft_created': result.get('draft_created', False),
        'reply_sent': result.get('reply_sent', False),
        'delivery_method': result.get('delivery_method', 'none'),
        'send_fallback_reason': result.get('send_fallback_reason'),
        # Quality
        'was_humanized': response_result.get('was_humanized', False),
        'validation_compliant': first_validation.get('compliant'),
        # CRM
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
                auto_send=False
            )

            success = result.get('success', False)
            stage = result.get('workflow_stage', 'UNKNOWN')
            triage_action = result.get('triage_result', {}).get('action', 'N/A')
            intent = result.get('analysis_result', {}).get('primary_intent', 'N/A')
            delivery = result.get('delivery_method', 'none')

            if success:
                success_count += 1
                # Log enrichi selon le type d'action
                if result.get('reply_sent'):
                    tag = 'SENT'
                elif result.get('draft_created'):
                    tag = 'DRAFT'
                elif triage_action == 'ROUTE':
                    target = result.get('triage_result', {}).get('target_department', '?')
                    tag = f'ROUTE→{target}'
                elif stage.startswith('CLOSED_CMA'):
                    tag = 'CMA_CLOSED'
                elif stage.startswith('SKIPPED'):
                    tag = 'SKIP'
                elif stage == 'STOPPED_SPAM':
                    tag = 'SPAM'
                else:
                    tag = stage
                log(f"    [{tag}] {triage_action} | {intent}")
            else:
                error_count += 1
                log(f"    [ERREUR] {result.get('error', 'Unknown')}")

            # Sauvegarder dans processed
            save_processed_ticket(ticket_info, result)

            # Collecter pour batch results
            analysis = result.get('analysis_result', {})
            response = result.get('response_result', {})
            triage = result.get('triage_result', {})
            se = response.get('state_engine', {})
            se_ctx = se.get('context', {})
            validation = response.get('validation', {})
            first_val = next(iter(validation.values()), {}) if validation else {}
            humanizer = response.get('humanizer', {})
            conv_state = analysis.get('conversation_state')

            results.append({
                'ticket_id': ticket_id,
                'success': success,
                'stage': stage,
                # Triage
                'triage_action': triage_action,
                'target_department': triage.get('target_department'),
                'incoming_thread_count': triage.get('incoming_thread_count'),
                'intent': intent,
                'secondary_intents': triage.get('secondary_intents', []),
                # State engine
                'state_id': se.get('state_id'),
                'state_name': se.get('state_name'),
                'template_used': se.get('template_used'),
                'evalbox': se_ctx.get('evalbox'),
                'date_case': se_ctx.get('date_case'),
                'dossier_termine': se_ctx.get('dossier_termine', False),
                'resultat_category': se_ctx.get('resultat_category'),
                # Delivery
                'draft_created': result.get('draft_created', False),
                'reply_sent': result.get('reply_sent', False),
                'delivery_method': result.get('delivery_method', 'none'),
                'send_fallback_reason': result.get('send_fallback_reason'),
                # Quality — humanizer
                'was_humanized': response.get('was_humanized', False),
                'humanizer_failed': humanizer.get('validation_failed', False) or bool(humanizer.get('error')),
                'humanizer_issues': humanizer.get('validation_issues', []),
                # Quality — validation
                'validation_compliant': first_val.get('compliant'),
                'validation_errors': first_val.get('errors', []),
                'validation_warnings': first_val.get('warnings', []),
                # CRM
                'crm_updated': result.get('crm_updated', False),
                'crm_updates': response.get('crm_updates') or None,
                'crm_updates_blocked': se.get('crm_updates_blocked') or None,
                # Health check data
                'valid_dates': response.get('valid_dates', []),
                'template_response': (response.get('template_response') or '')[:3000],
                # V3 conversation
                'conversation_mode': getattr(conv_state, 'conversation_mode', None) if conv_state else None,
                'response_mode': getattr(conv_state, 'response_mode', None) if conv_state else None,
                # Context flags for health check
                'ctx_flags': {
                    'report_bloque': se_ctx.get('report_bloque', False),
                    'can_modify_exam_date': se_ctx.get('can_modify_exam_date', True),
                    'uber_cas_d': se_ctx.get('uber_cas_d', False),
                    'uber_cas_e': se_ctx.get('uber_cas_e', False),
                    'show_dates_section': se_ctx.get('show_dates_section'),
                    'show_sessions_section': se_ctx.get('show_sessions_section'),
                    'credentials_invalid': se_ctx.get('credentials_invalid', False),
                } if se_ctx else None,
                'error': result.get('error'),
                # Contenu pour analyse demande/réponse
                'ticket_subject': analysis.get('ticket_subject', '') or triage.get('ticket_subject', ''),
                'customer_message': analysis.get('customer_message', '') or triage.get('customer_message', ''),
                'draft_content': response.get('response_text', ''),
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
    batch_file = save_batch_results(results, cycle_num)

    # Summary enrichi
    sent = sum(1 for r in results if r.get('reply_sent'))
    drafts = sum(1 for r in results if r.get('draft_created'))
    skipped = sum(1 for r in results if r.get('stage', '').startswith('SKIPPED'))
    errors = sum(1 for r in results if r.get('error'))

    # Routes par département
    routes = {}
    for r in results:
        if r.get('triage_action') == 'ROUTE':
            dept = r.get('target_department', '?')
            routes[dept] = routes.get(dept, 0) + 1

    # CMA auto-closed
    cma_closed = sum(1 for r in results if r.get('stage', '').startswith('CLOSED_CMA'))

    log(f"\n--- Cycle {cycle_num} Summary ---")
    log(f"  Auto-send: {sent} | Drafts: {drafts} | CMA closed: {cma_closed} | Skipped: {skipped} | Errors: {error_count}")
    if routes:
        route_parts = [f"{dept}: {n}" for dept, n in sorted(routes.items(), key=lambda x: -x[1])]
        log(f"  Routes: {' | '.join(route_parts)} (total: {sum(routes.values())})")
    log(f"---")

    # Health check automatique post-cycle
    try:
        report = run_health_check([batch_file])
        summary = report.get('summary', {})
        total_issues = summary.get('total_issues', 0)

        if total_issues == 0:
            log("  Health check: OK (0 issues)")
        else:
            parts = []
            for sev in ['CRITICAL', 'ERROR', 'WARNING', 'INFO']:
                count = summary.get(sev, 0)
                if count > 0:
                    parts.append(f"{sev}: {count}")
            log(f"  Health check: {total_issues} issues ({', '.join(parts)})")

            # Détail des checks avec issues
            for check_id, info in report.get('checks', {}).items():
                if info['severity'] in ('CRITICAL', 'ERROR'):
                    log(f"    [{info['severity']}] {check_id}: {info['count']}x — {info['sample_message'][:80]}")

        # Sauvegarder le rapport health check
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        hc_file = f"{RESULTS_DIR}/health_check_{timestamp}_cycle{cycle_num}.json"
        with open(hc_file, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    except Exception as e:
        log(f"  Health check: ERREUR — {str(e)}")

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
