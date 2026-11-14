"""Staff report page: activity in a time window plus what currently needs attention. Also served as JSON.

Each staff user can also have the report emailed on their own schedule (``AdminReportSubscription``), configured on the
same page.
"""

import datetime
import typing
from zoneinfo import available_timezones

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_POST

from viewer.forms import AdminReportSubscriptionForm
from viewer.models import AdminReportSubscription, UserLongLivedToken
from viewer.services.admin_report import (
    DEFAULT_TOP_N,
    LOG_LEVEL_CHOICES,
    MAX_TOP_N,
    SECTIONS,
    ReportOptions,
    build_admin_report,
)
from viewer.services.admin_report_delivery import mail_unavailable_reason, schedule_subscription
from viewer.utils.requests import double_check_auth
from viewer.views.head import render_error

# Preset windows, ending now.
WINDOW_PRESETS: dict[str, tuple[str, datetime.timedelta]] = {
    "24h": ("Last 24 hours", datetime.timedelta(hours=24)),
    "7d": ("Last 7 days", datetime.timedelta(days=7)),
    "30d": ("Last 30 days", datetime.timedelta(days=30)),
}
DEFAULT_WINDOW = "24h"
WINDOW_CUSTOM = "custom"
# Since the end of the last report emailed to the current user.
WINDOW_LAST_REPORT = "last_report"

ADMIN_REPORT_SCHEDULER = "timed_admin_reporter"


class ReportParamsError(ValueError):
    pass


def _parse_moment(value: str, field: str) -> datetime.datetime:
    moment = parse_datetime(value)
    if moment is None:
        day = parse_date(value)
        if day is not None:
            moment = datetime.datetime.combine(day, datetime.time.min)
    if moment is None:
        raise ReportParamsError(f"Invalid date for '{field}': {value!r}")
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_current_timezone())
    return moment


def parse_report_params(
    params: typing.Mapping[str, str], last_report_at: datetime.datetime | None = None
) -> tuple[ReportOptions, dict[str, typing.Any]]:
    """Report options from query parameters, and the values to show back in the form.

    window: a preset (24h, 7d, 30d), "last_report" (since ``last_report_at``) or "custom" with since (and optionally
    until, default now). sections: comma separated section keys (all if missing). log_level: WARNING, ERROR or
    CRITICAL. limit: top N.
    """
    now = timezone.now()
    since_param = (params.get("since") or "").strip()
    until_param = (params.get("until") or "").strip()
    window = (params.get("window") or "").strip() or (WINDOW_CUSTOM if since_param else DEFAULT_WINDOW)

    if window == WINDOW_CUSTOM:
        if not since_param:
            raise ReportParamsError("A custom window needs a 'since' date.")
        since = _parse_moment(since_param, "since")
        until = _parse_moment(until_param, "until") if until_param else now
    elif window == WINDOW_LAST_REPORT:
        if last_report_at is None:
            raise ReportParamsError("No report has been emailed to you yet.")
        since, until = last_report_at, now
    elif window in WINDOW_PRESETS:
        until = now
        since = now - WINDOW_PRESETS[window][1]
    else:
        raise ReportParamsError(f"Unknown window: {window!r}")

    sections_param = (params.get("sections") or "").strip()
    sections: tuple[str, ...] | None = None
    if sections_param:
        sections = tuple(key.strip() for key in sections_param.split(",") if key.strip())

    level_names = {name: number for number, name in LOG_LEVEL_CHOICES}
    level_param = (params.get("log_level") or "WARNING").strip().upper()
    if level_param not in level_names:
        raise ReportParamsError(f"Unknown log level: {level_param!r}")

    limit_param = (params.get("limit") or "").strip()
    try:
        top_n = int(limit_param) if limit_param else DEFAULT_TOP_N
    except ValueError:
        raise ReportParamsError(f"Invalid limit: {limit_param!r}")

    try:
        options = ReportOptions(
            since=since,
            until=until,
            sections=sections,
            min_log_level=level_names[level_param],
            top_n=top_n,
        )
    except ValueError as e:
        raise ReportParamsError(str(e))

    form_values = {
        "window": window,
        "since": timezone.localtime(since).strftime("%Y-%m-%dT%H:%M"),
        "until": timezone.localtime(until).strftime("%Y-%m-%dT%H:%M"),
        "sections": list(options.enabled_sections),
        "log_level": level_param,
        "limit": top_n,
    }
    return options, form_values


def _is_staff(user: typing.Any) -> bool:
    return bool(user is not None and user.is_authenticated and user.is_staff)


def admin_report(request: HttpRequest) -> HttpResponse:
    """Admin report. Session or bearer token (read scope) auth, staff only. ``format=json`` returns the raw data."""
    wants_json = request.GET.get("format") == "json"
    _, user = double_check_auth(request, UserLongLivedToken.SCOPE_READ)

    if not _is_staff(user):
        if wants_json:
            return JsonResponse({"error": "You need to be an admin to see the report."}, status=403)
        return render_error(request, "You need to be an admin to see the report.")

    subscription = AdminReportSubscription.objects.filter(user=user).first()
    last_report_at = subscription.last_sent_at if subscription else None

    # A section list coming from the form arrives as repeated keys, not comma separated.
    params = request.GET.dict()
    if len(request.GET.getlist("sections")) > 1:
        params["sections"] = ",".join(request.GET.getlist("sections"))

    try:
        options, form_values = parse_report_params(params, last_report_at)
    except ReportParamsError as e:
        if wants_json:
            return JsonResponse({"error": str(e)}, status=400)
        # Show the default report with the error, instead of bouncing back to wherever the user came from.
        messages.error(request, str(e))
        options, form_values = parse_report_params({})

    report = build_admin_report(options)

    if wants_json:
        return JsonResponse(report, encoder=DjangoJSONEncoder, json_dumps_params={"ensure_ascii": False})

    return _render_report_page(request, report, form_values, subscription)


def _render_report_page(
    request: HttpRequest,
    report: dict[str, typing.Any],
    form_values: dict[str, typing.Any],
    subscription: AdminReportSubscription | None,
    subscription_form: AdminReportSubscriptionForm | None = None,
    status: int = 200,
) -> HttpResponse:
    from workers.models import SchedulerState

    if subscription_form is None:
        subscription_form = AdminReportSubscriptionForm(
            instance=subscription or AdminReportSubscription(user=typing.cast(User, request.user))
        )

    window_presets = [(key, label) for key, (label, _) in WINDOW_PRESETS.items()]
    if subscription and subscription.last_sent_at:
        window_presets.append((WINDOW_LAST_REPORT, "Since my last report email"))

    crawler_settings = settings.CRAWLER_SETTINGS
    context = {
        "page_title": "Admin report",
        "report": report,
        "form_values": form_values,
        "window_presets": window_presets,
        "section_choices": SECTIONS,
        "log_level_choices": [name for _, name in LOG_LEVEL_CHOICES],
        "max_limit": MAX_TOP_N,
        "json_query": _json_query(request),
        "subscription": subscription,
        "subscription_form": subscription_form,
        "subscription_open": bool(subscription_form.errors),
        "time_zones": sorted(available_timezones()),
        "mail_problem": mail_unavailable_reason(crawler_settings),
        "scheduler_enabled": crawler_settings.admin_report.enable,
        "scheduler_running": SchedulerState.is_scheduler_running(ADMIN_REPORT_SCHEDULER),
    }
    return render(request, "viewer/admin_report.html", context, status=status)


def _json_query(request: HttpRequest) -> str:
    query = request.GET.copy()
    query["format"] = "json"
    return query.urlencode()


def _report_url(anchor: str = "report-email") -> str:
    return reverse("viewer:admin-report") + f"#{anchor}"


@require_POST
def admin_report_subscription(request: HttpRequest) -> HttpResponse:
    """Save the current staff user's report email settings, or ask for their report to be sent now."""
    if not _is_staff(request.user):
        return render_error(request, "You need to be an admin to receive the report.")

    user = typing.cast(User, request.user)
    subscription = AdminReportSubscription.objects.filter(user=user).first()

    if request.POST.get("action") == "send_now":
        return _send_now(request, subscription)

    form = AdminReportSubscriptionForm(
        request.POST,
        instance=subscription or AdminReportSubscription(user=user),
    )
    if not form.is_valid():
        messages.error(request, "The report email settings were not saved, check the errors below.")
        options, form_values = parse_report_params({})
        report = build_admin_report(options)
        return _render_report_page(request, report, form_values, subscription, form, status=400)

    subscription = form.save(commit=False)
    schedule_subscription(subscription)
    subscription.save()

    if subscription.enabled and subscription.next_send_at:
        next_local = timezone.localtime(subscription.next_send_at, subscription.zone)
        messages.success(
            request,
            f"Report email settings saved. Next report: {next_local:%Y-%m-%d %H:%M} ({subscription.time_zone}).",
        )
    else:
        messages.success(request, "Report email settings saved. The report email is disabled.")
    return HttpResponseRedirect(_report_url())


def _send_now(request: HttpRequest, subscription: AdminReportSubscription | None) -> HttpResponse:
    """Make the user's report due now and wake the scheduler up.

    Daily and weekly schedules continue as before afterwards; an "every N hours" schedule restarts from now.
    """
    from workers.models import SchedulerState

    if subscription is None or not subscription.enabled:
        messages.error(request, "Enable and save the report email first.")
        return HttpResponseRedirect(_report_url())

    AdminReportSubscription.objects.filter(pk=subscription.pk).update(next_send_at=timezone.now())

    if SchedulerState.is_scheduler_running(ADMIN_REPORT_SCHEDULER):
        SchedulerState.request_command(ADMIN_REPORT_SCHEDULER, "force_run")
        messages.success(request, "Your report will be sent in a moment.")
    else:
        messages.warning(
            request,
            "Your report is due, but the admin report scheduler is not running: it will be sent when it starts "
            "(enable admin_report in the settings, or start it from the workers dashboard).",
        )
    return HttpResponseRedirect(_report_url())
