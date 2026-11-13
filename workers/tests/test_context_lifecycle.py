# -*- coding: utf-8 -*-

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.setup import Settings

User = get_user_model()




class WorkerContextLifecycleTests(TestCase):

    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.settings.is_worker_process = False

    def test_web_process_does_not_stop_schedulers_or_workers(self):
        from core.workers.holder import WorkerContext
        from workers.models import SchedulerState

        context = WorkerContext()
        context.start_workers(self.settings, start_threads=False)
        self.assertFalse(context.is_worker_process)

        # Pre-create SchedulerState for schedulers
        SchedulerState.objects.update_or_create(
            name="download_progress_checker",
            defaults={"is_running": True, "command_requested": ""},
        )
        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={"is_running": True, "command_requested": ""},
        )

        # Calling command_workers_to_stop or stop_workers_and_wait from web process must be a no-op
        context.command_workers_to_stop()
        context.stop_workers_and_wait()

        # Verify no stop command was requested
        st1 = SchedulerState.objects.get(name="download_progress_checker")
        st2 = SchedulerState.objects.get(name="auto_wanted")
        self.assertEqual(st1.command_requested, "")
        self.assertEqual(st2.command_requested, "")

    def test_worker_process_stops_local_schedulers_without_ipc_command(self):
        from core.workers.holder import WorkerContext
        from workers.models import SchedulerState

        worker_settings = Settings(load_from_disk=False)
        worker_settings.is_worker_process = True

        context = WorkerContext()
        context.start_workers(worker_settings, start_threads=False)
        self.assertTrue(context.is_worker_process)

        SchedulerState.objects.update_or_create(
            name="download_progress_checker",
            defaults={"is_running": True, "command_requested": ""},
        )

        # In worker process, command_workers_to_stop stops local schedulers directly
        context.command_workers_to_stop()

        # IPC command_requested should NOT be set to "stop"
        st = SchedulerState.objects.get(name="download_progress_checker")
        self.assertEqual(st.command_requested, "")


