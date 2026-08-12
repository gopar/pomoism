"""Behavior tests for server.py: last-write-wins, history, and end/idle.

Includes concurrency tests that exercise the WHERE-guarded pointer UPDATE under
concurrent writers (WAL + busy_timeout).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from pomo import common, server


def _session(updated_at: float, state: str = "pomodoro", sid: str | None = None) -> dict:
    s = common.new_session(state, 1000, 60, "laptop")
    if sid is not None:
        s["id"] = sid
    s["updated_at"] = updated_at
    return s


class TestLWW:
    """Tests for apply_session / end_current: LWW semantics and state handling."""

    @pytest.fixture(autouse=True)
    def _init_db(self, isolated, monkeypatch):
        monkeypatch.setattr(server, "DB_PATH", isolated / "data" / "pomo.db")
        server.init_db()

    def test_version_is_set(self):
        v = common.version()
        assert v
        assert v != "unknown"

    def test_apply_missing_field_raises(self):
        # When: apply_session is called with a dict missing required fields
        # Then: ValueError is raised
        with pytest.raises(ValueError):
            server.apply_session({"id": "x"})

    def test_apply_invalid_state_raises(self):
        # Given: a session with a bogus state
        bad = _session(1.0)
        bad["state"] = "bogus"
        # When: apply_session is called
        # Then: ValueError is raised
        with pytest.raises(ValueError):
            server.apply_session(bad)

    def test_newer_write_wins(self):
        # Given: an older session "a" is applied
        applied, current = server.apply_session(_session(100.0, sid="a"))
        assert applied
        assert current["id"] == "a"
        # When: a newer session "b" is applied
        applied, current = server.apply_session(_session(200.0, sid="b"))
        # Then: "b" wins the current pointer
        assert applied
        assert current["id"] == "b"

    def test_all_writes_are_recorded(self):
        # Given: a session "winner" is applied
        server.apply_session(_session(200.0, sid="winner"))
        # When: another session "loser" is applied
        applied, current = server.apply_session(_session(100.0, sid="loser"))
        # Then: all writes are accepted (no stale rejection)
        assert applied
        assert current["id"] == "loser"
        # Then: both sessions are recorded in the sessions table
        with sqlite3.connect(server.DB_PATH) as conn:
            ids = {r[0] for r in conn.execute("SELECT id FROM sessions")}
        assert "loser" in ids
        assert "winner" in ids

    def test_ended_pointer_reports_idle(self):
        # Given: an active session applied, then ended
        server.apply_session(_session(100.0, sid="a"))
        server.end_current(_session(200.0, sid="a"))
        # When / Then: get_current_session reports idle
        assert common.is_idle(server.get_current_session())

    def test_ended_pointer_idle_response_includes_timestamp_and_session_id(self):
        # Given: an active session applied, then ended
        s = _session(200.0, sid="a")
        server.apply_session(s)
        server.end_current(_session(300.0, sid="a"))
        # When: get_current_session reports idle because the session ended
        current = server.get_current_session()
        # Then: the idle response carries updated_at and session_id so
        # agents can compare whether the remote-end is newer than local
        assert common.is_idle(current)
        assert "updated_at" in current
        assert "session_id" in current
        assert current["session_id"] == "a"

    def test_end_current_sets_ended_at(self):
        # Given: an active session
        server.apply_session(_session(100.0, sid="a"))
        # When: end_current is called
        applied, _ = server.end_current(_session(200.0, sid="a"))
        # Then: session is marked ended with ended_at set
        assert applied
        with sqlite3.connect(server.DB_PATH) as conn:
            row = conn.execute(
                "SELECT state, ended_at FROM sessions WHERE id = 'a' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        assert row[0] == "ended"
        assert row[1] is not None

    def test_last_write_wins_in_sessions_preserved_in_history(self):
        # Given: session "a" applied at t=200
        server.apply_session(_session(200.0, sid="a"))
        # When: same session "a" re-applied at t=100 (older, but last writer wins)
        applied, _ = server.apply_session(_session(100.0, sid="a"))
        # Then: the write is accepted
        assert applied
        # Then: sessions reflects the last write (t=100)
        with sqlite3.connect(server.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT updated_at, state FROM sessions WHERE id = 'a'").fetchall()
        assert len(rows) == 1
        assert rows[0]["updated_at"] == 100.0
        # Then: session_history has both writes (audit trail preserved)
        with sqlite3.connect(server.DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            hrows = conn.execute(
                "SELECT updated_at, state FROM session_history WHERE id = 'a'"
                " ORDER BY updated_at ASC"
            ).fetchall()
        assert len(hrows) == 2
        assert hrows[0]["updated_at"] == 100.0  # last write (applied second)
        assert hrows[1]["updated_at"] == 200.0  # first write (applied first)

    def test_name_survives_roundtrip(self):
        # Given: a session is created with a name
        s = _session(100.0, sid="named")
        s["name"] = "project-x"
        # When: it is applied and read back
        server.apply_session(s)
        current = server.get_current_session()
        # Then: name is preserved
        assert current["name"] == "project-x"

    def test_get_today_sessions_returns_today_only(self):
        # Given: a session from today and one from yesterday
        today_epoch = int(time.time())
        yesterday_epoch = today_epoch - 90000
        s_today = common.new_session("pomodoro", today_epoch, 60, "laptop")
        s_yesterday = common.new_session("pomodoro", yesterday_epoch, 60, "laptop")
        server.apply_session(s_today)
        server.apply_session(s_yesterday)
        # When: get_today_sessions is called
        sessions = server.get_today_sessions()
        # Then: only today's session is returned
        assert len(sessions) == 1
        assert sessions[0]["id"] == s_today["id"]

    def test_get_today_sessions_deduplicates_to_latest(self):
        # Given: the same session id written twice today
        now = int(time.time())
        start_epoch = now - 60
        s1 = common.new_session("pomodoro", start_epoch, 60, "laptop")
        s1["updated_at"] = now - 1
        s2 = dict(s1)
        s2["state"] = "ended"
        s2["updated_at"] = now
        s2["ended_at"] = now
        server.apply_session(s1)
        server.apply_session(s2)
        # When: get_today_sessions is called
        sessions = server.get_today_sessions()
        # Then: only the latest row (ended) is returned
        assert len(sessions) == 1
        assert sessions[0]["state"] == "ended"

    def test_get_today_sessions_returns_empty_when_none(self):
        # Given: no sessions exist
        # When: get_today_sessions is called
        sessions = server.get_today_sessions()
        # Then: empty list
        assert sessions == []

    def test_project_survives_roundtrip(self):
        # Given: a session is created with a project
        s = _session(100.0, sid="proj")
        s["project"] = "website"
        # When: it is applied and read back
        server.apply_session(s)
        current = server.get_current_session()
        # Then: project is preserved
        assert current["project"] == "website"

    def test_get_today_sessions_filters_by_project(self):
        # Given: sessions with different projects today
        s1 = common.new_session("pomodoro", int(time.time()) - 60, 60, "laptop", project="website")
        s2 = common.new_session("pomodoro", int(time.time()) - 120, 60, "laptop", project="backend")
        server.apply_session(s1)
        server.apply_session(s2)
        # When: get_today_sessions is called with project filter
        filtered = server.get_today_sessions(project="website")
        # Then: only matching sessions are returned
        assert len(filtered) == 1
        assert filtered[0]["project"] == "website"

    def test_get_projects_returns_distinct(self):
        # Given: sessions with various projects
        s1 = common.new_session("pomodoro", int(time.time()) - 60, 60, "laptop", project="website")
        s2 = common.new_session("pomodoro", int(time.time()) - 120, 60, "laptop", project="backend")
        s3 = common.new_session("pomodoro", int(time.time()) - 180, 60, "laptop", project="website")
        server.apply_session(s1)
        server.apply_session(s2)
        server.apply_session(s3)
        # When: get_projects is called
        projects = server.get_projects()
        # Then: distinct projects sorted alphabetically
        assert projects == [{"project": "backend"}, {"project": "website"}]

    def test_get_projects_empty_when_none(self):
        # Given: no sessions with projects exist
        # When: get_projects is called
        projects = server.get_projects()
        # Then: empty list
        assert projects == []

    def test_kind_survives_roundtrip(self):
        # Given: a pomodoro session with kind="pomodoro"
        s = _session(100.0, sid="kind-test")
        s["kind"] = "pomodoro"
        # When: it is applied and read back
        server.apply_session(s)
        current = server.get_current_session()
        # Then: kind is preserved
        assert current["kind"] == "pomodoro"

    def test_kind_preserved_after_ended(self):
        # Given: a pomodoro session, then ended (use recent timestamps
        # close to now to avoid UTC date boundary issues).
        now = time.time()
        s = common.new_session("pomodoro", int(now) - 60, 60, "laptop")
        s["updated_at"] = now - 30
        s["kind"] = "pomodoro"
        server.apply_session(s)
        s2 = dict(s)
        s2["updated_at"] = now - 1
        server.end_current(s2)
        # When: reading today's sessions
        sessions = server.get_today_sessions()
        # Then: the ended record still has kind="pomodoro"
        assert len(sessions) == 1
        assert sessions[0]["kind"] == "pomodoro"
        assert sessions[0]["state"] == "ended"


class TestConcurrency:
    """LWW must hold under concurrent writers.

    apply_session runs the history insert and a WHERE-guarded pointer UPDATE
    inside one BEGIN IMMEDIATE transaction (WAL + busy_timeout), so concurrent
    writers cannot lose the highest-updated_at winner.
    """

    @pytest.fixture(autouse=True)
    def _init_db(self, isolated, monkeypatch):
        monkeypatch.setattr(server, "DB_PATH", isolated / "data" / "pomo.db")
        server.init_db()

    def test_concurrent_apply_succeeds_under_load(self):
        # Given: n sessions with sequential updated_at timestamps
        n = 25
        sessions = [_session(float(i + 1), sid=f"s{i:02d}") for i in range(n)]

        errors: list[Exception] = []
        barrier = threading.Barrier(n)

        def worker(s: dict) -> None:
            try:
                barrier.wait()
                server.apply_session(s)
            except Exception as exc:  # noqa: BLE001 - collected for assertion
                errors.append(exc)

        # When: all sessions are applied concurrently from n threads
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(worker, sessions))

        # Then: no errors under load, all sessions recorded
        assert errors == [], f"apply_session raised under load: {errors}"

        current = server.get_current_session()
        assert "id" in current, "current pointer must point to a valid session"

        with sqlite3.connect(server.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == n, "not all sessions were recorded in sessions"

    def test_concurrent_mixed_order_succeeds_under_load(self):
        # Given: updated_at values in shuffled order across threads
        pairs = [
            (f"s{i:02d}", float(v)) for i, v in enumerate([50, 10, 99, 30, 70, 5, 88, 42, 60, 15])
        ]

        errors: list[Exception] = []
        barrier = threading.Barrier(len(pairs))

        def worker(pair) -> None:
            sid, ts = pair
            try:
                barrier.wait()
                server.apply_session(_session(ts, sid=sid))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        # When: all sessions are applied concurrently in shuffled order
        with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
            list(pool.map(worker, pairs))

        # Then: no errors, current points to a valid session, all recorded
        assert errors == [], f"apply_session raised under load: {errors}"
        assert "id" in server.get_current_session()

        with sqlite3.connect(server.DB_PATH) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert count == len(pairs)


class TestGetSessions:
    """Date-range, state, and archive filtering for get_sessions."""

    @pytest.fixture(autouse=True)
    def _init_db(self, isolated, monkeypatch):
        monkeypatch.setattr(server, "DB_PATH", isolated / "data" / "pomo.db")
        server.init_db()

    def test_no_params_returns_today(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop")
        server.apply_session(s)
        sessions = server.get_sessions()
        assert len(sessions) == 1
        assert sessions[0]["id"] == s["id"]

    def test_date_range_filtering(self):
        # Given: sessions on today and yesterday
        now = int(time.time())
        today = common.new_session("pomodoro", now - 60, 60, "laptop")
        yesterday_epoch = now - 100000
        yesterday = common.new_session("pomodoro", yesterday_epoch, 60, "laptop")
        server.apply_session(today)
        server.apply_session(yesterday)
        # When: filtered to yesterday's date only
        yesterday_str = datetime.fromtimestamp(yesterday_epoch).date().isoformat()
        sessions = server.get_sessions(from_date=yesterday_str, to_date=yesterday_str)
        # Then: only yesterday's session returned
        assert len(sessions) == 1
        assert sessions[0]["id"] == yesterday["id"]

    def test_state_filtering(self):
        now = int(time.time())
        s1 = common.new_session("pomodoro", now - 60, 60, "laptop")
        s2 = common.new_session("break", now - 120, 60, "laptop")
        server.apply_session(s1)
        server.apply_session(s2)
        sessions = server.get_sessions(state="break")
        assert len(sessions) == 1
        assert sessions[0]["kind"] == "break"

    def test_include_archived(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop")
        server.apply_session(s)
        server.archive_session(s["id"], {"updated_at": time.time() + 1})
        sessions = server.get_sessions()
        assert len(sessions) == 0
        sessions = server.get_sessions(include_archived=True)
        assert len(sessions) == 1
        assert sessions[0]["state"] == "archived"


class TestStats:
    """get_stats aggregates session data."""

    @pytest.fixture(autouse=True)
    def _init_db(self, isolated, monkeypatch):
        monkeypatch.setattr(server, "DB_PATH", isolated / "data" / "pomo.db")
        server.init_db()

    def test_stats_today_only(self):
        now = int(time.time())
        s1 = common.new_session("pomodoro", now - 60, 25 * 60, "laptop", project="work")
        s2 = common.new_session("pomodoro", now - 120, 5 * 60, "laptop", project="work")
        server.apply_session(s1)
        server.apply_session(s2)
        s1["ended_at"] = s1["start_epoch"] + 25 * 60
        server.end_current(s1)
        s2["ended_at"] = s2["start_epoch"] + 5 * 60
        server.end_current(s2)
        stats = server.get_stats()
        assert stats["session_count"] == 2
        assert stats["total_seconds"] == 30 * 60
        assert stats["projects"]["work"]["seconds"] == 30 * 60
        assert stats["projects"]["work"]["count"] == 2

    def test_stats_excludes_archived(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 25 * 60, "laptop", project="work")
        server.apply_session(s)
        s["ended_at"] = s["start_epoch"] + 25 * 60
        server.end_current(s)
        server.archive_session(s["id"], {"updated_at": time.time() + 1})
        stats = server.get_stats()
        assert stats["session_count"] == 0
        assert stats["total_seconds"] == 0

    def test_stats_include_archived(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 25 * 60, "laptop", project="work")
        server.apply_session(s)
        s["ended_at"] = s["start_epoch"] + 25 * 60
        server.end_current(s)
        server.archive_session(s["id"], {"updated_at": time.time() + 1})
        stats = server.get_stats(include_archived=True)
        assert stats["session_count"] == 1
        assert stats["total_seconds"] == 25 * 60
        assert stats["projects"]["work"]["seconds"] == 25 * 60

    def test_stats_project_breakdown(self):
        now = int(time.time())
        s1 = common.new_session("pomodoro", now - 60, 25 * 60, "laptop", project="work")
        s2 = common.new_session("pomodoro", now - 120, 15 * 60, "laptop", project="learning")
        server.apply_session(s1)
        server.apply_session(s2)
        s1["ended_at"] = s1["start_epoch"] + 25 * 60
        server.end_current(s1)
        s2["ended_at"] = s2["start_epoch"] + 15 * 60
        server.end_current(s2)
        stats = server.get_stats()
        assert stats["projects"]["work"]["seconds"] == 25 * 60
        assert stats["projects"]["learning"]["seconds"] == 15 * 60

    def test_stats_date_range(self):
        # Given: sessions on today and two days ago
        now = int(time.time())
        today = common.new_session("pomodoro", now - 60, 25 * 60, "laptop", project="work")
        old_epoch = now - 172800  # two days ago
        old = common.new_session("pomodoro", old_epoch, 25 * 60, "laptop", project="work")
        server.apply_session(today)
        server.apply_session(old)
        today["ended_at"] = today["start_epoch"] + 25 * 60
        server.end_current(today)
        old["ended_at"] = old["start_epoch"] + 25 * 60
        server.end_current(old)
        # When: filtered to today only
        today_str = datetime.now().strftime("%Y-%m-%d")
        stats = server.get_stats(from_date=today_str, to_date=today_str)
        # Then: only today's session counted
        assert stats["session_count"] == 1
        assert stats["total_seconds"] == 25 * 60


class TestEditSession:
    """PATCH /sessions/<id> — edit name/project."""

    @pytest.fixture(autouse=True)
    def _init_db(self, isolated, monkeypatch):
        monkeypatch.setattr(server, "DB_PATH", isolated / "data" / "pomo.db")
        server.init_db()

    def test_edit_name(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop", name="old name")
        server.apply_session(s)
        updated = server.edit_session(s["id"], {"name": "new name", "updated_at": time.time() + 1})
        assert updated["name"] == "new name"
        assert updated["project"] == s["project"]

    def test_edit_project(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop", project="old-proj")
        server.apply_session(s)
        updated = server.edit_session(
            s["id"], {"project": "new-proj", "updated_at": time.time() + 1}
        )
        assert updated["project"] == "new-proj"
        assert updated["name"] == s["name"]

    def test_edit_not_found_raises(self):
        with pytest.raises(LookupError):
            server.edit_session("nonexistent", {"name": "nope", "updated_at": 1.0})

    def test_edit_applies_regardless_of_timestamp(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop", name="original")
        server.apply_session(s)
        # Given a stale timestamp, the edit still succeeds (no stale rejection)
        updated = server.edit_session(s["id"], {"name": "renamed", "updated_at": 0.0})
        assert updated["name"] == "renamed"

    def test_edit_updates_in_place_and_logs_history(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop", name="v1")
        server.apply_session(s)
        server.edit_session(s["id"], {"name": "v2", "updated_at": time.time() + 1})
        # Then: sessions has one row with the new name (mutated in place)
        with sqlite3.connect(server.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT name, updated_at FROM sessions WHERE id = ?", (s["id"],)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "v2"
        # Then: session_history has both versions (audit trail preserved)
        with sqlite3.connect(server.DB_PATH) as conn:
            hrows = conn.execute(
                "SELECT name FROM session_history WHERE id = ? ORDER BY updated_at ASC",
                (s["id"],),
            ).fetchall()
        assert len(hrows) == 2
        assert hrows[0][0] == "v1"
        assert hrows[1][0] == "v2"


class TestArchiveSession:
    """POST /sessions/<id>/archive — soft-delete."""

    @pytest.fixture(autouse=True)
    def _init_db(self, isolated, monkeypatch):
        monkeypatch.setattr(server, "DB_PATH", isolated / "data" / "pomo.db")
        server.init_db()

    def test_archive_sets_state(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop")
        server.apply_session(s)
        applied, _ = server.archive_session(s["id"], {"updated_at": time.time() + 1})
        assert applied
        current = server.get_current_session()
        assert common.is_idle(current)

    def test_archive_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            server.archive_session("nonexistent", {"updated_at": 1.0})

    def test_archive_record_exists_in_history(self):
        now = int(time.time())
        s = common.new_session("pomodoro", now - 60, 60, "laptop")
        server.apply_session(s)
        server.archive_session(s["id"], {"updated_at": time.time() + 1})
        sessions = server.get_sessions(include_archived=True)
        assert len(sessions) == 1
        assert sessions[0]["state"] == "archived"


class TestValidateDate:
    def test_none_returns_none(self):
        assert server._validate_date(None) is None

    def test_valid_date_returns_unchanged(self):
        assert server._validate_date("2024-01-15") == "2024-01-15"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="invalid date"):
            server._validate_date("01-15-2024")

    def test_not_a_date_raises(self):
        with pytest.raises(ValueError, match="invalid date"):
            server._validate_date("not-a-date")

    def test_bad_month_day_raises(self):
        with pytest.raises(ValueError, match="invalid date"):
            server._validate_date("2024-13-40")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="invalid date"):
            server._validate_date("")
