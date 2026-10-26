import json
import logging
import re
from collections import defaultdict
from itertools import groupby
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.contrib.contenttypes.models import ContentType
from django.core import management
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Case, When, QuerySet, Q, F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import get_random_string

from core.base.setup import Settings
from core.base.utilities import thread_exists_in_db, clamp, get_schedulers_status
from viewer.management.commands import update_provider_wanted
from viewer.utils.functions import (
    archive_manage_results_to_json,
    galleries_update_metadata,
    gallery_search_results_to_json,
)
from viewer.utils.general import clean_up_referer
from viewer.utils.actions import event_log
from workers import client
from viewer.forms import (
    GallerySearchForm,
    ArchiveSearchForm,
    WantedGalleryCreateOrEditForm,
    ArchiveCreateForm,
    ArchiveGroupSelectForm,
    GalleryCreateForm,
    ArchiveManageEntrySimpleForm,
    WantedGalleryColSearchForm,
    ArchiveManageSearchSimpleForm,
    AllGalleriesSearchForm,
    EventSearchForm,
    GallerySearchSimpleForm,
    ArchiveExtraFileSimpleForm,
    WantedGalleryCommand,
    GallerySearchFields,
    DownloadHistoryForm,
    MonitoredLinkCreateOrEditForm,
    MonitoredLinkSearchForm,
)
from viewer.models import (
    Archive,
    Gallery,
    EventLog,
    ArchiveMatches,
    Tag,
    WantedGallery,
    ArchiveGroup,
    ArchiveGroupEntry,
    GallerySubmitEntry,
    MonitoredLink,
    ArchiveRecycleEntry,
    ArchiveTag,
    UserLongLivedToken,
    DownloadEvent,
    Category,
    Provider,
    GalleryMatchGroup,
    GalleryMatchGroupEntry,
    GalleryGroupPossibleMatches, ArchiveManageEntry, FoundGallery
)
from viewer.utils.requests import double_check_auth
from viewer.utils.types import AuthenticatedHttpRequest
from viewer.views.head import (
    gallery_filter_keys,
    filter_galleries_simple,
    archive_filter_keys,
    filter_archives_simple,
    render_error,
    wanted_gallery_filter_keys,
    filter_wanted_galleries_simple,
    gallery_list_filters_keys,
)

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)



@permission_required("viewer.add_wantedgallery")
def create_wanted_gallery(request: HttpRequest) -> HttpResponse:
    if request.POST.get("submit-wanted-gallery"):
        # create a form instance and populate it with data from the request:
        edit_form = WantedGalleryCreateOrEditForm(request.POST)
        # check whether it's valid:
        if edit_form.is_valid():
            new_wanted_gallery = edit_form.save(commit=False)
            new_wanted_gallery.save()
            edit_form.save_m2m()
            message = "WantedGallery successfully created: {}"
            messages.success(request, message.format(request.build_absolute_uri(new_wanted_gallery.get_absolute_url())))
            logger.info(
                "User {}: {}".format(request.user.username, message.format(new_wanted_gallery.get_absolute_url()))
            )
            event_log(request.user, "CREATE_WANTED_GALLERY", content_object=new_wanted_gallery, result="added")
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")
    else:
        if "wanted-gallery" in request.GET:
            try:
                wanted_gallery_id = int(request.GET["wanted-gallery"])
                try:
                    wg_instance: WantedGallery | None = WantedGallery.objects.get(pk=wanted_gallery_id)
                except Gallery.DoesNotExist:
                    wg_instance = None
            except ValueError:
                wg_instance = None
        else:
            wg_instance = None

        if wg_instance:
            edit_form = WantedGalleryCreateOrEditForm(instance=wg_instance)
        else:
            edit_form = WantedGalleryCreateOrEditForm()

    d = {"edit_form": edit_form}
    return render(request, "viewer/collaborators/add_wanted_gallery.html", d)


@permission_required("viewer.add_wantedgallery")
def create_wanted_galleries_from_command(request: HttpRequest) -> HttpResponse:
    if request.POST.get("submit-wanted-artist-gallery"):
        wanted_publisher = request.POST.get("wanted_publisher")
        artist_or_group_tag = request.POST.getlist("artist_or_group_tag")
        wanted_providers_pk = request.POST.getlist("wanted_providers")
        categories_pk = request.POST.getlist("categories")

        artist_or_group_tag = [x.strip() for x in artist_or_group_tag]

        wanted_providers = list(Provider.objects.filter(pk__in=wanted_providers_pk).values_list('slug', flat=True))
        categories = list(Category.objects.filter(pk__in=categories_pk).values_list('name', flat=True))

        command = "wanted_publisher: {}, artist_or_group_tag: {}, wanted_providers: {}, categories: {}".format(wanted_publisher, artist_or_group_tag, wanted_providers, categories)
        management.call_command(update_provider_wanted.Command(), wanted_publisher, wanted_providers=wanted_providers, categories=categories, artist_list=artist_or_group_tag, should_search=True)
        message = "WantedGallery command successfully run: {}".format(command)
        messages.success(request, message)
        logger.info("User {}: {}".format(request.user.username, message))
        event_log(request.user, "CREATE_WANTED_GALLERY_FROM_COMMAND", data=command, result="run")
        wanted_gallery_command_form = WantedGalleryCommand()
    else:

        categories_objects = []
        for category in crawler_settings.default_wanted_categories:
            category_obj, _ = Category.objects.get_or_create(name=category)
            categories_objects.append(category_obj)

        providers = Provider.objects.filter(slug__in=crawler_settings.default_wanted_providers)

        default_fields = {
            'categories': categories_objects,
            'wanted_providers': providers,
            'wanted_publisher': crawler_settings.default_wanted_publisher,
        }

        wanted_gallery_command_form = WantedGalleryCommand(initial=default_fields)

    d = {"wanted_gallery_command_form": wanted_gallery_command_form}
    return render(request, "viewer/collaborators/add_wanted_galleries_from_command.html", d)


def _can_manage_monitored_links(user: Any) -> bool:
    return bool(
        user.is_authenticated
        and (
            user.is_staff
            or user.has_perm("viewer.change_monitoredlink")
            or user.has_perm("viewer.view_monitored_links")
        )
    )


@permission_required("viewer.view_monitored_links")
def monitored_links(request: HttpRequest) -> HttpResponse:
    get = request.GET
    post = request.POST

    # Handle quick inline actions (Start / Stop / Force Run / Delete)
    action = post.get("action") or get.get("tool") or get.get("action")
    link_id_val = post.get("link_id") or get.get("id") or get.get("tool-id")

    if action and link_id_val:
        try:
            link_id = int(link_id_val)
            link = MonitoredLink.objects.filter(pk=link_id).first()
            if link:
                if action == "start":
                    link.start_running()
                    messages.success(request, f"Requested worker to start monitored link: '{link.name}'")
                    event_log(request.user, "START_MONITORED_LINK", content_object=link, result="started")
                elif action == "stop":
                    link.stop_running()
                    messages.success(request, f"Requested worker to stop monitored link: '{link.name}'")
                    event_log(request.user, "STOP_MONITORED_LINK", content_object=link, result="stopped")
                elif action == "force_run":
                    link.force_run()
                    messages.success(request, f"Requested worker to force-run monitored link: '{link.name}'")
                    event_log(request.user, "FORCE_RUN_MONITORED_LINK", content_object=link, result="force_run")
                elif action == "delete":
                    link_name = link.name
                    link.delete()
                    messages.success(request, f"Monitored link '{link_name}' successfully deleted")
                    event_log(request.user, "DELETE_MONITORED_LINK", result=f"deleted {link_name}")
                return HttpResponseRedirect(clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:monitored-links"))))
        except (ValueError, TypeError):
            pass

    # Handle inline creation of a MonitoredLink
    if post.get("submit-monitored-link"):
        edit_form = MonitoredLinkCreateOrEditForm(post)
        if edit_form.is_valid():
            new_link = edit_form.save(commit=False)
            if request.user.is_authenticated:
                new_link.user = request.user
            new_link.save()
            edit_form.save_m2m()
            messages.success(request, f"Monitored link '{new_link.name}' successfully created")
            event_log(request.user, "ADD_MONITORED_LINK", content_object=new_link, result="created")
            return HttpResponseRedirect(reverse("viewer:monitored-links"))
        else:
            messages.error(request, "Failed to create monitored link. Please correct the errors below.", extra_tags="danger")
    else:
        edit_form = MonitoredLinkCreateOrEditForm()

    filter_form = MonitoredLinkSearchForm(get)

    results = MonitoredLink.objects.select_related("provider", "user").prefetch_related("limited_wanted_galleries").all()

    # Apply filters
    name_query = get.get("name", "").strip()
    if name_query:
        results = results.filter(name__icontains=name_query)

    url_query = get.get("url", "").strip()
    if url_query:
        results = results.filter(url__icontains=url_query)

    provider_id = get.get("provider", "").strip()
    if provider_id:
        results = results.filter(provider_id=provider_id)

    enabled_filter = get.get("enabled", "").strip()
    if enabled_filter == "yes":
        results = results.filter(enabled=True)
    elif enabled_filter == "no":
        results = results.filter(enabled=False)

    auto_start_filter = get.get("auto_start", "").strip()
    if auto_start_filter == "yes":
        results = results.filter(auto_start=True)
    elif auto_start_filter == "no":
        results = results.filter(auto_start=False)

    # Activity status filter (running / stopped) based on SchedulerState in DB
    from datetime import timedelta
    from workers.models import SchedulerState, TaskQueue

    # Ensure all monitored links have corresponding SchedulerState entries
    all_links = list(MonitoredLink.objects.all())
    existing_states = {s.name: s for s in SchedulerState.objects.filter(name__startswith="link_monitor_")}

    for ml in all_links:
        sched_name = f"link_monitor_{ml.pk}"
        state = existing_states.get(sched_name)
        if state is None:
            state, _ = SchedulerState.objects.get_or_create(
                name=sched_name,
                defaults={
                    "display_name": f"MonitoredLink: {ml.name}",
                    "schedule_type": "scheduler",
                    "cycle_timer": ml.frequency.total_seconds(),
                    "is_running": False,
                },
            )
            existing_states[sched_name] = state

        # If last_run is None, check if TaskQueue has past crawler tasks for this URL
        if state.last_run is None:
            past_task = (
                TaskQueue.objects.filter(
                    task_type="web_crawler",
                    args__contains=[ml.url],
                )
                .order_by("-id")
                .first()
            )
            if past_task:
                task_time = past_task.finished_at or past_task.started_at or past_task.created_at
                if task_time:
                    state.last_run = task_time
                    timer = state.cycle_timer or ml.frequency.total_seconds()
                    state.next_run = task_time + timedelta(seconds=timer)
                    SchedulerState.objects.filter(name=sched_name).update(
                        last_run=state.last_run,
                        next_run=state.next_run,
                    )

    states = list(existing_states.values())
    schedulers_list = get_schedulers_status(states)
    schedulers = {}
    running_pks = set()
    for s_info in schedulers_list:
        try:
            link_pk = int(s_info[0].replace("link_monitor_", ""))
            schedulers[link_pk] = s_info
            if s_info[1]:  # is_running
                running_pks.add(link_pk)
        except ValueError:
            pass

    status_filter = get.get("status", "").strip()
    if status_filter == "running":
        results = results.filter(pk__in=running_pks)
    elif status_filter == "stopped":
        results = results.exclude(pk__in=running_pks)

    # Sorting
    sort_field = get.get("sort", "create_date")
    if sort_field not in ("create_date", "name", "last_modified", "frequency"):
        sort_field = "create_date"
    asc_desc = get.get("asc_desc", "desc")
    order_prefix = "" if asc_desc == "asc" else "-"
    results = results.order_by(f"{order_prefix}{sort_field}")

    # Pagination
    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        "results": results_page,
        "title": "Monitored Links",
        "schedulers": schedulers,
        "edit_form": edit_form,
        "filter_form": filter_form,
        "can_manage": _can_manage_monitored_links(request.user),
    }
    return render(request, "viewer/collaborators/monitored_links.html", d)


@permission_required("viewer.view_monitored_links")
def create_monitored_link(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        edit_form = MonitoredLinkCreateOrEditForm(request.POST)
        if edit_form.is_valid():
            new_link = edit_form.save(commit=False)
            if request.user.is_authenticated:
                new_link.user = request.user
            new_link.save()
            edit_form.save_m2m()
            messages.success(request, f"Monitored link '{new_link.name}' successfully created")
            event_log(request.user, "ADD_MONITORED_LINK", content_object=new_link, result="created")
            return HttpResponseRedirect(reverse("viewer:monitored-links"))
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")
    else:
        edit_form = MonitoredLinkCreateOrEditForm()

    d = {
        "edit_form": edit_form,
        "title": "Create Monitored Link",
        "is_create": True,
    }
    return render(request, "viewer/collaborators/edit_monitored_link.html", d)


@permission_required("viewer.view_monitored_links")
def edit_monitored_link(request: HttpRequest, pk: int) -> HttpResponse:
    link = MonitoredLink.objects.filter(pk=pk).first()
    if not link:
        raise Http404("Monitored link not found")

    if request.method == "POST":
        edit_form = MonitoredLinkCreateOrEditForm(request.POST, instance=link)
        if edit_form.is_valid():
            updated_link = edit_form.save()
            messages.success(request, f"Monitored link '{updated_link.name}' successfully updated")
            event_log(request.user, "CHANGE_MONITORED_LINK", content_object=updated_link, result="changed")
            return HttpResponseRedirect(reverse("viewer:monitored-links"))
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")
    else:
        edit_form = MonitoredLinkCreateOrEditForm(instance=link)

    d = {
        "link": link,
        "edit_form": edit_form,
        "title": f"Edit Monitored Link: {link.name}",
        "is_create": False,
    }
    return render(request, "viewer/collaborators/edit_monitored_link.html", d)


@permission_required("viewer.view_monitored_links")
def delete_monitored_link(request: HttpRequest, pk: int) -> HttpResponse:
    link = MonitoredLink.objects.filter(pk=pk).first()
    if not link:
        raise Http404("Monitored link not found")

    if request.method == "POST":
        link_name = link.name
        link.delete()
        messages.success(request, f"Monitored link '{link_name}' successfully deleted")
        event_log(request.user, "DELETE_MONITORED_LINK", result=f"deleted {link_name}")
        return HttpResponseRedirect(reverse("viewer:monitored-links"))

    d = {
        "link": link,
        "title": f"Delete Monitored Link: {link.name}",
    }
    return render(request, "viewer/collaborators/delete_monitored_link.html", d)


@permission_required("viewer.view_monitored_links")
def monitored_link_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    link = MonitoredLink.objects.filter(pk=pk).first()
    if not link:
        raise Http404("Monitored link not found")

    if action == "start":
        link.start_running()
        messages.success(request, f"Requested worker to start monitored link: '{link.name}'")
        event_log(request.user, "START_MONITORED_LINK", content_object=link, result="started")
    elif action == "stop":
        link.stop_running()
        messages.success(request, f"Requested worker to stop monitored link: '{link.name}'")
        event_log(request.user, "STOP_MONITORED_LINK", content_object=link, result="stopped")
    elif action == "force_run":
        link.force_run()
        messages.success(request, f"Requested worker to force-run monitored link: '{link.name}'")
        event_log(request.user, "FORCE_RUN_MONITORED_LINK", content_object=link, result="force_run")
    elif action == "delete" and request.method == "POST":
        link_name = link.name
        link.delete()
        messages.success(request, f"Monitored link '{link_name}' successfully deleted")
        event_log(request.user, "DELETE_MONITORED_LINK", result=f"deleted {link_name}")
    else:
        messages.warning(request, f"Unknown action: '{action}'")

    return HttpResponseRedirect(clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:monitored-links"))))


@login_required
def user_token(request: AuthenticatedHttpRequest, operation: str) -> HttpResponse:

    if operation == "create":

        token_name = request.GET.get("token-name", None)

        if not token_name:
            messages.error(request, "Missing parameter 'token-name'.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        token_exists = request.user.long_lived_tokens.filter(name=token_name)
        if len(token_exists) > 0:
            messages.error(request, "Cannot create token with name: {}. Already exists.".format(token_name))
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        else:
            secret_key = get_random_string(UserLongLivedToken.TOKEN_LENGTH, UserLongLivedToken.VALID_CHARS)

            salted_key = UserLongLivedToken.create_salted_key_from_key(secret_key)

            UserLongLivedToken.objects.create(
                key=salted_key,
                user=request.user,
                name=token_name,
            )
            messages.success(
                request,
                "Token with name: {} created successfully. Write down the token itself it will be only shown here (everything inside <>): <{}>.".format(
                    token_name, secret_key
                ),
            )
    elif operation == "delete":

        token_id = request.GET.get("id", None)

        if token_id is None:
            messages.error(request, "Missing parameter 'id'.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        token_exists = request.user.long_lived_tokens.filter(pk=token_id)
        if len(token_exists) < 1:
            messages.error(
                request,
                "Cannot delete token with ID: {}. It does not exist or does not belong to you.".format(token_id),
            )
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        else:
            token_exists.delete()
            messages.success(request, "Token with ID: {} deleted successfully.".format(token_id))

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

