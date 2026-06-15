"""
extractor.py
============
Step 1 – Extract raw text from a PDF using pdfplumber (newline-structured).
Step 2 – Pass that text to Google Gemini 2.5 Pro (via OCI Generative AI)
         to get structured JSON.
"""
from __future__ import annotations

import json
import logging
import os
from io import BytesIO
from typing import Any

import pdfplumber
import oci
from oci.generative_ai_inference import GenerativeAiInferenceClient
from oci.generative_ai_inference.models import (
    ChatDetails,
    OnDemandServingMode,
    GenericChatRequest,
    UserMessage,
    TextContent,
    BaseChatRequest,
)

from config import GenAIConfig
from logging_setup import dump_json, dump_text

log = logging.getLogger(__name__)

# ── Prompt sent to GenAI ──────────────────────────────────────────────────────

_EXTRACTION_PROMPT = """\
You are an expert Purchase Order data extraction engine integrated with Oracle Fusion Order Management.
Your task is to read the raw text of a Purchase Order (PO) document and extract specific fields
into a structured JSON object for creating a Sales Order via Oracle Fusion REST API.

═══════════════════════════════════════════════════
STRICT OUTPUT RULES
═══════════════════════════════════════════════════
- Return ONLY a valid JSON object. No explanation, no markdown, no code fences.
- Do not wrap the output in ```json or ```.
- Do not add any commentary before or after the JSON.
- All string values must be properly escaped.
- Numbers must be unquoted integers or decimals (not strings).

═══════════════════════════════════════════════════
CRITICAL CONCEPT: BUYER vs SELLER
═══════════════════════════════════════════════════
A Purchase Order is a document the BUYER sends to the SELLER to request goods.
For Sales Order creation in Oracle Fusion, we need the BUYER (called the "Customer").

The two parties on every PO:
  • BUYER  = the one placing the order = the CUSTOMER (this is what we extract)
  • SELLER = the one fulfilling the order = the supplier/vendor (IGNORE for CustomerName)

The BUYER goes by many names depending on the document format. Treat ALL of these
labels as referring to the BUYER:
  "Buyer", "Bill-To", "Bill To", "Sold-To", "Sold To Customer",
  "Customer", "Customer Name", "Client", "Purchaser", "Ordered By",
  "From Partner", "Bill-To Party", "Ship To"  (see special rule below)

The SELLER goes by these labels — these are NEVER the customer:
  "Supplier", "Vendor", "Seller", "Manufacturer", "From",
  "To Partner", "Issued To", "Sold By"

═══════════════════════════════════════════════════
MULTIPLE PURCHASE ORDERS IN ONE DOCUMENT
═══════════════════════════════════════════════════
A single PDF may contain SEVERAL distinct purchase orders (you will see more
than one "Purchase Order" block, each with its OWN PO number, order date,
amount, ship-to address and item(s)). Each distinct PO number is a SEPARATE
purchase order and must become its OWN object.

- ALWAYS return a top-level "purchase_orders" array.
- If the document has ONE purchase order, return an array with ONE object.
- If it has N distinct PO numbers, return N objects — one per PO — each with its
  own header fields, its own ShipToAddress, and only the line(s) that belong to
  that PO. NEVER merge two different PO numbers into one object. NEVER split a
  single PO into several objects.
- Detect multiple POs by repeated "Purchase Order" / "PO Number" headers with
  different numbers. Lines/items printed under a PO block belong to that PO.

═══════════════════════════════════════════════════
FIELD EXTRACTION RULES
═══════════════════════════════════════════════════
(Apply the rules below to EACH purchase order object in the array.)

SourceTransactionNumber:
  - Look for: "PO Number", "Purchase Order Number", "Order Number",
    "Document Identifier", "Source PO Number", "PO No", "Order No"
  - Use the primary document reference number exactly as it appears.
  - If null, use null.

SourceTransactionId:
  - Always set this to the SAME value as SourceTransactionNumber.

SourceTransactionRevisionNumber:
  - Always set to integer 1 unless the document explicitly mentions a revision/version.

TransactionalCurrencyCode:
  - Look for: "Currency", "Currency Code", "Currency Code of Order"
  - Use the 3-letter ISO currency code (e.g. USD, EUR, GBP, INR).
  - Default to "USD" if not found.

TransactionTypeCode:
  - SKIP this field — it is hardcoded downstream. Omit or set to null.

RequestedShipDate:
  - Look for: "Requested Ship Date", "Ship Date", "Delivery Date",
    "Required Date", "Need By Date"
  - If multiple lines have different ship dates, use the EARLIEST date.
  - Convert ALL date formats to ISO 8601 UTC: "YYYY-MM-DDTHH:mm:ssZ"
  - If null, use null.

CustomerName:  ← MOST CRITICAL FIELD — read carefully

  Step 1 — Try in this priority order:
    (a) Find an explicit BUYER label: "Buyer", "Bill-To", "Sold-To",
        "Customer", "Customer Name", "Client", "From Partner".
        Use the name attached to it. DONE.

    (b) If no explicit buyer label exists BUT the document has a
        "Ship To" address, use the "Ship To" party name.
        Rationale: in a PO, goods ship to the buyer's location by default.

    (c) If neither (a) nor (b) yield a name, set CustomerName to null and
        set _confidence to "LOW".

  Step 2 — Apply these NEGATIVE rules (REJECT these as CustomerName):
    • NEVER use a name labeled "Supplier", "Vendor", "Seller",
      "Manufacturer", "From", "To Partner", "Issued To", or "Sold By".
    • NEVER use the email address domain or the name beside an email address
      (those usually belong to the document issuer / seller / contact person).
    • NEVER use a person's name unless it is clearly tagged as the customer
      AND no company name is present. Companies > individual contacts.
    • NEVER use the company that issued / authored / printed the document
      (often appears in the header next to the logo).

  Step 3 — Sanity check:
    If your candidate CustomerName ALSO appears next to "Supplier", "Vendor",
    "From", or similar seller labels — you picked the wrong party.
    Re-run Step 1.

BusinessUnitName:
  - Look for: "Business Unit", "BU", "Selling BU", "Org", "Organization",
    "Operating Unit"
  - This is the SELLER'S business unit (the BU that will own the Sales Order
    in Oracle), NOT the buyer's department.
  - On many PDFs this field will be NEAR the seller/supplier block.
  - Use the exact name as it appears.
  - If null, set _confidence to at least "MEDIUM".

ShipToAddress (object):  ← capture the destination address for address matching
  - Find the "Ship To" / "Ship-to Address" / "Deliver To" / "Delivery Address"
    block for this purchase order and break it into structured parts.
  - Extract these sub-fields (use null for any you cannot find):
      Name        — the ship-to party/site name (e.g. "Site 01", company name)
      AddressLine1— street address line 1
      AddressLine2— street address line 2 (suite/unit), else null
      City        — city / town
      State       — state / province / region
      PostalCode  — ZIP / postal code  (IMPORTANT for downstream matching)
      Country     — country or country code
      Raw         — the FULL ship-to address exactly as printed, one string
  - Capture the SHIP-TO address, not the bill-to or supplier address. If several
    ship-to addresses exist in the document, use the one for THIS purchase order.

lines (array — one object per order line):
  - Scan the entire document for a line-items table or
    "Order Create Details" / "Item Details" section.
  - Create one entry per line — never merge lines.
  - For each line extract:

    SourceTransactionLineId:
      - The line number as a string (e.g. "1", "2").

    SourceTransactionLineNumber:
      - Same as SourceTransactionLineId.

    SourceScheduleNumber:
      - Same as SourceTransactionLineId.

    SourceTransactionScheduleId:
      - Same as SourceTransactionLineId.

    ProductNumber:
      - Look for: "Buyer Product", "Manufacturer Product", "Item Code",
        "SKU", "Part Number", "Product ID", "Item Number"
      - Prefer "Buyer Product" over "Manufacturer Product" if both exist.
      - Use the CODE/NUMBER, not the descriptive name.

    OrderedQuantity:
      - Look for: "Quantity", "Qty", "Ordered Qty"
      - Must be a number. Do NOT wrap in quotes.

    OrderedUOMCode:
      - Look for: "UOM", "Unit", "Unit of Measure"
      - Preserve exactly as written. Default to "Each" if not found.

    OrderedUOMName:
      - The FULL, human-readable form of the unit of measure.
      - Intelligently identify what the UOM code means and expand it, e.g.
        "EA"/"EACH" -> "Each", "CS" -> "Case", "BX" -> "Box", "PK" -> "Pack",
        "CTN" -> "Carton", "PLT"/"PAL" -> "Pallet", "DZ" -> "Dozen",
        "KG" -> "Kilogram", "LB" -> "Pound", "L" -> "Litre", "M" -> "Metre",
        "ROL" -> "Roll", "SET" -> "Set", "PR" -> "Pair".
      - If the document already spells the unit out, use that spelling.
      - If you genuinely cannot tell, repeat the OrderedUOMCode value.

    ProductDescription:
      - The human-readable description / name of the item (NOT the code).
      - Look for: "Description", "Item Description", "Product Description",
        "Material Description", or the descriptive text next to/under the item code.
      - This is REQUIRED whenever any descriptive text exists for the line —
        capture the full description text exactly (do not truncate, do not put
        the item code here).
      - Use null only if there is genuinely no description text for the line.

_confidence:
  - A self-assessment of extraction quality.
  - Values:
      "HIGH"   — every required field found with explicit, unambiguous labels.
      "MEDIUM" — one or two fields inferred (e.g. used "Ship To" because no
                 explicit Buyer label existed; defaulted a missing field).
      "LOW"    — CustomerName could not be determined OR multiple seller/buyer
                 labels conflicted OR the PDF text was sparse/garbled.
  - Always include this field. The downstream pipeline may route LOW-confidence
    extractions for human review instead of auto-creating the Sales Order.

_extraction_notes:
  - A short array of human-readable notes (max 3 strings) explaining any
    judgment calls you made. Examples:
      ["No explicit Buyer label — used Ship To party as customer."]
      ["Two currencies mentioned (USD/EUR); used USD from header."]
      ["Document had no Business Unit; defaulted to null."]
  - Empty array [] if no notes needed.

═══════════════════════════════════════════════════
COMMON EXTRACTION PITFALLS — AVOID THESE
═══════════════════════════════════════════════════
- Do NOT use the "Supplier" or "Vendor" name as CustomerName — that is the SELLER.
- Do NOT use the email contact's name or domain as CustomerName.
- Do NOT use the company in the document header/logo as CustomerName (that's the issuer).
- Do NOT use the product description as ProductNumber — use the code only.
- Do NOT merge multiple line items into one entry.
- Do NOT guess values — if a field is genuinely absent, use null.
- Do NOT convert UOMCode — preserve exactly as written.

═══════════════════════════════════════════════════
WORKED EXAMPLE
═══════════════════════════════════════════════════
If the PDF says:
    Supplier - ACME Corp
    Email - contact@acme.com
    Ship To - Walkswagen
              123 Main St, Detroit

Then:
  CustomerName       = "Walkswagen"            ← Ship To, because no Buyer label
  (NOT "ACME Corp"   ← that's the seller / supplier)
  (NOT "contact"     ← that's an email address)
  _confidence        = "MEDIUM"                ← inferred from Ship To
  _extraction_notes  = ["No explicit Buyer label; used Ship To party 'Walkswagen' as customer."]

═══════════════════════════════════════════════════
EXPECTED OUTPUT FORMAT
═══════════════════════════════════════════════════
Return a top-level object with a "purchase_orders" array (ONE element per
distinct PO number — see the MULTIPLE PURCHASE ORDERS section above):
{
  "purchase_orders": [
    {
      "SourceTransactionNumber":         "<PO Number>",
      "SourceTransactionId":             "<same as SourceTransactionNumber>",
      "SourceTransactionRevisionNumber": 1,
      "TransactionalCurrencyCode":       "<3-letter currency code>",
      "TransactionTypeCode":             null,
      "RequestedShipDate":               "<YYYY-MM-DDTHH:mm:ssZ>",
      "CustomerName":                    "<Buyer name>",
      "BusinessUnitName":                "<Business Unit name or null>",
      "ShipToAddress": {
        "Name":         "<ship-to site/party name or null>",
        "AddressLine1": "<street line 1 or null>",
        "AddressLine2": "<street line 2 or null>",
        "City":         "<city or null>",
        "State":        "<state/province or null>",
        "PostalCode":   "<postal/ZIP code or null>",
        "Country":      "<country or null>",
        "Raw":          "<full ship-to address as printed>"
      },
      "_confidence":                     "HIGH" | "MEDIUM" | "LOW",
      "_extraction_notes":               ["<note 1>", "<note 2>"],
      "lines": [
        {
          "SourceTransactionLineId":      "<line number as string>",
          "SourceTransactionLineNumber":  "<line number as string>",
          "SourceScheduleNumber":         "<line number as string>",
          "SourceTransactionScheduleId":  "<line number as string>",
          "ProductNumber":                "<product code>",
          "ProductDescription":           "<item description or null>",
          "OrderedQuantity":              <number>,
          "OrderedUOMCode":               "<unit of measure code>",
          "OrderedUOMName":               "<full form of the unit of measure>"
        }
      ]
    }
  ]
}

═══════════════════════════════════════════════════
PO DOCUMENT TEXT TO EXTRACT FROM:
═══════════════════════════════════════════════════

"""


def normalize_purchase_orders(extracted: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of per-PO dicts from the model output.

    The prompt now asks for a top-level {"purchase_orders": [...]} array. This
    helper accepts that AND the legacy single-object format, so the rest of the
    pipeline can always iterate over a list:

      • {"purchase_orders": [PO, PO, ...]}  -> that list
      • a single PO object (legacy)         -> [that object]

    Each returned element is a standalone PO dict in the shape the downstream
    build_payload / enrichment code already expects (CustomerName, lines, …)
    plus an optional ShipToAddress object.
    """
    if not isinstance(extracted, dict):
        return []
    pos = extracted.get("purchase_orders")
    if isinstance(pos, list) and pos:
        return [po for po in pos if isinstance(po, dict)]
    # Legacy / fallback: the whole object IS a single PO.
    if extracted.get("SourceTransactionNumber") or extracted.get("lines"):
        return [extracted]
    return []


class PDFExtractor:
    def __init__(self, cfg: GenAIConfig) -> None:
        self.cfg = cfg
        # Load the OCI config from the SAME location the rest of the app uses.
        # On a server (Render, etc.) there is no ~/.oci/config, so we honour the
        # OCI_CONFIG_PATH / OCI_PROFILE environment variables (e.g. a Render
        # Secret File at /etc/secrets/oci_config). Falls back to the local
        # default for development.
        oci_config = oci.config.from_file(
            file_location=os.getenv("OCI_CONFIG_PATH", "~/.oci/config"),
            profile_name=os.getenv("OCI_PROFILE", "DEFAULT"),
        )
        self._client = GenerativeAiInferenceClient(
            config=oci_config,
            service_endpoint=cfg.service_endpoint,
        )
        log.info("OCI GenAI client initialised (model=%s)", cfg.model_id)

    # ── Step 1: PDF → raw text (newline-structured) ──────────────────────────
    def extract_text(self, pdf_bytes: bytes) -> str:
        """Extract text from a PDF using pdfplumber, preserving line structure.

        Each page's text is extracted with explicit newlines between lines so
        the LLM sees the document's layout. Blank lines are collapsed but real
        line breaks are kept, which helps the model align labels with values
        and separate multi-line item tables.
        """
        text_parts: list[str] = []
        log.info("STEP 2 (extract_text): opening PDF (%d bytes) with pdfplumber",
                 len(pdf_bytes))
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            log.info("STEP 2 (extract_text): PDF has %d page(s)", page_count)
            for i, page in enumerate(pdf.pages, 1):
                # layout=True keeps the visual line/column structure; fall back
                # to plain extract_text if layout extraction returns nothing.
                page_text = page.extract_text(layout=True) or page.extract_text() or ""
                # Normalise: strip trailing spaces per line, drop empty lines,
                # re-join with single newlines so the structure is clean \n text.
                lines = [ln.rstrip() for ln in page_text.splitlines()]
                lines = [ln for ln in lines if ln.strip() != ""]
                cleaned = "\n".join(lines)
                log.debug("STEP 2 (extract_text): page %d/%d -> %d non-empty line(s), %d chars",
                          i, page_count, len(lines), len(cleaned))
                text_parts.append(f"[PAGE {i}]\n{cleaned}")
        full_text = "\n\n".join(text_parts)
        log.info("STEP 2 (extract_text): extracted %d characters total from PDF",
                 len(full_text))
        return full_text

    # ── Step 2: raw text → structured JSON via Gemini 2.5 Pro ─────────────────
    def extract_structured_data(self, raw_text: str,
                                source_label: str = "po") -> dict[str, Any]:
        """
        Send extracted PDF text to Gemini 2.5 Pro (via OCI Generative AI) and
        parse the returned JSON. Returns a dict with PO fields ready for merging.

        `source_label` (typically the source file/object name) is only used to
        name the artifact files written under logs/artifacts/ so you can tell
        which PO each dumped JSON belongs to.
        """
        prompt = _EXTRACTION_PROMPT + raw_text
        log.info(
            "STEP 3 (Gemini): calling model=%s (prompt=%d chars, max_tokens=%s, temp=%s)",
            self.cfg.model_id, len(prompt), self.cfg.max_tokens, self.cfg.temperature,
        )

        # Gemini uses the GENERIC chat format (not Cohere's). Build a single
        # user message with one text content part. Use the role-specific
        # UserMessage subclass — the SDK uses the message subtype as a
        # polymorphic discriminator, so a generic Message(role="USER") is not
        # equivalent and can be rejected by the service.
        user_message = UserMessage(
            content=[TextContent(text=prompt)],
        )
        chat_request = GenericChatRequest(
            api_format=BaseChatRequest.API_FORMAT_GENERIC,
            messages=[user_message],
            max_tokens=self.cfg.max_tokens,
            temperature=self.cfg.temperature,
            is_stream=False,
        )
        chat_details = ChatDetails(
            serving_mode=OnDemandServingMode(model_id=self.cfg.model_id),
            compartment_id=self.cfg.compartment_id,
            chat_request=chat_request,
        )

        response = self._client.chat(chat_details)
        log.info("STEP 3 (Gemini): response received; parsing content")

        # Generic chat response: text lives in
        # response.data.chat_response.choices[0].message.content[0].text
        raw_json_str = self._extract_generic_text(response).strip()
        log.info("STEP 3 (Gemini): raw model output = %d chars", len(raw_json_str))

        # Persist the raw model output to the backend BEFORE parsing, so even if
        # JSON parsing fails the exact text the model returned is on disk.
        raw_path = dump_text(raw_json_str, f"genai_raw_{source_label}", suffix="txt")
        if raw_path:
            log.info("STEP 3 (Gemini): raw output saved -> %s", raw_path)

        # Strip any accidental markdown fences the model may add
        if raw_json_str.startswith("```"):
            parts = raw_json_str.split("```")
            if len(parts) >= 2:
                raw_json_str = parts[1]
            if raw_json_str.startswith("json"):
                raw_json_str = raw_json_str[4:]
        raw_json_str = raw_json_str.strip()

        try:
            extracted: dict[str, Any] = json.loads(raw_json_str)
        except json.JSONDecodeError as exc:
            log.error("STEP 3 (Gemini): returned non-JSON: %s", raw_json_str[:500])
            # Raw text was already dumped above (raw_path); point the user to it.
            raise ValueError(
                f"Gemini response is not valid JSON: {exc}"
                + (f" (raw output saved to {raw_path})" if raw_path else "")
            ) from exc

        # ── Persist + log the structured JSON ────────────────────────────────
        # 1) Save the parsed JSON to the backend ("download" the JSON).
        json_path = dump_json(extracted, f"genai_{source_label}")
        if json_path:
            log.info("STEP 3 (Gemini): structured JSON saved -> %s", json_path)
        # 2) Print the full JSON into the logs as well.
        log.info(
            "STEP 3 (Gemini): structured JSON for %s:\n%s",
            source_label,
            json.dumps(extracted, indent=2, ensure_ascii=False, default=str),
        )

        confidence = extracted.get("_confidence", "UNKNOWN")
        notes      = extracted.get("_extraction_notes", []) or []
        log.info(
            "STEP 3 (Gemini) summary: TxnNumber=%s, Customer=%s, BU=%s, Lines=%d, confidence=%s",
            extracted.get("SourceTransactionNumber"),
            extracted.get("CustomerName"),
            extracted.get("BusinessUnitName"),
            len(extracted.get("lines", [])),
            confidence,
        )
        if confidence == "LOW":
            log.warning(
                "Extraction confidence is LOW — review before relying on this result."
            )
        for note in notes:
            log.info("Extraction note: %s", note)
        return extracted

    @staticmethod
    def _extract_generic_text(response: Any) -> str:
        """Pull the text out of a generic-chat response defensively.

        The expected path is:
          response.data.chat_response.choices[0].message.content[0].text
        but we guard each step so a shape change fails with a clear message.
        """
        try:
            chat_resp = response.data.chat_response
            choices = getattr(chat_resp, "choices", None)
            if not choices:
                raise ValueError("no choices in response")
            message = choices[0].message
            content = getattr(message, "content", None)
            if not content:
                raise ValueError("no content in message")
            # content is a list of content parts; concatenate any text parts
            texts = [getattr(part, "text", "") for part in content]
            joined = "".join(t for t in texts if t)
            if not joined:
                raise ValueError("no text in content parts")
            return joined
        except Exception as exc:
            log.error("Could not parse Gemini generic-chat response: %s", exc)
            raise ValueError(f"Unexpected Gemini response shape: {exc}") from exc

    # ── Combined entry point ──────────────────────────────────────────────────
    def process_pdf(self, pdf_bytes: bytes, source_label: str = "po") -> dict[str, Any]:
        """Full pipeline: bytes → text → structured dict.

        `source_label` is propagated to the artifact filenames written for the
        Gemini output so each dump is traceable back to its source PDF.
        """
        raw_text = self.extract_text(pdf_bytes)
        return self.extract_structured_data(raw_text, source_label=source_label)

    # ── GenAI ship-to address matcher (address matching step 2d) ─────────────
    def match_address_via_ai(self, pdf_address_text: str, candidates: list) -> int | None:
        """Ask Gemini to pick which BI-report address best matches the PDF
        ship-to address. `candidates` is a list of AddressCandidate objects
        (anything with .full_text()). Returns the chosen 0-based index, or None
        if the model can't decide. Best-effort — never raises.
        """
        if not candidates:
            return None
        listing = "\n".join(
            f"{i}: {getattr(c, 'full_text', lambda: str(c))()}"
            for i, c in enumerate(candidates)
        )
        prompt = (
            "You are matching a shipping address taken from a purchase order PDF "
            "to the correct address from a list of known customer addresses.\n\n"
            f"PDF ship-to address:\n{pdf_address_text}\n\n"
            f"Candidate addresses (index: address):\n{listing}\n\n"
            "Return ONLY the single integer index of the candidate that refers to "
            "the SAME physical address as the PDF address. Consider abbreviations, "
            "casing, punctuation and minor spelling differences. If none clearly "
            "matches, return -1. Output just the number, nothing else."
        )
        try:
            user_message = UserMessage(content=[TextContent(text=prompt)])
            chat_request = GenericChatRequest(
                api_format=BaseChatRequest.API_FORMAT_GENERIC,
                messages=[user_message],
                max_tokens=20, temperature=0.0, is_stream=False,
            )
            chat_details = ChatDetails(
                serving_mode=OnDemandServingMode(model_id=self.cfg.model_id),
                compartment_id=self.cfg.compartment_id,
                chat_request=chat_request,
            )
            response = self._client.chat(chat_details)
            text = self._extract_generic_text(response).strip()
            import re as _re
            m = _re.search(r"-?\d+", text)
            if not m:
                return None
            idx = int(m.group(0))
            return idx if idx >= 0 else None
        except Exception as exc:
            log.warning("GenAI address match failed: %s", exc)
            return None

    # ── GenAI ship-to address validator (used before accepting/failing) ──────
    def validate_address_via_ai(self, pdf_address_text: str, candidate) -> bool | None:
        """Ask Gemini a yes/no question: is the PDF ship-to address the SAME
        physical place as this one candidate? Returns True / False / None
        (None = couldn't decide). Best-effort — never raises.

        This is the "validator" role: it confirms a pick the scoring step is
        unsure about, and it runs before we either accept a medium-confidence
        match or fail the PO. Catching a semantic mismatch here (e.g. "Green
        Cove" vs "Green Vistas") is exactly what stops a wrong-address order.
        """
        cand_text = getattr(candidate, "full_text", lambda: str(candidate))()
        prompt = (
            "Decide whether two shipping addresses refer to the SAME physical "
            "place. Allow for abbreviations, casing, punctuation and minor spelling "
            "differences, but a DIFFERENT building/premise name or a different "
            "house/plot number means they are NOT the same.\n\n"
            f"Address A (from the PO PDF):\n{pdf_address_text}\n\n"
            f"Address B (on file in Oracle):\n{cand_text}\n\n"
            "Answer with exactly one word: YES if they are the same place, or NO "
            "if they are not. Output only YES or NO."
        )
        try:
            user_message = UserMessage(content=[TextContent(text=prompt)])
            chat_request = GenericChatRequest(
                api_format=BaseChatRequest.API_FORMAT_GENERIC,
                messages=[user_message],
                max_tokens=2000, temperature=0.0, is_stream=False,
            )
            chat_details = ChatDetails(
                serving_mode=OnDemandServingMode(model_id=self.cfg.model_id),
                compartment_id=self.cfg.compartment_id,
                chat_request=chat_request,
            )
            response = self._client.chat(chat_details)
            text = self._extract_generic_text(response).strip().upper()
            if "YES" in text and "NO" not in text:
                return True
            if "NO" in text:
                return False
            return None
        except Exception as exc:
            log.warning("GenAI address validation failed: %s", exc)
            return None

    # ── Error simplification (extra layer for the UI) ─────────────────────────
    def simplify_error(self, raw_error: str, context: dict[str, Any] | None = None) -> str:
        """Turn a raw Oracle API error into a short, plain-English explanation.

        Reuses the same OCI GenAI (Gemini) client. Best-effort: if the call
        fails for any reason it returns an empty string so the pipeline is never
        blocked by the simplifier. `context` (optional) can carry PO number /
        customer to make the explanation more specific.
        """
        if not raw_error:
            return ""
        ctx = ""
        if context:
            bits = [f"{k}={v}" for k, v in context.items() if v]
            if bits:
                ctx = "\nContext: " + ", ".join(bits)
        prompt = (
            "You are helping a non-technical business user understand why creating "
            "a Sales Order in Oracle Fusion failed. Read the raw API error below and "
            "explain, in 2-4 short plain-English sentences: (1) what went wrong, and "
            "(2) EXACTLY which field/parameter the user should readjust to fix it "
            "(name the specific field, e.g. 'Product Number', 'Unit of Measure', "
            "'Currency Code', 'Business Unit', or the price-list/item setup), and what "
            "to change it to if you can tell. Do not output JSON, code, stack traces, "
            "raw error codes, or technical jargon. Be specific and actionable. "
            "IMPORTANT: write COMPLETE sentences and finish your explanation fully — "
            "do not stop mid-sentence."
            f"{ctx}\n\nRaw error:\n{raw_error}"
        )
        try:
            user_message = UserMessage(content=[TextContent(text=prompt)])
            chat_request = GenericChatRequest(
                api_format=BaseChatRequest.API_FORMAT_GENERIC,
                messages=[user_message],
                # Gemini 2.5 Pro is a "thinking" model and spends part of the token
                # budget on internal reasoning before emitting the answer. A small
                # budget (e.g. 500) gets consumed by that reasoning and the visible
                # explanation is cut off mid-sentence. Give it ample room so the
                # full explanation always comes through.
                max_tokens=4000,
                temperature=0.2,
                is_stream=False,
            )
            chat_details = ChatDetails(
                serving_mode=OnDemandServingMode(model_id=self.cfg.model_id),
                compartment_id=self.cfg.compartment_id,
                chat_request=chat_request,
            )
            response = self._client.chat(chat_details)
            simplified = self._extract_generic_text(response).strip()
            log.info("Error simplified by Gemini (%d chars)", len(simplified))
            return simplified
        except Exception as exc:  # never let the simplifier break the flow
            log.warning("Could not simplify error via Gemini: %s", exc)
            return ""