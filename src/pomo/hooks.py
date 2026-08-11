#!/usr/bin/env python3
"""User-configurable lifecycle hooks (per machine).

A hook is any executable file placed in::

    ~/.config/pomo/hooks/<event>.d/*

For a given event, every executable in that directory is run, in lexical
order of filename (use numeric prefixes like ``10-``/``20-`` to control
ordering). This is language-agnostic: shell, python, whatever is executable.

Events fired: ``pomodoro_start``, ``break_start``, ``pomodoro_overtime``,
``break_overtime``, ``session_stop``.

Each script receives the session context two ways:

  * Environment variables (convenient for shell):
      POMO_EVENT, POMO_STATE, POMO_START_EPOCH, POMO_DURATION,
      POMO_MACHINE, POMO_ORIGIN_MACHINE, POMO_REMOTE (0/1), POMO_SESSION_ID
  * The full session dict as JSON on stdin (for power users).

Hooks are best-effort: failures, missing dirs, and timeouts never crash the
caller. Stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from pomo import common

POMODORO_START = "pomodoro_start"
BREAK_START = "break_start"
POMODORO_OVERTIME = "pomodoro_overtime"
BREAK_OVERTIME = "break_overtime"
SESSION_STOP = "session_stop"

EVENTS = (
    POMODORO_START,
    BREAK_START,
    POMODORO_OVERTIME,
    BREAK_OVERTIME,
    SESSION_STOP,
)


def _hooks_root(cfg: dict) -> Path:
    """Directory that contains the per-event ``<event>.d`` subdirs."""
    override = (cfg.get("hooks") or {}).get("dir") or ""
    if override:
        return Path(override).expanduser()
    return common.HOOKS_DIR


def _iter_hook_scripts(event: str, cfg: dict):
    """Yield executable hook scripts for ``event`` in deterministic order.

    A second (global/shared) search path can be appended here later without
    touching callers.
    """
    event_dir = _hooks_root(cfg) / f"{event}.d"
    if not event_dir.is_dir():
        return
    for path in sorted(event_dir.iterdir()):
        if path.is_file() and os.access(path, os.X_OK):
            yield path


def _build_env(event: str, session: dict, remote: bool) -> dict:
    env = os.environ.copy()
    env["POMO_EVENT"] = event
    env["POMO_STATE"] = str(session.get("state", ""))
    env["POMO_START_EPOCH"] = str(session.get("start_epoch", ""))
    env["POMO_DURATION"] = str(session.get("duration", ""))
    env["POMO_ORIGIN_MACHINE"] = str(session.get("origin_machine", ""))
    env["POMO_SESSION_ID"] = str(session.get("id", ""))
    env["POMO_SESSION_PROJECT"] = str(session.get("project", ""))
    env["POMO_REMOTE"] = "1" if remote else "0"
    return env


def dispatch(event: str, session: dict | None, cfg: dict, *, remote: bool = False) -> None:
    """Run all user hooks for a lifecycle event. Never raises.

    This is the single public entry point used by the CLI and the agent so
    neither imports the other. Side effects are entirely hook-driven; the
    daemon is OS-agnostic.
    """
    hooks_cfg = cfg.get("hooks") or {}
    if not hooks_cfg.get("enabled", True):
        return
    session = session or {}
    env = _build_env(event, session, remote)
    env["POMO_MACHINE"] = str(cfg.get("machine_name", ""))
    payload = json.dumps(session).encode("utf-8")
    try:
        timeout = float(hooks_cfg.get("timeout", 10))
    except (TypeError, ValueError):
        timeout = 10.0

    for script in _iter_hook_scripts(event, cfg):
        try:
            subprocess.run(
                [str(script)],
                input=payload,
                env=env,
                timeout=timeout,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            # Best-effort: a broken/slow hook must not affect pomo.
            continue
