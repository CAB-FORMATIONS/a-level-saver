#!/usr/bin/env python3
"""
Post-batch health check — détecte automatiquement les bugs et incohérences.

Usage:
    python batch_health_check.py data/batch_results_20260215_081031_cycle1.json
    python batch_health_check.py data/batch_results_*.json          # Plusieurs fichiers
    python batch_health_check.py --latest                           # Dernier fichier
    python batch_health_check.py --latest --json                    # Sortie JSON

Niveaux de sévérité:
    CRITICAL — Le draft est cassé ou vide (action immédiate requise)
    ERROR    — Le contenu est faux ou dangereux (correction nécessaire)
    WARNING  — Incohérence détectable (à investiguer)
    INFO     — Pattern cross-ticket ou dégradation qualité
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

os.environ['PYTHONIOENCODING'] = 'utf-8'
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


# ============================================================
# DATE UTILITIES
# ============================================================

DATE_PATTERN_DMY = re.compile(r'\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b')
DATE_PATTERN_YMD = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')


def extract_dates_from_text(text: str) -> List[str]:
    """Extrait toutes les dates d'un texte, retourne en format YYYY-MM-DD."""
    if not text:
        return []
    dates = set()
    for m in DATE_PATTERN_DMY.finditer(text):
        d, mo, y = m.groups()
        dates.add(f"{y}-{mo.zfill(2)}-{d.zfill(2)}")
    for m in DATE_PATTERN_YMD.finditer(text):
        y, mo, d = m.groups()
        dates.add(f"{y}-{mo}-{d}")
    return sorted(dates)


def is_past_date(date_str: str, ref_date: Optional[str] = None) -> bool:
    """Vérifie si une date est dans le passé."""
    try:
        ref = datetime.strptime(ref_date, "%Y-%m-%d") if ref_date else datetime.now()
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.date() < ref.date()
    except (ValueError, TypeError):
        return False


def normalize_date(date_str: str) -> Optional[str]:
    """Normalise une date vers YYYY-MM-DD."""
    if not date_str:
        return None
    # YYYY-MM-DD
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', str(date_str))
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    # DD/MM/YYYY
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{4})', str(date_str))
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return None


# ============================================================
# HEALTH CHECK ENGINE
# ============================================================

class Issue:
    """Un problème détecté par le health check."""
    def __init__(self, check_id: str, severity: str, ticket_id: str,
                 message: str, details: Optional[Dict] = None):
        self.check_id = check_id
        self.severity = severity
        self.ticket_id = ticket_id
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict:
        return {
            'check_id': self.check_id,
            'severity': self.severity,
            'ticket_id': self.ticket_id,
            'message': self.message,
            'details': self.details,
        }

    def __repr__(self):
        return f"[{self.severity}] {self.check_id} | {self.ticket_id} | {self.message}"


class HealthChecker:
    """Exécute tous les health checks sur les résultats de batch."""

    # Termes internes qui ne doivent JAMAIS apparaître dans un draft
    FORBIDDEN_TERMS = [
        'BFS', 'Evalbox', 'evalbox', 'ExamT3P', 'examt3p',
        'CDJ', 'CDS', 'cdj-', 'cds-', 'Montreuil',
        'state_engine', 'template_engine', 'triage_result',
        'context_data', 'placeholder', 'pybars',
    ]

    # Patterns de confirmation (ne doivent PAS apparaître si report_bloque)
    CONFIRMATION_PATTERNS = [
        r'votre date a .t. modifi',
        r'enregistr. votre choix',
        r'nouvelle date d.examen.*enregistr',
        r'nous avons bien modifi',
        r'votre inscription.*confirm',
    ]

    # Patterns de proposition de dates/sessions
    DATE_PROPOSAL_PATTERNS = [
        r'prochaines? dates?',
        r'voici les dates',
        r'dates? disponibles?',
        r'session de formation',
        r'cours du jour',
        r'cours du soir',
        r'18h.*22h',
        r'8h30.*17h30',
    ]

    ANNULATION_KEYWORDS = [
        'annuler', 'annulation', 'rembourser', 'remboursement',
        'résilier', 'résiliation', 'rétractation',
    ]

    def __init__(self, ref_date: Optional[str] = None):
        self.ref_date = ref_date or datetime.now().strftime("%Y-%m-%d")
        self.issues: List[Issue] = []

    def check_all(self, results: List[Dict]) -> List[Issue]:
        """Exécute tous les checks sur une liste de résultats."""
        self.issues = []

        # Per-ticket checks
        for r in results:
            if not isinstance(r, dict):
                continue
            # Skip non-GO tickets pour les checks de contenu
            stage = r.get('stage', '')
            if stage.startswith('SKIPPED'):
                continue

            self._check_critical(r)
            self._check_content_errors(r)
            self._check_warnings(r)

        # Cross-ticket checks
        self._check_cross_ticket(results)

        return self.issues

    # ================================================================
    # CRITICAL CHECKS — Le draft est cassé
    # ================================================================

    def _check_critical(self, r: Dict):
        tid = r.get('ticket_id', '?')
        stage = r.get('stage', '')
        draft = r.get('draft_content') or r.get('output', {}).get('draft_content') or ''
        draft_created = r.get('draft_created', False)
        reply_sent = r.get('reply_sent', False)

        # C1: Draft vide sur ticket COMPLETED
        if stage == 'COMPLETED' and draft_created and len(draft.strip()) < 50:
            self.issues.append(Issue(
                'C1_EMPTY_DRAFT', 'CRITICAL', tid,
                f"Draft vide ou trop court ({len(draft.strip())} chars) sur ticket COMPLETED",
                {'draft_length': len(draft.strip())}
            ))

        # C2: COMPLETED mais ni draft ni reply
        if stage == 'COMPLETED' and not draft_created and not reply_sent:
            self.issues.append(Issue(
                'C2_NO_DELIVERY', 'CRITICAL', tid,
                "Stage COMPLETED mais aucun draft créé ni reply envoyé"
            ))

        # C3: Placeholders non résolus dans le draft
        if draft and ('{{' in draft or '}}' in draft):
            placeholders = re.findall(r'\{\{[^}]+\}\}', draft)
            self.issues.append(Issue(
                'C3_UNRESOLVED_PLACEHOLDERS', 'CRITICAL', tid,
                f"{len(placeholders)} placeholder(s) non résolu(s) dans le draft",
                {'placeholders': placeholders[:5]}
            ))

    # ================================================================
    # ERROR CHECKS — Le contenu est faux ou dangereux
    # ================================================================

    def _check_content_errors(self, r: Dict):
        tid = r.get('ticket_id', '?')
        draft = r.get('draft_content') or r.get('output', {}).get('draft_content') or ''
        if not draft:
            return

        intent = r.get('intent') or r.get('triage', {}).get('detected_intent') or ''
        stage = r.get('stage', '')
        ctx = r.get('ctx_flags') or {}
        dossier_termine = r.get('dossier_termine', False)
        evalbox = r.get('evalbox') or (r.get('template_vars') or {}).get('evalbox') or ''
        report_bloque = ctx.get('report_bloque', False) or (r.get('template_vars') or {}).get('report_bloque', False)

        draft_lower = draft.lower()

        # E1: 20€ leaked dans le draft final
        if intent != 'DEMANDE_ANNULATION':
            if re.search(r'20\s*[€euros]|vingt\s*euros', draft_lower):
                self.issues.append(Issue(
                    'E1_AMOUNT_LEAK', 'ERROR', tid,
                    "Montant 20€ mentionné dans le draft (interdit sauf DEMANDE_ANNULATION)",
                    {'intent': intent}
                ))

        # E2: Termes internes dans le draft
        for term in self.FORBIDDEN_TERMS:
            if term in draft:
                self.issues.append(Issue(
                    'E2_INTERNAL_TERM', 'ERROR', tid,
                    f"Terme interne '{term}' trouvé dans le draft",
                    {'term': term}
                ))
                break  # Un seul par ticket

        # E3: dossier_termine + dates/sessions proposées
        if dossier_termine and intent not in ('DEMANDE_REINSCRIPTION', 'REPORT_DATE'):
            for pattern in self.DATE_PROPOSAL_PATTERNS:
                if re.search(pattern, draft_lower):
                    self.issues.append(Issue(
                        'E3_DOSSIER_TERMINE_DATES', 'ERROR', tid,
                        f"dossier_termine=True mais draft propose dates/sessions (pattern: {pattern})",
                        {'intent': intent, 'dossier_termine': True}
                    ))
                    break

        # E4: report_bloque + langage de confirmation
        if report_bloque:
            for pattern in self.CONFIRMATION_PATTERNS:
                if re.search(pattern, draft_lower):
                    self.issues.append(Issue(
                        'E4_BLOCKED_CONFIRMATION', 'ERROR', tid,
                        "report_bloque=True mais draft contient langage de confirmation",
                        {'pattern': pattern}
                    ))
                    break

        # E5: Date passée proposée comme future
        draft_dates = extract_dates_from_text(draft)
        # Exclure les dates dans un contexte "passé" (a eu lieu, ancien, etc.)
        past_context_pattern = re.compile(
            r'(a eu lieu|pass[ée]|ancien|pr[ée]c[ée]dent|était prévu)',
            re.IGNORECASE
        )
        for date_str in draft_dates:
            if is_past_date(date_str, self.ref_date):
                # Vérifier si la date est dans un contexte "passé"
                date_in_draft = re.search(
                    rf'(.{{0,80}}){re.escape(date_str[:4])}',
                    draft
                )
                context_text = date_in_draft.group(1) if date_in_draft else ''
                if not past_context_pattern.search(context_text):
                    # Vérifier aussi format DD/MM/YYYY
                    parts = date_str.split('-')
                    dmy = f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else ''
                    dmy_context = ''
                    if dmy:
                        m2 = re.search(rf'(.{{0,80}}){re.escape(dmy)}', draft)
                        dmy_context = m2.group(1) if m2 else ''
                    if not past_context_pattern.search(dmy_context):
                        self.issues.append(Issue(
                            'E5_PAST_DATE', 'ERROR', tid,
                            f"Date passée {date_str} proposée dans le draft",
                            {'date': date_str, 'ref_date': self.ref_date}
                        ))

        # E6: Proposition transformée en confirmation (pré vs post humanizer)
        template_resp = r.get('template_response', '')
        if template_resp and r.get('was_humanized'):
            # Le template propose, le draft confirme?
            template_lower = template_resp.lower()
            has_proposal = any(re.search(p, template_lower) for p in [
                r'merci de.*confirmer', r'voici les.*alternatives',
                r'souhaitez-vous', r'quel.*choix', r'merci de nous indiquer',
            ])
            has_confirmation = any(re.search(p, draft_lower) for p in self.CONFIRMATION_PATTERNS)
            if has_proposal and has_confirmation:
                self.issues.append(Issue(
                    'E6_PROPOSAL_TO_CONFIRMATION', 'ERROR', tid,
                    "Humanizer a transformé une proposition en confirmation",
                ))

        # E7: Codes session internes dans le draft
        if re.search(r'cd[js]-\w+-\w+|CDS\s+Montreuil|CDJ\s+Paris', draft):
            self.issues.append(Issue(
                'E7_SESSION_CODE_LEAK', 'ERROR', tid,
                "Code session interne (cds-/cdj-) trouvé dans le draft"
            ))

        # E8: VALIDE CMA + instructions d'inscription
        if evalbox in ('VALIDE CMA', 'Convoc CMA reçue', 'Convoc CMA recue'):
            inscription_patterns = [
                r'compl[ée]ter votre dossier',
                r'envoyer vos documents',
                r'cr[ée]er votre compte',
                r'transmettre.*pi[èe]ces',
            ]
            for p in inscription_patterns:
                if re.search(p, draft_lower):
                    self.issues.append(Issue(
                        'E8_VALIDATED_INSCRIPTION', 'ERROR', tid,
                        f"evalbox={evalbox} mais draft contient instructions d'inscription",
                        {'evalbox': evalbox, 'pattern': p}
                    ))
                    break

    # ================================================================
    # WARNING CHECKS — Incohérences détectables
    # ================================================================

    def _check_warnings(self, r: Dict):
        tid = r.get('ticket_id', '?')
        draft = r.get('draft_content') or r.get('output', {}).get('draft_content') or ''
        stage = r.get('stage', '')
        intent = r.get('intent') or r.get('triage', {}).get('detected_intent') or ''
        ctx = r.get('ctx_flags') or {}
        customer_msg = r.get('customer_message', '')
        triage_action = r.get('triage_action', '')
        valid_dates = r.get('valid_dates', [])

        # W1: Date hallucination — date dans le draft absente de toutes les sources
        if draft and valid_dates:
            draft_dates = extract_dates_from_text(draft)
            normalized_valid = set()
            for vd in valid_dates:
                nd = normalize_date(vd)
                if nd:
                    normalized_valid.add(nd)
            for dd in draft_dates:
                nd = normalize_date(dd)
                if nd and nd not in normalized_valid:
                    # Pas forcément un bug (dates formatées différemment), mais suspect
                    self.issues.append(Issue(
                        'W1_DATE_HALLUCINATION', 'WARNING', tid,
                        f"Date {dd} dans le draft absente des sources valides",
                        {'date': dd, 'valid_dates_count': len(valid_dates)}
                    ))

        # W2: Uber bloqué mais draft contient dates/sessions
        uber_blocked = ctx.get('uber_cas_d', False) or ctx.get('uber_cas_e', False)
        if uber_blocked and draft:
            draft_lower = draft.lower()
            for pattern in self.DATE_PROPOSAL_PATTERNS[:4]:  # dates only, pas sessions
                if re.search(pattern, draft_lower):
                    self.issues.append(Issue(
                        'W2_UBER_BLOCKED_DATES', 'WARNING', tid,
                        "Uber CAS D/E mais draft contient dates/sessions",
                        {'uber_cas_d': ctx.get('uber_cas_d'), 'uber_cas_e': ctx.get('uber_cas_e')}
                    ))
                    break

        # W3: Doublon Uber sur demande non-Uber (Rule 17)
        if stage == 'DUPLICATE_UBER_OFFER' and customer_msg:
            non_uber_keywords = [
                'cpf', 'compte cpf', 'compte formation', 'moncompteformation',
                'france travail', 'kairos', 'pole emploi', 'pôle emploi',
                '720', 'tarif complet', 'payer moi-même',
                'devis', 'facture pro forma', 'proforma',
                'opco', 'fafcea', 'agefice', 'fifpl', 'fif pl',
            ]
            msg_lower = customer_msg.lower()
            for kw in non_uber_keywords:
                if kw in msg_lower:
                    self.issues.append(Issue(
                        'W3_DOUBLON_NON_UBER', 'WARNING', tid,
                        f"DUPLICATE_UBER mais message contient keyword non-Uber: '{kw}'",
                        {'keyword': kw}
                    ))
                    break

        # W4: SalesIQ metadata false routing
        if triage_action == 'ROUTE' and customer_msg:
            salesiq_markers = [
                'informations sur le visiteur', 'prise en charge de java',
                'navigateur', 'résolution d\'écran', 'système d\'exploitation',
            ]
            msg_lower = customer_msg.lower()
            for marker in salesiq_markers:
                if marker in msg_lower:
                    self.issues.append(Issue(
                        'W4_SALESIQ_CONTAMINATION', 'WARNING', tid,
                        f"ROUTE possible faux positif: SalesIQ metadata détectée ('{marker}')",
                        {'marker': marker}
                    ))
                    break

        # W5: Humanizer a perdu des dates
        template_resp = r.get('template_response', '')
        if template_resp and draft and r.get('was_humanized'):
            template_dates = set(extract_dates_from_text(template_resp))
            draft_dates_set = set(extract_dates_from_text(draft))
            lost_dates = template_dates - draft_dates_set
            if lost_dates:
                self.issues.append(Issue(
                    'W5_HUMANIZER_LOST_DATES', 'WARNING', tid,
                    f"Humanizer a perdu {len(lost_dates)} date(s): {sorted(lost_dates)}",
                    {'lost_dates': sorted(lost_dates)}
                ))

        # W6: CRM update bloqué — Date_examen_VTC modifiée quand interdit
        crm_updates = r.get('crm_updates') or r.get('output', {}).get('crm_updates') or {}
        can_modify = ctx.get('can_modify_exam_date', True)
        if not can_modify and 'Date_examen_VTC' in crm_updates:
            self.issues.append(Issue(
                'W6_BLOCKED_DATE_CRM_UPDATE', 'WARNING', tid,
                "can_modify_exam_date=False mais CRM update Date_examen_VTC effectué",
                {'crm_updates': crm_updates}
            ))

        # W7: Validation non-compliant
        val_compliant = r.get('validation_compliant')
        if val_compliant is None:
            val = r.get('validation', {})
            if isinstance(val, dict) and 'compliant' in val:
                val_compliant = val['compliant']
        if val_compliant is False:
            val_errors = r.get('validation_errors') or r.get('validation', {}).get('errors', [])
            error_types = []
            for e in val_errors:
                if isinstance(e, dict):
                    error_types.append(e.get('type', 'unknown'))
                elif isinstance(e, str):
                    error_types.append('unknown')
            self.issues.append(Issue(
                'W7_VALIDATION_FAILED', 'WARNING', tid,
                f"Validation échouée: {', '.join(error_types) or 'inconnue'}",
                {'error_types': error_types, 'error_count': len(val_errors)}
            ))

        # W8: Humanizer failed
        humanizer_failed = r.get('humanizer_failed', False)
        if not humanizer_failed:
            hm = r.get('humanizer', {})
            if isinstance(hm, dict):
                humanizer_failed = hm.get('failed', False) or bool(hm.get('error'))
        if humanizer_failed:
            issues = r.get('humanizer_issues') or (r.get('humanizer', {}) or {}).get('issues', [])
            error = (r.get('humanizer', {}) or {}).get('error')
            self.issues.append(Issue(
                'W8_HUMANIZER_FAILED', 'WARNING', tid,
                f"Humanizer échoué: {error or ', '.join(str(i) for i in issues) or 'raison inconnue'}",
                {'issues': issues, 'error': error}
            ))

        # W9: Session change avec 1 seule option
        if intent == 'DEMANDE_CHANGEMENT_SESSION' and draft:
            session_mentions = len(re.findall(r'cours du (jour|soir)', draft.lower()))
            if session_mentions < 2 and stage == 'COMPLETED':
                self.issues.append(Issue(
                    'W9_SINGLE_SESSION_OPTION', 'WARNING', tid,
                    "DEMANDE_CHANGEMENT_SESSION mais draft ne propose qu'une seule session",
                    {'session_mentions': session_mentions}
                ))

        # W10: HTML cassé
        if draft:
            open_tags = len(re.findall(r'<b>', draft))
            close_tags = len(re.findall(r'</b>', draft))
            if open_tags != close_tags:
                self.issues.append(Issue(
                    'W10_BROKEN_HTML', 'WARNING', tid,
                    f"Tags <b> déséquilibrés: {open_tags} ouvrantes vs {close_tags} fermantes",
                ))

    # ================================================================
    # CROSS-TICKET CHECKS
    # ================================================================

    def _check_cross_ticket(self, results: List[Dict]):
        # I1: Taux de SKIPPED_DRAFT_EXISTS élevé
        stages = Counter(r.get('stage', '') for r in results if isinstance(r, dict))
        total = sum(stages.values())
        if total > 10:
            skip_rate = stages.get('SKIPPED_DRAFT_EXISTS', 0) / total
            if skip_rate > 0.7:
                self.issues.append(Issue(
                    'I1_HIGH_SKIP_RATE', 'INFO', 'BATCH',
                    f"Taux SKIPPED_DRAFT_EXISTS = {skip_rate:.0%} ({stages['SKIPPED_DRAFT_EXISTS']}/{total})",
                    {'skip_rate': round(skip_rate, 2), 'total': total}
                ))

        # I2: Même intent toujours en échec validation
        intent_validation = defaultdict(lambda: {'total': 0, 'failed': 0})
        for r in results:
            if not isinstance(r, dict) or r.get('stage', '').startswith('SKIPPED'):
                continue
            intent_key = r.get('intent') or r.get('triage', {}).get('detected_intent') or 'N/A'
            val_compliant = r.get('validation_compliant')
            if val_compliant is None:
                val = r.get('validation', {})
                if isinstance(val, dict):
                    val_compliant = val.get('compliant')
            if val_compliant is not None:
                intent_validation[intent_key]['total'] += 1
                if not val_compliant:
                    intent_validation[intent_key]['failed'] += 1

        for intent_key, stats in intent_validation.items():
            if stats['total'] >= 3 and stats['failed'] / stats['total'] > 0.3:
                self.issues.append(Issue(
                    'I2_INTENT_VALIDATION_RATE', 'INFO', 'BATCH',
                    f"Intent {intent_key}: {stats['failed']}/{stats['total']} ({stats['failed']/stats['total']:.0%}) validation échouée",
                    {'intent': intent_key, **stats}
                ))

        # I3: Même intent toujours humanizer fail
        intent_humanizer = defaultdict(lambda: {'total': 0, 'failed': 0})
        for r in results:
            if not isinstance(r, dict) or r.get('stage', '').startswith('SKIPPED'):
                continue
            intent_key = r.get('intent') or 'N/A'
            was_h = r.get('was_humanized')
            if was_h is None:
                hm = r.get('humanizer', {})
                if isinstance(hm, dict):
                    was_h = hm.get('was_humanized')
            hfailed = r.get('humanizer_failed', False)
            if not hfailed:
                hm = r.get('humanizer', {})
                if isinstance(hm, dict):
                    hfailed = hm.get('failed', False)
            if was_h is not None or hfailed:
                intent_humanizer[intent_key]['total'] += 1
                if hfailed:
                    intent_humanizer[intent_key]['failed'] += 1

        for intent_key, stats in intent_humanizer.items():
            if stats['total'] >= 3 and stats['failed'] / stats['total'] > 0.3:
                self.issues.append(Issue(
                    'I3_INTENT_HUMANIZER_RATE', 'INFO', 'BATCH',
                    f"Intent {intent_key}: {stats['failed']}/{stats['total']} ({stats['failed']/stats['total']:.0%}) humanizer échoué",
                    {'intent': intent_key, **stats}
                ))

        # I4: Même deal_id avec réponses contradictoires
        deal_responses = defaultdict(list)
        for r in results:
            if not isinstance(r, dict):
                continue
            deal_id = r.get('deal_id')
            if deal_id and r.get('stage') == 'COMPLETED':
                deal_responses[deal_id].append({
                    'ticket_id': r.get('ticket_id'),
                    'dossier_termine': r.get('dossier_termine'),
                    'evalbox': r.get('evalbox') or (r.get('template_vars') or {}).get('evalbox'),
                })

        for deal_id, responses in deal_responses.items():
            if len(responses) < 2:
                continue
            dt_values = set(r['dossier_termine'] for r in responses if r['dossier_termine'] is not None)
            if len(dt_values) > 1:
                self.issues.append(Issue(
                    'I4_CONTRADICTORY_RESPONSES', 'INFO', deal_id,
                    f"Deal {deal_id}: dossier_termine incohérent entre {len(responses)} tickets",
                    {'tickets': [r['ticket_id'] for r in responses]}
                ))


# ============================================================
# REPORT GENERATION
# ============================================================

def generate_report(issues: List[Issue], results: List[Dict]) -> Dict:
    """Génère un rapport structuré."""
    by_severity = defaultdict(list)
    by_check = defaultdict(list)
    for issue in issues:
        by_severity[issue.severity].append(issue)
        by_check[issue.check_id].append(issue)

    # Stats globales
    total_tickets = len([r for r in results if isinstance(r, dict) and not r.get('stage', '').startswith('SKIPPED')])
    go_tickets = len([r for r in results if isinstance(r, dict) and r.get('triage_action') == 'GO'])
    completed = len([r for r in results if isinstance(r, dict) and r.get('stage') == 'COMPLETED'])

    # Humanizer stats
    humanized = sum(1 for r in results if isinstance(r, dict) and (
        r.get('was_humanized') or (isinstance(r.get('humanizer'), dict) and r['humanizer'].get('was_humanized'))
    ))
    humanizer_failed = sum(1 for r in results if isinstance(r, dict) and (
        r.get('humanizer_failed') or (isinstance(r.get('humanizer'), dict) and (r['humanizer'].get('failed') or r['humanizer'].get('error')))
    ))

    # Validation stats
    val_failed = sum(1 for r in results if isinstance(r, dict) and (
        r.get('validation_compliant') is False or
        (isinstance(r.get('validation'), dict) and r['validation'].get('compliant') is False)
    ))

    return {
        'timestamp': datetime.now().isoformat(),
        'stats': {
            'total_tickets': total_tickets,
            'go_tickets': go_tickets,
            'completed': completed,
            'humanized': humanized,
            'humanizer_failed': humanizer_failed,
            'humanizer_rate': f"{humanized/(go_tickets or 1):.0%}",
            'validation_failed': val_failed,
        },
        'summary': {
            'CRITICAL': len(by_severity.get('CRITICAL', [])),
            'ERROR': len(by_severity.get('ERROR', [])),
            'WARNING': len(by_severity.get('WARNING', [])),
            'INFO': len(by_severity.get('INFO', [])),
            'total_issues': len(issues),
        },
        'checks': {
            check_id: {
                'count': len(items),
                'severity': items[0].severity,
                'sample_ticket': items[0].ticket_id,
                'sample_message': items[0].message,
            }
            for check_id, items in sorted(by_check.items())
        },
        'issues': [i.to_dict() for i in issues],
    }


def print_report(report: Dict):
    """Affiche le rapport en console."""
    stats = report['stats']
    summary = report['summary']

    print(f"\n{'='*70}")
    print("BATCH HEALTH CHECK REPORT")
    print(f"{'='*70}")
    print(f"  Date: {report['timestamp'][:19]}")
    print(f"  Tickets analysés: {stats['total_tickets']} (GO: {stats['go_tickets']}, COMPLETED: {stats['completed']})")
    print(f"  Humanizer: {stats['humanized']} OK, {stats['humanizer_failed']} échoués ({stats['humanizer_rate']})")
    print(f"  Validation: {stats['validation_failed']} échouée(s)")

    print(f"\n{'─'*70}")
    total = summary['total_issues']
    if total == 0:
        print("  ✅ Aucun problème détecté !")
    else:
        print(f"  Issues: {total} total")
        for sev in ['CRITICAL', 'ERROR', 'WARNING', 'INFO']:
            count = summary.get(sev, 0)
            if count > 0:
                icon = {'CRITICAL': '🔴', 'ERROR': '🟠', 'WARNING': '🟡', 'INFO': '🔵'}[sev]
                print(f"    {icon} {sev}: {count}")

    # Détail par check
    if report['checks']:
        print(f"\n{'─'*70}")
        print("  Par check:")
        for check_id, info in report['checks'].items():
            icon = {'CRITICAL': '🔴', 'ERROR': '🟠', 'WARNING': '🟡', 'INFO': '🔵'}.get(info['severity'], '⚪')
            print(f"    {icon} {check_id}: {info['count']}x — {info['sample_message'][:80]}")

    # Top 10 issues détaillées
    issues = report.get('issues', [])
    critical_and_errors = [i for i in issues if i['severity'] in ('CRITICAL', 'ERROR')]
    if critical_and_errors:
        print(f"\n{'─'*70}")
        print(f"  Top issues (CRITICAL+ERROR):")
        for i in critical_and_errors[:15]:
            print(f"    [{i['severity']}] {i['check_id']} | ticket {i['ticket_id']}")
            print(f"           {i['message'][:100]}")

    print(f"\n{'='*70}\n")


# ============================================================
# LOADING
# ============================================================

def load_batch_results(filepaths: List[str]) -> List[Dict]:
    """Charge et fusionne plusieurs fichiers de résultats."""
    all_results = []
    for fp in filepaths:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            all_results.extend(data)
        elif isinstance(data, dict) and 'results' in data:
            all_results.extend(data['results'])
    return all_results


def find_latest_batch_file() -> Optional[str]:
    """Trouve le fichier de résultats batch le plus récent."""
    data_dir = 'data'
    if not os.path.exists(data_dir):
        return None
    files = [
        os.path.join(data_dir, f) for f in os.listdir(data_dir)
        if f.startswith('batch_results_') and f.endswith('.json')
    ]
    if not files:
        return None
    return max(files, key=os.path.getmtime)


# ============================================================
# MAIN
# ============================================================

def run_health_check(filepaths: List[str], ref_date: Optional[str] = None) -> Dict:
    """Point d'entrée principal. Retourne le rapport."""
    results = load_batch_results(filepaths)
    checker = HealthChecker(ref_date=ref_date)
    issues = checker.check_all(results)
    return generate_report(issues, results)


def main():
    parser = argparse.ArgumentParser(
        description='Health check post-batch — détecte bugs et incohérences',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('files', nargs='*', help='Fichiers batch_results_*.json')
    parser.add_argument('--latest', action='store_true', help='Utiliser le dernier fichier')
    parser.add_argument('--json', action='store_true', help='Sortie JSON (pour automation)')
    parser.add_argument('--save', action='store_true', help='Sauvegarder le rapport dans data/')
    parser.add_argument('--ref-date', help='Date de référence YYYY-MM-DD (défaut: aujourd\'hui)')

    args = parser.parse_args()

    if args.latest:
        latest = find_latest_batch_file()
        if not latest:
            print("Aucun fichier batch trouvé dans data/")
            sys.exit(1)
        filepaths = [latest]
        print(f"Fichier: {latest}")
    elif args.files:
        filepaths = args.files
    else:
        parser.print_help()
        sys.exit(1)

    # Vérifier que les fichiers existent
    for fp in filepaths:
        if not os.path.exists(fp):
            print(f"Fichier introuvable: {fp}")
            sys.exit(1)

    report = run_health_check(filepaths, ref_date=args.ref_date)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)

    if args.save:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_file = f"data/health_check_{timestamp}.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(report, ensure_ascii=False, indent=2, fp=f)
        print(f"Rapport sauvegardé: {out_file}")

    # Exit code basé sur la sévérité
    if report['summary']['CRITICAL'] > 0:
        sys.exit(2)
    elif report['summary']['ERROR'] > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == '__main__':
    main()
