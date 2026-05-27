"""Dispatch GET/POST/PUT/DELETE requests to registered API handlers."""

import json
from typing import Any, Optional

from django.contrib.auth.models import User, AnonymousUser
from django.http import HttpResponse, HttpRequest, HttpResponseForbidden, HttpResponseNotFound

from viewer.views.api.registry import (
    DELETE_HANDLERS,
    GET_HANDLERS,
    POST_HANDLERS,
    PUT_HANDLERS,
)

def json_api_handle_get(request: HttpRequest, user_is_authenticated: bool):
    data = request.GET
    for key, handler in GET_HANDLERS:
        if key in data:
            return handler(request, data, user_is_authenticated)
    return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_post(request: HttpRequest, user: Optional[User | AnonymousUser]):
    data = request.GET
    body = json.loads(request.body)
    for key, required_perm, handler in POST_HANDLERS:
        if key in data:
            if user and user.has_perm(required_perm):
                return handler(request, data, body, user)
            else:
                return HttpResponseForbidden(
                    json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
                )
    return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_put(request: HttpRequest, user: Optional[User | AnonymousUser]):
    data = request.GET
    body = json.loads(request.body)
    for key, required_perm, handler in PUT_HANDLERS:
        if key in data:
            if user and user.has_perm(required_perm):
                return handler(request, data, body, user)
            else:
                return HttpResponseForbidden(
                    json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
                )
    return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_delete(request: HttpRequest, user: Optional[User | AnonymousUser]):
    data = request.GET
    body: dict[str, Any] = {}
    for key, required_perm, handler in DELETE_HANDLERS:
        if key in data:
            if user and user.has_perm(required_perm):
                return handler(request, data, body, user)
            else:
                return HttpResponseForbidden(
                    json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
                )
    return HttpResponseNotFound(
        json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8"
    )
