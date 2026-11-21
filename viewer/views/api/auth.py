"""The one authentication and scope check shared by every JSON API surface.

A session login is used as is (sessions are not limited by token scopes). Otherwise a bearer token must be active and
grant the scope the request needs: ``read`` for GET/HEAD/OPTIONS, ``write`` for other methods, ``remote`` for the
remote operations API.

A request without credentials is anonymous. A request with a bad token is anonymous on the legacy surfaces (that's
how they always behaved) and an error on ``/api/v1/``, so a client with a broken token finds out instead of silently
getting only public data.
"""

from dataclasses import dataclass

from django.contrib.auth.models import User
from django.http import HttpRequest

from viewer.utils.requests import (
    TOKEN_INVALID,
    TOKEN_MISSING_SCOPE,
    TOKEN_VALID,
    check_bearer_token,
    scope_for_method,
)
from viewer.views.api.errors import ApiError, ApiSurface


@dataclass
class ApiContext:
    """Who is calling and through which surface, attached to the request as ``request.api``."""

    surface: ApiSurface
    user: User | None
    scope: str

    @property
    def authenticated(self) -> bool:
        return self.user is not None

    @property
    def is_v1(self) -> bool:
        return self.surface == ApiSurface.V1


def authenticate_api_request(request: HttpRequest, surface: ApiSurface, scope: str | None = None) -> ApiContext:
    """Resolve the caller of an API request and attach the result as ``request.api``.

    Raises ApiError (on ``/api/v1/`` only) when a bearer token was sent but is invalid or lacks the scope.
    """
    needed_scope = scope or scope_for_method(request)
    user: User | None = None
    if request.user.is_authenticated:
        user = request.user  # type: ignore[assignment]
    else:
        state, token_user = check_bearer_token(request, needed_scope)
        if state == TOKEN_VALID:
            user = token_user
        elif surface == ApiSurface.V1 and state == TOKEN_INVALID:
            raise ApiError.invalid_token()
        elif surface == ApiSurface.V1 and state == TOKEN_MISSING_SCOPE:
            raise ApiError.insufficient_scope(needed_scope)

    context = ApiContext(surface=surface, user=user, scope=needed_scope)
    request.api = context  # type: ignore[attr-defined]
    return context


def get_api_context(request: HttpRequest) -> ApiContext:
    context = getattr(request, "api", None)
    if context is None:
        raise RuntimeError("authenticate_api_request() was not called for this request")
    return context


def require_user(request: HttpRequest) -> User:
    """The authenticated caller, or a 401 error."""
    user = get_api_context(request).user
    if user is None:
        raise ApiError.not_authenticated()
    return user
