"""
bip_api/client.py
=================
"""
from __future__ import annotations

import base64
import logging
import re
import textwrap
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bip_api.config import Settings
from bip_api.exceptions import AuthError, ReportError
from bip_api.models import DownloadRequest

log = logging.getLogger(__name__)

_SERVICE_PATH = "/xmlpserver/services/PublicReportService"
_SOAP_NS = "http://xmlns.oracle.com/oxp/service/PublicReportService"
_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_CONTENT_TYPE = "text/xml; charset=utf-8"

_RE_FAULT = re.compile(r"<faultstring>(.*?)</faultstring>", re.DOTALL)
_RE_REPORT_BYTES = re.compile(r"<reportBytes>(.*?)</reportBytes>", re.DOTALL)


def _build_envelope(req: DownloadRequest, username: str, password: str) -> str:
    # All user-supplied values are XML-escaped to prevent SOAP injection.
    params = ""
    if req.customer_name:
        params += (
            f"<pub:item>"
            f"<pub:name>P_CUSTOMER_NAME</pub:name>"
            f"<pub:values><pub:item>{xml_escape(req.customer_name)}</pub:item></pub:values>"
            f"</pub:item>"
        )
    
    # ADDED: Inject Business Unit Name from the GenAI JSON response
    if req.business_unit_name:
        params += (
            f"<pub:item>"
            f"<pub:name>P_BUSINESS_UNIT_NAME</pub:name>"
            f"<pub:values><pub:item>{xml_escape(req.business_unit_name)}</pub:item></pub:values>"
            f"</pub:item>"
        )
        
    if req.from_date:
        params += (
            f"<pub:item>"
            f"<pub:name>P_FROM_DATE</pub:name>"
            f"<pub:values><pub:item>{xml_escape(req.from_date)}</pub:item></pub:values>"
            f"</pub:item>"
        )
    if req.to_date:
        params += (
            f"<pub:item>"
            f"<pub:name>P_TO_DATE</pub:name>"
            f"<pub:values><pub:item>{xml_escape(req.to_date)}</pub:item></pub:values>"
            f"</pub:item>"
        )

    # Arbitrary extra parameters (e.g. P_PARTY_NAME / P_PARTY_NUMBER for the
    # customer-item cross-reference report).
    if req.params:
        for name, value in req.params.items():
            if value is None or value == "":
                continue
            params += (
                f"<pub:item>"
                f"<pub:name>{xml_escape(str(name))}</pub:name>"
                f"<pub:values><pub:item>{xml_escape(str(value))}</pub:item></pub:values>"
                f"</pub:item>"
            )

    # PublicReportService passes credentials as body elements.
    # attributeFormat is kept as 'csv' to match the architecture diagram and allow in-memory parsing.
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="utf-8"?>
        <soapenv:Envelope
            xmlns:soapenv="{_ENVELOPE_NS}"
            xmlns:pub="{_SOAP_NS}">
          <soapenv:Header/>
          <soapenv:Body>
            <pub:runReport>
              <pub:userID>{xml_escape(username)}</pub:userID>
              <pub:password>{xml_escape(password)}</pub:password>
              <pub:reportRequest>
                <pub:reportAbsolutePath>{xml_escape(req.report_path)}</pub:reportAbsolutePath>
                <pub:sizeOfDataChunkDownload>-1</pub:sizeOfDataChunkDownload>
                <pub:parameterNameValues>
                  {params}
                </pub:parameterNameValues>
                <pub:attributeFormat>csv</pub:attributeFormat>
              </pub:reportRequest>
            </pub:runReport>
          </soapenv:Body>
        </soapenv:Envelope>
    """)


def make_session(pool_size: int = 10) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[502, 503, 504],
        allowed_methods=frozenset(["GET", "POST", "PUT"]),
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=pool_size,
        pool_maxsize=pool_size,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def report_name(report_path: str) -> str:
    """Return the human-readable report name (basename without .xdo)."""
    return report_path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".xdo")


def report_stem(report_path: str) -> str:
    """Return the filesystem-safe stem used in saved CSV filenames."""
    return report_name(report_path).replace(" ", "_")


def fetch_report_csv(
    req: DownloadRequest,
    settings: Settings,
    session: requests.Session,
) -> tuple[str, bytes]:
    """Call BIP SOAP API; return (csv_filename, csv_bytes)."""
    url = settings.oracle_base_url.rstrip("/") + _SERVICE_PATH
    envelope = _build_envelope(req, settings.oracle_username, settings.oracle_password)

    try:
        response = session.post(
            url,
            data=envelope.encode("utf-8"),
            headers={"Content-Type": _CONTENT_TYPE, "SOAPAction": '"runReport"'},
            timeout=settings.request_timeout,
        )
    except requests.RequestException as exc:
        raise ReportError(f"Network error: {exc}") from exc

    if response.status_code == 401:
        raise AuthError("Oracle BIP authentication failed — check credentials")

    if not response.ok:
        log.error(
            "BIP HTTP %d for %s: %s",
            response.status_code,
            req.report_path,
            response.text[:1000],
        )
        raise ReportError(f"Oracle BIP returned HTTP {response.status_code}")

    text = response.text

    fault_match = _RE_FAULT.search(text)
    if fault_match:
        fault = fault_match.group(1).strip()
        if "Invalid username or password" in fault or "Authentication" in fault:
            raise AuthError("Oracle BIP authentication failed")
        log.error("BIP SOAP fault for %s: %s", req.report_path, fault)
        raise ReportError("Oracle BIP returned an error (see server logs)")

    bytes_match = _RE_REPORT_BYTES.search(text)
    if not bytes_match:
        log.error("BIP response missing reportBytes for %s: %s", req.report_path, text[:1000])
        raise ReportError("Oracle BIP returned an unexpected response shape")

    csv_bytes = base64.b64decode(bytes_match.group(1))

    stem = report_stem(req.report_path)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{stem}_{timestamp}.csv"
    log.info("Fetched %s (%d bytes)", filename, len(csv_bytes))
    return filename, csv_bytes