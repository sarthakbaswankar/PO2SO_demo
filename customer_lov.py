"""
customer_lov.py
================
Fetches the Shipping Customers LOV from Oracle Fusion (fscmRestApi) so the
UOM Conversions UI can offer a dropdown of valid customer names instead of
free text — avoiding typos that would silently never match a PO.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

LOV_PATH = "/fscmRestApi/resources/11.13.18.05/shippingCustomersLOV"


def fetch_shipping_customers(
    base_url: str, username: str, password: str,
    timeout: int = 30, page_size: int = 500,
) -> list[str]:
    """Return every distinct customer name from the Shipping Customers LOV,
    paginating until the API reports no more pages.

    Raises on HTTP/network failure — the caller (UI) should catch it and fall
    back to free text rather than blocking the page.
    """
    url = base_url.rstrip("/") + LOV_PATH
    names: list[str] = []
    offset = 0
    while True:
        resp = requests.get(
            url, auth=(username, password), timeout=timeout,
            params={"onlyData": "true", "limit": page_size, "offset": offset},
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", []) or []
        for item in items:
            name = item.get("Customer")
            if name:
                names.append(name)
        if not data.get("hasMore") or not items:
            break
        # Advance by the ACTUAL item count, not the requested page_size — the
        # API can silently cap the real page size below what was requested,
        # and advancing by the requested size in that case skips a whole
        # block of customers between the two offsets.
        offset += len(items)

    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    log.info("Customer LOV: fetched %d distinct customer name(s)", len(unique))
    return unique
