"""Behavior tests for hooks.py: dispatch runs user scripts with the right
context, and stay best-effort."""

from __future__ import annotations

import json
import os
import stat

import pytest

from pomo import common, hooks


def _write_hook(event_dir, name: str, body: str):
    event_dir.mkdir(parents=True, exist_ok=True)
    script = event_dir / name
    script.write_text(body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IRUSR)
    return script


class TestHooks:
    """Tests for hook dispatch: script execution, env, ordering, and robustness."""

    @pytest.fixture(autouse=True)
    def _setup(self, isolated):
        self.tmp = isolated
        self.cfg = {
            "machine_name": "laptop",
            "hooks": {"enabled": True, "timeout": 10, "dir": ""},
        }

    def _event_dir(self, event: str):
        return common.HOOKS_DIR / f"{event}.d"

    def test_dispatch_runs_matching_hook(self):
        # Given: an executable hook script for pomodoro_start
        out = self.tmp / "ran.txt"
        _write_hook(
            self._event_dir(hooks.POMODORO_START),
            "10-x.sh",
            f"#!/usr/bin/env bash\necho hi > {out}\n",
        )
        # When: dispatch is called for pomodoro_start
        session = common.new_session("pomodoro", 1, 60, "laptop")
        hooks.dispatch(hooks.POMODORO_START, session, self.cfg)
        # Then: the hook script runs and produces its output
        assert out.exists()

    def test_hook_receives_env_and_stdin(self):
        # Given: a hook script that captures env vars and stdin
        out = self.tmp / "ctx.txt"
        _write_hook(
            self._event_dir(hooks.POMODORO_START),
            "10-ctx.sh",
            "#!/usr/bin/env bash\n"
            f'printf "%s|%s|%s" "$POMO_EVENT" "$POMO_STATE" "$(cat)" > {out}\n',
        )
        # When: dispatch fires pomodoro_start with a session
        session = common.new_session("pomodoro", 1, 60, "laptop")
        hooks.dispatch(hooks.POMODORO_START, session, self.cfg)
        # Then: POMO_EVENT matches, POMO_STATE matches, stdin is the session JSON
        event, state, stdin = out.read_text(encoding="utf-8").split("|", 2)
        assert event == hooks.POMODORO_START
        assert state == "pomodoro"
        assert json.loads(stdin)["id"] == session["id"]

    def test_disabled_hooks_do_not_run(self):
        # Given: a hook script, but hooks are disabled in config
        out = self.tmp / "nope.txt"
        _write_hook(
            self._event_dir(hooks.POMODORO_START), "10-x.sh", f"#!/usr/bin/env bash\ntouch {out}\n"
        )
        self.cfg["hooks"]["enabled"] = False
        # When: dispatch is called
        hooks.dispatch(hooks.POMODORO_START, None, self.cfg)
        # Then: the script never runs
        assert not out.exists()

    def test_missing_event_dir_is_noop(self):
        # Given: no event directory for break_overtime
        # When: dispatch is called
        # Then: no error is raised (best-effort)
        hooks.dispatch(hooks.BREAK_OVERTIME, None, self.cfg)

    def test_non_executable_file_is_skipped(self):
        # Given: a script in the event dir that is readable but not executable
        out = self.tmp / "skip.txt"
        event_dir = self._event_dir(hooks.POMODORO_START)
        event_dir.mkdir(parents=True, exist_ok=True)
        script = event_dir / "10-x.sh"
        script.write_text(f"#!/usr/bin/env bash\ntouch {out}\n", encoding="utf-8")
        script.chmod(stat.S_IRUSR)  # readable, not executable
        # When: dispatch is called
        hooks.dispatch(hooks.POMODORO_START, None, self.cfg)
        # Then: the script is skipped (no output)
        assert not out.exists()

    def test_scripts_run_in_lexical_order(self):
        # Given: two hook scripts in the same event dir (20-b, 10-a)
        out = self.tmp / "order.txt"
        event_dir = self._event_dir(hooks.POMODORO_START)
        _write_hook(event_dir, "20-b.sh", f"#!/usr/bin/env bash\nprintf b >> {out}\n")
        _write_hook(event_dir, "10-a.sh", f"#!/usr/bin/env bash\nprintf a >> {out}\n")
        # When: dispatch is called
        hooks.dispatch(hooks.POMODORO_START, None, self.cfg)
        # Then: scripts run in lexical filename order (10-a before 20-b)
        assert out.read_text(encoding="utf-8") == "ab"

    def test_hook_receives_project_env_var(self):
        # Given: a hook script that captures POMO_SESSION_PROJECT
        out = self.tmp / "proj.txt"
        _write_hook(
            self._event_dir(hooks.POMODORO_START),
            "10-proj.sh",
            f"#!/usr/bin/env bash\nprintf '%s' \"$POMO_SESSION_PROJECT\" > {out}\n",
        )
        # When: dispatch fires with a session that has a project
        session = common.new_session("pomodoro", 1, 60, "laptop", project="website")
        hooks.dispatch(hooks.POMODORO_START, session, self.cfg)
        # Then: POMO_SESSION_PROJECT is set
        assert out.read_text(encoding="utf-8") == "website"

    def test_build_env_has_expected_pomo_keys(self, monkeypatch):
        # Given: a session with a project and no inherited POMO_* variables
        for key in list(os.environ):
            if key.startswith("POMO_"):
                monkeypatch.delenv(key)
        session = common.new_session("pomodoro", 1, 60, "laptop", project="website")
        # When: env is built
        env = hooks._build_env("pomodoro_start", session, remote=False)
        # Then: exactly the expected POMO_* keys are present
        expected = {
            "POMO_EVENT",
            "POMO_STATE",
            "POMO_START_EPOCH",
            "POMO_DURATION",
            "POMO_ORIGIN_MACHINE",
            "POMO_SESSION_ID",
            "POMO_SESSION_PROJECT",
            "POMO_REMOTE",
        }
        pomo_keys = {k for k in env if k.startswith("POMO_")}
        assert pomo_keys == expected, "POMO_* env vars changed — update README.md?"
