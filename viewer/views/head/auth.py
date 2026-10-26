import json
import logging
import typing
from typing import Any

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import BadHeaderError, Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.http.request import QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.conf import settings

from viewer.forms import BootstrapPasswordChangeForm, DivErrorList, ProfileChangeForm, UserChangeForm
from viewer.models import Archive, Profile, UserArchivePrefs
from viewer.utils.actions import event_log
from viewer.utils.general import clean_up_referer
from viewer.utils.types import AuthenticatedHttpRequest

logger = logging.getLogger(__name__)


def render_error(request: HttpRequest, message: str) -> HttpResponseRedirect:
    messages.error(request, message)
    if "HTTP_REFERER" in request.META:
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    else:
        return HttpResponseRedirect(reverse("viewer:main-page"))


def render_message(request: HttpRequest, message: str) -> HttpResponse:
    return render(request, "viewer/message.html", {"message": message})


def about(request: HttpRequest) -> HttpResponse:
    return render(request, "viewer/about.html")


def viewer_login(request: HttpRequest) -> HttpResponse:

    if request.method == "POST":
        username = request.POST["username"]
        password = request.POST["password"]
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                next_url = request.POST.get("next", "viewer:main-page")
                return redirect(next_url)
            else:
                return render_error(request, "This account has been disabled.")
        else:
            return render_error(request, "Invalid login credentials.")
    else:
        next_url = request.GET.get("next", "viewer:main-page")
        d = {"next": next_url}
        return render(request, "viewer/accounts/login.html", d)


@login_required
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
    return render(request, "viewer/accounts/user_profile.html", {"profile_form": profile_form, "user_form": user_form})


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
            if "viewer_parameters" not in request.session:
                request.session["viewer_parameters"] = {}
                request.session["viewer_parameters"]["image_width"] = 900
                request.session.modified = True
            return HttpResponse(
                json.dumps(request.session["viewer_parameters"]), content_type="application/json; charset=utf-8"
            )
        return HttpResponse(json.dumps({"result": "error"}), content_type="application/json; charset=utf-8")
    else:
        return HttpResponse(json.dumps({"result": "error"}), content_type="application/json; charset=utf-8")



@login_required
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
        clean_up_referer(request.META["HTTP_REFERER"]), preserve_request=False, content={"user_archive_preferences": current_user_archive_preferences}
    )



