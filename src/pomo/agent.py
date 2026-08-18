#!/usr/bin/env python3
"""Pomodoro local agent (one per machine).

Long-running daemon that keeps this machine in sync with the server and owns
the countdown timer.

Responsibilities:
  1. Poll  GET /current every `poll_interval` seconds. If the server's session
     is newer than our cache, adopt it (update cache) and, when
     configured, fire lifecycle hooks for remote-originated sessions.
  2. Local timer: when the active session passes start+duration, transition
     pomodoro->overtime / break->break-overtime, fire hooks, and push
     the transition to the server.
  3. Outbox flush: pending pushes queued by the CLI while offline are sent on
     each loop; last-write-wins on the server resolves conflicts.

Side effects are entirely hook-driven (see hooks.py), so the daemon itself is
OS-agnostic. Timer stays local so everything works offline. Stdlib only.
"""

from __future__ import annotations

import contextlib
import sys
import time
import traceback

if sys.version_info < (3, 11):
    sys.exit(f"Error: Python 3.11+ required (current: {sys.version.split()[0]})")

from pomo import common, hooks

OVERTIME_OF = {"pomodoro": "overtime", "break": "break-overtime"}
MIN_POLL_INTERVAL = 5.0


def on_remote_adopt(session: dict, cfg: dict) -> None:
    """Fire hooks when we adopt a session that started on another machine."""
    if not cfg.get("run_for_remote_sessions"):
        return
    state = session.get("state")
    if state == "pomodoro":
        hooks.dispatch(hooks.POMODORO_START, session, cfg, remote=True)
    elif state == "break":
        hooks.dispatch(hooks.BREAK_START, session, cfg, remote=True)
    elif state == "overtime":
        hooks.dispatch(hooks.POMODORO_OVERTIME, session, cfg, remote=True)
    elif state == "break-overtime":
        hooks.dispatch(hooks.BREAK_OVERTIME, session, cfg, remote=True)


# ---------------------------------------------------------------------------
# Sync helpers
# ---------------------------------------------------------------------------


def _updated_at(session: dict | None) -> float:
    if not session:
        return 0.0
    return float(session.get("updated_at") or 0.0)


def flush_outbox(cfg: dict) -> None:
    items = common.read_outbox()
    if not items:
        return
    remaining: list[dict] = []
    for item in items:
        try:
            if item["action"] == "end":
                common.post_end(cfg["server_url"], item["session"])
            else:
                common.post_session(cfg["server_url"], item["session"])
        except common.ServerUnavailable:
            remaining.append(item)  # keep for next attempt
    common.rewrite_outbox(remaining)
    if len(items) != len(remaining):
        flushed = len(items) - len(remaining)
        sys.stderr.write(f"pomo-agent: flushed {flushed} queued item(s)\n")


def poll_server(cfg: dict) -> None:
    """Adopt the server's session if it is newer than our cache."""
    try:
        remote = common.get_current(cfg["server_url"])
    except common.ServerUnavailable:
        return
    local = common.read_cache()
    if common.is_idle(remote):
        if local and not common.is_idle(local):
            # The server reporting this exact session ended is authoritative,
            # even if its timestamp is older (clock skew); otherwise LWW.
            ended_ours = remote.get("session_id") == local.get("id")
            if ended_ours or _updated_at(remote) > _updated_at(local):
                common.clear_cache()
            return
        if local is not None:
            common.clear_cache()
        return
    newer_timestamp = _updated_at(remote) > _updated_at(local)
    later_started = (
        local is not None
        and remote.get("id") != local.get("id")
        and float(remote.get("start_epoch") or 0) > float(local.get("start_epoch") or 0)
    )
    if newer_timestamp or later_started:
        remote_started_elsewhere = (
            not local or remote.get("id") != (local or {}).get("id")
        ) and remote.get("origin_machine") != cfg["machine_name"]
        common.write_cache(remote)
        sys.stderr.write(
            f"pomo-agent: adopted {common.sid8(remote)} "
            f"({remote['state']}, {remote.get('origin_machine', '?')}, "
            f"{int(remote['duration']) // 60}m)\n"
        )
        if remote_started_elsewhere:
            on_remote_adopt(remote, cfg)


def tick_timer(cfg: dict) -> None:
    """Advance the local session into overtime when its duration elapses."""
    session = common.read_cache()
    if common.is_idle(session):
        return
    assert session is not None
    state = session.get("state")
    overtime_state = OVERTIME_OF.get(state)
    if overtime_state is None:
        return  # already in an overtime state; nothing to do
    start_epoch = session.get("start_epoch")
    duration = session.get("duration")
    if not isinstance(start_epoch, int | float) or not isinstance(duration, int | float):
        return  # malformed cache; read_cache normally filters this out
    elapsed = time.time() - start_epoch
    if elapsed < duration:
        return
    # Transition to overtime locally, fire side effects, push.
    session["state"] = overtime_state
    session["updated_at"] = time.time()
    common.write_cache(session)
    sys.stderr.write(f"pomo-agent: overtime {common.sid8(session)}\n")
    overtime_event = (
        hooks.BREAK_OVERTIME if overtime_state == "break-overtime" else hooks.POMODORO_OVERTIME
    )
    hooks.dispatch(overtime_event, session, cfg)
    try:
        common.post_session(cfg["server_url"], session)
        sys.stderr.write(f"pomo-agent: pushed {common.sid8(session)}\n")
    except common.ServerUnavailable:
        common.enqueue_outbox("session", session)
        sys.stderr.write(f"pomo-agent: offline (queued {common.sid8(session)})\n")


def _poll_interval(cfg: dict) -> float:
    raw = float(cfg.get("poll_interval", 5))
    if raw < MIN_POLL_INTERVAL:
        sys.stderr.write(
            f"pomo-agent: poll_interval clamped to {MIN_POLL_INTERVAL}s (config had {raw}s)\n"
        )
        return MIN_POLL_INTERVAL
    return raw


def loop() -> None:
    cfg = common.load_config()
    interval = _poll_interval(cfg)
    sys.stderr.write(
        f"pomo-agent: v{common.version()} machine={cfg['machine_name']} "
        f"server={cfg['server_url']} "
        f"interval={interval}s\n"
    )
    while True:
        cfg = common.load_config()  # re-read so config edits take effect live
        try:
            flush_outbox(cfg)
            poll_server(cfg)
            tick_timer(cfg)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the daemon
            # KeyboardInterrupt/SystemExit derive from BaseException and still
            # propagate, so Ctrl-C and shutdown work. Everything else is logged
            # and the loop continues (self-heals on the next iteration).
            sys.stderr.write("pomo-agent: iteration error:\n" + traceback.format_exc())
        time.sleep(_poll_interval(cfg))


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        loop()


if __name__ == "__main__":
    main()
