"""URL-facing JSON API views.

``/api/v1/`` is the versioned API: the command dispatcher (``?<command>=``), the remote operations API
(``/api/v1/remote/``), the OpenAPI document and its interactive docs. The legacy routes call the same code:

- ``/api/``, ``/api``, ``/jsearch/``: the command dispatcher. Kept for public clients, with no end date. Errors keep
  the ``result`` key next to the error envelope.
- ``/r-api/...``: the old reader API (see legacy.py), deprecated.
- ``/admin-api/``, ``/jsonapi/``: the remote operations API, deprecated.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from viewer.models import UserLongLivedToken
from viewer.views.api.auth import authenticate_api_request
from viewer.views.api.dispatch import dispatch_command, user_has_required_perm
from viewer.views.api.errors import ApiError, ApiSurface, error_response, json_response
from viewer.views.api.handlers.remote import handle_remote_get, handle_remote_post

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("django.request")


def run_api_view(
    request: HttpRequest,
    surface: ApiSurface,
    view: Callable[[HttpRequest], HttpResponse],
    scope: str | None = None,
) -> HttpResponse:
    """Authenticate the request, run ``view`` and render any error in the envelope of ``surface``."""
    try:
        authenticate_api_request(request, surface, scope)
        return view(request)
    except ApiError as e:
        return error_response(e, surface)
    except Exception:
        if settings.DEBUG:
            raise
        # Same logger Django uses for unhandled exceptions, so error mails and log handlers still see them.
        request_logger.exception("Internal Server Error: %s", request.path, extra={"status_code": 500})
        return error_response(ApiError.internal(), surface)


def deprecated(response: HttpResponse, successor: str) -> HttpResponse:
    """Mark a response from a deprecated route (RFC 9745 Deprecation header, RFC 8288 successor link)."""
    response["Deprecation"] = "true"
    response["Link"] = f'<{successor}>; rel="successor-version"'
    return response


@csrf_exempt
def api_login(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        if not username or not password:
            return HttpResponse(
                json.dumps({"success": False, "message": "Username or password is empty."}),
                content_type="application/json",
            )
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                data: dict[str, Any] = {"success": True}
            else:
                data = {"success": False, "message": "This account has been disabled."}
        else:
            data = {"success": False, "message": "Invalid login credentials."}

        return HttpResponse(json.dumps(data), content_type="application/json")

    return HttpResponseBadRequest()


@csrf_exempt
@require_POST
def api_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    data = {"success": True, "message": "Logged out."}
    return HttpResponse(json.dumps(data), content_type="application/json")


def _api_v1_index(request: HttpRequest) -> HttpResponse:
    return json_response(
        {
            "version": "1",
            "openapi": request.build_absolute_uri(reverse("viewer:api-v1-openapi")),
            "docs": request.build_absolute_uri(reverse("viewer:api-v1-docs")),
            "authenticated": request.api.authenticated,  # type: ignore[attr-defined]
        }
    )


@csrf_exempt
def api_v1(request: HttpRequest) -> HttpResponse:
    """The versioned command API: ``/api/v1/?<command>=<value>``."""

    def view(req: HttpRequest) -> HttpResponse:
        if req.method == "GET" and not req.GET:
            return _api_v1_index(req)
        return dispatch_command(req)

    return run_api_view(request, ApiSurface.V1, view)


# NOTE: This is used by 3rd parties (/jsearch/ in particular): keep its successful responses as they are.
# Anonymous callers only get public data; a session or a token gives access to the rest and to write commands.
@csrf_exempt
def json_api(request: HttpRequest) -> HttpResponse:
    return run_api_view(request, ApiSurface.LEGACY_API, dispatch_command)


def _remote_view(request: HttpRequest) -> HttpResponse:
    user = request.api.user  # type: ignore[attr-defined]
    if user is None:
        raise ApiError.not_authenticated()
    if not user_has_required_perm(user, "viewer.use_remote_api"):
        raise ApiError.forbidden()
    if request.method == "POST":
        return handle_remote_post(request, user)
    if request.method == "GET":
        return handle_remote_get(request)
    raise ApiError.method_not_allowed(["GET", "POST"], f"Unsupported method: {request.method}")


@csrf_exempt
def api_v1_remote(request: HttpRequest) -> HttpResponse:
    """Remote operations: needs the ``remote`` token scope and the ``viewer.use_remote_api`` permission."""
    return run_api_view(request, ApiSurface.V1, _remote_view, scope=UserLongLivedToken.SCOPE_REMOTE)


# Deprecated aliases of /api/v1/remote/ (/admin-api/ is what older userscripts and remote instances call).
@csrf_exempt
def json_parser(request: HttpRequest) -> HttpResponse:
    response = run_api_view(request, ApiSurface.LEGACY_REMOTE, _remote_view, scope=UserLongLivedToken.SCOPE_REMOTE)
    return deprecated(response, reverse("viewer:api-v1-remote"))


@require_GET
def api_v1_openapi(request: HttpRequest) -> HttpResponse:
    from viewer.views.api.openapi import render_openapi_document

    response = HttpResponse(render_openapi_document(), content_type="application/json; charset=utf-8")
    response["Access-Control-Allow-Origin"] = "*"
    return response


@require_GET
def api_v1_docs(request: HttpRequest) -> HttpResponse:
    return render(request, "viewer/api_docs.html", {"openapi_url": reverse("viewer:api-v1-openapi")})
