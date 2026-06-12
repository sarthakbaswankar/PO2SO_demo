"""
oic_client.py
=============
Triggers the Oracle Integration Cloud (OIC) integration — exactly like calling
its REST endpoint in Postman: one HTTP request to a full URL, with Basic Auth.

Hitting the trigger URL runs the integration that reads the email inbox and
pushes PO PDFs into the POPDFS/ bucket folder, which this pipeline processes.

Config (see config.OICConfig):
  trigger_url   full integration REST URL (the URL you use in Postman)
  method        HTTP method (GET, as in the Postman screenshot)
  oic_username  Basic Auth username
  oic_password  Basic Auth password
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from config import OICConfig

log = logging.getLogger(__name__)


class OICError(Exception):
    """Raised when triggering the OIC integration fails."""


class OICClient:
    def __init__(self, cfg: OICConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        # HTTP Basic Auth — same as the "Basic Auth" tab in Postman.
        self._session.auth = (cfg.oic_username, cfg.oic_password)
        self._session.headers.update({"Accept": "*/*"})

    def trigger(self) -> dict[str, Any]:
        """Call the integration's trigger URL. Returns a result dict
        {status_code, message, data}. Raises OICError on failure."""
        url = self.cfg.trigger_url
        method = (self.cfg.method or "GET").upper()

        if not url or "CHANGE-ME" in url:
            raise OICError("OIC trigger URL is not configured — set OIC_TRIGGER_URL.")
        if not self.cfg.oic_username or not self.cfg.oic_password:
            raise OICError(
                "OIC credentials are not configured — set OIC_USERNAME and "
                "OIC_PASSWORD (Basic Auth)."
            )

        log.info("OIC trigger: %s %s", method, url)
        try:
            resp = self._session.request(
                method, url, timeout=self.cfg.request_timeout
            )
        except requests.RequestException as exc:
            raise OICError(f"Network error calling OIC: {exc}") from exc

        if resp.status_code == 401:
            raise OICError(
                "OIC authentication failed (HTTP 401) — check OIC_USERNAME / "
                "OIC_PASSWORD."
            )
        if not resp.ok:
            log.error("OIC HTTP %d for %s: %s", resp.status_code, url, resp.text[:1000])
            raise OICError(f"OIC returned HTTP {resp.status_code}: {resp.text[:300]}")

        # Response body may be JSON or plain text/XML — keep whatever comes back.
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text

        log.info("OIC trigger OK (HTTP %s, %d bytes)", resp.status_code, len(resp.content))
        return {
            "status_code": resp.status_code,
            "message": "Inbox integration triggered — POs are being pulled from "
                       "the inbox into the pipeline.",
            "data": data,
        }

    # ── Error-notification integration ───────────────────────────────────────
    def trigger_error_notification(
        self, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call the PO2SO_ERR integration to send an error notification.

        Invoked AFTER a PDF has been moved to the Object Storage error/ folder.
        Same OIC host + Basic Auth as the inbox trigger; only the flow path
        differs. Best-effort: never raises — a failed notification must not mask
        the original processing error. Returns a small result dict.

        `context` (PO number, customer, file name, error) is sent as query
        params if present, so the integration can include details in the email.
        """
        if not self.cfg.error_notification_enabled:
            log.info("OIC error-notification disabled (OIC_ERROR_NOTIFICATION_ENABLED=false).")
            return {"sent": False, "skipped": True, "reason": "disabled"}

        url = self.cfg.error_trigger_url
        method = (self.cfg.error_method or "GET").upper()
        if not url or "CHANGE-ME" in url:
            log.warning("OIC error-notification URL not configured — skipping.")
            return {"sent": False, "skipped": True, "reason": "not configured"}
        if not self.cfg.oic_username or not self.cfg.oic_password:
            log.warning("OIC credentials missing — skipping error notification.")
            return {"sent": False, "skipped": True, "reason": "no credentials"}

        params = {k: str(v) for k, v in (context or {}).items() if v}
        log.info("OIC error-notification: %s %s (context=%s)", method, url, params or "<none>")
        try:
            resp = self._session.request(
                method, url, params=params or None, timeout=self.cfg.request_timeout
            )
        except requests.RequestException as exc:
            log.warning("OIC error-notification network error (ignored): %s", exc)
            return {"sent": False, "error": str(exc)}

        if not resp.ok:
            log.warning("OIC error-notification HTTP %d: %s",
                        resp.status_code, resp.text[:300])
            return {"sent": False, "status_code": resp.status_code}

        log.info("OIC error-notification sent OK (HTTP %s).", resp.status_code)
        return {"sent": True, "status_code": resp.status_code}
