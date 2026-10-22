"""Behavior tests for the public dashboard and session CRUD pages."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pytest
from django.test import Client

from pomo import common, server

pytestmark = pytest.mark.django_db


@pytest.fixture
def store_db(isolated, monkeypatch):
    db_path = isolated / "data" / "pomo.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    server.init_db()
    return db_path


@pytest.fixture
def client():
    return Client()


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _create_completed(project: str = "website", name: str = "build") -> dict:
    return server.create_session(
        kind="pomodoro",
        start_epoch=int(time.time()) - 3600,
        duration=1500,
        project=project,
        name=name,
    )


def _form_data(
    *,
    kind: str = "pomodoro",
    day: str | None = None,
    start_time: str = "09:00",
    duration: int = 25,
    name: str = "",
    project: str = "",
) -> dict:
    return {
        "kind": kind,
        "date": day or _yesterday(),
        "start_time": start_time,
        "duration_minutes": str(duration),
        "name": name,
        "project": project,
    }


def _edit_data(session: dict, **overrides) -> dict:
    start = datetime.fromtimestamp(int(session["start_epoch"]))
    data = _form_data(
        kind=session.get("kind") or "pomodoro",
        day=start.date().isoformat(),
        start_time=start.strftime("%H:%M"),
        duration=max(1, int(session["duration"]) // 60),
        name=session.get("name") or "",
        project=session.get("project") or "",
    )
    data.update(overrides)
    return data


class TestDashboard:
    def test_dashboard_is_public(self, client, store_db):
        # When: an anonymous visitor opens the dashboard
        response = client.get("/")

        # Then: no login redirect is required
        assert response.status_code == 200
        assert "Public dashboard" in response.content.decode()

    def test_dashboard_shows_completed_stats(self, client, store_db):
        # Given: a completed focus session today
        _create_completed(project="website", name="build")

        # When: opening the dashboard
        response = client.get("/")

        # Then: the stats and project are rendered
        content = response.content.decode()
        assert response.status_code == 200
        assert "Focus time" in content
        assert "website" in content
        assert "25m" in content

    def test_dashboard_shows_active_session(self, client, store_db):
        # Given: an active pomodoro
        active = common.new_session("pomodoro", int(time.time()), 1500, "laptop")
        server.apply_session(active)

        # When: opening the dashboard
        response = client.get("/")

        # Then: the current banner and stop control are visible
        content = response.content.decode()
        assert "Stop session" in content
        assert "Pomodoro" in content

    def test_dashboard_includes_archived(self, client, store_db):
        session = _create_completed(project="hidden", name="archived-marker")
        server.archive_session(session["id"], {})

        without_archived = client.get("/").content.decode()
        with_archived = client.get("/", {"include_archived": "1"}).content.decode()

        assert "archived-marker" not in without_archived
        assert "archived-marker" in with_archived

    def test_dashboard_all_time_includes_old_sessions(self, client, store_db):
        # Given: a completed session older than the default range
        server.create_session(
            kind="pomodoro",
            start_epoch=int(time.time()) - 10 * 86400,
            duration=1500,
            name="old-marker",
        )

        # Then: the default range excludes it and all-time includes it
        assert "old-marker" not in client.get("/").content.decode()
        assert "old-marker" in client.get("/", {"range": "all"}).content.decode()


class TestCreateSession:
    def test_create_historical_session(self, client, store_db):
        # When: a completed session is submitted
        response = client.post(
            "/sessions/new/",
            _form_data(name="write tests", project="pomo"),
        )

        # Then: it is stored and redirects to the list
        assert response.status_code == 302
        sessions = server.get_sessions(from_date=_yesterday(), to_date=_yesterday())
        assert len(sessions) == 1
        assert sessions[0]["name"] == "write tests"
        assert sessions[0]["project"] == "pomo"
        assert sessions[0]["state"] == "ended"

        # Then: it did not become the current session
        assert common.is_idle(server.get_current_session())

    def test_create_rejects_future_start(self, client, store_db):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        response = client.post("/sessions/new/", _form_data(day=tomorrow))
        assert response.status_code == 200
        assert "cannot be in the future" in response.content.decode()
        assert server.get_sessions(all_time=True) == []

    def test_csrf_is_required(self, store_db):
        csrf_client = Client(enforce_csrf_checks=True)
        form_page = csrf_client.get("/sessions/new/")
        assert form_page.status_code == 200
        token = csrf_client.cookies["csrftoken"].value

        rejected = csrf_client.post("/sessions/new/", _form_data())
        assert rejected.status_code == 403

        accepted = csrf_client.post(
            "/sessions/new/",
            {**_form_data(), "csrfmiddlewaretoken": token},
        )
        assert accepted.status_code == 302


class TestEditSession:
    def test_edit_name_and_project_preserves_timing(self, client, store_db):
        session = _create_completed(project="old", name="old name")

        response = client.post(
            f"/sessions/{session['id']}/edit/",
            _edit_data(session, name="new name", project="new"),
        )

        assert response.status_code == 302
        updated = server.get_session(session["id"])
        assert updated is not None
        assert updated["name"] == "new name"
        assert updated["project"] == "new"
        assert updated["start_epoch"] == session["start_epoch"]
        assert updated["ended_at"] == session["ended_at"]

    def test_edit_timing_recomputes_end(self, client, store_db):
        session = _create_completed()
        new_day = (date.today() - timedelta(days=2)).isoformat()

        response = client.post(
            f"/sessions/{session['id']}/edit/",
            _form_data(day=new_day, start_time="08:00", duration=30),
        )

        assert response.status_code == 302
        updated = server.get_session(session["id"])
        assert updated is not None
        assert updated["duration"] == 30 * 60
        assert updated["ended_at"] == updated["start_epoch"] + 30 * 60

    def test_active_session_only_updates_metadata(self, client, store_db):
        active = common.new_session("pomodoro", int(time.time()), 1500, "laptop")
        server.apply_session(active)

        response = client.post(
            f"/sessions/{active['id']}/edit/",
            _edit_data(active, duration=120, name="renamed", project="work"),
        )

        assert response.status_code == 302
        updated = server.get_session(active["id"])
        assert updated is not None
        assert updated["name"] == "renamed"
        assert updated["project"] == "work"
        assert updated["duration"] == 1500

    def test_missing_session_returns_404(self, client, store_db):
        assert client.get("/sessions/nope/edit/").status_code == 404


class TestArchiveSession:
    def test_archive_confirm_and_post(self, client, store_db):
        session = _create_completed()

        confirm = client.get(f"/sessions/{session['id']}/delete/")
        assert confirm.status_code == 200
        assert "Archive this session" in confirm.content.decode()

        response = client.post(f"/sessions/{session['id']}/delete/")

        assert response.status_code == 302
        archived = server.get_session(session["id"])
        assert archived is not None
        assert archived["state"] == "archived"

    def test_archiving_current_session_clears_current(self, client, store_db):
        active = common.new_session("pomodoro", int(time.time()), 1500, "laptop")
        server.apply_session(active)

        response = client.post(f"/sessions/{active['id']}/delete/")

        assert response.status_code == 302
        assert common.is_idle(server.get_current_session())


class TestStopCurrent:
    def test_stop_current_session(self, client, store_db):
        active = common.new_session("pomodoro", int(time.time()), 1500, "laptop")
        server.apply_session(active)

        response = client.post("/sessions/current/end/")

        assert response.status_code == 302
        assert common.is_idle(server.get_current_session())

    def test_stop_when_idle_is_harmless(self, client, store_db):
        response = client.post("/sessions/current/end/")
        assert response.status_code == 302
        assert common.is_idle(server.get_current_session())
