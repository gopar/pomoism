from __future__ import annotations

from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _set_sqlite_pragmas(sender, connection, **kwargs) -> None:
    """Match the store's WAL/busy-timeout settings on Django connections."""
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")


class WebConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "pomo.web"

    def ready(self) -> None:
        connection_created.connect(_set_sqlite_pragmas, dispatch_uid="pomo.web.sqlite_pragmas")
