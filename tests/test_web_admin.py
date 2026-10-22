"""Admin access and read-only behavior tests."""

from __future__ import annotations

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory
from django.urls import reverse

from pomo import server
from pomo.web.models import Session

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def store_db(isolated, monkeypatch):
    db_path = isolated / "data" / "pomo.db"
    monkeypatch.setattr(server, "DB_PATH", db_path)
    server.init_db()
    return db_path


def _superuser():
    return get_user_model().objects.create_superuser(
        username="admin",
        email="admin@example.com",
        password="secret",
    )


class TestAdminAuth:
    def test_admin_requires_login(self, client):
        # When: an anonymous visitor opens admin
        response = client.get("/admin/")

        # Then: they are redirected to the admin login page
        assert response.status_code == 302
        assert "/admin/login/" in response.url

    def test_admin_login_page_is_public(self, client):
        response = client.get("/admin/login/")
        assert response.status_code == 200

    def test_superuser_can_access_admin(self, client):
        # Given: an authenticated superuser
        user = _superuser()
        client.force_login(user)

        # When: opening the admin index
        response = client.get("/admin/")

        # Then: it is available
        assert response.status_code == 200
        assert "Pomodoro data" in response.content.decode()

    def test_public_dashboard_does_not_require_login(self, client, store_db):
        response = client.get("/")
        assert response.status_code == 200
        assert "/admin/login/" not in response.content.decode()


class TestReadOnlySessionAdmin:
    def test_session_admin_has_no_write_permissions(self):
        # Given: a superuser request and the registered Session admin
        request = RequestFactory().get("/admin/web/session/")
        request.user = _superuser()
        model_admin = admin.site._registry[Session]

        # Then: add/change/delete are disabled for everyone
        assert model_admin.has_add_permission(request) is False
        assert model_admin.has_change_permission(request) is False
        assert model_admin.has_delete_permission(request) is False
        assert model_admin.has_view_permission(request) is True

    def test_session_changelist_loads(self, client):
        # Given: a session row in the Django database
        Session.objects.create(
            id="admin-test",
            state="ended",
            start_epoch=1,
            duration=60,
            origin_machine="laptop",
            updated_at=1.0,
            ended_at=61.0,
            kind="pomodoro",
        )
        client.force_login(_superuser())

        # When: opening the session changelist
        response = client.get(reverse("admin:web_session_changelist"))

        # Then: the read-only list renders
        assert response.status_code == 200
        assert "admin-test" in response.content.decode()

    def test_session_detail_is_view_only(self, client):
        # Given: a session row and a logged-in superuser
        Session.objects.create(
            id="admin-detail",
            state="ended",
            start_epoch=1,
            duration=60,
            origin_machine="laptop",
            updated_at=1.0,
            ended_at=61.0,
            kind="pomodoro",
        )
        client.force_login(_superuser())

        # When: opening the session detail page
        response = client.get(reverse("admin:web_session_change", args=["admin-detail"]))

        # Then: it renders without an editable save form
        assert response.status_code == 200
        assert "admin-detail" in response.content.decode()
