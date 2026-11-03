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

    def test_archive_worker_extract_enqueue_and_execution(self):
        from unittest.mock import patch
        from core.workers.archive_work import ArchiveWorker
        from viewer.models import EventLog

        a1 = Archive.objects.create(title="Archive Extract 1", crc32="55555555", extracted=False, user=self.user)
        aw = ArchiveWorker(self.settings)

        aw.extract_archives(archives=[a1], resized=True, user=self.user)
        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.task_type, "extract_worker")
        self.assertEqual(task.args, [a1.pk])
        self.assertEqual(task.options.get("resized"), True)
        self.assertEqual(task.user, self.user)
        self.assertTrue(client.is_archive_extracting(a1.pk))

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        self.assertTrue(client.is_archive_extracting(a1.pk))

        def fake_extract(resized=False):
            a1.extracted = True
            a1.save()
            return True

        with patch.object(Archive, "extract", side_effect=fake_extract) as mock_extract:
            self.runner.execute_job(claimed)
            mock_extract.assert_called_once_with(resized=True)

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)
        self.assertFalse(client.is_archive_extracting(a1.pk))
        self.assertTrue(
            EventLog.objects.filter(user=self.user, action="EXPAND_ARCHIVE", object_id=a1.pk, result="success").exists()
        )

    def test_archive_worker_publish_enqueue_and_execution(self):
        from unittest.mock import patch
        from core.workers.archive_work import ArchiveWorker
        from viewer.models import EventLog

        a1 = Archive.objects.create(title="Archive Publish 1", crc32="66666666", public=False, user=self.user)
        aw = ArchiveWorker(self.settings)

        aw.publish_archives(archives=[a1], reason="publish test", user=self.user)
        task = TaskQueue.objects.latest("pk")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.task_type, "publish_worker")
        self.assertEqual(task.args, [a1.pk])
        self.assertEqual(task.reason, "publish test")
        self.assertEqual(task.user, self.user)
        self.assertTrue(client.is_archive_publishing(a1.pk))

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        self.assertTrue(client.is_archive_publishing(a1.pk))

        def fake_set_public(reason="", skip_zip_check=False):
            a1.public = True
            a1.reason = reason
            a1.save()
            return True

        with (
            patch.object(Archive, "test_zip_file") as mock_test_zip,
            patch.object(Archive, "set_public", side_effect=fake_set_public) as mock_set_public,
        ):
            self.runner.execute_job(claimed)
            mock_test_zip.assert_called_once()
            mock_set_public.assert_called_once_with(reason="publish test", skip_zip_check=True)

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)
        self.assertFalse(client.is_archive_publishing(a1.pk))
        self.assertTrue(
            EventLog.objects.filter(
                user=self.user,
                action="PUBLISH_ARCHIVE",
                object_id=a1.pk,
                result="published",
            ).exists()
        )

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


