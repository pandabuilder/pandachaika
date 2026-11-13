# -*- coding: utf-8 -*-
"""Tasks that can't do their work must fail visibly, and cancellation must reach the running code."""
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.cancellation import TaskCancelledError, ping_activity
from core.base.setup import Settings
from viewer.models import Archive
from workers import client
from workers.models import TaskQueue
from workers.runner import WorkerRunner
from workers.tests.factories import TaskQueueFactory


class RunnerFailureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="runnerfailures", password="password123")
        self.runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)

    def _run(self, **kwargs):
        task = TaskQueueFactory(**kwargs)
        claimed = self.runner.claim_next_job()
        self.assertEqual(claimed.pk, task.pk)
        self.runner.execute_job(claimed)
        task.refresh_from_db()
        return task

    def test_unknown_task_type_fails_instead_of_crawling(self):
        with patch("workers.runner.WebCrawler") as crawler:
            task = self._run(task_type="does_not_exist", args=["https://example.com/"])
        crawler.assert_not_called()
        self.assertEqual(task.status, TaskQueue.Status.FAILED)
        self.assertIn("Unknown task_type", task.error_message)

    def test_gallery_creator_without_parser_fails(self):
        with patch.object(self.runner.settings.provider_context, "get_parsers", return_value=[]):
            task = self._run(
                task_type="gallery_creator",
                args=[],
                options={"provider": "no_such_provider", "galleries_data": [], "gallery_wanted_lists": {}},
            )
        self.assertEqual(task.status, TaskQueue.Status.FAILED)
        self.assertIn("No parser found", task.error_message)

    def test_archive_tasks_without_archive_id_fail(self):
        for task_type in ("clone_plus", "split_archive", "delete_archive"):
            with self.subTest(task_type=task_type):
                task = self._run(task_type=task_type, args=[])
                self.assertEqual(task.status, TaskQueue.Status.FAILED)
                self.assertIn("requires an archive id", task.error_message)

    def test_fileinfo_and_thumbnails_with_empty_args_do_nothing_unless_all(self):
        Archive.objects.create(title="a1", user=self.user)
        with patch("core.workers.archive_work.ArchiveWorker.recalc_all_file_info") as recalc:
            task = self._run(task_type="fileinfo_worker", args=[])
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertEqual(recalc.call_args.args[0].count(), 0)

        with patch("core.workers.archive_work.ArchiveWorker.regenerate_all_thumbs") as thumbs:
            self._run(task_type="thumbnails_worker", args=[])
        self.assertEqual(thumbs.call_args.args[0].count(), 0)

        with patch("core.workers.archive_work.ArchiveWorker.recalc_all_file_info") as recalc:
            self._run(task_type="fileinfo_worker", args=[], options={"all": True})
        self.assertEqual(recalc.call_args.args[0].count(), 1)

        with patch("core.workers.archive_work.ArchiveWorker.regenerate_all_thumbs") as thumbs:
            self._run(task_type="thumbnails_worker", args=[], options={"all": True})
        self.assertEqual(thumbs.call_args.args[0].count(), 1)

    def test_explicitly_named_all_tasks_still_run_on_everything(self):
        Archive.objects.create(title="a1", user=self.user)
        Archive.objects.create(title="a2", user=self.user)
        with patch("core.workers.archive_work.ArchiveWorker.recalc_all_file_info") as recalc:
            self._run(task_type="recalc_all_file_info", args=[])
        self.assertEqual(recalc.call_args.args[0].count(), 2)

    def test_archive_worker_enqueues_explicit_all_and_skips_empty_selection(self):
        from core.workers.archive_work import ArchiveWorker

        get_user_model().objects.create_user(username="enqueuer", password="password123")
        worker = ArchiveWorker(Settings(load_from_disk=False), 4)
        worker.recalc_all_file_info()
        task = TaskQueue.objects.get(task_type="fileinfo_worker")
        self.assertEqual(task.args, [])
        self.assertTrue(task.options.get("all"))

        TaskQueue.objects.all().delete()
        worker.recalc_all_file_info(archives=[])
        worker.regenerate_all_thumbs(archives=[])
        self.assertEqual(TaskQueue.objects.count(), 0)

        worker.regenerate_all_thumbs()
        self.assertTrue(TaskQueue.objects.get(task_type="thumbnails_worker").options.get("all"))

        archive = Archive.objects.create(title="x", user=get_user_model().objects.first())
        worker.recalc_all_file_info(archives=[archive.pk], full=True)
        task = TaskQueue.objects.filter(task_type="fileinfo_worker").latest("pk")
        self.assertEqual(task.args, [archive.pk])
        self.assertTrue(task.options.get("full"))
        self.assertFalse(task.options.get("all"))


class CancellationPropagationTests(TestCase):
    def test_ping_activity_propagates_cancellation_but_not_other_errors(self):
        ping_activity(None)
        ping_activity(MagicMock())

        def broken():
            raise RuntimeError("db hiccup")

        ping_activity(broken)

        def cancelled():
            raise TaskCancelledError("stop")

        with self.assertRaises(TaskCancelledError):
            ping_activity(cancelled)

    def test_timed_out_task_gets_its_cancel_event_set(self):
        import threading
        import time

        settings = Settings(load_from_disk=False)
        settings.worker_task_stall_timeout = 1.0
        runner = WorkerRunner(settings, enable_schedulers=False)
        task = client.enqueue(["https://example.com/a"], task_type="web_crawler")
        job = runner.claim_next_job()
        self.assertEqual(job.pk, task.pk)
        event = threading.Event()
        with runner._jobs_lock:
            runner.active_jobs[job.pk] = job
            runner.active_cancel_events[job.pk] = event
            runner.active_job_activity[job.pk] = time.monotonic() - 100
        self.assertEqual(runner.check_stuck_jobs(), [job.pk])
        self.assertTrue(event.is_set())
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.FAILED)
