# PO → SO Converter — Feature Extension Changes

This document describes the changes made to implement the four requested features
and, importantly, the **assumptions you need to verify** in your environment.

---

## 1. UOM conversions (line-level, after item cross-reference)

**New files**
- `data/uom_conversions.yaml` — stores the rules. Seeded with your example:
  `FG-201, CS → EA, *10, Walkswagen`.
- `uom_converter.py` — loads/saves the YAML, CRUD helpers for the UI, and
  `apply_uom_conversions(lines, customer_name)` which converts each line.
- `pages/1_UOM_Conversions.py` — a Streamlit page to view / add / edit / delete
  rules in a grid; changes are written straight back to the YAML. There is also
  a "Test a conversion" preview box.

**Rule meaning:** `sold_qty = ordered_qty <operator> factor`, and the line's UOM
becomes `uom_sold`. `operator` is one of `* / + -`. `customer` and `part_number`
may be `*` (wildcard). Matching is case-insensitive; the most specific rule wins.

**Where it runs:** `orchestrator._create_so_for_po()` calls `_apply_uom(po)`
**after** `_apply_item_xref(...)` and before `build_payload(...)`, exactly as
requested. The part number matched is the customer's item number when present
(set by the cross-reference step), else the product number. Each converted line
records a `_uom_conversion` note for the UI/logs. Toggle with `PO2SO_UOM_ENABLED`.

---

## 2. Ship-to address matching (multiple addresses per customer)

**New file:** `address_matcher.py` — the tiered matcher and BIP row parser.

**Flow (in `orchestrator._create_so_for_po` → `_resolve_ship_to`):**
1. The address report is called with **only the Customer Name** parameter
   (`bi_report.fetch_customer_data_and_addresses`) and returns the customer's
   base fields plus **all** ship-to addresses.
2. The PDF ship-to address (now extracted — see §3) is matched against the
   candidates using your logic:
   - **2a** one candidate → take it.
   - **2b** several → unique postal-code match → take it.
   - **2c** still several → fuzzy match the whole address (case/punctuation
     insensitive). A unique exact match wins outright; otherwise a blended
     sequence + token-overlap score with a clear winner wins.
   - **2d** still ambiguous → GenAI picks (Gemini, via
     `PDFExtractor.match_address_via_ai`).
3. On a match, the chosen address's `ShipToPartyId` / `ShipToPartySiteId` /
   `ShipToSiteUseId` are merged into `bi_data` and used by `build_payload`. The
   resolved address text is shown on the Order workbench screens.
4. **No match → the PO is routed to the error folder**, and the UI shows the
   **PDF address** (tagged "from PDF — not matched").

**Editable + reprocess:** the Order workbench reprocess editor now has editable
Address line 1/2, City, State, Postal fields. On reprocess the edited address is
put back into `ShipToAddress` and the **full 2a→2d matching re-runs** (reprocess
routes through the same core as the live pipeline).

> ⚠️ **ASSUMPTION — verify the BIP address column names.** The exact CSV headers
> from your new report are not known here, so `address_matcher.parse_bip_address_rows()`
> looks up several common names. **Open that function and adjust the name lists**
> to your report's actual columns. It currently expects (any of):
> `SHIP_TO_PARTY_ID`, `SHIP_TO_PARTY_SITE_ID` / `PARTY_SITE_ID`,
> `SHIP_TO_SITE_USE_ID` / `SITE_USE_ID`, `ADDRESS1`/`ADDRESS_LINE_1`, `CITY`,
> `STATE`, `POSTAL_CODE`/`ZIP`, `COUNTRY`, and an optional `SHIP_TO_PARTY_NAME`.
> The base (customer-level) fields in `bi_report._map_base_fields()` reuse the
> same column names the original customer report used.

Matching thresholds live at the top of `address_matcher.py`
(`FUZZY_ACCEPT_THRESHOLD = 0.82`, `FUZZY_MARGIN = 0.07`) — tune if needed.

---

## 3. Prompt changes (description, address, multiple POs, UOM full form)

Edited `extractor.py`:
- **Item description** — `ProductDescription` rule reinforced (capture full text).
- **Ship-to address** — new `ShipToAddress` object per PO (Name / AddressLine1 /
  AddressLine2 / City / State / PostalCode / Country / Raw).
- **Multiple PO numbers** — the model now always returns a top-level
  `{"purchase_orders": [ ... ]}` array; one element per distinct PO number, each
  with its own header, ship-to and lines. A new `normalize_purchase_orders()`
  helper accepts both the new array and the legacy single-object output.
- **UOM full form** — new line field `OrderedUOMName` (the model intelligently
  expands the code, e.g. `CS → Case`, `EA → Each`). `OrderedUOMCode` is unchanged.

**Pipeline branching (`orchestrator._run_pipeline`):** after extraction the POs
are normalised to a list. **One PO → the normal single path. Several POs → all
Sales Orders are created in parallel** (`_run_multi`, thread pool), the single
source PDF is attached to every created order, and the file is moved once. If
some POs fail, the ones that succeeded are kept and an error notification is sent;
if none succeed, the file goes to the error folder.

> Note: `OrderedUOMName` is captured for the UI/logs and is **not** sent to
> Oracle (the SO API uses `OrderedUOMCode`, which after conversion holds the
> sold UOM). Move it into the payload in `sales_order.build_payload` if your
> Oracle setup expects it.

---

## 4. Error-notification integration

- `config.py` → `OICConfig` gains `error_trigger_url` (defaults to the
  `…/PO2SO/PO2SO_ERR/1.0/v1/po2so` endpoint on the same host), `error_method`
  (GET), and `error_notification_enabled`.
- `oic_client.py` → new `trigger_error_notification(context)` — same Basic Auth
  as the inbox trigger; best-effort (never raises). PO number, customer, file
  name and a short error string are sent as query params.
- `orchestrator.py` → after a PDF is moved to the error folder
  (`_move_error_and_notify`), the notification fires. This covers
  download/extract failures, single-PO failures, and "no SO created" multi-PO
  failures. A manual **reprocess** does **not** re-notify (avoids spam).

---

## Files changed / added

```
NEW   data/uom_conversions.yaml
NEW   uom_converter.py
NEW   address_matcher.py
NEW   pages/1_UOM_Conversions.py
EDIT  config.py          (UOMConfig; OIC error endpoint; BIP address_report_path)
EDIT  extractor.py       (prompt: description, ShipToAddress, multi-PO, OrderedUOMName;
                          normalize_purchase_orders(); match_address_via_ai())
EDIT  bi_report.py       (fetch_customer_data_and_addresses(); _map_base_fields())
EDIT  oic_client.py      (trigger_error_notification())
EDIT  sales_order.py     (shipToCustomer SiteUseId; strip None from nested objects)
EDIT  orchestrator.py    (multi-PO split + parallel create; address match; UOM step;
                          error-notify; reprocess re-runs matching)
EDIT  app.py             (sidebar link; show resolved/PDF ship-to; editable address +
                          UOM-full-form fields on reprocess; record carries new fields)
EDIT  requirements.txt   (PyYAML)
```

## How to run the UOM page
It appears in the Streamlit sidebar as a normal page (`pages/1_UOM_Conversions.py`),
and there's also a "UOM conversions" button in the app's own sidebar.

## Tested here (offline, no Oracle/OCI calls)
- UOM: `FG-201` 3 CS → **30 EA**; non-matching line untouched; YAML CRUD round-trip.
- Address: 2a (single), 2b (unique postal → site 205), 2c (same-ZIP fuzzy →
  site 202), 2d (GenAI pick), and the no-match case.
- Orchestration with mocked clients: single PO success (with UOM + fuzzy match),
  multi-PO (3 SOs created in parallel, one ship-to each), and the error path
  (unmatched address → error status, PDF address surfaced, **one** OIC
  error-notification call).

Real Oracle/OCI calls and the live BIP address report still need to be validated
in your environment — especially the BIP column names (see §2).
