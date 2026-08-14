"""Behavior tests for pomo.py: CLI commands, state transitions, and tear-down.

Tests document current behavior; after bug fixes, expectations are updated
to the correct behaviour and re-verified.
"""

from __future__ import annotations

import builtins
import json
import time
import tomllib
from datetime import datetime
from unittest.mock import patch

import pytest

from pomo import cli as pomo
from pomo import common, hooks


class Base:
    """Shared test setup: isolated paths, recorded events, no network."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.events: list[str] = []
        self.event_sessions: list[dict] = []

        with patch.object(
            hooks,
            "dispatch",
            side_effect=lambda e, s, c, **kw: (
                self.events.append(e),
                self.event_sessions.append(s),
            ),
        ):
            with patch.object(common, "post_session", return_value={}):
                with patch.object(common, "post_end", return_value={}):
                    with patch.object(common, "enqueue_outbox"):
                        with patch.object(pomo, "_confirm_overwrite", return_value=True):
                            yield

    def _active(self, state: str = "pomodoro") -> dict:
        s = common.new_session(state, int(time.time()), 25 * 60, "laptop")
        common.write_cache(s)
        return s

    def _assert_cache_state(self, expected_state: str | None):
        session = common.read_cache()
        if expected_state is None:
            assert session is None, f"expected no cache, got {session}"
        else:
            assert session is not None, "expected active session but cache is None"
            assert session["state"] == expected_state


# ---------------------------------------------------------------------------
# Tests documenting current (broken) behaviour
# ---------------------------------------------------------------------------


class TestCmdBreak(Base):
    """pomo break N — behaviour with an active pomodoro."""

    def test_break_with_active_pomodoro_stops_first(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        # When: pomo break 5 is run
        pomo.cmd_break(5)
        # Then: session_stop fires first, then break_start
        assert self.events == [hooks.SESSION_STOP, hooks.BREAK_START]
        # Then: cache has the break session
        self._assert_cache_state("break")


class TestCmdClear(Base):
    """pomo clear — stop pomodoro, optionally start a break."""

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_with_break_stops_pomodoro_first(self, _mock):
        # Given: an active pomodoro, user enters break minutes "5"
        self._active("pomodoro")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires first, then break_start
        assert self.events == [hooks.SESSION_STOP, hooks.BREAK_START]
        # Then: cache has the break session
        self._assert_cache_state("break")

    @patch.object(builtins, "input", side_effect=["x", "5"])
    def test_clear_invalid_then_valid_break(self, _mock):
        # Given: an active pomodoro
        # When: user enters invalid "x" (re-prompted), then valid "5"
        self._active("pomodoro")
        pomo.cmd_clear()
        # Then: session_stop and break_start fire; cache has break session
        assert self.events == [hooks.SESSION_STOP, hooks.BREAK_START]
        self._assert_cache_state("break")

    @patch.object(builtins, "input", side_effect=["x", "y", ""])
    def test_clear_invalid_then_empty_clears_pomodoro(self, _mock):
        # Given: an active pomodoro
        # When: user enters invalid "x" (re-prompted), "y" (re-prompted),
        #       then "" (skip)
        self._active("pomodoro")
        pomo.cmd_clear()
        # Then: session_stop fires, no break, cache cleared
        assert self.events == [hooks.SESSION_STOP]
        self._assert_cache_state(None)

    @patch.object(builtins, "input", side_effect=[""])
    def test_clear_no_break_stops_pomodoro(self, _mock):
        # Given: an active pomodoro, user enters empty (no break)
        self._active("pomodoro")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires, cache cleared (this was already correct)
        assert self.events == [hooks.SESSION_STOP]
        self._assert_cache_state(None)

    def test_clear_active_break_stops_it(self):
        # Given: an active break
        self._active("break")
        # When: pomo clear is run
        pomo.cmd_clear()
        # Then: session_stop fires, cache cleared (this was already correct)
        assert self.events == [hooks.SESSION_STOP]
        self._assert_cache_state(None)

    @patch.object(builtins, "input", side_effect=[""])
    def test_clear_when_idle_is_noop(self, _mock):
        # Given: no active session (cache is empty/idle)
        common.clear_cache()
        # When: pomo clear is run with empty input (skip break)
        pomo.cmd_clear()
        # Then: no hooks fire, no pushes, no crash
        assert self.events == []
        self._assert_cache_state(None)


class TestStopFunction(Base):
    """stop() — low-level session teardown."""

    def test_stop_with_none_is_noop(self):
        # Given: nothing active
        common.clear_cache()
        # When: stop(None) is called
        pomo.stop(None)
        # Then: no hooks fire, no pushes, cache unchanged
        assert self.events == []
        self._assert_cache_state(None)

    def test_stop_active_session_clears_and_fires_hook(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        session = common.read_cache()
        # When: stop is called with the active session
        pomo.stop(session)
        # Then: session_stop fires, cache cleared
        assert self.events == [hooks.SESSION_STOP]
        self._assert_cache_state(None)


class TestCmdStart(Base):
    """pomo N — start pomodoro, overwriting existing session."""

    def test_start_overwrites_active_pomodoro(self):
        # Given: an active pomodoro
        self._active("pomodoro")
        # When: pomo 25 is run
        pomo.cmd_start(25)
        # Then: session_stop fires, then pomodoro_start (this was already correct)
        assert self.events == [hooks.SESSION_STOP, hooks.POMODORO_START]
        self._assert_cache_state("pomodoro")


class TestCmdStatus(Base):
    """pomo status [--json] — read current session."""

    def test_status_json_idle_no_cache(self, capsys):
        pomo.cmd_status(json_output=True)
        assert json.loads(capsys.readouterr().out) == {
            "state": "idle",
            "display": "No active session",
        }

    def test_status_json_idle_ended_cache(self, capsys):
        common.write_cache({"state": "ended"})
        pomo.cmd_status(json_output=True)
        assert json.loads(capsys.readouterr().out) == {
            "state": "idle",
            "display": "No active session",
        }

    def test_status_json_idle_marker_cache(self, capsys):
        common.write_cache({"state": "idle"})
        pomo.cmd_status(json_output=True)
        assert json.loads(capsys.readouterr().out) == {
            "state": "idle",
            "display": "No active session",
        }

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_pomodoro_countdown(self, _mock, capsys):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "pomodoro"
        assert out["start_epoch"] == int(now - 60)
        assert out["duration"] == duration
        assert out["elapsed"] == 60
        assert out["remaining"] == duration - 60
        assert out["display"] == "🍅 24:00"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_pomodoro_overtime(self, _mock, capsys):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 10)
        s = common.new_session("pomodoro", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "overtime"
        assert out["remaining"] == -10
        assert out["display"] == "⏰ +00:10"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_break_countdown(self, _mock, capsys):
        now = 1722520000.0
        duration = 5 * 60
        s = common.new_session("break", int(now - 30), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "break"
        assert out["remaining"] == duration - 30
        assert out["display"] == "☕ 04:30"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_break_overtime(self, _mock, capsys):
        now = 1722520000.0
        duration = 5 * 60
        start = int(now - duration - 5)
        s = common.new_session("break", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "break-overtime"
        assert out["remaining"] == -5
        assert out["display"] == "☕ +00:05"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_json_already_overtime_in_cache(self, _mock, capsys):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 30)
        s = common.new_session("overtime", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        out = json.loads(capsys.readouterr().out)
        assert out["state"] == "overtime"
        assert out["remaining"] == -30
        assert out["display"] == "⏰ +00:30"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_display_key_matches_human_output(self, _mock, capsys):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status(json_output=True)
        json_display = json.loads(capsys.readouterr().out)["display"]

        pomo.cmd_status()
        human = capsys.readouterr().out.strip()

        assert json_display == human

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_pomodoro_countdown(self, _mock, capsys):
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        assert capsys.readouterr().out.strip() == "🍅 24:00"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_overtime(self, _mock, capsys):
        now = 1722520000.0
        duration = 25 * 60
        start = int(now - duration - 65)
        s = common.new_session("pomodoro", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        assert capsys.readouterr().out.strip() == "⏰ +01:05"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_break_countdown(self, _mock, capsys):
        now = 1722520000.0
        duration = 5 * 60
        s = common.new_session("break", int(now - 30), duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        assert capsys.readouterr().out.strip() == "☕ 04:30"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_human_break_overtime_uses_coffee(self, _mock, capsys):
        now = 1722520000.0
        duration = 5 * 60
        start = int(now - duration - 10)
        s = common.new_session("break", start, duration, "laptop")
        common.write_cache(s)

        pomo.cmd_status()
        output = capsys.readouterr().out.strip()
        assert "☕" in output
        assert "+" in output
        assert output == "☕ +00:10"

    def test_status_human_idle(self, capsys):
        pomo.cmd_status()
        assert capsys.readouterr().out.strip() == "No active session"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_named_session_display(self, _mock, capsys):
        # Given: a named pomodoro session
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop", name="project-x")
        common.write_cache(s)

        # When: status is requested
        pomo.cmd_status()
        # Then: name appears after the timer
        assert capsys.readouterr().out.strip() == "🍅 24:00 [project-x]"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_named_session_json_includes_name(self, _mock, capsys):
        # Given: a named pomodoro session
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop", name="project-x")
        common.write_cache(s)

        # When: status --json is requested
        pomo.cmd_status(json_output=True)
        # Then: JSON output includes name
        out = json.loads(capsys.readouterr().out)
        assert out["name"] == "project-x"
        assert out["display"] == "🍅 24:00 [project-x]"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_with_project_display(self, _mock, capsys):
        # Given: a session with a project
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop", project="website")
        common.write_cache(s)
        # When: status is requested
        pomo.cmd_status()
        # Then: project is shown in brackets
        assert capsys.readouterr().out.strip() == "🍅 24:00 [website]"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_with_project_and_name_display(self, _mock, capsys):
        # Given: a session with both project and name
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session(
            "pomodoro", int(now - 60), duration, "laptop", name="fix-auth", project="website"
        )
        common.write_cache(s)
        # When: status is requested
        pomo.cmd_status()
        # Then: both are shown in brackets
        assert capsys.readouterr().out.strip() == "🍅 24:00 [website] [fix-auth]"

    @patch.object(time, "time", return_value=1722520000.0)
    def test_status_with_project_json(self, _mock, capsys):
        # Given: a session with a project
        now = 1722520000.0
        duration = 25 * 60
        s = common.new_session("pomodoro", int(now - 60), duration, "laptop", project="website")
        common.write_cache(s)
        # When: status --json is requested
        pomo.cmd_status(json_output=True)
        # Then: JSON includes project
        out = json.loads(capsys.readouterr().out)
        assert out["project"] == "website"
        assert out["display"] == "🍅 24:00 [website]"


class TestCmdClearInheritance(Base):
    """cmd_clear inherits name and project from the cleared session."""

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_inherits_name_and_project(self, _mock):
        # Given: an active pomodoro with name and project
        s = common.new_session(
            "pomodoro", int(time.time()), 25 * 60, "laptop", name="fix-auth", project="website"
        )
        common.write_cache(s)
        # When: pomo clear starts a break
        pomo.cmd_clear()
        # Then: the break session inherits name and project
        session = common.read_cache()
        assert session["state"] == "break"
        assert session["name"] == "fix-auth"
        assert session["project"] == "website"

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_inherits_only_name(self, _mock):
        # Given: an active pomodoro with name but no project
        s = common.new_session("pomodoro", int(time.time()), 25 * 60, "laptop", name="fix-auth")
        common.write_cache(s)
        # When: pomo clear starts a break
        pomo.cmd_clear()
        # Then: break inherits name, project is None
        session = common.read_cache()
        assert session["name"] == "fix-auth"
        assert session["project"] is None

    @patch.object(builtins, "input", side_effect=["5"])
    def test_clear_inherits_only_project(self, _mock):
        # Given: an active pomodoro with project but no name
        s = common.new_session("pomodoro", int(time.time()), 25 * 60, "laptop", project="website")
        common.write_cache(s)
        # When: pomo clear starts a break
        pomo.cmd_clear()
        # Then: break inherits project, name is None
        session = common.read_cache()
        assert session["name"] is None
        assert session["project"] == "website"


class TestCmdHistory(Base):
    """pomo history — session timeline with filters."""

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_human_output(self, get_sessions_mock, capsys):
        # Given: a named pomodoro session from today
        now = 1722520000.0
        s = common.new_session("pomodoro", int(now), 25 * 60, "laptop", name="fix-auth")
        s["ended_at"] = now + 25 * 60
        get_sessions_mock.return_value = [s]
        # When: pomo history is called
        pomo.cmd_history()
        output = capsys.readouterr().out
        # Then: output contains inline date, icon, name, duration, and time range
        expected_date = datetime.fromtimestamp(int(now)).strftime("%Y-%m-%d")
        expected_start = datetime.fromtimestamp(int(now)).strftime("%H:%M")
        expected_end = datetime.fromtimestamp(int(now + 25 * 60)).strftime("%H:%M")
        expected_row = f"{expected_date}  {expected_start} – {expected_end}  🍅  25:00 [fix-auth]"
        assert expected_row in output

    @patch.object(common, "get_sessions")
    def test_history_json_output(self, get_sessions_mock, capsys):
        # Given: sessions exist
        s = common.new_session("pomodoro", 1000, 60, "laptop", name="fix-auth")
        get_sessions_mock.return_value = [s]
        # When: pomo history --json is called
        pomo.cmd_history(json_output=True)
        out = json.loads(capsys.readouterr().out)
        # Then: session data is returned as JSON
        assert len(out) == 1
        assert out[0]["name"] == "fix-auth"

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_empty(self, _mock, capsys):
        # Given: no sessions today
        # When: pomo history is called
        pomo.cmd_history()
        # Then: empty message shown
        assert "No sessions" in capsys.readouterr().out

    @patch.object(common, "get_sessions", side_effect=common.ServerUnavailable("offline"))
    def test_history_offline_errors(self, _mock, capsys):
        # Given: server is unreachable
        # When / Then: pomo history exits with an error
        with pytest.raises(SystemExit):
            pomo.cmd_history()
        assert "unavailable" in capsys.readouterr().err

    @patch.object(common, "get_sessions")
    def test_history_passes_project_filter(self, get_sessions_mock, capsys):
        # Given: mock that returns empty list
        get_sessions_mock.return_value = []
        # When: pomo history --project website is called
        pomo.cmd_history(project="website")
        # Then: get_sessions was called with project filter and default None for others
        get_sessions_mock.assert_called_once_with(
            common.load_config()["server_url"],
            project="website",
            from_date=None,
            to_date=None,
            state=None,
        )

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_with_project_in_output(self, get_sessions_mock, capsys):
        # Given: a session with a project
        now = 1722520000.0
        s = common.new_session("pomodoro", int(now), 25 * 60, "laptop", project="website")
        s["ended_at"] = now + 25 * 60
        get_sessions_mock.return_value = [s]
        # When: pomo history is called
        pomo.cmd_history()
        output = capsys.readouterr().out
        # Then: project is shown in output
        assert "[website]" in output

    @patch.object(common, "get_sessions")
    def test_history_passes_from_to_state(self, get_sessions_mock, capsys):
        # Given: mock that returns empty list
        get_sessions_mock.return_value = []
        server_url = common.load_config()["server_url"]
        # When: pomo history with all new filters is called
        pomo.cmd_history(
            from_date="2026-08-01",
            to_date="2026-08-07",
            state="ended",
        )
        # Then: get_sessions was called with all filters forwarded
        get_sessions_mock.assert_called_once_with(
            server_url,
            project=None,
            from_date="2026-08-01",
            to_date="2026-08-07",
            state="ended",
        )

    @patch.object(common, "get_sessions", return_value=[])
    def test_history_inline_date_format(self, get_sessions_mock, capsys):
        # Given: two sessions on different days
        now = 1722520000.0
        s1 = common.new_session("pomodoro", int(now), 25 * 60, "laptop")
        s1["ended_at"] = now + 25 * 60
        s2 = common.new_session("break", int(now + 86400), 5 * 60, "laptop")
        s2["ended_at"] = now + 86400 + 5 * 60
        get_sessions_mock.return_value = [s1, s2]
        # When: pomo history is called
        pomo.cmd_history()
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        # Then: each row has its own date, no standalone date header line
        assert len(lines) == 2
        assert lines[0].startswith(datetime.fromtimestamp(int(now)).strftime("%Y-%m-%d"))
        assert lines[1].startswith(datetime.fromtimestamp(int(now + 86400)).strftime("%Y-%m-%d"))
        # Then: no standalone date header (a line containing only a date would
        # be caught by checking there are exactly 2 lines, not 3)


class TestCmdProjects(Base):
    """pomo projects — list all defined projects."""

    @patch.object(common, "get_projects", side_effect=common.ServerUnavailable("offline"))
    def test_projects_offline_errors(self, _mock, capsys):
        # Given: server is unreachable
        # When / Then: pomo projects exits with an error
        with pytest.raises(SystemExit):
            pomo.cmd_projects()
        assert "unavailable" in capsys.readouterr().err

    @patch.object(common, "get_projects", return_value=[])
    def test_projects_empty(self, _mock, capsys):
        # Given: no projects defined
        # When: pomo projects is called
        pomo.cmd_projects()
        # Then: empty message shown
        assert "No projects defined" in capsys.readouterr().out

    @patch.object(
        common, "get_projects", return_value=[{"project": "backend"}, {"project": "website"}]
    )
    def test_projects_list(self, _mock, capsys):
        # Given: projects exist
        # When: pomo projects is called
        pomo.cmd_projects()
        # Then: each project name is printed on its own line
        output = capsys.readouterr().out
        assert "backend" in output
        assert "website" in output
        lines = [line for line in output.split("\n") if line]
        assert lines == ["backend", "website"]

    @patch.object(
        common, "get_projects", return_value=[{"project": "backend"}, {"project": "website"}]
    )
    def test_projects_json(self, _mock, capsys):
        # Given: projects exist
        # When: pomo projects --json is called
        pomo.cmd_projects(json_output=True)
        # Then: projects returned as JSON array of objects
        out = json.loads(capsys.readouterr().out)
        assert out == [{"project": "backend"}, {"project": "website"}]


class TestCmdStats(Base):
    """pomo stats — aggregated session statistics."""

    @patch.object(common, "get_stats")
    def test_stats_human_output(self, get_stats_mock, capsys):
        # Given: stats with two ended pomodoros for "work"
        get_stats_mock.return_value = {
            "total_seconds": 50 * 60,
            "session_count": 2,
            "projects": {"work": {"seconds": 50 * 60, "count": 2}},
        }
        # When: pomo stats is called
        pomo.cmd_stats()
        output = capsys.readouterr().out
        # Then: output contains header date, session count, focus time, project breakdown
        assert datetime.now().strftime("%Y-%m-%d") in output
        assert "Sessions:    2" in output
        assert "Focus time:  50m" in output
        assert "work  2 sessions  50m" in output

    @patch.object(common, "get_stats")
    def test_stats_json_output(self, get_stats_mock, capsys):
        # Given: stats data
        stats = {"total_seconds": 1500, "session_count": 1, "projects": {}}
        get_stats_mock.return_value = stats
        # When: pomo stats --json is called
        pomo.cmd_stats(json_output=True)
        # Then: raw stats returned as JSON
        assert json.loads(capsys.readouterr().out) == stats

    @patch.object(common, "get_stats")
    def test_stats_empty(self, get_stats_mock, capsys):
        # Given: no completed sessions
        get_stats_mock.return_value = {"total_seconds": 0, "session_count": 0, "projects": {}}
        # When: pomo stats is called
        pomo.cmd_stats()
        # Then: empty message shown
        assert "No sessions" in capsys.readouterr().out

    @patch.object(common, "get_stats", side_effect=common.ServerUnavailable("offline"))
    def test_stats_offline_errors(self, _mock, capsys):
        # Given: server is unreachable
        # When / Then: pomo stats exits with an error
        with pytest.raises(SystemExit):
            pomo.cmd_stats()
        assert "unavailable" in capsys.readouterr().err

    @patch.object(common, "get_stats")
    def test_stats_passes_filters(self, get_stats_mock, capsys):
        # Given: mock that returns empty stats
        get_stats_mock.return_value = {"total_seconds": 0, "session_count": 0, "projects": {}}
        server_url = common.load_config()["server_url"]
        # When: pomo stats with all filters is called
        pomo.cmd_stats(
            from_date="2026-08-01",
            to_date="2026-08-07",
            project="website",
            include_archived=True,
        )
        # Then: get_stats was called with all filters forwarded
        get_stats_mock.assert_called_once_with(
            server_url,
            project="website",
            from_date="2026-08-01",
            to_date="2026-08-07",
            include_archived=True,
        )

    @patch.object(common, "get_stats")
    def test_stats_no_projects_section(self, get_stats_mock, capsys):
        # Given: stats with sessions but no projects
        get_stats_mock.return_value = {
            "total_seconds": 25 * 60,
            "session_count": 1,
            "projects": {},
        }
        # When: pomo stats is called
        pomo.cmd_stats()
        output = capsys.readouterr().out
        # Then: "By project:" section is not shown
        assert "By project:" not in output

    @patch.object(common, "get_stats")
    def test_stats_date_header_range(self, get_stats_mock, capsys):
        # Given: stats for a date range
        get_stats_mock.return_value = {
            "total_seconds": 25 * 60,
            "session_count": 1,
            "projects": {},
        }
        # When: pomo stats with both --from and --to is called
        pomo.cmd_stats(from_date="2026-08-01", to_date="2026-08-07")
        output = capsys.readouterr().out
        # Then: header shows date range
        assert "2026-08-01 → 2026-08-07" in output

    @patch.object(common, "get_stats")
    def test_stats_singular_session_label(self, get_stats_mock, capsys):
        # Given: single session for a project
        get_stats_mock.return_value = {
            "total_seconds": 25 * 60,
            "session_count": 1,
            "projects": {"work": {"seconds": 25 * 60, "count": 1}},
        }
        # When: pomo stats is called
        pomo.cmd_stats()
        output = capsys.readouterr().out
        # Then: "session" is singular
        assert "1 session" in output

    @patch.object(common, "get_stats")
    def test_stats_fmt_duration_sub_hour(self, get_stats_mock, capsys):
        # Given: less than an hour total
        get_stats_mock.return_value = {
            "total_seconds": 25 * 60,
            "session_count": 1,
            "projects": {"work": {"seconds": 25 * 60, "count": 1}},
        }
        # When: pomo stats is called
        pomo.cmd_stats()
        output = capsys.readouterr().out
        # Then: focus time uses "25m" format
        assert "Focus time:  25m" in output

    @patch.object(common, "get_stats")
    def test_stats_fmt_duration_hours_and_minutes(self, get_stats_mock, capsys):
        # Given: over an hour of focus time
        get_stats_mock.return_value = {
            "total_seconds": 7500,
            "session_count": 5,
            "projects": {"work": {"seconds": 7500, "count": 5}},
        }
        # When: pomo stats is called
        pomo.cmd_stats()
        output = capsys.readouterr().out
        # Then: focus time uses "2h 05m" format
        assert "Focus time:  2h 05m" in output
        assert "work  5 sessions  2h 05m" in output


class TestCmdConfig(Base):
    """pomo config — show effective config; --init creates the file."""

    def test_config_shows_effective_defaults_when_missing(self, capsys):
        # Given: no agent.toml exists
        # When: pomo config is run
        pomo.cmd_config()
        out = capsys.readouterr().out
        # Then: config file path is reported as missing and defaults are displayed
        assert str(common.CONFIG_FILE) in out
        assert "missing" in out
        assert 'server_url = "http://127.0.0.1:8787"' in out
        assert "[hooks]" in out
        assert "enabled = true" in out

    def test_config_json_when_missing(self, capsys):
        # Given: no agent.toml exists
        # When: pomo config --json is run
        pomo.cmd_config(json_output=True)
        out = json.loads(capsys.readouterr().out)
        # Then: output includes path, exists flag, and the effective config
        assert out["path"] == str(common.CONFIG_FILE)
        assert out["exists"] is False
        assert out["config"]["server_url"] == common._DEFAULT_CONFIG["server_url"]
        assert out["config"]["hooks"]["timeout"] == common._DEFAULT_CONFIG["hooks"]["timeout"]

    def test_config_init_creates_default_file(self, capsys):
        # Given: no agent.toml exists
        # When: pomo config --init is run
        pomo.cmd_config(init=True)
        # Then: the file exists and is a verbatim copy of the built-in sample
        assert common.CONFIG_FILE.exists()
        assert common.CONFIG_FILE.read_text(encoding="utf-8") == common.CONFIG_SAMPLE
        # Then: the sample parses as TOML and its values resolve on load
        assert tomllib.loads(common.CONFIG_SAMPLE)["server_url"] == "http://127.0.0.1:8787"
        cfg = common.load_config()
        assert cfg["machine_name"] == "laptop"
        assert cfg["hooks"]["timeout"] == 10
        assert "written" in capsys.readouterr().out

    def test_config_init_respects_existing_file(self, capsys):
        # Given: an existing custom agent.toml
        common.ensure_dirs()
        common.CONFIG_FILE.write_text('server_url = "http://example:1234"\n', encoding="utf-8")
        # When: pomo config --init is run
        with pytest.raises(SystemExit):
            pomo.cmd_config(init=True)
        # Then: the existing file is untouched and an error is reported
        assert (
            common.CONFIG_FILE.read_text(encoding="utf-8") == 'server_url = "http://example:1234"\n'
        )
        assert "already exists" in capsys.readouterr().err

    def test_config_shows_user_overrides(self, capsys):
        # Given: a user config overriding the server_url
        common.ensure_dirs()
        common.CONFIG_FILE.write_text('server_url = "http://example:1234"\n', encoding="utf-8")
        # When: pomo config is run
        pomo.cmd_config()
        out = capsys.readouterr().out
        # Then: the user value is shown and the file is reported as found
        assert "(found)" in out
        assert 'server_url = "http://example:1234"' in out

    def test_config_notes_server_url_env_override(self, monkeypatch, capsys):
        # Given: POMO_SERVER_URL env var is set
        monkeypatch.setenv("POMO_SERVER_URL", "http://env:9999")
        # When: pomo config is run
        pomo.cmd_config()
        out = capsys.readouterr().out
        # Then: the env override wins and is called out
        assert 'server_url = "http://env:9999"' in out
        assert "POMO_SERVER_URL" in out
