import os
import ssl
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import django.utils.timezone as django_tz
from django.test import TestCase

from core.base.comparison import get_list_closer_text_from_list
from core.base.setup import Settings
from core.base.types import GalleryData
from core.downloaders.postdownload import PostDownloader
from core.providers.panda.parsers import Parser as PandaParser
from core.workers.download_progress import DownloadProgressChecker
from viewer.models import Archive, DownloadEvent, FoundGallery, Gallery, Image, Provider, Tag, WantedGallery




class ReconciliationAndLifecycleSyncTest(TestCase):
    def test_filter_by_dl_remote_excludes_dead_events_and_includes_active(self):
        # Archive with active in_progress event
        arch_active = Archive.objects.create(title="arch active", match_type="torrent", crc32="", user=None)
        DownloadEvent.objects.create(name="ev_active", method="torrent", download_id="1", archive=arch_active, status="in_progress")

        # Archive with failed event
        arch_failed = Archive.objects.create(title="arch failed", match_type="torrent", crc32="", user=None)
        DownloadEvent.objects.create(name="ev_failed", method="torrent", download_id="2", archive=arch_failed, status="failed", completed=True, failed=True)
        # Even if un-binned (legacy), filter_by_dl_remote should exclude it
        Archive.objects.filter(pk=arch_failed.pk).update(binned=False)

        # Archive with cancelled event
        arch_cancelled = Archive.objects.create(title="arch cancelled", match_type="torrent", crc32="", user=None)
        DownloadEvent.objects.create(name="ev_cancelled", method="torrent", download_id="3", archive=arch_cancelled, status="cancelled", completed=True, failed=True)
        Archive.objects.filter(pk=arch_cancelled.pk).update(binned=False)

        # Archive with no event (backwards compatibility / manual)
        arch_no_event = Archive.objects.create(title="arch no event", match_type="torrent", crc32="", user=None)

        # Archive with old failed event AND new active event
        arch_requeued = Archive.objects.create(title="arch requeued", match_type="torrent", crc32="", user=None)
        DownloadEvent.objects.create(name="ev_old_fail", method="torrent", download_id="4a", archive=arch_requeued, status="failed", completed=True, failed=True)
        arch_requeued.binned = False
        arch_requeued.save()
        DownloadEvent.objects.create(name="ev_new_active", method="torrent", download_id="4b", archive=arch_requeued, status="in_progress")

        results = list(Archive.objects.filter_by_dl_remote().values_list("pk", flat=True))

        self.assertIn(arch_active.pk, results)
        self.assertIn(arch_no_event.pk, results)
        self.assertIn(arch_requeued.pk, results)
        self.assertNotIn(arch_failed.pk, results)
        self.assertNotIn(arch_cancelled.pk, results)

    def test_download_event_set_as_failed_recycles_empty_archive(self):
        archive = Archive.objects.create(title="empty archive to fail", match_type="torrent", crc32="", user=None)
        event = DownloadEvent.objects.create(name="ev_fail", method="torrent", download_id="f1", archive=archive, status="in_progress")

        self.assertFalse(archive.binned)
        event.set_as_failed("Torrent failed in client")
        archive.refresh_from_db()

        self.assertTrue(archive.binned)
        self.assertTrue(archive.is_recycled())
        self.assertIn("Torrent failed in client", archive.recycle_entry.reason)

    def test_download_event_set_as_failed_does_not_recycle_completed_archive(self):
        archive = Archive.objects.create(title="complete archive", match_type="torrent", crc32="A1B2C3D4", user=None)
        event = DownloadEvent.objects.create(name="ev_fail_complete", method="torrent", download_id="f2", archive=archive, status="in_progress")

        event.set_as_failed("Some late error")
        archive.refresh_from_db()

        self.assertFalse(archive.binned)
        self.assertFalse(archive.is_recycled())

    def test_download_event_set_as_cancelled_recycles_empty_archive(self):
        archive = Archive.objects.create(title="empty archive to cancel", match_type="torrent", crc32="", user=None)
        event = DownloadEvent.objects.create(name="ev_canc", method="torrent", download_id="c1", archive=archive, status="in_progress")

        event.set_as_cancelled()
        archive.refresh_from_db()

        self.assertTrue(archive.binned)
        self.assertTrue(archive.is_recycled())

    def test_check_torrent_progress_defers_completion_if_archive_crc32_empty(self):
        settings = Settings(load_from_disk=False)
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(title="torrent awaiting postdownload", match_type="torrent", crc32="", user=None)
        event = DownloadEvent.objects.create(
            name="test_torrent_100_unprocessed",
            method="torrent",
            download_id="dl_unproc",
            archive=archive,
            progress=50.0,
        )

        checker.check_torrent_progress(event, 100.0)
        event.refresh_from_db()

        # Progress should be 100%, but completed should stay False until PostDownloader finishes
        self.assertEqual(event.progress, 100.0)
        self.assertFalse(event.completed)
        self.assertEqual(event.status, DownloadEvent.Status.IN_PROGRESS)

    def test_check_hath_progress_defers_completion_if_archive_crc32_empty(self):
        settings = Settings(load_from_disk=False)
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(title="hath awaiting postdownload", match_type="hath", crc32="", user=None)
        event = DownloadEvent.objects.create(
            name="test_hath_100_unprocessed",
            method="hath",
            download_id="hath_unproc",
            archive=archive,
            progress=50.0,
        )

        checker.check_hath_progress(event, 1.0)
        event.refresh_from_db()

        # Progress should be 100%, but completed should stay False until PostDownloader finishes
        self.assertEqual(event.progress, 100.0)
        self.assertFalse(event.completed)
        self.assertEqual(event.status, DownloadEvent.Status.IN_PROGRESS)

    def test_check_hath_progress_finishes_when_crc32_set(self):
        settings = Settings(load_from_disk=False)
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(title="hath processed archive", match_type="hath", crc32="A1B2C3D4", user=None)
        event = DownloadEvent.objects.create(
            name="test_hath_completed",
            method="hath",
            download_id="hath_comp",
            archive=archive,
            progress=50.0,
        )

        checker.check_hath_progress(event, 1.0)
        event.refresh_from_db()

        self.assertEqual(event.progress, 100.0)
        self.assertTrue(event.completed)
        self.assertEqual(event.status, DownloadEvent.Status.COMPLETED)

    def test_check_hath_progress_finish_on_100_percent_without_archive(self):
        settings = Settings(load_from_disk=False)
        checker = DownloadProgressChecker(settings)

        event = DownloadEvent.objects.create(
            name="test_hath_no_archive",
            method="hath",
            download_id="hath_noarch",
            progress=50.0,
        )

        checker.check_hath_progress(event, 1.0)
        event.refresh_from_db()

        self.assertEqual(event.progress, 100.0)
        self.assertTrue(event.completed)
        self.assertEqual(event.status, DownloadEvent.Status.COMPLETED)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_finishes_download_event(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        post_downloader = PostDownloader(settings=settings)

        archive = Archive.objects.create(
            title="archive for postdownload",
            zipped="test.zip",
            crc32="12345678",
            match_type="torrent",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="ev_postproc",
            method="torrent",
            download_id="post1",
            archive=archive,
            status="in_progress",
            completed=False,
        )

        with patch("os.path.isfile", return_value=True), patch.object(archive, "recalc_fileinfo"), patch.object(archive, "save"):
            success = post_downloader.process_downloaded_archive(archive)

        self.assertTrue(success)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertEqual(event.status, DownloadEvent.Status.COMPLETED)
        self.assertEqual(event.progress, 100.0)

    def test_reconcile_downloads_rescues_and_recycles(self):
        from viewer.utils.downloads import reconcile_downloads

        # 1. Orphaned archive (no event, no file) -> should be recycled
        arch_orphaned = Archive.objects.create(title="orphaned arch", match_type="torrent", crc32="", user=None)

        # 2. Archive with failed event (simulate legacy un-binned archive)
        arch_failed = Archive.objects.create(title="failed arch", match_type="torrent", crc32="", user=None)
        DownloadEvent.objects.create(name="ev_failed", method="torrent", download_id="r1", archive=arch_failed, status="failed", completed=True, failed=True)
        Archive.objects.filter(pk=arch_failed.pk).update(binned=False)
        arch_failed.refresh_from_db()

        # 3. False-completed archive (completed event, but missing file) -> event marked failed, archive recycled
        arch_false_completed = Archive.objects.create(title="false completed arch", match_type="torrent", crc32="", user=None)
        ev_false = DownloadEvent.objects.create(name="ev_false", method="torrent", download_id="r2", archive=arch_false_completed, status="completed", completed=True, failed=False)
        Archive.objects.filter(pk=arch_false_completed.pk).update(binned=False)
        arch_false_completed.refresh_from_db()

        # 4. Active in-progress archive -> kept in progress
        arch_active = Archive.objects.create(title="active arch", match_type="torrent", crc32="", user=None)
        DownloadEvent.objects.create(name="ev_active", method="torrent", download_id="r3", archive=arch_active, status="in_progress", completed=False)
        Archive.objects.filter(pk=arch_active.pk).update(binned=False)
        arch_active.refresh_from_db()

        # Dry run first
        stats_dry = reconcile_downloads(dry_run=True, check_disk=False)
        self.assertEqual(stats_dry["recycled_orphaned"], 1)
        self.assertEqual(stats_dry["recycled_failed"], 1)
        self.assertEqual(stats_dry["recycled_false_completed"], 1)
        self.assertEqual(stats_dry["kept_in_progress"], 1)

        # Ensure dry run did NOT modify database
        arch_orphaned.refresh_from_db()
        self.assertFalse(arch_orphaned.binned)

        # Live reconciliation
        stats_live = reconcile_downloads(dry_run=False, check_disk=False)
        self.assertEqual(stats_live["recycled_orphaned"], 1)
        self.assertEqual(stats_live["recycled_failed"], 1)
        self.assertEqual(stats_live["recycled_false_completed"], 1)
        self.assertEqual(stats_live["kept_in_progress"], 1)

        arch_orphaned.refresh_from_db()
        self.assertTrue(arch_orphaned.binned)

        arch_failed.refresh_from_db()
        self.assertTrue(arch_failed.binned)

        arch_false_completed.refresh_from_db()
        self.assertTrue(arch_false_completed.binned)
        ev_false.refresh_from_db()
        self.assertEqual(ev_false.status, DownloadEvent.Status.FAILED)

        arch_active.refresh_from_db()
        self.assertFalse(arch_active.binned)

    def test_reconcile_downloads_management_command(self):
        from django.core.management import call_command
        from io import StringIO

        out = StringIO()
        call_command("reconcile_downloads", "--dry-run", stdout=out)
        output = out.getvalue()
        self.assertIn("Reconciliation Results", output)
        self.assertIn("DRY-RUN mode", output)

    def test_download_history_tools_non_staff_forbidden(self):
        from django.contrib.auth.models import User, Permission
        from django.urls import reverse

        user = User.objects.create_user(username="collab_user_forbidden", password="password", is_staff=False)
        perm = Permission.objects.get(codename="download_history")
        user.user_permissions.add(perm)
        self.client.login(username="collab_user_forbidden", password="password")

        ev = DownloadEvent.objects.create(name="ev_collab", method="torrent", download_id="c1", status="in_progress")

        response = self.client.post(
            reverse("viewer:downloads"),
            {"action": "mark_failed", "selected_events": [ev.pk]},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Staff permission required")
        ev.refresh_from_db()
        self.assertEqual(ev.status, DownloadEvent.Status.IN_PROGRESS)

    def test_download_history_tools_staff_mark_failed(self):
        from django.contrib.auth.models import User, Permission
        from django.urls import reverse

        staff_user = User.objects.create_user(username="staff_user_1", password="password", is_staff=True)
        perm = Permission.objects.get(codename="download_history")
        staff_user.user_permissions.add(perm)
        self.client.login(username="staff_user_1", password="password")

        arch = Archive.objects.create(title="arch to fail", match_type="torrent", crc32="", user=None)
        ev1 = DownloadEvent.objects.create(name="ev1", method="torrent", download_id="t1", archive=arch, status="in_progress")
        ev2 = DownloadEvent.objects.create(name="ev2", method="torrent", download_id="t2", status="in_progress")

        response = self.client.post(
            reverse("viewer:downloads"),
            {"action": "mark_failed", "selected_events": [ev1.pk, ev2.pk]},
        )
        self.assertEqual(response.status_code, 302)
        ev1.refresh_from_db()
        ev2.refresh_from_db()
        self.assertEqual(ev1.status, DownloadEvent.Status.FAILED)
        self.assertEqual(ev2.status, DownloadEvent.Status.FAILED)
        arch.refresh_from_db()
        self.assertTrue(arch.binned)

    def test_download_history_tools_staff_mark_completed(self):
        from django.contrib.auth.models import User, Permission
        from django.urls import reverse

        staff_user = User.objects.create_user(username="staff_user_2", password="password", is_staff=True)
        perm = Permission.objects.get(codename="download_history")
        staff_user.user_permissions.add(perm)
        self.client.login(username="staff_user_2", password="password")

        ev = DownloadEvent.objects.create(name="ev_complete", method="torrent", download_id="t3", status="in_progress")

        response = self.client.post(
            reverse("viewer:downloads"),
            {"action": "mark_completed", "selected_events": [ev.pk]},
        )
        self.assertEqual(response.status_code, 302)
        ev.refresh_from_db()
        self.assertEqual(ev.status, DownloadEvent.Status.COMPLETED)
        self.assertTrue(ev.completed)

    def test_download_history_tools_staff_delete_queued(self):
        from django.contrib.auth.models import User, Permission
        from django.urls import reverse

        staff_user = User.objects.create_user(username="staff_user_3", password="password", is_staff=True)
        perm = Permission.objects.get(codename="download_history")
        staff_user.user_permissions.add(perm)
        self.client.login(username="staff_user_3", password="password")

        arch = Archive.objects.create(title="arch queued", match_type="archive", crc32="", user=None)
        ev_queued = DownloadEvent.objects.create(name="ev_q", method="archive", download_id="q1", archive=arch, status=DownloadEvent.Status.QUEUED, completed=False)
        ev_active = DownloadEvent.objects.create(name="ev_act", method="torrent", download_id="q2", status=DownloadEvent.Status.IN_PROGRESS, completed=False)

        response = self.client.post(
            reverse("viewer:downloads"),
            {"action": "delete_queued", "selected_events": [ev_queued.pk, ev_active.pk]},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(DownloadEvent.objects.filter(pk=ev_queued.pk).exists())
        self.assertTrue(DownloadEvent.objects.filter(pk=ev_active.pk).exists())
        arch.refresh_from_db()
        self.assertTrue(arch.binned)










