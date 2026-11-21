
from django.contrib.auth.models import User

from viewer.models import UserLongLivedToken

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def get_long_token(request) -> str | None:
    header_value = request.META.get("HTTP_AUTHORIZATION")

    if not header_value:
        return None

    cleaned_header_value = header_value.replace("Bearer ", "")
    return cleaned_header_value


def scope_for_method(request) -> str:
    """Token scope needed for a request: read for safe methods, write for everything else."""
    if request.method in SAFE_METHODS:
        return UserLongLivedToken.SCOPE_READ
    return UserLongLivedToken.SCOPE_WRITE


TOKEN_ABSENT = "absent"
TOKEN_VALID = "valid"
TOKEN_INVALID = "invalid"
TOKEN_MISSING_SCOPE = "missing_scope"


def check_bearer_token(request, scope: str) -> tuple[str, User | None]:
    """Check the request's bearer token against `scope`.

    Returns one of TOKEN_ABSENT, TOKEN_VALID, TOKEN_INVALID (unknown, expired or revoked) or TOKEN_MISSING_SCOPE, and
    the token's user when it is valid. Usage is recorded on valid tokens.
    """
    long_token = get_long_token(request)

    if long_token is None:
        return TOKEN_ABSENT, None

    token = (
        UserLongLivedToken.active()
        .select_related("user")
        .filter(key=UserLongLivedToken.create_salted_key_from_key(long_token))
        .first()
    )

    if token is None or not token.user.is_active:
        return TOKEN_INVALID, None
    if not token.has_scope(scope):
        return TOKEN_MISSING_SCOPE, None

    token.mark_used()
    return TOKEN_VALID, token.user


def authenticate_by_token(request, scope: str) -> tuple[bool, User | None]:
    """Authenticate with a bearer token that is not expired and grants `scope`.

    A valid token without the requested scope is treated like an invalid one. Usage is recorded on the token.
    """
    state, user = check_bearer_token(request, scope)
    return state == TOKEN_VALID, user


def double_check_auth(request, scope: str | None = None) -> tuple[bool, User | None]:
    """Session authentication, falling back to a bearer token. Sessions are not limited by scopes.

    `scope` defaults to what the request method implies (read for GET, write otherwise).
    """
    if request.user.is_authenticated:
        return True, request.user

    token_valid, token_user = authenticate_by_token(request, scope or scope_for_method(request))

    if token_valid:
        return True, token_user

    return False, None
