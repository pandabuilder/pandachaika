from typing import Optional

from django.contrib.auth.models import User
from django.utils.timezone import now

from viewer.models import UserLongLivedToken


def get_long_token(request) -> Optional[str]:
    header_value = request.META.get("HTTP_AUTHORIZATION")

    if not header_value:
        return None

    cleaned_header_value = header_value.replace("Bearer ", "")
    return cleaned_header_value


def authenticate_by_token(request) -> tuple[bool, Optional[User]]:
    try:

        long_token = get_long_token(request)

        if long_token is None:
            return False, None

        token = UserLongLivedToken.objects.get(
            key=UserLongLivedToken.create_salted_key_from_key(long_token),
            expire_date__gt=now()
        )
        return True, token.user
    except UserLongLivedToken.DoesNotExist:
        return False, None


def double_check_auth(request) -> tuple[bool, Optional[User]]:

    if request.user.is_authenticated:
        return True, request.user

    token_valid, token_user = authenticate_by_token(request)

    if token_valid:
        return True, token_user

    return False, None
