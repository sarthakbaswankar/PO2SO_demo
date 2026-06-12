"""
uom_converter.py
================
Loads the UOM conversion rules from data/uom_conversions.yaml and applies them
to order lines at the LINE level — AFTER the item cross-reference step and just
before the Sales Order payload is built.

A rule maps (Customer + Part Number + Ordered UOM) to a Sold UOM plus an
arithmetic transformation of the quantity:

        sold_qty = ordered_qty  <operator>  factor

Supported operators: "*", "/", "+", "-".

The same module also exposes simple CRUD helpers (load_rules / save_rules /
add_rule / update_rule / delete_rule) used by the "UOM Conversions" UI page so
edits made in the app are written straight back into the YAML file.

Matching is case-insensitive. "*" in `customer` or `part_number` is a wildcard
that matches anything, letting you write customer-wide or part-wide rules.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

import yaml

log = logging.getLogger(__name__)

# data/uom_conversions.yaml next to this file (override with env if needed)
_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "uom_conversions.yaml")
UOM_CONVERSIONS_PATH = os.getenv("PO2SO_UOM_CONVERSIONS_PATH", _DEFAULT_PATH)

# Writing the YAML must be serialised so two UI saves can't corrupt the file.
_WRITE_LOCK = threading.Lock()

_VALID_OPERATORS = {"*", "/", "+", "-"}

# Canonical field order for a rule (used when writing the YAML / building rows).
RULE_FIELDS = ["part_number", "uom_ordered", "uom_sold", "operator", "factor", "customer"]


# ─────────────────────────────────────────────────────────────────────────────
# Load / save
# ─────────────────────────────────────────────────────────────────────────────
def load_rules(path: str | None = None) -> list[dict[str, Any]]:
    """Return the list of conversion-rule dicts. Missing/empty/broken file → []."""
    path = path or UOM_CONVERSIONS_PATH
    if not os.path.exists(path):
        log.info("UOM: conversions file not found at %s — no rules loaded.", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        rules = data.get("conversions") or []
        if not isinstance(rules, list):
            log.warning("UOM: 'conversions' is not a list in %s — ignoring.", path)
            return []
        # normalise every rule to a clean dict
        return [_clean_rule(r) for r in rules if isinstance(r, dict)]
    except Exception as exc:
        log.error("UOM: could not read %s: %s", path, exc)
        return []


def save_rules(rules: list[dict[str, Any]], path: str | None = None) -> None:
    """Write the rule list back to YAML (atomic, thread-safe)."""
    path = path or UOM_CONVERSIONS_PATH
    cleaned = [_clean_rule(r) for r in rules]
    payload = {"conversions": cleaned}
    with _WRITE_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(
                "# Managed by uom_converter.py and the 'UOM Conversions' UI page.\n"
                "# sold_qty = ordered_qty <operator> factor   (operator: * / + -)\n"
                "# customer / part_number may be '*' to match anything.\n\n"
            )
            yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True,
                           default_flow_style=False)
        os.replace(tmp, path)  # atomic on POSIX
    log.info("UOM: saved %d rule(s) -> %s", len(cleaned), path)


# ─────────────────────────────────────────────────────────────────────────────
# CRUD helpers (used by the UI)
# ─────────────────────────────────────────────────────────────────────────────
def add_rule(rule: dict[str, Any], path: str | None = None) -> list[dict[str, Any]]:
    validate_rule(rule)
    rules = load_rules(path)
    rules.append(_clean_rule(rule))
    save_rules(rules, path)
    return rules


def update_rule(index: int, rule: dict[str, Any],
                path: str | None = None) -> list[dict[str, Any]]:
    validate_rule(rule)
    rules = load_rules(path)
    if not (0 <= index < len(rules)):
        raise IndexError(f"UOM rule index {index} out of range (have {len(rules)})")
    rules[index] = _clean_rule(rule)
    save_rules(rules, path)
    return rules


def delete_rule(index: int, path: str | None = None) -> list[dict[str, Any]]:
    rules = load_rules(path)
    if not (0 <= index < len(rules)):
        raise IndexError(f"UOM rule index {index} out of range (have {len(rules)})")
    rules.pop(index)
    save_rules(rules, path)
    return rules


def replace_all(rules: list[dict[str, Any]], path: str | None = None) -> list[dict[str, Any]]:
    """Validate then overwrite the entire rule set (used by the grid editor)."""
    for r in rules:
        validate_rule(r)
    save_rules(rules, path)
    return load_rules(path)


# ─────────────────────────────────────────────────────────────────────────────
# Validation / cleaning
# ─────────────────────────────────────────────────────────────────────────────
def validate_rule(rule: dict[str, Any]) -> None:
    """Raise ValueError if the rule is not usable."""
    if not rule.get("part_number"):
        raise ValueError("UOM rule needs a part_number (use '*' for any).")
    if not rule.get("uom_ordered"):
        raise ValueError("UOM rule needs uom_ordered (the UOM on the incoming PO).")
    if not rule.get("uom_sold"):
        raise ValueError("UOM rule needs uom_sold (the UOM to sell in).")
    op = str(rule.get("operator", "*")).strip()
    if op not in _VALID_OPERATORS:
        raise ValueError(f"UOM rule operator must be one of {sorted(_VALID_OPERATORS)}, got {op!r}.")
    try:
        float(rule.get("factor"))
    except (TypeError, ValueError):
        raise ValueError("UOM rule factor must be a number.")
    if op == "/" and float(rule["factor"]) == 0:
        raise ValueError("UOM rule factor cannot be 0 when operator is '/'.")


def _clean_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """Coerce a rule into the canonical, serialisable shape."""
    factor: Any = rule.get("factor", 1)
    try:
        f = float(factor)
        factor = int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        factor = 1
    return {
        "part_number": str(rule.get("part_number", "")).strip(),
        "uom_ordered": str(rule.get("uom_ordered", "")).strip(),
        "uom_sold":    str(rule.get("uom_sold", "")).strip(),
        "operator":    str(rule.get("operator", "*")).strip() or "*",
        "factor":      factor,
        "customer":    str(rule.get("customer", "*")).strip() or "*",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Matching + application
# ─────────────────────────────────────────────────────────────────────────────
def _matches(rule_val: str, actual: str) -> bool:
    rv = (rule_val or "").strip()
    if rv == "*" or rv == "":
        return True
    return rv.upper() == (actual or "").strip().upper()


def _apply_operator(qty: float, operator: str, factor: float) -> float:
    if operator == "*":
        return qty * factor
    if operator == "/":
        return qty / factor
    if operator == "+":
        return qty + factor
    if operator == "-":
        return qty - factor
    return qty


def find_rule(part_number: str, uom_ordered: str, customer: str,
              rules: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Return the most specific matching rule, or None.

    Specificity: an exact (non-wildcard) customer AND part match wins over a
    rule that relies on wildcards, so customer/part-specific rules take
    precedence over broad ones.
    """
    rules = rules if rules is not None else load_rules()
    best: dict[str, Any] | None = None
    best_score = -1
    for r in rules:
        if not _matches(r.get("uom_ordered"), uom_ordered):
            continue
        if not _matches(r.get("part_number"), part_number):
            continue
        if not _matches(r.get("customer"), customer):
            continue
        score = 0
        if str(r.get("part_number", "*")).strip() != "*":
            score += 2
        if str(r.get("customer", "*")).strip() != "*":
            score += 1
        if score > best_score:
            best, best_score = r, score
    return best


def apply_uom_conversions(
    lines: list[dict[str, Any]],
    customer_name: str | None,
    rules: list[dict[str, Any]] | None = None,
) -> int:
    """Apply UOM conversion rules to each order line IN PLACE.

    For every line we look at its (Part Number, OrderedUOMCode) and the order's
    Customer. If a rule matches, we:
      • multiply/divide/etc. OrderedQuantity by the rule's factor,
      • change OrderedUOMCode to the rule's uom_sold,
      • record what happened on the line (_uom_conversion) for the UI/logs.

    The Part Number checked is the CUSTOMER's part number if present
    (CustomerItemNumber, set by the cross-reference step) else ProductNumber —
    so rules can be written against the number that appears on the PO.

    Returns the number of lines that were converted.
    """
    rules = rules if rules is not None else load_rules()
    if not rules:
        log.info("UOM: no conversion rules configured — quantities/UOMs unchanged.")
        return 0

    converted = 0
    for line in lines or []:
        # Try the customer's own item number first (what appears on the PO),
        # then fall back to the Fusion/internal ProductNumber — so YAML rules
        # can be written against whichever number is more convenient.
        cust_item = (line.get("CustomerItemNumber") or "").strip()
        fusion_item = (line.get("ProductNumber") or "").strip()
        uom_ordered = (line.get("OrderedUOMCode") or "").strip()

        # Primary lookup: customer item number (matches the PO-facing number).
        rule = find_rule(cust_item, uom_ordered, customer_name or "", rules) if cust_item else None
        # Fallback: Fusion/internal item number (after xref has remapped it).
        if not rule and fusion_item and fusion_item != cust_item:
            rule = find_rule(fusion_item, uom_ordered, customer_name or "", rules)
        # Last resort: wildcard match ignoring part number.
        part = cust_item or fusion_item
        if not rule:
            rule = find_rule(part, uom_ordered, customer_name or "", rules)
        if not rule:
            continue
        try:
            old_qty = float(line.get("OrderedQuantity", 0) or 0)
        except (TypeError, ValueError):
            log.warning("UOM: line %s has non-numeric quantity %r — skipping conversion",
                        part, line.get("OrderedQuantity"))
            continue
        new_qty = _apply_operator(old_qty, rule["operator"], float(rule["factor"]))
        # keep ints clean (10.0 -> 10)
        if float(new_qty).is_integer():
            new_qty = int(new_qty)

        line["_uom_conversion"] = {
            "from_uom": uom_ordered, "to_uom": rule["uom_sold"],
            "from_qty": old_qty, "to_qty": new_qty,
            "operator": rule["operator"], "factor": rule["factor"],
            "rule_customer": rule.get("customer"), "rule_part": rule.get("part_number"),
        }
        line["OrderedQuantity"] = new_qty
        line["OrderedUOMCode"] = rule["uom_sold"]
        # Keep the human-readable UOM name consistent with the converted (sold)
        # UOM code that will be sent to Oracle.
        # Expand the sold UOM code to a human-readable name for the UI.
        _UOM_NAMES = {
            "EA": "Each", "EACH": "Each", "CS": "Case", "BX": "Box",
            "PK": "Pack", "CTN": "Carton", "PLT": "Pallet", "PAL": "Pallet",
            "DZ": "Dozen", "KG": "Kilogram", "LB": "Pound", "L": "Litre",
            "M": "Metre", "ROL": "Roll", "SET": "Set", "PR": "Pair",
        }
        sold_code = rule["uom_sold"]
        line["OrderedUOMName"] = _UOM_NAMES.get(sold_code.upper(), sold_code)
        converted += 1
        log.info("UOM: line %s converted %s %s %s %s = %s %s (customer=%s)",
                 part, old_qty, rule["operator"], rule["factor"],
                 uom_ordered, new_qty, rule["uom_sold"], customer_name)
    log.info("UOM: applied conversions to %d of %d line(s).", converted, len(lines or []))
    return converted