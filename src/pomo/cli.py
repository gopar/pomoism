#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

if sys.version_info < (3, 11):
    sys.exit(f"Error: Python 3.11+ required (current: {sys.version.split()[0]})")

from pomo import agent, common, hooks, server, service


def _cfg() -> dict:
    return common.load_config()


def _require_int(value: str, name: str) -> int:
    if not value.isdigit():
        sys.stderr.write(f"Error: {name} must be an integer\n")
        sys.exit(1)
    return int(value)


def _push(action: str, session: dict) -> None:
    cfg = _cfg()
    try:
        if action == "end":
            common.post_end(cfg["server_url"], session)
        else:
            common.post_session(cfg["server_url"], session)
    except common.ServerUnavailable:
        common.enqueue_outbox(action, session)


def _current_active() -> dict | None:
    session = common.read_cache()
    if common.is_idle(session):
        return None
    return session


def _fmt_time(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def start_pomodoro(mins: int, name: str | None = None, project: str | None = None) -> None:
    cfg = _cfg()
    session = common.new_session(
        "pomodoro", int(time.time()), mins * 60, cfg["machine_name"], name=name, project=project
    )
    common.write_cache(session)
    hooks.dispatch(hooks.POMODORO_START, session, cfg)
    _push("session", session)
    print(f"Pomodoro {common.sid8(session)} started ({mins}m) 🍅")


def start_break(mins: int, name: str | None = None, project: str | None = None) -> None:
    cfg = _cfg()
    session = common.new_session(
        "break", int(time.time()), mins * 60, cfg["machine_name"], name=name, project=project
    )
    common.write_cache(session)
    hooks.dispatch(hooks.BREAK_START, session, cfg)
    _push("session", session)
    print(f"Break {common.sid8(session)} started ({mins}m) ☕")


def stop(session: dict | None) -> None:
    if session is None:
        return
    cfg = _cfg()
    end = dict(session)
    end["state"] = "ended"
    end["updated_at"] = time.time()
    end["ended_at"] = time.time()
    common.clear_cache()
    hooks.dispatch(hooks.SESSION_STOP, end, cfg)
    _push("end", end)
    sys.stderr.write(f"Session {common.sid8(end)} stopped\n")


def _confirm_overwrite() -> bool:
    if _current_active() is None:
        return True
    try:
        answer = input("Pomodoro/break already running. Overwrite? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_start(mins: int, name: str | None = None, project: str | None = None) -> None:
    if not _confirm_overwrite():
        print("Aborted.")
        return
    active = _current_active()
    if active and active["state"] in ("pomodoro", "overtime"):
        stop(active)
    start_pomodoro(mins, name=name, project=project)


def cmd_break(mins: int, name: str | None = None, project: str | None = None) -> None:
    if not _confirm_overwrite():
        print("Aborted.")
        return
    active = _current_active()
    if active and active["state"] in ("pomodoro", "overtime"):
        stop(active)
    start_break(mins, name=name, project=project)


def cmd_clear() -> None:
    active = _current_active()
    # Already in a break -> just stop it, no prompt.
    if active and active["state"] in ("break", "break-overtime"):
        stop(active)
        print("Break cleared 🧹")
        return
    while True:
        try:
            brk = input("Break minutes? (empty to skip): ").strip()
        except EOFError:
            brk = ""
        if not brk:
            stop(active)
            print("Pomodoro cleared 🧹")
            return
        if brk.isdigit():
            mins = int(brk)
            break
        print(f"Error: break minutes must be an integer, got '{brk}'", file=sys.stderr)
    inherited_name = active.get("name") if active else None
    inherited_project = active.get("project") if active else None
    stop(active)
    start_break(mins, name=inherited_name, project=inherited_project)


def cmd_status(json_output: bool = False) -> None:
    session = common.read_cache()
    if common.is_idle(session):
        if json_output:
            print(json.dumps({"state": "idle", "display": "No active session"}))
        else:
            print("No active session")
        return

    assert session is not None
    now = time.time()
    cache_state = session["state"]
    start = int(session["start_epoch"])
    duration = int(session["duration"])
    elapsed = int(now - start)
    remaining = duration - elapsed

    overtime_of = {"pomodoro": "overtime", "break": "break-overtime"}
    effective_state = overtime_of.get(cache_state, cache_state) if remaining <= 0 else cache_state

    icon = {
        "pomodoro": "🍅",
        "overtime": "⏰",
        "break": "☕",
        "break-overtime": "☕",
    }.get(effective_state, "")
    time_str = _fmt_time(-remaining) if remaining < 0 else _fmt_time(remaining)
    if remaining < 0:
        time_str = f"+{time_str}"
    name = session.get("name")
    project = session.get("project")
    parts = [icon, time_str]
    if project:
        parts.append(f"[{project}]")
    if name:
        parts.append(f"[{name}]")
    display = " ".join(parts)

    if json_output:
        print(
            json.dumps(
                {
                    "state": effective_state,
                    "start_epoch": start,
                    "duration": duration,
                    "elapsed": elapsed,
                    "remaining": remaining,
                    "display": display,
                    "name": name,
                    "project": project,
                }
            )
        )
    else:
        print(display)


def cmd_history(
    json_output: bool = False,
    project: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    state: str | None = None,
) -> None:
    cfg = _cfg()
    try:
        sessions = common.get_sessions(
            cfg["server_url"],
            project=project,
            from_date=from_date,
            to_date=to_date,
            state=state,
        )
    except common.ServerUnavailable:
        sys.stderr.write("Error: server unavailable\n")
        sys.exit(1)

    if json_output:
        print(json.dumps(sessions))
        return

    if not sessions:
        print("No sessions.")
        return

    icon_map = {
        "pomodoro": "🍅",
        "break": "☕",
    }
    for s in sessions:
        icon = icon_map.get(s.get("kind") or "", "")
        dur = _fmt_time(max(0, int((s.get("ended_at") or time.time()) - int(s["start_epoch"]))))
        label_parts = []
        p = s.get("project")
        if p:
            label_parts.append(f"[{p}]")
        name = s.get("name")
        if name:
            label_parts.append(f"[{name}]")
        label_str = " " + " ".join(label_parts) if label_parts else ""
        date_str = datetime.fromtimestamp(int(s["start_epoch"])).strftime("%Y-%m-%d")
        start_str = datetime.fromtimestamp(int(s["start_epoch"])).strftime("%H:%M")
        end_epoch = s.get("ended_at") or time.time()
        end_str = datetime.fromtimestamp(int(end_epoch)).strftime("%H:%M")
        print(f"{date_str}  {start_str} – {end_str}  {icon}  {dur}{label_str}")


def cmd_stats(
    json_output: bool = False,
    project: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    include_archived: bool = False,
) -> None:
    cfg = _cfg()
    try:
        stats = common.get_stats(
            cfg["server_url"],
            project=project,
            from_date=from_date,
            to_date=to_date,
            include_archived=include_archived,
        )
    except common.ServerUnavailable:
        sys.stderr.write("Error: server unavailable\n")
        sys.exit(1)

    if json_output:
        print(json.dumps(stats))
        return

    if stats["session_count"] == 0:
        print("No sessions.")
        return

    if from_date and to_date and from_date == to_date:
        header = from_date
    elif from_date and to_date:
        header = f"{from_date} → {to_date}"
    elif from_date:
        header = f"{from_date} → now"
    elif to_date:
        header = _local_today() + f" → {to_date}"
    else:
        header = _local_today()
    print(header)

    total_str = _fmt_duration(stats["total_seconds"])
    print(f"  Sessions:    {stats['session_count']}")
    print(f"  Focus time:  {total_str}")

    if stats["projects"]:
        print()
        print("  By project:")
        proj_items = list(stats["projects"].items())
        max_len = max(len(p) for p, _ in proj_items)
        for proj, data in proj_items:
            count = data["count"]
            label = "session" if count == 1 else "sessions"
            time_str = _fmt_duration(data["seconds"])
            print(f"  {proj.ljust(max_len)}  {count} {label}  {time_str}")


def cmd_config(json_output: bool = False, init: bool = False) -> None:
    if init:
        try:
            common.create_default_config()
        except FileExistsError:
            sys.stderr.write(f"Error: config already exists at {common.CONFIG_FILE}\n")
            sys.exit(1)
        print(f"Config written to {common.CONFIG_FILE}")
        return

    cfg = common.load_config()
    exists = common.CONFIG_FILE.exists()
    if json_output:
        print(json.dumps({"path": str(common.CONFIG_FILE), "exists": exists, "config": cfg}))
        return

    state = "found" if exists else "missing — run 'pomo config --init' to create"
    print(f"Config file: {common.CONFIG_FILE} ({state})")
    print()
    print(common.render_toml(cfg))
    if "POMO_SERVER_URL" in os.environ:
        print()
        print("# server_url overridden by POMO_SERVER_URL")


def _local_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def cmd_projects(json_output: bool = False) -> None:
    cfg = _cfg()
    try:
        projects = common.get_projects(cfg["server_url"])
    except common.ServerUnavailable:
        sys.stderr.write("Error: server unavailable\n")
        sys.exit(1)

    if json_output:
        print(json.dumps(projects))
        return

    if not projects:
        print("No projects defined.")
        return

    for p in projects:
        print(p["project"])


def _argparser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    parser = argparse.ArgumentParser(
        prog="pomo",
        description="Start, stop, and track pomodoro sessions.",
    )
    parser.add_argument("--version", action="version", version=f"pomo v{common.version()}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("start", help="Start a pomodoro for N minutes")
    p.add_argument("minutes", type=int, help="Duration in minutes")
    p.add_argument("-n", "--name", help="Optional session name")
    p.add_argument("-p", "--project", help="Optional project name")

    p = sub.add_parser("break", help="Start a break for N minutes")
    p.add_argument("minutes", type=int, help="Duration in minutes")
    p.add_argument("-n", "--name", help="Optional session name")
    p.add_argument("-p", "--project", help="Optional project name")

    sub.add_parser("clear", help="Stop current session, optionally start a break")

    p = sub.add_parser("status", help="Show current session status")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    p = sub.add_parser("history", help="Show session history")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("-p", "--project", help="Filter by project")
    p.add_argument("--from", dest="from_date", metavar="DATE", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="to_date", metavar="DATE", help="End date (YYYY-MM-DD)")
    p.add_argument("--state", choices=common.ALL_STATES, help="Filter by session state")

    p = sub.add_parser("projects", help="List all defined projects")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    p = sub.add_parser("stats", help="Show session statistics")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("-p", "--project", help="Filter by project")
    p.add_argument("--from", dest="from_date", metavar="DATE", help="Start date (YYYY-MM-DD)")
    p.add_argument("--to", dest="to_date", metavar="DATE", help="End date (YYYY-MM-DD)")
    p.add_argument("--include-archived", action="store_true", help="Include archived sessions")

    p = sub.add_parser("config", help="Show configuration")
    p.add_argument("--json", action="store_true", help="Output as JSON")
    p.add_argument("--init", action="store_true", help="Create the config file if missing")

    svc = sub.add_parser("service", help="Manage pomo processes")
    svc_subs = svc.add_subparsers(dest="service_command")

    pa = svc_subs.add_parser("agent", help="Manage the local agent")
    pa.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["install", "uninstall", "status", "logs"],
        help="Action (omit to run in foreground)",
    )

    ps = svc_subs.add_parser("server", help="Manage the sync server")
    ps.add_argument(
        "action",
        nargs="?",
        default=None,
        choices=["install", "uninstall", "status", "logs"],
        help="Action (omit to run in foreground)",
    )

    svc_subs.add_parser("list", help="List all managed services")

    return parser, svc


def main(argv: list[str] | None = None) -> None:
    parser, svc_parser = _argparser()
    args = parser.parse_args(argv)

    if args.command == "start":
        cmd_start(args.minutes, name=args.name, project=args.project)
    elif args.command == "break":
        cmd_break(args.minutes, name=args.name, project=args.project)
    elif args.command == "clear":
        cmd_clear()
    elif args.command == "status":
        cmd_status(json_output=args.json)
    elif args.command == "history":
        cmd_history(
            json_output=args.json,
            project=args.project,
            from_date=args.from_date,
            to_date=args.to_date,
            state=args.state,
        )
    elif args.command == "projects":
        cmd_projects(json_output=args.json)
    elif args.command == "stats":
        cmd_stats(
            json_output=args.json,
            project=args.project,
            from_date=args.from_date,
            to_date=args.to_date,
            include_archived=args.include_archived,
        )
    elif args.command == "config":
        cmd_config(json_output=args.json, init=args.init)
    elif args.command == "service":
        if args.service_command is None:
            svc_parser.print_help()
            return

        if args.service_command == "list":
            service.list_services()
            return

        is_server = args.service_command == "server"
        action = getattr(args, "action", None)

        if action is None:
            (server.main if is_server else agent.main)()
        elif action == "install":
            service.install(server=is_server)
        elif action == "uninstall":
            service.uninstall(server=is_server)
        elif action == "status":
            service.status(server=is_server)
        elif action == "logs":
            service.logs(server=is_server)
    else:
        parser.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
