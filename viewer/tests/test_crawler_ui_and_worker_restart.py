import json
import signal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
import django.utils.timezone as django_tz

from core.base.setup import Settings
from viewer.views import admin, admin_api
from workers.models import SchedulerState, WorkerProcess
from workers.runner import WorkerRunner

User = get_user_model()


class WebCrawlerViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_crawler",
            email="admin_crawler@example.com",
            password="testpassword",
        )
        self.non_staff_user = User.objects.create_user(
            username="normal_user",
            email="normal_user@example.com",
            password="testpassword",
        )

    def test_crawler_page_get_requires_staff(self):
        self.client.login(username="normal_user", password="testpassword")
        resp = self.client.get(reverse("viewer:crawler"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "You need to be an admin")

    def test_crawler_page_renders_options_and_priority(self):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.get(reverse("viewer:crawler"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "viewer/crawler.html")
        # Check priority input exists with default value 10
        self.assertContains(resp, 'name="priority"')
        self.assertContains(resp, 'value="10"')
        self.assertContains(resp, "Priority (1-10)")
        # Check replace metadata explanation
        self.assertContains(resp, "Replace metadata")
        self.assertContains(resp, "Re-fetches and overwrites existing gallery metadata")
        # Check Keep this settings note about restarting background workers
        self.assertContains(resp, "Keep this settings")
        self.assertContains(resp, "Settings saved to <code>settings.yaml</code> will only be applied to background workers when restarted")
        # Check localStorage buttons
        self.assertContains(resp, "btn-save-local-settings")
        self.assertContains(resp, "Save current settings")
        self.assertContains(resp, "btn-clear-local-settings")

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_default_priority_10(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc\nhttps://example.com/g/456/def",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["priority"], 10)
        self.assertEqual(kwargs["task_type"], "web_crawler")
        self.assertEqual(sorted(kwargs["args"]), ["https://example.com/g/123/abc", "https://example.com/g/456/def"])

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_custom_priority(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "priority": "3",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["priority"], 3)

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_legacy_run_separate_priority_5(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "run_separate": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["priority"], 5)

    @patch("workers.client.enqueue")
    @patch.object(admin.crawler_settings, "write")
    @patch.object(admin.crawler_settings, "load_config_from_file")
    def test_crawler_keep_this_settings_saves_transferable_options(self, mock_reload, mock_write, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "priority": "2",
                "keep_this_settings": "1",
                "replace_metadata": "1",
                "skip-non-current": "1",
                "downloaders-chaika_archive": "8",
                "reason": "Test crawler reason",
                "gallery_only": "1",
                "wanted_only": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        # Verify write was called
        mock_write.assert_called_once()
        mock_reload.assert_called_once()

        # Check transferable options updated in crawler_settings.config
        cfg = admin.crawler_settings.config
        self.assertTrue(cfg["allowed"]["replace_metadata"])
        self.assertTrue(cfg["allowed"]["non_current_links_as_deleted"])
        self.assertEqual(cfg["downloaders"]["chaika_archive"], 8)

        # Non-transferable options like gallery_only / wanted_only modify job options, not settings.yaml
        mock_enqueue.assert_called_once()
        queued_options = mock_enqueue.call_args.kwargs["override_options"]
        self.assertEqual(queued_options.archive_reason, "Test crawler reason")
        self.assertTrue(queued_options.non_current_links_as_deleted)
        self.assertTrue(queued_options.replace_metadata)
        self.assertIn("--wanted-only", mock_enqueue.call_args.kwargs["args"])

    def test_settings_load_from_config_non_current_links_as_deleted_precedence(self):
        """Ensure settings loaded from config preserve allowed.non_current_links_as_deleted even when general section exists."""
        s = Settings(
            load_from_config={
                "allowed": {"non_current_links_as_deleted": True},
                "general": {"non_current_links_as_deleted": False},
            }
        )
        self.assertTrue(s.non_current_links_as_deleted)

        s2 = Settings(
            load_from_config={
                "allowed": {"non_current_links_as_deleted": True},
                "general": {},
            }
        )
        self.assertTrue(s2.non_current_links_as_deleted)

    def test_panda_parser_post_processing_propagates_override_options_to_chain(self):
        """Ensure chained parent/first galleries enqueued by panda parser preserve caller's override_options."""
        from collections import defaultdict
        from core.base.types import GalleryData
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        settings.non_current_links_as_deleted = True
        mock_queue = MagicMock()
        settings.workers.web_queue = mock_queue

        parser = PandaParser(settings)
        parser.own_settings.auto_process_parent = True
        parser.own_settings.auto_process_first = True

        mock_gallery_entry = MagicMock()
        mock_gallery_entry.gid = 999
        mock_gallery_entry.get_absolute_url.return_value = "https://example.com/g/999"

        gallery_data = GalleryData(
            gid="999",
            provider="panda",
            link="https://example.com/g/999",
        )
        gallery_data.extra_data = {
            "parent_gid": "888",
            "parent_key": "abc",
            "first_gid": "777",
            "first_key": "def",
        }

        with patch("core.providers.panda.parsers.logger"):
            parser.post_gallery_processing(mock_gallery_entry, gallery_data)

        self.assertEqual(mock_queue.enqueue_args_list.call_count, 2)
        for call in mock_queue.enqueue_args_list.call_args_list:
            self.assertIn("override_options", call.kwargs)
            self.assertIs(call.kwargs["override_options"], settings)


class RestartWorkersApiAndToolsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_restart",
            email="admin_restart@example.com",
            password="testpassword",
        )
        self.normal_user = User.objects.create_user(
            username="normal_restart",
            email="normal_restart@example.com",
            password="testpassword",
        )

    def test_api_restart_workers_unauthorized(self):
        self.client.login(username="normal_restart", password="testpassword")
        resp = self.client.post(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp.status_code, 401)
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "Not allowed")

    def test_restart_workers_require_post(self):
        self.client.login(username="admin_restart", password="testpassword")
        resp_api = self.client.get(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp_api.status_code, 405)
        resp_id = self.client.get(reverse("viewer:tools-id", args=["restart_workers"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp_id.status_code, 405)

    def test_api_restart_workers_no_running_workers(self):
        self.client.login(username="admin_restart", password="testpassword")
        SchedulerState.objects.filter(name="worker_runner").delete()
        WorkerProcess.objects.all().delete()

        resp = self.client.post(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("Restart command queued", data["message"])
        self.assertEqual(data["worker_pids"], [])

        # Verify command queued in SchedulerState
        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "restart")

    @patch("os.kill")
    def test_api_restart_workers_with_active_workers(self, mock_os_kill):
        import socket
        self.client.login(username="admin_restart", password="testpassword")
        now = django_tz.now()
        wp = WorkerProcess.objects.create(
            pid=98765,
            hostname=socket.gethostname(),
            status="running",
            last_heartbeat=now,
        )

        resp = self.client.post(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("Restart requested for 1 running worker process", data["message"])
        self.assertEqual(data["worker_pids"], [98765])

        if hasattr(signal, "SIGHUP"):
            mock_os_kill.assert_called_with(98765, signal.SIGHUP)

        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "restart")

    @patch("os.kill")
    def test_admin_view_tools_restart_workers(self, mock_os_kill):
        import socket
        self.client.login(username="admin_restart", password="testpassword")
        WorkerProcess.objects.create(
            pid=54321,
            hostname=socket.gethostname(),
            status="running",
            last_heartbeat=django_tz.now(),
        )
        resp = self.client.post(
            reverse("viewer:tools-id", args=["restart_workers"]),
            HTTP_REFERER="/tools/",
        )
        self.assertEqual(resp.status_code, 302)
        if hasattr(signal, "SIGHUP"):
            mock_os_kill.assert_called_with(54321, signal.SIGHUP)

        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "restart")

    def test_tools_page_contains_restart_workers_link(self):
        self.client.login(username="admin_restart", password="testpassword")
        resp = self.client.get(reverse("viewer:tools"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("viewer:tools-id", args=["restart_workers"]))
        self.assertContains(resp, "Restart Workers")


class WorkerRunnerRestartTests(TestCase):
    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.runner = WorkerRunner(self.settings, enable_schedulers=False)

    def test_check_scheduler_commands_handles_restart_ipc(self):
        SchedulerState.request_command("worker_runner", "restart")
        self.runner.running = True
        self.runner.should_restart = False

        self.runner.check_scheduler_commands()

        self.assertTrue(self.runner.should_restart)
        self.assertFalse(self.runner.running)
        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "")

    def test_handle_restart_signal(self):
        self.runner.running = True
        self.runner.should_restart = False

        self.runner._handle_restart(signal.SIGHUP if hasattr(signal, "SIGHUP") else 1, None)

        self.assertTrue(self.runner.should_restart)
        self.assertFalse(self.runner.running)

    @patch.object(WorkerRunner, "_restart_process")
    def test_runner_run_invokes_restart_process_when_requested(self, mock_restart_process):
        runner = WorkerRunner(self.settings, enable_schedulers=False)
        runner.should_restart = True

        def stop_loop():
            runner.running = False
            return None

        with patch.object(runner, "claim_next_job", side_effect=stop_loop):
            runner.run()

        mock_restart_process.assert_called_once()
