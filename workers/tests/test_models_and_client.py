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
        self.assertEqual(hydrated.providers["panda"].proxies, {"http": "http://proxy.test:8080", "https": "http://proxy.test:8080"})
        self.assertEqual(hydrated.providers["panda"].wait_timer, 20)
        self.assertEqual(hydrated.providers["panda"].stop_page_number, 3)
        self.assertTrue(hydrated.providers["panda"].use_ex_for_fjord)
        self.assertTrue(hydrated.pushover.enable)
        self.assertEqual(hydrated.pushover.token, "secret_push_token")
        self.assertTrue(hydrated.update_metadata_mode)

        # Base remains untouched
        self.assertEqual(base_settings.providers["panda"].proxy, "")
        self.assertEqual(base_settings.providers["panda"].proxies, {})
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



