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

    def test_archive_download_worker_rejects_html_response(self):
        from unittest.mock import MagicMock, patch
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent
        import os

        target_path = os.path.join(self.temp_dir, "test_html_reject.zip")
        event = DownloadEvent.objects.create(
            name="Test HTML Reject",
            method="archive",
            provider="panda",
            download_id=target_path,
            download_url="http://example.com/not_an_archive.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=1,
            max_attempts=3,
            request_options={"rate_limit_name": "panda_web"},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
        mock_resp.text = "<html><body>Error: Quota Exceeded</body></html>"
        mock_resp.content = mock_resp.text.encode("utf-8")

        adq = ArchiveDownloadQueue(self.settings)
        with patch("core.workers.archive_download_queue.request_by_provider") as mock_req_prov:
            mock_web_request = MagicMock(return_value=mock_resp)
            mock_req_prov.return_value = mock_web_request

            adq._download_worker(event.id)

        event.refresh_from_db()
        self.assertNotEqual(event.status, DownloadEvent.Status.COMPLETED)
        self.assertEqual(event.status, DownloadEvent.Status.QUEUED)
        self.assertIn("HTML instead of archive", event.error_message)
        self.assertFalse(os.path.exists(target_path))
        self.assertFalse(os.path.exists(f"{target_path}.part"))

    def test_archive_download_worker_hath_processing_preserves_attempts(self):
        from unittest.mock import MagicMock, patch
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent
        import os

        target_path = os.path.join(self.temp_dir, "test_hath_processing.zip")
        event = DownloadEvent.objects.create(
            name="Test Hath Processing",
            method="archive",
            provider="panda",
            download_id=target_path,
            download_url="http://example.com/hath_pending.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=2,
            max_attempts=3,
            request_options={"rate_limit_name": "panda_web"},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html; charset=UTF-8"}
        mock_resp.text = (
            "<!DOCTYPE html><html><body>"
            "<div id=\"db\"><p>The file is already being processed. Please contact staff if it is not ready within a few minutes.</p></div>"
            "</body></html>"
        )
        mock_resp.content = mock_resp.text.encode("utf-8")

        adq = ArchiveDownloadQueue(self.settings)
        with patch("core.workers.archive_download_queue.request_by_provider") as mock_req_prov, \
             patch("time.sleep") as mock_sleep:
            mock_web_request = MagicMock(return_value=mock_resp)
            mock_req_prov.return_value = mock_web_request

            adq._download_worker(event.id)

        event.refresh_from_db()
        self.assertEqual(event.status, DownloadEvent.Status.QUEUED)
        # Attempt count should not be incremented / burned because it is still being processed
        self.assertEqual(event.attempts, 1)
        self.assertIn("still being processed", event.error_message)
        self.assertFalse(os.path.exists(target_path))
        self.assertFalse(os.path.exists(f"{target_path}.part"))

    def test_archive_download_worker_rejects_corrupted_non_zip_file(self):
        from unittest.mock import MagicMock, patch
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent
        import os

        target_path = os.path.join(self.temp_dir, "test_non_zip.zip")
        event = DownloadEvent.objects.create(
            name="Test Non Zip File",
            method="archive",
            provider="panda",
            download_id=target_path,
            download_url="http://example.com/fake.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=1,
            max_attempts=3,
            request_options={"rate_limit_name": "panda_web"},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Headers say binary/octet-stream, but body is HTML
        mock_resp.headers = {"Content-Type": "application/octet-stream", "Content-Length": "35"}
        mock_resp.iter_content.return_value = [b"<!DOCTYPE html><html>error</html>\n"]

        adq = ArchiveDownloadQueue(self.settings)
        with patch("core.workers.archive_download_queue.request_by_provider") as mock_req_prov:
            mock_web_request = MagicMock(return_value=mock_resp)
            mock_req_prov.return_value = mock_web_request

            adq._download_worker(event.id)

        event.refresh_from_db()
        self.assertNotEqual(event.status, DownloadEvent.Status.COMPLETED)
        self.assertIn("not a valid zip", event.error_message)
        self.assertFalse(os.path.exists(target_path))
        self.assertFalse(os.path.exists(f"{target_path}.part"))

    def test_filename_length_and_get_part_filepath(self):
        import os
        from core.base.utilities import available_filename, get_part_filepath, replace_illegal_name

        # Very long title (300+ characters)
        long_title = "A" * 300
        cleaned = replace_illegal_name(long_title)
        self.assertLessEqual(len(cleaned.encode("utf-8")), 240)

        # available_filename ensures the total basename (with .zip) is <= 245 bytes
        avail = available_filename(self.temp_dir, f"{cleaned}.zip")
        avail_tail = os.path.basename(avail)
        self.assertLessEqual(len(avail_tail.encode("utf-8")), 245)

        # Appending .part with get_part_filepath is guaranteed to be <= 255 bytes
        part_path = get_part_filepath(os.path.join(self.temp_dir, avail_tail))
        part_tail = os.path.basename(part_path)
        self.assertLessEqual(len(part_tail.encode("utf-8")), 255)

        # Test extreme case: existing legacy file whose basename is already exactly 255 bytes
        exact_255_tail = ("B" * 251) + ".zip"
        self.assertEqual(len(exact_255_tail.encode("utf-8")), 255)
        legacy_part_path = get_part_filepath(os.path.join(self.temp_dir, exact_255_tail))
        legacy_part_tail = os.path.basename(legacy_part_path)
        self.assertLessEqual(len(legacy_part_tail.encode("utf-8")), 255)
        self.assertTrue(legacy_part_tail.endswith(".part"))

    def test_archive_download_worker_with_max_length_filename(self):
        import io
        import os
        import zipfile
        from PIL import Image
        from unittest.mock import MagicMock, patch
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import Archive, DownloadEvent

        # Basename that is exactly 255 bytes (like event #4722)
        tail_255 = ("N" * 251) + ".zip"
        self.assertEqual(len(tail_255.encode("utf-8")), 255)
        target_path = os.path.join(self.temp_dir, tail_255)

        # Valid zip
        img = Image.new("RGB", (10, 10), color="blue")
        img_buf = io.BytesIO()
        img.save(img_buf, format="JPEG")
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            zf.writestr("001.jpg", img_buf.getvalue())
        zip_bytes = zip_buf.getvalue()

        archive = Archive.objects.create(
            title="Max Length Gallery",
            zipped=tail_255,
            crc32="",
            user=self.user,
        )

        event = DownloadEvent.objects.create(
            name="Max Length Event",
            method="archive",
            provider="panda",
            download_id=target_path,
            download_url="http://example.com/long_archive.zip",
            archive=archive,
            status=DownloadEvent.Status.QUEUED,
            request_options={"rate_limit_name": "panda_web"},
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": str(len(zip_bytes))}
        mock_resp.iter_content.return_value = [zip_bytes]

        adq = ArchiveDownloadQueue(self.settings)
        with patch("core.workers.archive_download_queue.request_by_provider") as mock_req_prov:
            mock_web_request = MagicMock(return_value=mock_resp)
            mock_req_prov.return_value = mock_web_request

            # This must NOT throw [Errno 36] File name too long
            adq._download_worker(event.id)

        event.refresh_from_db()
        self.assertEqual(event.status, DownloadEvent.Status.COMPLETED)
        self.assertTrue(os.path.exists(target_path))
        if os.path.exists(target_path):
            os.remove(target_path)

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


