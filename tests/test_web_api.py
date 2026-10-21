"""API parity tests for the Django server.

These tests exercise the JSON protocol used by the CLI and agent. The raw-SQL
store keeps its own SQLite file, so each test gets a fresh store DB even though
Django also has a test database for middleware/session support.
"""

from __future__ import annotations

import json
import time

import pytest
from django.db import connection
from django.test import Client

from pomo import common, server
from pomo.web.views import MAX_BODY_BYTES

pytestmark = pytest.mark.django_db


def _session(**overrides):
    session = common.new_session("pomodoro", int(time.time()), 60, "laptop")
    session.update(overrides)
    return session


@pytest.fixture
def api_db(isolated, monkeypatch):
    db_path = isolated / "data" / "pomo.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    server.init_db()
    return db_path


@pytest.fixture
def client():
    return Client()


def _post(client: Client, path: str, payload: dict):
    return client.post(path, data=json.dumps(payload), content_type="application/json")


def _patch(client: Client, path: str, payload: dict):
    return client.patch(path, data=json.dumps(payload), content_type="application/json")


class TestHealthAndVersion:
    def test_health(self, client, api_db):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_version(self, client, api_db):
        response = client.get("/version")
        assert response.status_code == 200
        assert response.json()["version"] == common.version()

    def test_api_aliases(self, client, api_db):
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/api/version").status_code == 200


class TestSessions:
    def test_current_is_idle_initially(self, client, api_db):
        response = client.get("/current")
        assert response.status_code == 200
        assert common.is_idle(response.json())

    def test_post_session_becomes_current(self, client, api_db):
        # Given: a new session
        session = _session(name="focus", project="website")

        # When: it is posted to the legacy endpoint
        response = _post(client, "/sessions", session)

        # Then: it is applied and becomes current
        assert response.status_code == 200
        body = response.json()
        assert body["applied"] is True
        assert body["current"]["id"] == session["id"]
        assert client.get("/current").json()["id"] == session["id"]

    def test_post_session_via_api_alias(self, client, api_db):
        session = _session()
        response = _post(client, "/api/sessions", session)
        assert response.status_code == 200
        assert response.json()["current"]["id"] == session["id"]

    def test_get_sessions_filters(self, client, api_db):
        # Given: two sessions in different projects
        first = _session(project="website")
        second = _session(project="backend")
        server.apply_session(first)
        server.apply_session(second)

        # When: filtering by project
        response = client.get("/sessions", {"project": "website"})

        # Then: only the matching session is returned
        assert response.status_code == 200
        assert [s["id"] for s in response.json()] == [first["id"]]

    def test_invalid_date_returns_400(self, client, api_db):
        response = client.get("/sessions", {"from": "not-a-date"})
        assert response.status_code == 400
        assert "invalid date" in response.json()["error"]

    def test_invalid_state_returns_400(self, client, api_db):
        session = _session()
        session["state"] = "bogus"
        response = _post(client, "/sessions", session)
        assert response.status_code == 400
        assert "invalid state" in response.json()["error"]

    def test_invalid_json_returns_400(self, client, api_db):
        response = client.post("/sessions", data="{", content_type="application/json")
        assert response.status_code == 400
        assert response.json() == {"error": "invalid json"}

    def test_oversized_body_returns_413(self, client, api_db):
        payload = json.dumps({"blob": "x" * (MAX_BODY_BYTES + 1)})
        response = client.post("/sessions", data=payload, content_type="application/json")
        assert response.status_code == 413
        assert response.json() == {"error": "request too large"}

    def test_end_session(self, client, api_db):
        # Given: an active session
        session = _session()
        server.apply_session(session)

        # When: it is ended through the API
        ended = dict(session)
        ended["ended_at"] = session["start_epoch"] + 60
        response = _post(client, "/sessions/end", ended)

        # Then: the stop wins and the current view is idle
        assert response.status_code == 200
        assert response.json()["applied"] is True
        assert common.is_idle(client.get("/current").json())

    def test_patch_edits_name_and_project(self, client, api_db):
        session = _session(name="old", project="old-project")
        server.apply_session(session)

        response = _patch(
            client,
            f"/sessions/{session['id']}",
            {"name": "new", "project": "new-project"},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "new"
        assert response.json()["project"] == "new-project"

    def test_patch_missing_session_returns_404(self, client, api_db):
        response = _patch(client, "/sessions/nope", {"name": "new"})
        assert response.status_code == 404

    def test_archive_session(self, client, api_db):
        session = _session()
        server.apply_session(session)

        response = _post(client, f"/sessions/{session['id']}/archive", {})

        assert response.status_code == 200
        assert response.json()["applied"] is True
        assert common.is_idle(client.get("/current").json())
        assert client.get("/sessions").json() == []
        archived = client.get("/sessions", {"include_archived": "1"}).json()
        assert len(archived) == 1
        assert archived[0]["state"] == "archived"

    def test_unknown_path_returns_json_404(self, client, api_db):
        response = client.get("/nope")
        assert response.status_code == 404
        assert response.json() == {"error": "not found"}


class TestStatsAndProjects:
    def test_stats_uses_ended_pomodoros(self, client, api_db):
        # Given: one completed pomodoro today
        session = _session(project="website")
        server.apply_session(session)
        ended = dict(session)
        ended["ended_at"] = session["start_epoch"] + 60
        server.end_current(ended)

        # When: requesting stats
        response = client.get("/stats")

        # Then: the completed session is counted
        assert response.status_code == 200
        body = response.json()
        assert body["session_count"] == 1
        assert body["total_seconds"] == 60
        assert body["projects"]["website"]["count"] == 1

    def test_projects(self, client, api_db):
        server.apply_session(_session(project="website"))
        server.apply_session(_session(project="backend"))

        response = client.get("/projects")

        assert response.status_code == 200
        assert response.json() == [{"project": "backend"}, {"project": "website"}]

    def test_stats_invalid_date_returns_400(self, client, api_db):
        response = client.get("/stats", {"to": "nope"})
        assert response.status_code == 400


class TestTokenAuth:
    def test_requests_rejected_without_token(self, client, api_db, monkeypatch):
        monkeypatch.setenv("POMO_TOKEN", "secret")
        response = client.get("/health")
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}

    def test_requests_rejected_with_wrong_token(self, client, api_db, monkeypatch):
        monkeypatch.setenv("POMO_TOKEN", "secret")
        response = client.get("/health", HTTP_AUTHORIZATION="Bearer nope")
        assert response.status_code == 401

    def test_requests_allowed_with_token(self, client, api_db, monkeypatch):
        monkeypatch.setenv("POMO_TOKEN", "secret")
        response = client.get("/health", HTTP_AUTHORIZATION="Bearer secret")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_post_requires_token(self, client, api_db, monkeypatch):
        monkeypatch.setenv("POMO_TOKEN", "secret")
        response = _post(client, "/sessions", _session())
        assert response.status_code == 401


class TestMigrations:
    def test_schema_and_seed_row_exist(self):
        # Given: the test database has all migrations applied
        tables = set(connection.introspection.table_names())

        # Then: the legacy schema is present alongside Django auth tables
        assert {"sessions", "session_history", "current", "django_migrations"} <= tables

        # Then: the current pointer is seeded
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM current WHERE singleton = 0")
            assert cursor.fetchone()[0] == 1

    def test_indexes_exist(self):
        expected = {
            "sessions_start_epoch_idx",
            "sessions_project_idx",
            "sessions_state_idx",
            "sessions_kind_state_idx",
        }
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA index_list('sessions')")
            names = {row[1] for row in cursor.fetchall()}
        assert expected <= names
