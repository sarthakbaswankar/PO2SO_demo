"""
main.py
=======
Entry point. Run with:
    python -m po_automation.main
or schedule as an OCI Function / cron job.
"""
from __future__ import annotations

import logging
import sys

# Configure logging before importing anything else so every module that grabs a
# logger at import time inherits the configured handlers (console + rotating file).
from logging_setup import configure_logging

configure_logging()

from orchestrator import POAutomationOrchestrator


def main() -> int:
    orchestrator = POAutomationOrchestrator()
    results = orchestrator.run()

    failed = [r for r in results if not r.success]
    if failed:
        logging.getLogger(__name__).error(
            "%d PDF(s) failed processing — check error/ folder.", len(failed)
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
