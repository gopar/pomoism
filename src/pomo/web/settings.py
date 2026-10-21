"""Django settings for the pomo server.

The database is the same SQLite file used by the legacy stdlib server, so an
existing installation can be adopted with ``migrate --fake-initial``.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from pomo import common

BASE_DIR = Path(__file__).resolve().parent


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _secret_key() -> str:
    """Use POMO_SECRET_KEY, or persist a generated key in the config dir."""
    configured = os.environ.get("POMO_SECRET_KEY")
    if configured:
        return configured
    path = Path(
        os.environ.get("POMO_SECRET_KEY_FILE", str(common.CONFIG_DIR / "django_secret_key"))
    )
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing
    key = secrets.token_urlsafe(50)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    path.chmod(0o600)
    return key


SECRET_KEY = _secret_key()
DEBUG = _env_flag("POMO_DEBUG")
ALLOWED_HOSTS = [
    host.strip() for host in os.environ.get("POMO_ALLOWED_HOSTS", "*").split(",") if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "pomo.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "pomo.web.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "pomo.web.wsgi.application"

_db_path = Path(os.environ.get("POMO_DB_PATH", str(common.DB_FILE)))
_db_path.parent.mkdir(parents=True, exist_ok=True)

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_db_path),
        "OPTIONS": {"timeout": 5},
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("POMO_TIME_ZONE", "UTC")
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Keep the API body limit identical to the legacy server.
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024

# The legacy API uses exact paths without trailing slashes.
APPEND_SLASH = False

_trusted_origins = os.environ.get("POMO_CSRF_TRUSTED_ORIGINS")
if _trusted_origins:
    CSRF_TRUSTED_ORIGINS = [
        origin.strip() for origin in _trusted_origins.split(",") if origin.strip()
    ]
