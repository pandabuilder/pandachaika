import json
import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from viewer.forms import (
    BootstrapPasswordChangeForm,
    DivErrorList,
    ProfileChangeForm,
    UserChangeForm,
    UserTokenCreateForm,
)
from viewer.models import Archive, EventLog, Profile, UserArchivePrefs, UserLongLivedToken
from viewer.utils.general import get_safe_referer, safe_redirect_back
from viewer.utils.task_actions import htmx_redirect, is_htmx
from viewer.utils.types import AuthenticatedHttpRequest

logger = logging.getLogger(__name__)


def render_error(request: HttpRequest, message: str) -> HttpResponse:
    messages.error(request, message)
    if is_htmx(request):
        return htmx_redirect(get_safe_referer(request, reverse("viewer:main-page")))
    return safe_redirect_back(request, reverse("viewer:main-page"))


def render_message(request: HttpRequest, message: str) -> HttpResponse:
    return render(request, "viewer/message.html", {"message": message})


def about(request: HttpRequest) -> HttpResponse:
    return render(request, "viewer/about.html")


def viewer_login(request: HttpRequest) -> HttpResponse:

    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                next_url = request.POST.get("next") or request.GET.get("next")
                if not next_url or not url_has_allowed_host_and_scheme(
                    url=next_url,
                    allowed_hosts={request.get_host()},
                    require_https=request.is_secure(),
                ):
                    next_url = reverse("viewer:main-page")
                return redirect(next_url)
            else:
                return render_error(request, "This account has been disabled.")
        else:
            return render_error(request, "Invalid login credentials.")
    else:
        next_url = request.GET.get("next", "")
        if next_url and not url_has_allowed_host_and_scheme(
            url=next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            next_url = ""
        d = {"next": next_url}
        return render(request, "viewer/accounts/login.html", d)


@login_required
@require_POST
def viewer_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return HttpResponseRedirect(reverse("viewer:main-page"))


@login_required
def change_password(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = BootstrapPasswordChangeForm(request.user, request.POST, error_class=DivErrorList)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Your password was successfully updated!")
            return redirect("viewer:change-password")
        else:
            messages.error(request, "Please correct the error below.")
    else:
        form = BootstrapPasswordChangeForm(request.user, error_class=DivErrorList)
    return render(request, "viewer/accounts/change_password.html", {"form": form})


# Session key holding a just-created token's secret until the profile page shows it, once.
NEW_TOKEN_SESSION_KEY = "new_user_token"


@login_required
def change_profile(request: AuthenticatedHttpRequest) -> HttpResponse:
    if not hasattr(request.user, "profile"):
        Profile.objects.create(user=request.user)
    if request.method == "POST":
        user_form = UserChangeForm(request.POST, instance=request.user, error_class=DivErrorList)
        profile_form = ProfileChangeForm(request.POST, instance=request.user.profile, error_class=DivErrorList)
        if all((user_form.is_valid(), profile_form.is_valid())):
            user = user_form.save()
            profile = profile_form.save(commit=False)
            profile.user = user
            profile.save()
            messages.success(request, "Your profile was successfully updated!")
            return redirect("viewer:change-profile")
        else:
            messages.error(request, "Please correct the error below.")
    else:
        user_form = UserChangeForm(instance=request.user, error_class=DivErrorList)
        profile_form = ProfileChangeForm(instance=request.user.profile, error_class=DivErrorList)

    # A token created on the previous request: its secret is shown on this page load only.
    new_token = request.session.pop(NEW_TOKEN_SESSION_KEY, None)

    tokens = list(request.user.long_lived_tokens.order_by("-create_date"))
    now = timezone.now()
    soon = now + timedelta(days=14)
    for token in tokens:
        token.expires_soon = token.expire_date is not None and now < token.expire_date <= soon  # type: ignore[attr-defined]

    own_events = EventLog.objects.filter(user=request.user)
    context = {
        "profile_form": profile_form,
        "user_form": user_form,
        "token_form": UserTokenCreateForm(user=request.user),
        "token_scopes": [
            (value, *(label.split(": ", 1) if ": " in label else (label, "")))
            for value, label in UserLongLivedToken.SCOPE_CHOICES
        ],
        "tokens": tokens,
        "token_counts": {
            "active": sum(1 for t in tokens if not t.is_expired),
            "expired": sum(1 for t in tokens if t.is_expired),
        },
        "new_token": new_token,
        "recent_events": own_events.prefetch_related("content_object", "content_type")[:6],
        "events_last_month": own_events.filter(create_date__gte=now - timedelta(days=30)).count(),
    }
    response = render(request, "viewer/accounts/user_profile.html", context)
    if new_token:
        # Keep the page with the secret out of the browser and back/forward caches.
        add_never_cache_headers(response)
    return response


def session_settings(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        data = json.loads(request.body.decode("utf-8"))
        if "viewer_parameters" in data:
            if "viewer_parameters" not in request.session:
                request.session["viewer_parameters"] = {}
            for k, v in data.items():
                if not k == "viewer_parameters":
                    request.session["viewer_parameters"][k] = v
            request.session.modified = True
        return HttpResponse(json.dumps({"result": "ok"}), content_type="application/json; charset=utf-8")
    elif request.method == "GET":
        data = json.loads(request.body.decode("utf-8"))
        if "viewer_parameters" in data:
            viewer_params = request.session.get("viewer_parameters", {"image_width": 900})
            return HttpResponse(
                json.dumps(viewer_params), content_type="application/json; charset=utf-8"
            )
        return HttpResponse(json.dumps({"result": "error"}), content_type="application/json; charset=utf-8")
    else:
        return HttpResponse(json.dumps({"result": "error"}), content_type="application/json; charset=utf-8")



@login_required
@require_POST
def user_archive_preferences(request: AuthenticatedHttpRequest, archive_pk: int, setting: str) -> HttpResponse:
    """Archive user favorite toggle."""
    try:
        Archive.objects.get(pk=archive_pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if setting == "favorite":
        current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
            user=User.objects.get(pk=request.user.id),
            archive=Archive.objects.get(pk=archive_pk),
            defaults={"favorite_group": 1},
        )
        if not created:
            current_user_archive_preferences.favorite_group = 1
            current_user_archive_preferences.save()
    elif setting == "unfavorite":
        current_user_archive_preferences, created = UserArchivePrefs.objects.get_or_create(
            user=User.objects.get(pk=request.user.id),
            archive=Archive.objects.get(pk=archive_pk),
            defaults={"favorite_group": 0},
        )
        if not created:
            current_user_archive_preferences.favorite_group = 0
            current_user_archive_preferences.save()
    else:
        return render_error(request, "Unknown user preference.")
    return HttpResponseRedirect(
        get_safe_referer(request, reverse("viewer:archive", args=[archive_pk])), preserve_request=False, content={"user_archive_preferences": current_user_archive_preferences}
    )



