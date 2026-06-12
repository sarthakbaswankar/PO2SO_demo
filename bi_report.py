"""
bi_report.py
============
Calls the BIP BI report with customer_name + business_unit_name parameters,
parses the CSV response, and returns the enrichment fields needed to build
the final Sales Order JSON payload.
"""

from __future__ import annotations

import csv
import io
import logging
from typing import Any

from config import BIPConfig
from logging_setup import dump_text

# Re-use the existing client.py that was already provided
from bip_api.client import fetch_report_csv, make_session
from bip_api.models import DownloadRequest
from bip_api.config import Settings as BIPSettings

log = logging.getLogger(__name__)


def _bip_settings(cfg: BIPConfig) -> BIPSettings:
    """Adapt our config into the shape expected by the existing bip_api client."""
    s = BIPSettings()
    s.oracle_base_url = cfg.oracle_base_url
    s.oracle_username = cfg.oracle_username
    s.oracle_password = cfg.oracle_password
    s.request_timeout = cfg.request_timeout
    return s


class BIReportClient:
    def __init__(self, cfg: BIPConfig) -> None:
        self.cfg = cfg
        self._session = make_session()
        self._bip_settings = _bip_settings(cfg)

    def fetch_customer_data(
        self,
        customer_name: str,
        business_unit_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Run the BI report filtered by customer_name (and optionally BU name).
        Returns a flat dict of the first matching row with the enrichment fields.

        Raises ValueError if no matching row is found.
        """
        req = DownloadRequest(
            report_path=self.cfg.report_path,
            customer_name=customer_name,
            # Pass BU name as a custom parameter if your DownloadRequest supports it
            # Adjust the model if needed to add business_unit_name
        )

        log.info(
            "BIP-STEP: calling report %s for customer=%r BU=%r",
            self.cfg.report_path, customer_name, business_unit_name,
        )

        _filename, csv_bytes = fetch_report_csv(
            req,
            self._bip_settings,
            self._session,
        )
        log.info("BIP-STEP: report returned %d CSV byte(s) as %s",
                 len(csv_bytes), _filename)

        # Persist the raw CSV to the backend for inspection/debugging.
        try:
            csv_path = dump_text(csv_bytes.decode("utf-8", errors="replace"),
                                 f"bip_csv_{customer_name or 'unknown'}", suffix="csv")
            if csv_path:
                log.info("BIP-STEP: raw CSV saved -> %s", csv_path)
        except Exception as dump_exc:  # never let dumping break the pipeline
            log.warning("BIP-STEP: could not save raw CSV: %s", dump_exc)

        # Parse CSV in-memory. Use utf-8-sig so a BOM (Oracle BIP prepends one)
        # doesn't corrupt the first column's header name.
        reader = csv.DictReader(
            io.StringIO(csv_bytes.decode("utf-8-sig"))
        )
        rows = list(reader)
        log.info("BIP-STEP: parsed %d row(s) from CSV", len(rows))

        if not rows:
            raise ValueError(
                f"BI report returned no rows for customer={customer_name!r}"
            )

        # If multiple rows returned, prefer the one matching the BU name
        row = rows[0]

        if business_unit_name and len(rows) > 1:
            for r in rows:
                if (
                    r.get("BUSINESS_UNIT_NAME", "").strip().upper()
                    == business_unit_name.strip().upper()
                ):
                    row = r
                    break

        log.info(
            "BIP row selected: BU_ID=%s, PartyNum=%s, "
            "BillToSiteUseId=%s, ShipToPartyId=%s",
            row.get("BUSINESS_UNIT_ID"),
            row.get("BUYING_PARTY_NUMBER"),
            row.get("BILL_TO_SITE_USE_ID"),
            row.get("SHIP_TO_PARTY_ID"),
        )

        # Map CSV column names → Sales Order API field names
        return {
            "BusinessUnitId": _int(
                row.get("BUSINESS_UNIT_ID")
            ),
            "BusinessUnitName": row.get(
                "BUSINESS_UNIT_NAME"
            ),
            "RequestingBusinessUnitId": _int(
                row.get("REQUESTING_BUSINESS_UNIT_ID")
            ),
            "BuyingPartyId": _int(
                row.get("BUYING_PARTY_ID")
            ),
            "BuyingPartyName": row.get(
                "BUYING_PARTY_NAME"
            ),
            "BuyingPartyNumber": row.get(
                "BUYING_PARTY_NUMBER"
            ),
            "RequestingLegalEntityId": _int(
                row.get("REQUESTING_LEGAL_ENTITY_ID")
            ),

            # billToCustomer
            "CustomerAccountId": _int(
                row.get("CUST_ACCOUNT_ID")
            ),
            "BillToSiteUseId": _int(
                row.get("BILL_TO_SITE_USE_ID")
            ),

            # shipToCustomer — SiteId must be PARTY_SITE_ID,
            # not SITE_USE_ID
            "ShipToPartyId": _int(
                row.get("SHIP_TO_PARTY_ID")
            ),
            "ShipToPartySiteId": _int(
                row.get("SHIP_TO_PARTY_SITE_ID")
            ),
        }

    # ── Customer base fields + ALL ship-to addresses (for address matching) ──
    def fetch_customer_data_and_addresses(
        self,
        customer_name: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Run the address report (one parameter: Customer Name) and return:

            (base_fields, address_rows)

        • base_fields  — the customer-level enrichment that is the SAME for every
                         row (BusinessUnitId, BuyingParty*, CustomerAccountId,
                         BillToSiteUseId, …). NO ship-to fields here — those vary
                         per address and are chosen by the address matcher.
        • address_rows — the raw CSV rows (one per ship-to address), passed to
                         address_matcher.parse_bip_address_rows() to build
                         candidates and select the right one.

        Raises ValueError if the report returns no rows.
        """
        req = DownloadRequest(
            report_path=self.cfg.address_report_path,
            customer_name=customer_name,
        )
        log.info("ADDR-STEP: calling address report %s for customer=%r",
                 self.cfg.address_report_path, customer_name)

        _filename, csv_bytes = fetch_report_csv(req, self._bip_settings, self._session)
        log.info("ADDR-STEP: report returned %d CSV byte(s) as %s", len(csv_bytes), _filename)

        try:
            csv_path = dump_text(csv_bytes.decode("utf-8", errors="replace"),
                                 f"bip_addresses_{customer_name or 'unknown'}", suffix="csv")
            if csv_path:
                log.info("ADDR-STEP: raw CSV saved -> %s", csv_path)
        except Exception as dump_exc:
            log.warning("ADDR-STEP: could not save raw CSV: %s", dump_exc)

        reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))
        rows = list(reader)
        log.info("ADDR-STEP: parsed %d address row(s) for customer=%r", len(rows), customer_name)
        if not rows:
            raise ValueError(f"Address report returned no rows for customer={customer_name!r}")

        base_fields = self._map_base_fields(rows[0])
        return base_fields, rows

    @staticmethod
    def _map_base_fields(row: dict[str, Any]) -> dict[str, Any]:
        """Customer-level fields that are constant across a customer's addresses.

        Column aliases handle reports whose column names differ from the original
        spec — e.g. BU_NAME instead of BUSINESS_UNIT_NAME, CUSTOMER_NUMBER
        instead of BUYING_PARTY_NUMBER, CUST_ACCOUNT_ID instead of
        CUSTOMER_ACCOUNT_ID, BILL_TO_PARTY_SITE_ID as fallback for ship-to IDs.
        """
        def _pick(*keys):
            """Return the first non-empty value found among the given column names."""
            for k in keys:
                v = row.get(k)
                if v not in (None, ""):
                    return v
            return None

        return {
            # BUSINESS_UNIT_NAME is the original; BU_NAME is what the report
            # actually emits — accept both.
            "BusinessUnitId":           _int(_pick("BUSINESS_UNIT_ID")),
            "BusinessUnitName":         _pick("BUSINESS_UNIT_NAME", "BU_NAME"),
            # REQUESTING_BUSINESS_UNIT_ID — present in the report, just needs
            # to be read; fall back to BUSINESS_UNIT_ID if somehow absent.
            "RequestingBusinessUnitId": _int(_pick("REQUESTING_BUSINESS_UNIT_ID",
                                                   "BUSINESS_UNIT_ID")),
            # BUYING_PARTY_ID maps directly.
            "BuyingPartyId":            _int(_pick("BUYING_PARTY_ID")),
            # Report has no BUYING_PARTY_NAME column — use CUSTOMER_NAME instead.
            "BuyingPartyName":          _pick("BUYING_PARTY_NAME", "CUSTOMER_NAME"),
            # Report has no BUYING_PARTY_NUMBER — use CUSTOMER_NUMBER / ACCOUNT_NUMBER.
            "BuyingPartyNumber":        _pick("BUYING_PARTY_NUMBER",
                                              "CUSTOMER_NUMBER", "ACCOUNT_NUMBER"),
            # REQUESTING_LEGAL_ENTITY_ID is not in this report — omit gracefully.
            "RequestingLegalEntityId":  _int(_pick("REQUESTING_LEGAL_ENTITY_ID")),
            "CustomerAccountId":        _int(_pick("CUST_ACCOUNT_ID",
                                                   "CUSTOMER_ACCOUNT_ID")),
            "BillToSiteUseId":          _int(_pick("BILL_TO_SITE_USE_ID")),
        }

    # ── Customer-item cross-reference report ──────────────────────────────────
    def fetch_item_xref(
        self,
        customer_name: str | None = None,
        customer_number: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Run the Trading Partner Item Details report and return a mapping of
        the customer's item number -> internal Fusion item.

        Returns { "<CUSTOMER_ITEM upper>": {"item_number", "item_description",
        "organization_code", "customer_item"} }. Best-effort: on any failure it
        returns {} so order creation falls back to the PDF item numbers. Uses its
        OWN session so it can run in parallel with fetch_customer_data safely.
        """
        params: dict[str, str] = {}
        if self.cfg.xref_filter_by_party:
            if customer_name:
                params[self.cfg.xref_party_name_param] = customer_name
            if customer_number:
                params[self.cfg.xref_party_number_param] = customer_number

        req = DownloadRequest(report_path=self.cfg.xref_report_path, params=params)
        log.info("XREF-STEP: calling cross-reference report %s (params=%s, filter_by_party=%s)",
                 self.cfg.xref_report_path, params or "<none — fetching all rows>",
                 self.cfg.xref_filter_by_party)

        session = make_session()  # separate session → safe alongside the customer call
        _filename, csv_bytes = fetch_report_csv(req, self._bip_settings, session)
        log.info("XREF-STEP: report returned %d CSV byte(s)", len(csv_bytes))

        try:
            csv_path = dump_text(csv_bytes.decode("utf-8", errors="replace"),
                                 f"bip_xref_{customer_name or 'unknown'}", suffix="csv")
            if csv_path:
                log.info("XREF-STEP: raw CSV saved -> %s", csv_path)
        except Exception as dump_exc:
            log.warning("XREF-STEP: could not save raw CSV: %s", dump_exc)

        # utf-8-sig strips a leading BOM so the first header (CUSTOMER_ITEM)
        # matches; BIP commonly emits a BOM that plain utf-8 would keep.
        reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))
        rows = list(reader)
        log.info("XREF-STEP: parsed %d cross-reference row(s)", len(rows))

        def _g(row: dict, *names: str):
            # strip whitespace AND a stray BOM from header keys, then lower-case
            lower = {(k or "").strip().lstrip("\ufeff").strip().lower(): v
                     for k, v in row.items()}
            for n in names:
                v = lower.get(n.lower())
                if v not in (None, ""):
                    return v
            return None

        mapping: dict[str, dict[str, Any]] = {}
        for row in rows:
            cust_item = _g(row, "CUSTOMER_ITEM", "Customer_Item", "TP_ITEM_NUMBER")
            item_num = _g(row, "ITEM_NUMBER", "Item_Number")
            if cust_item and item_num:
                mapping[str(cust_item).strip().upper()] = {
                    "customer_item": str(cust_item).strip(),
                    "item_number": str(item_num).strip(),
                    "item_description": _g(row, "ITEM_DESCRIPTION", "Item_Description",
                                           "CUSTOMER_ITEM_DESCRIPTION", "Customer_Item_Description"),
                    "organization_code": _g(row, "ORGANIZATION_CODE", "organization_code"),
                }
        log.info("XREF-STEP: built %d item mapping(s)", len(mapping))
        return mapping


def _int(value: str | None) -> int | None:
    """Safely convert a string to int; return None if blank or unconvertible."""
    if value is None or str(value).strip() == "":
        return None

    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None