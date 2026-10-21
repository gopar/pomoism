"""JSON API views backed by the existing raw-SQL store.

The CLI and agent speak the legacy paths (``/sessions``, ``/current``, ...).
Aliases under ``/api/`` are provided for new clients. Keeping the store layer
raw-SQL preserves the last-write-wins transaction semantics tested in
``tests/test_server.py``.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from functools import wraps
from typing import Any

from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from pomo import server

MAX_BODY_BYTES = 64 * 1024


class InvalidJSON(Exception):
    """Raised when a request body is not a JSON object."""


class RequestTooLarge(Exception):
    """Raised when a request body exceeds MAX_BODY_BYTES."""


class BadRequest(Exception):
    """Raised for invalid query parameters."""


def _error(message: str, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _read_json(request: HttpRequest) -> dict[str, Any]:
    raw_length = request.META.get("CONTENT_LENGTH") or "0"
    try:
        length = int(raw_length)
    except ValueError:
        length = 0
    if length > MAX_BODY_BYTES:
        raise RequestTooLarge(f"{length} > {MAX_BODY_BYTES}")
    if length <= 0:
        return {}
    try:
        payload = json.loads(request.body or b"{}")
    except RequestDataTooBig as exc:
        raise RequestTooLarge(str(exc)) from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidJSON("invalid json") from exc
    if not isinstance(payload, dict):
        raise InvalidJSON("invalid json")
    return payload


def _token_required(view: Callable[..., JsonResponse]) -> Callable[..., JsonResponse]:
    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs) -> JsonResponse:
        token = os.environ.get("POMO_TOKEN")
        if token:
            supplied = request.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            if not secrets.compare_digest(supplied.encode(), expected.encode()):
                return _error("unauthorized", 401)
        return view(request, *args, **kwargs)

    return wrapped


def _dates(request: HttpRequest) -> tuple[str | None, str | None]:
    try:
        return (
            server._validate_date(request.GET.get("from")),
            server._validate_date(request.GET.get("to")),
        )
    except ValueError as exc:
        raise BadRequest(str(exc)) from exc


def _read_payload(request: HttpRequest) -> dict[str, Any] | JsonResponse:
    try:
        return _read_json(request)
    except RequestTooLarge:
        return _error("request too large", 413)
    except InvalidJSON:
        return _error("invalid json", 400)


@csrf_exempt
@_token_required
@require_http_methods(["GET"])
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"ok": True})


@csrf_exempt
@_token_required
@require_http_methods(["GET"])
def version(request: HttpRequest) -> JsonResponse:
    from pomo import common

    return JsonResponse({"version": common.version()})


@csrf_exempt
@_token_required
@require_http_methods(["GET"])
def current(request: HttpRequest) -> JsonResponse:
    return JsonResponse(server.get_current_session())


@csrf_exempt
@_token_required
@require_http_methods(["GET", "POST"])
def sessions(request: HttpRequest) -> JsonResponse:
    if request.method == "POST":
        payload = _read_payload(request)
        if isinstance(payload, JsonResponse):
            return payload
        try:
            applied, current_session = server.apply_session(payload)
        except ValueError as exc:
            return _error(str(exc), 400)
        return JsonResponse({"applied": applied, "current": current_session})

    try:
        from_date, to_date = _dates(request)
    except BadRequest as exc:
        return _error(str(exc), 400)
    return JsonResponse(
        server.get_sessions(
            from_date=from_date,
            to_date=to_date,
            project=request.GET.get("project"),
            state=request.GET.get("state"),
            include_archived=request.GET.get("include_archived", "0") == "1",
        ),
        safe=False,
    )


@csrf_exempt
@_token_required
@require_http_methods(["POST"])
def end_session(request: HttpRequest) -> JsonResponse:
    payload = _read_payload(request)
    if isinstance(payload, JsonResponse):
        return payload
    try:
        applied, current_session = server.end_current(payload)
    except ValueError as exc:
        return _error(str(exc), 400)
    return JsonResponse({"applied": applied, "current": current_session})


@csrf_exempt
@_token_required
@require_http_methods(["PATCH"])
def session_detail(request: HttpRequest, session_id: str) -> JsonResponse:
    payload = _read_payload(request)
    if isinstance(payload, JsonResponse):
        return payload
    try:
        session = server.edit_session(session_id, payload)
    except LookupError as exc:
        return _error(str(exc), 404)
    except ValueError as exc:
        return _error(str(exc), 400)
    return JsonResponse(session)


@csrf_exempt
@_token_required
@require_http_methods(["POST"])
def archive_session(request: HttpRequest, session_id: str) -> JsonResponse:
    payload = _read_payload(request)
    if isinstance(payload, JsonResponse):
        return payload
    try:
        applied, current_session = server.archive_session(session_id, payload)
    except ValueError as exc:
        return _error(str(exc), 400)
    return JsonResponse({"applied": applied, "current": current_session})


@csrf_exempt
@_token_required
@require_http_methods(["GET"])
def projects(request: HttpRequest) -> JsonResponse:
    return JsonResponse(server.get_projects(), safe=False)


@csrf_exempt
@_token_required
@require_http_methods(["GET"])
def stats(request: HttpRequest) -> JsonResponse:
    try:
        from_date, to_date = _dates(request)
    except BadRequest as exc:
        return _error(str(exc), 400)
    return JsonResponse(
        server.get_stats(
            from_date=from_date,
            to_date=to_date,
            project=request.GET.get("project"),
            include_archived=request.GET.get("include_archived", "0") == "1",
        )
    )


def not_found(request: HttpRequest, *args, **kwargs) -> JsonResponse:
    return _error("not found", 404)
