"""Emails the admin report to the staff users who subscribed to it, each on their own schedule.

``send_due_admin_reports`` is called periodically by the ``TimedAdminReporter`` scheduler. For each due subscription it
first claims the run by moving ``next_send_at`` forward with a conditional UPDATE, so two worker processes never send
the same report, then builds and sends the report covering the time since that user's last report.
"""

import dataclasses
import datetime
import logging
from typing import Any
from urllib.parse import urlencode, urljoin

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db.models import QuerySet
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from viewer.models import AdminReportSubscription
from viewer.services.admin_report import (
    ATTENTION_ERROR,
    ATTENTION_WARNING,
    MAX_WINDOW,
    SECTION_KEYS,
    ReportOptions,
    build_admin_report,
)

logger = logging.getLogger(__name__)

RESULT_SENT = "sent"
RESULT_SKIPPED = "skipped"
RESULT_NOT_SENT = "not_sent"
RESULT_FAILED = "failed"


@dataclasses.dataclass
class DeliveryResult:
    status: str
    message: str


def due_subscriptions(now: datetime.datetime) -> QuerySet[AdminReportSubscription]:
    return (
        AdminReportSubscription.objects.filter(enabled=True, next_send_at__lte=now)
        .select_related("user")
        .order_by("next_send_at")
    )


def claim_subscription(subscription: AdminReportSubscription, now: datetime.datetime) -> bool:
    """Move the subscription's schedule forward, unless another process already did. True when this call did it."""
    next_send_at = subscription.advance_schedule(now)
    claimed = AdminReportSubscription.objects.filter(
        pk=subscription.pk, enabled=True, next_send_at=subscription.next_send_at
    ).update(next_send_at=next_send_at, last_run_at=now)
    if claimed:
        subscription.next_send_at = next_send_at
        subscription.last_run_at = now
    return bool(claimed)


def subscription_report_options(subscription: AdminReportSubscription, now: datetime.datetime) -> ReportOptions:
    since, until = subscription.report_window(now, MAX_WINDOW)
    sections = tuple(key for key in (subscription.sections or []) if key in SECTION_KEYS) or None
    return ReportOptions(
        since=since,
        until=until,
        sections=sections,
        min_log_level=subscription.min_log_level,
        top_n=subscription.top_n,
    )


def mail_unavailable_reason(crawler_settings: Any) -> str:
    """Why emails can't be sent with the current configuration, or an empty string when they can."""
    mail_settings = getattr(crawler_settings, "mail_logging", None)
    if mail_settings is None or not mail_settings.enable:
        return "mail_logging is not enabled in the settings"
    if not mail_settings.mailhost:
        return "mail_logging has no mailhost"
    return ""


def recipient_problem(subscription: AdminReportSubscription) -> str:
    user = subscription.user
    if not user.is_active:
        return "the user is inactive"
    if not user.is_staff:
        return "the user is not staff"
    if not user.email:
        return "the user has no email address"
    return ""


def _attention_counts(report: dict[str, Any]) -> tuple[int, int]:
    severities = [item["severity"] for item in report["attention"]]
    return severities.count(ATTENTION_ERROR), severities.count(ATTENTION_WARNING)


def report_page_url(report: dict[str, Any], crawler_settings: Any) -> str:
    """Absolute link to the same report on the site, or an empty string without a configured site URL."""
    base_url = getattr(getattr(crawler_settings, "urls", None), "main_webserver_url", "") or ""
    if not base_url:
        return ""
    query = urlencode(
        {
            "window": "custom",
            "since": report["since"].isoformat(timespec="seconds"),
            "until": report["until"].isoformat(timespec="seconds"),
        }
    )
    return urljoin(base_url, reverse("viewer:admin-report")) + "?" + query


def render_report_email(
    report: dict[str, Any], subscription: AdminReportSubscription, crawler_settings: Any
) -> tuple[str, str, str]:
    """Subject, plain text body and HTML body, with dates in the subscriber's time zone."""
    errors, warnings = _attention_counts(report)
    if errors or warnings:
        parts = []
        if errors:
            parts.append(f"{errors} error{'s' if errors != 1 else ''}")
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        summary = ", ".join(parts)
    else:
        summary = "nothing needs attention"

    with timezone.override(subscription.zone):
        until_local = timezone.localtime(report["until"])
        subject = f"Admin report {until_local:%Y-%m-%d %H:%M}: {summary}"
        context = {
            "report": report,
            "s": report["sections"],
            "summary": summary,
            "report_url": report_page_url(report, crawler_settings),
            "subscription": subscription,
            "time_zone": subscription.time_zone,
        }
        text_body = render_to_string("viewer/admin_report_email.txt", context)
        html_body = render_to_string("viewer/admin_report_email.html", context)
    return subject, text_body, html_body


def deliver_report(
    subscription: AdminReportSubscription,
    now: datetime.datetime,
    crawler_settings: Any = None,
) -> DeliveryResult:
    """Build and email one report, and record the outcome on the subscription. The run must be claimed first."""
    if crawler_settings is None:
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)

    problem = recipient_problem(subscription) or mail_unavailable_reason(crawler_settings)
    if problem:
        result = DeliveryResult(RESULT_NOT_SENT, f"Not sent: {problem}.")
    else:
        try:
            options = subscription_report_options(subscription, now)
            report = build_admin_report(options, crawler_settings)
            if subscription.skip_if_quiet and not report["attention"]:
                result = DeliveryResult(RESULT_SKIPPED, "Skipped: nothing needed attention.")
            else:
                subject, text_body, html_body = render_report_email(report, subscription, crawler_settings)
                message = EmailMultiAlternatives(
                    subject,
                    text_body,
                    crawler_settings.mail_logging.from_ or None,
                    [subscription.user.email],
                )
                message.attach_alternative(html_body, "text/html")
                message.send(fail_silently=False)
                subscription.last_sent_at = options.until
                result = DeliveryResult(RESULT_SENT, f"Sent to {subscription.user.email}.")
        except Exception as e:
            logger.error("Admin report for user %s could not be sent: %s", subscription.user, e, exc_info=True)
            result = DeliveryResult(RESULT_FAILED, f"Failed: {type(e).__name__}: {e}")

    if result.status == RESULT_NOT_SENT:
        logger.warning("Admin report for user %s: %s", subscription.user, result.message)

    subscription.last_result = result.message[:500]
    AdminReportSubscription.objects.filter(pk=subscription.pk).update(
        last_sent_at=subscription.last_sent_at, last_result=subscription.last_result
    )
    return result


def send_due_admin_reports(crawler_settings: Any = None, now: datetime.datetime | None = None) -> list[DeliveryResult]:
    """Send every report that is due. Returns the outcome of each report this call claimed."""
    now = now or timezone.now()
    results = []
    for subscription in due_subscriptions(now):
        if not claim_subscription(subscription, now):
            continue
        results.append(deliver_report(subscription, now, crawler_settings))
    return results


def schedule_subscription(subscription: AdminReportSubscription, now: datetime.datetime | None = None) -> None:
    """Set ``next_send_at`` from the subscription's current schedule (after it's created or edited)."""
    now = now or timezone.now()
    subscription.next_send_at = subscription.compute_next_send(now) if subscription.enabled else None
