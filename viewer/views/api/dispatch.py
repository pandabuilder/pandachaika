"""Dispatch GET/POST/PUT/DELETE requests to registered API handlers."""

from collections.abc import Sequence
import json
from typing import Any

from django.contrib.auth.models import User, AnonymousUser
from django.http import (
    HttpResponse,
    HttpRequest,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
)

from viewer.views.api.registry import (
    DELETE_HANDLERS,
    GET_HANDLERS,
    POST_HANDLERS,
    PUT_HANDLERS,
)

def _user_has_required_perm(
    user: User | AnonymousUser | None,
    required_perm: str | Sequence[str] | None,
) -> bool:
    if not user:
        return False
    if required_perm is None:
        return True
    if isinstance(required_perm, str):
        return user.has_perm(required_perm)
    return any(user.has_perm(perm) for perm in required_perm)


def json_api_handle_get(request: HttpRequest, user_is_authenticated: bool):
    data = request.GET
    for key, handler in GET_HANDLERS:
        if key in data:
            return handler(request, data, user_is_authenticated)
    return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_post(request: HttpRequest, user: User | AnonymousUser | None):
    data = request.GET
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest(
            json.dumps({"result": "Invalid JSON body"}), content_type="application/json; charset=utf-8"
        )
    for key, required_perm, handler in POST_HANDLERS:
        if key in data:
            if _user_has_required_perm(user, required_perm):
                return handler(request, data, body, user)
            else:
                return HttpResponseForbidden(
                    json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
                )
    return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_put(request: HttpRequest, user: User | AnonymousUser | None):
    data = request.GET
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest(
            json.dumps({"result": "Invalid JSON body"}), content_type="application/json; charset=utf-8"
        )
    for key, required_perm, handler in PUT_HANDLERS:
        if key in data:
            if _user_has_required_perm(user, required_perm):
                return handler(request, data, body, user)
            else:
                return HttpResponseForbidden(
                    json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
                )
    return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_delete(request: HttpRequest, user: User | AnonymousUser | None):
    data = request.GET
    try:
        body: dict[str, Any] = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponseBadRequest(
            json.dumps({"result": "Invalid JSON body"}), content_type="application/json; charset=utf-8"
        )
    for key, required_perm, handler in DELETE_HANDLERS:
        if key in data:
            if _user_has_required_perm(user, required_perm):
                return handler(request, data, body, user)
            else:
                return HttpResponseForbidden(
                    json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
                )
    return HttpResponseNotFound(
        json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8"
    )

