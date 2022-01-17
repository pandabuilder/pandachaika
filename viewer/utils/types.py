from django.http import HttpRequest
from django.contrib.auth.models import User


class AuthenticatedHttpRequest(HttpRequest):
    user: User
