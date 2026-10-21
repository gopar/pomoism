"""Test settings: keep Django away from the real config/data directories."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_had_secret = "POMO_SECRET_KEY" in os.environ
if not _had_secret:
    os.environ["POMO_SECRET_KEY"] = "pomo-test-secret-key"

_previous_db_path = os.environ.get("POMO_DB_PATH")
_test_root = Path(tempfile.mkdtemp(prefix="pomo-django-test-"))
os.environ["POMO_DB_PATH"] = str(_test_root / "pomo.db")

from pomo.web.settings import *  # noqa: E402,F403

if not _had_secret:
    del os.environ["POMO_SECRET_KEY"]
if _previous_db_path is None:
    del os.environ["POMO_DB_PATH"]
else:
    os.environ["POMO_DB_PATH"] = _previous_db_path

DATABASES["default"]["TEST"] = {"NAME": str(_test_root / "test.db")}  # noqa: F405
