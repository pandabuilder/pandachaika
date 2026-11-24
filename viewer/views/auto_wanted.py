"""Auto wanted sources: the provider queries that create wanted galleries for new releases."""

import datetime
import json
import logging
import typing

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count
from django.forms.models import model_to_dict
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

import django.utils.timezone as django_tz

from core.workers.auto_wanted import TimedAutoWanted
from viewer.forms import AutoWantedSourceForm
from viewer.models import AutoWantedSource
from viewer.services import auto_wanted as aw
from viewer.utils.actions import event_log

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)

RECENT_WANTED_LIMIT = 30


def _json_download(data: typing.Any, filename: str) -> HttpResponse:
    response = HttpResponse(json.dumps(data, indent=2, ensure_ascii=False), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _scheduler_info() -> dict[str, typing.Any]:
    info: dict[str, typing.Any] = {
        "enable": crawler_settings.auto_wanted.enable,
        "startup": crawler_settings.auto_wanted.startup,
        "cycle_timer": crawler_settings.auto_wanted.cycle_timer,
        "state": None,
    }
    try:
        from workers.models import SchedulerState

        info["state"] = SchedulerState.objects.filter(name="auto_wanted").first()
    except Exception:
        pass
    return info


def _default_period(scheduler: dict[str, typing.Any]) -> datetime.timedelta:
    """The running scheduler's cycle (the workers dashboard can change it), else the setting."""
    state = scheduler.get("state")
    if state is not None and state.cycle_timer and state.cycle_timer > 0:
        return datetime.timedelta(seconds=state.cycle_timer)
    return aw.default_period(crawler_settings)


def _hours(period: datetime.timedelta) -> str:
    hours = period.total_seconds() / 3600
    return f"{hours:g} h"


def _redirect_list() -> HttpResponseRedirect:
    return HttpResponseRedirect(reverse("viewer:auto-wanted"))


def _report_import(request: HttpRequest, result: aw.ImportResult, what: str) -> None:
    if result.created or result.updated:
        messages.success(request, f"Imported {what}: {result.created} created, {result.updated} updated.")
        event_log(request.user, "IMPORT_AUTO_WANTED", result=f"{result.created} created, {result.updated} updated")
    if result.skipped:
        messages.info(request, f"Already there, not changed: {', '.join(result.skipped)}.")
    for error in result.errors:
        messages.error(request, f"Not imported: {error}", extra_tags="danger")
    if not (result.created or result.updated or result.skipped or result.errors):
        messages.warning(request, "Nothing to import.")


def _queue_run(request: HttpRequest, sources: list[AutoWantedSource]) -> None:
    if not sources:
        messages.warning(request, "No sources to run.")
        return
    task = aw.enqueue_run(sources, user=request.user)
    names = ", ".join(s.name for s in sources)
    messages.success(
        request,
        f"Queued an auto wanted run (task #{getattr(task, 'pk', '?')}) for: {names}. The last run columns update "
        f"when it finishes.",
    )
    event_log(request.user, "RUN_AUTO_WANTED", data=names, result="queued")


# --- Overview ---


@staff_member_required(login_url="viewer:login")
def auto_wanted_list(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        return _list_action(request)

    sources = list(AutoWantedSource.objects.annotate(wanted_count=Count("wanted_galleries")))
    scheduler = _scheduler_info()
    default = _default_period(scheduler)
    now = django_tz.now()
    for source in sources:
        source.next_run = source.next_run_at(default)  # type: ignore[attr-defined]
        source.is_due = source.active and aw.is_due(source, default, now)  # type: ignore[attr-defined]
    if request.GET.get("export") == "1":
        return _json_download(aw.export_data(sources), "auto-wanted.json")
    if request.GET.get("legacy") == "1":
        return _json_download(
            {"version": aw.EXPORT_VERSION, "sources": aw.legacy_sources()}, "auto-wanted-legacy.json"
        )
    if request.GET.get("format") == "json":
        return JsonResponse(
            {
                "sources": [
                    {
                        "id": s.pk,
                        **aw.source_to_dict(s),
                        "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
                        "next_run_at": s.next_run.isoformat() if s.next_run else None,  # type: ignore[attr-defined]
                        "last_run_status": s.last_run_status,
                        "last_run_message": s.last_run_message,
                        "last_run_links": s.last_run_links,
                        "last_run_created": s.last_run_created,
                        "last_run_mentions": s.last_run_mentions,
                        "wanted_count": s.wanted_count,
                    }
                    for s in sources
                ]
            }
        )

    providers = aw.wanted_providers()
    d: dict[str, typing.Any] = {
        "sources": sources,
        "active_count": sum(1 for s in sources if s.active),
        "failing_count": sum(1 for s in sources if s.active and s.last_run_status == AutoWantedSource.RunStatus.FAILED),
        "created_total": sum(s.last_run_created for s in sources),
        "providers": providers,
        "unknown_provider_ids": {s.pk for s in sources if s.provider not in providers},
        "legacy": aw.legacy_summary(),
        "scheduler": scheduler,
        "default_period": _hours(default),
        "check_interval_minutes": TimedAutoWanted.CHECK_INTERVAL // 60,
        "providers_setting": crawler_settings.auto_wanted.providers,
    }
    return render(request, "viewer/auto_wanted_list.html", d)


def _list_action(request: HttpRequest) -> HttpResponse:
    p = request.POST
    action = p.get("action", "")
    if action == "import":
        raw = p.get("data", "")
        upload = request.FILES.get("file")
        if upload is not None:
            raw = upload.read().decode("utf-8", errors="replace")
        try:
            items = aw.parse_import(raw)
        except (ValueError, TypeError) as e:
            messages.error(request, f"Could not read the sources to import: {e}", extra_tags="danger")
            return _redirect_list()
        _report_import(request, aw.import_sources(items, user=request.user), "auto wanted sources")
    elif action == "import_legacy":
        _report_import(
            request,
            aw.import_sources(aw.legacy_sources(), user=request.user, skip_existing=True),
            "the provider attribute queries",
        )
    elif action == "delete_legacy":
        deleted, _ = aw.legacy_attributes().delete()
        messages.success(request, f"Deleted {deleted} old auto wanted provider attributes.")
        event_log(request.user, "DELETE_AUTO_WANTED_LEGACY", result=f"deleted {deleted}")
    elif action in ("run", "run_active"):
        if action == "run_active":
            sources = list(AutoWantedSource.objects.filter(active=True))
        else:
            pks = [int(pk) for pk in p.getlist("source") if pk.isdigit()]
            sources = list(AutoWantedSource.objects.filter(pk__in=pks))
        _queue_run(request, sources)
    elif action in ("activate", "deactivate"):
        pks = [int(pk) for pk in p.getlist("source") if pk.isdigit()]
        changed = AutoWantedSource.objects.filter(pk__in=pks).update(active=action == "activate")
        messages.success(request, f"{'Activated' if action == 'activate' else 'Deactivated'} {changed} source(s).")
    else:
        messages.error(request, f"Unknown action '{action}'.", extra_tags="danger")
    return _redirect_list()


# --- Create / edit ---


def _form_context(form: AutoWantedSourceForm, source: AutoWantedSource | None) -> dict[str, typing.Any]:
    d: dict[str, typing.Any] = {
        "form": form,
        "source": source,
        "default_unwanted_title": crawler_settings.auto_wanted.unwanted_title,
        "default_period": _hours(_default_period(_scheduler_info())),
    }
    if source is not None:
        d["recent_wanted"] = list(
            source.wanted_galleries.order_by("-create_date")[:RECENT_WANTED_LIMIT].only(
                "id", "title", "title_jpn", "create_date", "found", "release_date"
            )
        )
        d["wanted_count"] = source.wanted_galleries.count()
        d["source_json"] = json.dumps(aw.export_data([source]), indent=2, ensure_ascii=False)
    return d


@staff_member_required(login_url="viewer:login")
def auto_wanted_create(request: HttpRequest) -> HttpResponse:
    initial: dict[str, typing.Any] = {}
    copy_of = None
    if request.method != "POST" and request.GET.get("copy", "").isdigit():
        copy_of = AutoWantedSource.objects.filter(pk=int(request.GET["copy"])).first()
    if copy_of is not None:
        initial = model_to_dict(copy_of, fields=aw.EXPORT_FIELDS)
        initial["name"] = f"{copy_of.name} (copy)"
        for name, value in (copy_of.params or {}).items():
            initial[AutoWantedSourceForm.param_field_name(copy_of.provider, name)] = value
    else:
        initial = {"unwanted_title": crawler_settings.auto_wanted.unwanted_title}
    form = AutoWantedSourceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        source = form.save(commit=False)
        source.user = typing.cast(typing.Any, request.user)
        source.save()
        messages.success(request, f"Created auto wanted source '{source.name}'.")
        event_log(request.user, "ADD_AUTO_WANTED", content_object=source, result="created")
        if request.POST.get("then") == "run":
            _queue_run(request, [source])
        return HttpResponseRedirect(reverse("viewer:auto-wanted-edit", args=(source.pk,)))
    return render(request, "viewer/auto_wanted_form.html", _form_context(form, None))


@staff_member_required(login_url="viewer:login")
def auto_wanted_edit(request: HttpRequest, pk: int) -> HttpResponse:
    source = get_object_or_404(AutoWantedSource, pk=pk)
    action = request.POST.get("action", "") if request.method == "POST" else ""
    if action == "delete":
        name = source.name
        source.delete()
        messages.success(request, f"Deleted auto wanted source '{name}'. Its wanted galleries are kept.")
        event_log(request.user, "DELETE_AUTO_WANTED", result=f"deleted {name}")
        return _redirect_list()
    if action == "run":
        _queue_run(request, [source])
        return HttpResponseRedirect(reverse("viewer:auto-wanted-edit", args=(source.pk,)))
    form = AutoWantedSourceForm(request.POST or None, instance=source)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Saved auto wanted source '{source.name}'.")
        event_log(request.user, "CHANGE_AUTO_WANTED", content_object=source, result="changed")
        if request.POST.get("then") == "run":
            _queue_run(request, [source])
        return HttpResponseRedirect(reverse("viewer:auto-wanted-edit", args=(source.pk,)))
    return render(request, "viewer/auto_wanted_form.html", _form_context(form, source))
