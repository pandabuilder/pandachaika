# -*- coding: utf-8 -*-
import json
import uuid
from datetime import timedelta

import django.utils.timezone as django_tz
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.parsers import BaseParser
from core.base.setup import Settings
from core.workers.webqueue import WebQueue
from viewer.models import Archive, EventLog, Gallery, GallerySubmitEntry, UserArchivePrefs
from workers import client
from workers.models import TaskQueue, WorkerProcess
from workers.runner import WorkerRunner
from workers.serialization import deserialize_settings_options, serialize_settings_options

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

    def test_sync_schedulers_state_post_downloader_extra_data(self):
        from unittest.mock import MagicMock
        from workers.models import SchedulerState
        from workers.runner import WorkerRunner

        runner = WorkerRunner(self.settings, enable_schedulers=True)
        mock_post_dl = MagicMock()
        mock_post_dl.thread_name = "post_downloader"
        mock_post_dl.timer = 60
        mock_post_dl.last_run = None
        mock_post_dl.current_last_run = None
        mock_post_dl.is_running_locally.return_value = True

        mock_download = MagicMock()
        mock_download.filename = "test.zip"
        mock_download.speed = 1024.0
        mock_download.index = 1
        mock_download.total = 1
        mock_download.downloaded = 512
        mock_download.filesize = 1024
        mock_post_dl.current_download.return_value = [mock_download]

        runner.settings.workers.timed_downloader = mock_post_dl
        runner.sync_schedulers_state()

        st = SchedulerState.objects.get(name="post_downloader")
        self.assertIn("current_download", st.extra_data)
        self.assertEqual(st.extra_data["current_download"][0]["filename"], "test.zip")

    def test_admin_tools_force_run_dispatches_ipc(self):
        from unittest.mock import patch
        from django.contrib.auth.models import User
        from django.test import RequestFactory
        from viewer.views.admin import tools
        from workers.models import SchedulerState

        user = User.objects.create_superuser("adminuser", "admin@example.com", "pass")
        factory = RequestFactory()

        # Test force_run_timed_dl
        req = factory.post("/tools/force_run_timed_dl")
        req.user = user
        req.META["HTTP_REFERER"] = "/tools/"
        self.settings.workers.start_workers(self.settings, start_threads=False)
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp = tools(req, "force_run_timed_dl")
            self.assertEqual(resp.status_code, 302)
            st = SchedulerState.objects.get(name="post_downloader")
            self.assertEqual(st.command_requested, "force_run")

        # Test force_run_timed_auto_wanted
        req2 = factory.post("/tools/force_run_timed_auto_wanted")
        req2.user = user
        req2.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp2 = tools(req2, "force_run_timed_auto_wanted")
            self.assertEqual(resp2.status_code, 302)
            st2 = SchedulerState.objects.get(name="auto_wanted")
            self.assertEqual(st2.command_requested, "force_run")

        # Test force_archive_download_queue
        req3 = factory.post("/tools/force_archive_download_queue")
        req3.user = user
        req3.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp3 = tools(req3, "force_archive_download_queue")
            self.assertEqual(resp3.status_code, 302)
            st3 = SchedulerState.objects.get(name="archive_download_queue")
            self.assertEqual(st3.command_requested, "force_run")

        # Test start_archive_download_queue
        req4 = factory.post("/tools/start_archive_download_queue")
        req4.user = user
        req4.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp4 = tools(req4, "start_archive_download_queue")
            self.assertEqual(resp4.status_code, 302)
            st4 = SchedulerState.objects.get(name="archive_download_queue")
            self.assertEqual(st4.command_requested, "start")

        # Test stop_archive_download_queue
        req5 = factory.post("/tools/stop_archive_download_queue")
        req5.user = user
        req5.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp5 = tools(req5, "stop_archive_download_queue")
            self.assertEqual(resp5.status_code, 302)
            st5 = SchedulerState.objects.get(name="archive_download_queue")
            self.assertEqual(st5.command_requested, "stop")


