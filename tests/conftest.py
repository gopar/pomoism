"""Shared pytest fixtures for pomo tests.

Each test gets an isolated temp directory — all common path globals are
redirected so tests never touch real ~/.config, ~/.cache, or ~/.local/share.
"""

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from pomo import common  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    tmp = Path(tempfile.mkdtemp())

    overrides = {
        "CONFIG_DIR": tmp / "config",
        "CACHE_DIR": tmp / "cache",
        "DATA_DIR": tmp / "data",
        "CONFIG_FILE": tmp / "config" / "agent.toml",
        "CACHE_FILE": tmp / "cache" / "current.json",
        "OUTBOX_FILE": tmp / "cache" / "outbox.jsonl",
        "DB_FILE": tmp / "data" / "pomo.db",
        "HOOKS_DIR": tmp / "config" / "hooks",
    }
    for name, value in overrides.items():
        monkeypatch.setattr(common, name, value)

    yield tmp

    shutil.rmtree(tmp, ignore_errors=True)
