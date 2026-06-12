"""bip_api/config.py — thin settings wrapper used by the existing client.py"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Settings:
    oracle_base_url: str = ""
    oracle_username: str = ""
    oracle_password: str = ""
    request_timeout: int = 120
