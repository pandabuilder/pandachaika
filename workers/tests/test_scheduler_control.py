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




class SchedulerStateAndControlTests(TestCase):

    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.settings.is_worker_process = False  # Simulates web process

    def test_scheduler_state_model_methods(self):
        from workers.models import SchedulerState

        self.assertFalse(SchedulerState.is_scheduler_running("test_sched"))

        state, _ = SchedulerState.objects.update_or_create(
            name="test_sched",
            defaults={"is_running": True, "last_heartbeat": django_tz.now()},
        )
        self.assertTrue(SchedulerState.is_scheduler_running("test_sched"))

        # Stale heartbeat
        state.last_heartbeat = django_tz.now() - timedelta(seconds=120)
        state.save(update_fields=["last_heartbeat"])
        self.assertFalse(SchedulerState.is_scheduler_running("test_sched", threshold_seconds=30))

        # Request command
        SchedulerState.request_command("test_sched", "start", "5.0")
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "start")
        self.assertEqual(state.command_arg, "5.0")

    def test_base_scheduler_ipc_commands_from_web(self):
        from core.workers.schedulers import BaseScheduler
        from workers.models import SchedulerState

        class DummyScheduler(BaseScheduler):
            thread_name = "dummy_scheduler"
            def job(self):
                pass

        scheduler = DummyScheduler(self.settings)

        # Web process dispatching commands
        scheduler.start_running(timer=2.5)
        state = SchedulerState.objects.get(name="dummy_scheduler")
        self.assertEqual(state.command_requested, "start")
        self.assertEqual(state.command_arg, "2.5")

        scheduler.stop_running()
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "stop")

        scheduler.force_run()
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "force_run")

        # Scheduler is_running checks SchedulerState
        self.assertFalse(scheduler.is_running())
        state.is_running = True
        state.last_heartbeat = django_tz.now()
        state.save(update_fields=["is_running", "last_heartbeat"])
        self.assertTrue(scheduler.is_running())

    def test_runner_processes_scheduler_commands(self):
        from core.workers.schedulers import BaseScheduler
        from workers.models import SchedulerState

        started = []
        stopped = []

        class MockScheduler(BaseScheduler):
            thread_name = "mock_sched"
            def job(self):
                pass
            def start_running(self, timer=None, force_local=False):
                started.append(True)
                self.job_thread = True
            def stop_running(self, force_local=False):
                stopped.append(True)
                self.job_thread = None
            def is_running_locally(self):
                return bool(self.job_thread)

        runner = WorkerRunner(self.settings, enable_schedulers=True)
        mock_sched = MockScheduler(self.settings)
        runner.settings.workers.timed_downloader = mock_sched  # Inject into active workers

        # Request start
        SchedulerState.request_command("mock_sched", "start", "10")
        runner.check_scheduler_commands()

        self.assertEqual(len(started), 1)
        state = SchedulerState.objects.get(name="mock_sched")
        self.assertEqual(state.command_requested, "")
        self.assertTrue(state.is_running)

        # Request stop
        SchedulerState.request_command("mock_sched", "stop")
        runner.check_scheduler_commands()

        self.assertEqual(len(stopped), 1)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "")
        self.assertFalse(state.is_running)

    def test_get_thread_status_integration(self):
        from core.base.setup_utilities import get_thread_status_bool
        from workers.models import SchedulerState

        # Timed downloader not running
        statuses = get_thread_status_bool()
        self.assertFalse(statuses.get("timed_downloader", False))

        # Mark timed downloader as running in SchedulerState
        SchedulerState.objects.update_or_create(
            name="timed_downloader",
            defaults={"is_running": True, "last_heartbeat": django_tz.now()},
        )

        statuses_updated = get_thread_status_bool()
        self.assertTrue(statuses_updated.get("timed_downloader", False))

    def test_stats_workers_view_with_processing_task(self):
        from django.test import Client
        from workers.models import TaskQueue, WorkerProcess

        WorkerProcess.objects.create(pid=99999, status="running")
        test_user = User.objects.create_user(username="statstestuser", password="password123")

        client_task = client.enqueue(
            args=["https://example.com/gallery/1/"],
            user=test_user,
            reason="processing download",
            override_options={"redownload": False, "archive_reason": "test download"},
        )
        client_task.status = TaskQueue.Status.PROCESSING
        client_task.started_at = django_tz.now()
        client_task.save()

        client.enqueue(
            args=["https://example.com/gallery/2/"],
            user=test_user,
        )

        admin_user = User.objects.create_superuser(
            username="adminuser", email="admin@example.com", password="adminpassword"
        )
        test_client = Client()
        test_client.force_login(admin_user)

        response = test_client.get("/stats/workers")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Web Queue")
        self.assertContains(response, "redownload: False")
        self.assertContains(response, "archive_reason: test download")

    def test_stats_workers_view_with_scheduler_state_custom_cycle_timer_and_pid(self):
        from django.test import Client
        from workers.models import SchedulerState

        # Create SchedulerState with custom cycle timer and worker PID
        SchedulerState.objects.update_or_create(
            name="post_downloader",
            defaults={
                "cycle_timer": 1800.0,
                "worker_pid": 42424,
                "is_running": True,
                "last_heartbeat": django_tz.now(),
            },
        )

        admin_user = User.objects.create_superuser(
            username="adminuser_sched", email="admin_sched@example.com", password="adminpassword"
        )
        test_client = Client()
        test_client.force_login(admin_user)

        response = test_client.get("/stats/workers")
        self.assertEqual(response.status_code, 200)
        # Check that table contains Worker PID header and the worker PID
        self.assertContains(response, "Worker PID")
        self.assertContains(response, "42424")
        # Check that table contains the cycle timer from the DB (1800), not the default (5)
        self.assertContains(response, "1800")

    def test_get_active_initialized_workers_from_db(self):
        from workers.models import SchedulerState
        from core.base.utilities import get_schedulers_status

        self.settings.workers.start_workers(self.settings, start_threads=False)

        # Create SchedulerState with custom cycle timer and worker PID
        state, _ = SchedulerState.objects.update_or_create(
            name="post_downloader",
            defaults={
                "cycle_timer": 3600.0,
                "worker_pid": 1337,
                "is_running": True,
                "last_heartbeat": django_tz.now(),
            },
        )

        db_workers = self.settings.workers.get_active_initialized_workers_from_db()
        # Ensure we got SchedulerState instance for post_downloader
        post_dl = next((w for w in db_workers if getattr(w, "name", "") == "post_downloader" or getattr(w, "thread_name", "") == "post_downloader"), None)
        self.assertIsNotNone(post_dl)
        self.assertEqual(getattr(post_dl, "cycle_timer", getattr(post_dl, "timer", 0.0)), 3600.0)

        # Check get_schedulers_status output
        statuses = get_schedulers_status(db_workers)
        post_dl_status = next((s for s in statuses if s.name == "post_downloader"), None)
        self.assertIsNotNone(post_dl_status)
        self.assertEqual(post_dl_status.worker_pid, 1337)
        self.assertTrue(post_dl_status.is_running)
        self.assertIn("3600", post_dl_status.cycle_timer)

        # Ensure in-memory get_active_initialized_workers used by worker runner still returns in-memory workers
        in_memory = self.settings.workers.get_active_initialized_workers()
        self.assertTrue(len(in_memory) > 0)


