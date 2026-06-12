"""
history.py — Local persistence for processed-order history.

Stores each processed PO as a record in a JSON file on disk so the Streamlit
frontend can show a history that survives app restarts and page refreshes.

⚠ For a localhost demo this is fine. For production with concurrent writers
(e.g. the scheduler in main.py AND the Streamlit app writing at the same time),
a JSON file can be corrupted by a race. Use SQLite or a real database instead.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

# History file lives next to the code by default; override with PO2SO_HISTORY_FILE
HISTORY_FILE = os.getenv("PO2SO_HISTORY_FILE", "history.json")

# Guards concurrent add_record calls when several PDFs are processed in parallel.
# This protects against two threads doing read-modify-write at the same time and
# losing each other's records within a single process.
_LOCK = threading.Lock()


def _read_all() -> list[dict[str, Any]]:
    if not os.path.isfile(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read history file %s: %s", HISTORY_FILE, exc)
        return []


def add_record(record: dict[str, Any]) -> None:
    """Append one processed-order record. Adds a timestamp automatically.

    Thread-safe within a process: the read-modify-write is guarded by a lock so
    parallel workers don't overwrite each other's records.
    """
    record = dict(record)  # shallow copy so we don't mutate the caller's dict
    record.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))

    with _LOCK:
        records = _read_all()
        records.append(record)
        try:
            # Write to a temp file then replace, so a crash mid-write doesn't
            # corrupt the existing history.
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, default=str)
            os.replace(tmp, HISTORY_FILE)
            log.info("History record added (%d total)", len(records))
        except OSError as exc:
            log.error("Could not write history file %s: %s", HISTORY_FILE, exc)


def update_record(new_record: dict[str, Any], *,
                  record_id: str | None = None,
                  object_name: str | None = None,
                  timestamp: str | None = None) -> bool:
    """Update an EXISTING record in place (used after a successful reprocess, so a
    fixed PO flips the original error row to success instead of adding a new one).

    Matches by `record_id` first; falls back to (object_name AND timestamp). The
    original record's id and original timestamp are preserved; a `reprocessed_at`
    stamp is added. If no match is found, the record is appended (nothing lost).
    Returns True if an existing record was updated, False if appended.
    """
    new_record = dict(new_record)
    with _LOCK:
        records = _read_all()
        idx = None
        for i, rec in enumerate(records):
            if record_id and rec.get("id") == record_id:
                idx = i
                break
            if (object_name is not None and timestamp is not None
                    and rec.get("object_name") == object_name
                    and rec.get("timestamp") == timestamp):
                idx = i
                break

        if idx is None:
            new_record.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
            records.append(new_record)
            updated = False
        else:
            original = records[idx]
            merged = dict(new_record)
            merged["id"] = original.get("id") or new_record.get("id")
            merged["timestamp"] = original.get("timestamp")  # keep its place in the list
            merged["reprocessed_at"] = datetime.now().isoformat(timespec="seconds")
            records[idx] = merged
            updated = True

        try:
            tmp = HISTORY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, default=str)
            os.replace(tmp, HISTORY_FILE)
            log.info("History record %s (%d total)",
                     "updated in place" if updated else "appended (no match)", len(records))
        except OSError as exc:
            log.error("Could not write history file %s: %s", HISTORY_FILE, exc)
        return updated


def get_all(newest_first: bool = True) -> list[dict[str, Any]]:
    """Return all history records, newest first by default."""
    records = _read_all()
    if newest_first:
        records = list(reversed(records))
    return records


def clear() -> None:
    """Delete all history (used by the 'Clear history' button)."""
    try:
        if os.path.isfile(HISTORY_FILE):
            os.remove(HISTORY_FILE)
            log.info("History cleared")
    except OSError as exc:
        log.error("Could not clear history file %s: %s", HISTORY_FILE, exc)