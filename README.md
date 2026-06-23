# Order Navigator — PO → SO Converter

Turns **Purchase Order PDFs** into **Sales Orders** in Oracle Fusion Cloud ERP,
automatically. A PDF lands in an Object Storage bucket, the app reads it with AI,
enriches it with data from Oracle BI Publisher reports, builds the Sales Order
payload, creates the order through the Fusion REST API, and attaches the source
PDF back to the order.

This README explains **what happens at every step and how the pieces fit
together** (the architecture), in plain language.

---

## 1. The big picture

```
                 +-------------------------------------------------------------+
                 |                     Order Navigator (app)                   |
                 |                                                             |
  Email inbox    |   +------------+   +-----------+   +----------------------+ |
  --(OIC)--> POPDFS/  | Storage    |   | Extractor |   | Orchestrator         | |
  bucket folder  |   | (download) |-->| (AI read) |-->| (runs all the steps) | |
                 |   +------------+   +-----------+   +----------+-----------+ |
                 |                                              |             |
                 |        +-------------------------------------+----------+  |
                 |        v                 v                    v          |  |
                 |  +-----------+    +--------------+    +--------------+    |  |
                 |  | BI Report |    | UOM converter|    | Address      |    |  |
                 |  | (enrich)  |    | (YAML rules) |    | matcher      |    |  |
                 |  +-----------+    +--------------+    +--------------+    |  |
                 |        |                                                 |  |
                 |        v                                                 |  |
                 |  +-------------+   +--------------+   +-----------------+ |  |
                 |  | Sales Order |-->| Oracle Fusion|   | OIC error notify| |  |
                 |  | (build+POST)|   | REST API     |   | (on failure)    | |  |
                 |  +-------------+   +--------------+   +-----------------+ |  |
                 |                                                          |  |
                 |  Streamlit UI: Dashboard . Upload . Workbench . UOM page |  |
                 +-------------------------------------------------------------+

External systems: OCI Object Storage . OCI Generative AI (Gemini 2.5 Pro) .
Oracle BI Publisher . Oracle Fusion Sales Order REST API . Oracle Integration Cloud (OIC).
```

---

## 2. The components (who does what)

| Module | Job (simple) |
|---|---|
| `config.py` | One place for every setting/credential (env-var overridable). |
| `storage.py` | Talks to OCI Object Storage: list, download, move, PAR links. |
| `extractor.py` | PDF -> text (pdfplumber) -> structured JSON (Gemini). Also AI helpers for address validation and error explanations. |
| `bi_report.py` | Calls BI Publisher reports: customer + all ship-to addresses, and the item cross-reference. |
| `uom_converter.py` | Reads `data/uom_conversions.yaml`; converts a line's quantity + UOM. |
| `address_matcher.py` | Picks the correct ship-to address out of many. |
| `sales_order.py` | Builds the SO payload, checks duplicates, POSTs the order, attaches the PDF. |
| `oic_client.py` | Triggers OIC integrations (inbox pull; **error notification**; **new-ship-to-address approval notification**). |
| `orchestrator.py` | The conductor - runs every step in order, in parallel where useful. |
| `app.py` + `pages/` | The Streamlit UI (dashboard, upload, history, workbench, UOM editor). |

---

## 3. The pipeline, step by step (architecture of each step)

Each step below shows: **what it does**, the **module/function**, and the
**input -> output** so you can see how data flows from one step to the next.

### Step 0 - (Optional) Pull POs from the email inbox
- **What:** Hitting an OIC integration reads the email inbox and drops PO PDFs
  into the `POPDFS/` bucket folder.
- **Arch:** UI button -> `oic_client.OICClient.trigger()` -> OIC -> bucket.
- **In -> Out:** *(button click)* -> PDFs appear in `POPDFS/`.

### Step 1 - Download the PDF
- **What:** Read the PDF bytes from Object Storage.
- **Arch:** `orchestrator._run_pipeline()` -> `storage.download_pdf(object_name)`.
- **In -> Out:** object name -> raw PDF bytes.

### Step 2+3 - Read the PDF with AI (extraction)
- **What:** Convert the PDF to text, then ask Gemini to return structured JSON.
  The prompt now also captures the **item description**, the **ship-to address**
  (as a structured object), the **UOM full form** (`OrderedUOMName`), and detects
  **multiple PO numbers**.
- **Arch:** `extractor.process_pdf()` -> pdfplumber (text) -> Gemini (JSON).
- **In -> Out:** PDF bytes -> a dict, always normalised to a
  `{"purchase_orders": [ ... ]}` array.

### Step 3a - Normalise to a list of POs
- **What:** Treat the document as a list of purchase orders (1 or many).
- **Arch:** `extractor.normalize_purchase_orders(extracted)`.
- **In -> Out:** extraction dict -> `list[PO dict]`.
- **Branch:** **1 PO** -> single path (`_run_single`). **N POs** -> parallel path
  (`_run_multi`, one Sales Order per PO, created concurrently).

> Everything below (Steps 3b-6) runs **per purchase order**, inside
> `orchestrator._create_so_for_po(po, ...)`.

### Step 3b - Validate the extracted fields
- **What:** Make sure the must-have fields exist (PO number, customer, lines).
- **Arch:** `orchestrator._validate_extracted(po)`.
- **In -> Out:** PO dict -> ok, or raises (-> error path).

### Step 3c - Duplicate guard
- **What:** Ask Oracle whether a Sales Order already exists for this Customer PO
  number, so we never create a duplicate.
- **Arch:** `sales_order.check_duplicate(po_number)` -> Fusion REST GET.
- **In -> Out:** PO number -> `exists?`; if yes, stop and mark **duplicate**.

### Step 4 - Enrichment (run in parallel)
- **What:** Fetch the customer's **base fields + ALL ship-to addresses** and the
  **item cross-reference** map, at the same time.
- **Arch:** `orchestrator._fetch_enrichment()` runs two BI calls in a thread pool:
  `bi_report.fetch_customer_data_and_addresses(customer_name)` (one parameter:
  Customer Name) and `bi_report.fetch_item_xref(...)`.
- **In -> Out:** customer name -> `(base_fields, address_rows, xref_map)`.

### Step 4a - Item cross-reference (remap item numbers)
- **What:** Swap the customer's item number (from the PDF) for the internal
  Fusion item number. Runs **before** UOM conversion.
- **Arch:** `orchestrator._apply_item_xref(po, xref_map)`.
- **In -> Out:** PO lines -> PO lines with `ProductNumber` remapped; the original
  is kept as `CustomerItemNumber`.

### Step 4b - Ship-to address matching
- **What:** Pick the single correct ship-to address out of the many the customer
  has on file. **Postal code only narrows; the address content confirms.**
- **Arch:** `orchestrator._resolve_ship_to()` ->
  `address_matcher.match_ship_to_address(pdf_addr, candidates, ai_pick, ai_validate)`.
  - Narrow by postal code (safety net: if none match, score them all).
  - Score each candidate: hard-reject on a different city or conflicting
    building/plot numbers; weigh the rare **distinguishing words** ("cove" vs
    "vistas") more than shared ones ("godrej", "green").
  - Confidence bands: **HIGH + clear winner ->** accept; **otherwise ->** ask the
    AI to confirm; **medium but unconfirmed ->** needs review; **low ->** AI
    pick + confirm; **nothing confident ->** fail.
- **In -> Out:** PDF ship-to + candidate list ->
  `ShipToPartyId` + `ShipToPartySiteId` (merged into `bi_data`), **or** no match
  (-> error path; the UI shows the PDF address + closest candidates).
- **New-address notification:** when the matcher **gives up entirely**
  (`method == "none"` - nothing on file is even close, as opposed to
  `needs_review` where a similar address exists and a human just picks it),
  this is treated as a likely **brand-new ship-to address** and triggers a
  separate OIC notification asking a human to approve it before any Sales
  Order can be created against it. See §11.

### Step 4c - UOM conversion
- **What:** Convert the line's quantity + unit using rules from the YAML file
  (e.g. `1 CS -> 10 EA`). Runs **after** the item cross-reference.
- **Arch:** `orchestrator._apply_uom(po)` ->
  `uom_converter.apply_uom_conversions(lines, customer_name)` reading
  `data/uom_conversions.yaml`.
- **In -> Out:** PO lines -> lines with converted `OrderedQuantity` and
  `OrderedUOMCode` set to the YAML **`uom_sold`** code.

### Step 5 - Build the Sales Order payload
- **What:** Merge the AI-extracted fields + BI enrichment into the exact JSON the
  Fusion API expects.
- **Arch:** `sales_order.build_payload(po, bi_data)`.
- **In -> Out:** PO + `bi_data` -> SO payload dict.
- **Note (ship-to):** `shipToCustomer` sends **`PartyId` + `SiteId` only**.
  `SiteUseId` is **not** sent - Oracle derives the site-use from PartyId + SiteId.
  Each converted line's `OrderedUOMCode` is the YAML `uom_sold` code.

### Step 6 - Create the Sales Order
- **What:** POST the payload to Fusion.
- **Arch:** `sales_order.create_order(payload)` -> Fusion REST POST.
- **In -> Out:** payload -> `{OrderKey, HeaderId, ...}` (success) or an error with
  the raw Oracle message.

### Step 6b - Attach the source PDF & move the file
- **What:** Attach the PDF to the created order, then move it out of the inbox.
- **Arch:** `orchestrator._finalize_attachment()`:
  - **URL mode:** move PDF to `processed/`, create a PAR link, attach the link.
  - **FILE mode:** attach the PDF bytes (base64), then move to `processed/`.
  - For multi-PO, the **one** PDF is attached to **every** created order, then
    moved once.
- **In -> Out:** order key(s) + PDF -> attachment created; file in `processed/`.

### Step 7 - On any failure: move to error + notify
- **What:** If a step fails, move the PDF to `error/` and trigger the OIC error
  notification.
- **Arch:** `orchestrator._move_error_and_notify()` ->
  `storage.move_to_error()` then `oic_client.trigger_error_notification(context)`.
- **In -> Out:** failure -> file in `error/`; one best-effort notification with PO
  number, customer, file name, and a short error.

---

## 4. Multiple POs in one PDF (parallel Sales Orders)

```
            normalize_purchase_orders()
PDF --> extract --> [PO #23, PO #24, PO #25]
                         |      |      |      (thread pool - created in parallel)
                         v      v      v
                   _create_so_for_po (each: validate->dup->enrich->match->UOM->build->create)
                         |      |      |
                         +------+------+--> attach the SAME PDF to all orders, move once
```
- If **all** POs succeed -> file moves to `processed/`.
- If **some** fail -> the successful orders are kept, the file stays with them,
  and an error notification is sent for the failed ones.
- If **none** succeed -> file moves to `error/` + notification.

---

## 5. Reprocess flow (fixing an errored PO)

- **What:** On the **Order Workbench**, an errored row is editable (customer, PO
  number, currency, **ship-to address**, and the line grid incl. **UOM full
  form**). Clicking **Reprocess** runs the back half of the pipeline again,
  without re-reading the PDF.
- **Arch:** `app.py` -> `orchestrator.reprocess(edited_po, object_name)` -> the
  **same** `_create_so_for_po` core. So editing the ship-to address **re-runs the
  whole address-matching flow**, and changed quantities/UOMs re-run conversion.
- **In -> Out:** edited PO -> new Sales Order (PDF moved `error/ -> processed/`),
  or a fresh error (the file stays in `error/`; no second notification on a
  manual retry).

---

## 6. UOM conversions management (UI)

- **What:** A page to view / add / edit / delete conversion rules; saving writes
  straight back to the YAML, and the next PO picks them up.
- **Arch:** `pages/1_UOM_Conversions.py` (grid editor + quick-add + a "test a
  conversion" preview) <-> `uom_converter.py` (load/save/CRUD) <->
  `data/uom_conversions.yaml`.
- **Rule shape:** `sold_qty = ordered_qty <operator> factor`, line sold in
  `uom_sold`. `customer` / `part_number` may be `*` (wildcard).

---

## 7. Where each external system is used

| System | Used by | For |
|---|---|---|
| OCI Object Storage | `storage.py` | PDFs in/out (`POPDFS/`, `processed/`, `error/`), PAR links. |
| OCI Generative AI (Gemini) | `extractor.py` | Read the PDF, validate addresses, explain errors. |
| Oracle BI Publisher | `bi_report.py` | Customer + ship-to addresses, item cross-reference. |
| Oracle Fusion SO REST API | `sales_order.py` | Duplicate check, create order, attach PDF. |
| Oracle Integration Cloud | `oic_client.py` | Pull inbox POs; **error notification**; **new-ship-to-address approval notification**. |

---

## 8. Configuration quick reference (`config.py`)

- **Storage:** namespace, bucket, folders (`POPDFS/` / `processed/` / `error/`),
  region, OCI profile.
- **GenAI:** compartment, endpoint, model (`google.gemini-2.5-pro`).
- **BIP:** base URL + credentials, `report_path`, `address_report_path`
  (defaults to `report_path`), `xref_report_path`.
- **Sales Order:** API path, `attachment_mode` (`url`/`file`), PAR TTL.
- **OIC:** `trigger_url` (inbox), `error_trigger_url` (`.../PO2SO_ERR/...`), and
  `shipto_notify_trigger_url` (`.../PO2SO_SHIPTO_APPROVAL_NOTIFY/...`, POST),
  Basic Auth, on/off switches. Ship-to notification also has fixed
  `shipto_requestor_email` / `shipto_approval_link` defaults (not derived from
  the PO).
- **UOM:** `conversions_path`, `enabled`.

Most values can be overridden with environment variables - see the `os.getenv`
defaults in `config.py`.

---

## 9. How to run

1. `pip install -r requirements.txt` (includes `PyYAML`).
2. Configure OCI (`~/.oci/config`, `DEFAULT` profile) and the `!! CHANGE` values
   in `config.py` (or the matching env vars).
3. Verify the BIP **address report column names** in
   `address_matcher.parse_bip_address_rows()` match your report.
4. **UI:** `streamlit run app.py` (the UOM page appears in the sidebar).
   **Batch/headless:** `python main.py` (processes everything in `POPDFS/`).

---

## 10. Recent change - ship-to `SiteUseId`

`shipToCustomer` in `sales_order.build_payload` now sends **`PartyId` + `SiteId`
only**; `SiteUseId` was **removed** (Oracle derives the site-use from
PartyId + SiteId). The address matcher still resolves a `SiteUseId` for display
and logging, but it is not part of the Sales Order payload.

---

## 11. New feature - new-ship-to-address approval notification

- **What:** When a PO's ship-to address is genuinely **new** (no address on
  file is even close - the matcher gives up entirely), trigger an OIC
  integration that emails a requestor an **approval link** so a human can add
  the address before the PO is reprocessed. This fires **only** on a hard
  failure - it does **not** fire for `needs_review` (an ambiguous but similar
  address already exists on file; that's a human pick, not a new address).
- **Arch:**
  `address_matcher.match_ship_to_address()` returns `method == "none"` when it
  gives up -> `orchestrator._resolve_ship_to()` propagates that result ->
  `orchestrator._create_so_for_po()` checks `match_res.method == "none"` and
  calls `orchestrator._notify_new_shipto(po)` -> `oic_client.OICClient.
  trigger_shipto_notification(...)` -> POST to the
  `PO2SO_SHIPTO_APPROVAL_NOTIFY` OIC flow. The existing general error
  notification (§OIC error-notification) still fires afterward as before -
  this is an **additional**, ship-to-specific notification, not a replacement.
- **Payload sent (JSON body, POST):**
  ```json
  {
    "customerNumber": "CUST1001",
    "customerName": "ABC Industries",
    "shipToAddress": "123 Main Street, Pune, Maharashtra",
    "requestorEmail": "requestor@company.com",
    "approvalLink": "https://your-streamlit-url/approve"
  }
  ```
  - `customerNumber`, `customerName`, `shipToAddress` are populated from the
    **extracted PO data** (`shipToAddress` is the PDF's ship-to text, since by
    definition it didn't match anything on file).
  - `requestorEmail` and `approvalLink` are **fixed** - always the configured
    defaults, never derived from the PO.
- **In -> Out:** ship-to match gives up -> one best-effort POST (never raises;
  a failed notification never masks the underlying ship-to match failure).
- **Config (`config.py` -> `OICConfig`):** `shipto_notify_trigger_url`
  (env `OIC_SHIPTO_NOTIFY_TRIGGER_URL`), `shipto_notify_method` (default
  `POST`), `shipto_notify_enabled` (env `OIC_SHIPTO_NOTIFY_ENABLED`),
  `shipto_requestor_email` (env `OIC_SHIPTO_REQUESTOR_EMAIL`),
  `shipto_approval_link` (env `OIC_SHIPTO_APPROVAL_LINK`).