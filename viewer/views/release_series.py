"""Release completeness: the issues of magazines and other periodicals missing from the collection."""

import json
import logging
import typing

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.base.setup import Settings
from viewer.forms import ReleaseSeriesForm
from viewer.models import Archive, ReleaseSeries, ReleaseSeriesDismissal
from viewer.services import release_series as rs
from viewer.utils.actions import event_log
from workers import client

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)

EXPORT_FIELDS = (
    "name",
    "key_type",
    "title_contains",
    "title_excludes",
    "title_regex",
    "tags",
    "allowed_languages",
    "schedule",
    "lead_days",
    "backlog_query",
    "backlog_page_query",
    "active",
    "notes",
)
MAX_BACKLOG_URLS = 200


def _wants_json(request: HttpRequest) -> bool:
    return request.GET.get("format") == "json"


def _series_or_404(pk: int) -> ReleaseSeries:
    return get_object_or_404(ReleaseSeries.objects.prefetch_related("dismissals"), pk=pk)


# --- Overview ---


@staff_member_required(login_url="viewer:login")
def release_series_list(request: HttpRequest) -> HttpResponse:
    """Every series with its missing issues, and the missing issues of all of them by estimated release date."""
    if request.method == "POST":
        if request.POST.get("action") == "import":
            return _import_series(request)
        messages.error(request, "Unknown action.", extra_tags="danger")
        return HttpResponseRedirect(reverse("viewer:release-series"))

    all_series = list(ReleaseSeries.objects.prefetch_related("dismissals"))
    if request.GET.get("export") == "1":
        return _export_series(all_series)

    today = timezone.localdate()
    results = [rs.check_series(series, today=today) for series in all_series if series.active]
    inactive = [series for series in all_series if not series.active]

    if _wants_json(request):
        return JsonResponse(
            {
                "series": [r.as_dict() for r in results],
                "inactive": [{"id": s.pk, "name": s.name} for s in inactive],
            }
        )

    windows = rs.gap_windows(results)
    due = [(r, issue) for r in results for issue in r.upcoming if issue.status == "due"]
    d: dict[str, typing.Any] = {
        "results": results,
        "inactive": inactive,
        "windows": windows,
        "due": due,
        "missing_total": sum(len(r.missing) for r in results),
        "present_total": sum(len(r.present) for r in results),
        "archived_total": sum(len(r.archived) for r in results),
        "error_count": sum(1 for r in results if r.error),
        "multi_series_windows": sum(1 for w in windows if w.series_count > 1),
        "backlog_sources": rs.backlog_sources(),
        "can_add": request.user.has_perm("viewer.crawler_adder"),
        "today": today,
        "window_gap_days": f"{rs.WINDOW_GAP_DAYS} days",
    }
    return render(request, "viewer/release_series_list.html", d)


def _export_series(all_series: list[ReleaseSeries]) -> HttpResponse:
    data = {
        "version": 1,
        "series": [
            {
                **{field: getattr(series, field) for field in EXPORT_FIELDS},
                "dismissals": [
                    {"key": d.key, "reason": d.reason, "comment": d.comment} for d in series.dismissals.all()
                ],
            }
            for series in all_series
        ],
    }
    response = HttpResponse(json.dumps(data, indent=2, ensure_ascii=False), content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="release-series.json"'
    return response


def _import_series(request: HttpRequest) -> HttpResponse:
    """Create or update series by name from an export. Dismissals are added or updated, never removed."""
    raw = request.POST.get("data", "")
    upload = request.FILES.get("file")
    if upload is not None:
        raw = upload.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
        items = data["series"] if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("expected a list of series")
    except (ValueError, KeyError, TypeError) as e:
        messages.error(request, f"Could not read the series to import: {e}", extra_tags="danger")
        return HttpResponseRedirect(reverse("viewer:release-series"))

    created = updated = 0
    errors = []
    with transaction.atomic():
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                errors.append("an entry without a name")
                continue
            instance = ReleaseSeries.objects.filter(name=item["name"]).first()
            values = {field: item[field] for field in EXPORT_FIELDS if field in item and item[field] is not None}
            # Fields left out keep their value, or the default for a new series (a missing checkbox would be False).
            base = instance if instance is not None else ReleaseSeries()
            form_data = {field: getattr(base, field) for field in EXPORT_FIELDS} | values
            form = ReleaseSeriesForm(data=form_data, instance=instance)
            if not form.is_valid():
                errors.append(f"{item['name']}: {'; '.join(str(m) for ms in form.errors.values() for m in ms)}")
                continue
            series = form.save(commit=False)
            if instance is None:
                series.user = request.user
                created += 1
            else:
                updated += 1
            series.save()
            for dismissal in item.get("dismissals") or []:
                if isinstance(dismissal, dict) and dismissal.get("key"):
                    reason = dismissal.get("reason") or ReleaseSeriesDismissal.Reason.NOT_RELEASED
                    if reason not in ReleaseSeriesDismissal.Reason.values:
                        reason = ReleaseSeriesDismissal.Reason.OTHER
                    ReleaseSeriesDismissal.objects.update_or_create(
                        series=series,
                        key=str(dismissal["key"])[:50],
                        defaults={"reason": reason, "comment": str(dismissal.get("comment") or "")[:500]},
                    )
    if created or updated:
        messages.success(request, f"Imported release series: {created} created, {updated} updated.")
        event_log(request.user, "IMPORT_RELEASE_SERIES", result=f"{created} created, {updated} updated")
    for error in errors:
        messages.error(request, f"Not imported: {error}", extra_tags="danger")
    return HttpResponseRedirect(reverse("viewer:release-series"))


# --- Create / edit ---


@staff_member_required(login_url="viewer:login")
def release_series_create(request: HttpRequest) -> HttpResponse:
    form = ReleaseSeriesForm(request.POST or None, initial={"allowed_languages": "japanese"})
    if request.method == "POST" and form.is_valid():
        series = form.save(commit=False)
        series.user = request.user
        series.save()
        messages.success(request, f"Created release series '{series.name}'.")
        event_log(request.user, "ADD_RELEASE_SERIES", content_object=series, result="created")
        return HttpResponseRedirect(reverse("viewer:release-series-detail", args=(series.pk,)))
    return render(
        request,
        "viewer/release_series_form.html",
        {
            "form": form,
            "series": None,
            "default_backlog_query": rs.default_backlog_query(""),
            "default_backlog_page_query": rs.default_backlog_page_query(""),
        },
    )


@staff_member_required(login_url="viewer:login")
def release_series_edit(request: HttpRequest, pk: int) -> HttpResponse:
    series = _series_or_404(pk)
    if request.method == "POST" and request.POST.get("action") == "delete":
        name = series.name
        series.delete()
        messages.success(request, f"Deleted release series '{name}'.")
        event_log(request.user, "DELETE_RELEASE_SERIES", result=f"deleted {name}")
        return HttpResponseRedirect(reverse("viewer:release-series"))
    form = ReleaseSeriesForm(request.POST or None, instance=series)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved release series '{series.name}'.")
        event_log(request.user, "CHANGE_RELEASE_SERIES", content_object=series, result="changed")
        return HttpResponseRedirect(reverse("viewer:release-series-detail", args=(series.pk,)))
    return render(
        request,
        "viewer/release_series_form.html",
        {
            "form": form,
            "series": series,
            "default_backlog_query": rs.default_backlog_query(series.key_type),
            "default_backlog_page_query": rs.default_backlog_page_query(series.key_type),
        },
    )


# --- Detail ---


@staff_member_required(login_url="viewer:login")
def release_series_detail(request: HttpRequest, pk: int) -> HttpResponse:
    series = _series_or_404(pk)
    if request.method == "POST":
        return _detail_action(request, series)

    today = timezone.localdate()
    result = rs.check_series(series, today=today)
    if _wants_json(request):
        return JsonResponse(result.as_dict(include_entries=True))

    d: dict[str, typing.Any] = {
        "series": series,
        "result": result,
        "rows": result.rows(),
        "month_headers": range(1, 13),
        "dismiss_reasons": ReleaseSeriesDismissal.Reason.choices,
        "dismissals": list(series.dismissals.select_related("user")),
        "backlog_sources": rs.backlog_sources(),
        "can_add": request.user.has_perm("viewer.crawler_adder"),
        "schedule_differs": result.suggested_schedule.strip() != series.schedule.strip(),
        "today": today,
    }
    return render(request, "viewer/release_series_detail.html", d)


def _detail_action(request: HttpRequest, series: ReleaseSeries) -> HttpResponse:
    p = request.POST
    action = p.get("action", "")
    redirect = HttpResponseRedirect(reverse("viewer:release-series-detail", args=(series.pk,)))

    if action == "dismiss":
        keys = [k.strip() for k in p.getlist("key") if k.strip()]
        reason = p.get("reason", ReleaseSeriesDismissal.Reason.NOT_RELEASED)
        if reason not in ReleaseSeriesDismissal.Reason.values:
            reason = ReleaseSeriesDismissal.Reason.OTHER
        comment = p.get("comment", "")[:500]
        if not keys:
            messages.warning(request, "No issues selected.")
            return redirect
        for key in keys:
            ReleaseSeriesDismissal.objects.update_or_create(
                series=series,
                key=key[:50],
                defaults={
                    "reason": reason,
                    "comment": comment,
                    "user": typing.cast(typing.Any, request.user),
                    "created_at": timezone.now(),
                },
            )
        messages.success(request, f"Dismissed {len(keys)} issue(s): {', '.join(keys)}.")
        event_log(request.user, "DISMISS_RELEASE_SERIES_ISSUE", content_object=series, data=", ".join(keys), result=reason)
    elif action == "undismiss":
        keys = [k.strip() for k in p.getlist("key") if k.strip()]
        deleted, _ = series.dismissals.filter(key__in=keys).delete()
        messages.success(request, f"Restored {deleted} issue(s): they count as missing again if not found.")
    elif action == "apply_schedule":
        schedule = p.get("schedule", "")
        try:
            rs.parse_schedule(series.key_type, schedule)
        except rs.ConfigError as e:
            messages.error(request, str(e), extra_tags="danger")
            return redirect
        series.schedule = schedule.strip()
        series.save(update_fields=["schedule", "updated_at"])
        messages.success(request, "Saved the schedule.")
    elif action == "apply_lead_days":
        try:
            series.lead_days = int(p.get("lead_days", ""))
        except ValueError:
            messages.error(request, "Invalid lead days.", extra_tags="danger")
            return redirect
        series.save(update_fields=["lead_days", "updated_at"])
        messages.success(request, f"Lead days set to {series.lead_days}.")
    else:
        messages.error(request, f"Unknown action '{action}'.", extra_tags="danger")
    return redirect


# --- Backlog search ---


@staff_member_required(login_url="viewer:login")
@require_POST
def release_series_backlog_search(request: HttpRequest, pk: int) -> JsonResponse:
    """Search the backlog sources for a missing issue, without adding anything. Answers JSON.

    POST: key, source (several, empty for all), title (optional: the title to search instead of the series' name and
    the issue).
    """
    series = _series_or_404(pk)
    try:
        key = rs.parse_key(series.key_type, request.POST.get("key", ""))
        sources = [s for s in request.POST.getlist("source") if s]
        default_title = rs.search_title(series, key)
        title = rs.clean_search_title(request.POST.get("title", ""))
        if title == default_title:
            title = ""
        queries, hits, errors = rs.search_backlog(series, key, sources or None, title=title)
    except rs.ConfigError as e:
        return JsonResponse({"error": str(e)}, status=400)
    return JsonResponse(
        {
            "series": series.name,
            "key": rs.key_label(series.key_type, key),
            "title": title or default_title,
            "default_title": default_title,
            "queries": queries,
            "hits": [h.as_dict() for h in hits],
            "errors": errors,
        }
    )


@staff_member_required(login_url="viewer:login")
@require_POST
def release_series_backlog_add(request: HttpRequest) -> JsonResponse:
    """Queue the chosen backlog hits' links in the web crawler, like Add URLs."""
    if not request.user.has_perm("viewer.crawler_adder"):
        return JsonResponse({"error": "You don't have permission to add links."}, status=403)
    urls = list(dict.fromkeys(u.strip() for u in request.POST.getlist("url") if u.strip().startswith("http")))
    if not urls:
        return JsonResponse({"error": "No links selected."}, status=400)
    if len(urls) > MAX_BACKLOG_URLS:
        return JsonResponse({"error": f"Too many links (max {MAX_BACKLOG_URLS})."}, status=400)
    reason = request.POST.get("reason", "").strip()[:200]

    current_settings = Settings(load_from_config=crawler_settings.config)
    current_settings.replace_metadata = False
    current_settings.retry_failed = True
    if reason:
        current_settings.archive_reason = reason
        current_settings.gallery_reason = reason
    current_settings.archive_user = request.user
    current_settings.archive_origin = Archive.ORIGIN_ADD_URL
    current_settings.event_action = "ADD"
    task = client.enqueue(
        args=urls,
        task_type="web_crawler",
        override_options=current_settings,
        user=request.user,
        reason=reason,
        event_action="ADD",
    )
    for url in urls:
        logger.info("User %s: queued backlog link from release completeness: %s", request.user.username, url)
        event_log(request.user, "CRAWL_URL", reason=reason, data=url, result="queued")
    task_id = getattr(task, "pk", None)
    return JsonResponse({"queued": len(urls), "task_id": task_id})
