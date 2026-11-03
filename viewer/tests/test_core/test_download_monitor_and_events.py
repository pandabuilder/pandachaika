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




class DownloadMonitorAndEventTests(TestCase):

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission

        User = get_user_model()
        self.user = User.objects.create_user(username="downloader", password="password123")
        perm = Permission.objects.get(codename="download_history")
        self.user.user_permissions.add(perm)
        self.client.force_login(self.user)

    def test_downloadevent_save_sync_completed_to_status(self):
        # 1. completed=True, failed=False -> status becomes COMPLETED
        ev_comp = DownloadEvent.objects.create(
            name="dl1",
            method="test",
            download_id="d1",
            completed=True,
            failed=False,
        )
        self.assertEqual(ev_comp.status, DownloadEvent.Status.COMPLETED)

        # 2. completed=True, failed=True -> status becomes FAILED
        ev_fail = DownloadEvent.objects.create(
            name="dl2",
            method="test",
            download_id="d2",
            completed=True,
            failed=True,
        )
        self.assertEqual(ev_fail.status, DownloadEvent.Status.FAILED)

        # 3. status=COMPLETED -> completed becomes True
        ev_stat_comp = DownloadEvent.objects.create(
            name="dl3",
            method="test",
            download_id="d3",
            status=DownloadEvent.Status.COMPLETED,
            completed=False,
        )
        self.assertTrue(ev_stat_comp.completed)
        self.assertFalse(ev_stat_comp.failed)

        # 4. status=CANCELLED -> completed=True and failed=True
        ev_stat_canc = DownloadEvent.objects.create(
            name="dl4",
            method="test",
            download_id="d4",
            status=DownloadEvent.Status.CANCELLED,
            completed=False,
        )
        self.assertTrue(ev_stat_canc.completed)
        self.assertTrue(ev_stat_canc.failed)

    def test_downloadevent_save_with_update_fields(self):
        ev = DownloadEvent.objects.create(
            name="dl_update",
            method="test",
            download_id="dup",
            status=DownloadEvent.Status.IN_PROGRESS,
            completed=False,
        )
        ev.completed = True
        # Save with update_fields mentioning only "progress"
        ev.progress = 100.0
        ev.save(update_fields=["progress"])
        ev.refresh_from_db()
        self.assertEqual(ev.status, DownloadEvent.Status.COMPLETED)
        self.assertTrue(ev.completed)
        self.assertEqual(ev.progress, 100.0)

    def test_downloadevent_effective_status(self):
        ev = DownloadEvent.objects.create(
            name="dl_eff",
            method="test",
            download_id="deff",
            status=DownloadEvent.Status.IN_PROGRESS,
            completed=False,
        )
        # Simulate legacy DB row where completed=True but status='in_progress'
        DownloadEvent.objects.filter(pk=ev.pk).update(status="in_progress", completed=True, failed=False)
        ev.refresh_from_db()
        self.assertEqual(ev.status, "in_progress")
        self.assertEqual(ev.effective_status, DownloadEvent.Status.COMPLETED)

        # Simulate legacy DB row where failed=True but status='in_progress'
        DownloadEvent.objects.filter(pk=ev.pk).update(status="in_progress", completed=True, failed=True)
        ev.refresh_from_db()
        self.assertEqual(ev.status, "in_progress")
        self.assertEqual(ev.effective_status, DownloadEvent.Status.FAILED)

    def test_downloadevent_queryset_completed_and_failed(self):
        ev1 = DownloadEvent.objects.create(
            name="q_comp",
            method="test",
            download_id="qc",
            completed=True,
            failed=False,
        )
        ev2 = DownloadEvent.objects.create(
            name="q_fail",
            method="test",
            download_id="qf",
            completed=True,
            failed=True,
        )
        ev3 = DownloadEvent.objects.create(
            name="q_prog",
            method="test",
            download_id="qp",
            status=DownloadEvent.Status.IN_PROGRESS,
            completed=False,
            failed=False,
        )

        completed_ids = list(DownloadEvent.objects.completed().values_list("id", flat=True))
        failed_ids = list(DownloadEvent.objects.failed().values_list("id", flat=True))

        self.assertIn(ev1.pk, completed_ids)
        self.assertNotIn(ev2.pk, completed_ids)
        self.assertNotIn(ev3.pk, completed_ids)

        self.assertIn(ev2.pk, failed_ids)
        self.assertNotIn(ev1.pk, failed_ids)
        self.assertNotIn(ev3.pk, failed_ids)

    def test_migration_sync_function(self):
        import importlib
        migration_module = importlib.import_module("viewer.migrations.0211_sync_downloadevent_status")
        sync_downloadevent_status = migration_module.sync_downloadevent_status
        from django.apps import apps

        # Create simulated older deployment rows bypassing save()
        ev_old_comp = DownloadEvent.objects.create(name="old_comp", method="t", download_id="oc")
        DownloadEvent.objects.filter(pk=ev_old_comp.pk).update(status="in_progress", completed=True, failed=False)

        ev_old_fail = DownloadEvent.objects.create(name="old_fail", method="t", download_id="of")
        DownloadEvent.objects.filter(pk=ev_old_fail.pk).update(status="in_progress", completed=True, failed=True)

        ev_old_canc = DownloadEvent.objects.create(name="old_canc", method="t", download_id="oca")
        DownloadEvent.objects.filter(pk=ev_old_canc.pk).update(status="cancelled", completed=False, failed=False)

        # Run migration function
        sync_downloadevent_status(apps, None)

        ev_old_comp.refresh_from_db()
        ev_old_fail.refresh_from_db()
        ev_old_canc.refresh_from_db()

        self.assertEqual(ev_old_comp.status, DownloadEvent.Status.COMPLETED)
        self.assertEqual(ev_old_fail.status, DownloadEvent.Status.FAILED)
        self.assertEqual(ev_old_canc.status, DownloadEvent.Status.CANCELLED)
        self.assertTrue(ev_old_canc.completed)
        self.assertTrue(ev_old_canc.failed)

    def test_download_history_view_filtering(self):
        from django.urls import reverse

        # Legacy completed row with status='in_progress' in DB
        ev_legacy = DownloadEvent.objects.create(name="legacy_dl", method="http", download_id="leg")
        DownloadEvent.objects.filter(pk=ev_legacy.pk).update(status="in_progress", completed=True, failed=False)

        # Active downloading event
        ev_active = DownloadEvent.objects.create(
            name="active_dl",
            method="http",
            download_id="act",
            status=DownloadEvent.Status.IN_PROGRESS,
            completed=False,
            failed=False,
        )

        # Queued event
        ev_queued = DownloadEvent.objects.create(
            name="queued_dl",
            method="http",
            download_id="que",
            status=DownloadEvent.Status.QUEUED,
            completed=False,
            failed=False,
        )

        # Failed event
        ev_failed = DownloadEvent.objects.create(
            name="failed_dl",
            method="http",
            download_id="fai",
            completed=True,
            failed=True,
        )

        url = reverse("viewer:downloads")

        # 1. Default (in_progress): should only include active and queued, NOT legacy completed
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        results = resp.context["results"]
        result_ids = [r.pk for r in results]
        self.assertIn(ev_active.pk, result_ids)
        self.assertIn(ev_queued.pk, result_ids)
        self.assertNotIn(ev_legacy.pk, result_ids)
        self.assertNotIn(ev_failed.pk, result_ids)

        # 2. Filter: finished
        resp_fin = self.client.get(url, {"status": "finished"})
        self.assertEqual(resp_fin.status_code, 200)
        fin_ids = [r.pk for r in resp_fin.context["results"]]
        self.assertIn(ev_legacy.pk, fin_ids)
        self.assertNotIn(ev_active.pk, fin_ids)
        self.assertNotIn(ev_failed.pk, fin_ids)

        # 3. Filter: downloading
        resp_dl = self.client.get(url, {"status": "downloading"})
        self.assertEqual(resp_dl.status_code, 200)
        dl_ids = [r.pk for r in resp_dl.context["results"]]
        self.assertIn(ev_active.pk, dl_ids)
        self.assertNotIn(ev_queued.pk, dl_ids)
        self.assertNotIn(ev_legacy.pk, dl_ids)

        # 4. Filter: queued
        resp_q = self.client.get(url, {"status": "queued"})
        self.assertEqual(resp_q.status_code, 200)
        q_ids = [r.pk for r in resp_q.context["results"]]
        self.assertIn(ev_queued.pk, q_ids)
        self.assertNotIn(ev_active.pk, q_ids)

        # 5. Filter: failed
        resp_f = self.client.get(url, {"status": "failed"})
        self.assertEqual(resp_f.status_code, 200)
        f_ids = [r.pk for r in resp_f.context["results"]]
        self.assertIn(ev_failed.pk, f_ids)
        self.assertNotIn(ev_legacy.pk, f_ids)

        # 6. JSON output: check effective_status and status_display for legacy item
        resp_json = self.client.get(url, {"status": "finished", "json": "1"})
        self.assertEqual(resp_json.status_code, 200)
        import json
        data = json.loads(resp_json.content.decode("utf-8"))
        legacy_data = next((item for item in data["results"] if item["id"] == ev_legacy.pk), None)
        self.assertIsNotNone(legacy_data)
        self.assertEqual(legacy_data["status"], "completed")
        self.assertEqual(legacy_data["status_display"], "Completed")
        self.assertTrue(legacy_data["completed"])
        self.assertFalse(legacy_data["failed"])

    def test_download_history_provider_display_and_filter(self):
        from django.urls import reverse
        from viewer.models import Gallery, Archive
        import json

        # Event with direct provider
        ev1 = DownloadEvent.objects.create(
            name="https://example.com/dl1",
            method="archive",
            provider="chaika",
            download_id="id1",
            status=DownloadEvent.Status.IN_PROGRESS,
        )
        # Event with gallery provider fallback
        gal = Gallery.objects.create(title="Gallery Panda", gid="99901", provider="panda")
        ev2 = DownloadEvent.objects.create(
            name="https://example.com/dl2",
            method="torrent",
            gallery=gal,
            download_id="id2",
            status=DownloadEvent.Status.IN_PROGRESS,
        )
        # Event with archive -> gallery provider fallback
        gal_nexus = Gallery.objects.create(title="Gallery Nexus", gid="99902", provider="nexus")
        arch = Archive.objects.create(title="Archive Nexus", gallery=gal_nexus, user=self.user)
        ev3 = DownloadEvent.objects.create(
            name="https://example.com/dl3",
            method="hath",
            archive=arch,
            download_id="id3",
            status=DownloadEvent.Status.IN_PROGRESS,
        )

        self.assertEqual(ev1.effective_provider, "chaika")
        self.assertEqual(ev2.effective_provider, "panda")
        self.assertEqual(ev3.effective_provider, "nexus")

        url = reverse("viewer:downloads")

        # Check HTML view contains Provider header and provider badges
        resp = self.client.get(url, {"status": "in_progress"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<th>Provider</th>")
        self.assertContains(resp, "chaika")
        self.assertContains(resp, "panda")
        self.assertContains(resp, "nexus")

        # Filter by provider: chaika
        resp_chaika = self.client.get(url, {"status": "in_progress", "provider": "chaika"})
        self.assertEqual(resp_chaika.status_code, 200)
        chaika_ids = [r.pk for r in resp_chaika.context["results"]]
        self.assertIn(ev1.pk, chaika_ids)
        self.assertNotIn(ev2.pk, chaika_ids)
        self.assertNotIn(ev3.pk, chaika_ids)

        # Filter by provider: panda
        resp_panda = self.client.get(url, {"status": "in_progress", "provider": "panda"})
        self.assertEqual(resp_panda.status_code, 200)
        panda_ids = [r.pk for r in resp_panda.context["results"]]
        self.assertIn(ev2.pk, panda_ids)
        self.assertNotIn(ev1.pk, panda_ids)

        # JSON response contains provider
        resp_json = self.client.get(url, {"status": "in_progress", "json": "1"})
        self.assertEqual(resp_json.status_code, 200)
        data = json.loads(resp_json.content.decode("utf-8"))
        ev1_data = next((item for item in data["results"] if item["id"] == ev1.pk), None)
        self.assertIsNotNone(ev1_data)
        self.assertEqual(ev1_data["provider"], "chaika")
        ev2_data = next((item for item in data["results"] if item["id"] == ev2.pk), None)
        self.assertIsNotNone(ev2_data)
        self.assertEqual(ev2_data["provider"], "panda")

    def test_admin_actions(self):
        from django.contrib.admin.sites import AdminSite
        from viewer.admin import DownloadEventAdmin
        from django.test import RequestFactory

        site = AdminSite()
        admin_obj = DownloadEventAdmin(DownloadEvent, site)
        admin_obj.message_user = lambda request, msg: None

        ev = DownloadEvent.objects.create(name="adm_dl", method="t", download_id="adm")
        DownloadEvent.objects.filter(pk=ev.pk).update(status="in_progress", completed=True, failed=False)

        factory = RequestFactory()
        req = factory.post("/")
        req.user = self.user

        # Test sync_status_from_completed_flags
        qs = DownloadEvent.objects.filter(pk=ev.pk)
        admin_obj.sync_status_from_completed_flags(req, qs)

