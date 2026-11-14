import datetime
import logging
import time
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_db_logger.models import StatusLog

from viewer.models import Archive, DownloadEvent, Gallery, UserLongLivedToken
from viewer.services import admin_report
from viewer.services.admin_report import (
    SECTION_CONTENT,
    SECTION_DOWNLOADS,
    SECTION_HEALTH,
    SECTION_LOGS,
    ReportOptions,
    build_admin_report,
    normalize_log_message,
)
from viewer.tests.factories import ArchiveFactory, GalleryFactory
from viewer.views.admin_report import ReportParamsError, parse_report_params
from workers.models import ProviderRateLimit, SchedulerState, TaskQueue, WorkerProcess


def _options(hours: int = 24, **kwargs) -> ReportOptions:
    now = timezone.now()
    return ReportOptions(since=now - datetime.timedelta(hours=hours), until=now, **kwargs)


def _log(
    level: int, msg: str, age: datetime.timedelta = datetime.timedelta(minutes=5), logger_name: str = "core"
) -> None:
    entry = StatusLog.objects.create(logger_name=logger_name, level=level, msg=msg)
    StatusLog.objects.filter(pk=entry.pk).update(create_datetime=timezone.now() - age)


class ReportOptionsTests(TestCase):
    def test_rejects_inverted_window(self):
        now = timezone.now()
        with self.assertRaises(ValueError):
            ReportOptions(since=now, until=now - datetime.timedelta(hours=1))

    def test_rejects_too_long_window(self):
        with self.assertRaises(ValueError):
            _options(hours=24 * 400)

    def test_rejects_unknown_section(self):
        with self.assertRaises(ValueError):
            _options(sections=("content", "nope"))

    def test_enabled_sections_keep_canonical_order(self):
        options = _options(sections=(SECTION_LOGS, SECTION_CONTENT))
        self.assertEqual(options.enabled_sections, (SECTION_CONTENT, SECTION_LOGS))


class ContentSectionTests(TestCase):
    def test_counts_only_items_created_in_window(self):
        recent_gallery = GalleryFactory(provider="panda")
        old_gallery = GalleryFactory(provider="panda")
        GalleryFactory(provider="fakku")
        GalleryFactory(provider="panda", status=Gallery.StatusChoices.NO_METADATA)
        Gallery.objects.filter(pk=old_gallery.pk).update(create_date=timezone.now() - datetime.timedelta(days=3))

        ArchiveFactory(gallery=recent_gallery, filesize=100, filecount=3, public=True)
        ArchiveFactory(gallery=recent_gallery, filesize=50, filecount=2, origin=Archive.ORIGIN_WANTED_GALLERY)
        old_archive = ArchiveFactory(gallery=old_gallery, filesize=1000)
        Archive.objects.filter(pk=old_archive.pk).update(create_date=timezone.now() - datetime.timedelta(days=3))

        content = build_admin_report(_options(sections=(SECTION_CONTENT,)))["sections"][SECTION_CONTENT]

        self.assertEqual(content["archives_added"], 2)
        self.assertEqual(content["archives_added_public"], 1)
        self.assertEqual(content["archives_added_size"], 150)
        self.assertEqual(content["archives_added_files"], 5)
        self.assertIn({"key": "Wanted Gallery", "count": 1}, content["archives_by_origin"])
        # The "no metadata" gallery is counted apart.
        self.assertEqual(content["galleries_added"], 2)
        self.assertEqual(content["galleries_added_no_metadata"], 1)
        self.assertEqual(
            content["galleries_by_provider"], [{"key": "fakku", "count": 1}, {"key": "panda", "count": 1}]
        )


class LogsSectionTests(TestCase):
    def test_normalize_log_message_ignores_numbers_and_ids(self):
        self.assertEqual(
            normalize_log_message("Archive 123 failed: hash deadbeef01 not found\nTraceback..."),
            normalize_log_message("Archive 9 failed: hash 0123abcdef not found"),
        )

    def test_filters_by_level_and_window_and_groups_similar_messages(self):
        _log(logging.ERROR, "Archive 1 could not be extracted")
        _log(logging.ERROR, "Archive 2 could not be extracted")
        _log(logging.ERROR, "Archive 2 could not be extracted")
        _log(logging.WARNING, "Slow response from provider")
        _log(logging.INFO, "Just info")
        _log(logging.ERROR, "Too old", age=datetime.timedelta(days=2))

        logs = build_admin_report(_options(sections=(SECTION_LOGS,)))["sections"][SECTION_LOGS]

        self.assertEqual(logs["by_level"], {"ERROR": 3, "WARNING": 1})
        self.assertEqual(logs["total"], 4)
        self.assertEqual(logs["groups_total"], 2)
        top = logs["groups"][0]
        self.assertEqual(top["level"], "ERROR")
        self.assertEqual(top["count"], 3)
        self.assertEqual(top["variants"], 2)
        # The old log is outside the window, but is the oldest kept, so the window isn't pruned.
        self.assertFalse(logs["window_partially_pruned"])

    def test_min_level_error_excludes_warnings(self):
        _log(logging.WARNING, "Slow response")
        _log(logging.CRITICAL, "Disk gone")

        logs = build_admin_report(_options(sections=(SECTION_LOGS,), min_log_level=logging.ERROR))["sections"][
            SECTION_LOGS
        ]

        self.assertEqual(logs["by_level"], {"CRITICAL": 1})

    def test_flags_window_starting_before_the_oldest_kept_log(self):
        _log(logging.ERROR, "Recent", age=datetime.timedelta(hours=1))

        report = build_admin_report(_options(hours=48, sections=(SECTION_LOGS,)))

        self.assertTrue(report["sections"][SECTION_LOGS]["window_partially_pruned"])
        self.assertIn(
            {"severity": "error", "section": SECTION_LOGS, "message": "1 error log entry"}, report["attention"]
        )


class HealthSectionTests(TestCase):
    def test_detects_unresponsive_and_overdue_schedulers(self):
        now = timezone.now()
        WorkerProcess.objects.create(pid=11, hostname="host", last_heartbeat=now)
        SchedulerState.objects.create(
            name="healthy", is_running=True, cycle_timer=3600, last_run=now, last_heartbeat=now
        )
        SchedulerState.objects.create(
            name="dead", is_running=True, cycle_timer=3600, last_run=now,
            last_heartbeat=now - datetime.timedelta(hours=1),
        )
        SchedulerState.objects.create(
            name="late", display_name="Late one", is_running=True, cycle_timer=3600,
            last_run=now - datetime.timedelta(hours=5), last_heartbeat=now,
        )
        SchedulerState.objects.create(name="off", is_running=False)
        ProviderRateLimit.objects.create(name="panda", cooldown_until=time.time() + 120)

        report = build_admin_report(_options(sections=(SECTION_HEALTH,)))
        health = report["sections"][SECTION_HEALTH]

        problems = {s["name"]: s["problem"] for s in health["scheduler_problems"]}
        self.assertEqual(problems, {"dead": "unresponsive", "late": "overdue"})
        self.assertEqual(health["schedulers_running"], 1)
        self.assertEqual(health["schedulers_stopped"], 1)
        self.assertEqual(health["workers_alive"], 1)
        self.assertEqual([c["provider"] for c in health["rate_limit_cooldowns"]], ["panda"])
        messages = [item["message"] for item in report["attention"]]
        self.assertIn("Scheduler dead is unresponsive", messages)
        self.assertIn("Scheduler Late one is overdue", messages)
        # Unresponsive schedulers are errors, listed before the overdue warning.
        self.assertLess(
            messages.index("Scheduler dead is unresponsive"), messages.index("Scheduler Late one is overdue")
        )

    def test_no_worker_is_an_error(self):
        WorkerProcess.objects.create(
            pid=12, hostname="host", last_heartbeat=timezone.now() - datetime.timedelta(minutes=10)
        )

        report = build_admin_report(_options(sections=(SECTION_HEALTH,)))

        self.assertEqual(report["sections"][SECTION_HEALTH]["workers_alive"], 0)
        self.assertEqual(len(report["sections"][SECTION_HEALTH]["stale_workers"]), 1)
        self.assertEqual(report["attention"][0]["message"], "No worker process is running")


class DownloadsAndTasksSectionTests(TestCase):
    def _event(self, **kwargs) -> DownloadEvent:
        defaults = {"name": "dl", "method": "archive", "agent": "agent", "download_id": "1"}
        defaults.update(kwargs)
        return DownloadEvent.objects.create(**defaults)

    def test_counts_finished_downloads_and_stuck_queue(self):
        now = timezone.now()
        self._event(completed=True, status=DownloadEvent.Status.COMPLETED, completed_date=now, total_size=10)
        self._event(
            completed=True, failed=True, status=DownloadEvent.Status.FAILED, completed_date=now,
            provider="panda", error_message="Timeout\nstack",
        )
        self._event(completed=True, failed=True, status=DownloadEvent.Status.CANCELLED, completed_date=now)
        stuck = self._event(status=DownloadEvent.Status.QUEUED)
        DownloadEvent.objects.filter(pk=stuck.pk).update(create_date=now - datetime.timedelta(hours=7))

        report = build_admin_report(_options(sections=(SECTION_DOWNLOADS,)))
        downloads = report["sections"][SECTION_DOWNLOADS]

        self.assertEqual(downloads["completed"], 1)
        self.assertEqual(downloads["completed_size"], 10)
        self.assertEqual(downloads["failed"], 1)
        self.assertEqual(downloads["cancelled"], 1)
        self.assertEqual(downloads["failed_by_provider"], [{"key": "panda", "count": 1}])
        errors = {failure["status"]: failure["error"] for failure in downloads["recent_failures"]}
        self.assertEqual(errors["failed"], "Timeout")
        self.assertEqual(downloads["queued_now"], 1)
        self.assertEqual(downloads["stuck_queued"], 1)

    def test_failed_tasks(self):
        TaskQueue.objects.create(
            task_type="web_crawler", status=TaskQueue.Status.FAILED, finished_at=timezone.now(), error_message="Boom"
        )
        TaskQueue.objects.create(task_type="web_crawler", status=TaskQueue.Status.PENDING)

        tasks = build_admin_report(_options(sections=("tasks",)))["sections"]["tasks"]

        self.assertEqual(tasks["failed"], 1)
        self.assertEqual(tasks["failed_by_type"], [{"key": "web_crawler", "count": 1}])
        self.assertEqual(tasks["recent_failures"][0]["error"], "Boom")
        self.assertEqual(tasks["pending_now"], 1)


class ReportRobustnessTests(TestCase):
    def test_a_failing_section_does_not_break_the_report(self):
        with mock.patch.object(admin_report, "_content_section", side_effect=RuntimeError("db exploded")):
            report = build_admin_report(_options(sections=(SECTION_CONTENT, SECTION_LOGS)))

        self.assertEqual(report["sections"][SECTION_CONTENT], {"error": "RuntimeError: db exploded"})
        self.assertIn("total", report["sections"][SECTION_LOGS])
        self.assertEqual(report["attention"][0]["severity"], "error")

    def test_storage_reports_disk_usage(self):
        crawler_settings = mock.Mock(MEDIA_ROOT="/", archive_dl_folder="", torrent_dl_folder="")
        storage = build_admin_report(_options(sections=("storage",)), crawler_settings)["sections"]["storage"]

        self.assertEqual(len(storage["disks"]), 1)
        self.assertGreater(storage["disks"][0]["total"], 0)


class ParseReportParamsTests(TestCase):
    def test_defaults_to_last_24_hours_and_all_sections(self):
        options, form_values = parse_report_params({})

        self.assertAlmostEqual(
            (options.until - options.since).total_seconds(), datetime.timedelta(hours=24).total_seconds(), delta=1
        )
        self.assertIsNone(options.sections)
        self.assertEqual(form_values["window"], "24h")
        self.assertEqual(options.min_log_level, logging.WARNING)

    def test_custom_window_with_sections_and_level(self):
        options, _ = parse_report_params(
            {"since": "2026-01-01", "until": "2026-01-02T12:00", "sections": "logs,content", "log_level": "error",
             "limit": "5"}
        )

        self.assertEqual(timezone.localtime(options.since).date(), datetime.date(2026, 1, 1))
        self.assertEqual(options.enabled_sections, ("content", "logs"))
        self.assertEqual(options.min_log_level, logging.ERROR)
        self.assertEqual(options.top_n, 5)

    def test_invalid_values(self):
        for params in (
            {"window": "1y"},
            {"window": "custom"},
            {"since": "yesterday"},
            {"log_level": "DEBUG"},
            {"limit": "many"},
            {"limit": "0"},
            {"sections": "bogus"},
        ):
            with self.subTest(params=params), self.assertRaises(ReportParamsError):
                parse_report_params(params)


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class AdminReportViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.user = User.objects.create_user(username="regular", password="pw")

    def test_anonymous_and_regular_users_are_rejected(self):
        response = self.client.get(reverse("viewer:admin-report"), {"format": "json"})
        self.assertEqual(response.status_code, 403)

        self.client.login(username="regular", password="pw")
        response = self.client.get(reverse("viewer:admin-report"))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("viewer:admin-report"), {"format": "json"})
        self.assertEqual(response.status_code, 403)

    def test_staff_sees_the_page(self):
        _log(logging.ERROR, "Something broke")
        self.client.login(username="staff", password="pw")

        response = self.client.get(reverse("viewer:admin-report"), {"window": "7d", "sections": ["logs", "content"]})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Something broke")
        self.assertContains(response, "1 error log entry")
        self.assertEqual(response.context["form_values"]["sections"], ["content", "logs"])

    def test_invalid_params_show_the_default_report_with_an_error(self):
        self.client.login(username="staff", password="pw")

        response = self.client.get(reverse("viewer:admin-report"), {"window": "forever"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unknown window")
        self.assertEqual(response.context["form_values"]["window"], "24h")

    def test_json_with_session_and_bad_params(self):
        self.client.login(username="staff", password="pw")

        response = self.client.get(reverse("viewer:admin-report"), {"format": "json", "sections": "logs"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.json()["sections"]), ["logs"])

        response = self.client.get(reverse("viewer:admin-report"), {"format": "json", "limit": "0"})
        self.assertEqual(response.status_code, 400)

    def test_json_with_read_token(self):
        _, secret = UserLongLivedToken.create_for_user(self.staff, "report", scopes=[UserLongLivedToken.SCOPE_READ])
        _, regular_secret = UserLongLivedToken.create_for_user(self.user, "report")
        _, write_only_secret = UserLongLivedToken.create_for_user(
            self.staff, "write", scopes=[UserLongLivedToken.SCOPE_WRITE]
        )
        url = reverse("viewer:admin-report")

        response = self.client.get(url, {"format": "json"}, HTTP_AUTHORIZATION=f"Bearer {secret}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attention", response.json())

        response = self.client.get(url, {"format": "json"}, HTTP_AUTHORIZATION=f"Bearer {regular_secret}")
        self.assertEqual(response.status_code, 403)

        response = self.client.get(url, {"format": "json"}, HTTP_AUTHORIZATION=f"Bearer {write_only_secret}")
        self.assertEqual(response.status_code, 403)
