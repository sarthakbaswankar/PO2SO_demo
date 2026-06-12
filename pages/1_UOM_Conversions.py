"""
pages/1_UOM_Conversions.py
==========================
Original UI with Oracle-style = and × symbol separators between column headers.

Column reading order (mirrors Oracle's UOM screen):
  Part Number | UOM Ordered  =  Qty Conversion  ×  UOM Sold | Customer
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import uom_converter as uom

st.set_page_config(page_title="UOM Conversions", page_icon="🔁", layout="wide")

st.markdown(
    "<style>[data-testid='stSidebarNav']{display:none;}</style>",
    unsafe_allow_html=True,
)
with st.sidebar:
    st.markdown("### Order Navigator")
    st.caption("PO → SO Converter")
    st.markdown("---")
    if st.button("←  Back to app", use_container_width=True):
        try:
            st.switch_page("app.py")
        except Exception:
            st.info("Use the navigation to return to the main app.")

st.title("🔁 UOM Conversions")
st.caption(
    "Line-level unit-of-measure conversions, applied AFTER the item "
    "cross-reference check and before the Sales Order is built. "
    "Rule: **sold_qty = ordered_qty (operator) factor**, line is sold in **UOM Sold**."
)

PATH = uom.UOM_CONVERSIONS_PATH
st.caption(f"Stored in `{PATH}`")

OPERATORS = ["*", "/", "+", "-"]

# Column order: Part Number | UOM Ordered | Qty Conversion (factor) | UOM Sold | Customer
# The = sits between UOM Ordered and Qty Conversion; × sits between Qty Conversion and UOM Sold.
COLUMNS = ["part_number", "uom_ordered", "factor", "operator", "uom_sold", "customer"]


def _load_df() -> pd.DataFrame:
    rules = uom.load_rules()
    if not rules:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rules)
    # ensure all expected columns exist
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = None
    return df[COLUMNS]


# ── Grid editor ───────────────────────────────────────────────────────────────
st.subheader("All conversion rules")
st.write(
    "Edit cells directly, add rows with **+**, or tick a row and press the bin "
    "icon to delete. Click **Save changes** to write everything back to the file."
)

df = _load_df()
edited = st.data_editor(
    df,
    num_rows="dynamic",
    use_container_width=True,
    key="uom_grid",
    column_config={
        "part_number": st.column_config.TextColumn(
            "Part Number",
            help="Part / item number, or * for any.",
            required=True,
        ),
        "uom_ordered": st.column_config.TextColumn(
            "UOM Ordered",
            help="UOM as it appears on the incoming PO (e.g. CS = Case). The = symbol means: ordered qty [operator] factor = sold qty.",
            required=True,
        ),
        "factor": st.column_config.NumberColumn(
            "= Qty Conversion",
            help="Conversion factor. e.g. 10 means ordered_qty [operator] 10 = sold_qty.",
            min_value=0.0,
            step=1.0,
            required=True,
        ),
        "operator": st.column_config.SelectboxColumn(
            "[op]",
            options=OPERATORS,
            help="* = multiply,  / = divide,  + = add,  − = subtract. Formula: sold_qty = ordered_qty [op] factor.",
            required=True,
        ),
        "uom_sold": st.column_config.TextColumn(
            "UOM Sold",
            help="UOM the Sales Order is created in (e.g. EA = Each). This is the result unit after conversion.",
            required=True,
        ),
        "customer": st.column_config.TextColumn(
            "Customer",
            help="Customer name, or * for any customer.",
        ),
    },
)

c1, c2, _ = st.columns([1, 1, 4])
if c1.button("💾  Save changes", type="primary"):
    try:
        rows = edited.fillna("").to_dict("records")
        rows = [r for r in rows if str(r.get("part_number", "")).strip()
                or str(r.get("uom_ordered", "")).strip()]
        for r in rows:
            r.setdefault("operator", "*")
            r.setdefault("customer", "*")
        uom.replace_all(rows)
        st.success(f"Saved {len(rows)} rule(s) to the YAML file.")
        st.rerun()
    except Exception as exc:
        st.error(f"Could not save: {exc}")

if c2.button("↻  Reload from file"):
    st.rerun()

st.divider()

# ── Quick add ────────────────────────────────────────────────────────────────
st.subheader("Quick add a rule")
with st.form("add_uom_rule", clear_on_submit=True):
    f1, f2, f3 = st.columns(3)
    part     = f1.text_input("Part Number",  placeholder="FG-201")
    uom_ord  = f2.text_input("UOM Ordered",  placeholder="CS")
    uom_sold = f3.text_input("UOM Sold",     placeholder="EA")
    g1, g2, g3 = st.columns(3)
    operator = g1.selectbox("Operator (* × / ÷ + −)", OPERATORS, index=0)
    factor   = g2.number_input("Qty Conversion", min_value=0.0, value=1.0, step=1.0)
    customer = g3.text_input("Customer", placeholder="Walkswagen")
    submitted = st.form_submit_button("➕  Add rule", type="primary")
    if submitted:
        rule = {
            "part_number": part, "uom_ordered": uom_ord, "uom_sold": uom_sold,
            "operator": operator, "factor": factor, "customer": customer or "*",
        }
        try:
            uom.add_rule(rule)
            st.success(
                f"Added: {part}  {uom_ord}  =  {factor} ×  {uom_sold}"
                f"  (operator: {operator}, customer: {customer or '*'})"
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Invalid rule: {exc}")

st.divider()

# ── Test a conversion ─────────────────────────────────────────────────────────
with st.expander("🧪 Test a conversion"):
    t1, t2, t3, t4 = st.columns(4)
    t_part = t1.text_input("Part Number",  value="FG-201",    key="t_part")
    t_uom  = t2.text_input("Ordered UOM",  value="CS",        key="t_uom")
    t_qty  = t3.number_input("Ordered Qty", min_value=0.0, value=3.0, key="t_qty")
    t_cust = t4.text_input("Customer",      value="Walkswagen", key="t_cust")
    if st.button("Preview"):
        line = {"ProductNumber": t_part, "OrderedUOMCode": t_uom, "OrderedQuantity": t_qty}
        n = uom.apply_uom_conversions([line], t_cust)
        if n:
            conv = line["_uom_conversion"]
            st.success(
                f"Matched a rule → {conv['from_qty']} {conv['from_uom']} "
                f"becomes **{line['OrderedQuantity']} {line['OrderedUOMCode']}** "
                f"(applied {conv['operator']}{conv['factor']})."
            )
        else:
            st.info("No matching rule — the quantity and UOM would be left unchanged.")