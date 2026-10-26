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
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404, QueryDict, JsonResponse
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



@login_required
def my_event_log(request: AuthenticatedHttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    selected_actions = get.getlist("actions")

    if selected_actions:
        results = EventLog.objects.filter(action__in=selected_actions)
    else:
        results = EventLog.objects.all()

    results = results.filter(user=request.user).prefetch_related("content_object")

    actions = EventLog.objects.filter(user=request.user).order_by().values_list("action", flat=True).distinct()

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "actions": actions}
    return render(request, "viewer/collaborators/event_log.html", d)


@permission_required("viewer.read_activity_logs")
def activity_event_log(request: HttpRequest) -> HttpResponse:
    get = request.GET

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    selected_actions = get.getlist("actions")

    data_field = get.get("data_field", "")
    result_field = get.get("result_field", "")
    title = get.get("title", "")
    tags = get.get("tags", "")
    filter_galleries = get.get("filter-galleries", "")
    filter_archives = get.get("filter-archives", "")
    event_date_from = get.get("event_date_from", "")
    event_date_to = get.get("event_date_to", "")
    if request.user.is_staff or request.user.has_perm("viewer.read_all_logs"):
        show_users = get.get("show-users", "")
    else:
        show_users = ""

    event_content_type = get.get("content_type", "")
    event_content_id = get.get("content_id", "")

    if "clear" in get:
        form = AllGalleriesSearchForm()
        form_event = EventSearchForm()
    else:
        form = AllGalleriesSearchForm(initial={"title": title, "tags": tags})
        form_event = EventSearchForm(
            initial={
                "data_field": data_field,
                "event_date_from": event_date_from,
                "event_date_to": event_date_to,
                "content_type": event_content_type,
                "content_id": event_content_id,
            }
        )

    if selected_actions:
        results = EventLog.objects.filter(action__in=selected_actions).prefetch_related(
            "content_object", "content_type"
        )
    else:
        results = EventLog.objects.all().prefetch_related("content_object", "content_type")

    if show_users:
        results = results.select_related("user")

    if event_date_from:
        results = results.filter(create_date__gte=event_date_from)
    if event_date_to:
        results = results.filter(create_date__lte=event_date_to)

    if data_field:
        results = results.filter(data__contains=data_field)

    if result_field:
        results = results.filter(result__contains=result_field)

    if event_content_type in ("archive", "gallery"):
        if event_content_type == "archive":
            event_type = ContentType.objects.get_for_model(Archive)
            results = results.filter(content_type=event_type)
        elif event_content_type == "gallery":
            event_type = ContentType.objects.get_for_model(Gallery)
            results = results.filter(content_type=event_type)

    if event_content_id:
        results = results.filter(object_id=event_content_id)

    if title or tags and (filter_galleries or filter_archives):

        total_filters = Q()

        if filter_galleries:
            params = {}

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in gallery_filter_keys:
                if k not in params:
                    params[k] = ""

            gallery_results = filter_galleries_simple(params)

            gallery_type = ContentType.objects.get_for_model(Gallery)
            total_filters.add(Q(object_id__in=gallery_results, content_type=gallery_type), Q.OR)

        if filter_archives:
            params = {
                "sort": "create_date",
                "asc_desc": "desc",
            }

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in archive_filter_keys:
                if k not in params:
                    params[k] = ""

            archive_results = filter_archives_simple(params, request.user.is_authenticated, show_binned=True)

            archive_type = ContentType.objects.get_for_model(Archive)
            total_filters.add(Q(object_id__in=archive_results, content_type=archive_type), Q.OR)

        results = results.filter(total_filters)

    actions = EventLog.objects.order_by("action").values_list("action", flat=True).distinct()

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "actions": actions, "form": form, "form_event": form_event, "show_users": show_users}
    return render(request, "viewer/collaborators/activity_event_log.html", d)


@permission_required("viewer.download_history")
def download_history(request: AuthenticatedHttpRequest) -> HttpResponse:

    if request.method == "POST":
        if not request.user.is_staff:
            if request.content_type == "application/json" or request.GET.get("json"):
                return JsonResponse({"error": "Staff permission required."}, status=403)
            return render_error(request, "Staff permission required to modify download events.")

        is_json = request.content_type == "application/json" or bool(request.GET.get("json"))
        if request.content_type == "application/json":
            try:
                body_data = json.loads(request.body)
                action = body_data.get("action", "")
                selected_ids = body_data.get("selected_events", [])
            except Exception:
                action = ""
                selected_ids = []
        else:
            action = request.POST.get("action", "")
            selected_ids = request.POST.getlist("selected_events")

        try:
            event_pks = [int(x) for x in selected_ids if str(x).isdigit()]
        except (ValueError, TypeError):
            event_pks = []

        if not event_pks:
            if is_json:
                return JsonResponse({"error": "No download events selected."}, status=400)
            messages.warning(request, "No download events were selected.")
            return HttpResponseRedirect(clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:downloads"))))

        events_qs = DownloadEvent.objects.filter(pk__in=event_pks)
        count = 0

        if action == "mark_failed":
            for ev in events_qs:
                ev.set_as_failed("Marked as failed by staff in download monitor")
                ev.save()
                count += 1
            msg = f"{count} download event{'s' if count != 1 else ''} marked as failed."
            messages.success(request, msg)
            event_log(request.user, "DOWNLOAD_EVENTS_MARK_FAILED", result=f"marked {count} failed")

        elif action == "mark_completed":
            for ev in events_qs:
                ev.finish_download()
                ev.save()
                count += 1
            msg = f"{count} download event{'s' if count != 1 else ''} marked as completed."
            messages.success(request, msg)
            event_log(request.user, "DOWNLOAD_EVENTS_MARK_COMPLETED", result=f"marked {count} completed")

        elif action == "delete_queued":
            queued_events = events_qs.filter(status=DownloadEvent.Status.QUEUED)
            for ev in queued_events:
                archive = ev.archive
                if archive and not archive.crc32 and not archive.is_recycled():
                    has_other_active = (
                        DownloadEvent.objects.filter(archive_id=archive.pk, completed=False)
                        .exclude(pk=ev.pk)
                        .exclude(status__in=[DownloadEvent.Status.COMPLETED, DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED])
                        .exists()
                    )
                    if not has_other_active:
                        archive.move_to_recycle_bin(reason="Download removed from queue by staff")
                ev.delete()
                count += 1
            if count > 0:
                msg = f"{count} queued download event{'s' if count != 1 else ''} removed from queue."
                messages.success(request, msg)
                event_log(request.user, "DOWNLOAD_EVENTS_DELETE_QUEUED", result=f"deleted {count} queued")
            else:
                msg = "No queued download events were selected (only queued events can be removed from queue)."
                messages.warning(request, msg)

        else:
            if is_json:
                return JsonResponse({"error": f"Unknown action: '{action}'"}, status=400)
            messages.error(request, f"Unknown action: '{action}'")
            return HttpResponseRedirect(clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:downloads"))))

        if is_json:
            return JsonResponse({"success": True, "action": action, "count": count, "message": msg})

        return HttpResponseRedirect(clean_up_referer(request.META.get("HTTP_REFERER", reverse("viewer:downloads"))))
    
    if len(request.GET) == 0:
        form = DownloadHistoryForm(initial={"status": "in_progress", "sort_by": "create_date", "direction": "desc"})
    else:
        form = DownloadHistoryForm(request.GET)

    download_events = DownloadEvent.objects.select_related("archive", "gallery")

    if form.is_valid():
        data = form.cleaned_data
        
        if data.get("name"):
            download_events = download_events.filter(name__icontains=data["name"])

        if data.get("title"):
             download_events = download_events.filter(archive__title__icontains=data["title"])
        
        if data.get("method"):
            download_events = download_events.filter(method__icontains=data["method"])
        
        status = data.get("status")
        if not status and "status" not in request.GET:
            status = "in_progress"

        if status == "in_progress":
            download_events = download_events.filter(
                completed=False
            ).exclude(
                status__in=[DownloadEvent.Status.COMPLETED, DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED]
            )
        elif status == "queued":
            download_events = download_events.filter(
                status=DownloadEvent.Status.QUEUED, completed=False, failed=False
            )
        elif status == "downloading":
            download_events = download_events.filter(
                status=DownloadEvent.Status.IN_PROGRESS, completed=False, failed=False
            )
        elif status == "finished":
            download_events = download_events.filter(
                Q(completed=True, failed=False) | Q(status=DownloadEvent.Status.COMPLETED)
            ).exclude(failed=True).exclude(status__in=[DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED])
        elif status == "failed":
            download_events = download_events.filter(
                Q(failed=True) | Q(status=DownloadEvent.Status.FAILED)
            ).exclude(status=DownloadEvent.Status.CANCELLED)
        elif status == "cancelled":
            download_events = download_events.filter(status=DownloadEvent.Status.CANCELLED)
        # "all" does no filtering
        
        if data.get("create_date_from"):
            download_events = download_events.filter(create_date__date__gte=data["create_date_from"])
            
        if data.get("create_date_to"):
            download_events = download_events.filter(create_date__date__lte=data["create_date_to"])

        sort_by = data.get("sort_by") or "create_date"
        direction = data.get("direction") or "desc"
        
        if direction == "desc":
            sort_by = f"-{sort_by}"
            
        download_events = download_events.order_by(sort_by)
    else:
        # Fallback default behavior
        download_events = (
            download_events.filter(completed=False)
            .exclude(status__in=[DownloadEvent.Status.COMPLETED, DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED])
            .order_by("-create_date")
        )

    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1

    try:
        limit = max(1, int(request.GET.get("limit", "100")))
    except ValueError:
        limit = 100

    paginator = Paginator(download_events, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    if request.GET.get("json"):
        from django.template.defaultfilters import filesizeformat

        serialized_results = []
        for event in results_page:
            archive_dict = None
            if event.archive:
                thumb_url = (
                    event.archive.thumbnail.url
                    if getattr(event.archive, "thumbnail", None) and event.archive.thumbnail.name
                    else ""
                )
                archive_dict = {
                    "id": event.archive.pk,
                    "title": event.archive.title,
                    "url": reverse("viewer:archive", args=[event.archive.pk]),
                    "thumb_url": thumb_url,
                }

            gallery_dict = None
            if event.gallery:
                thumb_url = (
                    event.gallery.thumbnail.url
                    if getattr(event.gallery, "thumbnail", None) and event.gallery.thumbnail.name
                    else ""
                )
                gallery_dict = {
                    "id": event.gallery.pk,
                    "title": event.gallery.title,
                    "url": reverse("viewer:gallery", args=[event.gallery.pk]),
                    "thumb_url": thumb_url,
                }

            end_date_str = ""
            if event.completed:
                end_date_str = event.completed_date.strftime("%Y-%m-%d %H:%M:%S") if event.completed_date else ""
            else:
                eta_val = event.eta()
                if eta_val:
                    end_date_str = eta_val.strftime("%Y-%m-%d %H:%M:%S")

            create_date_str = event.create_date.strftime("%Y-%m-%d %H:%M:%S") if event.create_date else ""

            effective_status = event.effective_status
            status_display = (
                DownloadEvent.Status(effective_status).label
                if effective_status in DownloadEvent.Status.values
                else (event.get_status_display() if hasattr(event, "get_status_display") else effective_status)
            )

            serialized_results.append(
                {
                    "id": event.id,
                    "name": event.name,
                    "method": event.method,
                    "provider": event.provider,
                    "status": effective_status,
                    "status_display": status_display,
                    "archive": archive_dict,
                    "gallery": gallery_dict,
                    "total_size": event.total_size,
                    "total_size_formatted": filesizeformat(event.total_size) if event.total_size else "0 Bytes",
                    "progress": round(float(event.progress), 1),
                    "failed": bool(event.failed or effective_status in (DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED)),
                    "completed": bool(event.completed or effective_status == DownloadEvent.Status.COMPLETED),
                    "error_message": event.error_message,
                    "create_date": create_date_str,
                    "end_date_or_eta": end_date_str,
                }
            )

        response_dict = {
            "results": serialized_results,
            "has_previous": results_page.has_previous(),
            "has_next": results_page.has_next(),
            "num_pages": paginator.num_pages,
            "count": paginator.count,
            "in_page_count": len(results_page),
            "number": results_page.number,
        }
        return HttpResponse(json.dumps(response_dict), content_type="application/json; charset=utf-8")

    d = {"results": results_page, "form": form}
    return render(request, "viewer/collaborators/download_history.html", d)


