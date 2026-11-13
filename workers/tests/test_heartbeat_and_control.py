# -*- coding: utf-8 -*-

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.setup import Settings

User = get_user_model()




class WorkerHeartbeatAndControlTests(TestCase):

    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.settings.is_worker_process = False

    def test_stale_scheduler_commands_cleared_on_startup(self):
        from unittest.mock import patch
        from workers.models import SchedulerState
        from workers.runner import WorkerRunner

        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={"command_requested": "stop", "command_arg": ""},
        )
        SchedulerState.objects.update_or_create(
            name="download_progress_checker",
            defaults={"command_requested": "stop", "command_arg": ""},
        )

        runner = WorkerRunner(self.settings, enable_schedulers=True)
        with patch.object(runner.settings.workers, "start_workers"):
            runner.start_schedulers_if_enabled()

        st1 = SchedulerState.objects.get(name="auto_wanted")
        st2 = SchedulerState.objects.get(name="download_progress_checker")
        self.assertEqual(st1.command_requested, "")
        self.assertEqual(st2.command_requested, "")

    def test_heartbeat_thread_lifecycle(self):
        import time
        from unittest.mock import patch
        from workers.runner import WorkerRunner

        runner = WorkerRunner(self.settings, heartbeat_interval=0.2, enable_schedulers=False)
        with patch("django.db.close_old_connections"), patch.object(runner, "update_heartbeat") as mock_hb, patch.object(runner, "check_scheduler_commands") as mock_cmd:
            runner.start_heartbeat_thread()
            self.assertIsNotNone(runner.heartbeat_thread)
            self.assertTrue(runner.heartbeat_thread.is_alive())

            # Wait briefly for thread loop to trigger
            time.sleep(0.3)
            self.assertGreaterEqual(mock_hb.call_count, 1)
            self.assertGreaterEqual(mock_cmd.call_count, 1)

            runner.stop_heartbeat_thread()
            self.assertIsNone(runner.heartbeat_thread)

    def test_sync_schedulers_state_download_progress_checker_extra_data(self):
        from unittest.mock import MagicMock
        from workers.models import SchedulerState
        from workers.runner import WorkerRunner

        runner = WorkerRunner(self.settings, enable_schedulers=True)
        mock_checker = MagicMock()
        mock_checker.thread_name = "download_progress_checker"
        mock_checker.timer = 60
        mock_checker.last_run = None
        mock_checker.current_last_run = None
        mock_checker.is_running_locally.return_value = True

        mock_download = MagicMock()
        mock_download.filename = "test.zip"
        mock_download.speed = 1024.0
        mock_download.index = 1
        mock_download.total = 1
        mock_download.downloaded = 512
        mock_download.filesize = 1024
        mock_checker.current_download.return_value = [mock_download]

        original_checker = runner.settings.workers.download_progress_checker
        runner.settings.workers.download_progress_checker = mock_checker
        self.addCleanup(setattr, runner.settings.workers, "download_progress_checker", original_checker)
        runner.sync_schedulers_state()

        st = SchedulerState.objects.get(name="download_progress_checker")
        self.assertIn("current_download", st.extra_data)
        self.assertEqual(st.extra_data["current_download"][0]["filename"], "test.zip")
