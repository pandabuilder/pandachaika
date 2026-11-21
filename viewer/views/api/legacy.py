"""Deprecated ``/r-api/`` routes, kept for the bundled image viewer and older clients.

They call the reader commands of the versioned API and mark every response with ``Deprecation`` and a
``Link: <...>; rel="successor-version"`` header pointing to the ``/api/v1/`` equivalent.

=============================================  ===================================================
Legacy route                                   Successor
=============================================  ===================================================
``GET /r-api/archive/<id>/``                   ``GET /api/v1/?archive-reader=<id>``
``GET /r-api/archive/<id>/image_list/``        ``GET /api/v1/?archive-images=<id>&i=...``
``POST /r-api/archive/<id>/extract_archive/``  ``POST /api/v1/?extract-archive=<id>``
``POST /r-api/archive/<id>/reduce_archive/``   ``POST /api/v1/?reduce-archive=<id>`` (DELETE also accepted here)
``GET /r-api/archive/``                        ``GET /api/v1/?archives-quick``
``GET /r-api/archives/``                       ``GET /api/v1/?archives-browse``
``GET /r-api/archives_simple/``                ``GET /api/v1/?archives-simple``
``GET /r-api/me/``                             ``GET /api/v1/?me``
=============================================  ===================================================
"""

import logging

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest, HttpResponse
from django.http.request import QueryDict
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from viewer.models import UserLongLivedToken
from viewer.views.api.auth import get_api_context
from viewer.views.api.dispatch import user_has_required_perm
from viewer.views.api.errors import ApiError, ApiSurface
from viewer.views.api.handlers.reader import (
    handle_get_archive_images,
    handle_get_archive_reader,
    handle_get_archives_browse,
    handle_get_archives_quick,
    handle_get_archives_simple,
    handle_legacy_me,
    handle_post_extract_archive,
    handle_post_reduce_archive,
)
from viewer.views.api.routes import deprecated, run_api_view

logger = logging.getLogger(__name__)

STATE_MODIFYING_ACTIONS = {"extract_archive": "extract-archive", "reduce_archive": "reduce-archive"}
LIST_MODELS = {
    "archive": ("archives-quick", handle_get_archives_quick),
    "archives": ("archives-browse", handle_get_archives_browse),
    "archives_simple": ("archives-simple", handle_get_archives_simple),
}


def _with_command(data: QueryDict, key: str, value: str = "") -> QueryDict:
    command_data = data.copy()
    command_data[key] = value
    return command_data


def _successor(model: str | None, obj_id: int | None, action: str | None) -> str:
    base = reverse("viewer:api-v1")
    if model == "archive" and obj_id is not None:
        if action in STATE_MODIFYING_ACTIONS:
            return f"{base}?{STATE_MODIFYING_ACTIONS[action]}={obj_id}"
        if action == "image_list":
            return f"{base}?archive-images={obj_id}"
        return f"{base}?archive-reader={obj_id}"
    if model in LIST_MODELS:
        return f"{base}?{LIST_MODELS[model][0]}"
    if model == "me":
        return f"{base}?me"
    return base


def _state_modifying(request: HttpRequest, obj_id: int, action: str) -> HttpResponse:
    allowed_methods = ["POST", "DELETE"] if action == "reduce_archive" else ["POST"]
    user: User | AnonymousUser | None = get_api_context(request).user
    if request.method == "GET":
        logger.warning(
            "Deprecated GET request received for state-modifying action '%s' on archive %s by %s.",
            action,
            obj_id,
            user.username if user else "anonymous",
        )
        raise ApiError.method_not_allowed(
            allowed_methods, f"GET method is deprecated for '{action}'. Use {', '.join(allowed_methods)} instead."
        )
    if request.method not in allowed_methods:
        raise ApiError.method_not_allowed(
            allowed_methods,
            f"Method {request.method} not allowed for '{action}'. Use {', '.join(allowed_methods)} instead.",
        )
    if user is None:
        raise ApiError.not_authenticated()
    if not user_has_required_perm(user, "viewer.expand_archive"):
        raise ApiError.forbidden("You don't have permission for this action.")
    key = STATE_MODIFYING_ACTIONS[action]
    handler = handle_post_extract_archive if action == "extract_archive" else handle_post_reduce_archive
    return handler(request, _with_command(request.GET, key, str(obj_id)), {}, user)


@csrf_exempt
def legacy_rapi(
    request: HttpRequest, model: str | None = None, obj_id: int | None = None, action: str | None = None
) -> HttpResponse:
    def view(req: HttpRequest) -> HttpResponse:
        authenticated = get_api_context(req).authenticated
        if model == "archive" and obj_id is not None:
            if action in STATE_MODIFYING_ACTIONS:
                return _state_modifying(req, obj_id, action)
            if req.method != "GET":
                raise ApiError.method_not_allowed(["GET"], "Method not allowed. Use GET.")
            if action == "image_list":
                return handle_get_archive_images(req, _with_command(req.GET, "archive-images", str(obj_id)), authenticated)
            return handle_get_archive_reader(req, _with_command(req.GET, "archive-reader", str(obj_id)), authenticated)

        if req.method != "GET":
            raise ApiError.method_not_allowed(["GET"], "Unsupported request method")
        if model in LIST_MODELS:
            key, handler = LIST_MODELS[model]
            data = req.GET if key == "archives-browse" else _with_command(req.GET, key)
            return handler(req, data, authenticated)
        if model == "me":
            return handle_legacy_me(req)
        raise ApiError(404, "unknown_command", "Unsupported operation")

    scope = UserLongLivedToken.SCOPE_WRITE if action in STATE_MODIFYING_ACTIONS else UserLongLivedToken.SCOPE_READ
    response = run_api_view(request, ApiSurface.LEGACY_RAPI, view, scope=scope)
    return deprecated(response, _successor(model, obj_id, action))
