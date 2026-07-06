"""Internal PlanBot API client for B2B Relations entreprises drafts."""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import settings


logger = logging.getLogger(__name__)


class PlanBotAPIClient:
    """Small client for the read-only PlanBot API exposed by Edusign."""

    def __init__(self, base_url: str | None = None, secret: str | None = None):
        self.base_url = (base_url or settings.planbot_api_url or "").rstrip("/")
        self.secret = secret or settings.planbot_api_secret or ""
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.secret)

    def check_availability(self, payload: dict[str, Any], action: str = "full") -> dict[str, Any]:
        """Run a read-only PlanBot availability query.

        Returns a normalized error dict instead of raising for operational failures,
        so the workflow can still create a safe draft asking for manual completion.
        """
        if not self.configured:
            return {
                "status": "skipped",
                "error": "planbot_api_not_configured",
                "message": "PlanBot API non configurée",
            }

        url = f"{self.base_url}/internal/planbot/availability"
        try:
            response = self.session.post(
                url,
                json={"action": action, "payload": payload},
                headers={"X-PlanBot-Secret": self.secret},
                timeout=90,
            )
            if response.status_code >= 400:
                logger.warning("PlanBot API error %s: %s", response.status_code, response.text[:500])
                return {
                    "status": "error",
                    "error": f"planbot_api_http_{response.status_code}",
                    "message": response.text[:500],
                }
            data = response.json()
            return data.get("result") or data
        except Exception as exc:
            logger.warning("PlanBot API unavailable: %s", exc)
            return {
                "status": "error",
                "error": "planbot_api_unavailable",
                "message": str(exc),
            }
