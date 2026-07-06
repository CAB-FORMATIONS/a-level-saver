#!/usr/bin/env python3
"""
Script de debug rapide pour tester des fonctions isolées sans lancer le workflow complet.

Usage:
    python test_debug_quick.py sessions --exam-date 2026-03-31 --type jour
    python test_debug_quick.py complaint --ticket 198709000449714052
    python test_debug_quick.py template --ticket 198709000449714052
    python test_debug_quick.py deal --ticket 198709000449714052
"""

import argparse
import json
import sys
import os
from datetime import datetime

# Fix Windows encoding
os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Initialiser logging minimal
import logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)


def test_sessions(exam_date: str, session_type: str = None, limit: int = 5):
    """Teste la recherche de sessions pour une date d'examen."""
    from src.zoho_client import ZohoCRMClient
    from src.utils.session_helper import get_sessions_for_exam_date

    print(f"\n{'='*60}")
    print(f"TEST: Recherche sessions avant examen {exam_date}")
    print(f"{'='*60}")

    crm = ZohoCRMClient()

    if session_type:
        print(f"\nType demandé: {session_type}")
        sessions = get_sessions_for_exam_date(crm, exam_date, session_type=session_type, limit=limit)
        print(f"\nSessions {session_type} trouvées: {len(sessions)}")
        for s in sessions:
            print(f"  → {s.get('Name')} | {s.get('Date_d_but')} - {s.get('Date_fin')}")
    else:
        # Tester les deux types
        for stype in ['jour', 'soir']:
            sessions = get_sessions_for_exam_date(crm, exam_date, session_type=stype, limit=limit)
            print(f"\nSessions {stype}: {len(sessions)}")
            for s in sessions[:3]:
                print(f"  → {s.get('Name')} | {s.get('Date_d_but')} - {s.get('Date_fin')}")


def test_complaint(ticket_id: str):
    """Teste la vérification de plainte session pour un ticket."""
    from src.zoho_client import ZohoDeskClient, ZohoCRMClient
    from src.utils.session_helper import verify_session_complaint
    from src.utils.crm_lookup_helper import enrich_deal_lookups

    print(f"\n{'='*60}")
    print(f"TEST: Vérification plainte session - Ticket {ticket_id}")
    print(f"{'='*60}")

    desk = ZohoDeskClient()
    crm = ZohoCRMClient()

    # Récupérer le ticket et le deal
    ticket = desk.get_ticket(ticket_id)
    cf_opportunite = ticket.get('cf', {}).get('cf_opportunite', '')

    if not cf_opportunite:
        print("❌ Pas d'opportunité liée au ticket")
        return

    deal_id = cf_opportunite.split('/')[-1]
    print(f"Deal ID: {deal_id}")

    deal = crm.get_deal(deal_id)
    print(f"Deal: {deal.get('Deal_Name')}")
    print(f"Session CRM: {deal.get('Session1')}")

    # Enrichir les lookups
    enriched = enrich_deal_lookups(crm, deal, {})
    print(f"\nSession enrichie:")
    print(f"  Type: {enriched.get('session_type')}")
    print(f"  Début: {enriched.get('session_date_debut')}")
    print(f"  Fin: {enriched.get('session_date_fin')}")
    print(f"  Nom: {enriched.get('session_name')}")

    # Récupérer le vrai claimed_session depuis le triage
    from src.agents.triage_agent import TriageAgent
    from src.utils.text_utils import get_clean_thread_content

    threads = desk.get_all_threads_with_full_content(ticket_id)
    last_thread_content = ""
    for t in threads:
        if t.get('isDraft'):
            continue
        if t.get('direction') == 'in':
            last_thread_content = get_clean_thread_content(t)
            break

    triage = TriageAgent()
    triage_result = triage.triage_ticket(
        ticket_subject=ticket.get('subject', ''),
        thread_content=last_thread_content,
        deal_data=deal,
        current_department='DOC'
    )
    intent_context = triage_result.get('intent_context', {})
    claimed_session = intent_context.get('claimed_session', {})
    session_preference = intent_context.get('session_preference')

    print(f"\nTriage result:")
    print(f"  is_complaint: {intent_context.get('is_complaint')}")
    print(f"  claimed_session: {claimed_session}")
    print(f"  session_preference: {session_preference}")

    exam_date = enriched.get('date_examen')
    assigned_type = enriched.get('session_type')

    print(f"\nDate examen: {exam_date}")
    print(f"Session actuelle: {assigned_type}")
    if claimed_session:
        claimed_type = claimed_session.get('claimed_type')
        print(f"Claimed type (triage): {claimed_type}")
        print(f"TYPE MATCH: {claimed_type == assigned_type}")
    else:
        print(f"Pas de claimed_session")

    result = verify_session_complaint(
        crm_client=crm,
        claimed_session=claimed_session,
        assigned_session=deal.get('Session1'),
        enriched_lookups=enriched,
        session_preference=session_preference,
        exam_date=exam_date
    )

    print(f"\n{'='*40}")
    print("RÉSULTAT VÉRIFICATION:")
    print(f"{'='*40}")
    print(f"  is_cab_error: {result.get('is_cab_error')}")
    print(f"  error_type: {result.get('error_type')}")
    print(f"  verification_details: {result.get('verification_details')}")

    if result.get('matched_session'):
        ms = result['matched_session']
        print(f"\n  matched_session:")
        print(f"    → {ms.get('Name')} | {ms.get('Date_d_but')} - {ms.get('Date_fin')}")

    if result.get('alternatives'):
        print(f"\n  alternatives ({len(result['alternatives'])}):")
        for alt in result['alternatives'][:5]:
            print(f"    → {alt.get('Name')} | {alt.get('Date_d_but')} - {alt.get('Date_fin')}")

    # Nouvelles variables pour le template
    print(f"\n  has_all_sessions: {result.get('has_all_sessions', False)}")
    if result.get('all_sessions_jour'):
        print(f"  all_sessions_jour ({len(result['all_sessions_jour'])}):")
        for s in result['all_sessions_jour']:
            print(f"    → {s.get('Name')} | {s.get('Date_d_but')} - {s.get('Date_fin')}")
    if result.get('all_sessions_soir'):
        print(f"  all_sessions_soir ({len(result['all_sessions_soir'])}):")
        for s in result['all_sessions_soir']:
            print(f"    → {s.get('Name')} | {s.get('Date_d_but')} - {s.get('Date_fin')}")


def test_deal(ticket_id: str):
    """Affiche les données du deal lié à un ticket."""
    from src.zoho_client import ZohoDeskClient, ZohoCRMClient
    from src.utils.crm_lookup_helper import enrich_deal_lookups

    print(f"\n{'='*60}")
    print(f"TEST: Données deal - Ticket {ticket_id}")
    print(f"{'='*60}")

    desk = ZohoDeskClient()
    crm = ZohoCRMClient()

    ticket = desk.get_ticket(ticket_id)
    print(f"Ticket: {ticket.get('subject', 'N/A')[:50]}")

    cf_opportunite = ticket.get('cf', {}).get('cf_opportunite', '')
    if not cf_opportunite:
        print("❌ Pas d'opportunité liée")
        return

    deal_id = cf_opportunite.split('/')[-1]
    deal = crm.get_deal(deal_id)

    print(f"\nDeal: {deal.get('Deal_Name')}")
    print(f"Stage: {deal.get('Stage')}")
    print(f"Email: {deal.get('Email')}")
    print(f"Evalbox: {deal.get('Evalbox')}")
    print(f"Date_examen_VTC: {deal.get('Date_examen_VTC')}")
    print(f"Session1: {deal.get('Session1')}")
    print(f"Type_de_session: {deal.get('Type_de_session')}")

    # Enrichir
    enriched = enrich_deal_lookups(crm, deal, {})
    print(f"\n--- Enriched ---")
    print(f"date_examen: {enriched.get('date_examen')}")
    print(f"session_type: {enriched.get('session_type')}")
    print(f"session_date_debut: {enriched.get('session_date_debut')}")
    print(f"session_date_fin: {enriched.get('session_date_fin')}")


def test_template(ticket_id: str):
    """Teste le contexte injecté dans le template pour un ticket."""
    from src.zoho_client import ZohoDeskClient, ZohoCRMClient
    from src.utils.crm_lookup_helper import enrich_deal_lookups
    from src.utils.session_helper import verify_session_complaint, get_sessions_for_exam_date

    print(f"\n{'='*60}")
    print(f"TEST: Contexte template - Ticket {ticket_id}")
    print(f"{'='*60}")

    desk = ZohoDeskClient()
    crm = ZohoCRMClient()

    # Récupérer données
    ticket = desk.get_ticket(ticket_id)
    cf_opportunite = ticket.get('cf', {}).get('cf_opportunite', '')

    if not cf_opportunite:
        print("❌ Pas d'opportunité liée")
        return

    deal_id = cf_opportunite.split('/')[-1]
    deal = crm.get_deal(deal_id)
    enriched = enrich_deal_lookups(crm, deal, {})

    exam_date = enriched.get('date_examen')
    current_type = enriched.get('session_type')

    print(f"Deal: {deal.get('Deal_Name')}")
    print(f"Session actuelle: {current_type} ({enriched.get('session_name')})")
    print(f"Date examen: {exam_date}")

    # Simuler ce qui devrait être injecté pour DEMANDE_CHANGEMENT_SESSION
    print(f"\n--- Variables pour le template ---")

    # 1. Sessions disponibles
    print(f"\nSessions disponibles avant {exam_date}:")
    for stype in ['jour', 'soir']:
        sessions = get_sessions_for_exam_date(crm, exam_date, session_type=stype, limit=3)
        print(f"  {stype}: {len(sessions)} session(s)")
        for s in sessions[:2]:
            print(f"    → {s.get('Name')}")

    # 2. Vérification plainte
    claimed = {'claimed_type': 'jour', 'claimed_dates': None}
    result = verify_session_complaint(
        crm_client=crm,
        claimed_session=claimed,
        assigned_session=deal.get('Session1'),
        enriched_lookups=enriched,
        session_preference='jour',
        exam_date=exam_date
    )

    print(f"\nPour le template:")
    print(f"  is_complaint: True")
    print(f"  is_cab_error: {result.get('is_cab_error')}")
    print(f"  corrected_session: {result.get('matched_session', {}).get('Name') if result.get('matched_session') else 'None'}")
    print(f"  has_complaint_alternatives: {len(result.get('alternatives', [])) > 0}")
    print(f"  alternatives count: {len(result.get('alternatives', []))}")


def test_triage(ticket_id: str):
    """Teste le triage et la détection de plainte pour un ticket."""
    from src.zoho_client import ZohoDeskClient, ZohoCRMClient
    from src.agents.triage_agent import TriageAgent
    from src.utils.text_utils import get_clean_thread_content

    print(f"\n{'='*60}")
    print(f"TEST: Triage - Ticket {ticket_id}")
    print(f"{'='*60}")

    desk = ZohoDeskClient()
    crm = ZohoCRMClient()

    # Récupérer le ticket et les threads
    ticket = desk.get_ticket(ticket_id)
    threads = desk.get_all_threads_with_full_content(ticket_id)

    print(f"Subject: {ticket.get('subject', 'N/A')[:60]}")
    print(f"Threads: {len(threads)}")

    # Analyser chaque thread
    print("\n--- Analyse des threads ---")
    for i, t in enumerate(threads):
        direction = t.get('direction', 'N/A')
        is_draft = t.get('isDraft', False)
        from_email = t.get('fromEmailAddress', 'N/A')
        clean_content = get_clean_thread_content(t)
        print(f"Thread {i}: direction={direction}, draft={is_draft}, from={from_email}")
        print(f"  Clean content (100 chars): {clean_content[:100]}...")

    # Extraire le dernier message entrant (comme le workflow)
    last_thread_content = ""
    for t in threads:
        if t.get('isDraft'):
            continue
        if t.get('direction') == 'in':
            last_thread_content = get_clean_thread_content(t)
            break

    print(f"\nlast_thread_content (200 chars): {last_thread_content[:200]}...")

    # Récupérer le deal
    cf_opportunite = ticket.get('cf', {}).get('cf_opportunite', '')
    deal_id = cf_opportunite.split('/')[-1] if cf_opportunite else None
    deal_data = crm.get_deal(deal_id) if deal_id else {}

    if not last_thread_content:
        print("\n⚠️ ATTENTION: last_thread_content est VIDE !")
        print("Le triage ne fonctionnera pas correctement.")
        return

    # Triage avec la bonne méthode
    triage = TriageAgent()
    result = triage.triage_ticket(
        ticket_subject=ticket.get('subject', ''),
        thread_content=last_thread_content,
        deal_data=deal_data,
        current_department='DOC'
    )

    print(f"\n--- Résultat Triage ---")
    print(f"Action: {result.get('action')}")
    print(f"Primary intent: {result.get('primary_intent')}")
    print(f"Session preference: {result.get('intent_context', {}).get('session_preference')}")
    print(f"Is complaint: {result.get('intent_context', {}).get('is_complaint')}")
    print(f"Claimed session: {result.get('intent_context', {}).get('claimed_session')}")
    print(f"Wrong session: {result.get('intent_context', {}).get('assigned_session_wrong')}")


def main():
    parser = argparse.ArgumentParser(description='Debug rapide des fonctions isolées')
    subparsers = parser.add_subparsers(dest='command', help='Commande à exécuter')

    # Sous-commande: sessions
    p_sessions = subparsers.add_parser('sessions', help='Tester recherche de sessions')
    p_sessions.add_argument('--exam-date', required=True, help='Date examen (YYYY-MM-DD)')
    p_sessions.add_argument('--type', choices=['jour', 'soir'], help='Type de session')
    p_sessions.add_argument('--limit', type=int, default=5, help='Nombre max de sessions')

    # Sous-commande: complaint
    p_complaint = subparsers.add_parser('complaint', help='Tester vérification plainte')
    p_complaint.add_argument('--ticket', required=True, help='ID du ticket')

    # Sous-commande: deal
    p_deal = subparsers.add_parser('deal', help='Afficher données deal')
    p_deal.add_argument('--ticket', required=True, help='ID du ticket')

    # Sous-commande: template
    p_template = subparsers.add_parser('template', help='Tester contexte template')
    p_template.add_argument('--ticket', required=True, help='ID du ticket')

    # Sous-commande: triage
    p_triage = subparsers.add_parser('triage', help='Tester détection triage/plainte')
    p_triage.add_argument('--ticket', required=True, help='ID du ticket')

    args = parser.parse_args()

    if args.command == 'sessions':
        test_sessions(args.exam_date, args.type, args.limit)
    elif args.command == 'complaint':
        test_complaint(args.ticket)
    elif args.command == 'deal':
        test_deal(args.ticket)
    elif args.command == 'template':
        test_template(args.ticket)
    elif args.command == 'triage':
        test_triage(args.ticket)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
