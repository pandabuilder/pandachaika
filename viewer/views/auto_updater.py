"""Auto updater schedules: when each provider's galleries get their metadata refreshed."""

import datetime
import json
import logging
import typing

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse

import django.utils.timezone as django_tz

from core.workers.autoupdate import TimedAutoUpdater
from viewer.forms import AutoUpdaterScheduleForm
from viewer.models import AutoUpdaterSchedule, Gallery
from viewer.services import auto_updater as au
from viewer.utils.actions import event_log

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


def _json_download(data: typing.Any, filename: str) -> HttpResponse:
    response = HttpResponse(json.dumps(data, indent=2, ensure_ascii=False), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _scheduler_info() -> dict[str, typing.Any]:
    info: dict[str, typing.Any] = {
        "enable": crawler_settings.autoupdater.enable,
        "startup": crawler_settings.autoupdater.startup,
        "state": None,
        "check_interval_minutes": TimedAutoUpdater.CHECK_INTERVAL // 60,
    }
    try:
        from workers.models import SchedulerState

        info["state"] = SchedulerState.objects.filter(name=TimedAutoUpdater.thread_name).first()
    except Exception:
        pass
    return info


def _redirect_list() -> HttpResponseRedirect:
    return HttpResponseRedirect(reverse("viewer:auto-updater"))


def _redirect_edit(provider: str) -> HttpResponseRedirect:
    return HttpResponseRedirect(reverse("viewer:auto-updater-edit", args=(provider,)))


def _report_import(request: HttpRequest, result: au.ImportResult, what: str) -> None:
    if result.created or result.updated:
        messages.success(request, f"Imported {what}: {result.created} created, {result.updated} updated.")
        event_log(request.user, "IMPORT_AUTO_UPDATER", result=f"{result.created} created, {result.updated} updated")
    if result.skipped:
        messages.info(request, f"Already there, not changed: {', '.join(result.skipped)}.")
    for error in result.errors:
        messages.error(request, f"Not imported: {error}", extra_tags="danger")
    if not (result.created or result.updated or result.skipped or result.errors):
        messages.warning(request, "Nothing to import.")


def _report_run(request: HttpRequest, result: au.RunResult) -> None:
    if result.error:
        messages.error(request, f"{result.provider}: {result.error}", extra_tags="danger")
    elif result.skipped:
        messages.info(request, f"{result.provider}: {result.skipped}.")
    elif result.queued:
        messages.success(
            request, f"{result.provider}: queued {result.queued} galleries for update (task #{result.task_id})."
        )
    else:
        messages.info(request, f"{result.provider}: no galleries to update right now.")
    event_log(
        request.user,
        "RUN_AUTO_UPDATER",
        data=result.provider,
        result=result.error or result.skipped or f"queued {result.queued}",
    )


def _row(provider: str, schedule: AutoUpdaterSchedule | None, now: datetime.datetime) -> dict[str, typing.Any]:
    row: dict[str, typing.Any] = {"provider": provider, "schedule": schedule}
    if schedule is None:
        return row
    ages = au.schedule_ages(schedule)
    per_day = au.galleries_per_day(provider, now=now)
    row.update(
        {
            "ages": [au.format_age(a) for a in ages],
            "ages_seconds": [int(a.total_seconds()) for a in ages],
            "queue_every": au.format_age(schedule.queue_every),
            "summary": au.summarize(ages),
            "galleries_per_day": per_day,
            "updates_per_day": per_day * len(ages),
            "in_range": au.galleries_in_range(schedule, now).count() if ages else 0,
            "next_run": schedule.next_run_at(),
            "is_due": schedule.active and au.is_due(schedule, now),
            "known": provider in crawler_settings.providers,
        }
    )
    return row


# --- Overview ---


@staff_member_required(login_url="viewer:login")
def auto_updater_list(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        return _list_action(request)

    schedules = {s.provider: s for s in AutoUpdaterSchedule.objects.all()}
    if request.GET.get("export") == "1":
        return _json_download(au.export_data(schedules.values()), "auto-updater.json")
    if request.GET.get("legacy") == "1":
        return _json_download({"version": au.EXPORT_VERSION, "schedules": au.legacy_schedules()}, "auto-updater-legacy.json")
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "schedules": [
                    {
                        **au.schedule_to_dict(s),
                        "ages": [au.format_age(a) for a in au.schedule_ages(s)],
                        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                        "next_run_at": s.next_run_at().isoformat() if s.next_run_at() else None,  # type: ignore[union-attr]
                        "last_run_status": s.last_run_status,
                        "last_run_message": s.last_run_message,
                        "last_run_queued": s.last_run_queued,
                        "last_run_task_id": s.last_run_task_id,
                    }
                    for s in schedules.values()
                ]
            }
        )

    now = django_tz.now()
    updatable = au.updatable_providers()
    providers = sorted(set(updatable) | set(schedules))
    rows = [_row(p, schedules.get(p), now) for p in providers]
    d: dict[str, typing.Any] = {
        "rows": rows,
        "active_count": sum(1 for s in schedules.values() if s.active),
        "schedule_count": len(schedules),
        "updates_per_day": sum(r.get("updates_per_day", 0) for r in rows if r["schedule"] and r["schedule"].active),
        "scheduler": _scheduler_info(),
        "legacy_pending": au.legacy_pending(),
        "providers_setting": crawler_settings.autoupdater.providers,
    }
    return render(request, "viewer/auto_updater_list.html", d)


def _list_action(request: HttpRequest) -> HttpResponse:
    p = request.POST
    action = p.get("action", "")
    if action == "import":
        raw = p.get("data", "")
        upload = request.FILES.get("file")
        if upload is not None:
            raw = upload.read().decode("utf-8", errors="replace")
        try:
            items = au.parse_import(raw)
        except (ValueError, TypeError) as e:
            messages.error(request, f"Could not read the schedules to import: {e}", extra_tags="danger")
            return _redirect_list()
        _report_import(request, au.import_schedules(items, user=request.user), "auto updater schedules")
    elif action == "import_legacy":
        _report_import(
            request,
            au.import_schedules(au.legacy_schedules(), user=request.user, skip_existing=True),
            "the autoupdater settings",
        )
    elif action == "run":
        providers = p.getlist("provider")
        schedules = list(AutoUpdaterSchedule.objects.filter(provider__in=providers))
        if not schedules:
            messages.warning(request, "No schedules to run.")
        for schedule in schedules:
            _report_run(request, au.run_schedule(schedule, user=request.user))
    elif action in ("activate", "deactivate"):
        changed = AutoUpdaterSchedule.objects.filter(provider__in=p.getlist("provider")).update(
            active=action == "activate"
        )
        messages.success(request, f"{'Activated' if action == 'activate' else 'Deactivated'} {changed} schedule(s).")
    else:
        messages.error(request, f"Unknown action '{action}'.", extra_tags="danger")
    return _redirect_list()


# --- Edit ---


LEGACY_PRESET = {
    "key": "legacy",
    "name": "Old settings",
    "description": "The equivalent of the autoupdater settings (cycle_timer, buffer_back and buffer_after).",
}


def _editor_data(provider: str, form_rules: typing.Any) -> dict[str, typing.Any]:
    now = django_tz.now()
    try:
        legacy = au.legacy_rules(crawler_settings, provider)
    except Exception:
        legacy = []
    return {
        "provider": provider,
        "rules": form_rules,
        "presets": au.PRESETS + ([{**LEGACY_PRESET, "rules": legacy}] if legacy else []),
        "galleries_per_day": round(au.galleries_per_day(provider, now=now), 2),
        "changes": au.change_stats(provider, now=now),
        "min_every": au.format_age(au.MIN_EVERY),
        "max_updates": au.MAX_UPDATES,
        "preview_url": reverse("viewer:auto-updater-preview", args=(provider,)),
    }


@staff_member_required(login_url="viewer:login")
def auto_updater_edit(request: HttpRequest, provider: str) -> HttpResponse:
    schedule = AutoUpdaterSchedule.objects.filter(provider=provider).first()
    if schedule is None and provider not in au.updatable_providers():
        messages.error(request, f"Provider '{provider}' can't update its galleries by link.", extra_tags="danger")
        return _redirect_list()
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if schedule is not None and action == "delete":
        schedule.delete()
        messages.success(request, f"Deleted the auto updater schedule of {provider}.")
        event_log(request.user, "DELETE_AUTO_UPDATER", result=f"deleted {provider}")
        return _redirect_list()
    if schedule is not None and action == "run":
        _report_run(request, au.run_schedule(schedule, user=request.user))
        return _redirect_edit(provider)
    if schedule is not None and action == "run_range":
        result = au.queue_whole_range(schedule, user=request.user)
        if result.queued:
            messages.success(
                request, f"Queued all {result.queued} galleries in the schedule's range for update (task #{result.task_id})."
            )
        else:
            messages.info(request, "No galleries in the schedule's range.")
        event_log(request.user, "RUN_AUTO_UPDATER", data=provider, result=f"whole range, queued {result.queued}")
        return _redirect_edit(provider)

    instance = schedule or AutoUpdaterSchedule(provider=provider)
    if schedule is None and request.method != "POST":
        legacy = next((s for s in au.legacy_schedules() if s["provider"] == provider), None)
        instance.rules = legacy["rules"] if legacy else au.PRESETS[0]["rules"]
    form = AutoUpdaterScheduleForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        is_new = instance.pk is None
        saved = form.save(commit=False)
        if is_new:
            saved.user = typing.cast(typing.Any, request.user)
        saved.save()
        messages.success(request, f"{'Created' if is_new else 'Saved'} the auto updater schedule of {provider}.")
        event_log(request.user, "ADD_AUTO_UPDATER" if is_new else "CHANGE_AUTO_UPDATER", result=provider)
        return _redirect_edit(provider)

    if form.is_bound:
        try:
            rules = json.loads(form.data.get("rules") or "[]")
        except ValueError:
            rules = []
    else:
        rules = instance.rules or []
    d: dict[str, typing.Any] = {
        "provider": provider,
        "schedule": schedule,
        "form": form,
        "editor": _editor_data(provider, rules),
        "scheduler": _scheduler_info(),
        "known": provider in crawler_settings.providers,
    }
    if schedule is not None:
        d["schedule_json"] = json.dumps(au.export_data([schedule]), indent=2, ensure_ascii=False)
        d["next_run"] = schedule.next_run_at()
    return render(request, "viewer/auto_updater_form.html", d)


@staff_member_required(login_url="viewer:login")
def auto_updater_preview(request: HttpRequest, provider: str) -> JsonResponse:
    """How many galleries the rules being edited cover now, and would queue on a run now."""
    try:
        rules = au.parse_rules(request.GET.get("rules", "[]"))
    except au.ScheduleError as e:
        return JsonResponse({"error": str(e)}, status=400)
    ages = au.update_ages(rules)
    now = django_tz.now()
    d: dict[str, typing.Any] = {"updates": len(ages), "in_range": 0, "next_run": None}
    if ages:
        d["in_range"] = Gallery.objects.eligible_for_use(
            provider=provider, posted__gte=now - ages[-1], posted__lte=now - ages[0]
        ).count()
        d["oldest_posted"] = (now - ages[-1]).isoformat()
        d["newest_posted"] = (now - ages[0]).isoformat()
        schedule = AutoUpdaterSchedule.objects.filter(provider=provider).first()
        if schedule is not None:
            start = au.window_start(schedule, now)
            d["next_run"] = {
                "since": start.isoformat(),
                "galleries": au.galleries_reaching(provider, ages, start, now).count(),
            }
    return JsonResponse(d)
