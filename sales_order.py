"""
sales_order.py
==============
Builds the final Sales Order REST API payload and POSTs it to Oracle Fusion.
"""
from __future__ import annotations

import base64
import logging
import uuid
from typing import Any

import requests

from config import SalesOrderConfig

log = logging.getLogger(__name__)


class SalesOrderAPIError(Exception):
    """Raised when the Sales Order API rejects a request (non-2xx, non-401).

    Carries the HTTP status code and the *raw response body* from Oracle so the
    caller can surface the real reason (e.g. the FOM-4515095 pricing message)
    and feed it to the GenAI error-simplifier.
    """
    def __init__(self, message: str, status_code: int | None = None,
                 body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class SalesOrderClient:
    def __init__(self, cfg: SalesOrderConfig) -> None:
        self.cfg = cfg
        self._session = requests.Session()
        self._session.auth = (cfg.oracle_username, cfg.oracle_password)
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
        })

    # ── Payload builder ───────────────────────────────────────────────────────
    def build_payload(
        self,
        extracted: dict[str, Any],
        bi_data:   dict[str, Any],
    ) -> dict[str, Any]:
        """
        Merge GenAI-extracted fields with BI report enrichment data
        into the exact shape expected by salesOrdersForOrderHub POST.
        """
        # Build order lines
        lines = []
        for line in extracted.get("lines", []):
            # UOM code sent to Oracle: if a YAML UOM conversion was applied to
            # this line, use the conversion's sold UOM code from the YAML file
            # (e.g. CS -> EA). Otherwise use the line's own UOM code.
            conv = line.get("_uom_conversion") or {}
            uom_code = conv.get("to_uom") or line.get("OrderedUOMCode", "Each")
            lines.append({
                "SourceTransactionLineId":       str(line.get("SourceTransactionLineId", "1")),
                "SourceTransactionLineNumber":   str(line.get("SourceTransactionLineNumber", "1")),
                "SourceScheduleNumber":          str(line.get("SourceScheduleNumber", "1")),
                "SourceTransactionScheduleId":   str(line.get("SourceTransactionScheduleId", "1")),
                "ProductNumber":                 line.get("ProductNumber"),
                "OrderedQuantity":               line.get("OrderedQuantity", 1),
                "OrderedUOMCode":                uom_code,
            })

        payload: dict[str, Any] = {
            # ── From PDF (via GenAI) ──────────────────────────────────────────
            "SourceTransactionNumber":         extracted["SourceTransactionNumber"],
            "SourceTransactionId":             extracted["SourceTransactionId"],
            "SourceTransactionRevisionNumber": extracted.get("SourceTransactionRevisionNumber", 1),
            "SourceTransactionSystem":         self.cfg.source_transaction_system,
            "TransactionalCurrencyCode":       extracted.get("TransactionalCurrencyCode", "USD"),
            # CustomerPONumber — the number the customer sends for this purchase
            # order (Oracle field title "Purchase Order", max length 50). We use
            # the PO number extracted from the document and attach it to the SO.
            "CustomerPONumber":                (str(extracted.get("SourceTransactionNumber"))[:50]
                                                if extracted.get("SourceTransactionNumber") else None),
            # Hardcoded — Oracle tenant expects this exact code; extracted PDF value
            # was unreliable and caused null payloads. Change here if the Oracle
            # Order Type config changes.
            "TransactionTypeCode":             "STD_SALES_ORDER",
            "RequestedShipDate":               extracted.get("RequestedShipDate"),

            # ── From BI Report ────────────────────────────────────────────────
            "BusinessUnitId":                  bi_data["BusinessUnitId"],
            "RequestingBusinessUnitId":        bi_data.get("RequestingBusinessUnitId"),
            "BuyingPartyNumber":               bi_data.get("BuyingPartyNumber"),

            # ── Fixed flags ───────────────────────────────────────────────────
            "SubmittedFlag":                   self.cfg.submitted_flag,
            "FreezePriceFlag":                 False,
            "FreezeShippingChargeFlag":        False,
            "FreezeTaxFlag":                   False,

            # ── Nested objects from BI Report ─────────────────────────────────
            "billToCustomer": [{
                "CustomerAccountId": bi_data.get("CustomerAccountId"),
                "SiteUseId":         bi_data.get("BillToSiteUseId"),
            }],
            "shipToCustomer": [{
                "PartyId": bi_data.get("ShipToPartyId"),
                "SiteId":  bi_data.get("ShipToPartySiteId"),
                # SiteUseId is populated when the ship-to address matcher resolved
                # a specific site-use; omitted (None → stripped below) otherwise.
            }],

            # ── Order lines (from PDF via GenAI) ──────────────────────────────
            "lines": lines,
        }

        # Remove keys with None values to keep the payload clean
        payload = {k: v for k, v in payload.items() if v is not None}
        # Also strip None from the nested bill/ship customer objects (e.g. an
        # unresolved ShipToSiteUseId) so we never POST explicit nulls there.
        for key in ("billToCustomer", "shipToCustomer"):
            if key in payload and isinstance(payload[key], list):
                payload[key] = [{k: v for k, v in obj.items() if v is not None}
                                for obj in payload[key]]
        return payload

    # ── Duplicate guard ───────────────────────────────────────────────────────
    def check_duplicate(self, po_number: str | None) -> dict[str, Any]:
        """Ask Oracle whether a Sales Order already exists for this customer PO.

        Calls GET salesOrdersForOrderHub?q=CustomerPONumber='<po>'. Returns
        {"exists": bool, "orders": [...], "checked": bool}. If the check itself
        cannot run (network / non-auth HTTP error) it returns exists=False so a
        transient glitch doesn't block a legitimate order — but it logs a warning.
        A 401 is raised (same as create_order) since that's a real config problem.
        """
        result: dict[str, Any] = {"exists": False, "orders": [], "checked": False}
        if not po_number:
            return result

        url = self.cfg.oracle_base_url.rstrip("/") + self.cfg.api_path
        params = {"q": f"CustomerPONumber='{po_number}'", "limit": 5, "onlyData": "true"}
        log.info("Duplicate check: GET %s?q=CustomerPONumber='%s'", url, po_number)
        try:
            response = self._session.get(url, params=params, timeout=self.cfg.request_timeout)
        except requests.RequestException as exc:
            log.warning("Duplicate-check request failed for PO %s: %s — continuing without it",
                        po_number, exc)
            return result

        if response.status_code == 401:
            raise PermissionError("Oracle SO API authentication failed — check credentials")
        if not response.ok:
            log.warning("Duplicate-check HTTP %d for PO %s: %s — continuing without it",
                        response.status_code, po_number, response.text[:500])
            return result

        try:
            data = response.json()
        except ValueError:
            log.warning("Duplicate-check returned non-JSON for PO %s — continuing", po_number)
            return result

        items = data.get("items", []) or []
        total = data.get("totalResults")
        result["checked"] = True
        result["orders"] = items
        result["exists"] = bool(items) or bool(total)
        log.info("Duplicate check PO %s: exists=%s (%d item(s) returned)",
                 po_number, result["exists"], len(items))
        return result

    # ── API call ──────────────────────────────────────────────────────────────
    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST payload to the Sales Order API. Returns the API response JSON."""
        url = self.cfg.oracle_base_url.rstrip("/") + self.cfg.api_path
        log.info(
            "Creating Sales Order: SourceTransactionNumber=%s",
            payload.get("SourceTransactionNumber"),
        )

        response = self._session.post(
            url,
            json=payload,
            timeout=self.cfg.request_timeout,
        )

        if response.status_code == 401:
            raise PermissionError("Oracle SO API authentication failed — check credentials")

        if not response.ok:
            log.error(
                "SO API error %d for %s: %s",
                response.status_code,
                payload.get("SourceTransactionNumber"),
                response.text[:2000],
            )
            raise SalesOrderAPIError(
                f"Sales Order API returned HTTP {response.status_code}",
                status_code=response.status_code,
                body=response.text,
            )

        result: dict[str, Any] = response.json()
        log.info(
            "Order created: HeaderId=%s, OrderKey=%s",
            result.get("HeaderId"),
            result.get("OrderKey"),
        )
        return result

    # ── Attach PDF to the created Sales Order ────────────────────────────────
    def attach_pdf(
        self,
        order_key: str,
        pdf_bytes: bytes,
        file_name: str,
        title: str | None = None,
        description: str = "Source Purchase Order PDF",
        category_name: str = "MISC",
    ) -> dict[str, Any]:
        """
        Attach a PDF file to an existing Sales Order via:
          POST /salesOrdersForOrderHub/{OrderKey}/child/attachments

        Returns the API response JSON.
        """
        if not order_key:
            raise ValueError("order_key is required to attach a PDF")

        url = (
            self.cfg.oracle_base_url.rstrip("/")
            + self.cfg.api_path
            + f"/{order_key}/child/attachments"
        )

        file_contents_b64 = base64.b64encode(pdf_bytes).decode("ascii")

        payload = {
            "DatatypeCode":                "FILE",
            "FileName":                    file_name,
            "CategoryName":                category_name,
            "UploadedText":                None,
            "UploadedFileContentType":     "application/pdf",
            "UploadedFileName":            file_name,
            "ContentRepositoryFileShared": "false",
            "Title":                       title or file_name,
            "Description":                 description,
            "FileContents":                file_contents_b64,
        }

        log.info(
            "Attaching PDF %s (%d bytes, %d b64 chars) to OrderKey=%s",
            file_name, len(pdf_bytes), len(file_contents_b64), order_key,
        )

        response = self._session.post(
            url,
            json=payload,
            timeout=self.cfg.request_timeout,
        )

        if response.status_code == 401:
            raise PermissionError("Oracle SO Attachment API authentication failed")

        if not response.ok:
            log.error(
                "Attachment API error %d for OrderKey=%s: %s",
                response.status_code, order_key, response.text[:2000],
            )
            response.raise_for_status()

        result: dict[str, Any] = response.json()
        log.info(
            "PDF attached: AttachedDocumentId=%s, OrderKey=%s",
            result.get("AttachedDocumentId"), order_key,
        )
        return result

    # ── Attach a URL (PAR link) to the created Sales Order ───────────────────
    def attach_url(
        self,
        order_key: str,
        url: str,
        title: str,
        description: str = "Source Purchase Order PDF (link)",
        category_name: str = "MISC",
    ) -> dict[str, Any]:
        """Attach a clickable URL to an existing Sales Order via:
          POST /salesOrdersForOrderHub/{OrderKey}/child/attachments

        Uses DatatypeCode = "WEB_PAGE" so the attachment shows as a link in
        Oracle, not an embedded file. The URL should be a PAR (Pre-Authenticated
        Request) so anyone clicking it can download the PDF without OCI login.

        Returns the API response JSON.
        """
        if not order_key:
            raise ValueError("order_key is required to attach a URL")
        if not url:
            raise ValueError("url is required")

        endpoint = (
            self.cfg.oracle_base_url.rstrip("/")
            + self.cfg.api_path
            + f"/{order_key}/child/attachments"
        )

        payload = {
            "DatatypeCode":      "WEB_PAGE",
            "CategoryName":       category_name,
            "Title":              title,
            "Description":        description,
            "Url":                url,
        }

        log.info(
            "Attaching URL to OrderKey=%s: title=%s url=%s",
            order_key, title, url,
        )

        response = self._session.post(
            endpoint,
            json=payload,
            timeout=self.cfg.request_timeout,
        )

        if response.status_code == 401:
            raise PermissionError("Oracle SO Attachment API authentication failed")

        if not response.ok:
            log.error(
                "URL-Attachment API error %d for OrderKey=%s: %s",
                response.status_code, order_key, response.text[:2000],
            )
            response.raise_for_status()

        result: dict[str, Any] = response.json()
        log.info(
            "URL attached: AttachedDocumentId=%s, OrderKey=%s",
            result.get("AttachedDocumentId"), order_key,
        )
        return result