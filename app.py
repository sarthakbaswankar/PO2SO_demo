"""
app.py — Streamlit frontend for the PO → Sales Order automation.

Pages:
  dashboard       Oracle-style analytics dashboard (KPIs, charts) from history
  upload          Upload PO PDF(s) → push to OCI → run the orchestrator
  batch_summary   Results table for a multi-file run
  success_summary Confirmation with a button into the detail view
  order_details   Full extracted detail (works for live result AND history clicks)
  error           Failure reason
  history         Persistent list of all processed orders; rows are clickable

A "Check inbox & create orders" button (dashboard + upload page) triggers an
Oracle Integration Cloud integration that reads the email inbox and pushes PO
PDFs into the pipeline.

Run:
    streamlit run app.py
"""
import io
import csv
import time
import logging
from collections import Counter
from datetime import datetime, date, timedelta

import streamlit as st
import plotly.graph_objects as go

# Configure logging FIRST so the whole pipeline logs to console + ./logs/po2so.log
# even when launched via `streamlit run app.py`. Safe across Streamlit reruns.
from logging_setup import configure_logging, LOG_FILE

configure_logging()
log = logging.getLogger("app")

from orchestrator import POAutomationOrchestrator
from storage import StorageClient
from config import settings
import history


# ─────────────────────────────────────────────────────────────────────────────
# Streamlit version-aware width helper
# Newer Streamlit deprecates use_container_width in favour of width="stretch".
# This picks the right kwarg so we avoid the deprecation warning on new versions
# while still working on >=1.30.
# ─────────────────────────────────────────────────────────────────────────────
def _st_ver() -> tuple[int, int]:
    try:
        return tuple(int(x) for x in st.__version__.split(".")[:2])  # type: ignore
    except Exception:
        return (1, 30)


_NEW_WIDTH_API = _st_ver() >= (1, 41)


def stretch(on: bool = True) -> dict:
    if _NEW_WIDTH_API:
        return {"width": "stretch" if on else "content"}
    return {"use_container_width": on}


# ─────────────────────────────────────────────────────────────────────────────
# App setup & state
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Order Navigator — PO to SO Converter",
                   page_icon="🧭", layout="wide")

if "page" not in st.session_state:
    st.session_state.page = "dashboard"
if "process_result" not in st.session_state:
    st.session_state.process_result = None
if "detail_record" not in st.session_state:
    st.session_state.detail_record = None
if "batch_results" not in st.session_state:
    st.session_state.batch_results = None


# ─────────────────────────────────────────────────────────────────────────────
# Global styling (light theme, matching the Oracle dashboard mockup)
# ─────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
      /* Hide Streamlit's auto-generated multipage navigation at the top of the
         sidebar (the "app / UOM Conversions" links). Navigation is handled by
         the custom buttons below. */
      [data-testid="stSidebarNav"] { display: none; }
      /* Transparent Streamlit toolbar + modest top padding so the header
         clears it without a big empty band; no max-width so it fills the
         screen like before. */
      [data-testid="stHeader"] { background: transparent; }
      .block-container { padding-top: 2.5rem; padding-bottom: 1rem; }

      /* ── Clean branded header ───────────────────────────────────────── */
      .app-head { display:flex; align-items:center; justify-content:space-between;
                  padding:4px 4px 14px; border-bottom:2px solid #e2e8f0; margin-bottom:18px; }
      .app-title { font-size:30px; font-weight:800; color:#0f172a; line-height:1.35;
                   padding-top:2px; }
      /* Gradient applies to the TEXT span only — keeps the emoji full-colour and
         stops glyph tops getting clipped by background-clip on the whole line. */
      .app-title .grad { background:linear-gradient(90deg,#6366f1,#8b5cf6);
                         -webkit-background-clip:text; background-clip:text;
                         -webkit-text-fill-color:transparent; }
      .app-tag { color:#64748b; font-size:13.5px; margin-top:3px; font-weight:500; }
      .app-meta { color:#94a3b8; font-size:12.5px; text-align:right; }
      .live-pill { display:inline-flex; align-items:center; gap:7px; border:1px solid #5eead4;
                   color:#0f766e; background:#ccfbf1; border-radius:999px; padding:4px 11px; font-weight:700; }
      .live-dot { width:8px; height:8px; border-radius:50%; background:#14b8a6; box-shadow:0 0 0 3px #14b8a633; }

      /* ── KPI cards ──────────────────────────────────────────────────── */
      .kpi { position: relative; background:#fff; border:1px solid #e7ebf2; border-radius:14px;
             border-top:4px solid var(--accent,#6366f1); padding:18px 18px 16px;
             box-shadow:0 1px 2px rgba(15,23,42,.04); overflow:hidden; min-height:142px; }
      .kpi-circle { position:absolute; top:-26px; right:-26px; width:90px; height:90px; border-radius:50%; }
      .kpi-icon { width:38px; height:38px; border-radius:10px; display:flex; align-items:center;
                  justify-content:center; font-size:18px; font-weight:700; }
      .kpi-label { color:#64748b; font-size:11.5px; font-weight:600; letter-spacing:.6px;
                   text-transform:uppercase; margin-top:14px; }
      .kpi-value { color:#0f172a; font-size:30px; font-weight:800; margin-top:2px; line-height:1.1; }
      .kpi-sub { font-size:12.5px; font-weight:600; margin-top:8px; }

      /* ── Panel titles ───────────────────────────────────────────────── */
      .panel-title { color:#0f172a; font-size:16px; font-weight:700; }
      .panel-sub { color:#94a3b8; font-size:12.5px; margin-bottom:6px; }

      /* ── Error reason bars ──────────────────────────────────────────── */
      .err-row { display:flex; align-items:center; gap:12px; margin:9px 0; }
      .err-label { width:170px; color:#334155; font-size:13px; }
      .err-track { flex:1; background:#eef2f7; height:9px; border-radius:999px; overflow:hidden; }
      .err-fill { height:100%; border-radius:999px; }
      .err-count { width:34px; text-align:right; font-weight:700; color:#0f172a; font-size:13px; }
      .err-pct { width:42px; text-align:right; color:#94a3b8; font-size:12px; }

      /* ── Progress (STP / SLA) ───────────────────────────────────────── */
      .prog-track { background:#eef2f7; height:10px; border-radius:999px; overflow:hidden; margin:6px 0; }
      .prog-fill { height:100%; border-radius:999px; background:linear-gradient(90deg,#14b8a6,#5eead4); }
      .prog-meta { display:flex; justify-content:space-between; color:#94a3b8; font-size:12px; }

      /* ── Format breakdown mini-cards ────────────────────────────────── */
      .fmt { border:1px solid #e7ebf2; border-radius:10px; padding:12px 14px; }
      .fmt-pct { font-size:20px; font-weight:800; }
      .fmt-label { font-size:12px; color:#64748b; }

      /* ── Generic card (used by detail / success / error pages) ──────── */
      .card { background:#fff; border:1px solid #e7ebf2; border-radius:12px; padding:20px 24px; margin-bottom:16px; }
      .card-header { background:linear-gradient(110deg,#15224d,#1b2a59); border-radius:12px;
                     padding:18px 24px; margin-bottom:16px; color:#fff; }
      .card-header h2 { margin:0; font-size:21px; font-weight:700; }
      .card-header .sub { color:#9fb0d4; font-size:13px; margin-top:4px; }
      .field-label { color:#64748b; font-size:11.5px; text-transform:uppercase; letter-spacing:.5px; margin-bottom:2px; }
      .field-value { color:#0f172a; font-size:16px; font-weight:600; margin-bottom:14px; }

      /* ── Badges ─────────────────────────────────────────────────────── */
      .badge { display:inline-block; padding:3px 12px; border-radius:999px; font-size:12px; font-weight:700; }
      .badge-success { background:#ccfbf1; color:#0f766e; border:1px solid #5eead4; }
      .badge-failed  { background:#ffe4e6; color:#be123c; border:1px solid #fda4af; }
      .badge-low     { background:#fef3c7; color:#b45309; border:1px solid #fcd34d; }
      .badge-medium  { background:#e0e7ff; color:#4338ca; border:1px solid #a5b4fc; }
      .badge-high    { background:#ccfbf1; color:#0f766e; border:1px solid #5eead4; }

      /* ── Animated loading banner ────────────────────────────────────── */
      .po-loader { display:flex; align-items:center; gap:14px;
                   background:linear-gradient(90deg,#eef2ff,#f5f3ff);
                   border:1px solid #c7d2fe; border-radius:12px; padding:16px 20px; margin:12px 0;
                   color:#3730a3; font-weight:600; }
      .po-spin { width:24px; height:24px; border:3px solid #c7d2fe; border-top-color:#6366f1;
                 border-radius:50%; animation:po-rot .8s linear infinite; flex:none; }
      @keyframes po-rot { to { transform:rotate(360deg); } }
      .po-bar { height:6px; border-radius:999px; background:#e0e7ff; overflow:hidden; margin-top:8px; }
      .po-bar::after { content:""; display:block; height:100%; width:38%; border-radius:999px;
                       background:linear-gradient(90deg,#6366f1,#8b5cf6); animation:po-slide 1.1s ease-in-out infinite; }
      @keyframes po-slide { 0% { margin-left:-40%; } 100% { margin-left:100%; } }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─────────────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────────────
def go_to_page(page_name: str) -> None:
    st.session_state.page = page_name
    st.rerun()


def reset_app() -> None:
    st.session_state.process_result = None
    st.session_state.detail_record = None
    go_to_page("dashboard")


def confidence_badge(conf: str | None) -> str:
    if not conf:
        return '<span class="badge badge-medium">—</span>'
    c = conf.upper()
    cls = {"HIGH": "badge-high", "MEDIUM": "badge-medium", "LOW": "badge-low"}.get(c, "badge-medium")
    return f'<span class="badge {cls}">{c}</span>'


def status_badge(success: bool) -> str:
    return ('<span class="badge badge-success">SUCCESS</span>' if success
            else '<span class="badge badge-failed">FAILED</span>')


def loader_html(msg: str) -> str:
    return (f'<div class="po-loader"><div class="po-spin"></div>'
            f'<div style="flex:1"><div>{msg}</div><div class="po-bar"></div></div></div>')


def show_loader(placeholder, msg: str) -> None:
    placeholder.markdown(loader_html(msg), unsafe_allow_html=True)


def result_to_record(res) -> dict:
    """Convert a live ProcessingResult into the dict shape used by render_detail/history."""
    import uuid
    status = getattr(res, "status", None) or ("success" if res.success else "error")
    return {
        "id":                 uuid.uuid4().hex,
        "file":               res.object_name.split("/")[-1],
        "object_name":        res.object_name,
        "success":            res.success,
        "status":             status,
        "order_key":          res.order_key,
        "header_id":          res.header_id,
        "customer_name":      res.customer_name,
        "business_unit_name": res.business_unit_name,
        "transaction_number": res.transaction_number,
        "currency_code":      res.currency_code,
        "confidence":         res.confidence,
        "line_count":         len(res.lines) if res.lines else 0,
        "lines":              res.lines or [],
        "par_url":            res.par_url,
        "elapsed_ms":         getattr(res, "elapsed_ms", None),
        "error":              res.error,
        # ── Final-API data + error layers + per-file log (for the records page) ──
        "payload":               getattr(res, "payload", None),
        "api_response":          getattr(res, "api_response", None),
        "api_error_raw":         getattr(res, "api_error_raw", None),
        "api_error_simplified":  getattr(res, "api_error_simplified", None),
        "log_file":              getattr(res, "log_file", None),
        "extracted":             getattr(res, "extracted", None),
        "existing_orders":       getattr(res, "existing_orders", None),
        "bi_data":               getattr(res, "bi_data", None),
        # ── Ship-to address (resolved from BIP, or PDF fallback on error) ──────
        "ship_to_address":       getattr(res, "ship_to_address", None),
        "ship_to_source":        getattr(res, "ship_to_source", None),
        "ship_to_match_method":  getattr(res, "ship_to_match_method", None),
        # ── Multiple POs in one PDF ───────────────────────────────────────────
        "order_keys":            getattr(res, "order_keys", None),
        "sub_results":           getattr(res, "sub_results", None),
    }


# ─────────────────────────────────────────────────────────────────────────────
# OIC "Check inbox & create orders" trigger button (dashboard + upload page)
# ─────────────────────────────────────────────────────────────────────────────
def _process_bucket_from_trigger(cfg):
    """After the OIC trigger, wait for PO PDFs to land in the bucket, then run
    the pipeline on them. Returns a list of ProcessingResult, or None if no
    files appeared within the wait window."""
    storage = StorageClient(settings.storage)
    deadline = time.time() + max(0, cfg.wait_for_files_seconds)
    objs = list(storage.list_pdf_objects())
    while not objs and time.time() < deadline:
        log.info("UI: no PDFs in bucket yet, waiting %ss…", cfg.poll_interval_seconds)
        time.sleep(max(1, cfg.poll_interval_seconds))
        objs = list(storage.list_pdf_objects())

    if not objs:
        return None

    log.info("UI: %d PO PDF(s) found in bucket after trigger; running pipeline", len(objs))
    with st.status(f"Processing {len(objs)} PO(s) from the bucket…", expanded=True) as status:
        st.write(f"📥 Picked up {len(objs)} PDF(s) from {settings.storage.input_folder}")
        st.write("🤖 Extracting with Gemini 2.5 Pro and creating Sales Orders…")
        orchestrator = POAutomationOrchestrator()
        results = orchestrator.process_batch(objs)
        ok_n = sum(1 for r in results if r.success)
        status.update(label=f"Done — {ok_n}/{len(results)} succeeded", state="complete")
    return results


def render_inbox_button(prefix: str) -> None:
    cfg = settings.oic
    disabled = not cfg.enabled
    clicked = st.button("Check inbox & create orders", key=f"{prefix}_oic",
                        type="primary", disabled=disabled, **stretch())
    st.caption("Triggers the Oracle Integration that reads the email inbox and drops "
               "PO PDFs into the bucket — then the pipeline picks them up and creates "
               "the Sales Orders automatically.")
    if disabled:
        st.caption("⚠️ Disabled — set OIC_ENABLED=true and configure OIC credentials in config.")
        return
    if not clicked:
        return

    ph = st.empty()

    # 1) Trigger the OIC integration
    show_loader(ph, "Triggering the inbox integration in Oracle Integration Cloud…")
    log.info("UI: inbox integration trigger requested from %s page", prefix)
    try:
        from oic_client import OICClient
        res = OICClient(cfg).trigger()
    except Exception as exc:
        ph.empty()
        st.error(f"❌ Could not trigger the inbox integration:\n\n{exc}")
        log.error("UI: inbox trigger failed: %s", exc)
        return
    st.success(f"✅ {res.get('message', 'Integration triggered.')}  "
               f"(OIC HTTP {res.get('status_code')})")

    # 2) Wait for the PDFs to land in the bucket, then run the pipeline
    show_loader(ph, "Waiting for POs to land in the OCI bucket, then creating Sales Orders…")
    try:
        results = _process_bucket_from_trigger(cfg)
    except Exception as exc:
        ph.empty()
        st.error(f"❌ Triggered OK, but processing the bucket failed:\n\n{exc}")
        log.error("UI: post-trigger bucket processing failed: %s", exc)
        return
    ph.empty()

    if not results:
        st.info(
            "Inbox integration triggered, but no PO PDFs have appeared in the bucket "
            f"yet (waited {cfg.wait_for_files_seconds}s). They may still be syncing "
            "from email — click the button again shortly, or use ‘Upload PO manually’."
        )
        return

    # 3) Persist results to history and navigate to the right view
    records = []
    for r in results:
        rec = result_to_record(r)
        history.add_record(rec)
        records.append(rec)
    log.info("UI: post-trigger run produced %d result(s)", len(records))
    st.session_state.batch_results = records
    if len(results) == 1:
        only = results[0]
        st.session_state.process_result = only
        st.session_state.detail_record = records[0]
        go_to_page("success_summary" if only.success else "error")
    else:
        go_to_page("batch_summary")


# ─────────────────────────────────────────────────────────────────────────────
# Analytics helpers (all driven by real history records)
# ─────────────────────────────────────────────────────────────────────────────
def parse_ts(rec: dict):
    ts = rec.get("timestamp") or ""
    try:
        return datetime.fromisoformat(ts[:19])
    except Exception:
        return None


def records_in_range(records, start: date, end: date) -> list:
    out = []
    for r in records:
        dt = parse_ts(r)
        if dt and start <= dt.date() <= end:
            out.append(r)
    return out


def period_key(dt: datetime, view: str) -> str:
    if view == "Daily":
        return dt.strftime("%Y-%m-%d")
    if view == "Weekly":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return dt.strftime("%Y-%m")  # Monthly


def time_series(records, view: str):
    buckets: dict[str, dict] = {}
    for r in records:
        dt = parse_ts(r)
        if not dt:
            continue
        b = buckets.setdefault(period_key(dt, view), {"processed": 0, "ok": 0, "fail": 0})
        b["processed"] += 1
        b["ok" if r.get("success") else "fail"] += 1
    return sorted(buckets), buckets


def kpi_numbers(records):
    total = len(records)
    ok = sum(1 for r in records if r.get("success"))
    fail = total - ok
    stp = (ok / total * 100) if total else 0.0
    err_rate = (fail / total * 100) if total else 0.0
    times = [r.get("elapsed_ms") for r in records if r.get("success") and r.get("elapsed_ms")]
    avg_s = (sum(times) / len(times) / 1000.0) if times else None
    return total, ok, fail, stp, err_rate, avg_s


def prior_delta(all_records, start: date, end: date):
    span = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=span - 1)
    prev = records_in_range(all_records, prev_start, prev_end)
    cur = records_in_range(all_records, start, end)

    def stp(rs):
        t = len(rs)
        return (sum(1 for r in rs if r.get("success")) / t * 100) if t else 0.0

    pct = ((len(cur) - len(prev)) / len(prev) * 100) if prev else None
    d_stp = (stp(cur) - stp(prev)) if prev else None
    return pct, d_stp, len(prev)


def categorize_error(err: str) -> str:
    e = (err or "").lower()
    if not e:
        return "Other"
    if "http 500" in e or "bip" in e or "bi report" in e or " report" in e:
        return "BI report / customer data"
    if "authentication" in e or "401" in e or "username or password" in e or "credential" in e:
        return "Authentication"
    if "missing required" in e or "no order lines" in e or "missing" in e:
        return "Extraction missing fields"
    if "not valid json" in e or "json" in e or "confidence" in e:
        return "Low AI confidence / parse"
    if "timeout" in e or "timed out" in e or "network" in e:
        return "API timeout / network"
    if "move" in e or "par" in e or "attach" in e:
        return "Attachment / storage"
    if "order" in e:
        return "Sales Order API"
    return "Other"


def error_breakdown(records):
    fails = [r for r in records if not r.get("success")]
    c = Counter(categorize_error(r.get("error")) for r in fails)
    total = sum(c.values())
    items = sorted(c.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return total, items


def format_breakdown(records):
    def fmt(name):
        n = (name or "").lower()
        if n.endswith(".pdf"):
            return "PDF"
        if n.endswith((".xlsx", ".xls")):
            return "Excel"
        if n.endswith((".csv", ".txt")):
            return "CSV / Text"
        return "Image / Other"

    c = Counter(fmt(r.get("file")) for r in records)
    total = sum(c.values()) or 1
    order = ["PDF", "Excel", "CSV / Text", "Image / Other"]
    return {k: (c.get(k, 0) / total * 100) for k in order}


def light_layout(fig, height=300, legend=True):
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#475569", size=12),
        margin=dict(l=8, r=8, t=10, b=8), height=height,
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=-0.22, x=0,
                    font=dict(color="#475569")),
    )
    fig.update_xaxes(showgrid=False, linecolor="#e2e8f0", tickfont=dict(color="#64748b"))
    fig.update_yaxes(showgrid=True, gridcolor="#eef2f7", zeroline=False,
                     tickfont=dict(color="#64748b"))
    return fig


def delta_text(pct, prev_count) -> tuple[str, str]:
    """Return (text, color) for a 'vs prior period' subtitle."""
    if pct is None or prev_count == 0:
        return "in selected range", "#94a3b8"
    arrow = "▲" if pct >= 0 else "▼"
    color = "#14b8a6" if pct >= 0 else "#f43f5e"
    return f"{arrow} {abs(pct):.1f}% vs prior period", color


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar navigation
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### Order Navigator")
    st.caption("PO → SO Converter")
    st.markdown("---")
    if st.button("📊  Dashboard", **stretch()):
        go_to_page("dashboard")
    if st.button("⬆️  Upload PO", **stretch()):
        go_to_page("upload")
    if st.button("🛠️  Order Workbench", **stretch()):
        go_to_page("records")
    if st.button("🔁  UOM conversions", **stretch()):
        # The UOM editor is a standalone Streamlit page under pages/. Jump to it
        # if the Streamlit version supports programmatic navigation; otherwise
        # the user can use the auto-generated "pages" nav above.
        try:
            st.switch_page("pages/1_UOM_Conversions.py")
        except Exception:
            st.info("Open the **UOM Conversions** page from the pages menu at the "
                    "top of the sidebar.")
    if st.button("🕘  History", **stretch()):
        go_to_page("history")


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: Dashboard
# ═════════════════════════════════════════════════════════════════════════════
if st.session_state.page == "dashboard":
    st.markdown(
        """
        <div class="app-head">
          <div>
            <div class="app-title"><span class="grad">Order Navigator</span></div>
            <div class="app-tag">Purchase Order → Sales Order Converter · Oracle Fusion Cloud ERP</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    all_records = history.get_all(newest_first=True)

    # ── Action row: inbox trigger + upload CTA ──────────────────────────────
    a1, a2, a3 = st.columns([2.2, 1.4, 1.4])
    with a1:
        render_inbox_button("dash")
    with a2:
        if st.button("Upload PO manually", **stretch()):
            go_to_page("upload")
    with a3:
        if all_records:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["timestamp", "file", "success", "order_key",
                             "customer_name", "business_unit_name", "error"])
            for r in all_records:
                writer.writerow([r.get("timestamp", ""), r.get("file", ""),
                                 r.get("success"), r.get("order_key") or "",
                                 r.get("customer_name") or "", r.get("business_unit_name") or "",
                                 r.get("error") or ""])
            st.download_button("Export CSV", buf.getvalue(),
                               file_name="po2so_history.csv", mime="text/csv", **stretch())

    if not all_records:
        st.markdown(
            """
            <div class="card" style="text-align:center; padding:44px;">
              <h3 style="color:#0f172a;">No orders processed yet</h3>
              <p style="color:#64748b;">Trigger the inbox integration above, or upload a PO,
                 to populate this dashboard.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        # ── Filter bar ───────────────────────────────────────────────────────
        dates = [parse_ts(r).date() for r in all_records if parse_ts(r)]
        min_d = min(dates) if dates else date.today()
        max_d = max(dates) if dates else date.today()
        # Default the "to" date to today so freshly-processed POs are always in range.
        default_to = max(max_d, date.today())

        # Seed widget + applied-filter state once (avoids the value+key warning and
        # lets the Apply button be the real trigger).
        if "flt_applied" not in st.session_state:
            st.session_state.dash_from = min_d
            st.session_state.dash_to = default_to
            st.session_state.dash_view = "Monthly"
            st.session_state.dash_status = "All"
            st.session_state.flt_applied = {
                "from": min_d, "to": default_to, "view": "Monthly", "status": "All",
            }

        with st.container(border=True):
            f1, f2, f3, f4, f5 = st.columns([1.3, 1.3, 1.8, 1.8, 1])
            f1.date_input("From", key="dash_from")
            f2.date_input("To", key="dash_to")
            f3.radio("View", ["Monthly", "Weekly", "Daily"], horizontal=True, key="dash_view")
            f4.radio("Status", ["All", "Success", "Errors"], horizontal=True, key="dash_status")
            f5.write("")
            if f5.button("Apply Filters", type="primary", **stretch()):
                st.session_state.flt_applied = {
                    "from":   st.session_state.dash_from,
                    "to":     st.session_state.dash_to,
                    "view":   st.session_state.dash_view,
                    "status": st.session_state.dash_status,
                }
                st.rerun()

        # Everything below renders from the APPLIED filters (set on Apply click).
        flt = st.session_state.flt_applied
        start_d, end_d = flt["from"], flt["to"]
        view, status_f = flt["view"], flt["status"]
        if start_d > end_d:
            start_d, end_d = end_d, start_d

        # Date filter + status filter drive the ENTIRE dashboard (KPIs, charts,
        # error breakdown, format, recent activity) — so changing Status visibly
        # updates everything, not just the activity list.
        records = records_in_range(all_records, start_d, end_d)
        if status_f == "Success":
            records = [r for r in records if r.get("success")]
        elif status_f == "Errors":
            records = [r for r in records if not r.get("success")]

        total, ok, fail, stp, err_rate, avg_s = kpi_numbers(records)
        pct, d_stp, prev_count = prior_delta(all_records, start_d, end_d)

        # ── KPI cards ─────────────────────────────────────────────────────────
        k1, k2, k3, k4, k5 = st.columns(5)
        d_txt, d_col = delta_text(pct, prev_count)
        with k1:
            st.markdown(
                f'<div class="kpi" style="--accent:#6366f1">'
                f'<div class="kpi-circle" style="background:#6366f114"></div>'
                f'<div class="kpi-icon" style="background:#6366f11a;color:#6366f1">▤</div>'
                f'<div class="kpi-label">Total Orders Processed</div>'
                f'<div class="kpi-value">{total:,}</div>'
                f'<div class="kpi-sub" style="color:{d_col}">{d_txt}</div></div>',
                unsafe_allow_html=True)
        with k2:
            stp_sub = (f"{stp:.1f}% STP rate" + (f" · {d_stp:+.1f}pts" if d_stp is not None else ""))
            st.markdown(
                f'<div class="kpi" style="--accent:#14b8a6">'
                f'<div class="kpi-circle" style="background:#14b8a614"></div>'
                f'<div class="kpi-icon" style="background:#14b8a61a;color:#14b8a6">✓</div>'
                f'<div class="kpi-label">Successful SO Documents</div>'
                f'<div class="kpi-value">{ok:,}</div>'
                f'<div class="kpi-sub" style="color:#14b8a6">{stp_sub}</div></div>',
                unsafe_allow_html=True)
        with k3:
            st.markdown(
                f'<div class="kpi" style="--accent:#f43f5e">'
                f'<div class="kpi-circle" style="background:#f43f5e14"></div>'
                f'<div class="kpi-icon" style="background:#f43f5e1a;color:#f43f5e">⚠</div>'
                f'<div class="kpi-label">Purchase Orders With Error</div>'
                f'<div class="kpi-value">{fail:,}</div>'
                f'<div class="kpi-sub" style="color:#f43f5e">{err_rate:.1f}% error rate</div></div>',
                unsafe_allow_html=True)
        with k4:
            avg_txt = f"{avg_s:.0f}s" if avg_s is not None else "—"
            sla = settings.sales_order.request_timeout
            avg_sub = (f"Within {sla}s SLA" if (avg_s is not None and avg_s <= sla)
                       else "no timing yet" if avg_s is None else f"over {sla}s SLA")
            avg_col = "#14b8a6" if (avg_s is not None and avg_s <= sla) else "#94a3b8"
            st.markdown(
                f'<div class="kpi" style="--accent:#f59e0b">'
                f'<div class="kpi-circle" style="background:#f59e0b14"></div>'
                f'<div class="kpi-icon" style="background:#f59e0b1a;color:#f59e0b">◷</div>'
                f'<div class="kpi-label">Avg Conversion Time</div>'
                f'<div class="kpi-value">{avg_txt}</div>'
                f'<div class="kpi-sub" style="color:{avg_col}">{avg_sub}</div></div>',
                unsafe_allow_html=True)
        with k5:
            st.markdown(
                f'<div class="kpi" style="--accent:#8b5cf6">'
                f'<div class="kpi-circle" style="background:#8b5cf614"></div>'
                f'<div class="kpi-icon" style="background:#8b5cf61a;color:#8b5cf6">$</div>'
                f'<div class="kpi-label">Total SO Amount (USD)</div>'
                f'<div class="kpi-value">—</div>'
                f'<div class="kpi-sub" style="color:#94a3b8">price not captured</div></div>',
                unsafe_allow_html=True)

        st.write("")

        # ── Row: order volume + status donut ──────────────────────────────────
        c_left, c_right = st.columns([1.55, 1])
        keys, buckets = time_series(records, view)

        with c_left:
            with st.container(border=True):
                st.markdown('<div class="panel-title">Order volume — processed vs successful vs errors</div>'
                            f'<div class="panel-sub">{view} breakdown</div>', unsafe_allow_html=True)
                if keys:
                    fig = go.Figure()
                    fig.add_trace(go.Bar(x=keys, y=[buckets[k]["processed"] for k in keys],
                                         name="Processed", marker_color="#6366f1"))
                    fig.add_trace(go.Bar(x=keys, y=[buckets[k]["ok"] for k in keys],
                                         name="Successful SO", marker_color="#14b8a6"))
                    fig.add_trace(go.Bar(x=keys, y=[buckets[k]["fail"] for k in keys],
                                         name="Errors", marker_color="#f43f5e"))
                    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
                    fig.update_xaxes(type="category")
                    fig.update_yaxes(dtick=1)
                    st.plotly_chart(light_layout(fig, height=330), **stretch())
                else:
                    st.info("No data in the selected range.")

        with c_right:
            with st.container(border=True):
                st.markdown('<div class="panel-title">Status distribution</div>'
                            f'<div class="panel-sub">{total:,} total</div>', unsafe_allow_html=True)
                fig = go.Figure(data=[go.Pie(
                    labels=["Success", "Error", "Pending"], values=[ok, fail, 0], hole=0.62,
                    marker=dict(colors=["#14b8a6", "#f43f5e", "#f59e0b"],
                                line=dict(color="#ffffff", width=2)),
                    textinfo="value", textfont=dict(size=13),
                    sort=False)])
                fig.add_annotation(text=f"<b>{stp:.1f}%</b><br>"
                                        f"<span style='font-size:11px;color:#94a3b8'>STP Rate</span>",
                                   showarrow=False, font=dict(size=22, color="#0f172a"))
                st.plotly_chart(light_layout(fig, height=330), **stretch())

        # ── Row: cumulative trend + top error reasons ────────────────────────
        c_left2, c_right2 = st.columns([1.55, 1])
        with c_left2:
            with st.container(border=True):
                st.markdown('<div class="panel-title">Orders created — cumulative</div>'
                            '<div class="panel-sub">Running total through the automation pipeline</div>',
                            unsafe_allow_html=True)
                if keys:
                    cumulative, running = [], 0
                    for k in keys:
                        running += buckets[k]["processed"]
                        cumulative.append(running)
                    avg_per = (sum(buckets[k]["processed"] for k in keys) / len(keys)) if keys else 0
                    avg_line = [avg_per * (i + 1) for i in range(len(keys))]

                    # A line needs >=2 points. When the filtered data lands in a
                    # single time bucket we'd otherwise get a lone dot, so prepend
                    # a zero "Start" anchor — the running total correctly begins at 0.
                    x_vals = keys
                    cum_vals = cumulative
                    avg_vals = avg_line
                    if len(keys) == 1:
                        x_vals = ["Start", keys[0]]
                        cum_vals = [0, cumulative[0]]
                        avg_vals = [0, avg_line[0]]

                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=x_vals, y=cum_vals, mode="lines+markers", name="Cumulative orders",
                        line=dict(color="#8b5cf6", width=3, shape="spline"),
                        marker=dict(size=8, color="#8b5cf6"),
                        fill="tozeroy", fillcolor="rgba(139,92,246,0.13)"))
                    fig.add_trace(go.Scatter(
                        x=x_vals, y=avg_vals, mode="lines", name="Period avg",
                        line=dict(color="#6366f1", width=2, dash="dash")))
                    fig.update_xaxes(type="category")
                    fig.update_yaxes(rangemode="tozero")
                    st.plotly_chart(light_layout(fig, height=320), **stretch())
                else:
                    st.info("No data in the selected range.")

        with c_right2:
            with st.container(border=True):
                err_total, err_items = error_breakdown(records)
                st.markdown('<div class="panel-title">Top error reasons</div>'
                            f'<div class="panel-sub">{err_total} total errors this period</div>',
                            unsafe_allow_html=True)
                if err_items:
                    palette = ["#f43f5e", "#6366f1", "#f59e0b", "#a5b4fc", "#a5b4fc", "#a5b4fc"]
                    max_n = max(n for _, n in err_items)
                    rows_html = ""
                    for i, (label, n) in enumerate(err_items):
                        width = (n / max_n * 100) if max_n else 0
                        pct_v = (n / err_total * 100) if err_total else 0
                        rows_html += (
                            f'<div class="err-row"><div class="err-label">{label}</div>'
                            f'<div class="err-track"><div class="err-fill" '
                            f'style="width:{width:.0f}%;background:{palette[i]}"></div></div>'
                            f'<div class="err-count">{n}</div>'
                            f'<div class="err-pct">{pct_v:.0f}%</div></div>')
                    st.markdown(rows_html, unsafe_allow_html=True)
                else:
                    st.success("No errors in the selected range. 🎉")

            # Document Format Breakdown — moved up here to fill the empty space
            # under the (usually short) error-reasons panel.
            with st.container(border=True):
                st.markdown('<div class="field-label">Document Format Breakdown</div>',
                            unsafe_allow_html=True)
                fmt = format_breakdown(records)
                colors = {"PDF": ("#0f766e", "#f0fdfa"), "Excel": ("#4338ca", "#eef2ff"),
                          "CSV / Text": ("#b45309", "#fef9ec"), "Image / Other": ("#8b5cf6", "#f5f3ff")}
                g1, g2 = st.columns(2)
                for (name, v), col in zip(list(fmt.items()), [g1, g2, g1, g2]):
                    fg, bg = colors[name]
                    col.markdown(
                        f'<div class="fmt" style="background:{bg};margin-bottom:8px">'
                        f'<div class="fmt-pct" style="color:{fg}">{v:.0f}%</div>'
                        f'<div class="fmt-label">{name}</div></div>', unsafe_allow_html=True)

        # ── Row: STP progress + SLA progress ─────────────────────────────────
        p1, p2 = st.columns(2)
        with p1:
            with st.container(border=True):
                st.markdown('<div class="field-label">Straight-Through Processing Rate</div>'
                            f'<div style="font-size:26px;font-weight:800;color:#14b8a6">{stp:.1f}%</div>',
                            unsafe_allow_html=True)
                st.markdown(
                    f'<div class="prog-track"><div class="prog-fill" style="width:{min(stp,100):.0f}%"></div></div>'
                    f'<div class="prog-meta"><span>Target: 95%</span>'
                    f'<span style="color:{"#14b8a6" if stp>=95 else "#f43f5e"}">'
                    f'{"✓ Above target" if stp>=95 else "below target"}</span></div>',
                    unsafe_allow_html=True)
        with p2:
            with st.container(border=True):
                sla = settings.sales_order.request_timeout
                avg_disp = f"{avg_s:.0f}s avg" if avg_s is not None else "—"
                fill = min((avg_s / sla * 100) if (avg_s and sla) else 0, 100)
                head = f"✓ {100-fill:.0f}% headroom" if avg_s is not None and avg_s <= sla else "—"
                st.markdown('<div class="field-label">Avg Conversion Time vs SLA '
                            f'({sla}s)</div>'
                            f'<div style="font-size:26px;font-weight:800;color:#14b8a6">{avg_disp}</div>',
                            unsafe_allow_html=True)
                st.markdown(
                    f'<div class="prog-track"><div class="prog-fill" style="width:{fill:.0f}%"></div></div>'
                    f'<div class="prog-meta"><span>SLA limit: {sla}s</span>'
                    f'<span style="color:#14b8a6">{head}</span></div>',
                    unsafe_allow_html=True)

        # ── Recent activity ──────────────────────────────────────────────────
        st.markdown("#### Recent activity")
        recent = sorted(records, key=lambda r: r.get("timestamp", ""), reverse=True)[:6]
        if not recent:
            st.caption("No matching activity for the selected filters.")
        for r in recent:
            st.markdown(
                f"{status_badge(bool(r.get('success')))} &nbsp; **{r.get('file','')}** &nbsp;·&nbsp; "
                f"{r.get('customer_name') or '—'} &nbsp;·&nbsp; "
                f"{(r.get('timestamp') or '')[:19].replace('T',' ')}",
                unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: Upload & Process
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "upload":
    st.title("📥 Upload Purchase Orders")
    st.markdown(
        "Upload one or more Purchase Order PDFs. Multiple files are processed "
        "**in parallel** (up to "
        f"{settings.processing.max_workers} at a time) and each becomes a Sales "
        "Order in Oracle Fusion."
    )

    # Inbox trigger also available here
    with st.container(border=True):
        st.markdown("**Pull POs from email automatically**")
        render_inbox_button("upload")

    st.markdown("---")
    st.markdown("**Or upload PO PDFs manually**")

    uploaded_files = st.file_uploader(
        "Select PO PDF(s)", type=["pdf"], accept_multiple_files=True
    )

    if uploaded_files:
        st.caption(f"{len(uploaded_files)} file(s) selected.")
        if st.button("Upload & Process Orders", type="primary", **stretch()):

            log.info("UI: user triggered upload+process for %d file(s): %s",
                     len(uploaded_files), [uf.name for uf in uploaded_files])

            loader = st.empty()
            prog = st.progress(0.0, text="Preparing upload…")

            # Step 1 — upload every file to the bucket (real per-file progress)
            object_names: list[str] = []
            show_loader(loader, f"Uploading {len(uploaded_files)} file(s) to OCI bucket "
                                f"({settings.storage.bucket_name})…")
            storage = StorageClient(settings.storage)
            for i, uf in enumerate(uploaded_files, 1):
                log.info("UI-STEP upload: '%s' (%d bytes)", uf.name, len(uf.getvalue()))
                obj = storage.upload_pdf(uf.getvalue(), uf.name)
                object_names.append(obj)
                prog.progress(i / len(uploaded_files),
                              text=f"Uploaded {i}/{len(uploaded_files)} · {uf.name}")

            # Step 2 — process them all in parallel (animated banner + status box
            # so the screen is never empty while the blocking call runs)
            show_loader(loader, "Processing POs via Gemini 2.5 Pro and creating Sales "
                                "Orders in Oracle…")
            with st.status("Running the automation pipeline…", expanded=True) as status:
                st.write("📥 Downloading PDFs from Object Storage…")
                st.write("🤖 Extracting structured data with Gemini 2.5 Pro…")
                st.write("📊 Enriching from BI Publisher and creating Sales Orders…")
                orchestrator = POAutomationOrchestrator()
                results = orchestrator.process_batch(object_names)
                ok_n = sum(1 for r in results if r.success)
                status.update(label=f"Processing complete — {ok_n}/{len(results)} succeeded",
                              state="complete")

            loader.empty()
            prog.empty()

            # Persist every result to history
            records = []
            for res in results:
                rec = result_to_record(res)
                history.add_record(rec)
                records.append(rec)
            log.info("UI-STEP process done: %d result(s) persisted to history", len(records))

            st.session_state.batch_results = records
            if len(results) == 1:
                only = results[0]
                st.session_state.process_result = only
                st.session_state.detail_record = records[0]
                go_to_page("success_summary" if only.success else "error")
            else:
                go_to_page("batch_summary")


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: Batch summary
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "batch_summary":
    records = st.session_state.get("batch_results") or []
    st.title("📦 Batch results")

    total = len(records)
    ok = sum(1 for r in records if r.get("success"))
    failed = total - ok
    c1, c2, c3 = st.columns(3)
    c1.metric("Processed", total)
    c2.metric("Succeeded", ok)
    c3.metric("Failed", failed)

    st.markdown("---")
    st.caption("Click **View** on a successful order to see its full details.")

    h = st.columns([3, 1.4, 2, 2, 1.4])
    for col, label in zip(h, ["File", "Status", "Order", "Customer", ""]):
        col.markdown(f"**{label}**")

    row_idx = 0
    for r in records:
        sub_results = r.get("sub_results") or []
        if sub_results:
            # Multi-PO record: expand into one row per sub-result so each SO
            # appears separately in the table instead of as a combined row.
            for sub in sub_results:
                cols = st.columns([3, 1.4, 2, 2, 1.4])
                po_num = sub.get("po_number") or "—"
                cols[0].write(f"{r.get('file', '')}  ·  PO {po_num}")
                cols[1].markdown(status_badge(bool(sub.get("success"))), unsafe_allow_html=True)
                cols[2].write(sub.get("order_key") or "—")
                cols[3].write(r.get("customer_name") or "—")
                if sub.get("success"):
                    # Build a synthetic detail record from this sub-result + parent context
                    if cols[4].button("View", key=f"batch_view_{row_idx}"):
                        detail = {**r, **sub,
                                  "transaction_number": po_num,
                                  "order_key": sub.get("order_key"),
                                  "sub_results": None}  # single-card view for this PO
                        st.session_state.detail_record = detail
                        go_to_page("order_details")
                else:
                    cols[4].write("")
                row_idx += 1
        else:
            # Single-PO record: original one-row behaviour.
            cols = st.columns([3, 1.4, 2, 2, 1.4])
            cols[0].write(r.get("file", ""))
            cols[1].markdown(status_badge(bool(r.get("success"))), unsafe_allow_html=True)
            cols[2].write(r.get("order_key") or "—")
            cols[3].write(r.get("customer_name") or "—")
            if r.get("success"):
                if cols[4].button("View", key=f"batch_view_{row_idx}"):
                    st.session_state.detail_record = r
                    go_to_page("order_details")
            else:
                cols[4].write("")
            row_idx += 1

    failed_records = [r for r in records if not r.get("success")]
    if failed_records:
        st.markdown("---")
        st.markdown("#### Failed orders — error details")
        for r in failed_records:
            with st.expander(f"{r.get('file')}"):
                st.write(f"**Reason:** {r.get('error', 'Unknown')}")

    st.markdown("---")
    if st.button("Upload more POs"):
        reset_app()


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: Success summary
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "success_summary":
    res = st.session_state.process_result
    st.success("✅ Sales Order created successfully.")
    st.markdown(
        f"""
        <div class="card">
          <div class="field-label">Source file</div>
          <div class="field-value">{res.object_name}</div>
          <div class="field-label">Order number</div>
          <div class="field-value">{res.order_key}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if res.confidence and res.confidence.upper() == "LOW":
        st.warning("Extraction confidence was LOW for this document. "
                   "Please verify the order details against the original PO.")
    if st.button("📄  View order details", type="primary"):
        st.session_state.detail_record = result_to_record(res)
        go_to_page("order_details")
    st.markdown("---")
    if st.button("Upload another PO"):
        reset_app()


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: Order details
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "order_details":
    rec = st.session_state.detail_record
    if not rec:
        st.info("No order selected. Go to History or Upload a PO.")
        if st.button("Back to home"):
            reset_app()
    else:
        sub_results = rec.get("sub_results") or []
        is_multi = bool(sub_results)

        # ── Helper: render one SO card (used for both single and each sub-PO) ─
        def _render_so_card(r: dict, idx: int, total: int, shared_rec: dict) -> None:
            """Render header + fields + lines for one Sales Order."""
            order_key = r.get("order_key") or "—"
            po_num    = r.get("po_number") or r.get("transaction_number") or "—"
            status    = r.get("status", "success")
            hdr_id    = r.get("header_id") or shared_rec.get("header_id") or "—"
            currency  = shared_rec.get("currency_code") or "USD"

            if total > 1:
                st.markdown(f"### Sales Order {idx} of {total}")

            badge = confidence_badge(shared_rec.get("confidence")) if idx == 1 else ""
            st.markdown(
                f"""
                <div class="card-header">
                  <h2>Order: {order_key} &nbsp; {badge}</h2>
                  <div class="sub">Header ID: {hdr_id} &nbsp;|&nbsp;
                       Currency: {currency} &nbsp;|&nbsp; Status: Draft</div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            col1, col2 = st.columns(2)
            with col1:
                st.markdown(
                    f'<div class="field-label">Customer</div>'
                    f'<div class="field-value">{shared_rec.get("customer_name") or "—"}</div>',
                    unsafe_allow_html=True)
                st.markdown('<div class="field-label">Order Type</div>'
                            '<div class="field-value">Standard Sales Order</div>',
                            unsafe_allow_html=True)
                st.markdown(
                    f'<div class="field-label">Source Document</div>'
                    f'<div class="field-value">{shared_rec.get("file") or "—"}</div>',
                    unsafe_allow_html=True)
            with col2:
                st.markdown(
                    f'<div class="field-label">Business Unit</div>'
                    f'<div class="field-value">{shared_rec.get("business_unit_name") or "—"}</div>',
                    unsafe_allow_html=True)
                st.markdown(
                    f'<div class="field-label">PO Number</div>'
                    f'<div class="field-value">{po_num}</div>',
                    unsafe_allow_html=True)
                st.markdown(
                    f'<div class="field-label">Processed</div>'
                    f'<div class="field-value">{shared_rec.get("timestamp") or "—"}</div>',
                    unsafe_allow_html=True)

            # Status badge for failed sub-orders in a multi-PO batch
            if status == "error":
                st.error(f"❌ This PO failed: {r.get('error') or 'Unknown error'}")
            elif status == "duplicate":
                st.warning(f"⚠️ Duplicate: {r.get('error') or ''}")

            st.markdown("#### Order lines")
            lines = r.get("lines") or []
            if lines:
                st.table({
                    "Line":     [str(ln.get("SourceTransactionLineNumber", "")) for ln in lines],
                    "Product":  [str(ln.get("ProductNumber", "")) for ln in lines],
                    "Quantity": [str(ln.get("OrderedQuantity", "")) for ln in lines],
                    "UOM":      [str(ln.get("OrderedUOMCode", "")) for ln in lines],
                })
            else:
                st.info("No line items were captured for this PO.")

        # ── Multi-PO: banner + one card per sub-result ────────────────────────
        if is_multi:
            total = len(sub_results)
            st.info(f"📦 This PDF contained **{total} Purchase Orders**. "
                    f"Each created its own Sales Order in Oracle.")
            for i, sub in enumerate(sub_results, start=1):
                _render_so_card(sub, i, total, rec)
                if i < total:
                    st.markdown("---")
        else:
            # ── Single-PO: original behaviour ─────────────────────────────────
            _render_so_card(rec, 1, 1, rec)

        # ── Shared source PDF attachment (one link for the whole batch) ───────
        if rec.get("par_url"):
            st.markdown("#### Attached source PDF")
            st.markdown(f"[Open source PO PDF]({rec['par_url']})")

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🕘  Back to history"):
                go_to_page("history")
        with c2:
            if st.button("🏠  Home"):
                reset_app()


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: Error
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "error":
    res = st.session_state.process_result
    st.error("❌ Processing failed.")
    st.markdown(
        f"""
        <div class="card">
          <div class="field-label">File name</div>
          <div class="field-value">{res.object_name.split('/')[-1]}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    simplified = getattr(res, "api_error_simplified", None)
    if simplified:
        st.warning(f"**What went wrong:**\n\n{simplified}")
        with st.expander("Raw error from Oracle API"):
            st.code(getattr(res, "api_error_raw", None) or res.error or "—")
    else:
        st.warning(f"**Reason:**\n\n{res.error}")
    st.info("The file has been moved to the `error/` folder in your OCI bucket.")
    st.caption("Tip: open the **API records** page to edit the values and reprocess this PO.")
    if st.button("Upload a corrected PO"):
        reset_app()


# ═════════════════════════════════════════════════════════════════════════════
# PAGE: History
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "history":
    st.title("🕘 Processing history")
    records = history.get_all(newest_first=True)

    if not records:
        st.info("No orders processed yet. Upload a PO to get started.")
    else:
        total = len(records)
        ok = sum(1 for r in records if r.get("success"))
        failed = total - ok
        c1, c2, c3 = st.columns(3)
        c1.metric("Total processed", total)
        c2.metric("Succeeded", ok)
        c3.metric("Failed", failed)

        st.markdown("---")
        st.caption("Click **View** on any successful order to see its full details.")

        h = st.columns([2, 3, 1.4, 2, 2, 1.6])
        for col, label in zip(h, ["Time", "File", "Status", "Order", "Customer", ""]):
            col.markdown(f"**{label}**")

        for idx, r in enumerate(records):
            cols = st.columns([2, 3, 1.4, 2, 2, 1.6])
            cols[0].write(r.get("timestamp", "")[:19].replace("T", " "))
            cols[1].write(r.get("file", ""))
            cols[2].markdown(status_badge(bool(r.get("success"))), unsafe_allow_html=True)
            cols[3].write(r.get("order_key") or "—")
            cols[4].write(r.get("customer_name") or "—")
            if r.get("success"):
                if cols[5].button("View", key=f"view_{idx}"):
                    st.session_state.detail_record = r
                    go_to_page("order_details")
            else:
                cols[5].write("")

        failed_records = [r for r in records if not r.get("success")]
        if failed_records:
            st.markdown("---")
            st.markdown("#### Failed orders — error details")
            for r in failed_records:
                with st.expander(f"{r.get('file')} — {r.get('timestamp')}"):
                    st.write(f"**Reason:** {r.get('error', 'Unknown')}")

        st.markdown("---")
        if st.button("Clear history"):
            history.clear()
            st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
# PAGE: API records — final Sales Order API result per PO.
#   • SUCCESS / DUPLICATE rows are read-only.
#   • ERROR rows are editable and can be reprocessed.
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.page == "records":
    import os
    import pandas as pd

    st.title("🛠️ Order Workbench")
    st.caption(
        "The result of the final Sales Order API call for every PO. Successful "
        "rows are read-only; rows that errored can be edited and reprocessed."
    )

    def _first(d, keys, default="—"):
        """Return the first present, non-empty value among `keys` in dict `d`."""
        if not isinstance(d, dict):
            return default
        for k in keys:
            v = d.get(k)
            if v not in (None, "", []):
                return v
        return default

    records = history.get_all(newest_first=True)
    if not records:
        st.info("No records yet. Process a PO from the Dashboard or Upload page.")
    else:
        def _rec_status(r: dict) -> str:
            return r.get("status") or ("success" if r.get("success") else "error")

        def _status_chip(status: str) -> str:
            m = {
                "success":   ("badge-success", "SUCCESS"),
                "error":     ("badge-failed",  "ERROR"),
                "duplicate": ("badge-medium",  "DUPLICATE"),
            }
            cls, label = m.get(status, ("badge-medium", status.upper()))
            return f'<span class="badge {cls}">{label}</span>'

        n_ok  = sum(1 for r in records if _rec_status(r) == "success")
        n_err = sum(1 for r in records if _rec_status(r) == "error")
        n_dup = sum(1 for r in records if _rec_status(r) == "duplicate")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Records", len(records))
        m2.metric("Success", n_ok)
        m3.metric("Errors", n_err)
        m4.metric("Duplicates", n_dup)

        flt = st.radio("Show", ["All", "Errors only", "Success only", "Duplicates"],
                       horizontal=True, key="rec_filter")
        st.markdown("---")

        for idx, r in enumerate(records):
            status = _rec_status(r)
            if flt == "Errors only" and status != "error":
                continue
            if flt == "Success only" and status != "success":
                continue
            if flt == "Duplicates" and status != "duplicate":
                continue

            with st.container(border=True):
                top = st.columns([1.4, 3, 2.4, 2])
                top[0].markdown(_status_chip(status), unsafe_allow_html=True)
                top[1].markdown(f"**{r.get('file', '—')}**")
                top[2].caption(f"PO: {r.get('transaction_number') or '—'}  ·  "
                               f"{r.get('customer_name') or '—'}")
                top[3].caption((r.get("timestamp", "") or "")[:19].replace("T", " "))

                # ── SUCCESS — read-only ─────────────────────────────────────
                if status == "success":
                    pl = r.get("payload") or {}
                    bd = r.get("bi_data") or {}
                    api = r.get("api_response") or {}

                    order_type = _first(api, ["TransactionTypeCode", "OrderType"]) 
                    if order_type == "—":
                        order_type = _first(pl, ["TransactionTypeCode"])

                    # Ship-to address — prefer the address the matcher resolved
                    # from the BI report; fall back to BI fields, then payload IDs.
                    ship_to = r.get("ship_to_address") or _first(bd, [
                        "ShipToAddress", "ShipToPartySiteName", "ShipToPartySite",
                        "ShipToSiteName", "ShipToLocation", "ShipToPartyName",
                        "ShipToCustomerName", "ShipToAddress1",
                    ])
                    if not ship_to or ship_to == "—":
                        stc = (pl.get("shipToCustomer") or [{}])
                        s0 = stc[0] if stc else {}
                        if s0.get("SiteId") or s0.get("PartyId"):
                            ship_to = f"Site {s0.get('SiteId') or '—'} / Party {s0.get('PartyId') or '—'}"
                    bill_acct = _first(bd, [
                        "BillToAccountNumber", "CustomerAccountNumber",
                        "BuyingPartyNumber", "CustomerAccountId",
                    ])

                    sc = st.columns(3)
                    sc[0].markdown(f'<div class="field-label">Order Key</div>'
                                   f'<div class="field-value">{r.get("order_key") or "—"}</div>',
                                   unsafe_allow_html=True)
                    sc[1].markdown(f'<div class="field-label">Header ID</div>'
                                   f'<div class="field-value">{r.get("header_id") or "—"}</div>',
                                   unsafe_allow_html=True)
                    sc[2].markdown(f'<div class="field-label">Order Type</div>'
                                   f'<div class="field-value">{order_type}</div>',
                                   unsafe_allow_html=True)
                    sc2 = st.columns(3)
                    sc2[0].markdown(f'<div class="field-label">Business Unit</div>'
                                    f'<div class="field-value">{r.get("business_unit_name") or "—"}</div>',
                                    unsafe_allow_html=True)
                    sc2[1].markdown(f'<div class="field-label">Ship-to</div>'
                                    f'<div class="field-value">{ship_to}</div>',
                                    unsafe_allow_html=True)
                    sc2[2].markdown(f'<div class="field-label">Bill-to Account</div>'
                                    f'<div class="field-value">{bill_acct}</div>',
                                    unsafe_allow_html=True)

                    lines = r.get("lines") or []
                    if lines:
                        st.caption("Order lines  ·  Product = Fusion item (mapped); "
                                   "Customer Item = number from the PO")
                        st.table({
                            "Customer Item": [str(ln.get("CustomerItemNumber") or "—") for ln in lines],
                            "Product":     [str(ln.get("ProductNumber", "")) for ln in lines],
                            "Description": [str(ln.get("ProductDescription") or "—") for ln in lines],
                            "Quantity":    [str(ln.get("OrderedQuantity", "")) for ln in lines],
                            "UOM":         [str(ln.get("OrderedUOMCode", "")) for ln in lines],
                        })

                    if bd:
                        with st.expander("Customer enrichment (BI report)"):
                            st.json(bd)
                    if api:
                        with st.expander("Final API response"):
                            st.json(api)
                    if pl:
                        with st.expander("Payload sent to Oracle"):
                            st.json(pl)

                # ── DUPLICATE — read-only ───────────────────────────────────
                elif status == "duplicate":
                    st.info(r.get("error") or "A Sales Order already exists for this PO.")
                    if r.get("existing_orders"):
                        with st.expander("Existing order(s) found in Oracle"):
                            st.json(r["existing_orders"])

                # ── ERROR — editable + reprocess ────────────────────────────
                else:
                    pl = r.get("payload") or {}
                    bd = r.get("bi_data") or {}
                    api = r.get("api_response") or {}
                    raw_err = r.get("api_error_raw") or r.get("error") or ""

                    simplified = r.get("api_error_simplified")
                    if simplified:
                        st.warning(f"**What went wrong / what to fix:** {simplified}")
                    else:
                        st.warning(f"**Reason:** {r.get('error') or 'Unknown error.'}")

                    # Raw Oracle API error — always visible so the actual rejection
                    # message from Oracle is never hidden behind the simplified text.
                    with st.expander("Raw error from Oracle API"):
                        st.code(raw_err or "—")

                    # AI "Suggest a fix" button — available whether or not a
                    # simplified message was already generated (it may be stale or
                    # incomplete for multi-PO batches).
                    sugg_key = f"sugg_{idx}"
                    if st.session_state.get(sugg_key):
                        st.info(f"💡 **Suggested fix:** {st.session_state[sugg_key]}")
                    elif raw_err and st.button("💡  Suggest a fix (AI)", key=f"do_sugg_{idx}"):
                        with st.spinner("Asking Gemini to explain the error…"):
                            txt = ""
                            try:
                                from extractor import PDFExtractor
                                txt = PDFExtractor(settings.genai).simplify_error(
                                    raw_err,
                                    context={"PO Number": r.get("transaction_number"),
                                             "Customer": r.get("customer_name")},
                                )
                            except Exception as exc:
                                log.warning("On-demand simplify failed: %s", exc)
                        if txt:
                            st.session_state[sugg_key] = txt
                            st.rerun()
                        else:
                            st.caption("Couldn't generate a suggestion (GenAI unavailable).")

                    # Read-only context — same enrichment fields as success rows.
                    # (Often "—" on early failures where BIP/payload never ran.)
                    order_type = _first(api, ["TransactionTypeCode", "OrderType"])
                    if order_type == "—":
                        order_type = _first(pl, ["TransactionTypeCode"])
                    # On an errored row the BIP address usually wasn't resolved,
                    # so show the address captured from the PDF (per requirement:
                    # "if no address found … in the UI add the address from the PDF").
                    ship_to = r.get("ship_to_address") or _first(bd, [
                        "ShipToAddress", "ShipToPartySiteName", "ShipToPartySite",
                        "ShipToSiteName", "ShipToLocation", "ShipToPartyName",
                        "ShipToCustomerName", "ShipToAddress1",
                    ])
                    if not ship_to or ship_to == "—":
                        _pdf_addr = ((r.get("extracted") or {}).get("ShipToAddress")) or {}
                        _raw = _pdf_addr.get("Raw") or ", ".join(
                            str(_pdf_addr.get(k)) for k in
                            ("AddressLine1", "City", "State", "PostalCode") if _pdf_addr.get(k))
                        ship_to = _raw or "—"
                    if (r.get("ship_to_source") == "pdf") and ship_to != "—":
                        ship_to = f"{ship_to}  (from PDF — not matched)"
                    bill_acct = _first(bd, [
                        "BillToAccountNumber", "CustomerAccountNumber",
                        "BuyingPartyNumber", "CustomerAccountId",
                    ])
                    cc = st.columns(3)
                    cc[0].markdown(f'<div class="field-label">Order Type</div>'
                                   f'<div class="field-value">{order_type}</div>',
                                   unsafe_allow_html=True)
                    cc[1].markdown(f'<div class="field-label">Ship-to</div>'
                                   f'<div class="field-value">{ship_to}</div>',
                                   unsafe_allow_html=True)
                    cc[2].markdown(f'<div class="field-label">Bill-to Account</div>'
                                   f'<div class="field-value">{bill_acct}</div>',
                                   unsafe_allow_html=True)
                    if bd:
                        with st.expander("Customer enrichment (BI report)"):
                            st.json(bd)

                    st.markdown("**Edit and reprocess** — fix the values, then click Reprocess.")
                    e1, e2 = st.columns(2)
                    ed_customer = e1.text_input("Customer name",
                                                value=r.get("customer_name") or "",
                                                key=f"ed_cust_{idx}")
                    ed_bu = e2.text_input("Business unit",
                                          value=r.get("business_unit_name") or "",
                                          key=f"ed_bu_{idx}")
                    e3, e4 = st.columns(2)
                    ed_po = e3.text_input("Customer PO number",
                                          value=r.get("transaction_number") or "",
                                          key=f"ed_po_{idx}")
                    ed_cur = e4.text_input("Currency code",
                                           value=r.get("currency_code") or "USD",
                                           key=f"ed_cur_{idx}")

                    # ── Editable ship-to address (re-runs the full 2a→2d match on
                    #    reprocess if changed). Pre-filled from the resolved/PDF
                    #    address so the user can correct it. ──────────────────────
                    _pdf_addr = ((r.get("extracted") or {}).get("ShipToAddress")) or {}
                    st.caption("Ship-to address (edited values re-run address matching)")
                    a1, a2 = st.columns(2)
                    ed_addr1 = a1.text_input("Address line 1",
                                             value=_pdf_addr.get("AddressLine1") or "",
                                             key=f"ed_addr1_{idx}")
                    ed_addr2 = a2.text_input("Address line 2",
                                             value=_pdf_addr.get("AddressLine2") or "",
                                             key=f"ed_addr2_{idx}")
                    a3, a4, a5 = st.columns(3)
                    ed_city = a3.text_input("City", value=_pdf_addr.get("City") or "",
                                            key=f"ed_city_{idx}")
                    ed_state = a4.text_input("State", value=_pdf_addr.get("State") or "",
                                             key=f"ed_state_{idx}")
                    ed_postal = a5.text_input("Postal code",
                                              value=_pdf_addr.get("PostalCode") or "",
                                              key=f"ed_postal_{idx}")

                    src_lines = ((r.get("extracted") or {}).get("lines")
                                 or r.get("lines") or [])
                    line_rows = [{
                        "Product": ln.get("ProductNumber"),
                        "Description": ln.get("ProductDescription"),
                        "Quantity": ln.get("OrderedQuantity"),
                        "UOM": ln.get("OrderedUOMCode"),
                        "UOM full form": ln.get("OrderedUOMName"),
                    } for ln in src_lines] or [{"Product": "", "Description": "",
                                                "Quantity": 1, "UOM": "Each",
                                                "UOM full form": "Each"}]
                    st.caption("Order lines")
                    edited_df = st.data_editor(
                        pd.DataFrame(line_rows), num_rows="dynamic",
                        key=f"ed_lines_{idx}", **stretch(),
                    )

                    if st.button("🔁  Reprocess this record", type="primary",
                                 key=f"reproc_{idx}"):
                        base = dict(r.get("extracted") or {})
                        base["CustomerName"] = ed_customer.strip()
                        base["BusinessUnitName"] = ed_bu.strip()
                        base["SourceTransactionNumber"] = ed_po.strip()
                        base["TransactionalCurrencyCode"] = (ed_cur or "USD").strip()
                        # Edited ship-to address → re-run the matcher on reprocess.
                        _orig_addr = dict(base.get("ShipToAddress") or {})
                        _new_addr = {
                            "Name": _orig_addr.get("Name"),
                            "AddressLine1": ed_addr1.strip() or None,
                            "AddressLine2": ed_addr2.strip() or None,
                            "City": ed_city.strip() or None,
                            "State": ed_state.strip() or None,
                            "PostalCode": ed_postal.strip() or None,
                            "Country": _orig_addr.get("Country"),
                            "Raw": ", ".join(p for p in [
                                ed_addr1.strip(), ed_addr2.strip(), ed_city.strip(),
                                ed_state.strip(), ed_postal.strip()] if p),
                        }
                        base["ShipToAddress"] = _new_addr
                        base.setdefault("SourceTransactionId",
                                        base.get("SourceTransactionNumber") or ed_po.strip())
                        base.setdefault("SourceTransactionRevisionNumber", 1)

                        new_lines = []
                        rows = edited_df.to_dict("records")
                        for i, row in enumerate(rows):
                            src = dict(src_lines[i]) if i < len(src_lines) else {}
                            src["ProductNumber"] = row.get("Product")
                            src["ProductDescription"] = row.get("Description")
                            src["OrderedQuantity"] = row.get("Quantity")
                            src["OrderedUOMCode"] = row.get("UOM") or "Each"
                            if row.get("UOM full form"):
                                src["OrderedUOMName"] = row.get("UOM full form")
                            if src.get("ProductNumber"):
                                new_lines.append(src)
                        base["lines"] = new_lines

                        obj_name = r.get("object_name") or r.get("file") or "reprocess.pdf"
                        with st.spinner("Reprocessing — validating, checking duplicates, "
                                        "and creating the Sales Order…"):
                            log.info("UI: reprocess requested for %s", obj_name)
                            orch = POAutomationOrchestrator()
                            new_res = orch.reprocess(base, obj_name)
                            new_rec = result_to_record(new_res)
                            # Update the ORIGINAL record in place — a fixed PO flips
                            # the existing error row to success instead of adding a
                            # new row.
                            history.update_record(
                                new_rec,
                                record_id=r.get("id"),
                                object_name=r.get("object_name"),
                                timestamp=r.get("timestamp"),
                            )
                        st.session_state.pop(f"sugg_{idx}", None)
                        if new_res.success:
                            st.success(f"✅ Reprocessed — Order Key {new_res.order_key}. "
                                       "This record is now marked success.")
                        elif getattr(new_res, "status", None) == "duplicate":
                            st.info(new_res.error)
                        else:
                            st.error(f"Still failing: {new_res.api_error_simplified or new_res.error}")
                        st.rerun()

                # ── Per-file log (all statuses) ─────────────────────────────
                lf = r.get("log_file")
                if lf and os.path.isfile(lf):
                    with st.expander("Per-file processing log"):
                        try:
                            st.code(open(lf, encoding="utf-8").read()[-8000:])
                        except OSError as exc:
                            st.caption(f"Could not read log file: {exc}")
                elif lf:
                    st.caption(f"Log file: {lf}")