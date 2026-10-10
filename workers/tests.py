# -*- coding: utf-8 -*-
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


class WorkersModelAndClientTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="testworkeruser", password="password123")
        self.crawler_settings = Settings(load_from_disk=False)

    def test_enqueue_task(self):
        task = client.enqueue(
            args=["https://example.com/gallery/1/", "-wanted"],
            user=self.user,
            reason="testing",
            event_action="ADD",
            priority=5,
        )
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.args, ["https://example.com/gallery/1/", "-wanted"])
        self.assertEqual(task.user, self.user)
        self.assertEqual(task.reason, "testing")
        self.assertEqual(task.event_action, "ADD")
        self.assertEqual(task.priority, 5)
        self.assertEqual(client.queue_size(), 1)

    def test_queue_ordering_and_remove_by_index(self):
        task1 = client.enqueue(args=["url1"], priority=10)
        task2 = client.enqueue(args=["url2"], priority=1)  # higher priority
        task3 = client.enqueue(args=["url3"], priority=10)

        pending = list(client.get_pending_tasks())
        self.assertEqual(len(pending), 3)
        self.assertEqual(pending[0].pk, task2.pk)
        self.assertEqual(pending[1].pk, task1.pk)
        self.assertEqual(pending[2].pk, task3.pk)

        # Remove task at index 0 (which is task2)
        removed = client.remove_by_index(0)
        self.assertTrue(removed)
        task2.refresh_from_db()
        self.assertEqual(task2.status, TaskQueue.Status.CANCELLED)
        self.assertEqual(client.queue_size(), 2)

    def test_cancel_by_id(self):
        task = client.enqueue(args=["url_to_cancel"])
        self.assertTrue(client.cancel_by_id(task.pk))
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.CANCELLED)

    def test_worker_process_heartbeat(self):
        self.assertFalse(client.is_worker_running())

        WorkerProcess.objects.create(
            pid=99999,
            hostname="testhost",
            status="running",
            last_heartbeat=django_tz.now(),
        )
        self.assertTrue(client.is_worker_running())

        # Cleanup old workers
        WorkerProcess.objects.filter(pid=99999).update(
            last_heartbeat=django_tz.now() - timedelta(seconds=120)
        )
        WorkerProcess.cleanup_dead_workers(threshold_seconds=60)
        proc = WorkerProcess.objects.get(pid=99999)
        self.assertEqual(proc.status, "stopped")
        self.assertFalse(client.is_worker_running(threshold_seconds=30))

    def test_webqueue_interface_compatibility(self):
        web_queue = WebQueue(self.crawler_settings)

        self.assertEqual(web_queue.queue_size(), 0)
        self.assertIsNone(web_queue.current_processing_items)
        self.assertEqual(len(web_queue.queue), 0)

        web_queue.enqueue_args_list(
            args=["https://example.com/test/"],
            user=self.user,
            reason="test reason",
            event_action="DOWNLOAD",
        )

        self.assertEqual(web_queue.queue_size(), 1)
        items = web_queue.queue
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].args, ["https://example.com/test/"])
        self.assertEqual(items[0].reason, "test reason")

        # Test remove_by_index through WebQueue
        self.assertTrue(web_queue.remove_by_index(0))
        self.assertEqual(web_queue.queue_size(), 0)


    def test_settings_serialization(self):
        settings = Settings(load_from_disk=False)
        settings.redownload = True
        settings.archive_reason = "serialized reason"
        settings.archive_user = self.user
        settings.event_action = "ADD_ARCHIVE"
        settings.preserve_extracted = True
        settings.preserve_user_favorites = [{"user": self.user.pk, "favorite_group": 1}]

        data = serialize_settings_options(settings)
        self.assertTrue(data["redownload"])
        self.assertEqual(data["archive_reason"], "serialized reason")
        self.assertEqual(data["user_id"], self.user.pk)
        self.assertEqual(data["event_action"], "ADD_ARCHIVE")
        self.assertTrue(data["preserve_extracted"])
        self.assertEqual(len(data["preserve_user_favorites"]), 1)

        hydrated = deserialize_settings_options(settings, data)
        self.assertTrue(hydrated.redownload)
        self.assertEqual(hydrated.archive_reason, "serialized reason")
        self.assertEqual(hydrated.event_action, "ADD_ARCHIVE")
        self.assertTrue(hydrated.preserve_extracted)
        self.assertEqual(len(hydrated.preserve_user_favorites), 1)


class CallbackMigrationTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="callbackuser", password="password123")
        self.settings = Settings(load_from_disk=False)
        self.parser = BaseParser(self.settings)

    def test_notify_archive_without_callback(self):
        self.settings.archive_user = self.user
        self.settings.archive_reason = "Reason for archive"
        self.settings.event_action = "DOWNLOAD_ARCHIVE"

        # Call notify_archive without callback closure
        self.parser.notify_archive(None, "https://example.com/archive.zip", "success")

        log = EventLog.objects.filter(user=self.user, action="DOWNLOAD_ARCHIVE").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.reason, "Reason for archive")
        self.assertEqual(log.data, "https://example.com/archive.zip")
        self.assertEqual(log.result, "success")

    def test_notify_gallery_without_callback(self):
        self.settings.archive_user = self.user
        self.settings.gallery_reason = "Reason for gallery"
        self.settings.event_action = "UPDATE_METADATA"

        self.parser.notify_gallery(None, "https://example.com/gallery/123", "success")

        log = EventLog.objects.filter(user=self.user, action="UPDATE_METADATA").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.reason, "Reason for gallery")
        self.assertEqual(log.data, "https://example.com/gallery/123")
        self.assertEqual(log.result, "success")

    def test_notify_gallery_updates_submit_entry(self):
        group_uuid = uuid.uuid4()
        entry = GallerySubmitEntry.objects.create(
            submit_url="https://example.com/gallery/submit1",
            submit_group=group_uuid,
        )

        self.settings.submit_group_uuid = str(group_uuid)
        self.parser.notify_gallery(None, "https://example.com/gallery/submit1", "approved")

        entry.refresh_from_db()
        self.assertEqual(entry.submit_result, "approved")

    def test_legacy_callback_still_called_if_provided(self):
        called = []

        def my_callback(obj, url, result):
            called.append((url, result))

        self.parser.archive_callback = my_callback
        self.parser.notify_archive(None, "https://example.com/test", "success")
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0], ("https://example.com/test", "success"))


class WorkerRunnerClaimTests(TestCase):

    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.runner = WorkerRunner(self.settings, enable_schedulers=False)

    def test_claim_next_job_atomic(self):
        task1 = client.enqueue(args=["arg1"], priority=10)
        task2 = client.enqueue(args=["arg2"], priority=1)

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task2.pk)
        self.assertEqual(claimed.status, TaskQueue.Status.PROCESSING)
        self.assertEqual(claimed.worker_pid, self.runner.pid)

        # Claim again gets task1
        claimed2 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2.pk, task1.pk)

        # Claim again when empty
        self.assertIsNone(self.runner.claim_next_job())

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


class OnDemandWorkerTests(TestCase):

    def setUp(self):
        from unittest.mock import patch
        self.user = User.objects.create_user(username="ondemanduser", password="password123")
        self.settings = Settings(load_from_disk=False)
        self.settings.is_worker_process = False  # Simulates web process
        self.runner_settings = Settings(load_from_disk=False)
        self.runner = WorkerRunner(self.runner_settings, enable_schedulers=False)

    def test_folder_crawler_thread_enqueue_and_execution(self):
        from unittest.mock import patch
        from core.local.foldercrawlerthread import FolderCrawlerThread

        self.settings.archive_user = self.user
        self.settings.archive_reason = "testing folder crawl"
        fct = FolderCrawlerThread(
            settings=self.settings,
            argv=["/test/folder"],
        )
        fct.start()

        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.task_type, "folder_crawler")
        self.assertEqual(task.args, ["/test/folder"])
        self.assertEqual(task.user, self.user)
        self.assertEqual(task.reason, "testing folder crawl")

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        with patch("core.local.foldercrawler.FolderCrawler.start_crawling") as mock_crawl:
            self.runner.execute_job(claimed)
            mock_crawl.assert_called_once()
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)

    def test_web_crawler_thread_enqueue_and_execution(self):
        from unittest.mock import patch
        from core.web.crawlerthread import CrawlerThread

        self.settings.archive_user = self.user
        self.settings.archive_reason = "crawl gallery"
        ct = CrawlerThread(
            settings=self.settings,
            argv=["https://example.com/gallery/123"],
        )
        ct.start()

        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.task_type, "web_crawler")
        self.assertEqual(task.args, ["https://example.com/gallery/123"])
        self.assertEqual(task.user, self.user)
        self.assertEqual(task.reason, "crawl gallery")

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        with patch("core.web.crawler.WebCrawler.start_crawling") as mock_crawl:
            self.runner.execute_job(claimed)
            mock_crawl.assert_called_once()
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)

    def test_archive_worker_fileinfo_enqueue_and_execution(self):
        from unittest.mock import patch
        from core.workers.archive_work import ArchiveWorker

        a1 = Archive.objects.create(title="Archive 1", crc32="11111111", user=self.user)
        a2 = Archive.objects.create(title="Archive 2", crc32="22222222", user=self.user)
        aw = ArchiveWorker(self.settings)

        aw.recalc_all_file_info(archives=[a1, a2], user=self.user)
        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.task_type, "fileinfo_worker")
        self.assertEqual(task.args, [a1.pk, a2.pk])
        self.assertEqual(task.user, self.user)

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        with patch.object(ArchiveWorker, "recalc_all_file_info") as mock_info:
            self.runner.execute_job(claimed)
            mock_info.assert_called_once()
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)

    def test_archive_worker_thumbnails_enqueue_and_execution(self):
        from unittest.mock import patch
        from core.workers.archive_work import ArchiveWorker

        a1 = Archive.objects.create(title="Archive 1", crc32="33333333", user=self.user)
        aw = ArchiveWorker(self.settings)

        aw.regenerate_all_thumbs(archives=[a1], user=self.user)
        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.task_type, "thumbnails_worker")
        self.assertEqual(task.args, [a1.pk])
        self.assertEqual(task.user, self.user)

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        with patch.object(ArchiveWorker, "regenerate_all_thumbs") as mock_thumbs:
            self.runner.execute_job(claimed)
            mock_thumbs.assert_called_once()
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)

    def test_matching_thread_targets_enqueue_and_execution(self):
        from unittest.mock import patch
        from viewer.utils.matching import (
            MatchingThread,
            generate_possible_matches_for_archives,
            create_matches_wanted_galleries_from_providers,
            create_matches_wanted_galleries_from_providers_internal,
            generate_possible_matches_for_gallery_match_groups,
        )

        a1 = Archive.objects.create(title="Archive Match", crc32="44444444", user=self.user)

        # 1. match_unmatched_worker
        mt = MatchingThread(
            name="match_unmatched_worker",
            target=generate_possible_matches_for_archives,
            args=([a1],),
            kwargs={"fast_mode": True},
            settings=self.settings,
            user=self.user,
        )
        mt.start()

        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.task_type, "match_unmatched_worker")
        self.assertEqual(task.args, [a1.pk])
        self.assertTrue(task.options.get("matching_kwargs", {}).get("fast_mode"))

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        with patch("viewer.utils.matching.generate_possible_matches_for_archives") as mock_m:
            self.runner.execute_job(claimed)
            mock_m.assert_called_once()
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)

        # 2. web_search_worker
        mt2 = MatchingThread(
            name="web_search_worker",
            target=create_matches_wanted_galleries_from_providers,
            args=([], "panda"),
            kwargs={},
            settings=self.settings,
            user=self.user,
        )
        mt2.start()
        claimed2 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2.task_type, "web_search_worker")
        with patch("viewer.utils.matching.create_matches_wanted_galleries_from_providers") as mock_w:
            self.runner.execute_job(claimed2)
            mock_w.assert_called_once()
        claimed2.refresh_from_db()
        self.assertEqual(claimed2.status, TaskQueue.Status.COMPLETED)

        # 3. wanted_local_search_worker
        mt3 = MatchingThread(
            name="wanted_local_search_worker",
            target=create_matches_wanted_galleries_from_providers_internal,
            args=([], "internal"),
            kwargs={},
            settings=self.settings,
            user=self.user,
        )
        mt3.start()
        claimed3 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed3)
        self.assertEqual(claimed3.task_type, "wanted_local_search_worker")
        with patch("viewer.utils.matching.create_matches_wanted_galleries_from_providers_internal") as mock_l:
            self.runner.execute_job(claimed3)
            mock_l.assert_called_once()
        claimed3.refresh_from_db()
        self.assertEqual(claimed3.status, TaskQueue.Status.COMPLETED)

        # 4. match_unmatched_gallery_groups_worker
        mt4 = MatchingThread(
            name="match_unmatched_gallery_groups_worker",
            target=generate_possible_matches_for_gallery_match_groups,
            args=([],),
            kwargs={"mode": "groups"},
            settings=self.settings,
            user=self.user,
        )
        mt4.start()
        claimed4 = self.runner.claim_next_job()
        self.assertIsNotNone(claimed4)
        self.assertEqual(claimed4.task_type, "match_unmatched_gallery_groups_worker")
        with patch("viewer.utils.matching.generate_possible_matches_for_gallery_match_groups") as mock_g:
            self.runner.execute_job(claimed4)
            mock_g.assert_called_once()
        claimed4.refresh_from_db()
        self.assertEqual(claimed4.status, TaskQueue.Status.COMPLETED)

    def test_thread_exists_integration(self):
        from core.base.utilities import thread_exists

        # Initially empty
        self.assertFalse(thread_exists("fileinfo_worker"))
        self.assertFalse(thread_exists("recalc_all_file_info"))
        self.assertFalse(thread_exists("folder_crawler"))

        # When a fileinfo task is pending
        t1 = client.enqueue(task_type="fileinfo_worker", args=[])
        self.assertTrue(thread_exists("fileinfo_worker"))
        self.assertTrue(thread_exists("recalc_all_file_info"))
        self.assertFalse(thread_exists("folder_crawler"))

        # When completed, thread_exists returns False
        t1.status = TaskQueue.Status.COMPLETED
        t1.save()
        self.assertFalse(thread_exists("fileinfo_worker"))
        self.assertFalse(thread_exists("recalc_all_file_info"))

    def test_worker_context_attributes(self):
        from core.workers.archive_work import ArchiveWorker
        from core.workers.holder import WorkerContext

        context = WorkerContext()
        self.assertIsNone(context.archive_worker)
        self.assertIsNone(context.folder_crawler)

        context.start_workers(self.settings)
        self.assertIsNotNone(context.archive_worker)
        self.assertIsInstance(context.archive_worker, ArchiveWorker)


