#!/usr/bin/env python3
"""
Analyse des notes CRM [META] pour identifier les combinaisons STATE:INTENTION
les plus fréquentes et détecter celles qui tombent sur un wildcard (*:INTENTION).

Utilité : Prioriser les entrées à ajouter dans state_intention_matrix.yaml.

Usage:
    python analyze_meta_notes.py [--weeks N] [--top N] [--output FILE]

Exemples:
    python analyze_meta_notes.py                  # 8 dernières semaines, top 20
    python analyze_meta_notes.py --weeks 12       # 12 dernières semaines
    python analyze_meta_notes.py --output data/wildcard_report.json
"""

import json
import sys
import os
import argparse
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def load_matrix(matrix_path: Path) -> Tuple[set, set]:
    """
    Charge la matrice et retourne:
    - explicit_keys: ensemble des clés STATE:INTENTION explicitement définies
    - wildcard_intentions: ensemble des intentions avec wildcard *:INTENTION
    """
    with open(matrix_path, 'r', encoding='utf-8') as f:
        matrix = yaml.safe_load(f)

    explicit_keys = set()
    wildcard_intentions = set()

    for key in matrix.keys():
        if key.startswith('*:'):
            wildcard_intentions.add(key[2:])  # Remove "*:"
        elif ':' in key:
            explicit_keys.add(key)

    return explicit_keys, wildcard_intentions


def classify_match(state: str, intention: str, explicit_keys: set, wildcard_intentions: set) -> str:
    """
    Détermine comment la combinaison STATE:INTENTION a été matchée :
    - 'exact'    : entrée explicite STATE:INTENTION dans la matrice
    - 'wildcard' : match via *:INTENTION
    - 'fallback' : ni exact ni wildcard (fallback vers legacy ou response_master générique)
    """
    exact_key = f"{state}:{intention}"
    if exact_key in explicit_keys:
        return 'exact'
    if intention in wildcard_intentions:
        return 'wildcard'
    return 'fallback'


def parse_meta_line(line: str) -> Optional[Dict]:
    """Parse une ligne [META] en dictionnaire."""
    if not line or '[META]' not in line:
        return None
    try:
        meta_part = line.split('[META]', 1)[1].strip()
        if not meta_part:
            return None
        pairs = {}
        for item in meta_part.split('|'):
            item = item.strip()
            if '=' in item:
                k, _, v = item.partition('=')
                pairs[k.strip()] = v.strip()
        state = pairs.get('state', '')
        intent = pairs.get('intent', '')
        if not state:
            return None
        return {
            'state': state,
            'intent': intent,
            'evalbox': pairs.get('evalbox', ''),
            'date_exam': pairs.get('date_exam', ''),
            'ts': pairs.get('ts', ''),
            'ticket': pairs.get('ticket', ''),
        }
    except Exception:
        return None


def fetch_all_meta_records(crm_client, weeks: int = 8, verbose: bool = False) -> List[Dict]:
    """
    Récupère tous les enregistrements [META] depuis les notes CRM.

    Stratégie: cherche les deals modifiés récemment, puis lit leurs notes.
    """
    # Calcul de la date de début
    since_date = (datetime.now() - timedelta(weeks=weeks)).strftime('%Y-%m-%dT%H:%M:%S+00:00')

    # Zoho CRM ne supporte pas le filtre sur Modified_Time dans /search
    # On utilise /Deals avec le paramètre sort_by=Modified_Time
    # Alternative: on cherche tous les deals avec notes [META] via un range scan
    # En pratique, on récupère les deals récents par lot et on filtre

    print(f"  Recherche deals actifs (pipeline standard)...")

    # Chercher les deals dans les stages actifs
    stages = [
        "Qualification",
        "Needs Analysis",
        "Value Proposition",
        "Id. Decision Makers",
        "Perception Analysis",
        "Proposal/Price Quote",
        "Negotiation/Review",
        "Closed Won",
        "Closed Lost"
    ]

    all_deals = []
    seen_ids = set()

    # Approche 1: deals par stage actif
    for stage in stages[:6]:  # Stages ouverts
        try:
            criteria = f'(Stage:equals:{stage})'
            response = crm_client.search_deals(criteria=criteria, per_page=200)
            deals = response.get('data', [])
            for deal in deals:
                deal_id = deal.get('id')
                if deal_id and deal_id not in seen_ids:
                    seen_ids.add(deal_id)
                    all_deals.append(deal)
        except Exception as e:
            logger.warning(f"Erreur recherche stage {stage}: {e}")

    # Approche 2: deals récemment modifiés (dernières semaines) via Closed Won/Lost
    for stage in stages[6:]:
        try:
            criteria = f'(Stage:equals:{stage})'
            response = crm_client.search_deals(criteria=criteria, per_page=200)
            deals = response.get('data', [])
            for deal in deals:
                deal_id = deal.get('id')
                modified = deal.get('Modified_Time', '')
                # Filtrer par date si disponible
                if deal_id and deal_id not in seen_ids:
                    if modified and modified >= since_date[:10]:
                        seen_ids.add(deal_id)
                        all_deals.append(deal)
        except Exception as e:
            logger.warning(f"Erreur recherche stage {stage}: {e}")

    print(f"  {len(all_deals)} deals trouvés")
    print(f"  Lecture des notes CRM pour chaque deal...")

    meta_records = []
    deals_with_meta = 0
    errors = 0

    for i, deal in enumerate(all_deals):
        deal_id = deal.get('id')
        deal_name = deal.get('Deal_Name', 'N/A')

        if verbose and i % 20 == 0:
            print(f"    Progression: {i}/{len(all_deals)} deals traités, {deals_with_meta} avec [META]")

        try:
            notes_response = crm_client.get_deal_notes(deal_id)
            notes = notes_response.get('data', [])

            deal_meta_count = 0
            for note in notes:
                content = note.get('Note_Content', '')
                if not content or '[META]' not in content:
                    continue
                for line in content.split('\n'):
                    line = line.strip()
                    if line.startswith('[META]'):
                        record = parse_meta_line(line)
                        if record:
                            record['deal_id'] = deal_id
                            record['deal_name'] = deal_name
                            meta_records.append(record)
                            deal_meta_count += 1
                        break  # Only first [META] per note

            if deal_meta_count > 0:
                deals_with_meta += 1

        except Exception as e:
            errors += 1
            logger.warning(f"Erreur lecture notes deal {deal_id}: {e}")

    print(f"  {deals_with_meta} deals avec [META] | {len(meta_records)} enregistrements total | {errors} erreurs")
    return meta_records


def analyze_records(
    records: List[Dict],
    explicit_keys: set,
    wildcard_intentions: set,
    top_n: int = 20
) -> Dict:
    """Analyse les enregistrements [META] et génère le rapport."""

    # Compteurs
    combo_counter = Counter()          # STATE:INTENTION → count
    state_counter = Counter()          # STATE → count
    intent_counter = Counter()         # INTENT → count
    match_type_counter = Counter()     # exact/wildcard/fallback → count
    wildcard_combos = Counter()        # STATE:INTENTION quand wildcard → count
    fallback_combos = Counter()        # STATE:INTENTION quand fallback → count
    evalbox_counter = Counter()        # evalbox → count

    for record in records:
        state = record.get('state', 'UNKNOWN')
        intent = record.get('intent', '')
        evalbox = record.get('evalbox', '')

        state_counter[state] += 1
        if intent:
            intent_counter[intent] += 1
        if evalbox:
            evalbox_counter[evalbox] += 1

        if intent:
            combo = f"{state}:{intent}"
            combo_counter[combo] += 1
            match = classify_match(state, intent, explicit_keys, wildcard_intentions)
            match_type_counter[match] += 1
            if match == 'wildcard':
                wildcard_combos[combo] += 1
            elif match == 'fallback':
                fallback_combos[combo] += 1

    # Construire le rapport
    total = len(records)
    total_with_intent = sum(1 for r in records if r.get('intent'))

    report = {
        'metadata': {
            'generated_at': datetime.now().isoformat(),
            'total_records': total,
            'records_with_intent': total_with_intent,
            'unique_states': len(state_counter),
            'unique_intents': len(intent_counter),
            'unique_combos': len(combo_counter),
        },
        'match_quality_summary': dict(match_type_counter),
        'top_states': dict(state_counter.most_common(top_n)),
        'top_intents': dict(intent_counter.most_common(top_n)),
        'top_combos': dict(combo_counter.most_common(top_n)),
        'wildcard_hits': {
            'count': len(wildcard_combos),
            'total_occurrences': sum(wildcard_combos.values()),
            'combos': dict(wildcard_combos.most_common(top_n)),
        },
        'fallback_hits': {
            'count': len(fallback_combos),
            'total_occurrences': sum(fallback_combos.values()),
            'combos': dict(fallback_combos.most_common(top_n)),
        },
        'evalbox_distribution': dict(evalbox_counter.most_common()),
    }

    return report


def print_report(report: Dict, top_n: int = 20):
    """Affiche le rapport de manière lisible."""
    meta = report['metadata']
    print("\n" + "="*70)
    print("RAPPORT D'ANALYSE DES NOTES [META] CRM")
    print("="*70)
    print(f"Généré le : {meta['generated_at'][:19]}")
    print(f"Total enregistrements : {meta['total_records']}")
    print(f"  dont avec intention : {meta['records_with_intent']}")
    print(f"  combinaisons uniques : {meta['unique_combos']}")

    print("\n── QUALITÉ DES MATCHES ─────────────────────────────")
    mq = report['match_quality_summary']
    total_intented = report['metadata']['records_with_intent']
    for match_type, count in sorted(mq.items(), key=lambda x: -x[1]):
        pct = (count / total_intented * 100) if total_intented else 0
        bar = '█' * int(pct / 2)
        print(f"  {match_type:<12} : {count:>5} ({pct:5.1f}%) {bar}")

    print("\n── TOP ÉTATS ───────────────────────────────────────")
    for state, count in list(report['top_states'].items())[:15]:
        print(f"  {state:<45} : {count}")

    print("\n── TOP INTENTIONS ──────────────────────────────────")
    for intent, count in list(report['top_intents'].items())[:15]:
        print(f"  {intent:<45} : {count}")

    print("\n── WILDCARD HITS (priorité d'ajout dans la matrice) ─")
    wc = report['wildcard_hits']
    print(f"  {wc['count']} combinaisons distinctes, {wc['total_occurrences']} occurrences total\n")
    for combo, count in list(wc['combos'].items())[:top_n]:
        state, _, intent = combo.partition(':')
        print(f"  {count:>4}x  {state:<35} + {intent}")

    if report['fallback_hits']['count'] > 0:
        print("\n── FALLBACK HITS (pas de match du tout ! 🚨) ───────")
        fb = report['fallback_hits']
        print(f"  {fb['count']} combinaisons, {fb['total_occurrences']} occurrences\n")
        for combo, count in list(fb['combos'].items())[:10]:
            state, _, intent = combo.partition(':')
            print(f"  {count:>4}x  {state:<35} + {intent}")

    print("\n── DISTRIBUTION EVALBOX ────────────────────────────")
    for evalbox, count in report['evalbox_distribution'].items():
        print(f"  {evalbox:<35} : {count}")

    print("\n── RECOMMANDATIONS ─────────────────────────────────")
    wc_combos = report['wildcard_hits']['combos']
    if wc_combos:
        print("  Ajouter en priorité dans state_intention_matrix.yaml :")
        print("  (du plus fréquent au moins fréquent)\n")
        for i, (combo, count) in enumerate(list(wc_combos.items())[:10], 1):
            state, _, intent = combo.partition(':')
            print(f"  {i:2}. \"{combo}\"  ({count}x)")
            print(f"      → template: response_master.html")
            print(f"      → context_flags:")
            intent_flag = intent.lower()
            print(f"          intention_{intent_flag}: true")
            print()

    print("="*70 + "\n")


def main():
    parser = argparse.ArgumentParser(description='Analyse des notes [META] CRM')
    parser.add_argument('--weeks', type=int, default=8, help='Nombre de semaines à analyser (défaut: 8)')
    parser.add_argument('--top', type=int, default=20, help='Top N résultats à afficher (défaut: 20)')
    parser.add_argument('--output', type=str, default=None, help='Fichier JSON de sortie (optionnel)')
    parser.add_argument('--verbose', '-v', action='store_true', help='Afficher la progression détaillée')
    args = parser.parse_args()

    # Charger la matrice
    matrix_path = Path(__file__).parent / 'states' / 'state_intention_matrix.yaml'
    if not matrix_path.exists():
        print(f"❌ Matrice non trouvée: {matrix_path}")
        sys.exit(1)

    print(f"📂 Chargement matrice : {matrix_path}")
    explicit_keys, wildcard_intentions = load_matrix(matrix_path)
    print(f"   {len(explicit_keys)} entrées explicites | {len(wildcard_intentions)} wildcards\n")

    # Initialiser le client CRM
    try:
        from src.zoho_client import ZohoCRMClient
        crm_client = ZohoCRMClient()
    except Exception as e:
        print(f"❌ Impossible d'initialiser le client CRM: {e}")
        print("   Vérifiez votre fichier .env et les tokens Zoho.")
        sys.exit(1)

    print(f"🔍 Analyse des notes CRM ({args.weeks} dernières semaines)...\n")

    # Récupérer les enregistrements [META]
    records = fetch_all_meta_records(crm_client, weeks=args.weeks, verbose=args.verbose)

    if not records:
        print("⚠️  Aucun enregistrement [META] trouvé.")
        print("   Vérifiez que le workflow tourne et écrit des notes CRM.")
        sys.exit(0)

    # Analyser
    report = analyze_records(records, explicit_keys, wildcard_intentions, top_n=args.top)

    # Afficher
    print_report(report, top_n=args.top)

    # Sauvegarder si demandé
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"💾 Rapport sauvegardé dans : {output_path}")


if __name__ == '__main__':
    main()
