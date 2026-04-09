"""
MODULE D'EXTRACTION AUTOMATIQUE EXAMENT3P VIA HTTP
Version: 5.0
Date: 09/04/2026

Extrait automatiquement TOUTES les données du portail ExamenT3P :
- Vue d'ensemble : statut dossier, progression, actions requises, historique
- Mes Examens : dates, convocation
- Mes Documents : statut de chaque pièce justificative
- Mon Compte : informations personnelles
- Mes Paiements : historique complet des paiements
- Messages : échanges avec la CMA

V5.0 — Migration Playwright → HTTP (httpx + BeautifulSoup)
- Plus de navigateur Chromium (économie ~400MB RAM)
- Login via POST /Cma/UserAccount/login
- Dashboard via GET /mon-espace
- Parsing via BeautifulSoup (CSS selectors) + regex fallback
- Interface identique : extract_exament3p_sync() retourne le même dict

Usage:
    from exament3p_playwright import extract_exament3p_sync

    data = extract_exament3p_sync(identifiant, password)
"""

import re
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Configuration
MAX_RETRIES = 3
RETRY_DELAY = 2
HTTP_TIMEOUT = 30.0

BASE_URL = "https://www.exament3p.fr"
LOGIN_URL = f"{BASE_URL}/Cma/UserAccount/login"
DASHBOARD_URL = f"{BASE_URL}/mon-espace"
MESSAGES_URL = f"{BASE_URL}/Cmacandidate/getMessages"
INIT_URL = f"{BASE_URL}/id/14"
PAGE_ID = "14"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class ExamT3PLoginError(Exception):
    """Échec de connexion à exament3p.fr."""
    pass


class ExamT3PHttpClient:
    """Extracteur ExamT3P via requetes HTTP directes (remplace Playwright)."""

    def __init__(self, identifiant: str, password: str, max_retries: int = MAX_RETRIES,
                 num_dossier: Optional[str] = None):
        self.identifiant = identifiant
        self.password = password
        self.max_retries = max_retries
        self.expected_num_dossier = num_dossier
        self.data = {
            'identifiant': identifiant,
            'extraction_requise': True,
            'errors': []
        }

    def extract_all(self) -> Dict:
        """Extraction complete avec retry."""
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(
                    timeout=HTTP_TIMEOUT,
                    follow_redirects=True,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                ) as client:
                    # 1. Init session PHP (PHPSESSID cookie)
                    logger.info("   Init session PHP...")
                    client.get(INIT_URL)

                    # 2. Login
                    logger.info("   Connexion en cours...")
                    login_result = self._login(client)
                    if not login_result:
                        raise ExamT3PLoginError("Echec de connexion")
                    logger.info("   Connexion reussie")

                    # 3. Get dashboard HTML (dossier par defaut)
                    logger.info("   Recuperation du dashboard...")
                    dashboard_html = self._get_dashboard(client)

                    # 4. Verifier si on est sur le bon dossier (multi-dossier)
                    dashboard_html = self._ensure_correct_dossier(client, dashboard_html)

                    # 5. Parse all data from dashboard HTML
                    self._parse_all(dashboard_html)

                    # 6. Fetch messages via JSON API (separate endpoint)
                    self._fetch_messages_json(client)

                    # Mark success
                    self.data['extraction_requise'] = False
                    self.data['extraction_date'] = datetime.now().isoformat()
                    self.data['extraction_attempt'] = attempt

                    logger.info("   ✅ Extraction complète terminée")
                    return self.data

            except ExamT3PLoginError as e:
                self.data['errors'].append(f"Tentative {attempt}: {str(e)}")
                self.data['error'] = str(e)
                logger.warning(f"   ❌ Login échoué: {e}")
                return self.data  # Don't retry login failures

            except Exception as e:
                self.data['errors'].append(f"Tentative {attempt}: {str(e)}")
                if attempt < self.max_retries:
                    logger.warning(f"   ⚠️ Tentative {attempt}/{self.max_retries} échouée: {str(e)[:80]}")
                    time.sleep(RETRY_DELAY * attempt)
                else:
                    logger.error(f"   ❌ Échec après {self.max_retries} tentatives")
                    self.data['error'] = str(e)

        return self.data

    def _login(self, client: httpx.Client) -> bool:
        """Login via POST form-urlencoded. Retourne True si succès."""
        resp = client.post(
            LOGIN_URL,
            data={
                "email": self.identifiant,
                "password": self.password,
                "pageId": PAGE_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()

        try:
            result = resp.json()
        except Exception:
            raise ExamT3PLoginError(f"Réponse login non-JSON: {resp.text[:200]}")

        if result.get("success") == 1:
            return True

        msg = result.get("message", "unknown error")
        raise ExamT3PLoginError(f"Login échoué pour {self.identifiant}: {msg}")

    def _get_dashboard(self, client: httpx.Client, dossier_id: Optional[int] = None) -> str:
        """GET /mon-espace -> HTML du dashboard. Si dossier_id, charge ce dossier."""
        params = {}
        if dossier_id is not None:
            params["dossier"] = str(dossier_id)
        resp = client.get(DASHBOARD_URL, params=params)
        resp.raise_for_status()
        return resp.text

    def _ensure_correct_dossier(self, client: httpx.Client, html: str) -> str:
        """
        Verifie qu'on est sur le bon dossier (multi-dossier / reinscription).
        Si num_dossier attendu != dossier affiche, recharge le bon.
        Retourne le HTML du bon dossier.
        """
        if not self.expected_num_dossier:
            return html  # Pas de numero attendu, on prend le defaut

        soup = BeautifulSoup(html, "html.parser")
        dossiers = self._parse_dossier_select(soup)

        if not dossiers:
            return html  # Pas de select dropdown, un seul dossier

        self.data['dossiers_disponibles'] = [d['num'] for d in dossiers]
        logger.info(f"   Dossiers disponibles: {self.data['dossiers_disponibles']}")

        # Trouver le dossier correspondant
        match = self._find_matching_dossier(dossiers, self.expected_num_dossier)
        if not match:
            logger.warning(f"   Dossier {self.expected_num_dossier} introuvable dans {self.data['dossiers_disponibles']}")
            return html  # On garde le defaut

        if match.get('selected'):
            logger.info(f"   Dossier correct deja affiche: {match['num']}")
            return html

        # Recharger avec le bon dossier
        logger.info(f"   Chargement du dossier {match['num']} (id={match['id']})...")
        return self._get_dashboard(client, dossier_id=match['id'])

    @staticmethod
    def _parse_dossier_select(soup: BeautifulSoup) -> List[Dict]:
        """Parse le <select> de selection de dossier. Retourne [{id, num, selected}]."""
        dossiers = []
        select = soup.select_one("select[onchange]")
        if not select:
            return dossiers

        seen_ids = set()
        for option in select.find_all("option"):
            value = option.get("value", "")
            text = option.get_text(strip=True)
            match = re.search(r"dossier=(\d+)", value)
            if not match:
                continue
            dossier_id = int(match.group(1))
            # Exclure les placeholders
            if not re.match(r"^\d", text):
                continue
            if dossier_id in seen_ids:
                continue
            seen_ids.add(dossier_id)
            dossiers.append({
                "id": dossier_id,
                "num": text,
                "selected": option.has_attr("selected"),
            })

        return dossiers

    @staticmethod
    def _find_matching_dossier(dossiers: List[Dict], num_dossier: str) -> Optional[Dict]:
        """
        Trouve le dossier correspondant dans la liste.
        Gere les reinscriptions TEn (match ascendant seulement).
        """
        if not dossiers:
            return None
        if not num_dossier:
            # Prendre le dossier selectionne par defaut
            for d in dossiers:
                if d.get("selected"):
                    return d
            return dossiers[0]

        num_clean = num_dossier.strip().lstrip("0")

        # Match exact
        for d in dossiers:
            d_num = d["num"].strip().lstrip("0")
            if d_num == num_clean:
                return d

        # Match ascendant : si num_dossier=XXXTE1, accepter XXX (base)
        # Mais si num_dossier=XXX (pas de suffixe), NE PAS matcher XXXTE1
        num_has_suffix = bool(re.search(r"TE\d+$", num_clean))
        if num_has_suffix:
            n_base = re.sub(r"TE\d+$", "", num_clean)
            for d in dossiers:
                d_num = d["num"].strip().lstrip("0")
                d_base = re.sub(r"TE\d+$", "", d_num)
                if d_base == n_base:
                    return d

        return None

    def _fetch_messages_json(self, client: httpx.Client):
        """GET /Cmacandidate/getMessages -> JSON (endpoint dedie)."""
        try:
            resp = client.get(MESSAGES_URL, params={"page": "0"})
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, dict):
                messages_list = data.get('messages', data.get('data', []))
                if isinstance(messages_list, list):
                    self.data['messages'] = {
                        'nombre': len(messages_list),
                        'liste': []
                    }
                    for msg in messages_list:
                        if isinstance(msg, dict):
                            self.data['messages']['liste'].append({
                                'date': msg.get('date', msg.get('created_at', '')),
                                'expediteur': msg.get('sender', msg.get('from', 'CMA')),
                                'contenu': msg.get('message', msg.get('content', msg.get('text', '')))
                            })
            logger.info(f"   Messages: {self.data.get('messages', {}).get('nombre', 0)} message(s)")
        except Exception as e:
            # Non-bloquant : on garde le fallback regex du dashboard
            logger.debug(f"   Messages JSON non disponible: {str(e)[:60]}")

    # ------------------------------------------------------------------
    #  Parsing principal
    # ------------------------------------------------------------------

    def _parse_all(self, html: str):
        """Parse toutes les données depuis le HTML du dashboard."""
        soup = BeautifulSoup(html, "html.parser")
        text_content = soup.get_text(separator='\n')

        self._parse_status(soup, text_content)
        self._parse_exam_date(soup, text_content)
        self._parse_progression(text_content)
        self._parse_documents(soup, text_content)
        self._parse_actions(text_content)
        self._parse_paiements(text_content)
        self._parse_compte(text_content)
        self._parse_messages(text_content)

    # ------------------------------------------------------------------
    #  Statut dossier + candidat
    # ------------------------------------------------------------------

    def _parse_status(self, soup: BeautifulSoup, text_content: str):
        """Extraction statut dossier, numéro, nom candidat, type examen."""

        # --- Numéro de dossier (BS4 first) ---
        num_el = soup.select_one("strong.row_applicationNumber")
        if num_el:
            self.data['num_dossier'] = num_el.get_text(strip=True)
        else:
            match = re.search(r'N°\s*Dossier[:\s]*(\d+)', text_content)
            if match:
                self.data['num_dossier'] = match.group(1)
            else:
                match = re.search(r'(\d{8})\s*-\s*VTC', text_content)
                if match:
                    self.data['num_dossier'] = match.group(1)

        # --- Statut dossier (BS4 first → widget, badge) ---
        statut = None
        widget = soup.select_one("div.dashboard-widget div.h4")
        if widget:
            widget_text = widget.get_text(strip=True)
            # Le widget peut contenir la date, pas le statut
            if not re.match(r'^À partir du|^\d', widget_text):
                statut = widget_text

        if not statut:
            badge = soup.select_one("td.row_statusBadge span.status-badge")
            if badge:
                statut = badge.get_text(strip=True)

        if not statut:
            # Fallback regex (même logique que v4)
            statuts_par_priorite = [
                ('Incomplet', [r'statut[:\s]*incomplet', r'dossier[:\s]*incomplet', r'\bincomplet\b(?!\s*validé)']),
                ('Refusé', [r'statut[:\s]*refusé', r'dossier[:\s]*refusé', r'\brefusé\b']),
                ('En attente du paiement', [r'en attente du paiement', r'attente[:\s]*paiement']),
                ('En cours de composition', [r'en cours de composition']),
                ("En attente d'instruction des pièces", [r"en attente d'instruction", r'instruction des pièces']),
                ("En cours d'instruction", [r"en cours d'instruction"]),
                ('En attente de convocation', [r'en attente de convocation', r'attente[:\s]*convocation']),
                ('Dossier validé', [r'dossier validé', r'dossier[:\s]*validé']),
                ('Valide', [r'statut[:\s]*valide\b', r'(?<!document[:\s])(?<!pièce[:\s])valide\s*$']),
            ]
            for s, patterns in statuts_par_priorite:
                for pattern in patterns:
                    if re.search(pattern, text_content, re.IGNORECASE):
                        statut = s
                        break
                if statut:
                    break

        if statut:
            self.data['statut_dossier'] = statut

        # --- Nom candidat ---
        match = re.search(r'Bienvenue\s+([A-Za-zÀ-ÿ\s]+)\s+-', text_content)
        if match:
            self.data['nom_candidat'] = match.group(1).strip()

        # --- Type d'examen et département ---
        match = re.search(r'-\s*(VTC|Taxi|VMDTR)\s*-\s*(Complète|Réinscription|Mobilité)?\s*-?\s*(\d{2,3})?', text_content)
        if match:
            self.data['type_examen'] = match.group(1)
            if match.group(2):
                self.data['type_epreuve'] = match.group(2)
            if match.group(3):
                self.data['departement'] = match.group(3)

        # --- Département (BS4 timeline) ---
        if 'departement' not in self.data:
            dept = self._parse_departement_bs4(soup)
            if dept:
                self.data['departement'] = dept

        # --- Date réception dossier ---
        match = re.search(r'Dossier reçu le\s+(\d{2}/\d{2}/\d{4})', text_content)
        if match:
            self.data['date_reception_dossier'] = match.group(1)

    def _parse_departement_bs4(self, soup: BeautifulSoup) -> Optional[str]:
        """Extrait le département depuis la timeline (BS4)."""
        for title in soup.select("h6.timeline-title"):
            if "partement" in title.get_text().lower():
                content_div = title.find_parent("div", class_="timeline-content")
                if not content_div:
                    content_div = title.find_parent()
                desc = content_div.find("p", class_="timeline-description") if content_div else None
                if desc:
                    text = desc.get_text(strip=True)
                    match = re.match(r"(\d[\dABab]{1,2})\s*[-–•]", text)
                    if match:
                        return match.group(1).upper()
        return None

    # ------------------------------------------------------------------
    #  Date d'examen
    # ------------------------------------------------------------------

    def _parse_exam_date(self, soup: BeautifulSoup, text_content: str):
        """Extraction date d'examen."""
        self.data['examens'] = {}

        mois_map = {
            'janvier': 1, 'février': 2, 'fevrier': 2, 'mars': 3,
            'avril': 4, 'mai': 5, 'juin': 6, 'juillet': 7,
            'août': 8, 'aout': 8, 'septembre': 9, 'octobre': 10,
            'novembre': 11, 'décembre': 12, 'decembre': 12,
        }

        # BS4 — div.h4.fw-bold "À partir du ..."
        for el in soup.select("div.h4.fw-bold, div.h4"):
            text = el.get_text(strip=True)
            if "partir du" not in text.lower():
                continue
            match = re.search(r"(\d{1,2})\s+([a-zéûà]+)\s+(\d{4})", text, re.IGNORECASE)
            if match:
                jour = int(match.group(1))
                mois_texte = match.group(2).lower()
                annee = int(match.group(3))
                mois = mois_map.get(mois_texte)
                if mois:
                    self.data['date_examen'] = f"{jour:02d}/{mois:02d}/{annee}"
                    self.data['examens']['date'] = f"{jour:02d}/{mois:02d}/{annee}"
                    break

        # Fallback regex
        if 'date_examen' not in self.data:
            mois_fr = (
                r'janvier|février|fevrier|mars|avril|mai|juin|'
                r'juillet|août|aout|septembre|octobre|novembre|décembre|decembre'
            )
            match = re.search(rf'(\d{{1,2}})\s+({mois_fr})\s+(\d{{4}})', text_content, re.IGNORECASE)
            if match:
                jour = int(match.group(1))
                mois_texte = match.group(2).lower()
                annee = int(match.group(3))
                mois = mois_map.get(mois_texte)
                if mois:
                    self.data['date_examen'] = f"{jour:02d}/{mois:02d}/{annee}"
                    self.data['examens']['date'] = f"{jour:02d}/{mois:02d}/{annee}"
            else:
                match = re.search(r'(\d{2}/\d{2}/\d{4})', text_content)
                if match:
                    self.data['date_examen'] = match.group(1)
                    self.data['examens']['date'] = match.group(1)

        # Type d'épreuve
        match = re.search(r'Examen\s+(vtc|taxi|vmdtr)\s*-\s*Épreuve\s+(écrite|pratique)', text_content, re.IGNORECASE)
        if match:
            self.data['epreuve_session'] = match.group(2).capitalize()

        # Lieu d'examen
        match = re.search(r'Lieu\s*:\s*([^\n]+)', text_content)
        if match:
            self.data['examens']['lieu'] = match.group(1).strip()

        # Convocation
        if 'Convocation disponible' in text_content or 'Télécharger la convocation' in text_content:
            self.data['examens']['convocation_disponible'] = True
            self.data['convocation'] = 'DISPONIBLE'
        elif "convocation d'examen disponible" in text_content.lower() or "convocation d\u2019examen disponible" in text_content.lower():
            self.data['examens']['convocation_disponible'] = True
            self.data['convocation'] = 'DISPONIBLE'
        else:
            self.data['examens']['convocation_disponible'] = False

        # Résultats d'examen (BS4 data attributes)
        result_btn = soup.select_one("[data-finalscore]")
        if result_btn:
            self.data['examens']['resultat'] = result_btn.get("data-finalscore", "")
        else:
            match = re.search(r'Résultat\s*:\s*(Admis|Ajourné|En attente)', text_content, re.IGNORECASE)
            if match:
                self.data['examens']['resultat'] = match.group(1)

    # ------------------------------------------------------------------
    #  Progression
    # ------------------------------------------------------------------

    def _parse_progression(self, text_content: str):
        """Extraction de la progression du dossier."""
        self.data['progression'] = {}

        progression_items = {
            'convocation': r"Convocation d'examen",
            'documents': r'Documents justificatifs',
            'paiement': r'Paiement',
            'infos_perso': r'Informations personnelles',
        }
        status_order = ['À VALIDER', 'EN ATTENTE', 'REFUSÉ', 'VALIDÉ']

        for key, label_pattern in progression_items.items():
            # Vérifier À VALIDER AVANT VALIDÉ pour éviter les faux positifs
            for status in status_order:
                pattern = rf'{label_pattern}.*?{re.escape(status)}'
                if re.search(pattern, text_content, re.DOTALL | re.IGNORECASE):
                    self.data['progression'][key] = status
                    break

        # Choix département/session
        match = re.search(
            r'Choix du département.*?(\d{2,3})\s*-\s*([A-Za-zÀ-ÿ\-\s]+).*?(À VALIDER|VALIDÉ)',
            text_content, re.DOTALL | re.IGNORECASE
        )
        if match:
            if 'departement' not in self.data:
                self.data['departement'] = match.group(1)
            self.data['region'] = match.group(2).strip()
            self.data['progression']['choix_session'] = match.group(3).upper()

        # Type d'examen sélectionné
        match = re.search(
            r"Type d'examen sélectionné.*?(VTC|Taxi|VMDTR).*?(À VALIDER|VALIDÉ)",
            text_content, re.DOTALL | re.IGNORECASE
        )
        if match:
            self.data['progression']['type_examen'] = match.group(2).upper()

        # Paiement détaillé (date, montant, mode, statut)
        paiement_match = re.search(
            r'Paiement\s*\n\s*(\d{2}/\d{2}/\d{4})\s*\n\s*(\d+[.,]\d{2})\s*€\s*-\s*Paiement par\s*(\w+)\s*\n\s*(VALIDÉ|EN ATTENTE|REFUSÉ)',
            text_content, re.IGNORECASE
        )
        if paiement_match:
            self.data['paiement_cma'] = {
                'date': paiement_match.group(1),
                'montant': float(paiement_match.group(2).replace(',', '.')),
                'mode': paiement_match.group(3),
                'statut': paiement_match.group(4).upper()
            }

        # Convocation (fallback si pas déjà set)
        if 'convocation' not in self.data:
            self.data['convocation'] = self.data['progression'].get('convocation', 'EN ATTENTE')

    # ------------------------------------------------------------------
    #  Documents
    # ------------------------------------------------------------------

    def _parse_documents(self, soup: BeautifulSoup, text_content: str):
        """Extraction du statut des documents."""
        STATUTS_PATTERN = r"(VALIDÉ|VALIDE|À VALIDER|A VALIDER|REFUSÉ|REFUSE|À CORRIGER|A CORRIGER)"

        documents_config = [
            {"nom": "Pièce d'identité", "doc_type": "identity"},
            {"nom": "Photo d'identité", "doc_type": "photo"},
            {"nom": "Signature", "doc_type": "signature"},
            {"nom": "Justificatif de domicile", "doc_type": "address"},
            {"nom": "Permis de conduire", "doc_type": "license"},
        ]

        self.data['documents'] = []

        # --- Méthode 1: BS4 div.document-item (fiable) ---
        bs4_docs = {}
        for doc_div in soup.select("div.document-item"):
            dt = doc_div.get("data-doctype", "")
            status_el = doc_div.select_one(".status-badge, span:last-child")
            statut = status_el.get_text(strip=True).upper() if status_el else ""
            if dt and statut:
                bs4_docs[dt] = statut

        # --- Méthode 2: BS4 refused documents avec motifs ---
        refused_by_bs4 = {}
        for file_item in soup.select("div.uploaded-file-item-full"):
            status_el = file_item.select_one("span.status-badge")
            if not status_el:
                continue
            statut = status_el.get_text(strip=True).upper()
            if statut not in ("REFUSÉ", "REFUSE", "À CORRIGER", "A CORRIGER"):
                continue
            reason_el = file_item.select_one("span.uploaded-file-reason")
            raison = reason_el.get_text(strip=True) if reason_el else ""
            # Find doc_type from parent modal
            doc_type = ""
            for parent in file_item.parents:
                parent_id = parent.get("id", "")
                for cfg in documents_config:
                    if cfg["doc_type"] in parent_id.lower():
                        doc_type = cfg["doc_type"]
                        break
                if doc_type:
                    break
            if doc_type and doc_type not in refused_by_bs4:
                refused_by_bs4[doc_type] = raison

        # --- Build documents list ---
        for doc_config in documents_config:
            doc_info = {'nom': doc_config['nom'], 'statut': 'INCONNU', 'motif_refus': None}

            dt = doc_config['doc_type']

            # Try BS4 first
            if dt in bs4_docs:
                raw_statut = bs4_docs[dt]
                if raw_statut in ['VALIDÉ', 'VALIDE']:
                    doc_info['statut'] = 'VALIDÉ'
                elif raw_statut in ['À VALIDER', 'A VALIDER']:
                    doc_info['statut'] = 'À VALIDER'
                elif raw_statut in ['REFUSÉ', 'REFUSE', 'À CORRIGER', 'A CORRIGER']:
                    doc_info['statut'] = 'REFUSÉ'
                    doc_info['motif_refus'] = refused_by_bs4.get(dt) or self._extract_refusal_reason(text_content, doc_config['nom'])
            else:
                # Fallback: regex sur texte
                patterns = [
                    rf"{re.escape(doc_config['nom'])}.*?{STATUTS_PATTERN}",
                ]
                for pattern in patterns:
                    match = re.search(pattern, text_content, re.IGNORECASE | re.DOTALL)
                    if match:
                        statut = match.group(1).upper()
                        if statut in ['VALIDÉ', 'VALIDE']:
                            doc_info['statut'] = 'VALIDÉ'
                        elif statut in ['À VALIDER', 'A VALIDER']:
                            doc_info['statut'] = 'À VALIDER'
                        elif statut in ['REFUSÉ', 'REFUSE', 'À CORRIGER', 'A CORRIGER']:
                            doc_info['statut'] = 'REFUSÉ'
                            doc_info['motif_refus'] = self._extract_refusal_reason(text_content, doc_config['nom'])
                        break

            self.data['documents'].append(doc_info)

        # Documents facultatifs
        self.data['documents_facultatifs'] = []
        if "Pièce d'identité - Justificatif FACULTATIF" in text_content:
            self.data['documents_facultatifs'].append({'nom': "Pièce d'identité - Justificatif FACULTATIF", 'statut': 'OPTIONNEL'})
        if 'Permis de conduire - Justificatif FACULTATIF' in text_content:
            self.data['documents_facultatifs'].append({'nom': "Permis de conduire - Justificatif FACULTATIF", 'statut': 'OPTIONNEL'})

        # FALLBACK: Si dossier "Valide", tous les docs INCONNU → VALIDÉ
        statut_dossier = self.data.get('statut_dossier', '').lower()
        docs_inconnus = sum(1 for d in self.data['documents'] if d['statut'] == 'INCONNU')
        if statut_dossier == 'valide' and docs_inconnus > 0:
            for doc in self.data['documents']:
                if doc['statut'] == 'INCONNU':
                    doc['statut'] = 'VALIDÉ'

        # Calculer statut global
        statuts = [d['statut'] for d in self.data['documents']]
        if all(s == 'VALIDÉ' for s in statuts):
            self.data['statut_documents'] = 'VALIDÉ'
        elif any(s == 'REFUSÉ' for s in statuts):
            self.data['statut_documents'] = 'REFUSÉ'
        elif any(s == 'À VALIDER' for s in statuts):
            self.data['statut_documents'] = 'À VALIDER'
        elif any(s == 'INCONNU' for s in statuts):
            self.data['statut_documents'] = 'INCONNU'
        else:
            self.data['statut_documents'] = 'EN COURS'

        validés = sum(1 for s in statuts if s == 'VALIDÉ')
        self.data['documents_valides'] = f"{validés}/{len(statuts)}"

        # Documents en attente / refusés
        self.data['documents_en_attente'] = []
        self.data['documents_refuses'] = []
        self.data['pieces_refusees_details'] = []

        for doc in self.data['documents']:
            if doc['statut'] == 'À VALIDER':
                self.data['documents_en_attente'].append(doc['nom'])
            elif doc['statut'] == 'REFUSÉ':
                self.data['documents_refuses'].append(doc['nom'])
                self.data['pieces_refusees_details'].append({
                    'nom': doc['nom'],
                    'motif': doc.get('motif_refus', 'Motif non précisé'),
                    'solution': self._get_solution_for_document(doc['nom'])
                })
                if 'document_problematique' not in self.data:
                    self.data['document_problematique'] = doc['nom']
                    self.data['document_problematique_statut'] = 'REFUSÉ'
                    self.data['document_problematique_motif'] = doc.get('motif_refus')

        self.data['action_candidat_requise'] = len(self.data['documents_refuses']) > 0

    # ------------------------------------------------------------------
    #  Actions requises
    # ------------------------------------------------------------------

    def _parse_actions(self, text_content: str):
        """Extraction des actions requises."""
        self.data['actions_requises'] = []
        self.data['_motifs_refus_overview'] = {}

        if 'Reçu de paiement disponible' in text_content:
            self.data['actions_requises'].append({
                'type': 'recu_disponible',
                'description': 'Reçu de paiement disponible'
            })

        if 'Photo non conforme' in text_content or ('photo' in text_content.lower() and 'à valider' in text_content.lower()):
            self.data['actions_requises'].append({
                'type': 'photo_requise',
                'description': "Photo d'identité à mettre à jour"
            })

        documents_actions = [
            ("Justificatif de domicile", r"Justificatif de domicile[^.]*?non conforme"),
            ("Photo d'identité", r"Photo d'identité[^.]*?non conforme"),
            ("Pièce d'identité", r"Pièce d'identité[^.]*?non conforme"),
            ("Signature", r"Signature[^.]*?non conforme"),
            ("Permis de conduire", r"Permis de conduire[^.]*?non conforme"),
        ]

        for doc_name, pattern in documents_actions:
            if re.search(pattern, text_content, re.IGNORECASE):
                motif_patterns = [
                    rf"{pattern}.*?(?:Le document|Raison\s*:)\s*([^\n]{{10,300}})",
                    rf"{pattern}\s*\n\s*([A-Za-zÀ-ÿ][^\n]{{10,200}})",
                ]
                motif = None
                for motif_pattern in motif_patterns:
                    motif_match = re.search(motif_pattern, text_content, re.IGNORECASE | re.DOTALL)
                    if motif_match:
                        motif = re.sub(r'\s+', ' ', motif_match.group(1).strip())
                        break

                self.data['actions_requises'].append({
                    'type': 'document_non_conforme',
                    'document': doc_name,
                    'description': f'{doc_name} non conforme',
                    'motif': motif or 'Document non conforme'
                })
                self.data['_motifs_refus_overview'][doc_name] = motif or 'Document non conforme'

        # Historique étapes
        self.data['historique_etapes'] = []
        etapes = [
            'En cours de composition', 'En attente du paiement',
            "En attente d'instruction des pièces", 'Incomplet', 'Valide'
        ]
        for etape in etapes:
            if etape.lower() in text_content.lower():
                self.data['historique_etapes'].append(etape)

    # ------------------------------------------------------------------
    #  Paiements
    # ------------------------------------------------------------------

    def _parse_paiements(self, text_content: str):
        """Extraction historique paiements."""
        self.data.setdefault('historique_paiements', [])

        # Pattern complet: "00039634   15/01/2026   Examen complet   241,00 €   VALIDÉ"
        pattern = r'(\d{8})[\s\n]+(\d{2}/\d{2}/\d{4})[\s\n]+(.+?)[\s\n]+(\d+[.,]\d{2})\s*€[\s\n]*(VALIDÉ|REFUSÉ|EN ATTENTE)'
        matches = re.findall(pattern, text_content, re.IGNORECASE | re.DOTALL)
        for match in matches:
            self.data['historique_paiements'].append({
                'num_dossier': match[0],
                'date': match[1],
                'description': match[2].strip(),
                'montant': float(match[3].replace(',', '.')),
                'statut': match[4].upper().replace(' ', '_')
            })

        # Pattern en attente: "00039617\t--\t..."
        if not self.data['historique_paiements']:
            pattern_pending = r'(\d{8})\t--\t.*?--\s*€\t(EN ATTENTE|VALIDÉ|REFUSÉ)'
            matches = re.findall(pattern_pending, text_content, re.DOTALL | re.IGNORECASE)
            for match in matches:
                desc_match = re.search(r'Examen[^\t]+', text_content)
                description = desc_match.group(0).strip() if desc_match else 'Examen'
                self.data['historique_paiements'].append({
                    'num_dossier': match[0],
                    'date': None,
                    'description': description,
                    'montant': None,
                    'statut': 'EN_ATTENTE' if 'attente' in match[1].lower() else match[1].upper()
                })

        # Fallback simple
        if not self.data['historique_paiements']:
            has_num = re.search(r'\d{8}', text_content)
            has_attente = re.search(r'EN.ATTENTE', text_content, re.IGNORECASE)
            if has_num and has_attente:
                num_match = re.search(r'(\d{8})', text_content)
                self.data['historique_paiements'].append({
                    'num_dossier': num_match.group(1) if num_match else None,
                    'date': None, 'description': 'Examen',
                    'montant': None, 'statut': 'EN_ATTENTE'
                })
            elif re.search(r'(\d+[.,]\d{2})\s*€', text_content):
                match = re.search(r'(\d+[.,]\d{2})\s*€', text_content)
                self.data['historique_paiements'].append({
                    'montant': float(match.group(1).replace(',', '.')),
                    'statut': 'VALIDÉ' if 'VALIDÉ' in text_content.upper() else 'INCONNU'
                })

    # ------------------------------------------------------------------
    #  Compte
    # ------------------------------------------------------------------

    def _parse_compte(self, text_content: str):
        """Extraction informations du compte."""
        self.data['compte'] = {}

        if 'Homme' in text_content:
            self.data['compte']['genre'] = 'Homme'
        elif 'Femme' in text_content:
            self.data['compte']['genre'] = 'Femme'

        patterns = {
            'prenom': r'Prénom\(?s?\)?\s*\n\s*([A-Za-zÀ-ÿ\s\-]+)',
            'nom': r'Nom\s*\n\s*([A-Za-zÀ-ÿ\s\-]+)',
            'date_naissance': r'Date de naissance\s*\n\s*(\d{2}/\d{2}/\d{4})',
            'lieu_naissance': r'Lieu de naissance\s*\n\s*([A-Za-zÀ-ÿ\s\-]+)',
            'adresse': r'Adresse de domicile\s*\n\s*([^\n]+)',
            'code_postal': r'Code postal\s*\n\s*(\d{5})',
            'ville': r'Ville\s*\n\s*([A-Za-zÀ-ÿ\s\-]+)',
            'email': r'Email\s*\n\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            'telephone': r'Téléphone\s*\n\s*([0-9\s\+]+)',
        }

        for key, pattern in patterns.items():
            match = re.search(pattern, text_content)
            if match:
                self.data['compte'][key] = match.group(1).strip()

    # ------------------------------------------------------------------
    #  Messages
    # ------------------------------------------------------------------

    def _parse_messages(self, text_content: str):
        """Extraction messages avec la CMA."""
        self.data['messages'] = {'nombre': 0, 'liste': []}

        match = re.search(r'(\d+)\s*nouveau[sx]?', text_content, re.IGNORECASE)
        if match:
            self.data['messages']['nombre'] = int(match.group(1))

        messages_pattern = r'(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})\s*\n\s*(CMA|Candidat)\s*\n\s*([^\n]+)'
        matches = re.findall(messages_pattern, text_content, re.IGNORECASE)
        for match in matches:
            self.data['messages']['liste'].append({
                'date': match[0],
                'expediteur': match[1],
                'contenu': match[2].strip()
            })

    # ------------------------------------------------------------------
    #  Helpers (conservés de v4)
    # ------------------------------------------------------------------

    def _extract_refusal_reason(self, text_content: str, doc_name: str) -> Optional[str]:
        """Extrait le motif de refus d'un document."""
        motifs_overview = self.data.get('_motifs_refus_overview', {})
        if doc_name in motifs_overview:
            return motifs_overview[doc_name]

        refusal_patterns = [
            rf"Raison\s*:\s*([^\n]{{10,300}})",
            rf"{re.escape(doc_name)}.*?(?:REFUS[ÉE]?|À CORRIGER|A CORRIGER)\s*[:\-]?\s*([^\n]{{10,200}})",
            rf"{re.escape(doc_name)}.*?(?:REFUS[ÉE]?|À CORRIGER|A CORRIGER).*?\n\s*([A-Za-zÀ-ÿ][^\n]{{10,200}})",
            rf"{re.escape(doc_name)}.*?(?:REFUS[ÉE]?|À CORRIGER|A CORRIGER).*?(?:Commentaire|Motif|Raison)\s*[:\-]?\s*([^\n]{{5,200}})",
            rf"{re.escape(doc_name)}.*?Le document fourni\s+([^\n]{{10,200}})",
        ]

        for pattern in refusal_patterns:
            match = re.search(pattern, text_content, re.IGNORECASE | re.DOTALL)
            if match:
                motif = re.sub(r'\s+', ' ', match.group(1).strip())
                if len(motif) > 5 and not motif.upper().startswith('VALID'):
                    return motif

        default_reasons = {
            "Pièce d'identité": "Document non conforme ou illisible - veuillez fournir une copie lisible recto/verso",
            "Photo d'identité": "Photo non conforme aux normes (fond non uni, visage non centré, ou qualité insuffisante)",
            "Signature": "Signature non manuscrite ou non conforme - une signature manuscrite scannée est requise",
            "Justificatif de domicile": "Document de plus de 3 mois ou non conforme - veuillez fournir un justificatif récent",
            "Permis de conduire": "Permis non valide ou illisible - veuillez fournir une copie lisible recto/verso",
        }

        return default_reasons.get(doc_name, "Motif non précisé par la CMA")

    def _get_solution_for_document(self, doc_name: str) -> str:
        """Retourne la solution pour corriger un document refusé."""
        solutions = {
            "Pièce d'identité": (
                "Scannez ou photographiez votre pièce d'identité (carte d'identité ou passeport) "
                "RECTO et VERSO sur un fond uni et bien éclairé. "
                "Assurez-vous que le document est lisible et non coupé."
            ),
            "Photo d'identité": (
                "Fournissez une photo d'identité récente aux normes officielles : "
                "fond uni clair, visage de face bien centré, expression neutre, "
                "sans lunettes si possible. Format recommandé : 35x45mm minimum."
            ),
            "Signature": (
                "Signez sur une feuille blanche avec un stylo noir, "
                "puis scannez ou photographiez votre signature. "
                "La signature doit être manuscrite (pas de signature électronique)."
            ),
            "Justificatif de domicile": (
                "Fournissez un justificatif de domicile de moins de 3 mois à votre nom : "
                "facture d'électricité, de gaz, d'eau, de téléphone fixe ou mobile, "
                "ou avis d'imposition. Le document doit être complet et lisible."
            ),
            "Permis de conduire": (
                "Scannez ou photographiez votre permis de conduire "
                "RECTO et VERSO sur un fond uni. "
                "Le permis doit être en cours de validité et lisible."
            ),
        }
        return solutions.get(doc_name, "Veuillez nous fournir un nouveau document conforme.")


# ======================================================================
#  Public API — signature identique à v4
# ======================================================================

def extract_exament3p_sync(identifiant: str, password: str, max_retries: int = MAX_RETRIES,
                           num_dossier: Optional[str] = None) -> Dict:
    """
    Fonction synchrone pour extraire les donnees ExamenT3P.

    Args:
        identifiant: Email du candidat
        password: Mot de passe ExamenT3P
        max_retries: Nombre maximum de tentatives
        num_dossier: Numero de dossier attendu (pour multi-dossier/reinscription)

    Returns:
        Dictionnaire avec les donnees extraites (compatible v4)
    """
    extractor = ExamT3PHttpClient(identifiant, password, max_retries, num_dossier=num_dossier)
    return extractor.extract_all()
