"""
logging_setup.py
================
Central logging configuration + debug-artifact dumping for the PO -> SO pipeline.

Why this module exists
----------------------
* There are TWO entry points: main.py (CLI / scheduler) and app.py (Streamlit).
  Previously only main.py configured logging, so when the pipeline ran from the
  Streamlit UI every log call went nowhere. `configure_logging()` fixes that for
  BOTH entry points and is safe to call repeatedly (important: Streamlit re-runs
  the whole script top-to-bottom on every interaction, so a naive setup would
  attach duplicate handlers and print every line several times).

* Everything is mirrored to a rotating file under ./logs/ so there is a durable
  record to inspect after a run (e.g. when debugging a BIP HTTP 500 or a Gemini
  extraction that came back as non-JSON).

* `dump_json()` / `dump_text()` persist per-step artifacts to ./logs/artifacts/.
  The most important use is saving the raw Gemini JSON so you can open/"download"
  the exact model output at the backend instead of scraping it out of the logs.

Environment overrides
---------------------
  PO2SO_LOG_DIR     directory for logs + artifacts   (default: "logs")
  PO2SO_LOG_LEVEL   root log level                   (default: "INFO")
  PO2SO_LOG_FILE    log file name                    (default: "po2so.log")
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Iterator

# ── Paths / levels (overridable via env) ──────────────────────────────────────
LOG_DIR = os.getenv("PO2SO_LOG_DIR", "logs")
ARTIFACT_DIR = os.path.join(LOG_DIR, "artifacts")
PER_FILE_DIR = os.path.join(LOG_DIR, "per_file")
LOG_FILE = os.path.join(LOG_DIR, os.getenv("PO2SO_LOG_FILE", "po2so.log"))
LOG_LEVEL = os.getenv("PO2SO_LOG_LEVEL", "INFO").upper()

_FORMAT = "%(asctime)s  %(levelname)-8s  [%(threadName)s]  %(name)s  %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"

# Guards configuration so concurrent / repeated calls don't race or duplicate.
_CONFIG_LOCK = threading.Lock()
_CONFIGURED = False


def configure_logging(force: bool = False) -> None:
    """Configure root logging: console + rotating file. Idempotent.

    Call this once at every entry point (main.py, app.py). Calling it again is
    cheap and safe — it will not attach duplicate handlers. Pass force=True to
    rebuild handlers (rarely needed).
    """
    global _CONFIGURED
    with _CONFIG_LOCK:
        if _CONFIGURED and not force:
            return

        os.makedirs(LOG_DIR, exist_ok=True)

        root = logging.getLogger()
        root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

        # Remove only the handlers WE previously added, so repeated calls
        # (e.g. Streamlit reruns) don't stack up and duplicate every line.
        for h in list(root.handlers):
            if getattr(h, "_po2so_handler", False):
                root.removeHandler(h)

        fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

        # Console handler
        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(fmt)
        console._po2so_handler = True  # type: ignore[attr-defined]
        root.addHandler(console)

        # Rotating file handler (5 MB x 5 files)
        try:
            file_handler = RotatingFileHandler(
                LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            file_handler._po2so_handler = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except OSError as exc:
            root.warning("Could not open log file %s: %s — console logging only.",
                         LOG_FILE, exc)

        # Quiet down chatty third-party libraries so our steps stay readable.
        for noisy in ("urllib3", "oci", "pdfminer", "pdfplumber", "requests"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _CONFIGURED = True
        logging.getLogger(__name__).info(
            "Logging configured -> console + file '%s' (level=%s, artifacts in '%s')",
            LOG_FILE, LOG_LEVEL, ARTIFACT_DIR,
        )


# ── Per-file logging ──────────────────────────────────────────────────────────
@contextmanager
def per_file_log(label: str) -> Iterator[str | None]:
    """Attach a dedicated log file for ONE processed PDF, for the duration of
    the `with` block, in addition to the shared console + po2so.log handlers.

    Writes to <LOG_DIR>/per_file/<timestamp>__<label>.log. The handler is
    filtered to the calling thread, so when several PDFs are processed in
    parallel (each in its own worker thread) every file gets its own clean log
    containing only its own steps — not the interleaved output of every thread.

    Yields the path of the per-file log (or None if it couldn't be created).
    Never raises: a logging problem must not break processing.
    """
    tid = threading.get_ident()
    handler = None
    path: str | None = None
    try:
        os.makedirs(PER_FILE_DIR, exist_ok=True)
        path = os.path.join(PER_FILE_DIR, f"{_timestamp()}__{_safe_label(label)}.log")
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        handler.addFilter(lambda record: record.thread == tid)
        handler._po2so_perfile = True  # type: ignore[attr-defined]
        logging.getLogger().addHandler(handler)
    except OSError as exc:
        logging.getLogger(__name__).error("Could not create per-file log for %s: %s", label, exc)
        handler = None
        path = None

    try:
        yield path
    finally:
        if handler is not None:
            try:
                logging.getLogger().removeHandler(handler)
                handler.close()
            except Exception:  # pragma: no cover
                pass


# ── Artifact dumping ──────────────────────────────────────────────────────────
def _safe_label(label: str) -> str:
    """Make an arbitrary label safe for a filename.

    Sanitises the whole string (path separators become underscores) rather than
    dropping everything before the last '/', so a descriptive prefix like
    'genai_' or 'genai_raw_' is preserved even when the label embeds an object
    path such as 'genai_POPDFS/My PO.pdf'.
    """
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", (label or "artifact")).strip("_")
    return label or "artifact"


def _timestamp() -> str:
    # UTC, millisecond precision, filename-safe.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")[:-3]


def dump_text(text: str, label: str, suffix: str = "txt",
              subdir: str | None = None) -> str | None:
    """Write `text` to <LOG_DIR>/<subdir>/<timestamp>__<label>.<suffix>.

    Returns the path written, or None if writing failed (never raises — dumping
    a debug artifact must not break the pipeline).
    """
    try:
        target_dir = ARTIFACT_DIR if subdir is None else os.path.join(LOG_DIR, subdir)
        os.makedirs(target_dir, exist_ok=True)
        filename = f"{_timestamp()}__{_safe_label(label)}.{suffix}"
        path = os.path.join(target_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text if isinstance(text, str) else str(text))
        return path
    except OSError as exc:
        logging.getLogger(__name__).error(
            "Could not write artifact '%s': %s", label, exc
        )
        return None


def dump_json(data: Any, label: str, subdir: str | None = None) -> str | None:
    """Pretty-print `data` as JSON and persist it. Returns the path or None."""
    try:
        pretty = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        logging.getLogger(__name__).error(
            "Could not serialise artifact '%s' to JSON: %s", label, exc
        )
        return None
    return dump_text(pretty, label, suffix="json", subdir=subdir)