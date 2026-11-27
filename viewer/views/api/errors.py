"""The JSON API error envelope.

Every API error is rendered as ``{"error": {"code": "<machine code>", "message": "<human text>"}}`` with a matching
HTTP status. Handlers raise :class:`ApiError` and the entry views render it for the surface the request came in on.

The legacy surfaces (``/api/``, ``/r-api/``, ``/admin-api/``) also keep the keys their clients used to read the error
from, so existing tools keep working during the deprecation window. ``/api/v1/`` only returns the envelope.
"""

import enum
import json
from typing import Any

from django.http import HttpResponse


class ApiSurface(enum.StrEnum):
    V1 = "v1"
    # /api/, /api, /jsearch/: errors used {"result": "<message>"}.
    LEGACY_API = "api"
    # /r-api/: errors used {"result": "error", "message": "<message>"}.
    LEGACY_RAPI = "r-api"
    # /admin-api/, /jsonapi/: errors used {"error": "<message>"}; "message" is what the userscript shows.
    LEGACY_REMOTE = "remote"


JSON_CONTENT_TYPE = "application/json; charset=utf-8"


class ApiError(Exception):
    """An API error with its HTTP status, a stable machine-readable code and a message for humans."""

    def __init__(self, status: int, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}

    @classmethod
    def bad_request(cls, message: str, code: str = "bad_request") -> "ApiError":
        return cls(400, code, message)

    @classmethod
    def invalid_json(cls, message: str = "Invalid JSON body") -> "ApiError":
        return cls(400, "invalid_json", message)

    @classmethod
    def unknown_command(cls, message: str = "Unknown command") -> "ApiError":
        return cls(400, "unknown_command", message)

    @classmethod
    def not_authenticated(cls, message: str = "Authentication required.") -> "ApiError":
        return cls(401, "not_authenticated", message, headers={"WWW-Authenticate": 'Bearer realm="api"'})

    @classmethod
    def invalid_token(cls, message: str = "The bearer token is invalid, expired or revoked.") -> "ApiError":
        return cls(
            401, "invalid_token", message, headers={"WWW-Authenticate": 'Bearer realm="api", error="invalid_token"'}
        )

    @classmethod
    def insufficient_scope(cls, scope: str) -> "ApiError":
        return cls(
            403,
            "insufficient_scope",
            f"The bearer token does not have the '{scope}' scope.",
            headers={"WWW-Authenticate": f'Bearer realm="api", error="insufficient_scope", scope="{scope}"'},
        )

    @classmethod
    def forbidden(cls, message: str = "Not authorized", code: str = "forbidden") -> "ApiError":
        return cls(403, code, message)

    @classmethod
    def not_found(cls, message: str, code: str = "not_found") -> "ApiError":
        return cls(404, code, message)

    @classmethod
    def method_not_allowed(cls, allowed: list[str], message: str | None = None) -> "ApiError":
        return cls(
            405,
            "method_not_allowed",
            message or f"Method not allowed. Use {', '.join(allowed)}.",
            headers={"Allow": ", ".join(allowed)},
        )

    @classmethod
    def internal(cls, message: str = "Internal server error.") -> "ApiError":
        return cls(500, "internal_error", message)

    @classmethod
    def query_timeout(
        cls, message: str = "The query took too long to execute. Please narrow your search filters."
    ) -> "ApiError":
        return cls(504, "query_timeout", message)


def error_body(error: ApiError, surface: ApiSurface = ApiSurface.V1) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": error.code, "message": error.message}}
    if surface == ApiSurface.LEGACY_API:
        body["result"] = error.message
    elif surface == ApiSurface.LEGACY_RAPI:
        body["result"] = "error"
        body["message"] = error.message
    elif surface == ApiSurface.LEGACY_REMOTE:
        body["message"] = error.message
    return body


def error_response(error: ApiError, surface: ApiSurface = ApiSurface.V1) -> HttpResponse:
    response = HttpResponse(
        json.dumps(error_body(error, surface), ensure_ascii=False), status=error.status, content_type=JSON_CONTENT_TYPE
    )
    for header, value in error.headers.items():
        response[header] = value
    return response


def json_response(data: Any, status: int = 200, sort_keys: bool = False) -> HttpResponse:
    return HttpResponse(
        json.dumps(data, sort_keys=sort_keys, ensure_ascii=False), status=status, content_type=JSON_CONTENT_TYPE
    )
