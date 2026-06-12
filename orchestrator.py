"""
orchestrator.py
===============
Ties together all steps for one PDF:
  1. Download from OCI Object Storage
  2. Extract text (pdfplumber)
  3. Extract structured data (OCI GenAI)
  4. Call BI Report (BIP SOAP)
  5. Merge and build Sales Order payload
  6. POST to Sales Order API
  6b. Attach source PDF to the created order:
        - URL mode  → move PDF to processed/, create a PAR, attach the URL
        - FILE mode → base64-encode the PDF and attach it as a file
      Controlled by SO_ATTACHMENT_MODE in config.
  7. Move PDF to processed/ (FILE mode only; URL mode does this in step 6b)
     or to error/ on failure.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from config import settings
from storage import StorageClient
from extractor import PDFExtractor, normalize_purchase_orders
from bi_report import BIReportClient
from sales_order import SalesOrderClient, SalesOrderAPIError
from oic_client import OICClient
from uom_converter import apply_uom_conversions
from address_matcher import (
    parse_bip_address_rows, match_ship_to_address, pdf_address_text,
)
from logging_setup import configure_logging, dump_json, per_file_log

log = logging.getLogger(__name__)


@contextmanager
def _step(number: str, name: str, context: str):
    """Log the start, end, and elapsed time of a pipeline step.

    Logs a clear ▶ at entry and ✓ at success with millisecond timing, or ✗ with
    timing if the step raises — so every step boundary is visible in the logs.
    """
    log.info("  STEP %s ▶ %s  [%s]", number, name, context)
    t0 = time.perf_counter()
    try:
        yield
    except Exception as exc:
        dt = (time.perf_counter() - t0) * 1000.0
        log.error("  STEP %s ✗ %s FAILED after %.0f ms: %s", number, name, dt, exc)
        raise
    else:
        dt = (time.perf_counter() - t0) * 1000.0
        log.info("  STEP %s ✓ %s done (%.0f ms)", number, name, dt)


@dataclass
class ProcessingResult:
    object_name: str
    success: bool
    order_key: str | None = None
    header_id: int | None = None
    error: str | None = None
    # Real extracted data so the frontend can show actual values (not mocks)
    customer_name: str | None = None
    business_unit_name: str | None = None
    transaction_number: str | None = None
    currency_code: str | None = None
    confidence: str | None = None
    lines: list | None = None        # list of extracted line dicts
    par_url: str | None = None       # the attached PAR URL (URL mode)
    elapsed_ms: float | None = None  # wall-clock processing time for this PDF
    # ── New: status + final-API data + error layers + per-file log ───────────
    status: str | None = None            # "success" | "error" | "duplicate"
    payload: dict | None = None          # the SO payload we sent (or would send)
    api_response: dict | None = None     # the create_order API response (success)
    api_error_raw: str | None = None     # the raw Oracle API error body
    api_error_simplified: str | None = None  # GenAI plain-English version
    log_file: str | None = None          # path to this PDF's per-file log
    extracted: dict | None = None        # full GenAI JSON (for reprocess/edits)
    existing_orders: list | None = None  # matches found by the duplicate check
    bi_data: dict | None = None          # BI report enrichment (ship-to / bill-to / etc.)
    # ── Ship-to address matching ─────────────────────────────────────────────
    ship_to_address: str | None = None       # the resolved (BIP) or PDF address shown in UI
    ship_to_source: str | None = None        # "bip" (matched) | "pdf" (fallback on error)
    ship_to_match_method: str | None = None  # single | postal | fuzzy | genai | none
    # ── Multiple POs in one PDF ──────────────────────────────────────────────
    order_keys: list | None = None       # all OrderKeys created from this PDF
    sub_results: list | None = None      # per-PO detail dicts when a PDF holds many POs


class POAutomationOrchestrator:
    def __init__(self) -> None:
        # Ensure logging is configured even if the orchestrator is used outside
        # the main.py / app.py entry points (idempotent — no duplicate handlers).
        configure_logging()
        self.storage    = StorageClient(settings.storage)
        self.extractor  = PDFExtractor(settings.genai)
        self.bi_client  = BIReportClient(settings.bip)
        self.so_client  = SalesOrderClient(settings.sales_order)
        self.oic        = OICClient(settings.oic)

    def _fetch_enrichment(self, customer_name, business_unit_name, customer_number=None):
        """Call the customer ADDRESS report AND the item cross-reference report
        at the SAME time. Returns (base_fields, address_rows, xref_map).

        The address report (one parameter: Customer Name) returns the customer's
        base enrichment plus EVERY ship-to address; the right one is chosen later
        by the address matcher. The customer report is fatal if it fails; the
        cross-reference is best-effort.
        """
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_cust = ex.submit(self.bi_client.fetch_customer_data_and_addresses,
                               customer_name)
            f_xref = ex.submit(self._safe_item_xref, customer_name, customer_number)
            base_fields, address_rows = f_cust.result()  # fatal if it fails
            xref_map = f_xref.result()                    # never raises
        return base_fields, address_rows, xref_map

    def _resolve_ship_to(self, po: dict, address_rows: list) -> tuple[dict, Any]:
        """Run the ship-to address matcher for one PO. Returns (ship_to_ids, result).

        On a confident match, ship_to_ids carries ShipToPartyId /
        ShipToPartySiteId / ShipToSiteUseId / ShipToAddress to merge into bi_data.
        Otherwise ship_to_ids is {} and the caller raises (→ error/ + PDF address
        in the UI). When the matcher flags `needs_review`, the closest on-file
        addresses are added to the reason so a human can pick/fix quickly.
        """
        candidates = parse_bip_address_rows(address_rows)
        pdf_addr = po.get("ShipToAddress") or {}
        result = match_ship_to_address(
            pdf_addr, candidates,
            genai_match_fn=self.extractor.match_address_via_ai,       # AI picks
            genai_validate_fn=self.extractor.validate_address_via_ai,  # AI confirms
        )
        if result.matched and result.candidate:
            ids = result.candidate.to_ids()
            log.info("Ship-to address resolved via %s: %s",
                     result.method, result.candidate.display())
            return ids, result

        # Not matched → build a helpful reason that also lists the closest
        # on-file addresses, so the workbench shows the user what to choose from.
        reason = result.reason
        if getattr(result, "top_candidates", None):
            closest = "; ".join(c.display() for c in result.top_candidates[:3])
            reason += f" Closest addresses on file: {closest}."
        result.reason = reason
        log.warning("Ship-to address NOT resolved (review=%s): %s | PDF address: %s",
                    getattr(result, "needs_review", False), reason,
                    pdf_address_text(pdf_addr) or "<none>")
        return {}, result

    @staticmethod
    def _apply_uom(po: dict) -> int:
        """Apply UOM conversions to a PO's lines (after the item cross-reference).
        No-op when disabled. Returns the number of lines converted."""
        if not settings.uom.enabled:
            log.info("UOM conversions disabled (PO2SO_UOM_ENABLED=false).")
            return 0
        return apply_uom_conversions(
            po.get("lines", []) or [], po.get("CustomerName"),
        )

    def _notify_error(self, object_name: str, data: dict | None, error: str) -> None:
        """Best-effort: trigger the OIC error-notification integration AFTER the
        PDF has been moved to the error/ folder. Never raises."""
        try:
            self.oic.trigger_error_notification(context={
                "file": object_name.rsplit("/", 1)[-1],
                "po_number": (data or {}).get("SourceTransactionNumber"),
                "customer": (data or {}).get("CustomerName"),
                "error": (error or "")[:300],
            })
        except Exception as exc:  # belt-and-braces; method is already best-effort
            log.warning("Error-notification trigger raised (ignored): %s", exc)

    # ── Core: turn ONE purchase-order dict into a Sales Order (no file move) ──
    def _create_so_for_po(self, po: dict, object_name: str, label: str) -> dict:
        """Validate → duplicate guard → enrich + address match → UOM convert →
        build payload → create order, for a SINGLE purchase-order dict.

        Returns a result dict (never raises) describing what happened. File
        move/attachment is handled by the caller because one PDF may hold many
        POs sharing the same source file.
        """
        out: dict[str, Any] = {
            "po_number": po.get("SourceTransactionNumber"),
            "customer_name": po.get("CustomerName"),
            "business_unit_name": po.get("BusinessUnitName"),
            "currency_code": po.get("TransactionalCurrencyCode"),
            "confidence": po.get("_confidence"),
            "success": False, "status": "error",
        }
        try:
            _validate_extracted(po)

            po_num = po.get("SourceTransactionNumber")
            dup = self.so_client.check_duplicate(po_num)
            if dup.get("exists"):
                existing = dup.get("orders") or []
                refs = ", ".join(
                    str(o.get("OrderNumber") or o.get("OrderKey") or o.get("HeaderId"))
                    for o in existing) or "an existing order"
                out.update(status="duplicate",
                           error=f"A Sales Order already exists for Customer PO '{po_num}' ({refs}).",
                           existing_orders=existing)
                return out

            # Enrichment: base fields + all ship-to addresses + item xref
            base_fields, address_rows, xref_map = self._fetch_enrichment(
                customer_name=po["CustomerName"],
                business_unit_name=po.get("BusinessUnitName"),
                customer_number=po.get("CustomerNumber"),
            )

            # Item cross-reference remap (BEFORE UOM conversion).
            self._apply_item_xref(po, xref_map)

            # Ship-to address matching (2a→2d). No match → route to error/.
            ship_to_ids, match_res = self._resolve_ship_to(po, address_rows)
            out["ship_to_match_method"] = match_res.method
            if not ship_to_ids:
                out["ship_to_address"] = pdf_address_text(po.get("ShipToAddress"))
                out["ship_to_source"] = "pdf"
                raise ValueError(
                    "Ship-to address could not be matched to the BI report "
                    f"({match_res.reason}). PDF address kept for review."
                )
            out["ship_to_address"] = ship_to_ids.get("ShipToAddress")
            out["ship_to_source"] = "bip"

            bi_data = {**base_fields, **ship_to_ids}

            # UOM conversion at the line level (AFTER the item cross-reference).
            self._apply_uom(po)

            payload = self.so_client.build_payload(po, bi_data)
            dump_json(payload, f"so_payload_{label}")
            result = self.so_client.create_order(payload)

            out.update(success=True, status="success",
                       order_key=result.get("OrderKey"),
                       header_id=result.get("HeaderId"),
                       payload=payload, api_response=result, bi_data=bi_data,
                       lines=po.get("lines"), extracted=po)
            return out
        except Exception as exc:
            raw = exc.body if isinstance(exc, SalesOrderAPIError) and exc.body else str(exc)
            simplified = ""
            try:
                simplified = self.extractor.simplify_error(
                    raw, context={"PO Number": po.get("SourceTransactionNumber"),
                                  "Customer": po.get("CustomerName")})
            except Exception:
                pass
            out.update(success=False, status="error", error=str(exc),
                       api_error_raw=raw, api_error_simplified=simplified,
                       lines=po.get("lines"), extracted=po)
            return out

    # ── Enrichment: customer BIP + item cross-reference (run in parallel) ─────
    def _safe_item_xref(self, customer_name, customer_number=None):
        """Fetch the customer-item cross-reference map. Never raises — on any
        failure returns {} so order creation falls back to the PDF item numbers."""
        try:
            xref = self.bi_client.fetch_item_xref(
                customer_name=customer_name, customer_number=customer_number
            )
            if not xref:
                log.warning("Item cross-reference returned no usable mappings for "
                            "customer=%r — lines will use the PDF item numbers", customer_name)
            return xref
        except Exception as exc:
            log.warning("Item cross-reference report failed (%s) — "
                        "falling back to PDF item numbers", exc, exc_info=True)
            return {}

    @staticmethod
    def _apply_item_xref(extracted, xref_map):
        """Remap each line's ProductNumber (the customer's item number from the
        PDF) to the internal Fusion Item Number using the cross-reference map.
        If a line has no match, it is left unchanged (uses the PDF value).
        Returns the number of lines remapped."""
        lines = extracted.get("lines", []) or []
        if not xref_map:
            log.warning("Item cross-reference map is EMPTY — every line keeps its PDF "
                        "item number. (Check the cross-reference report / its CSV.)")
            return 0
        log.info("Item cross-reference map has %d entr(y/ies): %s",
                 len(xref_map), list(xref_map.keys()))
        remapped = 0
        for line in lines:
            cust_item = str(line.get("ProductNumber") or "").strip().lstrip("\ufeff").strip()
            if not cust_item:
                continue
            hit = xref_map.get(cust_item.upper())
            if hit and hit.get("item_number"):
                line["CustomerItemNumber"] = cust_item          # keep original for display
                line["ProductNumber"] = hit["item_number"]      # internal Fusion item
                if not line.get("ProductDescription") and hit.get("item_description"):
                    line["ProductDescription"] = hit["item_description"]
                remapped += 1
                log.info("Item cross-reference MATCH: customer item '%s' -> Fusion item '%s' "
                         "(this is what will be sent to Oracle)", cust_item, hit["item_number"])
            else:
                log.warning("Item cross-reference: NO match for customer item '%s' "
                            "(available keys: %s) — sending it to Oracle unchanged",
                            cust_item, list(xref_map.keys()))
        return remapped

    # ── Process a single PDF ─────────────────────────────────────────────────
    def process_one(self, object_name: str) -> ProcessingResult:
        """Process one PDF inside its own dedicated log file (in addition to the
        shared console + po2so.log). The per-file log path is recorded on the
        result so the UI can show exactly what happened to that one file."""
        with per_file_log(object_name) as log_path:
            result = self._run_pipeline(object_name)
            result.log_file = log_path
            return result

    def _run_pipeline(self, object_name: str) -> ProcessingResult:
        log.info("═══ Processing START: %s ═══", object_name)
        t_start = time.perf_counter()
        extracted: dict[str, Any] | None = None
        try:
            # Step 1 — Download PDF
            with _step("1", "Download PDF from Object Storage", object_name):
                pdf_bytes = self.storage.download_pdf(object_name)

            # Step 2 & 3 — Extract text + GenAI structured extraction
            with _step("2+3", "Extract text + Gemini structured extraction", object_name):
                extracted = self.extractor.process_pdf(pdf_bytes, source_label=object_name)

            # Normalise to a list of purchase orders. One PDF may carry several
            # distinct PO numbers — each becomes its own Sales Order.
            pos = normalize_purchase_orders(extracted)
            if not pos:
                raise ValueError("No purchase orders found in the document.")
            log.info("Document %s contains %d purchase order(s): %s",
                     object_name, len(pos),
                     ", ".join(str(p.get("SourceTransactionNumber")) for p in pos))

            if len(pos) == 1:
                return self._run_single(object_name, pdf_bytes, pos[0], t_start)
            return self._run_multi(object_name, pdf_bytes, pos, t_start)

        except Exception as exc:
            # Failure BEFORE per-PO handling (download / extract / no POs).
            return self._handle_pdf_failure(object_name, extracted, exc, t_start)

    # ── Single-PO path (unchanged external behaviour) ────────────────────────
    def _run_single(self, object_name: str, pdf_bytes: bytes,
                    po: dict, t_start: float) -> ProcessingResult:
        with _step("3-6", "Validate → duplicate → enrich → match → UOM → create",
                   object_name):
            core = self._create_so_for_po(po, object_name, object_name)

        elapsed = (time.perf_counter() - t_start) * 1000.0

        if core["status"] == "duplicate":
            try:
                self.storage.move_to_processed(object_name)
            except Exception as exc:
                log.warning("Could not move duplicate %s to processed/: %s", object_name, exc)
            return ProcessingResult(
                object_name=object_name, success=False, status="duplicate",
                error=core.get("error"), existing_orders=core.get("existing_orders"),
                customer_name=core.get("customer_name"),
                business_unit_name=core.get("business_unit_name"),
                transaction_number=core.get("po_number"),
                currency_code=core.get("currency_code"),
                confidence=core.get("confidence"), lines=po.get("lines"),
                extracted=po, elapsed_ms=elapsed,
            )

        if not core["success"]:
            self._move_error_and_notify(object_name, po, core.get("error"))
            return ProcessingResult(
                object_name=object_name, success=False, status="error",
                error=core.get("error"), api_error_raw=core.get("api_error_raw"),
                api_error_simplified=core.get("api_error_simplified"),
                customer_name=core.get("customer_name"),
                business_unit_name=core.get("business_unit_name"),
                transaction_number=core.get("po_number"),
                currency_code=core.get("currency_code"),
                confidence=core.get("confidence"), lines=po.get("lines"),
                extracted=po, payload=core.get("payload"),
                ship_to_address=core.get("ship_to_address"),
                ship_to_source=core.get("ship_to_source"),
                ship_to_match_method=core.get("ship_to_match_method"),
                elapsed_ms=elapsed,
            )

        # Success — attach the source PDF (URL/FILE) and move the file once.
        order_key = core.get("order_key")
        par_url = self._finalize_attachment(
            object_name, pdf_bytes, [str(order_key)] if order_key else [],
            po_number=core.get("po_number"),
        )
        log.info("═══ Processing DONE: %s → OrderKey=%s (%.0f ms) ═══",
                 object_name, order_key, elapsed)
        return ProcessingResult(
            object_name=object_name, success=True, status="success",
            order_key=order_key, header_id=core.get("header_id"),
            order_keys=[order_key] if order_key else [],
            customer_name=core.get("customer_name"),
            business_unit_name=core.get("business_unit_name"),
            transaction_number=core.get("po_number"),
            currency_code=core.get("currency_code"),
            confidence=core.get("confidence"), lines=core.get("lines"),
            par_url=par_url, extracted=po, payload=core.get("payload"),
            api_response=core.get("api_response"), bi_data=core.get("bi_data"),
            ship_to_address=core.get("ship_to_address"),
            ship_to_source=core.get("ship_to_source"),
            ship_to_match_method=core.get("ship_to_match_method"),
            elapsed_ms=(time.perf_counter() - t_start) * 1000.0,
        )

    # ── Multi-PO path: one PDF → several Sales Orders, created in parallel ───
    def _run_multi(self, object_name: str, pdf_bytes: bytes,
                   pos: list[dict], t_start: float) -> ProcessingResult:
        workers = min(len(pos), max(1, settings.processing.max_workers))
        log.info("Multi-PO: creating %d Sales Orders in parallel (%d worker(s)) for %s",
                 len(pos), workers, object_name)

        indexed: list[tuple[int, dict]] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut = {ex.submit(self._create_so_for_po, po, object_name,
                             f"{object_name}__po{i+1}"): i
                   for i, po in enumerate(pos)}
            for f in as_completed(fut):
                indexed.append((fut[f], f.result()))
        indexed.sort(key=lambda x: x[0])
        sub = [r for _, r in indexed]

        created_keys = [str(r["order_key"]) for r in sub
                        if r.get("success") and r.get("order_key")]
        all_success = all(r.get("success") for r in sub)
        any_success = bool(created_keys)
        po_numbers = [str(r.get("po_number")) for r in sub]

        par_url = None
        if any_success:
            # Attach the shared PDF to every created order and move it once.
            par_url = self._finalize_attachment(
                object_name, pdf_bytes, created_keys,
                po_number=",".join(po_numbers),
            )
            if not all_success:
                # Some POs failed though others succeeded — notify, but leave the
                # PDF in processed/ alongside the orders that WERE created.
                failed = "; ".join(f"{r.get('po_number')}: {r.get('error')}"
                                   for r in sub if not r.get("success"))
                self._notify_error(object_name, pos[0],
                                   f"Partial multi-PO failure — {failed}")
        else:
            # Nothing created at all → error folder + notification.
            errs = "; ".join(f"{r.get('po_number')}: {r.get('error')}"
                             for r in sub if r.get("error"))
            self._move_error_and_notify(object_name, pos[0], errs)

        # Trim per-PO detail for storage on the result/history.
        sub_trimmed = [{
            "po_number": r.get("po_number"), "success": r.get("success"),
            "status": r.get("status"), "order_key": r.get("order_key"),
            "header_id": r.get("header_id"), "error": r.get("error"),
            "ship_to_address": r.get("ship_to_address"),
            "ship_to_match_method": r.get("ship_to_match_method"),
            "lines": r.get("lines"),
        } for r in sub]

        elapsed = (time.perf_counter() - t_start) * 1000.0
        combined_lines: list = []
        for r in sub:
            combined_lines.extend(r.get("lines") or [])

        status = "success" if all_success else ("error" if not any_success else "error")
        summary_err = None
        # Collect raw Oracle API error bodies from every failed sub-result so the
        # UI can show the real Oracle rejection message (not just the HTTP status).
        raw_parts = [
            f"PO {r.get('po_number')}: {r.get('api_error_raw') or r.get('error') or 'unknown error'}"
            for r in sub if not r.get("success")
        ]
        simplified_parts = [
            f"PO {r.get('po_number')}: {r.get('api_error_simplified')}"
            for r in sub if not r.get("success") and r.get("api_error_simplified")
        ]
        combined_api_error_raw = "\n\n---\n\n".join(raw_parts) if raw_parts else None
        combined_api_error_simplified = "\n".join(simplified_parts) if simplified_parts else None
        if not all_success:
            summary_err = (f"{len(created_keys)}/{len(pos)} Sales Orders created. "
                           "Failed: " + "; ".join(
                               f"{r.get('po_number')} ({r.get('error')})"
                               for r in sub if not r.get("success")))

        log.info("═══ Multi-PO DONE: %s → %d/%d orders created (%.0f ms) ═══",
                 object_name, len(created_keys), len(pos), elapsed)
        first = sub[0] if sub else {}
        return ProcessingResult(
            object_name=object_name, success=all_success,
            status=status, error=summary_err,
            api_error_raw=combined_api_error_raw,
            api_error_simplified=combined_api_error_simplified,
            order_key=created_keys[0] if created_keys else None,
            order_keys=created_keys,
            customer_name=first.get("customer_name"),
            business_unit_name=first.get("business_unit_name"),
            transaction_number=", ".join(po_numbers),
            currency_code=first.get("currency_code"),
            confidence=first.get("confidence"),
            lines=combined_lines, par_url=par_url,
            extracted={"purchase_orders": pos},
            sub_results=sub_trimmed,
            ship_to_address=first.get("ship_to_address"),
            ship_to_source=first.get("ship_to_source"),
            elapsed_ms=elapsed,
        )

    # ── Shared attachment / move (best-effort; never fatal) ──────────────────
    def _finalize_attachment(self, object_name: str, pdf_bytes: bytes,
                             order_keys: list[str], po_number: str | None) -> str | None:
        """Attach the source PDF (URL or FILE mode) to every created order and
        move the PDF to processed/ exactly once. Best-effort: attachment/move
        problems are logged but never fail the run (the orders already exist)."""
        mode = (settings.sales_order.attachment_mode or "url").lower()
        file_name = object_name.rsplit("/", 1)[-1]
        title = file_name.rsplit(".", 1)[0]
        par_url = None
        if not order_keys:
            return None
        try:
            if mode == "url":
                processed_name = self.storage.move_to_processed(object_name)
                par_url = self.storage.create_par_url(
                    object_name=processed_name,
                    ttl_seconds=settings.sales_order.par_ttl_seconds,
                )
                for ok in order_keys:
                    try:
                        self.so_client.attach_url(
                            order_key=ok, url=par_url, title=title,
                            description=f"Source PO PDF for {po_number}")
                    except Exception as ae:
                        log.warning("URL attach failed for order %s: %s", ok, ae)
            else:  # FILE mode
                for ok in order_keys:
                    try:
                        self.so_client.attach_pdf(
                            order_key=ok, pdf_bytes=pdf_bytes, file_name=file_name,
                            title=title, description=f"Source PO PDF for {po_number}")
                    except Exception as ae:
                        log.warning("File attach failed for order %s: %s", ok, ae)
                self.storage.move_to_processed(object_name)
        except Exception as exc:
            log.warning("Finalize attachment/move had a problem for %s: %s "
                        "(orders already created in Oracle)", object_name, exc)
        return par_url

    def _move_error_and_notify(self, object_name: str, data: dict | None,
                               error: str | None) -> None:
        """Move the PDF to error/ then trigger the OIC error notification."""
        try:
            log.info("Moving %s to error/ folder", object_name)
            self.storage.move_to_error(object_name)
        except Exception as exc:
            log.error("Could not move %s to error/: %s", object_name, exc)
        self._notify_error(object_name, data, error or "")

    def _handle_pdf_failure(self, object_name: str, extracted: dict | None,
                            exc: Exception, t_start: float) -> ProcessingResult:
        """Handle a failure that happened before/around extraction (whole-PDF)."""
        log.error("═══ Processing FAILED: %s after %.0f ms: %s ═══",
                  object_name, (time.perf_counter() - t_start) * 1000.0, exc,
                  exc_info=True)
        raw_error = exc.body if isinstance(exc, SalesOrderAPIError) and exc.body else str(exc)
        simplified = ""
        try:
            simplified = self.extractor.simplify_error(
                raw_error,
                context={"PO Number": (extracted or {}).get("SourceTransactionNumber"),
                         "Customer": (extracted or {}).get("CustomerName")})
        except Exception as se:
            log.warning("Error simplification step failed: %s", se)

        self._move_error_and_notify(object_name, extracted, raw_error)

        return ProcessingResult(
            object_name=object_name, success=False, status="error",
            error=str(exc), api_error_raw=raw_error, api_error_simplified=simplified,
            customer_name=(extracted or {}).get("CustomerName"),
            business_unit_name=(extracted or {}).get("BusinessUnitName"),
            transaction_number=(extracted or {}).get("SourceTransactionNumber"),
            currency_code=(extracted or {}).get("TransactionalCurrencyCode"),
            confidence=(extracted or {}).get("_confidence"),
            lines=(extracted or {}).get("lines"),
            extracted=extracted,
            elapsed_ms=(time.perf_counter() - t_start) * 1000.0,
        )

    def reprocess(self, extracted: dict[str, Any], object_name: str) -> ProcessingResult:
        """Re-run the back half of the pipeline from an (edited) PO dict, WITHOUT
        re-downloading or re-extracting the PDF. Used by the Order workbench's
        "Reprocess" button after a user fixes the data on a failed row.

        Because it routes through the same core as the live pipeline, editing the
        ship-to address (or any field) re-runs the FULL address-matching steps
        (2a→2d) and UOM conversion automatically.
        """
        with per_file_log(f"reprocess_{object_name}") as log_path:
            log.info("═══ REPROCESS START: %s ═══", object_name)
            t_start = time.perf_counter()

            # If the edited record wraps multiple POs, reprocess each of them.
            pos = normalize_purchase_orders(extracted)
            po = pos[0] if pos else extracted

            with _step("R", "Validate → duplicate → enrich → match → UOM → create",
                       object_name):
                core = self._create_so_for_po(po, object_name, f"reprocess_{object_name}")
            elapsed = (time.perf_counter() - t_start) * 1000.0

            if core["status"] == "duplicate":
                return ProcessingResult(
                    object_name=object_name, success=False, status="duplicate",
                    error=core.get("error"), existing_orders=core.get("existing_orders"),
                    customer_name=core.get("customer_name"),
                    business_unit_name=core.get("business_unit_name"),
                    transaction_number=core.get("po_number"),
                    currency_code=core.get("currency_code"),
                    lines=po.get("lines"), extracted=po, log_file=log_path,
                    elapsed_ms=elapsed,
                )

            if not core["success"]:
                # Leave the PDF in error/; do not re-notify on a manual retry.
                log.warning("REPROCESS did not create an order for %s: %s",
                            object_name, core.get("error"))
                return ProcessingResult(
                    object_name=object_name, success=False, status="error",
                    error=core.get("error"), api_error_raw=core.get("api_error_raw"),
                    api_error_simplified=core.get("api_error_simplified"),
                    customer_name=core.get("customer_name"),
                    business_unit_name=core.get("business_unit_name"),
                    transaction_number=core.get("po_number"),
                    currency_code=core.get("currency_code"),
                    lines=po.get("lines"), extracted=po, payload=core.get("payload"),
                    ship_to_address=core.get("ship_to_address"),
                    ship_to_source=core.get("ship_to_source"),
                    ship_to_match_method=core.get("ship_to_match_method"),
                    log_file=log_path, elapsed_ms=elapsed,
                )

            # Success — move the PDF out of error/ to processed/ and attach a PAR.
            order_key = core.get("order_key")
            par_url = None
            try:
                file_name = object_name.rsplit("/", 1)[-1]
                err_obj = settings.storage.error_folder + file_name
                processed_name = self.storage.move_to_processed(err_obj)
                if order_key and (settings.sales_order.attachment_mode or "url").lower() == "url":
                    par_url = self.storage.create_par_url(
                        object_name=processed_name,
                        ttl_seconds=settings.sales_order.par_ttl_seconds)
                    self.so_client.attach_url(
                        order_key=str(order_key), url=par_url,
                        title=file_name.rsplit(".", 1)[0],
                        description=f"Source PO PDF for {core.get('po_number')}")
            except Exception as attach_exc:
                log.warning("Reprocess: order created but PDF move/attach failed: %s",
                            attach_exc)

            log.info("═══ REPROCESS DONE: %s → OrderKey=%s ═══", object_name, order_key)
            return ProcessingResult(
                object_name=object_name, success=True, status="success",
                order_key=order_key, header_id=core.get("header_id"),
                order_keys=[order_key] if order_key else [],
                customer_name=core.get("customer_name"),
                business_unit_name=core.get("business_unit_name"),
                transaction_number=core.get("po_number"),
                currency_code=core.get("currency_code"),
                confidence=core.get("confidence"),
                lines=core.get("lines"), par_url=par_url, extracted=po,
                payload=core.get("payload"), api_response=core.get("api_response"),
                bi_data=core.get("bi_data"),
                ship_to_address=core.get("ship_to_address"),
                ship_to_source=core.get("ship_to_source"),
                ship_to_match_method=core.get("ship_to_match_method"),
                log_file=log_path, elapsed_ms=elapsed,
            )

    def run(self) -> list[ProcessingResult]:
        """
        Main entry point: find all PDFs in POPDFS/ and process them in parallel.
        Returns a summary of results.
        """
        pdf_objects = list(self.storage.list_pdf_objects())
        if not pdf_objects:
            log.info("No PDFs found in %s — nothing to process.",
                     settings.storage.input_folder)
            return []

        log.info("Found %d PDF(s) to process.", len(pdf_objects))
        return self.process_batch(pdf_objects)

    def process_batch(self, object_names: list[str]) -> list[ProcessingResult]:
        """Process several PDFs concurrently using a thread pool.

        Each worker thread uses its OWN orchestrator instance (and therefore its
        own OCI / Oracle clients), because the underlying SDK clients are not
        guaranteed thread-safe. The pipeline is I/O-bound (network calls to
        GenAI, BIP, and Oracle), so threads give a real throughput gain.

        Concurrency is capped by settings.processing.max_workers (default 6).
        """
        if not object_names:
            return []

        max_workers = max(1, settings.processing.max_workers)
        # Don't spin up more workers than there are files.
        workers = min(max_workers, len(object_names))
        log.info("Processing %d PDF(s) with %d parallel worker(s).",
                 len(object_names), workers)

        results: list[ProcessingResult] = []

        def _worker(obj_name: str) -> ProcessingResult:
            # Fresh orchestrator per task → fresh clients → thread-safe.
            local = POAutomationOrchestrator()
            return local.process_one(obj_name)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_obj = {pool.submit(_worker, obj): obj for obj in object_names}
            for future in as_completed(future_to_obj):
                obj = future_to_obj[future]
                try:
                    result = future.result()
                except Exception as exc:
                    # A worker raising is unexpected (process_one catches its own
                    # errors), but guard anyway so one bad file can't kill the batch.
                    log.error("Unhandled error processing %s: %s", obj, exc)
                    result = ProcessingResult(object_name=obj, success=False,
                                              error=f"Unhandled error: {exc}")
                results.append(result)
                _log_result(result)

        success_count = sum(1 for r in results if r.success)
        log.info("Batch complete: %d/%d succeeded.", success_count, len(results))
        return results


# ── Helpers ──────────────────────────────────────────────────────────────────
def _validate_extracted(data: dict[str, Any]) -> None:
    """Raise ValueError for any field that GenAI failed to extract."""
    required = ["SourceTransactionNumber", "CustomerName"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        raise ValueError(f"GenAI extraction missing required fields: {missing}")
    if not data.get("lines"):
        raise ValueError("GenAI extraction returned no order lines")


def _log_result(r: ProcessingResult) -> None:
    if r.success:
        log.info("✓ %s → OrderKey=%s HeaderId=%s", r.object_name, r.order_key, r.header_id)
    else:
        log.warning("✗ %s → ERROR: %s", r.object_name, r.error)