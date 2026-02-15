#!/usr/bin/env python3
"""
Exécution du workflow DOC en batch sur les tickets en attente.

Usage:
    python run_workflow_batch.py --count 10              # Traiter 10 tickets
    python run_workflow_batch.py --count 10 --dry-run    # Dry run (pas de draft/CRM)
    python run_workflow_batch.py --ticket 198709000449714052  # Un ticket spécifique
    python run_workflow_batch.py --status                # Voir le statut de la file

Le script:
- Lit les tickets depuis doc_tickets_pending.json
- Exécute le workflow complet sur chaque ticket
- Retire les tickets traités de la liste
- Sauvegarde les résultats dans data/batch_results_<timestamp>.json
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import List, Dict, Any, Optional

# Fix Windows encoding
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.workflows.doc_ticket_workflow import DOCTicketWorkflow
from run_workflow_continuous import sync_pending_from_zoho

# Fichiers
PENDING_FILE = "doc_tickets_pending.json"
PROCESSED_FILE = "doc_tickets_processed.json"
RESULTS_DIR = "data"


def load_pending_tickets() -> List[Dict]:
    """Charge la liste des tickets en attente."""
    if not os.path.exists(PENDING_FILE):
        print(f"Fichier {PENDING_FILE} non trouvé. Lancez d'abord l'extraction.")
        return []

    with open(PENDING_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_pending_tickets(tickets: List[Dict]):
    """Sauvegarde la liste des tickets en attente."""
    with open(PENDING_FILE, 'w', encoding='utf-8') as f:
        json.dump(tickets, f, ensure_ascii=False, indent=2)


def load_processed_tickets() -> List[Dict]:
    """Charge l'historique des tickets traités."""
    if not os.path.exists(PROCESSED_FILE):
        return []

    with open(PROCESSED_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_processed_ticket(ticket_info: Dict, result: Dict):
    """Ajoute un ticket à l'historique des traités."""
    processed = load_processed_tickets()

    # Extraire les updates CRM
    crm_updates = result.get('response_result', {}).get('crm_updates', {})

    triage = result.get('triage_result', {})

    processed.append({
        **ticket_info,
        'processed_at': datetime.now().isoformat(),
        'deal_id': result.get('analysis_result', {}).get('deal_id'),
        'success': result.get('success', False),
        'workflow_stage': result.get('workflow_stage'),
        'triage_action': triage.get('action'),
        'target_department': triage.get('target_department'),
        'primary_intent': result.get('analysis_result', {}).get('primary_intent'),
        'draft_created': result.get('draft_created', False),
        'reply_sent': result.get('reply_sent', False),
        'delivery_method': result.get('delivery_method', 'none'),
        'send_fallback_reason': result.get('send_fallback_reason'),
        'crm_updated': result.get('crm_updated', False),
        'crm_updates': crm_updates if crm_updates else None
    })

    with open(PROCESSED_FILE, 'w', encoding='utf-8') as f:
        json.dump(processed, f, ensure_ascii=False, indent=2)


def remove_from_pending(ticket_id: str, pending: List[Dict]) -> List[Dict]:
    """Retire un ticket de la liste en attente."""
    return [t for t in pending if t['id'] != ticket_id]


def show_status():
    """Affiche le statut de la file d'attente."""
    pending = load_pending_tickets()
    processed = load_processed_tickets()

    print(f"\n{'='*60}")
    print("STATUT DE LA FILE D'ATTENTE")
    print(f"{'='*60}")
    print(f"  Tickets en attente: {len(pending)}")
    print(f"  Tickets traités:    {len(processed)}")

    if pending:
        print(f"\n  5 prochains tickets:")
        for t in pending[:5]:
            print(f"    {t['id']} | {t.get('createdTime', '')[:10]} | {t.get('subject', '')[:40]}")

    if processed:
        # Stats des derniers traités
        success_count = sum(1 for p in processed if p.get('success'))
        print(f"\n  Derniers résultats: {success_count}/{len(processed)} succès")

        # Derniers 3 traités
        print(f"\n  3 derniers traités:")
        for p in processed[-3:]:
            status = "OK" if p.get('success') else "KO"
            print(f"    [{status}] {p['id']} | {p.get('triage_action', 'N/A')} | {p.get('primary_intent', 'N/A')[:30]}")


def process_batch(
    count: int = 10,
    dry_run: bool = False,
    delay_seconds: float = 2.0,
    specific_ticket: Optional[str] = None,
    auto_send: bool = False
):
    """
    Traite un lot de tickets.

    Args:
        count: Nombre de tickets à traiter
        dry_run: Si True, ne crée pas de draft/CRM updates
        delay_seconds: Pause entre chaque ticket (rate limit)
        specific_ticket: ID d'un ticket spécifique à traiter
        auto_send: Si True, envoie directement la réponse (avec guard rails)
    """
    pending = load_pending_tickets()

    if not pending and not specific_ticket:
        print("Aucun ticket en attente.")
        return

    # Si ticket spécifique
    if specific_ticket:
        tickets_to_process = [{'id': specific_ticket, 'subject': 'Ticket spécifique'}]
        # Vérifier s'il est dans la liste pending
        in_pending = any(t['id'] == specific_ticket for t in pending)
    else:
        tickets_to_process = pending[:count]
        in_pending = True

    print(f"\n{'='*60}")
    print(f"TRAITEMENT BATCH - {len(tickets_to_process)} ticket(s)")
    print(f"{'='*60}")
    mode_label = 'DRY RUN' if dry_run else ('AUTO-SEND' if auto_send else 'PRODUCTION (draft)')
    print(f"  Mode: {mode_label}")
    print(f"  Délai entre tickets: {delay_seconds}s")
    print(f"  Tickets restants après: {len(pending) - len(tickets_to_process)}")
    print(f"{'='*60}\n")

    # Initialiser le workflow une seule fois
    workflow = DOCTicketWorkflow()

    results = []
    success_count = 0
    error_count = 0
    sent_count = 0
    draft_count = 0
    fallback_count = 0

    for i, ticket_info in enumerate(tickets_to_process, 1):
        ticket_id = ticket_info['id']
        subject = ticket_info.get('subject', '')[:50]

        print(f"\n[{i}/{len(tickets_to_process)}] Ticket {ticket_id}")
        print(f"    Sujet: {subject}")

        try:
            # Exécuter le workflow
            result = workflow.process_ticket(
                ticket_id=ticket_id,
                auto_create_draft=not dry_run and not auto_send,
                auto_update_crm=not dry_run,
                auto_update_ticket=not dry_run,
                auto_send=auto_send and not dry_run
            )

            success = result.get('success', False)
            stage = result.get('workflow_stage', 'UNKNOWN')
            triage_action = result.get('triage_result', {}).get('action', 'N/A')
            intent = result.get('analysis_result', {}).get('primary_intent', 'N/A')

            # Track delivery method
            delivery = result.get('delivery_method', 'none')
            if delivery == 'sent':
                sent_count += 1
            elif delivery == 'draft':
                draft_count += 1
                if result.get('send_fallback_reason'):
                    fallback_count += 1

            if success:
                success_count += 1
                delivery_icon = '📨' if delivery == 'sent' else ('📝' if delivery == 'draft' else '')
                print(f"    [OK] Stage: {stage} | Action: {triage_action} | Intent: {intent} {delivery_icon}")
            else:
                error_count += 1
                error_msg = result.get('error', 'Unknown error')
                print(f"    [ERREUR] {error_msg}")

            # Extraire les infos CRM
            crm_updated = result.get('crm_updated', False)
            crm_updates = result.get('response_result', {}).get('crm_updates', {})
            deal_id = result.get('analysis_result', {}).get('deal_id')

            # Extraire les données de triage
            triage = result.get('triage_result', {})
            detected_intent = triage.get('detected_intent')
            secondary_intents = triage.get('secondary_intents', [])
            intent_context = triage.get('intent_context', {})

            # Extraire les données d'entrée (input)
            analysis = result.get('analysis_result', {})
            deal_data = analysis.get('deal_data', {})
            examt3p_data = analysis.get('examt3p_data', {})
            enriched_lookups = analysis.get('enriched_lookups', {})

            # Données CRM input simplifiées
            crm_input = {
                'deal_name': deal_data.get('Deal_Name'),
                'stage': deal_data.get('Stage'),
                'evalbox': deal_data.get('Evalbox'),
                'date_examen_vtc': deal_data.get('Date_examen_VTC'),
                'session1': deal_data.get('Session1'),
                'email': deal_data.get('Email'),
            } if deal_data else None

            # Données ExamT3P input simplifiées
            examt3p_input = {
                'statut_dossier': examt3p_data.get('statut_dossier'),
                'num_dossier': examt3p_data.get('num_dossier'),
                'documents_count': len(examt3p_data.get('documents', [])) if isinstance(examt3p_data.get('documents'), list) else 0,
                'examens': examt3p_data.get('examens', [])[:3] if isinstance(examt3p_data.get('examens'), list) else [],
                'credentials_valid': examt3p_data.get('connection_test_success', False),
            } if examt3p_data else None

            # Enriched lookups
            lookups_input = {
                'date_examen': enriched_lookups.get('date_examen'),
                'session_type': enriched_lookups.get('session_type'),
                'session_date_debut': enriched_lookups.get('session_date_debut'),
                'session_date_fin': enriched_lookups.get('session_date_fin'),
            } if enriched_lookups else None

            # Draft content et variables template
            response_result = result.get('response_result', {})
            draft_content = response_result.get('final_response', '') or response_result.get('response_text', '')
            state_engine = response_result.get('state_engine', {})
            ctx = state_engine.get('context', {})

            # Variables template importantes (depuis state_engine.context)
            template_vars = {
                'state_id': state_engine.get('state_id'),
                'state_name': state_engine.get('state_name'),
                'primary_intent': response_result.get('primary_intent'),
                'secondary_intents': response_result.get('secondary_intents', []),
                'intents_handled': response_result.get('intents_handled', []),
                'date_case': ctx.get('date_case'),
                'uber_case': ctx.get('uber_case'),
                'session_preference': ctx.get('session_preference'),
                'is_complaint': ctx.get('is_complaint'),
                'is_cab_error': ctx.get('is_cab_error'),
                'can_modify_exam_date': ctx.get('can_modify_exam_date'),
                'has_sessions_proposees': ctx.get('has_sessions_proposees'),
                'report_possible': ctx.get('report_possible'),
                'report_bloque': ctx.get('report_bloque'),
                'evalbox': ctx.get('evalbox'),
            } if ctx or state_engine else None

            # Humanizer & validation metadata
            humanizer_meta = response_result.get('humanizer', {})
            validation = response_result.get('validation', {})
            first_val = next(iter(validation.values()), {}) if validation else {}

            # Sauvegarder le résultat complet
            results.append({
                'ticket_id': ticket_id,
                'deal_id': deal_id,
                'success': success,
                'stage': stage,
                'triage_action': triage_action,
                'target_department': triage.get('target_department'),
                'draft_created': result.get('draft_created', False),
                'reply_sent': result.get('reply_sent', False),
                'delivery_method': result.get('delivery_method', 'none'),
                'send_fallback_reason': result.get('send_fallback_reason'),
                # Triage data
                'triage': {
                    'detected_intent': detected_intent,
                    'secondary_intents': secondary_intents,
                    'intent_context': intent_context if intent_context else None,
                    'incoming_thread_count': triage.get('incoming_thread_count'),
                },
                # Input data
                'input': {
                    'crm': crm_input,
                    'examt3p': examt3p_input,
                    'lookups': lookups_input,
                },
                # Template variables
                'template_vars': template_vars,
                # Quality — humanizer
                'humanizer': {
                    'was_humanized': response_result.get('was_humanized', False),
                    'failed': humanizer_meta.get('validation_failed', False) or bool(humanizer_meta.get('error')),
                    'issues': humanizer_meta.get('validation_issues', []),
                    'error': humanizer_meta.get('error'),
                    'attempts': humanizer_meta.get('attempts', 0),
                },
                # Quality — validation
                'validation': {
                    'compliant': first_val.get('compliant'),
                    'errors': first_val.get('errors', []),
                    'warnings': first_val.get('warnings', []),
                },
                # Health check data
                'valid_dates': response_result.get('valid_dates', []),
                'template_response': (response_result.get('template_response') or '')[:3000],
                'template_used': state_engine.get('template_used') if state_engine else None,
                # Output data
                'output': {
                    'crm_updated': crm_updated,
                    'crm_updates': crm_updates if crm_updates else None,
                    'crm_updates_blocked': state_engine.get('crm_updates_blocked') if state_engine else None,
                    'draft_content': draft_content[:3000] if draft_content else None,
                },
                'error': result.get('error')
            })

            # Retirer de la liste pending et sauvegarder dans processed
            if in_pending and not dry_run:
                pending = remove_from_pending(ticket_id, pending)
                save_pending_tickets(pending)
                save_processed_ticket(ticket_info, result)

        except Exception as e:
            error_count += 1
            print(f"    [EXCEPTION] {str(e)[:100]}")
            results.append({
                'ticket_id': ticket_id,
                'success': False,
                'error': str(e)
            })

        # Pause pour rate limit (sauf dernier ticket)
        if i < len(tickets_to_process):
            print(f"    Pause {delay_seconds}s...")
            time.sleep(delay_seconds)

    # Résumé final
    print(f"\n{'='*60}")
    print("RÉSUMÉ")
    print(f"{'='*60}")
    print(f"  Traités: {len(tickets_to_process)}")
    print(f"  Succès:  {success_count}")
    print(f"  Erreurs: {error_count}")
    if sent_count or draft_count:
        print(f"  Envoyés: {sent_count}")
        print(f"  Drafts:  {draft_count}")
        if fallback_count:
            print(f"  Fallbacks: {fallback_count}")

    # Routes par département
    routes = {}
    for r in results:
        if r.get('triage_action') == 'ROUTE':
            dept = r.get('target_department', '?')
            routes[dept] = routes.get(dept, 0) + 1
    if routes:
        print(f"  Routes:")
        for dept, n in sorted(routes.items(), key=lambda x: -x[1]):
            print(f"    {dept}: {n}")

    print(f"  Restants: {len(pending)}")

    # Sauvegarder les résultats du batch
    if not os.path.exists(RESULTS_DIR):
        os.makedirs(RESULTS_DIR)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_file = os.path.join(RESULTS_DIR, f"batch_results_{timestamp}.json")

    with open(results_file, 'w', encoding='utf-8') as f:
        json.dump({
            'timestamp': timestamp,
            'count': len(tickets_to_process),
            'success': success_count,
            'errors': error_count,
            'dry_run': dry_run,
            'results': results
        }, f, ensure_ascii=False, indent=2)

    print(f"\n  Résultats sauvegardés: {results_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Exécution du workflow DOC en batch',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  python run_workflow_batch.py --status              # Voir le statut
  python run_workflow_batch.py --count 5 --dry-run   # Test sur 5 tickets
  python run_workflow_batch.py --count 10            # Traiter 10 tickets
  python run_workflow_batch.py --ticket 198709...    # Un ticket spécifique
        """
    )

    parser.add_argument('--count', '-n', type=int, default=10,
                        help='Nombre de tickets à traiter (défaut: 10)')
    parser.add_argument('--dry-run', '-d', action='store_true',
                        help='Mode test: pas de draft/CRM updates')
    parser.add_argument('--delay', type=float, default=2.0,
                        help='Délai entre tickets en secondes (défaut: 2.0)')
    parser.add_argument('--ticket', '-t', type=str,
                        help='Traiter un ticket spécifique')
    parser.add_argument('--auto-send', action='store_true',
                        help='Envoyer directement les réponses (avec guard rails, fallback draft)')
    parser.add_argument('--status', '-s', action='store_true',
                        help='Afficher le statut de la file')

    args = parser.parse_args()

    if args.status:
        show_status()
    elif args.ticket:
        process_batch(
            count=1,
            dry_run=args.dry_run,
            delay_seconds=args.delay,
            specific_ticket=args.ticket,
            auto_send=args.auto_send
        )
    else:
        # Toujours resynchroniser avant un batch pour avoir des tickets frais
        print("\n🔄 Resynchronisation avec Zoho Desk...")
        sync_pending_from_zoho()
        print()
        process_batch(
            count=args.count,
            dry_run=args.dry_run,
            delay_seconds=args.delay,
            auto_send=args.auto_send
        )


if __name__ == '__main__':
    main()
