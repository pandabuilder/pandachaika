import datetime
import threading
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core import mail
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.base.setup import Settings
from core.workers.admin_report import TimedAdminReporter
from viewer.models import AdminReportSubscription
from viewer.services import admin_report_delivery
from viewer.services.admin_report_delivery import (
    RESULT_FAILED,
    RESULT_NOT_SENT,
    RESULT_SENT,
    RESULT_SKIPPED,
    claim_subscription,
    send_due_admin_reports,
)
from workers.models import SchedulerState

UTC = datetime.UTC
MADRID = ZoneInfo("Europe/Madrid")


def _mail_settings(base_url: str = "https://panda.example/") -> Settings:
    crawler_settings = Settings(load_from_disk=False)
    crawler_settings.mail_logging.enable = True
    crawler_settings.mail_logging.mailhost = "smtp.example"
    crawler_settings.mail_logging.from_ = "panda@example.com"
    crawler_settings.urls.main_webserver_url = base_url
    return crawler_settings


class SubscriptionScheduleTests(TestCase):
    def _subscription(self, **kwargs) -> AdminReportSubscription:
        return AdminReportSubscription(time_zone="Europe/Madrid", **kwargs)

    def test_daily_is_the_next_send_time_in_the_subscription_time_zone(self):
        subscription = self._subscription(frequency="daily", send_time=datetime.time(8, 0))

        before = datetime.datetime(2026, 11, 14, 6, 0, tzinfo=UTC)  # 07:00 in Madrid
        self.assertEqual(subscription.compute_next_send(before), datetime.datetime(2026, 11, 14, 8, 0, tzinfo=MADRID))

        at_send_time = datetime.datetime(2026, 11, 14, 8, 0, tzinfo=MADRID)
        self.assertEqual(
            subscription.compute_next_send(at_send_time), datetime.datetime(2026, 11, 15, 8, 0, tzinfo=MADRID)
        )

    def test_daily_keeps_the_local_time_across_dst(self):
        subscription = self._subscription(frequency="daily", send_time=datetime.time(8, 0))

        # Madrid leaves summer time on 2026-10-25.
        next_send = subscription.compute_next_send(datetime.datetime(2026, 10, 24, 9, 0, tzinfo=MADRID))

        self.assertEqual(next_send.astimezone(MADRID).hour, 8)
        self.assertEqual(next_send.astimezone(UTC).hour, 7)

    def test_weekly_picks_the_configured_weekday(self):
        subscription = self._subscription(frequency="weekly", send_time=datetime.time(9, 30), weekday=4)

        # 2026-11-14 is a Saturday: next Friday is the 20th.
        next_send = subscription.compute_next_send(datetime.datetime(2026, 11, 14, 12, 0, tzinfo=MADRID))

        self.assertEqual(next_send, datetime.datetime(2026, 11, 20, 9, 30, tzinfo=MADRID))

    def test_hourly_advance_keeps_the_rhythm(self):
        subscription = self._subscription(
            frequency="hourly", interval_hours=2, next_send_at=datetime.datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
        )

        next_send = subscription.advance_schedule(datetime.datetime(2026, 11, 14, 13, 30, tzinfo=UTC))

        self.assertEqual(next_send, datetime.datetime(2026, 11, 14, 14, 0, tzinfo=UTC))

    def test_report_window(self):
        now = datetime.datetime(2026, 11, 14, 8, 0, tzinfo=UTC)
        max_window = datetime.timedelta(days=30)

        first = self._subscription(frequency="weekly")
        self.assertEqual(first.report_window(now, max_window), (now - datetime.timedelta(days=7), now))

        later = self._subscription(frequency="daily", last_sent_at=now - datetime.timedelta(days=3))
        self.assertEqual(later.report_window(now, max_window), (now - datetime.timedelta(days=3), now))

        long_ago = self._subscription(frequency="daily", last_sent_at=now - datetime.timedelta(days=90))
        self.assertEqual(long_ago.report_window(now, max_window), (now - max_window, now))


class DeliveryTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.staff = User.objects.create_user(username="staff", email="staff@example.com", is_staff=True)
        self.subscription = AdminReportSubscription.objects.create(
            user=self.staff,
            enabled=True,
            frequency="daily",
            send_time=datetime.time(8, 0),
            time_zone="UTC",
            next_send_at=self.now - datetime.timedelta(minutes=1),
        )
        self.crawler_settings = _mail_settings()

    def test_sends_due_report_and_moves_the_schedule(self):
        results = send_due_admin_reports(self.crawler_settings, now=self.now)

        self.assertEqual([r.status for r in results], [RESULT_SENT])
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["staff@example.com"])
        self.assertEqual(message.from_email, "panda@example.com")
        # No worker process exists in the test database, which the report flags.
        self.assertIn("Admin report", message.subject)
        self.assertIn("1 error", message.subject)
        self.assertIn("No worker process is running", message.body)
        self.assertIn("https://panda.example/admin-report/?window=custom", message.body)
        html, mimetype = message.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("No worker process is running", html)

        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_sent_at, self.now)
        self.assertGreater(self.subscription.next_send_at, self.now)
        self.assertEqual(self.subscription.last_run_at, self.now)
        self.assertEqual(self.subscription.last_result, "Sent to staff@example.com.")

        # Not due anymore.
        self.assertEqual(send_due_admin_reports(self.crawler_settings, now=self.now), [])
        self.assertEqual(len(mail.outbox), 1)

    def test_report_covers_the_time_since_the_last_one(self):
        last_sent = self.now - datetime.timedelta(days=3)
        AdminReportSubscription.objects.filter(pk=self.subscription.pk).update(last_sent_at=last_sent)

        with mock.patch.object(
            admin_report_delivery, "build_admin_report", wraps=admin_report_delivery.build_admin_report
        ) as build:
            send_due_admin_reports(self.crawler_settings, now=self.now)

        options = build.call_args.args[0]
        self.assertEqual((options.since, options.until), (last_sent, self.now))

    def test_a_run_claimed_by_another_process_is_not_sent_twice(self):
        stale = AdminReportSubscription.objects.get(pk=self.subscription.pk)
        self.assertTrue(claim_subscription(self.subscription, self.now))

        self.assertFalse(claim_subscription(stale, self.now))

    def test_not_sent_to_non_staff_or_without_mail(self):
        User.objects.filter(pk=self.staff.pk).update(is_staff=False)

        results = send_due_admin_reports(self.crawler_settings, now=self.now)

        self.assertEqual([r.status for r in results], [RESULT_NOT_SENT])
        self.assertEqual(mail.outbox, [])
        self.subscription.refresh_from_db()
        self.assertIn("not staff", self.subscription.last_result)
        self.assertIsNone(self.subscription.last_sent_at)
        self.assertGreater(self.subscription.next_send_at, self.now)

        User.objects.filter(pk=self.staff.pk).update(is_staff=True)
        AdminReportSubscription.objects.filter(pk=self.subscription.pk).update(next_send_at=self.now)
        self.crawler_settings.mail_logging.enable = False

        results = send_due_admin_reports(self.crawler_settings, now=self.now)

        self.assertEqual([r.status for r in results], [RESULT_NOT_SENT])
        self.assertIn("mail_logging is not enabled", results[0].message)

    def test_skip_if_quiet(self):
        AdminReportSubscription.objects.filter(pk=self.subscription.pk).update(
            skip_if_quiet=True, sections=["content"]
        )

        results = send_due_admin_reports(self.crawler_settings, now=self.now)

        self.assertEqual([r.status for r in results], [RESULT_SKIPPED])
        self.assertEqual(mail.outbox, [])
        self.subscription.refresh_from_db()
        # The next report covers the skipped period too.
        self.assertIsNone(self.subscription.last_sent_at)

    def test_send_failure_is_recorded(self):
        with mock.patch(
            "viewer.services.admin_report_delivery.EmailMultiAlternatives.send", side_effect=OSError("SMTP down")
        ):
            results = send_due_admin_reports(self.crawler_settings, now=self.now)

        self.assertEqual([r.status for r in results], [RESULT_FAILED])
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.last_result, "Failed: OSError: SMTP down")
        self.assertIsNone(self.subscription.last_sent_at)

    def test_email_dates_use_the_subscriber_time_zone(self):
        AdminReportSubscription.objects.filter(pk=self.subscription.pk).update(time_zone="Asia/Tokyo")

        send_due_admin_reports(self.crawler_settings, now=self.now)

        until_tokyo = timezone.localtime(self.now, ZoneInfo("Asia/Tokyo"))
        self.assertIn(f"{until_tokyo:%Y-%m-%d %H:%M}", mail.outbox[0].subject)
        self.assertIn("(Asia/Tokyo)", mail.outbox[0].body)


class TimedAdminReporterTests(TestCase):
    def test_job_sends_due_reports_and_records_the_run(self):
        reporter = TimedAdminReporter(Settings(load_from_disk=False), timer=15)
        self.assertEqual(reporter.timer, 15 * 60)
        SchedulerState.objects.create(name=reporter.thread_name)

        def stop_after_run(*args, **kwargs):
            reporter.stop.set()
            return []

        with mock.patch(
            "viewer.services.admin_report_delivery.send_due_admin_reports", side_effect=stop_after_run
        ) as send, mock.patch("django.db.connections.close_all"):
            thread = threading.Thread(target=reporter.job)
            thread.start()
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        send.assert_called_once_with(reporter.settings)
        self.assertIsNotNone(reporter.last_run)


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class SubscriptionViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="staff", password="pw", email="staff@example.com", is_staff=True
        )
        self.url = reverse("viewer:admin-report-subscription")

    def _form_data(self, **overrides):
        data = {
            "enabled": "on",
            "frequency": "weekly",
            "interval_hours": "6",
            "send_time": "09:15",
            "weekday": "2",
            "time_zone": "Europe/Madrid",
            "sections": ["logs", "health"],
            "min_log_level": "40",
            "top_n": "5",
        }
        data.update(overrides)
        return data

    def test_regular_users_cannot_subscribe(self):
        User.objects.create_user(username="regular", password="pw")
        self.client.login(username="regular", password="pw")

        response = self.client.post(self.url, self._form_data())

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AdminReportSubscription.objects.exists())

    def test_save_creates_and_schedules_the_subscription(self):
        self.client.login(username="staff", password="pw")

        response = self.client.post(self.url, self._form_data())

        self.assertRedirects(response, reverse("viewer:admin-report") + "#report-email", fetch_redirect_response=False)
        subscription = AdminReportSubscription.objects.get(user=self.staff)
        self.assertEqual(subscription.sections, ["logs", "health"])
        self.assertEqual(subscription.min_log_level, 40)
        next_local = subscription.next_send_at.astimezone(MADRID)
        self.assertEqual((next_local.weekday(), next_local.hour, next_local.minute), (2, 9, 15))

        # Disabling clears the schedule.
        self.client.post(self.url, self._form_data(enabled=""))
        subscription.refresh_from_db()
        self.assertFalse(subscription.enabled)
        self.assertIsNone(subscription.next_send_at)

    def test_invalid_time_zone_shows_the_errors(self):
        self.client.login(username="staff", password="pw")

        response = self.client.post(self.url, self._form_data(time_zone="Mars/Olympus"))

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Unknown time zone", status_code=400)
        self.assertFalse(AdminReportSubscription.objects.exists())

    def test_send_now_makes_the_report_due_and_wakes_the_scheduler(self):
        self.client.login(username="staff", password="pw")
        self.client.post(self.url, self._form_data())
        SchedulerState.objects.create(
            name="timed_admin_reporter", is_running=True, last_heartbeat=timezone.now()
        )

        self.client.post(self.url, {"action": "send_now"})

        subscription = AdminReportSubscription.objects.get(user=self.staff)
        self.assertLessEqual(subscription.next_send_at, timezone.now())
        self.assertEqual(SchedulerState.objects.get(name="timed_admin_reporter").command_requested, "force_run")

    def test_since_last_report_window(self):
        self.client.login(username="staff", password="pw")
        report_url = reverse("viewer:admin-report")

        response = self.client.get(report_url, {"window": "last_report", "format": "json"})
        self.assertEqual(response.status_code, 400)

        last_sent = timezone.now() - datetime.timedelta(hours=30)
        AdminReportSubscription.objects.create(user=self.staff, last_sent_at=last_sent)

        response = self.client.get(report_url, {"window": "last_report", "format": "json"})
        self.assertEqual(response.status_code, 200)
        # The JSON encoder keeps milliseconds only.
        since = parse_datetime(response.json()["since"])
        self.assertAlmostEqual(since.timestamp(), last_sent.timestamp(), delta=0.001)

        page = self.client.get(report_url)
        self.assertContains(page, "Since my last report email")
        self.assertContains(page, "My report email")
