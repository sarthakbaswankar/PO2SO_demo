"""
bip_api/models.py  — request model for the BIP SOAP client
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class DownloadRequest:
    report_path: str
    customer_name: str | None = None
    business_unit_name: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    # Arbitrary extra BIP parameters {PARAM_NAME: value} — used e.g. by the
    # customer-item cross-reference report (P_PARTY_NAME / P_PARTY_NUMBER).
    params: dict | None = None