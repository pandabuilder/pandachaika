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

    def test_crawler_frontend_options_serialization_and_override(self):
        """Test options originating from the crawler view (admin.py) serialize and override correctly."""
        base_settings = Settings(load_from_disk=False)
        base_settings.downloaders = {"panda_archive": 1, "panda_info": 1, "panda_torrent": 1}
        base_settings.non_current_links_as_deleted = False
        base_settings.replace_metadata = True

        # Simulate user changing options in crawler view
        current_settings = Settings(load_from_disk=False)
        current_settings.downloaders = {"panda_archive": -1, "panda_info": 1, "panda_torrent": -1}
        current_settings.non_current_links_as_deleted = True
        current_settings.replace_metadata = False
        current_settings.archive_reason = "Crawl requested by admin"
        current_settings.gallery_reason = "Crawl requested by admin"

        data = serialize_settings_options(current_settings)
        self.assertTrue(data["non_current_links_as_deleted"])
        self.assertFalse(data["replace_metadata"])
        self.assertEqual(data["archive_reason"], "Crawl requested by admin")
        self.assertEqual(data["gallery_reason"], "Crawl requested by admin")
        self.assertEqual(data["downloaders"]["panda_archive"], -1)
        self.assertEqual(data["downloaders"]["panda_info"], 1)

        hydrated = deserialize_settings_options(base_settings, data)
        self.assertTrue(hydrated.non_current_links_as_deleted)
        self.assertFalse(hydrated.replace_metadata)
        self.assertEqual(hydrated.archive_reason, "Crawl requested by admin")
        self.assertEqual(hydrated.gallery_reason, "Crawl requested by admin")
        self.assertEqual(hydrated.downloaders["panda_archive"], -1)
        self.assertEqual(hydrated.downloaders["panda_info"], 1)

        # Ensure base_settings is not mutated
        self.assertFalse(base_settings.non_current_links_as_deleted)
        self.assertTrue(base_settings.replace_metadata)
        self.assertEqual(base_settings.downloaders["panda_archive"], 1)

    def test_foldercrawler_frontend_options_serialization_and_override(self):
        """Test options originating from folder crawler view serialize and override correctly."""
        base_settings = Settings(load_from_disk=False)
        base_settings.matchers = {"panda_title": 1, "panda_image": -1}
        base_settings.internal_matches_for_non_matches = False

        current_settings = Settings(load_from_disk=False)
        current_settings.matchers = {"panda_title": 5, "panda_image": 2}
        current_settings.internal_matches_for_non_matches = True
        current_settings.archive_source = "folder_import"
        current_settings.archive_details = "import_batch_12"

        data = serialize_settings_options(current_settings)
        self.assertTrue(data["internal_matches_for_non_matches"])
        self.assertEqual(data["archive_source"], "folder_import")
        self.assertEqual(data["archive_details"], "import_batch_12")
        self.assertEqual(data["matchers"]["panda_title"], 5)
        self.assertEqual(data["matchers"]["panda_image"], 2)

        hydrated = deserialize_settings_options(base_settings, data)
        self.assertTrue(hydrated.internal_matches_for_non_matches)
        self.assertEqual(hydrated.archive_source, "folder_import")
        self.assertEqual(hydrated.archive_details, "import_batch_12")
        self.assertEqual(hydrated.matchers["panda_title"], 5)
        self.assertEqual(hydrated.matchers["panda_image"], 2)

        # Base should remain unchanged
        self.assertFalse(base_settings.internal_matches_for_non_matches)
        self.assertEqual(base_settings.matchers["panda_title"], 1)

    def test_provider_and_subsettings_serialization_and_override(self):
        """Test complete serialization of provider settings and sub-settings objects."""
        from core.providers.panda.settings import OwnSettings as PandaSettings
        base_settings = Settings(load_from_disk=False)
        panda_prov = PandaSettings(base_settings, {})
        panda_prov.proxy = ""
        panda_prov.wait_timer = 5
        base_settings.providers = {"panda": panda_prov}

        current_settings = Settings(load_from_disk=False)
        panda_override = PandaSettings(current_settings, {})
        panda_override.proxy = "http://proxy.test:8080"
        panda_override.wait_timer = 20
        panda_override.stop_page_number = 3
        panda_override.use_ex_for_fjord = True
        current_settings.providers = {"panda": panda_override}
        current_settings.pushover.enable = True
        current_settings.pushover.token = "secret_push_token"
        current_settings.update_metadata_mode = True

        data = serialize_settings_options(current_settings)
        self.assertIn("providers", data)
        self.assertEqual(data["providers"]["panda"]["proxy"], "http://proxy.test:8080")
        self.assertEqual(data["providers"]["panda"]["wait_timer"], 20)
        self.assertEqual(data["providers"]["panda"]["stop_page_number"], 3)
        self.assertTrue(data["providers"]["panda"]["use_ex_for_fjord"])
        self.assertTrue(data["pushover"]["enable"])
        self.assertEqual(data["pushover"]["token"], "secret_push_token")
        self.assertTrue(data["update_metadata_mode"])

        hydrated = deserialize_settings_options(base_settings, data)
        self.assertEqual(hydrated.providers["panda"].proxy, "http://proxy.test:8080")
        self.assertEqual(hydrated.providers["panda"].wait_timer, 20)
        self.assertEqual(hydrated.providers["panda"].stop_page_number, 3)
        self.assertTrue(hydrated.providers["panda"].use_ex_for_fjord)
        self.assertTrue(hydrated.pushover.enable)
        self.assertEqual(hydrated.pushover.token, "secret_push_token")
        self.assertTrue(hydrated.update_metadata_mode)

        # Base remains untouched
        self.assertEqual(base_settings.providers["panda"].proxy, "")
        self.assertEqual(base_settings.providers["panda"].wait_timer, 5)

    def test_sensitive_attributes_not_serialized(self):
        """Verify sensitive credentials and paths are never included in serialized data."""
        settings = Settings(load_from_disk=False)
        settings.django_secret_key = "super_secret_key"
        settings.database = {"postgresql_password": "db_password"}
        settings.MEDIA_ROOT = "/secret/path"
        settings.ftps = {"passwd": "ftp_password"}
        settings.torrent = {"pass": "torrent_password"}
        settings.remote_site = {"user_token": "token123"}
        settings.config = {"database": {"postgresql_password": "db_password"}}

        data = serialize_settings_options(settings)
        for sensitive_key in ("django_secret_key", "database", "MEDIA_ROOT", "ftps", "torrent", "remote_site", "config"):
            self.assertNotIn(sensitive_key, data)

    def test_dict_options_dynamic_override(self):
        """Verify that passing a dict of override options works dynamically."""
        base_settings = Settings(load_from_disk=False)
        base_settings.non_current_links_as_deleted = False

        dict_options = {
            "non_current_links_as_deleted": True,
            "downloaders": {"panda_info": 10},
            "custom_flag": "test_value",
        }
        serialized = serialize_settings_options(dict_options)
        self.assertTrue(serialized["non_current_links_as_deleted"])

        hydrated = deserialize_settings_options(base_settings, serialized)
        self.assertTrue(hydrated.non_current_links_as_deleted)
        self.assertEqual(hydrated.downloaders["panda_info"], 10)
        self.assertEqual(getattr(hydrated, "custom_flag"), "test_value")



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
        import threading
        from core.base.utilities import thread_exists, thread_exists_in_db

        # Initially empty in DB
        self.assertFalse(thread_exists_in_db("fileinfo_worker"))
        self.assertFalse(thread_exists_in_db("recalc_all_file_info"))
        self.assertFalse(thread_exists_in_db("folder_crawler"))

        # When a fileinfo task is pending
        t1 = client.enqueue(task_type="fileinfo_worker", args=[])
        self.assertTrue(thread_exists_in_db("fileinfo_worker"))
        self.assertTrue(thread_exists_in_db("recalc_all_file_info"))
        self.assertFalse(thread_exists_in_db("folder_crawler"))

        # Local thread check does not see DB task
        self.assertFalse(thread_exists("fileinfo_worker"))

        # When completed, thread_exists_in_db returns False
        t1.status = TaskQueue.Status.COMPLETED
        t1.save()
        self.assertFalse(thread_exists_in_db("fileinfo_worker"))
        self.assertFalse(thread_exists_in_db("recalc_all_file_info"))

        # Local thread check detects actual running thread
        stop_event = threading.Event()
        dummy_thread = threading.Thread(name="test_local_thread_check", target=stop_event.wait, args=(5.0,))
        dummy_thread.daemon = True
        self.assertFalse(thread_exists("test_local_thread_check"))
        dummy_thread.start()
        try:
            self.assertTrue(thread_exists("test_local_thread_check"))
        finally:
            stop_event.set()
            dummy_thread.join(timeout=2.0)
        self.assertFalse(thread_exists("test_local_thread_check"))

    def test_worker_context_attributes(self):
        from core.workers.archive_work import ArchiveWorker
        from core.workers.holder import WorkerContext

        context = WorkerContext()
        self.assertIsNone(context.archive_worker)
        self.assertIsNone(context.folder_crawler)

        context.start_workers(self.settings)
        self.assertIsNotNone(context.archive_worker)
        self.assertIsInstance(context.archive_worker, ArchiveWorker)

    def test_enqueue_matching_task_helper(self):
        from unittest.mock import patch
        from viewer.models import Archive, WantedGallery, GalleryMatchGroup
        from viewer.utils.matching import (
            generate_possible_matches_for_archives,
            create_matches_wanted_galleries_from_providers,
            create_matches_wanted_galleries_from_providers_internal,
            generate_possible_matches_for_gallery_match_groups,
        )

        a1 = Archive.objects.create(title="Match Test 1", crc32="99999991", user=self.user)
        a2 = Archive.objects.create(title="Match Test 2", crc32="99999992", user=self.user)

        # 1. Enqueue matching task for archives
        task1 = client.enqueue_matching_task(
            task_type="match_unmatched_worker",
            args=Archive.objects.filter(pk__in=[a1.pk, a2.pk]),
            matching_kwargs={"cutoff": 0.5, "filters": ("panda",)},
            user=self.user,
        )
        self.assertEqual(task1.task_type, "match_unmatched_worker")
        self.assertEqual(set(task1.args), {a1.pk, a2.pk})
        self.assertEqual(task1.options.get("matching_kwargs", {}).get("filters"), ["panda"])
        self.assertEqual(task1.user, self.user)

        claimed1 = self.runner.claim_next_job()
        self.assertEqual(claimed1.pk, task1.pk)
        with patch("viewer.utils.matching.generate_possible_matches_for_archives") as mock_match:
            self.runner.execute_job(claimed1)
            mock_match.assert_called_once()
        claimed1.refresh_from_db()
        self.assertEqual(claimed1.status, TaskQueue.Status.COMPLETED)

        # 2. Enqueue web search worker for wanted galleries
        w1 = WantedGallery.objects.create(title="Wanted 1")
        task2 = client.enqueue_matching_task(
            task_type="web_search_worker",
            args=[w1],
            provider="panda",
            matching_kwargs={"cutoff": 0.4, "max_matches": 5},
            user=self.user,
        )
        self.assertEqual(task2.task_type, "web_search_worker")
        self.assertEqual(task2.args, [w1.pk])
        self.assertEqual(task2.options.get("provider"), "panda")

        claimed2 = self.runner.claim_next_job()
        self.assertEqual(claimed2.pk, task2.pk)
        with patch("viewer.utils.matching.create_matches_wanted_galleries_from_providers") as mock_ws:
            self.runner.execute_job(claimed2)
            mock_ws.assert_called_once()
        claimed2.refresh_from_db()
        self.assertEqual(claimed2.status, TaskQueue.Status.COMPLETED)


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
        req = factory.get("/tools/force_run_timed_dl")
        req.user = user
        req.META["HTTP_REFERER"] = "/tools/"
        self.settings.workers.start_workers(self.settings, start_threads=False)
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp = tools(req, "force_run_timed_dl")
            self.assertEqual(resp.status_code, 302)
            st = SchedulerState.objects.get(name="post_downloader")
            self.assertEqual(st.command_requested, "force_run")

        # Test force_run_timed_auto_wanted
        req2 = factory.get("/tools/force_run_timed_auto_wanted")
        req2.user = user
        req2.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp2 = tools(req2, "force_run_timed_auto_wanted")
            self.assertEqual(resp2.status_code, 302)
            st2 = SchedulerState.objects.get(name="auto_wanted")
            self.assertEqual(st2.command_requested, "force_run")

        # Test force_archive_download_queue
        req3 = factory.get("/tools/force_archive_download_queue")
        req3.user = user
        req3.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp3 = tools(req3, "force_archive_download_queue")
            self.assertEqual(resp3.status_code, 302)
            st3 = SchedulerState.objects.get(name="archive_download_queue")
            self.assertEqual(st3.command_requested, "force_run")

        # Test start_archive_download_queue
        req4 = factory.get("/tools/start_archive_download_queue")
        req4.user = user
        req4.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp4 = tools(req4, "start_archive_download_queue")
            self.assertEqual(resp4.status_code, 302)
            st4 = SchedulerState.objects.get(name="archive_download_queue")
            self.assertEqual(st4.command_requested, "start")

        # Test stop_archive_download_queue
        req5 = factory.get("/tools/stop_archive_download_queue")
        req5.user = user
        req5.META["HTTP_REFERER"] = "/tools/"
        with patch("viewer.views.admin.crawler_settings", self.settings):
            resp5 = tools(req5, "stop_archive_download_queue")
            self.assertEqual(resp5.status_code, 302)
            st5 = SchedulerState.objects.get(name="archive_download_queue")
            self.assertEqual(st5.command_requested, "stop")


class ArchiveDownloadQueueTests(TestCase):

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.mkdtemp()
        self.user, _ = User.objects.get_or_create(id=1, defaults={"username": "testarchiveuser", "password": "password123"})
        self.settings = Settings(load_from_disk=False)
        self.settings.MEDIA_ROOT = self.temp_dir
        self.settings.queue_archive_downloads = True
        self.settings.parallel_archive_downloaders = 2

    def tearDown(self):
        import shutil
        import os
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_provider_rate_limit_slot_acquisition(self):
        from core.base.utilities import acquire_provider_rate_limit_slot
        from workers.models import ProviderRateLimit

        # Clean slate
        ProviderRateLimit.objects.filter(name="test_prov_slot").delete()

        # Slot 1
        res1 = acquire_provider_rate_limit_slot("test_prov_slot", provider_calls=2, provider_limit=1.0, url="http://test.com/1")
        self.assertTrue(res1)
        # Slot 2
        res2 = acquire_provider_rate_limit_slot("test_prov_slot", provider_calls=2, provider_limit=1.0, url="http://test.com/2")
        self.assertTrue(res2)

        rl_entry = ProviderRateLimit.objects.get(name="test_prov_slot")
        self.assertEqual(len(rl_entry.call_timestamps), 2)
        self.assertEqual(rl_entry.calls, 2)
        self.assertEqual(rl_entry.period, 1.0)

    def test_download_event_querysets_and_status(self):
        from viewer.models import DownloadEvent

        ev_queued = DownloadEvent.objects.create(
            name="dl1",
            method="archive",
            status=DownloadEvent.Status.QUEUED,
            download_url="http://example.com/1.zip",
        )
        ev_progress = DownloadEvent.objects.create(
            name="dl2",
            method="archive",
            status=DownloadEvent.Status.IN_PROGRESS,
            download_url="http://example.com/2.zip",
        )
        ev_completed = DownloadEvent.objects.create(
            name="dl3",
            method="archive",
            status=DownloadEvent.Status.COMPLETED,
            completed=True,
        )

        queued_ids = list(DownloadEvent.objects.queued().values_list("id", flat=True))
        downloading_ids = list(DownloadEvent.objects.downloading().values_list("id", flat=True))
        in_prog_ids = list(DownloadEvent.objects.in_progress().values_list("id", flat=True))

        self.assertIn(ev_queued.id, queued_ids)
        self.assertNotIn(ev_progress.id, queued_ids)
        self.assertNotIn(ev_completed.id, queued_ids)

        self.assertIn(ev_progress.id, downloading_ids)
        self.assertNotIn(ev_queued.id, downloading_ids)

        self.assertIn(ev_queued.id, in_prog_ids)
        self.assertIn(ev_progress.id, in_prog_ids)
        self.assertNotIn(ev_completed.id, in_prog_ids)

    def test_archive_download_queue_claim_concurrency_and_priority(self):
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        # Setup mock provider settings: chaika max 1, nexus max 2
        self.settings.parallel_archive_downloaders = 3
        chaika_settings = type("MockProvSettings", (), {"max_parallel_archive_downloads": 1})()
        nexus_settings = type("MockProvSettings", (), {"max_parallel_archive_downloads": 2})()
        self.settings.providers = {"chaika": chaika_settings, "nexus": nexus_settings}

        adq = ArchiveDownloadQueue(self.settings)

        # Chaika 1 (priority 10), Chaika 2 (priority 20 - higher)
        c1 = DownloadEvent.objects.create(
            name="c1",
            method="archive",
            provider="chaika",
            priority=10,
            status=DownloadEvent.Status.QUEUED,
            download_url="http://chaika.moe/1.zip",
        )
        c2 = DownloadEvent.objects.create(
            name="c2",
            method="archive",
            provider="chaika",
            priority=20,
            status=DownloadEvent.Status.QUEUED,
            download_url="http://chaika.moe/2.zip",
        )
        # Nexus 1 and 2
        n1 = DownloadEvent.objects.create(
            name="n1",
            method="archive",
            provider="nexus",
            priority=10,
            status=DownloadEvent.Status.QUEUED,
            download_url="http://nexus.moe/1.zip",
        )
        n2 = DownloadEvent.objects.create(
            name="n2",
            method="archive",
            provider="nexus",
            priority=10,
            status=DownloadEvent.Status.QUEUED,
            download_url="http://nexus.moe/2.zip",
        )

        # Claim up to 3 slots
        claimed = adq.claim_next_events(3)
        self.assertEqual(len(claimed), 3)

        # Higher priority chaika (c2) should be claimed, c1 should be skipped because chaika max is 1
        self.assertIn(c2.id, claimed)
        self.assertNotIn(c1.id, claimed)
        # Both nexus events should be claimed because nexus max is 2
        self.assertIn(n1.id, claimed)
        self.assertIn(n2.id, claimed)

        # Verify DB state of claimed events
        c2.refresh_from_db()
        self.assertEqual(c2.status, DownloadEvent.Status.IN_PROGRESS)
        self.assertIsNotNone(c2.started_date)
        self.assertIsNotNone(c2.worker_pid)
        self.assertEqual(c2.attempts, 1)

    def test_archive_download_worker_successful_download(self):
        import io
        import os
        import uuid
        import zipfile
        from PIL import Image
        from unittest.mock import MagicMock, patch
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import Archive, DownloadEvent

        # Create dummy valid zip with a real valid JPEG
        img = Image.new("RGB", (10, 10), color="red")
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format="JPEG")
        valid_jpg_bytes = img_byte_arr.getvalue()

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("001.jpg", valid_jpg_bytes)
        zip_bytes = zip_buf.getvalue()

        zip_name = f"test_download_{uuid.uuid4().hex[:8]}.zip"
        archive = Archive.objects.create(
            title="Test Gallery",
            zipped=zip_name,
            crc32="",
            user=self.user,
        )

        target_path = archive.zipped.path

        event = DownloadEvent.objects.create(
            name="Test Gallery Event",
            method="archive",
            provider="chaika",
            download_id=target_path,
            download_url="http://example.com/archive.zip",
            archive=archive,
            status=DownloadEvent.Status.QUEUED,
            request_options={"rate_limit_name": "chaika_web", "rate_limit_calls": 1, "rate_limit_period": 0.01},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": str(len(zip_bytes))}
        mock_resp.iter_content.return_value = [zip_bytes]

        adq = ArchiveDownloadQueue(self.settings)

        with patch("core.workers.archive_download_queue.request_by_provider") as mock_req_prov:
            mock_web_request = MagicMock(return_value=mock_resp)
            mock_req_prov.return_value = mock_web_request

            adq._download_worker(event.id)

        event.refresh_from_db()
        self.assertEqual(event.status, DownloadEvent.Status.COMPLETED)
        self.assertTrue(event.completed)
        self.assertEqual(event.progress, 100)

        # Verify archive was updated with CRC32 and fileinfo
        archive.refresh_from_db()
        self.assertTrue(bool(archive.crc32))
        self.assertGreater(archive.filesize, 0)
        self.assertEqual(archive.filecount, 1)
        self.assertTrue(bool(archive.thumbnail.name))
        self.assertIsNotNone(archive.thumbnail_width)
        self.assertIsNotNone(archive.thumbnail_height)
        if archive.thumbnail and os.path.exists(archive.thumbnail.path):
            os.remove(archive.thumbnail.path)
        self.assertTrue(os.path.exists(target_path))
        if os.path.exists(target_path):
            os.remove(target_path)

    def test_archive_download_worker_retry_then_fail(self):
        from unittest.mock import MagicMock, patch
        import requests
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        import os
        target_path = os.path.join(self.temp_dir, "test_retry.zip")

        event = DownloadEvent.objects.create(
            name="Test Retry Event",
            method="archive",
            provider="chaika",
            download_id=target_path,
            download_url="http://example.com/fail.zip",
            status=DownloadEvent.Status.QUEUED,
            attempts=0,
            max_attempts=2,
            request_options={"rate_limit_name": "chaika_web"},
        )

        adq = ArchiveDownloadQueue(self.settings)

        with patch("core.workers.archive_download_queue.request_by_provider") as mock_req_prov:
            mock_web_request = MagicMock(side_effect=requests.exceptions.ConnectionError("Connection refused"))
            mock_req_prov.return_value = mock_web_request

            # First attempt (attempts will become 1, less than max_attempts 2)
            event.status = DownloadEvent.Status.IN_PROGRESS
            event.attempts = 1
            event.save()
            adq._download_worker(event.id)

            event.refresh_from_db()
            self.assertEqual(event.status, DownloadEvent.Status.QUEUED)
            self.assertIn("Connection refused", event.error_message)

            # Second attempt (attempts reaches max_attempts 2)
            event.status = DownloadEvent.Status.IN_PROGRESS
            event.attempts = 2
            event.save()
            adq._download_worker(event.id)

            event.refresh_from_db()
            self.assertEqual(event.status, DownloadEvent.Status.FAILED)
            self.assertTrue(event.failed)
            self.assertTrue(event.completed)

    def test_cleanup_dead_workers_reverts_orphaned_archive_downloads(self):
        from datetime import timedelta
        import django.utils.timezone as django_tz
        from workers.models import WorkerProcess
        from viewer.models import DownloadEvent

        dead_worker = WorkerProcess.objects.create(
            pid=999999,
            hostname="deadhost",
            status="running",
            last_heartbeat=django_tz.now() - timedelta(minutes=10),
        )

        orphaned_event = DownloadEvent.objects.create(
            name="Orphaned Event",
            method="archive",
            provider="chaika",
            download_url="http://example.com/orphan.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=999999,
        )

        WorkerProcess.cleanup_dead_workers(threshold_seconds=30)

        dead_worker.refresh_from_db()
        self.assertEqual(dead_worker.status, "stopped")

        orphaned_event.refresh_from_db()
        self.assertEqual(orphaned_event.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(orphaned_event.worker_pid)

    def test_download_progress_checker_skips_queued_archives(self):
        from core.workers.download_progress import DownloadProgressChecker
        from viewer.models import Archive, DownloadEvent

        archive_pending = Archive.objects.create(
            title="Pending Archive",
            zipped="pending.zip",
            crc32="",
            user=self.user,
        )

        queued_event = DownloadEvent.objects.create(
            name="Queued Event",
            method="archive",
            provider="chaika",
            download_url="http://example.com/pending.zip",
            archive=archive_pending,
            status=DownloadEvent.Status.QUEUED,
            completed=False,
        )

        checker = DownloadProgressChecker(self.settings)
        # Run one check cycle via the logic inside check
        download_events = list(DownloadEvent.objects.in_progress().filter(id=queued_event.id))
        self.assertEqual(len(download_events), 1)

        # ARCHIVE_METHODS loop
        for ev in download_events:
            if ev.status == DownloadEvent.Status.QUEUED:
                continue
            if ev.archive and ev.archive.crc32:
                ev.finish_download()
                ev.save()

        queued_event.refresh_from_db()
        self.assertFalse(queued_event.completed)
        self.assertEqual(queued_event.status, DownloadEvent.Status.QUEUED)

    def test_download_monitor_view_html_and_json(self):
        import json
        from django.contrib.auth.models import Permission
        from django.test import RequestFactory
        from viewer.models import DownloadEvent
        from viewer.views.collaborators import download_history

        # Grant download_history permission
        perm = Permission.objects.filter(codename="download_history").first()
        if perm:
            self.user.user_permissions.add(perm)

        # Create sample events
        ev_queued = DownloadEvent.objects.create(
            name="https://example.com/1.zip",
            method="archive",
            status=DownloadEvent.Status.QUEUED,
            progress=0.0,
            total_size=1048576,
        )
        ev_active = DownloadEvent.objects.create(
            name="https://example.com/2.zip",
            method="archive",
            status=DownloadEvent.Status.IN_PROGRESS,
            progress=45.5,
            total_size=2097152,
        )
        ev_done = DownloadEvent.objects.create(
            name="https://example.com/3.zip",
            method="archive",
            status=DownloadEvent.Status.COMPLETED,
            completed=True,
            progress=100.0,
            total_size=5242880,
        )

        factory = RequestFactory()

        # 1. HTML view test
        req_html = factory.get("/downloads/")
        req_html.user = self.user
        resp_html = download_history(req_html)
        self.assertEqual(resp_html.status_code, 200)
        content = resp_html.content.decode("utf-8")
        self.assertIn("Download Monitor", content)
        self.assertIn("id=\"auto-refresh\"", content)
        self.assertIn("id=\"download-table\"", content)

        # 2. JSON view test
        req_json = factory.get("/downloads/?json=1")
        req_json.user = self.user
        resp_json = download_history(req_json)
        self.assertEqual(resp_json.status_code, 200)
        data = json.loads(resp_json.content.decode("utf-8"))
        self.assertIn("results", data)
        self.assertIn("count", data)
        self.assertEqual(data["count"], 2)  # default filter is in_progress (queued + active)
        result_ids = [r["id"] for r in data["results"]]
        self.assertIn(ev_queued.id, result_ids)
        self.assertIn(ev_active.id, result_ids)
        self.assertNotIn(ev_done.id, result_ids)

        # Check serialized fields
        active_res = next(r for r in data["results"] if r["id"] == ev_active.id)
        self.assertEqual(active_res["progress"], 45.5)
        self.assertEqual(active_res["status"], "in_progress")
        self.assertEqual(active_res["total_size"], 2097152)

        # 3. Filter status=queued
        req_q = factory.get("/downloads/?json=1&status=queued")
        req_q.user = self.user
        resp_q = download_history(req_q)
        data_q = json.loads(resp_q.content.decode("utf-8"))
        self.assertEqual(data_q["count"], 1)
        self.assertEqual(data_q["results"][0]["id"], ev_queued.id)

        # 4. Filter status=downloading
        req_dl = factory.get("/downloads/?json=1&status=downloading")
        req_dl.user = self.user
        resp_dl = download_history(req_dl)
        data_dl = json.loads(resp_dl.content.decode("utf-8"))
        self.assertEqual(data_dl["count"], 1)
        self.assertEqual(data_dl["results"][0]["id"], ev_active.id)

    def test_default_queue_archive_downloads_settings(self):
        default_settings = Settings(load_from_disk=False)
        self.assertTrue(default_settings.queue_archive_downloads)
        self.assertEqual(default_settings.parallel_archive_downloaders, 1)

    def test_should_queue_archive_download_precedence(self):
        from types import SimpleNamespace
        from core.base.setup_utilities import GeneralUtils
        from core.downloaders.handlers import BaseDownloader

        class DummyDownloader(BaseDownloader):
            provider = "test_prov"

        self.settings.providers["test_prov"] = SimpleNamespace(queue_archive_downloads=None)
        gen_utils = GeneralUtils(self.settings)
        dl = DummyDownloader(self.settings, gen_utils)

        # Global True
        self.settings.queue_archive_downloads = True
        self.settings.parallel_archive_downloaders = 1
        self.assertTrue(dl.should_queue_archive_download())

        # Global False should NOT be overridden by parallel_archive_downloaders > 0
        self.settings.queue_archive_downloads = False
        self.settings.parallel_archive_downloaders = 1
        self.assertFalse(dl.should_queue_archive_download())

        # Provider override: False overrides global True
        self.settings.queue_archive_downloads = True
        prov_settings = SimpleNamespace(queue_archive_downloads=False)
        self.assertFalse(dl.should_queue_archive_download(provider_settings=prov_settings))

        # Provider override: True overrides global False
        self.settings.queue_archive_downloads = False
        prov_settings_true = SimpleNamespace(queue_archive_downloads=True)
        self.assertTrue(dl.should_queue_archive_download(provider_settings=prov_settings_true))

    def test_crawler_cli_queue_archive_downloads_flags(self):
        from core.web.crawler import WebCrawler

        crawler = WebCrawler(self.settings)

        # --no-queue-archive-downloads
        args = crawler.get_args(["--no-queue-archive-downloads", "https://example.com"])
        self.assertFalse(args.queue_archive_downloads)

        # -nqad
        args_short = crawler.get_args(["-nqad", "https://example.com"])
        self.assertFalse(args_short.queue_archive_downloads)

        # --queue-archive-downloads
        args_qad = crawler.get_args(["--queue-archive-downloads", "https://example.com"])
        self.assertTrue(args_qad.queue_archive_downloads)

        # -qad
        args_qad_short = crawler.get_args(["-qad", "https://example.com"])
        self.assertTrue(args_qad_short.queue_archive_downloads)

        args_default = crawler.get_args(["https://example.com"])
        self.assertIsNone(args_default.queue_archive_downloads)


class WorkersDashboardAPITests(TestCase):

    def setUp(self):
        self.staff_user = User.objects.create_superuser(
            username="dash_admin", email="dash_admin@example.com", password="password123"
        )
        self.regular_user = User.objects.create_user(
            username="dash_user", email="dash_user@example.com", password="password123"
        )
        from django.test import Client
        self.staff_client = Client()
        self.staff_client.force_login(self.staff_user)

        self.user_client = Client()
        self.user_client.force_login(self.regular_user)

        self.anon_client = Client()

    def test_workers_status_api_permissions(self):
        # Anonymous user should be redirected to login
        res_anon = self.anon_client.get("/api/workers/status/")
        self.assertEqual(res_anon.status_code, 302)

        # Non-staff user should get 403 Forbidden
        res_user = self.user_client.get("/api/workers/status/")
        self.assertEqual(res_user.status_code, 403)

        # Staff user should get 200 OK
        res_staff = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res_staff.status_code, 200)
        data = res_staff.json()
        self.assertIn("summary", data)
        self.assertIn("workers", data)
        self.assertIn("queues", data)
        self.assertIn("schedulers", data)
        self.assertIn("diagram", data)

    def test_workers_status_api_live_data(self):
        from workers.models import WorkerProcess, SchedulerState, TaskQueue

        # Create active worker
        worker = WorkerProcess.objects.create(
            pid=55555,
            hostname="test-node-1",
            worker_type="general",
            status="running",
            last_heartbeat=django_tz.now(),
        )

        # Create processing task assigned to this worker
        task_proc = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/live/1/"],
            status=TaskQueue.Status.PROCESSING,
            worker_pid=worker.pid,
            started_at=django_tz.now(),
        )

        # Create pending task
        TaskQueue.objects.create(
            task_type="fileinfo_worker",
            args=[123],
            status=TaskQueue.Status.PENDING,
        )

        # Create scheduler
        SchedulerState.objects.update_or_create(
            name="timed_downloader",
            defaults={
                "cycle_timer": 15.0,
                "is_running": True,
                "worker_pid": worker.pid,
                "last_heartbeat": django_tz.now(),
            },
        )

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Summary check
        self.assertEqual(data["summary"]["active_workers_count"], 1)
        self.assertEqual(data["summary"]["processing_tasks_count"], 1)
        self.assertEqual(data["summary"]["pending_tasks_count"], 1)

        # Worker check
        matched_worker = next((w for w in data["workers"] if w["pid"] == 55555), None)
        self.assertIsNotNone(matched_worker)
        self.assertEqual(matched_worker["status"], "healthy")
        self.assertEqual(len(matched_worker["current_tasks"]), 1)
        self.assertEqual(matched_worker["current_tasks"][0]["id"], task_proc.pk)

        # Diagram check
        self.assertIn("nodes", data["diagram"])
        self.assertIn("edges", data["diagram"])

    def test_workers_action_api_scheduler_commands(self):
        from workers.models import SchedulerState
        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={"is_running": False},
        )

        # Start scheduler command
        res_start = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_start", "target": "auto_wanted", "timer": "1800"}),
            content_type="application/json",
        )
        self.assertEqual(res_start.status_code, 200)
        self.assertTrue(res_start.json()["success"])
        state = SchedulerState.objects.get(name="auto_wanted")
        self.assertEqual(state.command_requested, "start")
        self.assertEqual(state.command_arg, "1800")

        # Stop scheduler command
        res_stop = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_stop", "target": "auto_wanted"}),
            content_type="application/json",
        )
        self.assertEqual(res_stop.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "stop")

        # Force run command
        res_force = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_force_run", "target": "auto_wanted"}),
            content_type="application/json",
        )
        self.assertEqual(res_force.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "force_run")

    def test_workers_action_api_task_cancel_and_retry(self):
        from workers.models import TaskQueue

        # Cancel task
        pending_task = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/cancel-me/"],
            status=TaskQueue.Status.PENDING,
        )
        res_cancel = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "task_cancel", "target": pending_task.pk}),
            content_type="application/json",
        )
        self.assertEqual(res_cancel.status_code, 200)
        pending_task.refresh_from_db()
        self.assertEqual(pending_task.status, TaskQueue.Status.CANCELLED)

        # Retry failed task
        failed_task = TaskQueue.objects.create(
            task_type="thumbnails_worker",
            args=[999],
            status=TaskQueue.Status.FAILED,
            error_message="Thumbnail generation failed",
        )
        res_retry = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "task_retry", "target": failed_task.pk}),
            content_type="application/json",
        )
        self.assertEqual(res_retry.status_code, 200)
        self.assertTrue(res_retry.json()["success"])
        retried_task = TaskQueue.objects.filter(
            task_type="thumbnails_worker", status=TaskQueue.Status.PENDING
        ).last()
        self.assertIsNotNone(retried_task)
        self.assertIn("Retry of #", retried_task.reason)

    def test_workers_action_api_cleanup_workers(self):
        from workers.models import WorkerProcess
        WorkerProcess.objects.create(
            pid=77777,
            hostname="dead-node",
            status="running",
            last_heartbeat=django_tz.now() - timedelta(seconds=120),
        )
        res_clean = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "cleanup_workers"}),
            content_type="application/json",
        )
        self.assertEqual(res_clean.status_code, 200)
        self.assertTrue(res_clean.json()["success"])
        w = WorkerProcess.objects.get(pid=77777)
        self.assertEqual(w.status, "stopped")

    def test_workers_action_api_worker_delete(self):
        from workers.models import WorkerProcess, SchedulerState

        # Stopped worker can be deleted
        WorkerProcess.objects.create(
            pid=88881,
            hostname="old-node",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(hours=2),
        )
        SchedulerState.objects.update_or_create(
            name="test_worker_sched",
            defaults={"worker_pid": 88881, "is_running": False},
        )
        res = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "worker_delete", "target": 88881}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertFalse(WorkerProcess.objects.filter(pid=88881).exists())
        sched = SchedulerState.objects.get(name="test_worker_sched")
        self.assertIsNone(sched.worker_pid)

        # Actively running healthy worker cannot be deleted directly
        WorkerProcess.objects.create(
            pid=88882,
            hostname="live-node",
            status="running",
            last_heartbeat=django_tz.now(),
        )
        res_live = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "worker_delete", "target": 88882}),
            content_type="application/json",
        )
        self.assertEqual(res_live.status_code, 400)
        self.assertTrue(WorkerProcess.objects.filter(pid=88882).exists())

    def test_workers_action_api_purge_workers(self):
        from workers.models import WorkerProcess

        # Create stopped workers and 1 healthy worker
        WorkerProcess.objects.create(
            pid=99901,
            hostname="stopped-1",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(hours=1),
        )
        WorkerProcess.objects.create(
            pid=99902,
            hostname="stopped-2",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(days=10),
        )
        WorkerProcess.objects.create(
            pid=99903,
            hostname="live-runner",
            status="running",
            last_heartbeat=django_tz.now(),
        )

        res = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "purge_workers"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

        # Stopped workers deleted, healthy runner preserved
        self.assertFalse(WorkerProcess.objects.filter(pid=99901).exists())
        self.assertFalse(WorkerProcess.objects.filter(pid=99902).exists())
        self.assertTrue(WorkerProcess.objects.filter(pid=99903).exists())

    def test_stats_workers_json_content_negotiation(self):
        # GET with ?json=1
        res_query = self.staff_client.get("/stats/workers/?json=1")
        self.assertEqual(res_query.status_code, 200)
        self.assertEqual(res_query["Content-Type"], "application/json")
        self.assertIn("summary", res_query.json())

        # GET with Accept: application/json
        res_header = self.staff_client.get("/stats/workers/", HTTP_ACCEPT="application/json")
        self.assertEqual(res_header.status_code, 200)
        self.assertEqual(res_header["Content-Type"], "application/json")
        self.assertIn("summary", res_header.json())

    def test_workers_status_scheduler_next_run_and_stopped(self):
        from workers.models import SchedulerState

        now = django_tz.now()

        # 1. Stopped scheduler with past next_run
        SchedulerState.objects.update_or_create(
            name="link_monitor_7",
            defaults={
                "display_name": "link_monitor_7",
                "is_running": False,
                "cycle_timer": 900.0,
                "last_run": now - timedelta(hours=2),
                "next_run": now - timedelta(hours=1, minutes=45),
                "last_heartbeat": now - timedelta(hours=2),
            },
        )

        # 2. Active scheduler running a cycle (next_run in past / due now)
        SchedulerState.objects.update_or_create(
            name="auto_updater_panda",
            defaults={
                "display_name": "auto_updater_panda",
                "is_running": True,
                "cycle_timer": 86400.0,
                "last_run": now - timedelta(days=1),
                "next_run": now - timedelta(seconds=5),
                "last_heartbeat": now,
            },
        )

        # 3. Active scheduler with future next_run
        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={
                "display_name": "auto_wanted",
                "is_running": True,
                "cycle_timer": 3600.0,
                "last_run": now - timedelta(minutes=10),
                "next_run": now + timedelta(seconds=500),
                "last_heartbeat": now,
            },
        )

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        sched_map = {s["name"]: s for s in data["schedulers"]}

        # Check stopped scheduler
        s_stopped = sched_map["link_monitor_7"]
        self.assertFalse(s_stopped["is_active"])
        self.assertFalse(s_stopped["is_running"])
        self.assertIsNone(s_stopped["next_run_in_seconds"])
        self.assertEqual(s_stopped["next_run_in_formatted"], "-")
        self.assertEqual(s_stopped["next_run_formatted"], "Stopped")

        # Check active cycle running scheduler
        s_running = sched_map["auto_updater_panda"]
        self.assertTrue(s_running["is_active"])
        self.assertEqual(s_running["next_run_in_seconds"], 0.0)
        self.assertEqual(s_running["next_run_in_formatted"], "Running...")
        self.assertEqual(s_running["next_run_formatted"], "Running cycle")

        # Check future next run scheduler
        s_future = sched_map["auto_wanted"]
        self.assertTrue(s_future["is_active"])
        self.assertIsNotNone(s_future["next_run_in_seconds"])
        self.assertGreater(s_future["next_run_in_seconds"], 490)
        self.assertEqual(s_future["next_run_in_formatted"], "8m 20s")

    def test_workers_status_download_speed_aggregation(self):
        from viewer.models import DownloadEvent
        from workers.models import SchedulerState

        now = django_tz.now()

        # Scheduler with active download speed
        SchedulerState.objects.update_or_create(
            name="archive_download_queue",
            defaults={
                "display_name": "archive_download_queue",
                "is_running": True,
                "cycle_timer": 2.0,
                "last_heartbeat": now,
                "extra_data": {
                    "current_download": [
                        {
                            "filename": "manga_chapter_1.zip",
                            "speed": 5242880.0,  # 5.0 MB/s
                            "downloaded": 10485760,
                            "filesize": 20971520,
                            "index": 1,
                            "total": 1,
                        }
                    ]
                },
            },
        )

        # DownloadEvent in progress for that file
        DownloadEvent.objects.create(
            name="manga_chapter_1",
            download_id="/media/galleries/dls/manga_chapter_1.zip",
            download_url="https://example.com/download/1.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            total_size=20971520,
            progress=50.0,
        )

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Summary download speed check
        self.assertEqual(data["summary"]["active_downloads_count"], 1)
        self.assertEqual(data["summary"]["total_download_speed"], 5242880.0)
        self.assertEqual(data["summary"]["total_download_speed_formatted"], "5.0 MB/s")

        # Downloads list speed check
        self.assertEqual(len(data["downloads"]), 1)
        dl = data["downloads"][0]
        self.assertEqual(dl["speed"], 5242880.0)
        self.assertEqual(dl["speed_formatted"], "5.0 MB/s")


class QueueStallAndTimeoutFixesTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="stalltestuser", password="password123")
        self.settings = Settings(load_from_disk=False)

    def test_claim_next_events_ignores_non_queued_in_progress_and_avoids_head_of_line_blocking(self):
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        self.settings.parallel_archive_downloaders = 1
        panda_settings = type("MockProvSettings", (), {"max_parallel_archive_downloads": 1})()
        chaika_settings = type("MockProvSettings", (), {"max_parallel_archive_downloads": 1})()
        self.settings.providers = {"panda": panda_settings, "chaika": chaika_settings}
        adq = ArchiveDownloadQueue(self.settings)

        # 1. Create an active torrent/hath DownloadEvent (download_url="") for panda
        DownloadEvent.objects.create(
            name="https://e-hentai.org/g/1/abc/",
            method="torrent",
            provider="panda",
            download_id="torrent_hash_123",
            download_url="",
            status=DownloadEvent.Status.IN_PROGRESS,
            completed=False,
        )

        # Queued panda archive download should NOT be blocked by the torrent IN_PROGRESS row
        panda_q1 = DownloadEvent.objects.create(
            name="panda_q1",
            method="archive",
            provider="panda",
            download_url="http://hath.network/archive/1?start=1",
            status=DownloadEvent.Status.QUEUED,
            priority=10,
        )
        claimed_first = adq.claim_next_events(1)
        self.assertEqual(claimed_first, [panda_q1.id])

        # 2. Now panda_q1 is IN_PROGRESS (with download_url != "").
        # Enqueue 10 more panda archive downloads (created first) followed by 5 chaika downloads.
        for i in range(10):
            DownloadEvent.objects.create(
                name=f"panda_queued_{i}",
                method="archive",
                provider="panda",
                download_url=f"http://hath.network/archive/extra_{i}?start=1",
                status=DownloadEvent.Status.QUEUED,
                priority=10,
            )
        chaika_events = [
            DownloadEvent.objects.create(
                name=f"chaika_queued_{i}",
                method="chaika",
                provider="chaika",
                download_url=f"https://panda.chaika.moe/archive/{i}/download/",
                status=DownloadEvent.Status.QUEUED,
                priority=10,
            )
            for i in range(5)
        ]

        # Even though max_to_claim=1 and there are 10 panda items ahead of chaika,
        # claim_next_events(1) must skip blocked provider 'panda' and claim the first 'chaika' item!
        claimed_second = adq.claim_next_events(1)
        self.assertEqual(claimed_second, [chaika_events[0].id])

    def test_register_start_and_reconcile_recover_same_pid_and_stopped_worker_orphans(self):
        import os
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        runner = WorkerRunner(self.settings, enable_schedulers=False)

        # Simulate a Docker restart where a previous run with the SAME PID left an event IN_PROGRESS
        ev_same_pid = DownloadEvent.objects.create(
            name="same_pid_orphan",
            method="archive",
            provider="panda",
            download_url="http://example.com/same_pid.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=runner.pid,
            attempts=2,
            progress=55.0,
        )
        runner.register_start()
        ev_same_pid.refresh_from_db()
        self.assertEqual(ev_same_pid.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(ev_same_pid.worker_pid)
        self.assertEqual(ev_same_pid.attempts, 1)

        # Simulate a stopped WorkerProcess whose event was left IN_PROGRESS
        WorkerProcess.objects.create(
            pid=444444,
            hostname="stopped-host",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(minutes=5),
        )
        ev_stopped = DownloadEvent.objects.create(
            name="stopped_worker_orphan",
            method="chaika",
            provider="chaika",
            download_url="http://example.com/stopped.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=444444,
            attempts=1,
        )
        WorkerProcess.cleanup_dead_workers(threshold_seconds=60)
        ev_stopped.refresh_from_db()
        self.assertEqual(ev_stopped.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(ev_stopped.worker_pid)
        self.assertEqual(ev_stopped.attempts, 0)

        # Simulate an orphan with worker_pid=os.getpid() and no live thread in ArchiveDownloadQueue
        adq = ArchiveDownloadQueue(self.settings)
        ev_local_orphan = DownloadEvent.objects.create(
            name="local_orphan",
            method="chaika",
            provider="chaika",
            download_url="http://example.com/local_orphan.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=os.getpid(),
            attempts=1,
        )
        reconciled = adq.reconcile_orphaned_events()
        self.assertEqual(reconciled, 1)
        ev_local_orphan.refresh_from_db()
        self.assertEqual(ev_local_orphan.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(ev_local_orphan.worker_pid)

    def test_force_run_does_not_abort_active_downloads(self):
        import threading
        from unittest.mock import patch
        from core.downloaders.postdownload import CurrentDownload
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        adq = ArchiveDownloadQueue(self.settings)
        ev = DownloadEvent.objects.create(
            name="active_dl",
            method="chaika",
            provider="chaika",
            download_url="http://example.com/active.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=12345,
        )
        dummy_thread = threading.Thread(target=lambda: None)
        adq._active_downloads[ev.id] = CurrentDownload(filename="active.zip", speed=1000.0, index=1, total=1)
        adq._active_threads[ev.id] = dummy_thread

        with (
            patch.object(adq, "is_running_locally", return_value=True),
            patch.object(adq, "process_queue") as mock_pq,
        ):
            adq.force_run(force_local=True)
            mock_pq.assert_called_once()

        ev.refresh_from_db()
        self.assertEqual(ev.status, DownloadEvent.Status.IN_PROGRESS)
        self.assertIn(ev.id, adq._active_downloads)

    def test_archive_download_stall_watchdog_aborts_stuck_download_and_allows_slow_download(self):
        import threading
        import time
        from unittest.mock import MagicMock
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        self.settings.archive_download_stall_timeout = 10.0
        adq = ArchiveDownloadQueue(self.settings)

        ev_stuck = DownloadEvent.objects.create(
            name="stuck_dl",
            method="archive",
            provider="panda",
            download_id="/tmp/stuck_test.zip",
            download_url="http://example.com/stuck.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=1,
            max_attempts=3,
        )
        ev_slow = DownloadEvent.objects.create(
            name="slow_dl",
            method="chaika",
            provider="chaika",
            download_id="/tmp/slow_test.zip",
            download_url="http://example.com/slow.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=1,
            max_attempts=3,
        )

        stop_threads = threading.Event()
        t_stuck = threading.Thread(target=lambda: stop_threads.wait(5.0), daemon=True)
        t_slow = threading.Thread(target=lambda: stop_threads.wait(5.0), daemon=True)
        t_stuck.start()
        t_slow.start()

        try:
            cancel_stuck = threading.Event()
            cancel_slow = threading.Event()
            resp_stuck = MagicMock()
            resp_slow = MagicMock()

            now_mono = time.monotonic()
            with adq._lock:
                adq._active_threads[ev_stuck.id] = t_stuck
                adq._download_activity[ev_stuck.id] = now_mono - 25.0  # Stalled for 25s (> 10s)
                adq._download_cancel_events[ev_stuck.id] = cancel_stuck
                adq._download_responses[ev_stuck.id] = resp_stuck

                adq._active_threads[ev_slow.id] = t_slow
                adq._download_activity[ev_slow.id] = now_mono - 2.0  # Active 2s ago (< 10s)
                adq._download_cancel_events[ev_slow.id] = cancel_slow
                adq._download_responses[ev_slow.id] = resp_slow

            stalled = adq.check_stalled_downloads()
            self.assertEqual(stalled, [ev_stuck.id])
            self.assertTrue(cancel_stuck.is_set())
            resp_stuck.close.assert_called_once()
            self.assertNotIn(ev_stuck.id, adq._active_threads)

            # Slow download that is actively receiving chunks must remain untouched
            self.assertFalse(cancel_slow.is_set())
            resp_slow.close.assert_not_called()
            self.assertIn(ev_slow.id, adq._active_threads)

            ev_stuck.refresh_from_db()
            self.assertEqual(ev_stuck.status, DownloadEvent.Status.QUEUED)
            self.assertIn("timed out", ev_stuck.error_message)
            self.assertIsNone(ev_stuck.worker_pid)

            ev_slow.refresh_from_db()
            self.assertEqual(ev_slow.status, DownloadEvent.Status.IN_PROGRESS)
        finally:
            stop_threads.set()
            t_stuck.join(timeout=1.0)
            t_slow.join(timeout=1.0)

    def test_worker_runner_task_stall_timeout_marks_failed_and_unblocks_queue(self):
        import os
        import tempfile
        import time
        from viewer.models import DownloadEvent

        self.settings.worker_task_stall_timeout = 10.0
        runner = WorkerRunner(self.settings, enable_schedulers=False)

        task_stuck = client.enqueue(args=["https://example.com/stuck"], task_type="web_crawler", priority=1)
        task_next = client.enqueue(args=["https://example.com/next"], task_type="web_crawler", priority=5)

        claimed = runner.claim_next_job()
        self.assertEqual(claimed.pk, task_stuck.pk)
        with runner._jobs_lock:
            runner.active_jobs[claimed.pk] = claimed
            runner.active_job_activity[claimed.pk] = time.monotonic() - 30.0  # Inactive for 30s (> 10s)

        # Case 1: If an inline download is actively writing to disk, check_stuck_jobs refreshes activity
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"active bytes")
            tmp_path = tmp.name

        try:
            inline_ev = DownloadEvent.objects.create(
                name="inline_active",
                method="archive",
                provider="panda",
                download_id=tmp_path,
                download_url="",
                status=DownloadEvent.Status.IN_PROGRESS,
                completed=False,
            )
            timed_out_while_downloading = runner.check_stuck_jobs()
            self.assertEqual(timed_out_while_downloading, [])
            claimed.refresh_from_db()
            self.assertEqual(claimed.status, TaskQueue.Status.PROCESSING)

            # Case 2: Once no inline download is active and task is stalled > 10s, it times out
            inline_ev.finish_download()
            inline_ev.save()
            with runner._jobs_lock:
                runner.active_job_activity[claimed.pk] = time.monotonic() - 30.0

            timed_out = runner.check_stuck_jobs()
            self.assertEqual(timed_out, [claimed.pk])
            claimed.refresh_from_db()
            self.assertEqual(claimed.status, TaskQueue.Status.FAILED)
            self.assertIn("FAILED (timeout)", claimed.error_message)

            # Next web_crawler task is now unblocked and can be claimed
            next_claimed = runner.claim_next_job()
            self.assertIsNotNone(next_claimed)
            self.assertEqual(next_claimed.pk, task_next.pk)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)









