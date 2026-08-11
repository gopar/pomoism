#!/usr/bin/env python3
"""Pomodoro sync server. Stdlib only (http.server + sqlite3).

Single source of truth for the current pomodoro session across machines.
Stores an append-only history; "current" is the latest non-ended session.
Conflict resolution is last-write-wins by `updated_at`.

Endpoints:
    GET  /health              -> {"ok": true}
    GET  /version             -> {"version": "0.1.0"}
    GET  /current             -> current session JSON or {"state": "idle"}
    GET  /sessions            -> today's sessions
                                 (?from=&to=&project=&state=&include_archived=)
    GET  /projects            -> distinct project names
    GET  /stats               -> aggregate stats (?from=&to=&project=)
    POST /sessions            -> upsert current (LWW), append to history
    POST /sessions/end        -> mark current ended (LWW)
    PATCH  /sessions/<id>     -> edit name/project on a session (LWW)
    POST   /sessions/<id>/archive -> soft-delete a session (LWW)

Auth: none by default. If POMO_TOKEN is set in the environment, all requests
must send `Authorization: Bearer <token>`.

Config via env:
    POMO_PORT      (default 8787)
    POMO_HOST      (default 0.0.0.0)
    POMO_DB_PATH   (default ~/.local/share/pomo/pomo.db)
    POMO_TOKEN     (optional; enables bearer auth when set)
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if sys.version_info < (3, 11):
    sys.exit(f"Error: Python 3.11+ required (current: {sys.version.split()[0]})")

from pomo import common

DB_PATH = Path(os.environ.get("POMO_DB_PATH", str(common.DB_FILE)))
PORT = int(os.environ.get("POMO_PORT", "8787"))
HOST = os.environ.get("POMO_HOST", "0.0.0.0")
TOKEN = os.environ.get("POMO_TOKEN") or None

# Reject request bodies larger than this (sessions are ~200 bytes). Guards the
# open LAN endpoint against unbounded reads / memory exhaustion.
MAX_BODY_BYTES = 65536


class RequestTooLarge(Exception):
    """Raised when a request body exceeds MAX_BODY_BYTES."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; we drive transactions explicitly with
    # BEGIN IMMEDIATE so the LWW read-modify-write cannot interleave.
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL improves read/write concurrency; busy_timeout blocks (instead of
    # instantly erroring) when another writer holds the lock. synchronous=NORMAL
    # is the safe pairing for WAL (durable across app crashes).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    with contextlib.closing(_connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id             TEXT NOT NULL,
                state          TEXT NOT NULL,
                start_epoch    INTEGER NOT NULL,
                duration       INTEGER NOT NULL,
                origin_machine TEXT NOT NULL,
                updated_at     REAL NOT NULL,
                ended_at       REAL,
                PRIMARY KEY (id, updated_at)
            )
            """
        )
        # `current` holds the id of the active session (single row, id=0).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS current (singleton INTEGER PRIMARY KEY "
            "CHECK (singleton = 0), session_id TEXT, updated_at REAL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO current (singleton, session_id, updated_at) VALUES (0, NULL, 0)"
        )
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN project TEXT")
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ALTER TABLE sessions ADD COLUMN kind TEXT")


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


def _current_session_locked(conn: sqlite3.Connection) -> dict:
    """Read the current session using an already-open connection.

    Shared by the public getter and apply_session (so the returned `current`
    can be read inside the same transaction, closing the read-after-commit gap).
    """
    row = conn.execute("SELECT session_id FROM current WHERE singleton = 0").fetchone()
    if not row or not row["session_id"]:
        return common.idle_session()
    srow = conn.execute(
        "SELECT * FROM sessions WHERE id = ? ORDER BY updated_at DESC LIMIT 1", (row["session_id"],)
    ).fetchone()
    if not srow:
        return common.idle_session()
    session = _row_to_session(srow)
    if session["state"] == "ended":
        return {"state": "idle", "updated_at": session["updated_at"], "session_id": session["id"]}
    return session


def get_current_session() -> dict:
    with contextlib.closing(_connect()) as conn:
        return _current_session_locked(conn)


def apply_session(session: dict) -> tuple[bool, dict]:
    """Insert/replace current session under last-write-wins.

    Returns (applied, current_session). If the incoming updated_at is older
    than the stored current pointer, the write is ignored (applied=False).

    The history insert and the guarded pointer UPDATE run inside a single
    BEGIN IMMEDIATE transaction. The LWW comparison lives in the UPDATE's
    WHERE clause (`? >= updated_at`), so it is atomic and cannot lose an
    update under concurrent writers.
    """
    required = ("id", "state", "start_epoch", "duration", "origin_machine", "updated_at")
    for key in required:
        if key not in session:
            raise ValueError(f"missing field: {key}")
    if session["state"] not in common.ALL_STATES:
        raise ValueError(f"invalid state: {session['state']!r}")

    with contextlib.closing(_connect()) as conn:
        incoming = float(session["updated_at"])
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Always record history (even losers) for a faithful log.
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, state, start_epoch, duration, origin_machine, "
                "updated_at, ended_at, name, project, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            cur = conn.execute(
                "UPDATE current SET session_id = ?, updated_at = ? "
                "WHERE singleton = 0 AND ? >= updated_at",
                (session["id"], incoming, incoming),
            )
            applied = cur.rowcount == 1
            sid = common.sid8(session)
            if applied:
                sys.stderr.write(f"pomo-server: {sid} applied ({session['state']})\n")
            else:
                sys.stderr.write(f"pomo-server: {sid} rejected (stale)\n")
            current = _current_session_locked(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return applied, current


def end_current(session: dict) -> tuple[bool, dict]:
    """Mark the current session ended under LWW using the provided record."""
    ended = dict(session)
    ended["state"] = "ended"
    ended.setdefault("ended_at", time.time())
    ended["ended_at"] = ended["ended_at"] or time.time()
    return apply_session(ended)


def get_sessions(
    from_date: str | None = None,
    to_date: str | None = None,
    project: str | None = None,
    state: str | None = None,
    include_archived: bool = False,
) -> list[dict]:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        where: list[str] = []
        params: list = []

        if from_date is not None:
            where.append("date(s.start_epoch, 'unixepoch', 'localtime') >= ?")
            params.append(from_date)
        else:
            where.append(
                "date(s.start_epoch, 'unixepoch', 'localtime') >= date('now', 'localtime')"
            )

        if to_date is not None:
            where.append("date(s.start_epoch, 'unixepoch', 'localtime') <= ?")
            params.append(to_date)
        elif from_date is None:
            where.append(
                "date(s.start_epoch, 'unixepoch', 'localtime') <= date('now', 'localtime')"
            )

        if project is not None:
            where.append("s.project = ?")
            params.append(project)

        if state is not None:
            where.append("s.state = ?")
            params.append(state)

        if not include_archived:
            where.append("s.state != 'archived'")

        where_clause = " AND ".join(where)

        # Inner subquery: same filters EXCEPT the archive exclusion (we want
        # the latest row regardless of state, then filter in the outer query).
        inner_parts = [w for w in where if "archived" not in w]
        inner_where = " AND ".join(inner_parts).replace("s.", "") if inner_parts else "1=1"
        inner_params = [p for w, p in zip(where, params, strict=False) if "archived" not in w]

        sql = (
            "SELECT s.* FROM sessions s "
            "INNER JOIN ("
            "  SELECT id, MAX(updated_at) AS max_updated "
            "  FROM sessions "
            f" WHERE {inner_where}"
            "  GROUP BY id"
            ") latest ON s.id = latest.id AND s.updated_at = latest.max_updated "
            f"WHERE {where_clause} "
            "ORDER BY s.start_epoch ASC"
        )
        rows = conn.execute(sql, inner_params + params).fetchall()
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
) -> dict:
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        where: list[str] = ["s.kind = 'pomodoro'"]
        params: list = []

        if not include_archived:
            where.append("s.state != 'archived'")

        if from_date is not None:
            where.append("date(s.start_epoch, 'unixepoch', 'localtime') >= ?")
            params.append(from_date)
        else:
            where.append("date(s.start_epoch, 'unixepoch', 'localtime') = date('now', 'localtime')")

        if to_date is not None:
            where.append("date(s.start_epoch, 'unixepoch', 'localtime') <= ?")
            params.append(to_date)
        elif from_date is None:
            where.append(
                "date(s.start_epoch, 'unixepoch', 'localtime') <= date('now', 'localtime')"
            )

        if project is not None:
            where.append("s.project = ?")
            params.append(project)

        where_clause = " AND ".join(where) if where else "1=1"
        # Inner subquery: same filters EXCEPT the archive exclusion.
        inner_parts = [w for w in where if "archived" not in w]
        inner_where = " AND ".join(inner_parts).replace("s.", "") if inner_parts else "1=1"
        inner_params = [p for w, p in zip(where, params, strict=False) if "archived" not in w]
        sql = (
            "SELECT s.id, s.state, s.duration, s.project, s.start_epoch FROM sessions s "
            "INNER JOIN ("
            "  SELECT id, MAX(updated_at) AS max_updated "
            "  FROM sessions "
            f" WHERE {inner_where}"
            "  GROUP BY id"
            ") latest ON s.id = latest.id AND s.updated_at = latest.max_updated "
            f"WHERE {where_clause}"
        )
        rows = conn.execute(sql, inner_params + params).fetchall()

    total_seconds = 0
    session_count = 0
    projects: dict[str, dict] = {}
    for r in rows:
        dur = int(r["duration"])
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
    """Edit name/project on the latest row for a session (LWW-guarded).

    The incoming `fields` dict must include `updated_at` for the LWW guard.
    Inserts a new history row with the updated values so the audit log is kept.
    Returns the updated session dict. Raises LookupError if session not found,
    ValueError if the write is stale.
    """
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row:
            raise LookupError(f"session {session_id!r} not found")

        session = _row_to_session(row)
        incoming_updated_at = float(fields.get("updated_at", 0))

        if incoming_updated_at < session["updated_at"]:
            raise ValueError("stale write rejected")

        name = fields.get("name") if "name" in fields else session["name"]
        project = fields.get("project") if "project" in fields else session["project"]
        new_updated_at = time.time()

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, state, start_epoch, duration, origin_machine, "
                "updated_at, ended_at, name, project, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session["id"],
                    session["state"],
                    int(session["start_epoch"]),
                    int(session["duration"]),
                    session["origin_machine"],
                    new_updated_at,
                    session.get("ended_at"),
                    name,
                    project,
                    session.get("kind"),
                ),
            )
            srow = conn.execute(
                "SELECT * FROM sessions WHERE id = ? AND updated_at = ?",
                (session_id, new_updated_at),
            ).fetchone()
            if not srow:
                conn.execute("ROLLBACK")
                raise LookupError(f"session {session_id!r} not found after insert")
            result = _row_to_session(srow)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    sys.stderr.write(f"pomo-server: {session_id[:8]} edited\n")
    return result


def archive_session(session_id: str, session: dict) -> tuple[bool, dict]:
    """Mark a session as archived (soft-delete) under LWW.

    The incoming `session` dict carries the `updated_at` for the LWW gate.
    Returns (applied, current_session).
    """
    with contextlib.closing(_connect()) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"session {session_id!r} not found")

        existing = _row_to_session(row)
        incoming = float(session.get("updated_at", 0))
        new_updated_at = time.time()

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(id, state, start_epoch, duration, origin_machine, "
                "updated_at, ended_at, name, project, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    existing["id"],
                    "archived",
                    int(existing["start_epoch"]),
                    int(existing["duration"]),
                    existing["origin_machine"],
                    new_updated_at,
                    existing.get("ended_at") or time.time(),
                    existing.get("name"),
                    existing.get("project"),
                    existing.get("kind"),
                ),
            )
            cur = conn.execute(
                "UPDATE current SET session_id = ?, updated_at = ? "
                "WHERE singleton = 0 AND session_id = ? AND ? >= updated_at",
                (session_id, new_updated_at, session_id, incoming),
            )
            applied = cur.rowcount == 1
            sid = session_id[:8]
            if applied:
                sys.stderr.write(f"pomo-server: {sid} archived\n")
            current = _current_session_locked(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return applied, current


def _validate_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid date: {value!r}, expected YYYY-MM-DD") from exc
    return value


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "pomo/1.0"

    # -- helpers ----------------------------------------------------------
    def _send_json(self, obj: dict | list, status: int = 200) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {TOKEN}"

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise RequestTooLarge(f"{length} > {MAX_BODY_BYTES}")
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def log_message(self, format, *args):  # quieter logs
        sys.stderr.write(f"{self.address_string()} - {format % args}\n")

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/health":
            return self._send_json({"ok": True})
        if path == "/version":
            return self._send_json({"version": common.version()})
        if path == "/current":
            return self._send_json(get_current_session())
        if path == "/sessions":
            project = qs.get("project", [None])[0]
            state = qs.get("state", [None])[0]
            include_archived = qs.get("include_archived", ["0"])[0] == "1"
            try:
                from_date = _validate_date(qs.get("from", [None])[0])
                to_date = _validate_date(qs.get("to", [None])[0])
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            return self._send_json(
                get_sessions(
                    from_date=from_date,
                    to_date=to_date,
                    project=project,
                    state=state,
                    include_archived=include_archived,
                )
            )
        if path == "/projects":
            return self._send_json(get_projects())
        if path == "/stats":
            try:
                from_date = _validate_date(qs.get("from", [None])[0])
                to_date = _validate_date(qs.get("to", [None])[0])
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 400)
            project = qs.get("project", [None])[0]
            include_archived = qs.get("include_archived", ["0"])[0] == "1"
            stats = get_stats(
                from_date=from_date,
                to_date=to_date,
                project=project,
                include_archived=include_archived,
            )
            return self._send_json(stats)
        return self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        try:
            payload = self._read_json()
        except RequestTooLarge:
            return self._send_json({"error": "request too large"}, 413)
        except json.JSONDecodeError:
            return self._send_json({"error": "invalid json"}, 400)

        try:
            if self.path == "/sessions":
                applied, current = apply_session(payload)
                return self._send_json({"applied": applied, "current": current})
            if self.path == "/sessions/end":
                applied, current = end_current(payload)
                return self._send_json({"applied": applied, "current": current})
            if self.path.startswith("/sessions/") and self.path.endswith("/archive"):
                session_id = self.path.split("/")[2]
                applied, current = archive_session(session_id, payload)
                return self._send_json({"applied": applied, "current": current})
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, 400)
        return self._send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        if not self._authorized():
            return self._send_json({"error": "unauthorized"}, 401)
        try:
            payload = self._read_json()
        except RequestTooLarge:
            return self._send_json({"error": "request too large"}, 413)
        except json.JSONDecodeError:
            return self._send_json({"error": "invalid json"}, 400)

        if self.path.startswith("/sessions/") and not self.path.endswith("/archive"):
            parts = self.path.split("/")
            if len(parts) == 3:
                session_id = parts[2]
                try:
                    session = edit_session(session_id, payload)
                    return self._send_json(session)
                except ValueError as exc:
                    return self._send_json({"error": str(exc)}, 400)
                except LookupError as exc:
                    return self._send_json({"error": str(exc)}, 404)
        return self._send_json({"error": "not found"}, 404)


def main() -> None:
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"pomo-server v{common.version()} listening on {HOST}:{PORT} (db={DB_PATH})\n")
    if TOKEN:
        sys.stderr.write("auth: bearer token REQUIRED\n")
    else:
        sys.stderr.write("auth: NONE (network-level only)\n")
    with contextlib.suppress(KeyboardInterrupt):
        httpd.serve_forever()


if __name__ == "__main__":
    main()
