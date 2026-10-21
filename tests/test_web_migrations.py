"""Adoption tests for the Django migrations.

A real legacy database must be adopted in place: ``migrate --fake-initial``
records the initial schema as applied without touching session rows, then the
follow-up migration adds history/indexes idempotently.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                start_epoch INTEGER NOT NULL,
                duration INTEGER NOT NULL,
                origin_machine TEXT NOT NULL,
                updated_at REAL NOT NULL,
                ended_at REAL,
                name TEXT,
                project TEXT,
                kind TEXT
            );
            CREATE TABLE session_history (
                id TEXT NOT NULL,
                state TEXT NOT NULL,
                start_epoch INTEGER NOT NULL,
                duration INTEGER NOT NULL,
                origin_machine TEXT NOT NULL,
                updated_at REAL NOT NULL,
                ended_at REAL,
                name TEXT,
                project TEXT,
                kind TEXT
            );
            CREATE TABLE current (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 0),
                session_id TEXT,
                updated_at REAL
            );
            """
        )
        now = int(time.time())
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-id", "ended", now - 120, 60, "old", now - 60, now - 60, "legacy", "old", None),
        )
        conn.execute(
            "INSERT INTO session_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-id", "ended", now - 120, 60, "old", now - 60, now - 60, "legacy", "old", None),
        )
        conn.execute("INSERT INTO current VALUES (0, NULL, 0)")


def _migrate(db_path: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "pomo.web.settings"
    env["POMO_DB_PATH"] = str(db_path)
    env["POMO_SECRET_KEY"] = "migration-test"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(REPO_ROOT / "src"), env.get("PYTHONPATH", "")])
    )
    return subprocess.run(
        [sys.executable, "-m", "pomo.web.manage", "migrate", "--fake-initial"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_legacy_database_is_adopted_in_place(tmp_path):
    # Given: a database created by the old stdlib server (no indexes)
    db_path = tmp_path / "pomo.db"
    _make_legacy_db(db_path)

    # When: migrations run with fake-initial
    result = _migrate(db_path)

    # Then: migration succeeds and the legacy row is untouched
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT state, name, project FROM sessions WHERE id = 'legacy-id'"
        ).fetchone()
        assert row == ("ended", "legacy", "old")
        recorded = {
            r[0]
            for r in conn.execute(
                "SELECT app || '.' || name FROM django_migrations WHERE app = 'web'"
            )
        }
        assert recorded == {"web.0001_initial", "web.0002_history_and_indexes"}
        indexes = {r[1] for r in conn.execute("PRAGMA index_list('sessions')")}
        assert "sessions_start_epoch_idx" in indexes
        current = conn.execute("SELECT session_id FROM current WHERE singleton = 0").fetchone()
        assert current is not None
