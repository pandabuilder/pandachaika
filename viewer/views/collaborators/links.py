import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404
from django.db.models import Count, Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.base.utilities import get_schedulers_status
from viewer.utils.general import PAGE_SIZE_OPTIONS, page_size, safe_redirect_back
from viewer.utils.actions import event_log
from workers import client
from viewer.forms import (
    WantedGalleryCreateOrEditForm,
    WantedGalleryCommand,
    MonitoredLinkCreateOrEditForm,
    MonitoredLinkSearchForm,
    UserTokenCreateForm,
)
from viewer.models import (
    Gallery,
    WantedGallery,
    MonitoredLink,
    MonitoredLinkRun,
    ProcessedLinks,
    UserLongLivedToken,
    Category,
    Provider
)
from viewer.services.monitored_link_stats import is_gap, monitored_link_report
from viewer.utils.types import AuthenticatedHttpRequest
from viewer.views.head.auth import NEW_TOKEN_SESSION_KEY

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
        client.enqueue(
            args=[],
            task_type="update_provider_wanted_worker",
            override_options={
                "wanted_publisher": wanted_publisher,
                "artist_or_group_tag": artist_or_group_tag,
                "wanted_providers": wanted_providers,
                "categories": categories,
                "should_search": True,
            },
            user=request.user,
            event_action="CREATE_WANTED_GALLERY_FROM_COMMAND",
        )
        message = "Queued WantedGallery command: {}".format(command)
        messages.success(request, message)
        logger.info("User {}: {}".format(request.user.username, message))
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
        )
    )


# Permission needed for each state-changing monitored link action.
MONITORED_LINK_ACTION_PERMISSIONS = {
    "start": "viewer.change_monitoredlink",
    "stop": "viewer.change_monitoredlink",
    "force_run": "viewer.change_monitoredlink",
    "delete": "viewer.delete_monitoredlink",
}


def _check_monitored_link_action_permission(user: Any, action: str) -> None:
    perm = MONITORED_LINK_ACTION_PERMISSIONS.get(action)
    if perm and not user.has_perm(perm):
        raise PermissionDenied


@permission_required("viewer.view_monitored_links")
def monitored_links(request: HttpRequest) -> HttpResponse:
    get = request.GET
    post = request.POST

    # Handle quick inline actions (Start / Stop / Force Run / Delete). These change state, so they are POST only.
    action = post.get("action")
    link_id_val = post.get("link_id")

    if request.method == "POST" and action and link_id_val:
        _check_monitored_link_action_permission(request.user, action)
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
                return safe_redirect_back(request, reverse("viewer:monitored-links"))
        except (ValueError, TypeError):
            pass

    # Handle inline creation of a MonitoredLink
    if request.method == "POST" and post.get("submit-monitored-link"):
        if not request.user.has_perm("viewer.add_monitoredlink"):
            raise PermissionDenied
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

    priority_filter = get.get("priority", "").strip()
    if priority_filter:
        try:
            results = results.filter(priority=int(priority_filter))
        except ValueError:
            pass

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
                    "cycle_timer": ml.shortest_interval.total_seconds(),
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
                    timer = state.cycle_timer or ml.shortest_interval.total_seconds()
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
    if sort_field not in ("create_date", "name", "last_modified", "frequency", "priority"):
        sort_field = "create_date"
    asc_desc = get.get("asc_desc", "desc")
    order_prefix = "" if asc_desc == "asc" else "-"
    results = results.order_by(f"{order_prefix}{sort_field}")

    # Pagination
    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    limit = page_size(get, default=50)
    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    # Latest capture run and pending links of each link in the page, to see at a glance how they are doing.
    page_pks = [ml.pk for ml in results_page.object_list]
    capture_kinds = (MonitoredLinkRun.Kind.CAPTURE, MonitoredLinkRun.Kind.FULL)
    pending_counts = dict(
        ProcessedLinks.objects.filter(monitored_link_id__in=page_pks, status=ProcessedLinks.Status.PENDING)
        .values_list("monitored_link_id")
        .annotate(count=Count("pk"))
        .values_list("monitored_link_id", "count")
    )
    first_capture_ids = dict(
        MonitoredLinkRun.objects.filter(monitored_link_id__in=page_pks, kind__in=capture_kinds)
        .order_by("monitored_link_id", "started")
        .distinct("monitored_link_id")
        .values_list("monitored_link_id", "pk")
    )
    last_captures = {}
    for run in (
        MonitoredLinkRun.objects.filter(monitored_link_id__in=page_pks, kind__in=capture_kinds, finished__isnull=False)
        .order_by("monitored_link_id", "-started")
        .distinct("monitored_link_id")
    ):
        setattr(run, "gap", is_gap(run, first_capture_ids.get(run.monitored_link_id)))
        last_captures[run.monitored_link_id] = run

    enabled_count = sum(1 for ml in all_links if ml.enabled)
    stats = {
        "total": len(all_links),
        "running": len(running_pks),
        "stopped": len(all_links) - len(running_pks),
        "enabled": enabled_count,
        "disabled": len(all_links) - enabled_count,
        "auto_start": sum(1 for ml in all_links if ml.auto_start),
    }

    d = {
        "results": results_page,
        "title": "Monitored Links",
        "schedulers": schedulers,
        "pending_counts": pending_counts,
        "last_captures": last_captures,
        "edit_form": edit_form,
        "filter_form": filter_form,
        "can_manage": _can_manage_monitored_links(request.user),
        "stats": stats,
        "limit": limit,
        "per_page_options": PAGE_SIZE_OPTIONS,
    }
    return render(request, "viewer/collaborators/monitored_links.html", d)


def _page_of(paginator: Paginator, number: str | None) -> Any:
    try:
        return paginator.page(int(number or 1))
    except (ValueError, InvalidPage, EmptyPage):
        return paginator.page(paginator.num_pages if number and number.isdigit() else 1)


@permission_required("viewer.view_monitored_links")
def monitored_link_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Configuration, schedule and run stats of a monitored link, to judge whether its configuration fits."""
    link = MonitoredLink.objects.select_related("provider", "user").filter(pk=pk).first()
    if not link:
        raise Http404("Monitored link not found")
    get = request.GET

    report = monitored_link_report(link)

    scheduler_state = link.get_scheduler_state()
    scheduler = get_schedulers_status([scheduler_state])[0] if scheduler_state else None

    runs = MonitoredLinkRun.objects.filter(monitored_link=link).select_related("provider")
    run_kind = get.get("run_kind", "")
    if run_kind == "capture":
        runs = runs.filter(kind__in=(MonitoredLinkRun.Kind.CAPTURE, MonitoredLinkRun.Kind.FULL))
    elif run_kind == "process":
        runs = runs.filter(kind__in=(MonitoredLinkRun.Kind.PROCESS, MonitoredLinkRun.Kind.FULL))
    elif run_kind == "issues":
        runs = runs.filter(
            Q(links_known=0, links_new__gt=0)
            | ~Q(error="")
            | Q(stop_reason=MonitoredLinkRun.StopReason.NO_RESPONSE)
            | Q(pending_retried__gt=0)
            | Q(pending_failed__gt=0)
        )
    runs_page = _page_of(Paginator(runs, 25), get.get("runs_page"))
    for run in runs_page:
        setattr(run, "gap", is_gap(run, report["first_capture_id"]))
        setattr(run, "first_capture", run.pk == report["first_capture_id"])
        setattr(run, "new_pct", round(100 * run.links_new / run.links_found) if run.links_found else 0)

    links = ProcessedLinks.objects.filter(monitored_link=link).order_by("-create_date")
    link_status = get.get("link_status", "")
    if link_status in ProcessedLinks.Status.values:
        links = links.filter(status=link_status)
    links_page = _page_of(Paginator(links, 25), get.get("links_page"))

    d = {
        "link": link,
        "title": link.name,
        "report": report,
        "scheduler": scheduler,
        "runs": runs_page,
        "run_kind": run_kind,
        "run_kinds": [("", "All"), ("capture", "Capture"), ("process", "Process"), ("issues", "Issues")],
        "links": links_page,
        "link_status": link_status,
        "link_statuses": ProcessedLinks.Status.choices,
        "limited_wanted_galleries": list(link.limited_wanted_galleries.all()) if link.use_limited_wanted_galleries else [],
    }
    return render(request, "viewer/collaborators/monitored_link.html", d)


@permission_required("viewer.view_monitored_links")
@permission_required("viewer.add_monitoredlink")
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
@permission_required("viewer.change_monitoredlink")
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
@permission_required("viewer.delete_monitoredlink")
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


@require_POST
@permission_required("viewer.view_monitored_links")
def monitored_link_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    _check_monitored_link_action_permission(request.user, action)
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
    elif action == "delete":
        link_name = link.name
        link.delete()
        messages.success(request, f"Monitored link '{link_name}' successfully deleted")
        event_log(request.user, "DELETE_MONITORED_LINK", result=f"deleted {link_name}")
    else:
        messages.warning(request, f"Unknown action: '{action}'")

    return safe_redirect_back(request, reverse("viewer:monitored-links"))


@login_required
@require_POST
def user_token(request: AuthenticatedHttpRequest, operation: str) -> HttpResponse:
    """Create or revoke one of the user's own API tokens. POST only, since both change state."""
    profile_url = reverse("viewer:change-profile")

    if operation == "create":
        form = UserTokenCreateForm(request.POST, user=request.user)

        if not form.is_valid():
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"Cannot create token ({field}): {error}")
            return HttpResponseRedirect(profile_url)

        token, secret_key = UserLongLivedToken.create_for_user(
            request.user,
            form.cleaned_data["name"],
            expire_date=form.expire_date(),
            scopes=form.cleaned_data["scopes"],
        )
        event_log(
            request.user,
            "CREATE_USER_TOKEN",
            data=f"scopes: {','.join(token.scopes)}; expires: {token.expire_date or 'never'}",
            result=f"created {token.name}",
            content_object=token,
        )
        # Handed to the profile page through the session (not a message, which can end up in a cookie); the page
        # pops it, so the secret is shown once, in its own panel.
        request.session[NEW_TOKEN_SESSION_KEY] = {"id": token.pk, "name": token.name, "secret": secret_key}
    elif operation == "delete":
        token_id = request.POST.get("id", "")

        token_to_revoke = request.user.long_lived_tokens.filter(pk=token_id).first() if token_id.isdigit() else None
        if token_to_revoke is None:
            messages.error(
                request,
                "Cannot revoke token with ID: {}. It does not exist or does not belong to you.".format(token_id),
            )
            return HttpResponseRedirect(profile_url)

        token_name = token_to_revoke.name
        token_to_revoke.delete()
        event_log(request.user, "REVOKE_USER_TOKEN", result=f"revoked {token_name}")
        messages.success(request, "Token '{}' revoked successfully.".format(token_name))
    else:
        raise Http404("Unknown token operation")

    return HttpResponseRedirect(profile_url)
