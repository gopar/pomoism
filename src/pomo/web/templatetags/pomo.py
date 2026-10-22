"""Template filters for human-readable session values."""

from __future__ import annotations

from datetime import datetime

from django import template

from pomo.web.stats import session_seconds

register = template.Library()

_STATE_LABELS = {
    "pomodoro": "Pomodoro",
    "overtime": "Overtime",
    "break": "Break",
    "break-overtime": "Break overtime",
    "ended": "Ended",
    "archived": "Archived",
    "idle": "Idle",
}


@register.filter
def duration(seconds: int | float | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


@register.filter
def clock(seconds: int | float | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


@register.filter
def date_of(epoch: int | float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d")


@register.filter
def time_of(epoch: int | float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")


@register.filter
def datetime_of(epoch: int | float | None) -> str:
    if epoch is None:
        return ""
    return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M")


@register.filter
def session_duration(session: dict) -> str:
    return duration(session_seconds(session))


@register.filter
def state_label(state: str | None) -> str:
    return _STATE_LABELS.get(state or "", state or "")


@register.filter
def kind_label(kind: str | None) -> str:
    return "Break" if kind == "break" else "Pomodoro"


@register.filter
def short_id(value: str | None) -> str:
    return (value or "")[:8]
