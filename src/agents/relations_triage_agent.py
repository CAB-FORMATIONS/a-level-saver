"""Triage and structured extraction for Relations entreprises tickets."""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

import anthropic

from config import settings
from src.constants.models import MODEL_EXTRACTION


logger = logging.getLogger(__name__)


RELATIONS_INTENTS = {
    "DEMANDE_DEVIS_FORMATION",
    "DEMANDE_DISPONIBILITE_SESSION",
    "INSCRIPTION_CANDIDATS",
    "COMMANDE_FORMALOGISTICS",
    "ANNULATION_REPORT_ABSENCE",
    "CONVENTION_CONTRAT_DOSSIER",
    "BON_DE_COMMANDE",
    "CONVOCATION_CONFIRMATION",
    "ATTESTATION_FIN_FORMATION",
    "DOCUMENTS_SIGNATURES_MANQUANTS",
    "FACTURE_FINANCEMENT_PEC",
    "BILAN_FORMATEUR",
    "PROSPECTION_PARTENARIAT",
    "CV_PROFILS_INTERVENANTS",
    "AUTRE_A_QUALIFIER",
}


SYSTEM_PROMPT = """Tu analyses des tickets email B2B pour CAB Formations, departement Relations entreprises.

Objectif: produire un JSON strict pour creer un brouillon de reponse, jamais envoyer directement.

Intentions possibles:
- DEMANDE_DEVIS_FORMATION: demande de devis, prix, proposition commerciale.
- DEMANDE_DISPONIBILITE_SESSION: demande de places, dates, disponibilites.
- INSCRIPTION_CANDIDATS: inscription ou ajout de candidat(s) a une session.
- COMMANDE_FORMALOGISTICS: nouvelle commande ou commande plateforme Formalogistics.
- ANNULATION_REPORT_ABSENCE: annulation, report, absence, retrait candidat.
- CONVENTION_CONTRAT_DOSSIER: convention, contrat, dossier de formation.
- BON_DE_COMMANDE: BDC, bon de commande, commande client.
- CONVOCATION_CONFIRMATION: convocation, confirmation participation.
- ATTESTATION_FIN_FORMATION: attestation, documents/certificat fin de formation/session.
- DOCUMENTS_SIGNATURES_MANQUANTS: documents manquants, signature, liste stagiaires.
- FACTURE_FINANCEMENT_PEC: facture, reglement, OPCO, prise en charge.
- BILAN_FORMATEUR: bilan formateur/pedagogique.
- PROSPECTION_PARTENARIAT: RDV, partenariat, presentation commerciale.
- CV_PROFILS_INTERVENANTS: CV formateur/intervenant transmis ou mis a jour.
- AUTRE_A_QUALIFIER: si aucun cas clair.

Actions possibles:
- DRAFT: brouillon client possible.
- IGNORE_NOISE: spam, newsletter, no-reply, notification outil, message automatique sans valeur metier.
- ROUTE_COMPTA: litige facture, paiement, relance comptable qui doit etre traite par comptabilite.
- ROUTE_HUMAN: demande sensible, reclamation qualite grave, ambiguite forte, donnees insuffisantes pour repondre utilement.

Extraction attendue:
- formation_type: ex CACES R489, R486, SST, habilitation electrique, AIPR.
- centre: ex Tremblay, Herblay, Villabe, Venissieux, Bois d'Arcy, Seclin, Roissy.
- start_date/end_date: format YYYY-MM-DD si une periode/date est clairement demandee.
- nb_candidates: nombre entier si present, sinon 1 si la demande parle d'un seul candidat, sinon null.
- categories: liste ex ["1B", "3", "5", "A", "B"].
- nb_categories: nombre si categories non detaillees mais nombre present.
- type_ir: "initial", "recyclage" ou "".
- financement: "B2B", "B2C" ou "".
- nombre_jours_souhaites: duree demandee en jours si deduisible explicitement de la periode ou du texte.
- missing_fields: infos manquantes pour proposer une session fiable.

Regles:
- Ne devine pas les prix.
- Pour CACES, si categories absentes, ajoute "categories" dans missing_fields.
- Pour proposer des sessions, il faut au minimum formation_type, centre, start_date, end_date, nb_candidates.
- Si la date est floue ("semaine prochaine", "mai") laisse start_date/end_date null et mets "dates" dans missing_fields.
- Reponds uniquement avec un objet JSON valide, sans markdown."""


class RelationsTriageAgent:
    """LLM-backed B2B triage with deterministic fallback."""

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def process(self, data: dict[str, Any]) -> dict[str, Any]:
        subject = data.get("subject") or ""
        message = data.get("message") or ""
        email = data.get("email") or ""
        crm_context = data.get("crm_context") or {}

        prompt = json.dumps({
            "subject": subject,
            "email": email,
            "message": message[:5000],
            "crm_context": {
                "classification": crm_context.get("classification"),
                "contact_name": crm_context.get("contact_name"),
                "account_name": crm_context.get("account_name"),
            },
        }, ensure_ascii=False)

        try:
            response = self.client.messages.create(
                model=MODEL_EXTRACTION,
                max_tokens=1600,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            parsed = self._parse_json(text)
            return self._normalize(parsed, subject, message, email)
        except Exception as exc:
            logger.warning("Relations triage AI failed, using fallback: %s", exc)
            return self._fallback(subject, message, email)

    def _parse_json(self, text: str) -> dict[str, Any]:
        if text.startswith("{"):
            return json.loads(text)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("No JSON object returned")
        return json.loads(match.group(0))

    def _normalize(self, result: dict[str, Any], subject: str, message: str, email: str) -> dict[str, Any]:
        action = str(result.get("action") or "DRAFT").upper()
        if action not in {"DRAFT", "IGNORE_NOISE", "ROUTE_COMPTA", "ROUTE_HUMAN"}:
            action = "DRAFT"

        intent = str(result.get("intent") or "AUTRE_A_QUALIFIER").upper()
        if intent not in RELATIONS_INTENTS:
            intent = "AUTRE_A_QUALIFIER"

        deterministic_intent = self._detect_business_intent(subject, message)
        if self._is_obvious_auto_reply(subject, message):
            action = "IGNORE_NOISE"
        elif deterministic_intent:
            intent = deterministic_intent
            # Do not let the LLM discard obvious business mail as noise.
            if action in {"IGNORE_NOISE", "ROUTE_HUMAN"}:
                action = "DRAFT"

        extracted = result.get("extracted") or {}
        if not isinstance(extracted, dict):
            extracted = {}
        extracted = self._enrich_extracted(extracted, subject, message)
        missing = result.get("missing_fields") or extracted.get("missing_fields") or []
        if isinstance(missing, str):
            missing = [missing]

        extracted.setdefault("formation_type", "")
        extracted.setdefault("centre", "")
        extracted.setdefault("start_date", None)
        extracted.setdefault("end_date", None)
        extracted.setdefault("nb_candidates", None)
        extracted.setdefault("categories", [])
        extracted.setdefault("nb_categories", None)
        extracted.setdefault("type_ir", "")
        extracted.setdefault("financement", "B2B")
        extracted.setdefault("nombre_jours_souhaites", None)

        return {
            "action": action,
            "intent": intent,
            "confidence": float(result.get("confidence") or 0.6),
            "reason": result.get("reason") or "Analyse Relations entreprises",
            "extracted": extracted,
            "missing_fields": list(dict.fromkeys(str(item) for item in missing if item)),
            "subject": subject,
            "email": email,
        }

    def _detect_business_intent(self, subject: str, message: str) -> str | None:
        subject_text = self._norm(subject)
        message_text = self._norm(message[:1200])
        text = self._norm(f"{subject}\n{message[:1200]}")

        # Strong latest-message signals first. The subject can be old in a reply
        # thread, while the latest customer body carries the actual intent.
        if any(part in message_text for part in ["annuler cette demande", "annulation", "annuler", "absence", "absences", "report", "positionner plus tard", "retrait/ajout de candidat"]):
            return "ANNULATION_REPORT_ABSENCE"
        if any(part in message_text for part in ["convention dument signee", "convention signee", "dossier formateur", "documents certalis", "feuille d'emargement", "feuille d’emargement", "avis stagiaire", "dossier de formation"]):
            return "CONVENTION_CONTRAT_DOSSIER"
        if any(part in message_text for part in ["ci-joint differents bdc", "ci joint differents bdc", "bon de commande", "bdc"]):
            return "BON_DE_COMMANDE"
        if any(part in message_text for part in ["cv mis a jour", "cv actualise", "cv actualisé"]):
            return "CV_PROFILS_INTERVENANTS"

        # Strong subject-level signals after the latest body.
        if any(part in subject_text for part in ["reponse automatique", "absence du bureau", "out of office"]):
            return None
        if any(part in subject_text for part in ["cv mis a jour", "cv actualise", "cv actualisé"]):
            return "CV_PROFILS_INTERVENANTS"
        if any(part in subject_text for part in ["bilan formateur", "bilan pedagogique"]):
            return "BILAN_FORMATEUR"
        if any(part in subject_text for part in ["declaration des absences", "absence", "absences", "annulation", "report"]):
            return "ANNULATION_REPORT_ABSENCE"
        if any(part in subject_text for part in ["bon de commande", "bdc"]):
            return "BON_DE_COMMANDE"
        if "nouvelle commande" in subject_text:
            return "COMMANDE_FORMALOGISTICS"
        if any(part in subject_text for part in ["formation a programmer", "demande de formation", "formation confirmee", "devis"]):
            return "DEMANDE_DEVIS_FORMATION"
        if "formation" in subject_text and any(part in subject_text for part in ["caces", "sst", "habilitation", "aipr", "nacelle", "chariot"]):
            return "DEMANDE_DISPONIBILITE_SESSION"

        if any(part in text for part in ["bilan formateur", "bilan pedagogique"]):
            return "BILAN_FORMATEUR"
        if any(part in text for part in ["annulation", "absence", "absences", "report formation", "retrait/ajout de candidat"]):
            return "ANNULATION_REPORT_ABSENCE"
        if any(part in text for part in ["bon de commande", "bdc ", " bdc", "notre commande", "commande n"]):
            return "BON_DE_COMMANDE"
        if "nouvelle commande" in text:
            return "COMMANDE_FORMALOGISTICS"
        if any(part in text for part in ["demande de devis", "devis", "formation a programmer", "demande de formation", "formation confirmee", "formation confirmee", "demande formation"]):
            return "DEMANDE_DEVIS_FORMATION"
        if any(part in text for part in ["convention", "contrat", "dossier de formation"]):
            return "CONVENTION_CONTRAT_DOSSIER"
        if "convocation" in text:
            return "CONVOCATION_CONFIRMATION"
        if any(part in text for part in ["attestation", "fin de formation", "fin de session", "certificat"]):
            return "ATTESTATION_FIN_FORMATION"
        if any(part in text for part in ["documents manquants", "invite a signer", "signature", "documents de formation", "liste des agents"]):
            return "DOCUMENTS_SIGNATURES_MANQUANTS"
        if any(part in text for part in ["facture", "prise en charge", "opco", "reglement", "impayee"]):
            return "FACTURE_FINANCEMENT_PEC"
        if "cv mis a jour" in text:
            return "CV_PROFILS_INTERVENANTS"
        if "formation" in text and any(part in text for part in ["caces", "sst", "habilitation", "aipr", "nacelle", "chariot"]):
            return "DEMANDE_DISPONIBILITE_SESSION"
        return None

    def _enrich_extracted(self, extracted: dict[str, Any], subject: str, message: str) -> dict[str, Any]:
        text = self._norm(f"{subject}\n{message[:1200]}")
        raw = f"{subject}\n{message[:1200]}"

        if not extracted.get("formation_type"):
            formation = self._extract_formation(raw)
            if formation:
                extracted["formation_type"] = formation
        if not extracted.get("categories"):
            categories = self._extract_categories(raw)
            if categories:
                extracted["categories"] = categories
                extracted.setdefault("nb_categories", len(categories))
        if not extracted.get("type_ir"):
            if "recyclage" in text:
                extracted["type_ir"] = "recyclage"
            elif "initial" in text:
                extracted["type_ir"] = "initial"
        if not extracted.get("start_date"):
            date_value = self._extract_first_date(raw)
            if date_value:
                extracted["start_date"] = date_value
                extracted.setdefault("end_date", date_value)
        return extracted

    def _extract_formation(self, raw: str) -> str:
        text = self._norm(raw)
        caces_match = re.search(r"\bR\s?(48[24569]|490)\b", raw, flags=re.IGNORECASE)
        if caces_match:
            return f"CACES R{caces_match.group(1)}"
        if "caces" in text and "chariot" in text:
            return "CACES Chariot"
        if "caces" in text and "nacelle" in text:
            return "CACES Nacelle"
        if "caces" in text:
            return "CACES"
        if "sst" in text:
            return "SST"
        if "habilitation" in text and "elect" in text:
            return "Habilitation electrique"
        if "agent de manutention" in text or "bagagiste" in text:
            return "Agent de manutention"
        if "aipr" in text:
            return "AIPR"
        return ""

    def _extract_categories(self, raw: str) -> list[str]:
        candidates: list[str] = []
        category_zone = ""
        match = re.search(r"cat(?:e|é)gor(?:ie|ies)?\s*[:\-]?\s*([^\n\r]+)", raw, flags=re.IGNORECASE)
        if match:
            category_zone = match.group(1)[:80]
        else:
            # Only infer categories without the word "categorie" for common
            # subject formats like "CACES R486 A Initial". Do not scan the
            # whole subject, otherwise dates like "4 au 06-05-26" become cat 4.
            match = re.search(
                r"R\s?48[24569]\s+([A-G1-7][AB]?(?:[\s,/+\-]+[A-G1-7][AB]?)*)\s+(?:initial|recyclage|intermediaire|intermédiaire)",
                raw,
                flags=re.IGNORECASE,
            )
            if not match:
                return []
            category_zone = match.group(1)[:40]
        for token in re.findall(r"\b(?:[1-7][AB]?|[A-G]|1B|2B|2A)\b", category_zone, flags=re.IGNORECASE):
            clean = token.upper()
            if clean not in candidates:
                candidates.append(clean)
        return candidates[:6]

    def _extract_first_date(self, raw: str) -> str | None:
        match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", raw)
        if not match:
            return None
        day, month, year = match.groups()
        if len(year) == 2:
            year = f"20{year}"
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    def _is_obvious_auto_reply(self, subject: str, message: str) -> bool:
        text = self._norm(f"{subject}\n{message[:800]}")
        return any(part in text for part in ["reponse automatique", "absence du bureau", "out of office"])

    def _norm(self, value: str) -> str:
        normalized = unicodedata.normalize("NFD", value or "")
        without_accents = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
        return re.sub(r"\s+", " ", without_accents.lower()).strip()

    def _fallback(self, subject: str, message: str, email: str) -> dict[str, Any]:
        text = f"{subject}\n{message}".lower()
        action = "DRAFT"
        intent = "AUTRE_A_QUALIFIER"

        if any(part in email.lower() for part in ["no-reply", "noreply", "notification", "newsletter"]):
            action = "IGNORE_NOISE"
        elif any(word in text for word in ["facture", "règlement", "reglement", "impay"]):
            intent = "FACTURE_FINANCEMENT_PEC"
            action = "ROUTE_COMPTA" if "impay" in text or "mise en demeure" in text else "DRAFT"
        elif "devis" in text or "demande de formation" in text:
            intent = "DEMANDE_DEVIS_FORMATION"
        elif "nouvelle commande" in text:
            intent = "COMMANDE_FORMALOGISTICS"
        elif any(word in text for word in ["annulation", "absence", "report"]):
            intent = "ANNULATION_REPORT_ABSENCE"
        elif any(word in text for word in ["convention", "contrat", "dossier de formation"]):
            intent = "CONVENTION_CONTRAT_DOSSIER"
        elif "bon de commande" in text or "bdc" in text:
            intent = "BON_DE_COMMANDE"
        elif "convocation" in text:
            intent = "CONVOCATION_CONFIRMATION"
        elif "attestation" in text or "fin de formation" in text:
            intent = "ATTESTATION_FIN_FORMATION"

        return {
            "action": action,
            "intent": intent,
            "confidence": 0.35,
            "reason": "Fallback keyword triage",
            "extracted": {
                "formation_type": "",
                "centre": "",
                "start_date": None,
                "end_date": None,
                "nb_candidates": None,
                "categories": [],
                "nb_categories": None,
                "type_ir": "",
                "financement": "B2B",
                "nombre_jours_souhaites": None,
            },
            "missing_fields": [],
            "subject": subject,
            "email": email,
        }
