from __future__ import annotations

from django.urls import path

from pomo.web import views


def _routes(prefix: str = "") -> list:
    name = prefix.replace("/", "_")
    return [
        path(f"{prefix}health", views.health, name=f"{name}health"),
        path(f"{prefix}version", views.version, name=f"{name}version"),
        path(f"{prefix}current", views.current, name=f"{name}current"),
        path(f"{prefix}sessions", views.sessions, name=f"{name}sessions"),
        path(f"{prefix}sessions/end", views.end_session, name=f"{name}end_session"),
        path(
            f"{prefix}sessions/<str:session_id>/archive",
            views.archive_session,
            name=f"{name}archive_session",
        ),
        path(
            f"{prefix}sessions/<str:session_id>",
            views.session_detail,
            name=f"{name}session_detail",
        ),
        path(f"{prefix}projects", views.projects, name=f"{name}projects"),
        path(f"{prefix}stats", views.stats, name=f"{name}stats"),
    ]


urlpatterns = [
    *_routes(),
    *_routes("api/"),
    path("", views.not_found),
    path("<path:path>", views.not_found),
]
