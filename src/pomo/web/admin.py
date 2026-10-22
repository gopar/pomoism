"""Read-only Django admin for the sync data.

Session writes must go through ``pomo.server`` so LWW, history, and the
``current`` pointer stay correct. The admin is intentionally view-only; use
the public session pages for edits.
"""

from __future__ import annotations

from django.contrib import admin

from pomo.web.models import Current, Session

admin.site.site_header = "pomo admin"
admin.site.site_title = "pomo admin"
admin.site.index_title = "Pomodoro data"


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "start_epoch",
        "state",
        "kind",
        "project",
        "name",
        "origin_machine",
        "duration",
        "updated_at",
    )
    list_filter = ("state", "kind", "origin_machine")
    search_fields = ("id", "name", "project")
    ordering = ("-start_epoch",)
    readonly_fields = (
        "id",
        "state",
        "start_epoch",
        "duration",
        "origin_machine",
        "updated_at",
        "ended_at",
        "name",
        "project",
        "kind",
    )

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Current)
class CurrentAdmin(admin.ModelAdmin):
    list_display = ("singleton", "session_id", "updated_at")
    readonly_fields = ("singleton", "session_id", "updated_at")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
