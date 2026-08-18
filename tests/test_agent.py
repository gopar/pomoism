"""Behavior tests for agent.py: local overtime timer and server adoption.

Side-effect dispatch and the HTTP client are replaced with recorders/stubs so
tests exercise real transition logic without touching the network or macOS.
"""

from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from pomo import agent, common, hooks


class TestTickTimer:
    """Tests for tick_timer: countdown expiry, overtime transitions, and push."""

    @pytest.fixture(autouse=True)
    def _setup(self, isolated):
        self.events: list[str] = []
        self.cfg = {"server_url": "http://x", "machine_name": "laptop"}
        with patch.object(
            hooks,
            "dispatch",
            side_effect=lambda event, session, cfg, **kw: self.events.append(event),
        ):
            with patch.object(common, "post_session", return_value={}):
                yield

    def _active(self, state: str, elapsed: int, duration: int = 60) -> dict:
        start = int(time.time()) - elapsed
        s = common.new_session(state, start, duration, "laptop")
        common.write_cache(s)
        return s

    def test_no_transition_before_duration(self):
        # Given: an active pomodoro with time remaining (10s elapsed, 60s total)
        self._active("pomodoro", elapsed=10, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: state stays pomodoro, no events are fired
        assert common.read_cache()["state"] == "pomodoro"
        assert self.events == []

    def test_pomodoro_transitions_to_overtime(self):
        # Given: a pomodoro past its duration (61s elapsed, 60s total)
        before = self._active("pomodoro", elapsed=61, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: state becomes overtime, updated_at advances, overtime event fires
        after = common.read_cache()
        assert after["state"] == "overtime"
        assert after["updated_at"] >= before["updated_at"]
        assert self.events == [hooks.POMODORO_OVERTIME]

    def test_break_transitions_to_break_overtime(self):
        # Given: a break past its duration
        self._active("break", elapsed=61, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: state becomes break-overtime, break_overtime event fires
        assert common.read_cache()["state"] == "break-overtime"
        assert self.events == [hooks.BREAK_OVERTIME]

    def test_already_overtime_is_noop(self):
        # Given: already in overtime state
        self._active("overtime", elapsed=999, duration=60)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: no state change, no events fired
        assert common.read_cache()["state"] == "overtime"
        assert self.events == []

    def test_idle_is_noop(self):
        # Given: no active session (idle)
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: no events fired
        assert self.events == []

    def test_malformed_cache_is_noop(self):
        # Given: cache has active state but missing required numeric fields
        common.ensure_dirs()
        common.CACHE_FILE.write_text(
            json.dumps({"state": "pomodoro", "id": "x", "updated_at": 1.0}),
            encoding="utf-8",
        )
        # When: the timer ticks
        agent.tick_timer(self.cfg)
        # Then: no events fired (self-heals, no crash)
        assert self.events == []

    def test_offline_push_queues_outbox(self):
        # Given: an expired pomodoro, and the server is unreachable
        def boom(url, s):
            raise common.ServerUnavailable("offline")

        with patch.object(common, "post_session", side_effect=boom):
            self._active("pomodoro", elapsed=61, duration=60)
            # When: the timer ticks and attempts to push
            agent.tick_timer(self.cfg)
            # Then: the overtime session is queued in the outbox
            outbox = common.read_outbox()
            assert len(outbox) == 1
            assert outbox[0]["session"]["state"] == "overtime"


class TestPollServer:
    """Tests for poll_server: adopting remote sessions vs preserving local state."""

    @pytest.fixture(autouse=True)
    def _setup(self, isolated):
        self.adopted: list[dict] = []
        self.cfg = {"server_url": "http://x", "machine_name": "laptop"}
        with patch.object(
            agent,
            "on_remote_adopt",
            side_effect=lambda session, cfg: self.adopted.append(session),
        ):
            yield

    def test_adopts_newer_remote_session(self):
        # Given: a remote session from desktop newer than local (empty) cache
        remote = common.new_session("pomodoro", 1000, 60, "desktop")
        remote["updated_at"] = 500.0
        with patch.object(common, "get_current", return_value=remote):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: local cache is updated and on_remote_adopt is called
        assert common.read_cache()["id"] == remote["id"]
        assert len(self.adopted) == 1

    def test_keeps_local_when_remote_older(self):
        # Given: a local session newer than the remote one
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        local["updated_at"] = 900.0
        common.write_cache(local)
        remote = common.new_session("pomodoro", 1000, 60, "desktop")
        remote["updated_at"] = 100.0
        with patch.object(common, "get_current", return_value=remote):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: local session is kept (LWW favours the newer timestamp)
        assert common.read_cache()["id"] == local["id"]

    def test_server_idle_clears_stale_local(self):
        # Given: a stale local cache (ended session) and server reports idle
        local = common.new_session("ended", 1000, 0, "laptop")
        common.write_cache(local)
        common.CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(common, "get_current", return_value=common.idle_session()):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: local cache is cleared (server idle wins over stale local)
        assert common.is_idle(common.read_cache())

    def test_local_pending_active_kept_when_server_idle(self):
        # Given: a local active session not yet pushed, server reports idle
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        common.write_cache(local)
        with patch.object(common, "get_current", return_value=common.idle_session()):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: unpushed local active session is kept (not clobbered)
        assert common.read_cache()["id"] == local["id"]

    def test_remote_ended_newer_clears_locally_adopted_session(self):
        # Given: computer B adopted a pomodoro from the server (started by A)
        local = common.new_session("pomodoro", 1000, 60, "desktop")
        local["updated_at"] = 100.0
        common.write_cache(local)
        # And: A ended the session, so server now reports idle with a newer
        # timestamp (the ended record is more recent than B's local copy)
        remote_idle = {"state": "idle", "updated_at": 200.0, "session_id": local["id"]}
        with patch.object(common, "get_current", return_value=remote_idle):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: local cache is cleared because the server's end is newer
        assert common.is_idle(common.read_cache())

    def test_idle_marker_for_local_session_clears_despite_older_timestamp(self):
        # Given: a local session whose updated_at is ahead of the server
        # (the other machine's clock lags)
        local = common.new_session("pomodoro", 1000, 60, "desktop")
        local["updated_at"] = 900.0
        common.write_cache(local)
        # And: the server reports idle for this exact session with an older
        # timestamp
        remote_idle = {"state": "idle", "updated_at": 100.0, "session_id": local["id"]}
        with patch.object(common, "get_current", return_value=remote_idle):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: the server having this exact session ended is authoritative
        assert common.is_idle(common.read_cache())

    def test_adopts_remote_newer_started_session_despite_older_timestamp(self):
        # Given: a local pomodoro cached with a skewed (future) timestamp
        local = common.new_session("pomodoro", 1000, 60, "laptop")
        local["updated_at"] = 900.0
        common.write_cache(local)
        # And: the server has a break that started later, but with an older
        # timestamp (the other machine's clock lags)
        remote = common.new_session("break", 2000, 20, "desktop")
        remote["updated_at"] = 100.0
        with patch.object(common, "get_current", return_value=remote):
            # When: the agent polls the server
            agent.poll_server(self.cfg)
        # Then: the later-started remote session is adopted
        assert common.read_cache()["id"] == remote["id"]
        assert len(self.adopted) == 1


class TestOnRemoteAdopt:
    """Tests for on_remote_adopt: firing the right hooks for remote sessions."""

    @pytest.fixture(autouse=True)
    def _setup(self, isolated):
        self.events: list[tuple[str, bool]] = []
        self.cfg = {
            "server_url": "http://x",
            "machine_name": "laptop",
            "run_for_remote_sessions": True,
        }

        def record(event, session, cfg, *, remote=False):
            self.events.append((event, remote))

        with patch.object(hooks, "dispatch", new=record):
            yield

    def _session(self, state: str) -> dict:
        return common.new_session(state, 1000, 60, "desktop")

    def test_disabled_when_run_for_remote_sessions_is_false(self):
        # Given: run_for_remote_sessions is False
        self.cfg["run_for_remote_sessions"] = False
        # When: adopting a remote pomodoro session
        agent.on_remote_adopt(self._session("pomodoro"), self.cfg)
        # Then: no hooks are dispatched
        assert self.events == []

    def test_adopts_remote_pomodoro_start(self):
        # When: adopting a remote pomodoro session
        agent.on_remote_adopt(self._session("pomodoro"), self.cfg)
        # Then: pomodoro_start dispatched with remote=True
        assert self.events == [(hooks.POMODORO_START, True)]

    def test_adopts_remote_break_start(self):
        # When: adopting a remote break session
        agent.on_remote_adopt(self._session("break"), self.cfg)
        # Then: break_start dispatched with remote=True
        assert self.events == [(hooks.BREAK_START, True)]

    def test_adopts_remote_overtime(self):
        # When: adopting a remote overtime session
        agent.on_remote_adopt(self._session("overtime"), self.cfg)
        # Then: pomodoro_overtime dispatched with remote=True
        assert self.events == [(hooks.POMODORO_OVERTIME, True)]

    def test_adopts_remote_break_overtime(self):
        # When: adopting a remote break-overtime session
        agent.on_remote_adopt(self._session("break-overtime"), self.cfg)
        # Then: break_overtime dispatched with remote=True
        assert self.events == [(hooks.BREAK_OVERTIME, True)]


class _StopLoop(Exception):
    """Sentinel used to break agent.loop() deterministically in tests."""


class TestLoop:
    """Tests for the agent main loop: error resilience and shutdown."""

    @pytest.fixture(autouse=True)
    def _setup(self, isolated):
        # Config with a tiny interval; loop re-reads config each iteration.
        with (
            patch.object(
                common,
                "load_config",
                return_value={
                    "server_url": "http://x",
                    "machine_name": "laptop",
                    "poll_interval": 0,
                },
            ),
            patch.object(agent, "flush_outbox", return_value=None),
            patch.object(agent, "poll_server", return_value=None),
        ):
            yield

    def test_loop_survives_iteration_error_and_continues(self):
        # Given: tick_timer raises RuntimeError on first call, succeeds after
        self.calls = 0

        def flaky(cfg):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")

        # Break out on the 2nd sleep so we prove the loop kept going past the error.
        def sleeper(_):
            if self.calls >= 2:
                raise _StopLoop

        with (
            patch.object(agent, "tick_timer", new=flaky),
            patch.object(agent.time, "sleep", side_effect=sleeper),
        ):
            # When: the loop runs
            # Then: it survives the RuntimeError and continues iterating
            with pytest.raises(_StopLoop):
                agent.loop()
            assert self.calls >= 2

    def test_loop_does_not_swallow_keyboard_interrupt(self):
        # Given: tick_timer raises KeyboardInterrupt
        def interrupt(cfg):
            raise KeyboardInterrupt

        with (
            patch.object(agent, "tick_timer", new=interrupt),
            patch.object(agent.time, "sleep", return_value=None),
        ):
            # When / Then: KeyboardInterrupt propagates (the loop does not swallow it)
            with pytest.raises(KeyboardInterrupt):
                agent.loop()

    def test_poll_interval_below_minimum_is_clamped(self, capsys):
        # Given: config has poll_interval=1 (below minimum of 5)
        sleep_args: list[float] = []

        def capture_sleep(secs):
            if sleep_args:  # break after first sleep
                raise _StopLoop
            sleep_args.append(secs)

        with (
            patch.object(
                common,
                "load_config",
                return_value={
                    "server_url": "http://x",
                    "machine_name": "laptop",
                    "poll_interval": 1,
                },
            ),
            patch.object(agent.time, "sleep", side_effect=capture_sleep),
        ):
            # When: the loop runs
            with pytest.raises(_StopLoop):
                agent.loop()
            # Then: sleep is called with 5.0 (clamped), stderr warns about override
            assert sleep_args[0] == 5.0
            assert "clamped" in capsys.readouterr().err
