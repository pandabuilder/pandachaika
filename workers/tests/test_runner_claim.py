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




class WorkerRunnerClaimTests(TestCase):

    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.runner = WorkerRunner(self.settings, enable_schedulers=False)

    def test_claim_next_job_atomic(self):
        task1 = client.enqueue(args=["arg1"], task_type="web_crawler", priority=10)
        task2 = client.enqueue(args=["arg2"], task_type="web_crawler", priority=1)

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task2.pk)
        self.assertEqual(claimed.status, TaskQueue.Status.PROCESSING)
        self.assertEqual(claimed.worker_pid, self.runner.pid)

        # While task2 (web_crawler) is still PROCESSING, another web_crawler task (task1) cannot be claimed yet
        self.assertIsNone(self.runner.claim_next_job())

        # Once task2 completes, task1 can be claimed
        claimed.status = TaskQueue.Status.COMPLETED
        claimed.save(update_fields=["status"])

        claimed2 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2.pk, task1.pk)

        # Claim again when empty
        self.assertIsNone(self.runner.claim_next_job())

    def test_claim_different_task_types_in_parallel_while_same_type_queued(self):
        folder_task_low = client.enqueue(args=["/folder/low"], task_type="folder_crawler", priority=10)
        folder_task_high = client.enqueue(args=["/folder/high"], task_type="folder_crawler", priority=2)
        web_task_low = client.enqueue(args=["https://example.com/low"], task_type="web_crawler", priority=10)
        web_task_high = client.enqueue(args=["https://example.com/high"], task_type="web_crawler", priority=5)

        # 1st claim picks highest priority overall: folder_task_high (priority 2)
        claimed_folder = self.runner.claim_next_job()
        self.assertIsNotNone(claimed_folder)
        self.assertEqual(claimed_folder.pk, folder_task_high.pk)
        self.assertEqual(claimed_folder.task_type, "folder_crawler")
        self.assertEqual(claimed_folder.status, TaskQueue.Status.PROCESSING)

        # 2nd claim while folder_crawler is still PROCESSING must skip folder_task_low
        # and pick highest priority from other task_types: web_task_high (priority 5)
        claimed_web = self.runner.claim_next_job()
        self.assertIsNotNone(claimed_web)
        self.assertEqual(claimed_web.pk, web_task_high.pk)
        self.assertEqual(claimed_web.task_type, "web_crawler")
        self.assertEqual(claimed_web.status, TaskQueue.Status.PROCESSING)

        # 3rd claim while both folder_crawler and web_crawler are PROCESSING returns None
        self.assertIsNone(self.runner.claim_next_job())

        # When web_crawler finishes (even while folder_crawler is still PROCESSING),
        # next claim picks web_task_low
        claimed_web.status = TaskQueue.Status.COMPLETED
        claimed_web.save(update_fields=["status"])

        claimed_web_2 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed_web_2)
        self.assertEqual(claimed_web_2.pk, web_task_low.pk)

        # When folder_crawler finishes, next claim picks folder_task_low
        claimed_folder.status = TaskQueue.Status.COMPLETED
        claimed_folder.save(update_fields=["status"])

        claimed_folder_2 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed_folder_2)
        self.assertEqual(claimed_folder_2.pk, folder_task_low.pk)

    def test_cancel_active_task_with_queued_tasks_claims_next_task(self):
        """When an active (PROCESSING) task is cancelled via cancel_by_id,

        the worker must prune the cancelled task from active_jobs and allow
        the next queued task of the same type to be claimed without getting stuck.
        """
        task1 = client.enqueue(args=["https://example.com/task1"], task_type="web_crawler", priority=1)
        task2 = client.enqueue(args=["https://example.com/task2"], task_type="web_crawler", priority=2)

        # 1. First task is claimed and marked PROCESSING
        claimed1 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed1)
        self.assertEqual(claimed1.pk, task1.pk)
        self.assertEqual(claimed1.status, TaskQueue.Status.PROCESSING)

        # In real worker, the runner tracks the claimed job in active_jobs
        with self.runner._jobs_lock:
            self.runner.active_jobs[claimed1.pk] = claimed1
            self.runner.current_job = claimed1

        # 2. While task1 is active in the runner, task2 (same type) cannot be claimed
        self.assertIsNone(self.runner.claim_next_job())

        # 3. User cancels the active task using client.cancel_by_id
        cancelled = client.cancel_by_id(claimed1.pk)
        self.assertTrue(cancelled)

        task1.refresh_from_db()
        self.assertEqual(task1.status, TaskQueue.Status.CANCELLED)
        self.assertIsNotNone(task1.finished_at)

        # 4. Claim again: the runner must prune the cancelled task from active_jobs
        # and claim the next pending task (task2) rather than remaining stuck
        claimed2 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2.pk, task2.pk)
        self.assertEqual(claimed2.status, TaskQueue.Status.PROCESSING)
        self.assertNotIn(claimed1.pk, self.runner.active_jobs)

        # 5. When no more tasks are pending, claim_next_job returns None
        self.assertIsNone(self.runner.claim_next_job())

    def test_cancel_active_task_stuck_check_wakes_runner(self):
        """When an active task is cancelled in the DB, check_stuck_jobs() / prune_inactive_jobs()

        prunes it from active_jobs, fires its cancel_event, and sets _job_finished_event.
        """
        task = client.enqueue(args=["https://example.com/active"], task_type="web_crawler", priority=1)
        claimed = self.runner.claim_next_job()

        with self.runner._jobs_lock:
            self.runner.active_jobs[claimed.pk] = claimed
            self.runner.current_job = claimed
        self.runner._job_finished_event.clear()

        # Task is cancelled in DB (e.g. via workers_action_api or external cancel_by_id)
        client.cancel_by_id(claimed.pk)

        # check_stuck_jobs detects the cancelled task
        self.runner.check_stuck_jobs()
        self.assertNotIn(claimed.pk, self.runner.active_jobs)
        self.assertTrue(self.runner._job_finished_event.is_set())

    def test_active_job_thread_cancelled_aborts_and_next_task_claimed(self):
        """When an actively executing job thread is cancelled via cancel_by_id,

        the running task aborts promptly via TaskCancelledError and the runner
        claims the next queued task.
        """
        import threading
        import time
        from unittest.mock import patch
        from workers.runner import TaskCancelledError

        task1 = client.enqueue(args=["https://example.com/running"], task_type="web_crawler", priority=1)
        task2 = client.enqueue(args=["https://example.com/next_in_queue"], task_type="web_crawler", priority=2)

        claimed1 = self.runner.claim_next_job()
        self.assertEqual(claimed1.pk, task1.pk)

        started = threading.Event()
        aborted = threading.Event()

        def mock_execute(job):
            started.set()
            try:
                while True:
                    time.sleep(0.01)
                    self.runner.touch_job_activity(job.pk)
            except TaskCancelledError:
                aborted.set()

        with patch.object(self.runner, "execute_job", side_effect=mock_execute):
            thread = self.runner.start_job_thread(claimed1)
            self.assertTrue(started.wait(timeout=2.0))

            # Active task is cancelled using cancel_by_id
            self.assertTrue(client.cancel_by_id(claimed1.pk))

            # Claim next job: task2 is claimed immediately
            claimed2 = self.runner.claim_next_job()
            self.assertIsNotNone(claimed2)
            self.assertEqual(claimed2.pk, task2.pk)
            self.assertEqual(claimed2.status, TaskQueue.Status.PROCESSING)

            # Active thread aborts promptly
            self.assertTrue(aborted.wait(timeout=2.0))
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

    def test_parallel_execution_and_folder_crawler_enqueues_web_crawler(self):
        import threading
        from unittest.mock import patch

        folder_started = threading.Event()
        allow_folder_finish = threading.Event()
        web_executed_while_folder_running = []
        enqueued_from_folder = []

        folder_task = client.enqueue(args=["/scan/folder"], task_type="folder_crawler", priority=5)
        web_task = client.enqueue(args=["https://example.com/1"], task_type="web_crawler", priority=10)

        claimed_folder = self.runner.claim_next_job()
        claimed_web = self.runner.claim_next_job()
        self.assertEqual(claimed_folder.pk, folder_task.pk)
        self.assertEqual(claimed_web.pk, web_task.pk)

        orig_enqueue = client.enqueue

        def thread_safe_enqueue(*args, **kwargs):
            enqueued_from_folder.append((args, kwargs))

        def fake_folder_crawl(args):
            folder_started.set()
            # Folder crawler queues a new task back for web_crawler while running
            with patch("workers.client.enqueue", side_effect=thread_safe_enqueue):
                self.runner.settings.workers.web_queue.enqueue_args_list(["https://example.com/from_folder"])
            allow_folder_finish.wait(timeout=5.0)

        def fake_web_crawl(args):
            folder_started.wait(timeout=5.0)
            # Record that web_crawler ran while folder_crawler was still active
            with self.runner._jobs_lock:
                web_executed_while_folder_running.append(folder_task.pk in self.runner.active_jobs)

        with (
            patch("core.local.foldercrawler.FolderCrawler.start_crawling", side_effect=fake_folder_crawl),
            patch("core.web.crawler.WebCrawler.start_crawling", side_effect=fake_web_crawl),
            patch.object(TaskQueue, "refresh_from_db"),
            patch.object(TaskQueue, "save"),
        ):
            t_folder = self.runner.start_job_thread(claimed_folder)
            t_web = self.runner.start_job_thread(claimed_web)

            t_web.join(timeout=5.0)
            self.assertFalse(t_web.is_alive())
            self.assertEqual(web_executed_while_folder_running, [True])

            allow_folder_finish.set()
            t_folder.join(timeout=5.0)
            self.assertFalse(t_folder.is_alive())

        self.assertEqual(len(enqueued_from_folder), 1)
        call_args, call_kwargs = enqueued_from_folder[0]
        orig_enqueue(*call_args, **call_kwargs)

        # Verify the web_crawler task enqueued by folder_crawler is in the web_crawler queue
        pending_web = list(client.get_pending_tasks(task_type="web_crawler"))
        self.assertEqual(len(pending_web), 1)
        self.assertEqual(pending_web[0].args, ["https://example.com/from_folder"])

    def test_webqueue_isolated_to_web_crawler_tasks(self):
        web_queue = WebQueue(self.settings)
        client.enqueue(args=["/folder/1"], task_type="folder_crawler", priority=1)
        client.enqueue(args=[1, 2], task_type="fileinfo_worker", priority=2)
        web_task = client.enqueue(args=["https://example.com/web"], task_type="web_crawler", priority=10)

        # WebQueue only counts and lists web_crawler tasks
        self.assertEqual(web_queue.queue_size(), 1)
        self.assertEqual([t.pk for t in web_queue.queue], [web_task.pk])
        self.assertTrue(web_queue.remove_by_index(0))
        self.assertEqual(web_queue.queue_size(), 0)
        # Other task_types remain pending
        self.assertEqual(client.queue_size(task_type="folder_crawler"), 1)
        self.assertEqual(client.queue_size(task_type="fileinfo_worker"), 1)

    def test_execute_job_success(self):
        from unittest.mock import patch

        task = client.enqueue(args=["arg1"], priority=1)
        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)

        with patch("core.web.crawler.WebCrawler.start_crawling") as mock_crawl:
            self.runner.execute_job(claimed)
            mock_crawl.assert_called_once()

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)
        self.assertIsNotNone(claimed.finished_at)

    def test_execute_job_failure_records_error(self):
        from unittest.mock import patch

        task = client.enqueue(args=["bad_arg"], priority=1)
        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)

        with patch("core.web.crawler.WebCrawler.start_crawling", side_effect=RuntimeError("crawler crash")):
            self.runner.execute_job(claimed)

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.FAILED)
        self.assertEqual(claimed.error_message, "crawler crash")
        self.assertIn("crawler crash", claimed.traceback)
        self.assertIsNotNone(claimed.finished_at)

    def test_taskqueue_properties_for_templates(self):
        task = client.enqueue(
            args=["url", "-wanted", "test_filter"],
            override_options={"redownload": True, "archive_reason": "test"},
        )
        self.assertEqual(task.wanted, ["test_filter"])
        self.assertIn(("redownload", True), task.override_options.items())

    def test_webqueue_enqueue_args_list(self):
        web_queue = WebQueue(self.settings)
        web_queue.enqueue_args_list(
            args=["-wanted", "tag:cat"],
            user=User.objects.create_user("wanteduser", "password"),
            reason="wanted reason",
        )
        self.assertEqual(web_queue.queue_size(), 1)
        item = web_queue.queue[0]
        self.assertIn("-wanted", item.args)
        self.assertIn("tag:cat", item.args)


