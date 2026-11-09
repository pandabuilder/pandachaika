import datetime
from io import StringIO
import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django_db_logger.models import StatusLog

from core.base.config_models import RetentionSettings, PandaGalleryConfig
from core.base.setup import Settings
from core.workers.retention import TimedDatabasePruner
from viewer.models import Archive, DownloadEvent, EventLog, Gallery, ProcessedLinks
from viewer.services.retention import (
    get_database_info,
    get_retention_counts,
    prune_database,
)
from workers.models import SchedulerState, TaskQueue
from workers.runner import WorkerRunner

User = get_user_model()


class RetentionConfigurationTests(TestCase):
    """Test RetentionSettings model and integration into PandaGalleryConfig and Settings."""

    def test_default_retention_settings(self):
        retention = RetentionSettings()
        self.assertFalse(retention.enabled)
        self.assertEqual(retention.schedule_interval_hours, 24.0)
        self.assertEqual(retention.task_queue_days, 30)
        self.assertEqual(retention.download_event_days, 30)
        self.assertEqual(retention.event_log_days, 90)
        self.assertEqual(retention.status_log_days, 30)
        self.assertEqual(retention.processed_links_days, 180)
        self.assertEqual(retention.history_days, 90)
        self.assertEqual(retention.chunk_size, 5000)

    def test_root_config_retention(self):
        cfg = PandaGalleryConfig()
        self.assertIsInstance(cfg.retention, RetentionSettings)
        self.assertEqual(cfg.retention.task_queue_days, 30)

        # Update from dict
        cfg.update_from_dict({
            "retention": {
                "enabled": True,
                "task_queue_days": 14,
                "schedule_interval_hours": 12.0,
            }
        })
        self.assertTrue(cfg.retention.enabled)
        self.assertEqual(cfg.retention.task_queue_days, 14)
        self.assertEqual(cfg.retention.schedule_interval_hours, 12.0)

    def test_settings_object_retention(self):
        settings_obj = Settings()
        self.assertTrue(hasattr(settings_obj, "retention"))
        self.assertIsInstance(settings_obj.retention, RetentionSettings)


class DatabaseRetentionServiceTests(TestCase):
    """Test get_database_info, get_retention_counts, and prune_database."""

    def setUp(self):
        self.user = User.objects.create_user(username="retention_tester", password="password")
        self.now = timezone.now()

    def test_get_database_info(self):
        info = get_database_info()
        self.assertIn("vendor", info)
        self.assertIn("name", info)
        self.assertIn("host", info)
        self.assertIn("db_size", info)

    def test_prune_task_queue(self):
        old_time = self.now - datetime.timedelta(days=40)
        recent_time = self.now - datetime.timedelta(days=10)

        # Old completed -> prunable
        t_completed_old = TaskQueue.objects.create(
            task_type="web_crawler",
            status=TaskQueue.Status.COMPLETED,
            finished_at=old_time,
            created_at=old_time,
        )
        # Old failed -> prunable
        t_failed_old = TaskQueue.objects.create(
            task_type="web_crawler",
            status=TaskQueue.Status.FAILED,
            finished_at=old_time,
            created_at=old_time,
        )
        # Old cancelled -> prunable
        t_cancelled_old = TaskQueue.objects.create(
            task_type="web_crawler",
            status=TaskQueue.Status.CANCELLED,
            finished_at=old_time,
            created_at=old_time,
        )
        # Old pending -> MUST NOT be pruned!
        t_pending_old = TaskQueue.objects.create(
            task_type="web_crawler",
            status=TaskQueue.Status.PENDING,
            created_at=old_time,
        )
        # Old processing -> MUST NOT be pruned!
        t_processing_old = TaskQueue.objects.create(
            task_type="web_crawler",
            status=TaskQueue.Status.PROCESSING,
            started_at=old_time,
            created_at=old_time,
        )
        # Recent completed -> MUST NOT be pruned!
        t_completed_recent = TaskQueue.objects.create(
            task_type="web_crawler",
            status=TaskQueue.Status.COMPLETED,
            finished_at=recent_time,
            created_at=recent_time,
        )

        # Dry run test first
        dry_res = prune_database(
            entity="task_queue",
            days_override=30,
            dry_run=True,
        )
        self.assertEqual(dry_res["total_deleted"], 3)
        self.assertTrue(TaskQueue.objects.filter(pk=t_completed_old.pk).exists())

        # Live run
        res = prune_database(
            entity="task_queue",
            days_override=30,
            dry_run=False,
            user=self.user,
        )
        self.assertEqual(res["total_deleted"], 3)
        self.assertFalse(TaskQueue.objects.filter(pk=t_completed_old.pk).exists())
        self.assertFalse(TaskQueue.objects.filter(pk=t_failed_old.pk).exists())
        self.assertFalse(TaskQueue.objects.filter(pk=t_cancelled_old.pk).exists())

        # Active and recent tasks remain untouched
        self.assertTrue(TaskQueue.objects.filter(pk=t_pending_old.pk).exists())
        self.assertTrue(TaskQueue.objects.filter(pk=t_processing_old.pk).exists())
        self.assertTrue(TaskQueue.objects.filter(pk=t_completed_recent.pk).exists())

    def test_prune_download_events(self):
        old_time = self.now - datetime.timedelta(days=40)
        recent_time = self.now - datetime.timedelta(days=5)

        d_completed_old = DownloadEvent.objects.create(
            name="old_completed",
            completed=True,
            failed=False,
            status=DownloadEvent.Status.COMPLETED,
            completed_date=old_time,
        )
        d_failed_old = DownloadEvent.objects.create(
            name="old_failed",
            completed=False,
            failed=True,
            status=DownloadEvent.Status.FAILED,
            completed_date=old_time,
        )
        d_inprogress_old = DownloadEvent.objects.create(
            name="old_inprogress",
            completed=False,
            failed=False,
            status=DownloadEvent.Status.IN_PROGRESS,
        )
        d_completed_recent = DownloadEvent.objects.create(
            name="recent_completed",
            completed=True,
            status=DownloadEvent.Status.COMPLETED,
            completed_date=recent_time,
        )

        res = prune_database(
            entity="download_event",
            days_override=30,
            dry_run=False,
        )
        self.assertEqual(res["total_deleted"], 2)
        self.assertFalse(DownloadEvent.objects.filter(pk=d_completed_old.pk).exists())
        self.assertFalse(DownloadEvent.objects.filter(pk=d_failed_old.pk).exists())
        self.assertTrue(DownloadEvent.objects.filter(pk=d_inprogress_old.pk).exists())
        self.assertTrue(DownloadEvent.objects.filter(pk=d_completed_recent.pk).exists())

    def test_prune_event_logs_and_status_logs(self):
        old_time = self.now - datetime.timedelta(days=100)
        recent_time = self.now - datetime.timedelta(days=10)

        e_old = EventLog.objects.create(action="TEST_ACTION", create_date=old_time)
        e_recent = EventLog.objects.create(action="TEST_ACTION", create_date=recent_time)

        s_old = StatusLog.objects.create(msg="Old log message")
        StatusLog.objects.filter(pk=s_old.pk).update(create_datetime=old_time)
        s_recent = StatusLog.objects.create(msg="Recent log message")

        res_events = prune_database(entity="event_log", days_override=90, dry_run=False)
        self.assertEqual(res_events["total_deleted"], 1)
        self.assertFalse(EventLog.objects.filter(pk=e_old.pk).exists())
        self.assertTrue(EventLog.objects.filter(pk=e_recent.pk).exists())

        res_status = prune_database(entity="status_log", days_override=30, dry_run=False)
        self.assertEqual(res_status["total_deleted"], 1)
        self.assertFalse(StatusLog.objects.filter(pk=s_old.pk).exists())
        self.assertTrue(StatusLog.objects.filter(pk=s_recent.pk).exists())

    def test_prune_processed_links(self):
        old_time = self.now - datetime.timedelta(days=200)
        p_old = ProcessedLinks.objects.create(
            source_id="12345",
            url="https://example.com/item/12345",
            create_date=old_time,
        )
        # Update auto_now_add field via queryset update
        ProcessedLinks.objects.filter(pk=p_old.pk).update(create_date=old_time)

        p_recent = ProcessedLinks.objects.create(
            source_id="12346",
            url="https://example.com/item/12346",
        )

        res = prune_database(entity="processed_links", days_override=180, dry_run=False)
        self.assertEqual(res["total_deleted"], 1)
        self.assertFalse(ProcessedLinks.objects.filter(pk=p_old.pk).exists())
        self.assertTrue(ProcessedLinks.objects.filter(pk=p_recent.pk).exists())

    def test_get_retention_counts(self):
        counts = get_retention_counts()
        self.assertIsInstance(counts, list)
        keys = [c["key"] for c in counts]
        self.assertIn("task_queue", keys)
        self.assertIn("download_event", keys)
        self.assertIn("event_log", keys)
        self.assertIn("status_log", keys)
        self.assertIn("processed_links", keys)
        self.assertIn("history_archive", keys)
        self.assertIn("history_gallery", keys)


class TimedDatabasePrunerSchedulerTests(TestCase):
    """Test TimedDatabasePruner periodic scheduler."""

    def test_timer_conversion_and_thread_name(self):
        settings_obj = Settings()
        pruner = TimedDatabasePruner(settings_obj, timer=24.0)
        self.assertEqual(pruner.thread_name, "timed_database_pruner")
        self.assertEqual(pruner.timer, 24.0 * 3600)

    def test_pruner_job_execution(self):
        settings_obj = Settings()
        settings_obj.retention.enabled = True
        pruner = TimedDatabasePruner(settings_obj, timer=1.0)
        pruner.stop.set()  # Stop immediately so loop terminates
        pruner.job()  # Should complete without error


class ManagementCommandPruneDbTests(TestCase):
    """Test prune_db and prune_database management commands."""

    def test_prune_db_dry_run_command(self):
        out = StringIO()
        call_command("prune_db", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("DRY-RUN", output)
        self.assertIn("Database Pruning Results", output)

    def test_prune_database_alias_command(self):
        out = StringIO()
        call_command("prune_database", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("DRY-RUN", output)


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class DatabaseMaintenanceViewTests(TestCase):
    """Test database_maintenance view and navigation rendering."""

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="staff_admin", password="password", is_staff=True
        )
        self.regular_user = User.objects.create_user(
            username="regular_user", password="password", is_staff=False
        )

    def test_anonymous_redirects(self):
        resp = self.client.get(reverse("viewer:database-maintenance"))
        self.assertEqual(resp.status_code, 302)

    def test_non_staff_forbidden(self):
        self.client.login(username="regular_user", password="password")
        resp = self.client.get(reverse("viewer:database-maintenance"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"admin", resp.content)

    def test_staff_get_database_maintenance(self):
        self.client.login(username="staff_admin", password="password")
        resp = self.client.get(reverse("viewer:database-maintenance"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Database Maintenance & Retention", resp.content)
        self.assertIn(b"Task Queue", resp.content)
        self.assertIn(b"Download Events", resp.content)
        self.assertIn(b"Server Status Logs", resp.content)

    def test_staff_post_background_prune(self):
        self.client.login(username="staff_admin", password="password")
        resp = self.client.post(
            reverse("viewer:database-maintenance"),
            {"action": "prune_background", "entity": "task_queue", "days": "30"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        # Check task was created
        task = TaskQueue.objects.filter(task_type="database_prune").latest("id")
        self.assertEqual(task.override_options.get("entity"), "task_queue")
        self.assertEqual(task.override_options.get("days"), 30)

    def test_staff_post_immediate_prune(self):
        self.client.login(username="staff_admin", password="password")
        resp = self.client.post(
            reverse("viewer:database-maintenance"),
            {"action": "prune_immediate", "entity": "status_log", "days": "30", "dry_run": "1"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Dry run: would delete", resp.content)

    def test_staff_post_scheduler_control(self):
        self.client.login(username="staff_admin", password="password")
        resp = self.client.post(
            reverse("viewer:database-maintenance"),
            {"action": "scheduler_control", "command": "force_run"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        state = SchedulerState.objects.filter(name="timed_database_pruner").first()
        self.assertIsNotNone(state)
        self.assertEqual(state.command_requested, "force_run")

    def test_navbar_navigation_grouping(self):
        self.client.login(username="staff_admin", password="password")
        resp = self.client.get(reverse("viewer:database-maintenance"))
        content = resp.content.decode("utf-8")

        # Verify grouped dropdowns exist
        self.assertIn('id="manageDropdown"', content)
        self.assertIn('id="dataDropdown"', content)
        self.assertIn('id="statsDropdown"', content)
        self.assertIn('id="settingsDropdown"', content)

        # Verify menu items under Manage
        self.assertIn("Manage archives", content)
        self.assertIn("Archive Manager App", content)
        self.assertIn("Manage galleries", content)
        self.assertIn("URL Crawler", content)

        # Verify URL Crawler is also in the top main bar
        crawler_url = reverse("viewer:crawler")
        self.assertIn(f'<a class="nav-link" href="{crawler_url}">URL Crawler</a>', content)

        # Verify menu items under Data
        self.assertIn("Repeated archives", content)
        self.assertIn("Image Search", content)
        self.assertIn("Activity log", content)

        # Verify menu items under Settings
        self.assertIn("Workers Dashboard", content)
        self.assertIn("Server Logs", content)
        self.assertIn("Database Maintenance", content)


class BackgroundWorkerExecutionTests(TestCase):
    """Test WorkerRunner execution of database_prune task."""

    def setUp(self):
        self.user = User.objects.create_user(username="worker_tester", password="password")
        self.settings = Settings()
        self.runner = WorkerRunner(self.settings, enable_schedulers=False)

    def test_worker_runner_executes_database_prune(self):
        task = TaskQueue.objects.create(
            task_type="database_prune",
            status=TaskQueue.Status.PROCESSING,
            options={"entity": "all", "dry_run": True},
            user=self.user,
        )

        with patch("viewer.services.retention.prune_database") as mock_prune:
            mock_prune.return_value = {"total_deleted": 0, "duration_seconds": 0.05}
            self.runner.execute_job(task)

            mock_prune.assert_called_once()
            task.refresh_from_db()
            self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
