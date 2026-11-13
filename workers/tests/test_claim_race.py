# -*- coding: utf-8 -*-
import threading

from django.db import connection
from django.test import TransactionTestCase
from unittest.mock import patch

from core.base.setup import Settings
from workers import runner as runner_module
from workers.models import TaskQueue
from workers.runner import WorkerRunner
from workers.tests.factories import TaskQueueFactory


class ClaimRaceTests(TransactionTestCase):
    def test_two_workers_never_claim_two_tasks_of_the_same_type(self):
        TaskQueueFactory(args=["https://example.com/a"])
        TaskQueueFactory(args=["https://example.com/b"])

        runner_a = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        runner_b = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        runner_a.pid, runner_b.pid = 111111, 222222

        a_paused = threading.Event()
        b_done = threading.Event()
        real_expand = runner_module.expand_task_type_aliases
        results: dict[str, object] = {}

        def slow_expand(types):
            # Worker A has read the busy task types and is about to claim; give worker B the chance to claim now.
            if threading.current_thread().name == "worker-a":
                a_paused.set()
                b_done.wait(timeout=3)
            return real_expand(types)

        def run(name, runner):
            try:
                results[name] = runner.claim_next_job()
            finally:
                if name == "worker-b":
                    b_done.set()
                connection.close()

        with patch.object(runner_module, "expand_task_type_aliases", slow_expand):
            thread_a = threading.Thread(name="worker-a", target=run, args=("worker-a", runner_a))
            thread_b = threading.Thread(name="worker-b", target=run, args=("worker-b", runner_b))
            thread_a.start()
            self.assertTrue(a_paused.wait(timeout=5))
            thread_b.start()
            thread_a.join()
            thread_b.join()

        claimed = [r for r in results.values() if r is not None]
        self.assertEqual(len(claimed), 1, results)
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).count(), 1)
