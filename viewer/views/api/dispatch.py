"""Route GET/POST/PUT/DELETE requests to the registered API command.

``authenticate_api_request()`` must have run first (the entry views in routes.py do that). Errors are raised as
ApiError and rendered by the entry view for its surface.
"""

import json
import logging
from collections.abc import Sequence
from typing import Any

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpRequest, HttpResponse
from django.http.request import QueryDict

from viewer.views.api.auth import get_api_context
from viewer.views.api.errors import ApiError
from viewer.views.api.registry import COMMANDS, Command

logger = logging.getLogger(__name__)

SUPPORTED_METHODS = ["GET", "POST", "PUT", "DELETE"]


def user_has_required_perm(
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


def find_command(method: str, data: QueryDict) -> Command | None:
    for command in COMMANDS.get(method, []):
        if command.key in data:
            return command
    return None


FORM_CONTENT_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


def parse_json_body(request: HttpRequest) -> dict[str, Any]:
    """The request's JSON object body ({} when empty). Form-encoded bodies are only accepted when they are empty."""
    if request.content_type in FORM_CONTENT_TYPES:
        if request.POST or request.FILES:
            raise ApiError(415, "unsupported_media_type", "Send the request body as JSON (application/json).")
        return {}
    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError.invalid_json() from None
    if not isinstance(body, dict):
        raise ApiError.invalid_json("The JSON body must be an object.")
    return body


def dispatch_command(request: HttpRequest) -> HttpResponse:
    method = request.method or "GET"
    if method not in SUPPORTED_METHODS:
        raise ApiError.method_not_allowed(SUPPORTED_METHODS, "Unsupported request method")

    context = get_api_context(request)
    data = request.GET

    if method == "GET":
        command = find_command(method, data)
        if command is None:
            raise ApiError.unknown_command()
        if command.requires_auth and not context.authenticated:
            raise ApiError.not_authenticated()
        if command.perm and not user_has_required_perm(context.user, command.perm):
            raise ApiError.forbidden()
        return command.handler(request, data, context.authenticated)

    # Write commands: who is asking and whether the body parses come before which command it is.
    if not context.authenticated:
        raise ApiError.not_authenticated()
    body = parse_json_body(request)
    command = find_command(method, data)
    if command is None:
        raise ApiError.unknown_command()
    if not user_has_required_perm(context.user, command.perm):
        raise ApiError.forbidden()
    try:
        return command.handler(request, data, body, context.user)
    except KeyError as e:
        # The archive group handlers read required body fields with body["..."].
        logger.warning("API %s ?%s: missing body field %s", method, command.key, e, exc_info=True)
        raise ApiError.bad_request(f"Missing required field: {e.args[0]}", code="missing_field") from None
