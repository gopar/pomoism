"""Public dashboard and session CRUD views.

These views intentionally do not use ``login_required``. Only Django's
``/admin/`` is authenticated; all session mutations still go through the
raw-SQL store so LWW/history/current-pointer invariants are preserved.
"""

from __future__ import annotations

import time
from datetime import date, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from pomo import common, server
from pomo.web import stats
from pomo.web.forms import SessionForm, initial_for_new, initial_for_session

_RANGE_DAYS = {"today": 0, "7d": 6, "30d": 29}


def _projects() -> list[str]:
    return [entry["project"] for entry in server.get_projects()]


def _parse_filters(request: HttpRequest, default_range: str = "7d") -> dict:
    range_key = request.GET.get("range", default_range)
    if range_key not in (*_RANGE_DAYS, "all", "custom"):
        range_key = default_range

    from_raw = request.GET.get("from") or None
    to_raw = request.GET.get("to") or None
    today = date.today()
    all_time = range_key == "all" and not (from_raw or to_raw)

    if from_raw or to_raw:
        try:
            from_date = (
                server._validate_date(from_raw)
                or (today - timedelta(days=_RANGE_DAYS[default_range])).isoformat()
            )
            to_date = server._validate_date(to_raw) or today.isoformat()
        except ValueError as exc:
            messages.error(request, str(exc))
            from_date = (today - timedelta(days=_RANGE_DAYS[default_range])).isoformat()
            to_date = today.isoformat()
            range_key = default_range
        else:
            if from_date > to_date:
                messages.warning(request, "Date range was reversed.")
                from_date, to_date = to_date, from_date
            range_key = "custom"
    else:
        days = _RANGE_DAYS.get(range_key, _RANGE_DAYS[default_range])
        from_date = (today - timedelta(days=days)).isoformat()
        to_date = today.isoformat()

    if all_time:
        from_date = ""
        to_date = ""
        label = "All time"
    elif range_key == "today":
        label = "Today"
    elif range_key == "30d":
        label = "Last 30 days"
    elif range_key == "custom":
        label = f"{from_date} to {to_date}"
    else:
        label = "Last 7 days"

    return {
        "range_key": range_key,
        "from_date": from_date,
        "to_date": to_date,
        "all_time": all_time,
        "project": request.GET.get("project") or None,
        "include_archived": request.GET.get("include_archived") == "1",
        "label": label,
    }


def _current_context() -> dict:
    current = server.get_current_session()
    if common.is_idle(current):
        return {"current": current, "current_active": False}
    start_epoch = int(current["start_epoch"])
    duration = int(current["duration"])
    remaining = duration - int(time.time() - start_epoch)
    effective_state = current["state"]
    if remaining <= 0:
        effective_state = {
            "pomodoro": "overtime",
            "break": "break-overtime",
        }.get(current["state"], current["state"])
    return {
        "current": current,
        "current_active": True,
        "current_state": effective_state,
        "current_remaining": abs(remaining),
        "current_overtime": remaining < 0,
    }


def dashboard(request: HttpRequest) -> HttpResponse:
    filters = _parse_filters(request, "7d")
    from_date = None if filters["all_time"] else filters["from_date"]
    to_date = None if filters["all_time"] else filters["to_date"]
    sessions = server.get_sessions(
        from_date=from_date,
        to_date=to_date,
        project=filters["project"],
        include_archived=filters["include_archived"],
        all_time=filters["all_time"],
    )
    data = stats.aggregate_sessions(
        sessions,
        from_date=from_date,
        to_date=to_date,
        all_time=filters["all_time"],
    )
    recent = sorted(sessions, key=lambda item: int(item["start_epoch"]), reverse=True)[:12]
    context = {
        "filters": filters,
        "stats": data,
        "recent": recent,
        "projects": _projects(),
        **_current_context(),
    }
    return render(request, "web/dashboard.html", context)


def session_list(request: HttpRequest) -> HttpResponse:
    filters = _parse_filters(request, "30d")
    from_date = None if filters["all_time"] else filters["from_date"]
    to_date = None if filters["all_time"] else filters["to_date"]
    state = request.GET.get("state") or None
    if state not in common.ALL_STATES:
        state = None
    if state == "archived":
        filters["include_archived"] = True
    sessions = server.get_sessions(
        from_date=from_date,
        to_date=to_date,
        project=filters["project"],
        state=state,
        include_archived=filters["include_archived"],
        all_time=filters["all_time"],
    )
    sessions.sort(key=lambda item: int(item["start_epoch"]), reverse=True)
    paginator = Paginator(sessions, 50)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "filters": filters,
        "state": state,
        "sessions": page_obj.object_list,
        "page_obj": page_obj,
        "query_without_page": params.urlencode(),
        "projects": _projects(),
        "states": common.ALL_STATES,
    }
    return render(request, "web/session_list.html", context)


def session_create(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SessionForm(request.POST)
        if form.is_valid():
            fields = form.session_fields()
            try:
                server.create_session(**fields)
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Session created.")
                return redirect("session_list")
    else:
        form = SessionForm(initial=initial_for_new())
    return render(
        request,
        "web/session_form.html",
        {"form": form, "projects": _projects(), "mode": "create"},
    )


def session_edit(request: HttpRequest, session_id: str) -> HttpResponse:
    session = server.get_session(session_id)
    if session is None:
        raise Http404("session not found")

    active = session["state"] in common.ACTIVE_STATES
    initial = initial_for_session(session)
    if request.method == "POST":
        form = SessionForm(request.POST, active=active, session=session, initial=initial)
        if form.is_valid():
            name = (form.cleaned_data.get("name") or "").strip() or None
            project = (form.cleaned_data.get("project") or "").strip() or None
            payload: dict = {"name": name, "project": project}
            if not active:
                fields = form.session_fields()
                existing_kind = session.get("kind") or (
                    "break" if session["state"] in ("break", "break-overtime") else "pomodoro"
                )
                timing_changed = (
                    abs(fields["start_epoch"] - int(session["start_epoch"])) > 60
                    or fields["duration"] != int(session["duration"])
                    or fields["kind"] != existing_kind
                )
                if timing_changed:
                    payload.update(
                        {
                            "start_epoch": fields["start_epoch"],
                            "duration": fields["duration"],
                            "kind": fields["kind"],
                            "ended_at": fields["ended_at"],
                        }
                    )
            try:
                server.edit_session(session_id, payload)
            except (LookupError, ValueError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Session updated.")
                return redirect("session_list")
    else:
        form = SessionForm(initial=initial, active=active, session=session)

    return render(
        request,
        "web/session_form.html",
        {
            "form": form,
            "projects": _projects(),
            "mode": "edit",
            "session": session,
            "active": active,
        },
    )


def session_delete(request: HttpRequest, session_id: str) -> HttpResponse:
    session = server.get_session(session_id)
    if session is None:
        raise Http404("session not found")

    if request.method == "POST":
        try:
            server.archive_session(session_id, {})
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Session archived.")
        return redirect("session_list")

    return render(
        request,
        "web/session_confirm_delete.html",
        {"session": session},
    )


@require_POST
def end_current_session(request: HttpRequest) -> HttpResponse:
    current = server.get_current_session()
    if common.is_idle(current):
        messages.info(request, "No active session to stop.")
    else:
        server.end_current(current)
        messages.success(request, "Current session stopped.")
    return redirect("dashboard")
