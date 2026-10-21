"""``pomo-server`` entry point.

With arguments, dispatch to Django management commands (``migrate``,
``createsuperuser``, ``runserver``, ...). Without arguments, migrate if needed
and serve the WSGI app with waitress.
"""

from __future__ import annotations

import contextlib
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pomo.web.settings")


def _serve() -> None:
    import django
    from django.conf import settings
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from waitress import serve

    from pomo import common

    django.setup()
    if os.environ.get("POMO_AUTO_MIGRATE", "1") != "0":
        call_command("migrate", fake_initial=True, interactive=False, verbosity=1)

    host = os.environ.get("POMO_HOST", "0.0.0.0")
    port = int(os.environ.get("POMO_PORT", "8787"))
    db_path = settings.DATABASES["default"]["NAME"]
    sys.stderr.write(f"pomo-server v{common.version()} listening on {host}:{port} (db={db_path})\n")
    if os.environ.get("POMO_TOKEN"):
        sys.stderr.write("auth: bearer token REQUIRED\n")
    else:
        sys.stderr.write("auth: NONE (network-level only)\n")

    application = get_wsgi_application()
    with contextlib.suppress(KeyboardInterrupt):
        serve(application, host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        _serve()
        return
    if args in (["--version"], ["-V"]):
        from pomo import common

        print(f"pomo-server v{common.version()}")
        return

    from django.core.management import execute_from_command_line

    execute_from_command_line(["pomo-server", *args])


if __name__ == "__main__":
    main()
