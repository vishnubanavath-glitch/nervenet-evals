"""Load the app under test (../mcp-utilities) without modifying it.

Import this first in every eval entry point:

    from evals import bootstrap
    bootstrap.setup()

Order of settings: process env > nervenet-evals/.env > mcp-utilities/.env
(the app's settings.py loads its own .env with override=False, so ours wins).
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from evals.config import APP_DIR, EVALS_DIR

_done = False


def setup() -> None:
    global _done
    if _done:
        return
    # Windows consoles default to cp1252; DeepEval prints symbols outside it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    load_dotenv(EVALS_DIR / ".env", override=False)
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")

    if not (APP_DIR / "manage.py").exists():
        raise SystemExit(f"App not found at {APP_DIR}")
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coral_nervenet.settings")
    import django

    django.setup()
    _done = True
