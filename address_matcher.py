"""
address_matcher.py
==================
Picks the correct Ship-To address for a Sales Order when one customer has MANY
ship-to addresses on file in the BI Publisher report.

WHY THIS IS CAREFUL
-------------------
A postal code is a COARSE key, not an identity: two different buildings can
share one pincode (e.g. "Godrej Green Cove" and "Godrej Green Vistas" both at
411045). So the old "unique postal code => correct address" rule could ship to
the WRONG existing site, or — worse — pick an existing site when the address the
customer actually sent isn't set up in Fusion at all.

A wrong address that still creates an order (false positive) is far more
damaging than an order that fails for a human to look at (false negative). So
this matcher follows one rule:

    POSTAL CODE may only NARROW the candidates. It may NEVER decide on its own.
    The actual ADDRESS CONTENT must always CONFIRM the pick.

HOW IT DECIDES (high level)
---------------------------
  1. Narrow the candidates by postal code (if the PDF has one).
  2. Score each remaining candidate against the PDF using:
       * a STRUCTURED GATE  - city/state must agree, building/plot NUMBERS must
         agree (a number conflict is an automatic reject), and
       * DISTINGUISHING-TOKEN weighting - words shared by many candidates
         ("godrej", "green", "road") count for little; the rare words that
         actually tell sites apart ("cove" vs "vistas", "01" vs "02") count a lot.
  3. Put the best candidate into a CONFIDENCE BAND:
       * HIGH  + clear winner            -> accept automatically.
       * MEDIUM (or a near-tie)          -> ask GenAI to confirm; if it can't,
                                            mark NEEDS REVIEW (a human decides).
       * LOW                             -> ask GenAI to pick + confirm; else FAIL.
  4. If nothing is confidently confirmed, the PO fails and the UI shows the PDF
     address. Failing is the CORRECT outcome when the real site isn't on file.

The caller (orchestrator) wires GenAI in via `genai_match_fn` (pick one from a
list) and `genai_validate_fn` (yes/no: are these the same place?).
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable

log = logging.getLogger(__name__)

# -- Tunable thresholds (all scores are 0..1) --------------------------------
HIGH_CONFIDENCE = 0.85   # at/above this (with a clear margin) we auto-accept
REVIEW_FLOOR    = 0.62   # at/above this we trust GenAI to confirm, else review
MARGIN          = 0.10   # winner must beat the runner-up by at least this much
CITY_CONFLICT   = 0.50   # below this city-name similarity = different city (reject)


# -----------------------------------------------------------------------------
# Candidate / result containers
# -----------------------------------------------------------------------------
@dataclass
class AddressCandidate:
    ship_to_party_id: int | None = None
    ship_to_party_site_id: int | None = None
    ship_to_site_use_id: int | None = None
    name: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = ""
    raw: str = ""
    row: dict[str, Any] = field(default_factory=dict)

    def full_text(self) -> str:
        parts = [self.name, self.address1, self.address2, self.city,
                 self.state, self.postal_code, self.country]
        return ", ".join(p for p in parts if p) or self.raw

    # The "core" is the part that actually distinguishes one site from another:
    # the building / premise name and street lines (NOT city/state/postal, which
    # are used as the gate). Scoring on the core is what separates look-alikes.
    def core_text(self) -> str:
        return " ".join(p for p in [self.name, self.address1, self.address2] if p) \
            or self.raw

    def display(self) -> str:
        return self.full_text() or "—"

    def to_ids(self) -> dict[str, Any]:
        """The fields the Sales Order payload needs for shipToCustomer."""
        return {
            "ShipToPartyId":     self.ship_to_party_id,
            "ShipToPartySiteId": self.ship_to_party_site_id,
            "ShipToSiteUseId":   self.ship_to_site_use_id,
            "ShipToAddress":     self.full_text(),
        }


@dataclass
class MatchResult:
    matched: bool
    method: str               # single | postal+content | content | genai | genai_validated | review | none
    candidate: AddressCandidate | None
    score: float | None = None
    reason: str = ""
    needs_review: bool = False           # True = ambiguous; a human should decide
    candidates: list[AddressCandidate] = field(default_factory=list)
    top_candidates: list[AddressCandidate] = field(default_factory=list)  # best few, for the UI


# -----------------------------------------------------------------------------
# Text normalisation
# -----------------------------------------------------------------------------
# Common postal/street abbreviations expanded so "Rd" matches "Road" etc.
_ABBREV = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue", "blvd": "boulevard",
    "ln": "lane", "dr": "drive", "hwy": "highway", "ph": "phase", "bldg": "building",
    "blk": "block", "apt": "apartment", "ste": "suite", "fl": "floor", "flr": "floor",
    "no": "number", "opp": "opposite", "sec": "sector", "ext": "extension",
}


def _expand(text: str) -> str:
    """Expand abbreviations token-by-token so Rd/Road, Ph/Phase, etc. line up."""
    return " ".join(_ABBREV.get(t, t) for t in text.split())


def _norm(text: str | None) -> str:
    """Lower-case, drop punctuation, collapse spaces, expand abbreviations."""
    if not text:
        return ""
    t = re.sub(r"[^a-z0-9 ]+", " ", str(text).lower())
    t = re.sub(r"\s+", " ", t).strip()
    return _expand(t)


def _norm_postal(text: str | None) -> str:
    """Keep only alphanumerics (ZIP+4 vs ZIP, spaces in UK codes, etc.)."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _numbers(text: str) -> set[int]:
    """Pull building/plot/unit numbers out of text as integers.

    Leading zeros are dropped so "01" == "1". Used for the hard numeric-conflict
    check ("Plot 12" must not match "Plot 47").
    """
    return {int(n) for n in re.findall(r"\d+", text or "")}


def _tokens(text: str) -> set[str]:
    return {t for t in (text or "").split() if t}


def _strip_tokens(norm_text: str, noise: set[str] | None) -> str:
    """Drop whole noise tokens (e.g. the customer name) from already-normalised
    text. The PDF ship-to line often starts with the customer name or "Ship To"
    boilerplate (e.g. "ford site 01 monticello") which isn't in the on-file
    address; removing it lets the real address match."""
    if not noise:
        return norm_text
    kept = [t for t in norm_text.split() if t not in noise]
    # If stripping removed everything, keep the original so we don't lose all signal.
    return " ".join(kept) if kept else norm_text


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _pick(row: dict[str, Any], *names: str) -> str:
    """Case/space/BOM-insensitive lookup across several possible column names."""
    lower = {(k or "").strip().lstrip("\ufeff").strip().lower(): v for k, v in row.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


# -----------------------------------------------------------------------------
# Distinguishing-token weighting (mini IDF)
# -----------------------------------------------------------------------------
def _build_idf(cores: list[str]) -> dict[str, float]:
    """Give every word a weight: words shared by many candidates get a LOW
    weight, rare words that tell sites apart get a HIGH weight.

    Example: across [Godrej Green Cove, Godrej Green Vistas] the words "godrej"
    and "green" appear everywhere (low weight) while "cove"/"vistas" appear once
    (high weight) - so the comparison is driven by the part that matters.
    """
    n = len(cores) or 1
    df: dict[str, int] = {}
    for core in cores:
        for tok in _tokens(core):
            df[tok] = df.get(tok, 0) + 1
    return {tok: math.log((n + 1) / (d + 1)) + 0.1 for tok, d in df.items()}


def _weighted_jaccard(a: str, b: str, idf: dict[str, float]) -> float:
    """Token overlap, but each token counts as much as its IDF weight."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = sum(idf.get(t, 1.0) for t in (ta & tb))
    union = sum(idf.get(t, 1.0) for t in (ta | tb))
    return inter / union if union else 0.0


# -----------------------------------------------------------------------------
# BIP row -> candidate
# -----------------------------------------------------------------------------
def parse_bip_address_rows(rows: list[dict[str, Any]]) -> list[AddressCandidate]:
    """Turn raw BIP CSV rows into AddressCandidate objects.

    Tolerant of column-name variations. Adjust the name lists here if your
    report uses different headers.
    """
    candidates: list[AddressCandidate] = []
    for row in rows or []:
        c = AddressCandidate(
            ship_to_party_id=_int(_pick(row, "SHIP_TO_PARTY_ID", "ShipToPartyId")),
            ship_to_party_site_id=_int(_pick(row, "SHIP_TO_PARTY_SITE_ID",
                                             "ShipToPartySiteId", "PARTY_SITE_ID")),
            ship_to_site_use_id=_int(_pick(row, "SHIP_TO_SITE_USE_ID",
                                           "ShipToSiteUseId", "SITE_USE_ID")),
            name=_pick(row, "SHIP_TO_PARTY_NAME", "SHIP_TO_NAME", "PARTY_NAME",
                       "SITE_NAME", "PARTY_SITE_NAME"),
            address1=_pick(row, "SHIP_TO_ADDRESS1", "ADDRESS1", "ADDRESS_LINE_1",
                           "ADDRESS_LINE1", "SHIP_TO_ADDRESS_LINE_1"),
            address2=_pick(row, "SHIP_TO_ADDRESS2", "ADDRESS2", "ADDRESS_LINE_2",
                           "ADDRESS_LINE2"),
            city=_pick(row, "SHIP_TO_CITY", "CITY", "TOWN_OR_CITY"),
            state=_pick(row, "SHIP_TO_STATE", "STATE", "PROVINCE", "REGION"),
            postal_code=_pick(row, "SHIP_TO_POSTAL_CODE", "POSTAL_CODE", "ZIP",
                              "ZIP_CODE", "POSTCODE", "PostalCode"),
            country=_pick(row, "SHIP_TO_COUNTRY", "COUNTRY", "COUNTRY_CODE"),
            raw=_pick(row, "SHIP_TO_ADDRESS", "FULL_ADDRESS", "ADDRESS"),
            row=row,
        )
        # Only keep rows that actually carry the ship-to identifiers.
        if c.ship_to_party_id or c.ship_to_party_site_id or c.full_text():
            candidates.append(c)
    return candidates


def pdf_address_text(pdf_addr: dict[str, Any] | None) -> str:
    """Flatten the extractor's ShipToAddress object into one display string."""
    if not pdf_addr:
        return ""
    parts = [pdf_addr.get(k) for k in
             ("Name", "AddressLine1", "AddressLine2", "City", "State",
              "PostalCode", "Country")]
    return ", ".join(str(p).strip() for p in parts if p) \
        or str(pdf_addr.get("Raw", "")).strip()


def _pdf_core(pdf_addr: dict[str, Any]) -> str:
    """The PDF's building/street part (mirrors AddressCandidate.core_text)."""
    parts = [pdf_addr.get(k) for k in ("Name", "AddressLine1", "AddressLine2")]
    return " ".join(str(p).strip() for p in parts if p) \
        or str(pdf_addr.get("Raw", "")).strip()


# -----------------------------------------------------------------------------
# Score ONE candidate against the PDF (the structured gate + weighted content)
# -----------------------------------------------------------------------------
@dataclass
class _Score:
    confidence: float
    hard_fail: bool
    reason: str


def _score_candidate(pdf_addr: dict[str, Any], cand: AddressCandidate,
                     idf: dict[str, float], noise: set[str] | None = None) -> _Score:
    """Return a 0..1 confidence that this candidate is the PDF's address.

    HARD FAILS (confidence forced to 0) - these mean "definitely a different
    place", so the candidate is dropped no matter how similar the rest looks:
      * City clearly different (both present, names not similar).
      * Building/plot NUMBERS present on both sides but disjoint (12 vs 47).
    Otherwise the score is driven by the distinguishing-token similarity of the
    building/street ("core"), nudged up when postal and city/state also agree.

    `noise` tokens (e.g. the customer name) are removed from both sides first so
    a customer name embedded in the PDF address doesn't break the match.
    """
    pdf_core = _strip_tokens(_norm(_pdf_core(pdf_addr)), noise)
    cand_core = _strip_tokens(_norm(cand.core_text()), noise)

    pdf_city = _norm(pdf_addr.get("City"))
    pdf_state = _norm(pdf_addr.get("State"))
    pdf_postal = _norm_postal(pdf_addr.get("PostalCode"))

    # -- GATE 1: city must not clearly disagree -----------------------------
    if pdf_city and cand.city:
        if _similar(pdf_city, _norm(cand.city)) < CITY_CONFLICT:
            return _Score(0.0, True, f"different city ({pdf_city} vs {_norm(cand.city)})")

    # -- GATE 2: building/plot numbers must not conflict --------------------
    pdf_nums = _numbers(pdf_core)
    cand_nums = _numbers(cand_core)
    if pdf_nums and cand_nums and pdf_nums.isdisjoint(cand_nums):
        return _Score(0.0, True, f"different number(s) ({sorted(pdf_nums)} vs {sorted(cand_nums)})")

    # -- CONTENT: weighted-token + sequence similarity of the core ----------
    if pdf_core and cand_core:
        wj = _weighted_jaccard(pdf_core, cand_core, idf)   # rare words dominate
        seq = _similar(pdf_core, cand_core)                # catches typos/order
        base = 0.6 * wj + 0.4 * seq
    else:
        # No usable street/building text on one side - fall back to full text.
        base = _similar(_strip_tokens(_norm(pdf_address_text(pdf_addr)), noise),
                        _strip_tokens(_norm(cand.full_text()), noise))

    # -- BOOSTS: agreement on postal / city / state adds a little confidence -
    conf = base
    if pdf_postal and _norm_postal(cand.postal_code) == pdf_postal:
        conf += 0.05
    if pdf_city and cand.city and _similar(pdf_city, _norm(cand.city)) >= 0.8:
        conf += 0.05
    if pdf_state and cand.state and _similar(pdf_state, _norm(cand.state)) >= 0.8:
        conf += 0.03

    return _Score(min(conf, 1.0), False, "")


# -----------------------------------------------------------------------------
# The matcher
# -----------------------------------------------------------------------------
def match_ship_to_address(
    pdf_addr: dict[str, Any] | None,
    candidates: list[AddressCandidate],
    genai_match_fn: Callable[[str, list[AddressCandidate]], int | None] | None = None,
    genai_validate_fn: Callable[[str, AddressCandidate], bool | None] | None = None,
    customer_name: str | None = None,
) -> MatchResult:
    """Resolve the PDF ship-to address to one BI-report candidate.

    `genai_match_fn(pdf_text, cands) -> index|None`      : AI picks one candidate.
    `genai_validate_fn(pdf_text, candidate) -> bool|None`: AI confirms a pick is
        truly the same physical place. Used before any non-obvious accept AND
        before failing, so the AI is a validator, not just a tiebreaker.
    `customer_name`: stripped out of the address text before comparison, because
        PDFs often prefix the ship-to with the customer name / "Ship To" wording
        (e.g. "Ford, Site 01, ...") which isn't in the on-file address.
    """
    pdf_addr = pdf_addr or {}
    pdf_text = pdf_address_text(pdf_addr)
    cands = list(candidates or [])

    # Noise tokens removed from BOTH sides before scoring: the customer name plus
    # a little common ship-to boilerplate. (Gates on city/state/postal are NOT
    # affected — those fields don't carry the customer name.)
    noise: set[str] = set(_norm(customer_name).split()) if customer_name else set()
    noise |= {"shipto", "ship", "to", "deliver", "delivery", "attn", "attention"}

    # STEP 0 - nothing on file -> cannot match.
    if not cands:
        return MatchResult(False, "none", None,
                           reason="No ship-to addresses on file for this customer.")

    # Small helper: AI yes/no confirmation (safe - never raises).
    def _ai_confirms(c: AddressCandidate) -> bool:
        if not genai_validate_fn:
            return False
        try:
            return bool(genai_validate_fn(pdf_text, c))
        except Exception as exc:
            log.warning("ADDR: GenAI validation raised (ignored): %s", exc)
            return False

    # STEP 1 - narrow by postal code. Postal ONLY narrows; it never decides.
    pdf_postal = _norm_postal(pdf_addr.get("PostalCode"))
    if not pdf_postal:  # try to recover a ZIP from the flattened text
        m = re.search(r"\b\d{5}(?:-\d{4})?\b", pdf_text)
        pdf_postal = _norm_postal(m.group(0)) if m else ""

    work = cands
    postal_note = ""
    if len(cands) > 1 and pdf_postal:
        hits = [c for c in cands if _norm_postal(c.postal_code) == pdf_postal]
        if hits:
            work = hits  # confirm CONTENT among the same-postal candidates
            log.info("ADDR: postal %s narrowed %d -> %d candidate(s).",
                     pdf_postal, len(cands), len(work))
        else:
            # Safety net: a stale postal in Fusion shouldn't hide the right site,
            # so if no postal matches we score the FULL set instead of giving up.
            postal_note = " (no postal-code match; compared all addresses)"
            log.info("ADDR: postal %s matched none - scoring all %d candidate(s).",
                     pdf_postal, len(cands))

    # STEP 2 - score every candidate (structured gate + weighted content).
    idf = _build_idf([_strip_tokens(_norm(c.core_text()), noise) for c in work])
    scored = [(c, _score_candidate(pdf_addr, c, idf, noise)) for c in work]
    survivors = sorted([(c, s) for c, s in scored if not s.hard_fail],
                       key=lambda x: x[1].confidence, reverse=True)
    for c, s in scored:
        if s.hard_fail:
            log.info("ADDR: rejected %r - %s", c.display(), s.reason)

    top_list = [c for c, _ in survivors[:3]]

    # STEP 3 - decide using confidence bands.
    if survivors:
        best_c, best_s = survivors[0]
        runner = survivors[1][1].confidence if len(survivors) > 1 else 0.0
        log.info("ADDR: best=%.2f (runner=%.2f) -> %r", best_s.confidence, runner, best_c.display())

        # 3a - single candidate that survived the gate: accept only if the
        # content is at least convincing, otherwise let the AI confirm (prevents
        # auto-accepting the only site when it's actually a different building,
        # e.g. Green Cove when the PDF says Green Vistas).
        if len(cands) == 1:
            if best_s.confidence >= HIGH_CONFIDENCE or _ai_confirms(best_c):
                return MatchResult(True, "single", best_c, round(best_s.confidence, 2),
                                   reason="Only one address on file; content confirmed.",
                                   candidates=cands, top_candidates=top_list)
            return MatchResult(False, "review", None, round(best_s.confidence, 2),
                               needs_review=True, candidates=cands, top_candidates=top_list,
                               reason="Only one address on file but it does not clearly match "
                                      "the PDF address - needs review.")

        # 3b - HIGH confidence AND a clear winner -> accept automatically.
        if best_s.confidence >= HIGH_CONFIDENCE and (best_s.confidence - runner) >= MARGIN:
            method = "postal+content" if (pdf_postal and not postal_note) else "content"
            return MatchResult(True, method, best_c, round(best_s.confidence, 2),
                               reason=f"Confident address match (score {best_s.confidence:.2f})"
                                      f"{postal_note}.",
                               candidates=cands, top_candidates=top_list)

        # 3c - not a runaway: ask the AI to CONFIRM the best survivor (this is the
        # "validate before failing" gate, and it also resolves near-ties). If the
        # AI agrees it's the same place, accept it even when the score was modest.
        if _ai_confirms(best_c):
            return MatchResult(True, "genai_validated", best_c, round(best_s.confidence, 2),
                               reason="Address confirmed by GenAI.",
                               candidates=cands, top_candidates=top_list)

        # 3d - a medium score the AI couldn't confirm is genuinely ambiguous ->
        # send it to a human instead of guessing.
        if best_s.confidence >= REVIEW_FLOOR:
            return MatchResult(False, "review", None, round(best_s.confidence, 2),
                               needs_review=True, candidates=cands, top_candidates=top_list,
                               reason="Address is ambiguous between similar sites - needs review.")

    # STEP 4 - nothing confident yet: let AI PICK from the set, then CONFIRM it.
    if genai_match_fn is not None:
        try:
            idx = genai_match_fn(pdf_text, work)
            if idx is not None and 0 <= idx < len(work):
                chosen = work[idx]
                if not genai_validate_fn or _ai_confirms(chosen):
                    log.info("ADDR: GenAI selected + confirmed %r", chosen.display())
                    return MatchResult(True, "genai", chosen, None,
                                       reason="Selected and confirmed by GenAI.",
                                       candidates=cands, top_candidates=top_list or [chosen])
        except Exception as exc:
            log.warning("ADDR: GenAI selection failed: %s", exc)

    # STEP 5 - give up. Failing is the right call when the real site isn't on
    # file: the caller errors the PO and the UI shows the PDF address. We still
    # hand back the closest candidates so a human can pick/fix quickly.
    return MatchResult(False, "none", None, None,
                       needs_review=bool(survivors),
                       candidates=cands, top_candidates=top_list,
                       reason="Could not confidently match the PDF ship-to address to any "
                              "address on file" + postal_note + ".")