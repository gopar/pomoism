"""Pure aggregation helpers for the public dashboard."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

COMPLETED_STATES = ("ended", "archived")


def session_seconds(session: dict) -> int:
    """Actual elapsed seconds, falling back to the planned duration."""
    start = int(session["start_epoch"])
    ended_at = session.get("ended_at")
    if ended_at is not None and float(ended_at) > start:
        return max(0, int(float(ended_at) - start))
    return max(0, int(session["duration"]))


def session_kind(session: dict) -> str:
    """Return ``pomodoro`` or ``break``, tolerating old rows without kind."""
    kind = session.get("kind")
    if kind in ("pomodoro", "break"):
        return kind
    state = session.get("state")
    if state in ("break", "break-overtime"):
        return "break"
    return "pomodoro"


def _day_key(session: dict) -> str:
    return datetime.fromtimestamp(int(session["start_epoch"])).date().isoformat()


def _empty_day(day: str) -> dict[str, Any]:
    return {
        "date": day,
        "focus_seconds": 0,
        "break_seconds": 0,
        "focus_count": 0,
        "break_count": 0,
        "focus_pct": 0,
        "break_pct": 0,
    }


def aggregate_sessions(
    sessions: list[dict],
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    all_time: bool = False,
) -> dict[str, Any]:
    """Aggregate completed sessions into dashboard totals and daily buckets."""
    daily: dict[str, dict[str, Any]] = {}
    projects: dict[str, dict[str, int]] = {}
    focus_seconds = 0
    focus_count = 0
    break_seconds = 0
    break_count = 0

    for session in sessions:
        if session.get("state") not in COMPLETED_STATES:
            continue
        seconds = session_seconds(session)
        kind = session_kind(session)
        day = _day_key(session)
        bucket = daily.setdefault(day, _empty_day(day))

        if kind == "break":
            break_seconds += seconds
            break_count += 1
            bucket["break_seconds"] += seconds
            bucket["break_count"] += 1
            continue

        focus_seconds += seconds
        focus_count += 1
        bucket["focus_seconds"] += seconds
        bucket["focus_count"] += 1
        project = session.get("project")
        if project:
            entry = projects.setdefault(project, {"seconds": 0, "count": 0})
            entry["seconds"] += seconds
            entry["count"] += 1

    if from_date is not None and to_date is not None and not all_time:
        current = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date)
        while current <= end:
            daily.setdefault(current.isoformat(), _empty_day(current.isoformat()))
            current += timedelta(days=1)

    series = [daily[key] for key in sorted(daily)]
    chart_max = max(
        (entry["focus_seconds"] + entry["break_seconds"] for entry in series),
        default=0,
    )
    for entry in series:
        if chart_max:
            entry["focus_pct"] = round(entry["focus_seconds"] * 100 / chart_max)
            entry["break_pct"] = round(entry["break_seconds"] * 100 / chart_max)

    top_project: dict[str, Any] | None = None
    if projects:
        name, data = max(projects.items(), key=lambda item: (item[1]["seconds"], item[0]))
        top_project = {"name": name, **data}

    return {
        "focus_seconds": focus_seconds,
        "focus_count": focus_count,
        "break_seconds": break_seconds,
        "break_count": break_count,
        "average_focus_seconds": focus_seconds // focus_count if focus_count else 0,
        "projects": dict(sorted(projects.items())),
        "top_project": top_project,
        "daily": series,
        "chart_max": chart_max,
    }
