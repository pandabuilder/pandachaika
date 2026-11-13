# -*- coding: utf-8 -*-
from datetime import timedelta

import django.utils.timezone as django_tz
from django.test import TestCase

from core.base.setup import Settings
from workers.models import TaskQueue, WorkerProcess
from workers.runner import WorkerRunner
from workers.tests.factories import TaskQueueFactory


class TaskAttemptsTests(TestCase):
    def setUp(self):
        self.runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)

    def test_claim_counts_attempt(self):
        task = TaskQueueFactory()
        self.assertEqual(task.attempts, 0)
        claimed = self.runner.claim_next_job()
        self.assertEqual(claimed.pk, task.pk)
        task.refresh_from_db()
        self.assertEqual(task.attempts, 1)

    def test_claim_skips_tasks_with_future_not_before(self):
        delayed = TaskQueueFactory(not_before=django_tz.now() + timedelta(hours=1))
        self.assertIsNone(self.runner.claim_next_job())
        TaskQueue.objects.filter(pk=delayed.pk).update(not_before=django_tz.now() - timedelta(seconds=1))
        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, delayed.pk)

    def test_dead_worker_task_is_requeued_until_attempts_are_used_up(self):
        task = TaskQueueFactory(
            status=TaskQueue.Status.PROCESSING, worker_pid=424242, attempts=2, max_attempts=3
        )
        WorkerProcess.cleanup_dead_workers()
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertIsNone(task.worker_pid)

        # Claimed again and the worker dies again: this was the third start.
        TaskQueue.objects.filter(pk=task.pk).update(
            status=TaskQueue.Status.PROCESSING, worker_pid=424242, attempts=3
        )
        WorkerProcess.cleanup_dead_workers()
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.FAILED)
        self.assertIn("too many times", task.error_message)
        self.assertIsNotNone(task.finished_at)

    def test_register_start_fails_crash_looping_task(self):
        crashing = TaskQueueFactory(
            status=TaskQueue.Status.PROCESSING, worker_pid=999999, attempts=3, max_attempts=3
        )
        recoverable = TaskQueueFactory(
            task_type="folder_crawler", status=TaskQueue.Status.PROCESSING, worker_pid=999999, attempts=1
        )
        self.runner.register_start()
        crashing.refresh_from_db()
        recoverable.refresh_from_db()
        self.assertEqual(crashing.status, TaskQueue.Status.FAILED)
        self.assertEqual(recoverable.status, TaskQueue.Status.PENDING)

    def test_clean_shutdown_revert_does_not_use_an_attempt(self):
        task = TaskQueueFactory()
        claimed = self.runner.claim_next_job()
        self.runner.revert_interrupted_jobs([claimed])
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.attempts, 0)
        self.assertIsNone(task.worker_pid)

    def test_revert_does_not_touch_finished_tasks(self):
        task = TaskQueueFactory()
        claimed = self.runner.claim_next_job()
        TaskQueue.objects.filter(pk=task.pk).update(status=TaskQueue.Status.COMPLETED)
        self.runner.revert_interrupted_jobs([claimed])
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertEqual(task.attempts, 1)


class DatabasePrunerSchedulerTests(TestCase):
    def test_disabled_pruner_waits_for_its_interval_instead_of_spinning(self):
        import threading
        import time
        from unittest.mock import patch

        from core.workers.retention import TimedDatabasePruner

        settings = Settings(load_from_disk=False)
        settings.retention.enabled = False
        pruner = TimedDatabasePruner(settings, timer=24.0)
        runs = []
        real_update = pruner.update_last_run

        def counting_update(last_run):
            runs.append(last_run)
            real_update(last_run)

        with (
            patch.object(pruner, "update_last_run", side_effect=counting_update),
            patch("viewer.services.retention.prune_database") as prune,
        ):
            thread = threading.Thread(target=pruner.job, daemon=True)
            thread.start()
            time.sleep(0.5)
            pruner.stop.set()
            thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(runs), 1)
        prune.assert_not_called()


class WorkerPidCollisionTests(TestCase):
    def test_warns_when_another_host_uses_the_same_pid(self):
        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        WorkerProcess.objects.create(pid=runner.pid, hostname="some-other-host", status="running")
        with self.assertLogs("workers.runner", level="CRITICAL") as logs:
            runner.warn_on_pid_collision()
        self.assertIn("some-other-host", logs.output[0])

    def test_no_warning_for_own_previous_registration(self):
        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        WorkerProcess.objects.create(pid=runner.pid, hostname=runner.hostname, status="running")
        with self.assertNoLogs("workers.runner", level="CRITICAL"):
            runner.warn_on_pid_collision()
