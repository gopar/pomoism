#!/usr/bin/env python3
"""Pomodoro sync store. Stdlib only (sqlite3).

This module is the source of truth for session storage. The HTTP/JSON layer
lives in :mod:`pomo.web` (Django); the CLI and agent keep using the same JSON
protocol against it.

Stores an append-only history; "current" is the latest-started active session.
Row conflicts resolve last-write-wins by `updated_at`; `ended` is terminal and
an explicit stop always wins (server-clamped timestamp).

Config via env:
    POMO_DB_PATH   (default ~/.local/share/pomo/pomo.db)
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import time
import uuid
from datetime import date
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"Error: Python 3.11+ required (current: {sys.version.split()[0]})")

from pomo import common

DB_PATH = Path(os.environ.get("POMO_DB_PATH", str(common.DB_FILE)))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db() -> None:
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id             TEXT PRIMARY KEY,
                state          TEXT NOT NULL,
                start_epoch    INTEGER NOT NULL,
                duration       INTEGER NOT NULL,
                origin_machine TEXT NOT NULL,
                updated_at     REAL NOT NULL,
                ended_at       REAL,
                name           TEXT,
                project        TEXT,
                kind           TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_history ("
            "  id TEXT NOT NULL, state TEXT NOT NULL, start_epoch INTEGER NOT NULL,"
            "  duration INTEGER NOT NULL, origin_machine TEXT NOT NULL,"
            "  updated_at REAL NOT NULL, ended_at REAL, name TEXT, project TEXT, kind TEXT"
            ")"
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS session_history_id_idx ON session_history (id)",
            "CREATE INDEX IF NOT EXISTS session_history_updated_at_idx "
            "ON session_history (updated_at)",
            "CREATE INDEX IF NOT EXISTS sessions_start_epoch_idx ON sessions (start_epoch)",
            "CREATE INDEX IF NOT EXISTS sessions_project_idx ON sessions (project)",
            "CREATE INDEX IF NOT EXISTS sessions_state_idx ON sessions (state)",
            "CREATE INDEX IF NOT EXISTS sessions_kind_state_idx ON sessions (kind, state)",
        ):
            conn.execute(statement)

        # `current` holds the id of the active session (single row, id=0).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS current (singleton INTEGER PRIMARY KEY "
            "CHECK (singleton = 0), session_id TEXT, updated_at REAL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO current (singleton, session_id, updated_at) VALUES (0, NULL, 0)"
        )


def _record_history(conn: sqlite3.Connection, session: dict) -> None:
    """Append a row to the audit-log history table."""
    conn.execute(
        "INSERT INTO session_history "
        "(id, state, start_epoch, duration, origin_machine, "
        "updated_at, ended_at, name, project, kind) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session["id"],
            session["state"],
            int(session["start_epoch"]),
            int(session["duration"]),
            session["origin_machine"],
            float(session["updated_at"]),
            session.get("ended_at"),
            session.get("name"),
            session.get("project"),
            session.get("kind"),
        ),
    )


def _row_to_session(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "state": row["state"],
        "start_epoch": row["start_epoch"],
        "duration": row["duration"],
        "origin_machine": row["origin_machine"],
        "updated_at": row["updated_at"],
        "ended_at": row["ended_at"],
        "name": row["name"],
        "project": row["project"],
        "kind": row["kind"],
    }


def _fetch_session(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    """Return the sessions row for session_id, or None if absent."""
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def _current_session_locked(conn: sqlite3.Connection) -> dict:
    """Read the current session using an already-open connection."""
    row = conn.execute("SELECT session_id FROM current WHERE singleton = 0").fetchone()
    if not row or not row["session_id"]:
        return common.idle_session()
    srow = _fetch_session(conn, row["session_id"])
    if not srow:
        return common.idle_session()
    session = _row_to_session(srow)
    if session["state"] == "ended":
        return {"state": "idle", "updated_at": session["updated_at"], "session_id": session["id"]}
    return session


def get_current_session() -> dict:
    with contextlib.closing(_connect()) as conn:
        return _current_session_locked(conn)


def get_session(session_id: str) -> dict | None:
    """Return one session by id, or None if it does not exist."""
    with contextlib.closing(_connect()) as conn:
        row = _fetch_session(conn, session_id)
    return _row_to_session(row) if row is not None else None


def create_session(
    *,
    kind: str,
    start_epoch: int,
    duration: int,
    name: str | None = None,
    project: str | None = None,
    origin_machine: str = "web",
    state: str = "ended",
    ended_at: float | None = None,
) -> dict:
    """Create a completed session without moving the ``current`` pointer.

    This is intentionally different from :func:`apply_session`: a historical
    entry added from the web UI must not become the machine-wide current
    session just because the pointer happened to be empty.
    """
    if kind not in ("pomodoro", "break"):
        raise ValueError(f"invalid kind: {kind!r}")
    if state not in ("ended", "archived"):
        raise ValueError(f"invalid state for a completed session: {state!r}")
    duration = int(duration)
    if duration <= 0:
        raise ValueError("duration must be greater than zero")
    start_epoch = int(start_epoch)
    ended = start_epoch + duration if ended_at is None else float(ended_at)
    if ended < start_epoch:
        raise ValueError("ended_at must not be before start_epoch")

    session = {
        "id": str(uuid.uuid4()),
        "state": state,
        "start_epoch": start_epoch,
        "duration": duration,
        "origin_machine": origin_machine,
        "updated_at": time.time(),
        "ended_at": ended,
        "name": name,
        "project": project,
        "kind": kind,
    }

    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO sessions "
                "(id, state, start_epoch, duration, origin_machine, "
                "updated_at, ended_at, name, project, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session["id"],
                    session["state"],
                    session["start_epoch"],
                    session["duration"],
                    session["origin_machine"],
                    session["updated_at"],
                    session["ended_at"],
                    session["name"],
                    session["project"],
                    session["kind"],
                ),
            )
            _record_history(conn, session)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    sys.stderr.write(f"pomo-server: {common.sid8(session)} created ({session['state']})\n")
    return session


def apply_session(session: dict, force: bool = False) -> tuple[bool, dict]:
    """Insert/replace current session. Returns (True, current_session).

    LWW by `updated_at`: a write older than the stored row for its id is
    rejected (no history row, no pointer change), unless `force` is set
    (end_current uses it so an explicit stop always wins, even against a
    client timestamp inflated by clock skew). Writers serialize via
    BEGIN IMMEDIATE, so the staleness pre-read is race-free.

    `ended`/`archived` are terminal: a later non-terminal write for such a
    session is rejected so a just-woken machine can't resurrect it. The
    current pointer only moves to a *different* session when that session
    started later, so a late overtime push for an old session can't steal
    `current` from a newer one.
    """
    required = ("id", "state", "start_epoch", "duration", "origin_machine", "updated_at")
    for key in required:
        if key not in session:
            raise ValueError(f"missing field: {key}")
    if session["state"] not in common.ALL_STATES:
        raise ValueError(f"invalid state: {session['state']!r}")

    with contextlib.closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            incoming = float(session["updated_at"])
            row = conn.execute(
                "SELECT updated_at, state FROM sessions WHERE id = ?", (session["id"],)
            ).fetchone()
            if row is not None:
                if force:
                    # Clamp so the forced write is never older than the stored
                    # row, keeping `updated_at` monotonic for downstream LWW.
                    if row["updated_at"] >= incoming:
                        incoming = row["updated_at"] + 1e-6
                        session["updated_at"] = incoming
                elif row["updated_at"] > incoming or (
                    row["state"] in ("ended", "archived")
                    and session["state"] not in ("ended", "archived")
                ):
                    conn.execute("ROLLBACK")
                    sys.stderr.write(
                        f"pomo-server: rejected stale {common.sid8(session)} ({session['state']})\n"
                    )
                    return False, _current_session_locked(conn)
            conn.execute(
                "INSERT INTO sessions "
                "(id, state, start_epoch, duration, origin_machine, "
                "updated_at, ended_at, name, project, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "state = excluded.state, "
                "start_epoch = excluded.start_epoch, "
                "duration = excluded.duration, "
                "origin_machine = excluded.origin_machine, "
                "updated_at = excluded.updated_at, "
                "ended_at = excluded.ended_at, "
                "name = excluded.name, "
                "project = excluded.project, "
                "kind = excluded.kind",
                (
                    session["id"],
                    session["state"],
                    int(session["start_epoch"]),
                    int(session["duration"]),
                    session["origin_machine"],
                    incoming,
                    session.get("ended_at"),
                    session.get("name"),
                    session.get("project"),
                    session.get("kind"),
                ),
            )
            _record_history(conn, session)
            # Decide whether this write may move the `current` pointer.
            pointer = conn.execute(
                "SELECT c.session_id, s.start_epoch FROM current c "
                "LEFT JOIN sessions s ON s.id = c.session_id "
                "WHERE c.singleton = 0"
            ).fetchone()
            ptr_id = pointer["session_id"] if pointer else None
            ptr_start = pointer["start_epoch"] if pointer else None
            if (
                ptr_id is None
                or ptr_id == session["id"]
                or (ptr_start is not None and int(session["start_epoch"]) == ptr_start)
            ):
                # Same session, empty pointer, or ambiguous same-second start:
                # plain LWW by updated_at.
                conn.execute(
                    "UPDATE current SET session_id = ?, updated_at = ? "
                    "WHERE singleton = 0 AND updated_at <= ?",
                    (session["id"], incoming, incoming),
                )
            elif ptr_start is None or int(session["start_epoch"]) > ptr_start:
                # A genuinely later-started session takes current even if its
                # client timestamp lags (clock skew).
                conn.execute(
                    "UPDATE current SET session_id = ?, updated_at = ? WHERE singleton = 0",
                    (session["id"], incoming),
                )
            # else: an older session must not steal current from a newer one.
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        sys.stderr.write(f"pomo-server: {common.sid8(session)} ({session['state']})\n")
        current = _current_session_locked(conn)
    return True, current


def end_current(session: dict) -> tuple[bool, dict]:
    """Mark the current session ended using the provided record."""
    ended = dict(session)
    ended["state"] = "ended"
    # Server stamps the timestamp so an explicit stop always wins, even if
    # the client's own updated_at lags the stored row; `force` additionally
    # clamps it above any clock-skewed stored timestamp.
    ended["updated_at"] = time.time()
    ended.setdefault("ended_at", time.time())
    ended["ended_at"] = ended["ended_at"] or time.time()
    return apply_session(ended, force=True)


def get_sessions(
    from_date: str | None = None,
    to_date: str | None = None,
    project: str | None = None,
    state: str | None = None,
    include_archived: bool = False,
    *,
    all_time: bool = False,
) -> list[dict]:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        where: list[str] = []
        params: list = []

        if from_date is not None:
            where.append("date(start_epoch, 'unixepoch', 'localtime') >= ?")
            params.append(from_date)
        elif not all_time:
            where.append("date(start_epoch, 'unixepoch', 'localtime') >= date('now', 'localtime')")

        if to_date is not None:
            where.append("date(start_epoch, 'unixepoch', 'localtime') <= ?")
            params.append(to_date)
        elif from_date is None and not all_time:
            where.append("date(start_epoch, 'unixepoch', 'localtime') <= date('now', 'localtime')")

        if project is not None:
            where.append("project = ?")
            params.append(project)

        if state is not None:
            where.append("state = ?")
            params.append(state)

        if not include_archived:
            where.append("state != 'archived'")

        where_clause = " AND ".join(where) if where else "1=1"
        sql = f"SELECT * FROM sessions WHERE {where_clause} ORDER BY start_epoch ASC"
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_session(r) for r in rows]


def get_today_sessions(project: str | None = None) -> list[dict]:
    return get_sessions(project=project)


def get_projects() -> list[dict]:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT project
            FROM sessions
            WHERE project IS NOT NULL
            ORDER BY project ASC
            """
        ).fetchall()
    return [{"project": r["project"]} for r in rows]


def get_stats(
    from_date: str | None = None,
    to_date: str | None = None,
    project: str | None = None,
    include_archived: bool = False,
    *,
    all_time: bool = False,
) -> dict:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        where: list[str] = []
        params: list = []

        if from_date is not None:
            where.append("date(start_epoch, 'unixepoch', 'localtime') >= ?")
            params.append(from_date)
        elif not all_time:
            where.append("date(start_epoch, 'unixepoch', 'localtime') = date('now', 'localtime')")

        if to_date is not None:
            where.append("date(start_epoch, 'unixepoch', 'localtime') <= ?")
            params.append(to_date)
        elif from_date is None and not all_time:
            where.append("date(start_epoch, 'unixepoch', 'localtime') <= date('now', 'localtime')")

        if project is not None:
            where.append("project = ?")
            params.append(project)

        if not include_archived:
            where.append("state != 'archived'")

        where.append("kind = 'pomodoro'")

        where_clause = " AND ".join(where) if where else "1=1"
        sql = (
            "SELECT id, state, duration, project, start_epoch, ended_at "
            f"FROM sessions WHERE {where_clause}"
        )
        rows = conn.execute(sql, params).fetchall()

    total_seconds = 0
    session_count = 0
    projects: dict[str, dict] = {}
    for r in rows:
        dur = int(r["duration"])
        ended_at = r["ended_at"]
        start_epoch = int(r["start_epoch"])
        if ended_at is not None and float(ended_at) > start_epoch:
            dur = int(float(ended_at) - start_epoch)
        state = r["state"]
        proj = r["project"] or ""
        if state == "ended" or include_archived and state == "archived":
            total_seconds += dur
            session_count += 1
            if proj:
                if proj not in projects:
                    projects[proj] = {"seconds": 0, "count": 0}
                projects[proj]["seconds"] += dur
                projects[proj]["count"] += 1

    return {
        "total_seconds": total_seconds,
        "session_count": session_count,
        "projects": dict(sorted(projects.items())),
    }


def edit_session(session_id: str, fields: dict) -> dict:
    """Edit a session. Active sessions only accept metadata changes.

    Always stamps ``updated_at`` server-side and appends a history row. Timing
    fields are accepted for completed/archived sessions; changing the start or
    duration recomputes ``ended_at`` unless an explicit end is supplied.
    """
    timing_keys = {"start_epoch", "duration", "kind", "ended_at"}
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _fetch_session(conn, session_id)
            if not row:
                conn.execute("ROLLBACK")
                raise LookupError(f"session {session_id!r} not found")

            session = _row_to_session(row)
            if session["state"] in common.ACTIVE_STATES and timing_keys & fields.keys():
                conn.execute("ROLLBACK")
                raise ValueError("cannot edit the timing of an active session")

            name = fields.get("name") if "name" in fields else session["name"]
            project = fields.get("project") if "project" in fields else session["project"]
            start_epoch = (
                int(fields["start_epoch"])
                if "start_epoch" in fields
                else int(session["start_epoch"])
            )
            duration = int(fields["duration"]) if "duration" in fields else int(session["duration"])
            if duration <= 0:
                conn.execute("ROLLBACK")
                raise ValueError("duration must be greater than zero")

            kind = fields.get("kind") if "kind" in fields else session["kind"]
            if kind not in (None, "pomodoro", "break"):
                conn.execute("ROLLBACK")
                raise ValueError(f"invalid kind: {kind!r}")

            if fields.get("ended_at") is not None:
                ended_at: float | None = float(fields["ended_at"])
            elif "start_epoch" in fields or "duration" in fields:
                ended_at = float(start_epoch + duration)
            else:
                ended_at = session["ended_at"]
            if ended_at is not None and ended_at < start_epoch:
                conn.execute("ROLLBACK")
                raise ValueError("ended_at must not be before start_epoch")

            new_updated_at = time.time()
            conn.execute(
                "UPDATE sessions SET name = ?, project = ?, start_epoch = ?, "
                "duration = ?, kind = ?, ended_at = ?, updated_at = ? WHERE id = ?",
                (
                    name,
                    project,
                    start_epoch,
                    duration,
                    kind,
                    ended_at,
                    new_updated_at,
                    session_id,
                ),
            )
            srow = _fetch_session(conn, session_id)
            # Unreachable in practice: the row exists within this transaction.
            if srow is None:
                raise LookupError(f"session {session_id!r} not found")
            result = _row_to_session(srow)
            _record_history(conn, result)
            conn.execute("COMMIT")
        except BaseException:
            # The not-found path raises after its own rollback; guard the
            # second rollback so the original error propagates unchanged.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    sys.stderr.write(f"pomo-server: {session_id[:8]} edited\n")
    return result


def archive_session(session_id: str, session: dict) -> tuple[bool, dict]:
    """Mark a session as archived (soft-delete). Returns (True, current_session)."""
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = _fetch_session(conn, session_id)
            if not row:
                conn.execute("ROLLBACK")
                raise ValueError(f"session {session_id!r} not found")

            existing = _row_to_session(row)
            new_updated_at = time.time()

            conn.execute(
                "UPDATE sessions SET state = 'archived', ended_at = ?, updated_at = ? WHERE id = ?",
                (existing.get("ended_at") or time.time(), new_updated_at, session_id),
            )
            conn.execute(
                "UPDATE current SET session_id = ?, updated_at = ? "
                "WHERE singleton = 0 AND session_id = ?",
                (session_id, new_updated_at, session_id),
            )
            srow = _fetch_session(conn, session_id)
            if srow:
                _record_history(conn, _row_to_session(srow))
            conn.execute("COMMIT")
        except BaseException:
            # The not-found path raises after its own rollback; guard the
            # second rollback so the original error propagates unchanged.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        sys.stderr.write(f"pomo-server: {session_id[:8]} archived\n")
        current = _current_session_locked(conn)
    return True, current


def _validate_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date: {value!r}, expected YYYY-MM-DD") from exc
    return value


def main() -> None:
    """Compatibility wrapper: the ``pomo-server`` binary now runs Django."""
    from pomo.web.manage import main as web_main

    web_main()


if __name__ == "__main__":
    main()
