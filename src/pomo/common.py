"""Shared helpers for the pomodoro sync service.

Pure stdlib. Used by server.py, agent.py, and pomo.py (CLI).

Responsibilities:
- Canonical filesystem paths (config, cache, db, outbox).
- Session model helpers.
- Local cache read/write (JSON).
- Minimal HTTP JSON client (urllib) with optional bearer token.
- agent.toml config loading (tomllib, stdlib in 3.11+).
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, cast

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib: Any = None

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------


def version() -> str:
    try:
        from importlib.metadata import version as _get_version

        return _get_version("pomoism")
    except Exception:
        pass
    try:
        vf = Path(__file__).resolve().parent / "VERSION"
        return vf.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "unknown"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "pomo"
CACHE_DIR = HOME / ".cache" / "pomo"
DATA_DIR = HOME / ".local" / "share" / "pomo"

CONFIG_FILE = CONFIG_DIR / "agent.toml"
CACHE_FILE = CACHE_DIR / "current.json"
OUTBOX_FILE = CACHE_DIR / "outbox.jsonl"
DB_FILE = DATA_DIR / "pomo.db"

# Per-machine hook scripts. Executables in HOOKS_DIR/<event>.d/ run on the
# matching lifecycle event (see hooks.py). Local to each machine.
HOOKS_DIR = CONFIG_DIR / "hooks"

# Valid session states. `ended` is explicit so stops can propagate over the
# network (a file deletion cannot be synced; an `ended` record can).
ACTIVE_STATES = ("pomodoro", "overtime", "break", "break-overtime")
ALL_STATES = ACTIVE_STATES + ("ended", "archived")


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, CACHE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Session model
# ---------------------------------------------------------------------------


def new_session(
    state: str,
    start_epoch: int,
    duration: int,
    origin_machine: str,
    name: str | None = None,
    project: str | None = None,
) -> dict:
    """Build a session record with a fresh id and updated_at."""
    if state not in ALL_STATES:
        raise ValueError(f"invalid state: {state!r}")
    kind = "break" if state in ("break", "break-overtime") else "pomodoro"
    return {
        "id": str(uuid.uuid4()),
        "state": state,
        "start_epoch": int(start_epoch),
        "duration": int(duration),
        "origin_machine": origin_machine,
        "updated_at": time.time(),
        "ended_at": None,
        "name": name,
        "project": project,
        "kind": kind,
    }


def idle_session() -> dict:
    return {"state": "idle"}


def sid8(session: dict) -> str:
    return session.get("id", "?")[:8]


def is_idle(session: dict | None) -> bool:
    return not session or session.get("state") in (None, "idle", "ended", "archived")


# ---------------------------------------------------------------------------
# Local cache (JSON)
# ---------------------------------------------------------------------------


def _valid_session(obj: object) -> bool:
    """True if `obj` is a well-formed cached session.

    Idle markers (`{"state": "idle"}`, or state ``ended``/``None``) need only a
    recognizable state. Active/ended records must carry the fields the timer
    reads so a truncated or hand-edited cache can't crash the agent.
    """
    if not isinstance(obj, dict):
        return False
    state = obj.get("state")
    if state in (None, "idle", "ended"):
        return True
    if state not in ALL_STATES:
        return False
    if not isinstance(obj.get("start_epoch"), int | float):
        return False
    if not isinstance(obj.get("duration"), int | float):
        return False
    return "id" in obj and "updated_at" in obj


def read_cache() -> dict | None:
    """Return the cached session, or None if absent/corrupt/malformed.

    Malformed data is treated as "no session" so the agent self-heals on the
    next write instead of crashing on a bad field.
    """
    try:
        with CACHE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if not _valid_session(data):
        return None
    return data


def write_cache(session: dict) -> None:
    """Persist the session to the JSON cache.

    The write is atomic (temp file + rename) so concurrent readers never
    observe a half-written file.
    """
    ensure_dirs()
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(session, fh)
    tmp.replace(CACHE_FILE)


def clear_cache() -> None:
    with contextlib.suppress(FileNotFoundError):
        CACHE_FILE.unlink()


# ---------------------------------------------------------------------------
# Outbox (offline queue): newline-delimited JSON of pending pushes
# ---------------------------------------------------------------------------


def enqueue_outbox(action: str, session: dict) -> None:
    """Append a pending push. action is 'session' or 'end'."""
    ensure_dirs()
    with OUTBOX_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"action": action, "session": session}) + "\n")


def read_outbox() -> list[dict]:
    try:
        with OUTBOX_FILE.open("r", encoding="utf-8") as fh:
            items: list[dict] = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    items.append(json.loads(line))
            return items
    except FileNotFoundError:
        return []


def rewrite_outbox(items: list[dict]) -> None:
    ensure_dirs()
    tmp = OUTBOX_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item) + "\n")
    tmp.replace(OUTBOX_FILE)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:8787",
    "machine_name": socket.gethostname(),
    "poll_interval": 5,
    # Fire lifecycle hooks for sessions that STARTED on another machine?
    # false = remote sessions only update the local cache/display.
    "run_for_remote_sessions": False,
    "hooks": {
        "enabled": True,
        # Timeout (seconds) per hook script. Runaway scripts are killed.
        "timeout": 10,
        # Override the hooks directory. Empty -> HOOKS_DIR (~/.config/pomo/hooks).
        "dir": "",
    },
}


def load_config() -> dict:
    """Load agent.toml merged over defaults. Missing file -> defaults."""
    cfg = copy.deepcopy(_DEFAULT_CONFIG)
    if tomllib is not None and CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as fh:
            user = tomllib.load(fh)
        for key, val in user.items():
            if key == "hooks" and isinstance(val, dict):
                hooks = cfg.get("hooks")
                if isinstance(hooks, dict):
                    hooks.update(val)
            else:
                cfg[key] = val
    if not cfg.get("machine_name"):
        cfg["machine_name"] = socket.gethostname()
    # Env overrides (handy for launchd / testing)
    cfg["server_url"] = os.environ.get("POMO_SERVER_URL", cfg["server_url"])
    return cfg


# ---------------------------------------------------------------------------
# HTTP JSON client
# ---------------------------------------------------------------------------


class ServerUnavailable(Exception):
    """Raised when the server cannot be reached (offline)."""


def _token() -> str | None:
    tok = os.environ.get("POMO_TOKEN")
    return tok or None


def _request(
    method: str, url: str, payload: dict | None = None, timeout: float = 4.0
) -> dict | list:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    tok = _token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:  # server reachable, returned error
        raise ServerUnavailable(f"HTTP {exc.code}: {exc.reason}") from exc
    except (TimeoutError, urllib.error.URLError, OSError) as exc:
        raise ServerUnavailable(str(exc)) from exc
    if not body:
        return {}
    return json.loads(body)


def get_current(server_url: str) -> dict:
    return cast(dict, _request("GET", server_url.rstrip("/") + "/current"))


def post_session(server_url: str, session: dict) -> dict:
    return cast(dict, _request("POST", server_url.rstrip("/") + "/sessions", session))


def post_end(server_url: str, session: dict) -> dict:
    return cast(dict, _request("POST", server_url.rstrip("/") + "/sessions/end", session))


def get_sessions(
    server_url: str,
    project: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    state: str | None = None,
) -> list:
    params = {}
    if project is not None:
        params["project"] = project
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if state is not None:
        params["state"] = state
    url = server_url.rstrip("/") + "/sessions"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return cast(list, _request("GET", url))


def get_stats(
    server_url: str,
    project: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    include_archived: bool = False,
) -> dict:
    params = {}
    if project is not None:
        params["project"] = project
    if from_date is not None:
        params["from"] = from_date
    if to_date is not None:
        params["to"] = to_date
    if include_archived:
        params["include_archived"] = "1"
    url = server_url.rstrip("/") + "/stats"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return cast(dict, _request("GET", url))


def get_projects(server_url: str) -> list:
    return cast(list, _request("GET", server_url.rstrip("/") + "/projects"))
