"""Archive file check: start worker jobs that verify archive files exist (and their CRC32), and review the results."""

import json
import logging
import typing
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import EmptyPage, InvalidPage, Paginator
from django.db.models import Count, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from core.base.setup import Settings
from viewer.models import Archive, ArchiveFileCheck, ArchiveFileCheckIssue
from viewer.services.archive_file_check import (
    MAX_ARCHIVE_IDS,
    create_file_check,
    normalize_folder,
    parse_archive_ids,
)
from viewer.services.archive_selection import can_use_archive_picker, clean_filter
from workers import client

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)

ISSUES_PER_PAGE = 50
RECENT_CHECKS = 15


def _redirect_to(check: ArchiveFileCheck | None, extra: dict[str, str] | None = None) -> HttpResponseRedirect:
    params: dict[str, str] = {}
    if check is not None:
        params["check"] = str(check.pk)
    params.update(extra or {})
    url = reverse("viewer:archive-file-check")
    return HttpResponseRedirect(f"{url}?{urlencode(params)}" if params else url)


def _get_check(value: str | None) -> ArchiveFileCheck | None:
    if value and value.isdigit():
        check = ArchiveFileCheck.objects.select_related("task", "user").filter(pk=int(value)).first()
        if check is None:
            raise Http404("Archive file check not found.")
        return check
    return ArchiveFileCheck.objects.select_related("task", "user").first()


def _issue_filters(params: typing.Mapping[str, str]) -> dict[str, str]:
    """The issue list filters in a request (kind of issue, zip test result), only the valid ones."""
    filters = {}
    if params.get("kind", "") in ArchiveFileCheckIssue.Kind.values:
        filters["kind"] = params["kind"]
    if params.get("zip_test", "") in ArchiveFileCheckIssue.ZipTest.values:
        filters["zip_test"] = params["zip_test"]
    return filters


def _filtered_issues(check: ArchiveFileCheck, filters: dict[str, str]) -> QuerySet[ArchiveFileCheckIssue]:
    return check.issues.select_related("archive", "archive__gallery").filter(**filters).order_by("-archive_id")


def _parse_archive_selection(request: HttpRequest) -> tuple[str, list[int], dict[str, str]]:
    """Read the archive picker widget value (``archive_selection``), or the plain id list when JS is off.

    :return: (scope, archive ids, cleaned filter)
    :raises ValueError: on an invalid or empty selection.
    """
    p = request.POST
    raw = p.get("archive_selection", "").strip()
    if not raw:
        archive_ids = parse_archive_ids(p.get("archive_ids", ""))
        if not archive_ids:
            raise ValueError("Select at least one archive.")
        return ArchiveFileCheck.Scope.ARCHIVES, archive_ids, {}
    try:
        selection = json.loads(raw)
    except ValueError:
        raise ValueError("Invalid archive selection.")
    if not isinstance(selection, dict):
        raise ValueError("Invalid archive selection.")
    if selection.get("mode") == "filter":
        filter_params = selection.get("filter")
        if not isinstance(filter_params, dict):
            raise ValueError("Invalid archive filter.")
        if not can_use_archive_picker(request.user):
            raise ValueError("You can't select archives by filter.")
        return ArchiveFileCheck.Scope.FILTER, [], clean_filter(filter_params, request.user)
    ids = selection.get("ids")
    if not isinstance(ids, list):
        raise ValueError("Invalid archive selection.")
    archive_ids = list(dict.fromkeys(int(x) for x in ids if isinstance(x, int) or (isinstance(x, str) and x.isdigit())))
    if not archive_ids:
        raise ValueError("Select at least one archive.")
    if len(archive_ids) > MAX_ARCHIVE_IDS:
        raise ValueError(f"Too many archives selected (max {MAX_ARCHIVE_IDS}), select them with a filter instead.")
    return ArchiveFileCheck.Scope.ARCHIVES, archive_ids, {}


def _prefill_archive_ids(value: str) -> list[int]:
    try:
        return parse_archive_ids(value)[:1000]
    except ValueError:
        return []


def _queue_check(request: HttpRequest, **kwargs: typing.Any) -> HttpResponse:
    try:
        check = create_file_check(user=request.user, **kwargs)
    except ValueError as e:
        messages.error(request, f"Could not start the check: {e}", extra_tags="danger")
        return _redirect_to(None)
    messages.success(request, f"Queued archive file check #{check.pk} (task #{check.task_id}).")
    logger.info("User %s queued archive file check #%d: %s", request.user, check.pk, check.scope_description)
    return _redirect_to(check)


def _start_check(request: HttpRequest) -> HttpResponse:
    p = request.POST
    scope = p.get("scope", ArchiveFileCheck.Scope.ALL)
    archive_ids: list[int] = []
    folders: list[str] = []
    filter_params: dict[str, str] = {}
    try:
        if scope == ArchiveFileCheck.Scope.ARCHIVES:
            scope, archive_ids, filter_params = _parse_archive_selection(request)
        elif scope == ArchiveFileCheck.Scope.FOLDERS:
            raw = [line for line in p.get("folders", "").splitlines() if line.strip()]
            folders = list(dict.fromkeys(normalize_folder(f, settings.MEDIA_ROOT) for f in raw))
            if not folders:
                raise ValueError("Select at least one folder.")
        elif scope != ArchiveFileCheck.Scope.ALL:
            raise ValueError(f"Unknown scope '{scope}'.")
    except ValueError as e:
        messages.error(request, f"Could not start the check: {e}", extra_tags="danger")
        return _redirect_to(None)
    return _queue_check(
        request,
        scope=scope,
        archive_ids=archive_ids,
        folders=folders,
        filter_params=filter_params,
        check_crc32=p.get("check_crc32") in ("1", "on", "true"),
        test_zip=p.get("test_zip") in ("1", "on", "true"),
        include_binned=p.get("include_binned") in ("1", "on", "true"),
    )


def _rerun_check(request: HttpRequest, check: ArchiveFileCheck) -> HttpResponse:
    """Queue a new check with the same scope and options. A filter is applied again, by the current user."""
    filter_params: dict[str, str] = {}
    if check.scope == ArchiveFileCheck.Scope.FILTER:
        try:
            filter_params = clean_filter(check.filter_params or {}, request.user)
        except ValueError as e:
            messages.error(request, f"Could not start the check: {e}", extra_tags="danger")
            return _redirect_to(check)
    return _queue_check(
        request,
        scope=check.scope,
        archive_ids=list(check.archive_ids or []),
        folders=list(check.folders or []),
        filter_params=filter_params,
        check_crc32=check.check_crc32,
        test_zip=check.test_zip,
        include_binned=check.include_binned,
    )


def _queue_downloads(request: HttpRequest, archives: QuerySet[Archive]) -> int:
    queued = 0
    for archive in archives.filter(gallery__isnull=False).select_related("gallery"):
        if archive.gallery is None:
            continue
        # Force replace_metadata when queueing from this list, since it's mostly used to download non-used.
        current_settings = Settings(load_from_config=crawler_settings.config)
        current_settings.replace_metadata = True
        current_settings.retry_failed = True
        current_settings.redownload = True
        if archive.reason:
            current_settings.archive_reason = archive.reason
        current_settings.archive_user = request.user
        current_settings.event_action = "DOWNLOAD"
        client.enqueue(
            args=[archive.gallery.get_link()],
            task_type="web_crawler",
            override_options=current_settings,
            user=request.user,
            reason=archive.reason,
            event_action="DOWNLOAD",
        )
        logger.info("Queueing gallery: %s, link: %s", archive.gallery.title, archive.gallery.get_link())
        queued += 1
    return queued


def _issue_action(request: HttpRequest, check: ArchiveFileCheck) -> HttpResponse:
    p = request.POST
    action = p.get("issue_action", "")
    filters = _issue_filters(p)
    if p.get("apply_to_all_filtered") in ("1", "on", "true"):
        issues = _filtered_issues(check, filters)
    else:
        selected = [int(v) for v in p.getlist("issue") if v.isdigit()]
        issues = check.issues.filter(pk__in=selected)
    redirect_extra = filters
    archive_ids = list(issues.values_list("archive_id", flat=True))
    if not archive_ids:
        messages.warning(request, "No issues selected.")
        return _redirect_to(check, redirect_extra)
    archives = Archive.objects.filter(pk__in=archive_ids)

    if action == "recheck":
        new_check = create_file_check(
            scope=ArchiveFileCheck.Scope.ARCHIVES,
            archive_ids=archive_ids,
            check_crc32=check.check_crc32 or p.get("recheck_crc32") in ("1", "on", "true"),
            test_zip=check.test_zip,
            user=request.user,
        )
        messages.success(request, f"Queued file check #{new_check.pk} for {len(archive_ids)} archive(s).")
        return _redirect_to(new_check)
    elif action == "download":
        if not request.user.has_perm("viewer.download_gallery"):
            messages.error(request, "You don't have permission to queue downloads.", extra_tags="danger")
            return _redirect_to(check, redirect_extra)
        queued = _queue_downloads(request, archives)
        messages.success(request, f"Queued {queued} gallery download(s) (archives without a gallery are skipped).")
    elif action == "recalc_info":
        task = client.enqueue(
            args=archive_ids,
            task_type="fileinfo_worker",
            user=request.user,
            reason=f"Recalculate file info from file check #{check.pk}",
            event_action="RECALC_FILE_INFO",
        )
        messages.success(
            request, f"Queued task #{task.pk} to recalculate file info (size, CRC32) of {len(archive_ids)} archive(s)."
        )
    elif action == "clear_crc32":
        updated = archives.update(crc32="")
        messages.success(request, f"Cleared the CRC32 of {updated} archive(s), to force a redownload.")
    elif action == "mark_deleted":
        updated = archives.update(file_deleted=True)
        issues.delete()
        messages.success(request, f"Marked {updated} archive(s) as deleted.")
    elif action == "delete":
        deleted = 0
        for archive in archives:
            logger.info(
                "User %s removed archive missing in filesystem: %s, file: %s",
                request.user,
                archive.title,
                archive.zipped.name,
            )
            archive.delete()
            deleted += 1
        messages.success(request, f"Deleted {deleted} archive(s) from the database.")
    elif action == "dismiss":
        dismissed, _ = issues.delete()
        messages.success(request, f"Dismissed {dismissed} issue(s).")
    else:
        messages.error(request, f"Unknown action '{action}'.", extra_tags="danger")
    return _redirect_to(check, redirect_extra)


@staff_member_required(login_url="viewer:login")
def archive_file_check(request: HttpRequest) -> HttpResponse:
    """Start archive file checks (run by the workers) and review missing or corrupted files."""
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "start":
            return _start_check(request)
        check = _get_check(request.POST.get("check"))
        if check is None:
            return _redirect_to(None)
        if action == "cancel":
            if check.task_id and client.cancel_by_id(check.task_id):
                messages.success(request, f"Cancelling file check #{check.pk}.")
            elif not check.is_finished:
                check.status = ArchiveFileCheck.Status.CANCELLED
                check.save(update_fields=["status", "updated_at"])
                messages.success(request, f"File check #{check.pk} cancelled.")
            else:
                messages.warning(request, f"File check #{check.pk} already finished.")
            check.refresh_from_db()
            check.sync_with_task()
            return _redirect_to(check)
        if action == "rerun":
            return _rerun_check(request, check)
        if action == "delete_check":
            if not check.is_finished:
                messages.error(request, "Cancel the check before deleting it.", extra_tags="danger")
                return _redirect_to(check)
            pk = check.pk
            check.delete()
            messages.success(request, f"Deleted file check #{pk} and its results.")
            return _redirect_to(None)
        if action == "issues":
            return _issue_action(request, check)
        messages.error(request, f"Unknown action '{action}'.", extra_tags="danger")
        return _redirect_to(check)

    check = _get_check(request.GET.get("check"))
    if check is not None:
        check.sync_with_task()

    filters = _issue_filters(request.GET)
    issues_page = None
    kind_counts: dict[str, int] = {}
    zip_test_counts: dict[str, int] = {}
    if check is not None:
        issues = _filtered_issues(check, filters)
        paginator = Paginator(issues, ISSUES_PER_PAGE)
        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1
        try:
            issues_page = paginator.page(page)
        except (InvalidPage, EmptyPage):
            issues_page = paginator.page(paginator.num_pages)
        kind_counts = dict(check.issues.values_list("kind").annotate(c=Count("id")).values_list("kind", "c"))
        zip_test_counts = dict(
            check.issues.exclude(zip_test="").values_list("zip_test").annotate(c=Count("id")).values_list("zip_test", "c")
        )

    d: dict[str, typing.Any] = {
        "check": check,
        "issues": issues_page,
        "kind": filters.get("kind", ""),
        "zip_test": filters.get("zip_test", ""),
        "page_query": urlencode({"check": check.pk, **filters}) if check else "",
        "kind_choices": [(value, label, kind_counts.get(value, 0)) for value, label in ArchiveFileCheckIssue.Kind.choices],
        "zip_test_choices": [
            (value, label, zip_test_counts[value])
            for value, label in ArchiveFileCheckIssue.ZipTest.choices
            if zip_test_counts.get(value)
        ],
    }

    if request.GET.get("partial") == "issues":
        return render(request, "viewer/include/archive_file_check_issues.html", d)

    recent_checks = list(ArchiveFileCheck.objects.select_related("task", "user")[:RECENT_CHECKS])
    for recent in recent_checks:
        recent.sync_with_task()

    collection = Archive.objects.filter(file_deleted=False, binned=False).aggregate(
        count=Count("id"), size=Sum("filesize")
    )
    d.update(
        {
            "recent_checks": recent_checks,
            "collection_count": collection["count"] or 0,
            "collection_size": collection["size"] or 0,
            "media_root": settings.MEDIA_ROOT,
            "worker_running": client.is_worker_running(),
            "prefill_archive_ids": _prefill_archive_ids(request.GET.get("archives", "")),
            "queued_ahead": (
                client.queue_size("archive_file_check") if check is not None and check.status == "queued" else 0
            ),
        }
    )
    return render(request, "viewer/archive_file_check.html", d)


@staff_member_required(login_url="viewer:login")
def archive_file_check_status_api(request: HttpRequest, pk: int) -> JsonResponse:
    check = ArchiveFileCheck.objects.select_related("task", "user").filter(pk=pk).first()
    if check is None:
        return JsonResponse({"error": "Not found"}, status=404)
    check.sync_with_task()
    data = check.progress_dict()
    data["worker_running"] = client.is_worker_running()
    return JsonResponse(data)
