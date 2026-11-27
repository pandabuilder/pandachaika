"""Tests for the public /submit/ page and live /submit/check/ API."""

import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from viewer.models import Archive, Gallery, GallerySubmitEntry, Profile
from viewer.views.head.submission import ANONYMOUS_MAX_CHECK_URLS
from workers.models import TaskQueue


class PublicUrlSubmitTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("submit_admin", "submit_admin@example.com", "pw")
        self.user = User.objects.create_user("submit_user", "submit_user@example.com", "pw")
        self.submit_url = reverse("viewer:url-submit")
        self.check_url = reverse("viewer:url-submit-check")
        self.enable_patch = patch.object(settings.CRAWLER_SETTINGS.urls, "enable_public_submit", True)
        self.enable_patch.start()
        self.addCleanup(self.enable_patch.stop)

    def _create_archive(self, **kwargs):
        kwargs.setdefault("user", self.admin)
        return Archive.objects.create(**kwargs)

    def _post_check(self, urls):
        return self.client.post(
            self.check_url,
            data=json.dumps({"urls": urls}),
            content_type="application/json",
        )

    def test_submit_page_renders_streamlined_layout_and_prefill(self):
        resp = self.client.get(
            self.submit_url,
            {"url": "https://e-hentai.org/g/11111/aaaaaaaaaa/", "reason": "wani"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "dm-page-header")
        self.assertContains(resp, "dm-pages.css")
        self.assertContains(resp, "url_submit.js")
        self.assertContains(resp, "https://e-hentai.org/g/11111/aaaaaaaaaa/")
        self.assertContains(resp, 'value="wani"')

    def test_disabled_setting_blocks_submit_and_check_endpoints(self):
        with patch.object(settings.CRAWLER_SETTINGS.urls, "enable_public_submit", False):
            self.assertEqual(self.client.get(self.submit_url).status_code, 404)
            self.assertEqual(self._post_check(["https://e-hentai.org/g/1/a/"]).status_code, 404)

            self.client.force_login(self.admin)
            resp = self.client.get(self.submit_url, follow=True)
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "Page disabled by settings")
            check_resp = self._post_check(["https://e-hentai.org/g/1/a/"])
            self.assertEqual(check_resp.status_code, 403)

    def test_check_endpoint_rejects_get_and_invalid_json_and_oversized_batches(self):
        self.assertEqual(self.client.get(self.check_url).status_code, 405)
        bad = self.client.post(self.check_url, data="{not json", content_type="application/json")
        self.assertEqual(bad.status_code, 400)

        too_many = [f"https://e-hentai.org/g/{i}/abcdef1234/" for i in range(ANONYMOUS_MAX_CHECK_URLS + 1)]
        over = self._post_check(too_many)
        self.assertEqual(over.status_code, 400)
        self.assertIn("Maximum", over.json()["error"])

    def test_anonymous_public_archived_gallery_marked_already_available_with_links(self):
        gallery = Gallery.objects.create(
            gid="20001",
            token="abcdef1234",
            provider="panda",
            title="Public Complete Gallery",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
            dl_type="archive",
            filecount=24,
            filesize=5_000_000,
            posted=timezone.now(),
        )
        pub_archive = self._create_archive(
            gallery=gallery,
            title="Public Main Archive",
            public=True,
            public_date=timezone.now(),
            binned=False,
            file_deleted=False,
            filecount=24,
            filesize=5_000_000,
        )
        priv_archive = self._create_archive(
            gallery=gallery,
            title="Secret Private Archive",
            public=False,
            binned=False,
            file_deleted=False,
        )
        binned_archive = self._create_archive(
            gallery=gallery,
            title="Binned Archive",
            public=True,
            binned=True,
            file_deleted=False,
        )
        deleted_file_archive = self._create_archive(
            gallery=gallery,
            title="Deleted File Archive",
            public=True,
            binned=False,
            file_deleted=True,
        )

        resp = self._post_check(["https://e-hentai.org/g/20001/abcdef1234/"])
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["counts"]["already_available"], 1)
        self.assertEqual(data["counts"]["submittable"], 0)
        item = data["items"][0]
        self.assertEqual(item["status"], "already_available")
        self.assertFalse(item["can_submit"])
        self.assertFalse(item["needs_submit"])
        self.assertEqual(item["gallery"]["id"], gallery.pk)
        self.assertEqual(item["gallery"]["url"], reverse("viewer:gallery", args=[gallery.pk]))
        self.assertEqual(item["gallery"]["title"], "Public Complete Gallery")
        # Must not expose authenticated-only fields to anonymous users
        for forbidden_key in ("public", "status_display", "failed", "is_submitted"):
            self.assertNotIn(forbidden_key, item["gallery"])
        # Only the single public, non-binned, non-deleted archive is returned
        self.assertEqual(len(item["archives"]), 1)
        self.assertEqual(item["archives"][0]["id"], pub_archive.pk)
        self.assertEqual(item["archives"][0]["url"], reverse("viewer:archive", args=[pub_archive.pk]))
        self.assertEqual(item["archives"][0]["relation"], "main")
        for forbidden_key in ("public", "recycled", "file_deleted"):
            self.assertNotIn(forbidden_key, item["archives"][0])
        for hidden in (priv_archive, binned_archive, deleted_file_archive):
            self.assertNotIn(reverse("viewer:archive", args=[hidden.pk]), resp.content.decode())
            self.assertNotIn(hidden.title, resp.content.decode())

    def test_anonymous_public_alternative_source_archive_counts_as_already_available(self):
        gallery = Gallery.objects.create(
            gid="20002",
            token="abcdef1234",
            provider="panda",
            title="Alt Source Gallery",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
            dl_type="archive",
        )
        alt_archive = self._create_archive(
            title="Public Alt Archive",
            public=True,
            public_date=timezone.now(),
            binned=False,
            file_deleted=False,
        )
        alt_archive.alternative_sources.add(gallery)

        resp = self._post_check(["https://e-hentai.org/g/20002/abcdef1234/"])
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["items"][0]
        self.assertEqual(item["status"], "already_available")
        self.assertFalse(item["can_submit"])
        self.assertEqual(len(item["archives"]), 1)
        self.assertEqual(item["archives"][0]["id"], alt_archive.pk)
        self.assertEqual(item["archives"][0]["relation"], "alt")

    def test_anonymous_does_not_leak_private_deleted_denied_or_private_archive_data(self):
        # 1. Private normal gallery with a private archive
        priv_g = Gallery.objects.create(
            gid="30001",
            token="aaa111",
            provider="panda",
            title="Top Secret Private Gallery",
            public=False,
            status=Gallery.StatusChoices.NORMAL,
            reason="secret_reason",
        )
        self._create_archive(gallery=priv_g, title="Secret Archive", public=False)

        # 2. Deleted gallery
        Gallery.objects.create(
            gid="30002",
            token="aaa222",
            provider="panda",
            title="Secret Deleted Gallery",
            public=False,
            status=Gallery.StatusChoices.DELETED,
        )

        # 3. Denied gallery
        Gallery.objects.create(
            gid="30003",
            token="aaa333",
            provider="panda",
            title="Secret Denied Gallery",
            public=False,
            status=Gallery.StatusChoices.DENIED,
        )

        # 4. Public gallery with ONLY a private archive (must NOT be treated as already_available!)
        pub_only_priv_arc_g = Gallery.objects.create(
            gid="30004",
            token="aaa444",
            provider="panda",
            title="Public Gallery Private Archive Only",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
        )
        self._create_archive(gallery=pub_only_priv_arc_g, title="Hidden Private Archive", public=False)

        # 5. Public gallery with a public archive, but gallery dl_type is failed
        pub_failed_g = Gallery.objects.create(
            gid="30005",
            token="aaa555",
            provider="panda",
            title="Public Gallery Retry Case",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
            dl_type="failed",
        )
        pub_failed_arc = self._create_archive(
            gallery=pub_failed_g,
            title="Public Archive Retry Case",
            public=True,
            binned=False,
            file_deleted=False,
        )

        # 6. Completely nonexistent gallery (baseline for comparison)
        urls = [
            "https://e-hentai.org/g/30001/aaa111/",
            "https://e-hentai.org/g/30002/aaa222/",
            "https://e-hentai.org/g/30003/aaa333/",
            "https://e-hentai.org/g/30004/aaa444/",
            "https://e-hentai.org/g/30005/aaa555/",
            "https://e-hentai.org/g/39999/zzz999/",
        ]
        resp = self._post_check(urls)
        self.assertEqual(resp.status_code, 200)
        raw_body = resp.content.decode()
        items = resp.json()["items"]

        # Private, deleted, and denied galleries must be indistinguishable from nonexistent!
        baseline = items[5]
        for idx in (0, 1, 2):
            item = items[idx]
            self.assertEqual(item["status"], baseline["status"])
            self.assertEqual(item["can_submit"], baseline["can_submit"])
            self.assertEqual(item["needs_submit"], baseline["needs_submit"])
            self.assertEqual(item["message"], baseline["message"])
            self.assertIsNone(item["gallery"])
            self.assertEqual(item["archives"], [])

        # Private titles/reasons/failed words must never appear in anonymous response
        for secret in (
            "Top Secret Private Gallery",
            "Secret Deleted Gallery",
            "Secret Denied Gallery",
            "Secret Archive",
            "Hidden Private Archive",
            "secret_reason",
            "failed",
            "Failed",
        ):
            self.assertNotIn(secret, raw_body)

        # Public gallery with only a private archive -> gallery_only, submittable, 0 archives shown
        item_priv_arc = items[3]
        self.assertEqual(item_priv_arc["status"], "gallery_only")
        self.assertTrue(item_priv_arc["can_submit"])
        self.assertEqual(item_priv_arc["gallery"]["id"], pub_only_priv_arc_g.pk)
        self.assertEqual(item_priv_arc["archives"], [])

        # Public gallery with failed dl_type + public archive -> NOT already_available, still submittable
        item_failed = items[4]
        self.assertNotEqual(item_failed["status"], "already_available")
        self.assertTrue(item_failed["can_submit"])
        self.assertTrue(item_failed["needs_submit"])
        self.assertEqual(item_failed["gallery"]["id"], pub_failed_g.pk)
        self.assertEqual([a["id"] for a in item_failed["archives"]], [pub_failed_arc.pk])

    def test_authenticated_user_sees_full_status_and_private_entries(self):
        priv_g = Gallery.objects.create(
            gid="40001",
            token="bbb111",
            provider="panda",
            title="Private Gallery With Archive",
            public=False,
            status=Gallery.StatusChoices.NORMAL,
        )
        priv_arc = self._create_archive(gallery=priv_g, title="Private Arc", public=False)
        Gallery.objects.create(
            gid="40002",
            token="bbb222",
            provider="panda",
            title="Denied Gallery",
            public=False,
            status=Gallery.StatusChoices.DENIED,
        )
        Gallery.objects.create(
            gid="40003",
            token="bbb333",
            provider="panda",
            title="Failed Gallery",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
            dl_type="failed",
        )

        self.client.force_login(self.user)
        resp = self._post_check(
            [
                "https://e-hentai.org/g/40001/bbb111/",
                "https://e-hentai.org/g/40002/bbb222/",
                "https://e-hentai.org/g/40003/bbb333/",
            ]
        )
        self.assertEqual(resp.status_code, 200)
        items = resp.json()["items"]

        self.assertEqual(items[0]["status"], "private_available")
        self.assertFalse(items[0]["gallery"]["public"])
        self.assertEqual(items[0]["archives"][0]["id"], priv_arc.pk)
        self.assertFalse(items[0]["archives"][0]["public"])

        self.assertEqual(items[1]["status"], "removed")
        self.assertEqual(items[1]["gallery"]["status_display"], "Denied")

        self.assertEqual(items[2]["status"], "failed")
        self.assertTrue(items[2]["gallery"]["failed"])
        self.assertTrue(items[2]["can_submit"])

    def test_multi_link_deduplication_commands_notes_and_unsupported_urls(self):
        resp = self._post_check(
            [
                "https://e-hentai.org/g/50001/ccc111/",
                "https://exhentai.org/g/50001/ccc111/",  # same provider+gid
                "https://e-hentai.org/g/50001/ccc111/",  # exact duplicate
                "https://example.com/not-supported",
                "-rf --command",
                "just a note for reviewers",
            ]
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        statuses = [item["status"] for item in data["items"]]
        self.assertEqual(statuses, ["ready", "duplicate", "duplicate", "unsupported", "invalid", "note"])
        self.assertEqual(data["items"][1]["duplicate_of"], "https://e-hentai.org/g/50001/ccc111/")
        self.assertEqual(data["counts"]["submittable"], 1)
        self.assertEqual(data["counts"]["attention"], 5)

    def test_submit_skips_already_public_archived_urls_and_does_not_enqueue_or_email(self):
        Profile.objects.update_or_create(user=self.admin, defaults={"notify_new_submissions": True})
        gallery = Gallery.objects.create(
            gid="60001",
            token="ddd111",
            provider="panda",
            title="Already Public Book",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
            dl_type="archive",
        )
        archive = self._create_archive(
            gallery=gallery,
            title="Already Public Archive",
            public=True,
            public_date=timezone.now(),
            binned=False,
            file_deleted=False,
        )

        resp = self.client.post(
            self.submit_url,
            {"submit": "1", "urls": "https://e-hentai.org/g/60001/ddd111/", "reason": "test"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="web_crawler").count(), 0)
        self.assertEqual(TaskQueue.objects.filter(task_type="send_email_worker").count(), 0)
        self.assertEqual(GallerySubmitEntry.objects.count(), 0)

        # Follow redirect and verify feedback card presents gallery and public archive links
        page = self.client.get(self.submit_url)
        self.assertContains(page, "Already Public Book")
        self.assertContains(page, reverse("viewer:gallery", args=[gallery.pk]))
        self.assertContains(page, reverse("viewer:archive", args=[archive.pk]))

    def test_submit_mixed_batch_only_enqueues_missing_url_and_skips_already_available(self):
        Profile.objects.update_or_create(user=self.admin, defaults={"notify_new_submissions": True})
        gallery = Gallery.objects.create(
            gid="60002",
            token="ddd222",
            provider="panda",
            title="Existing Public Book",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
        )
        self._create_archive(
            gallery=gallery,
            title="Existing Public Archive",
            public=True,
            binned=False,
            file_deleted=False,
        )

        resp = self.client.post(
            self.submit_url,
            {
                "submit": "1",
                "urls": "https://e-hentai.org/g/60002/ddd222/\nhttps://e-hentai.org/g/60003/ddd333/",
                "reason": "mixed batch",
            },
        )
        self.assertEqual(resp.status_code, 302)
        crawler_task = TaskQueue.objects.filter(task_type="web_crawler").latest("pk")
        self.assertEqual(crawler_task.args, ["https://e-hentai.org/g/60003/ddd333/"])
        self.assertEqual(GallerySubmitEntry.objects.count(), 1)
        self.assertEqual(GallerySubmitEntry.objects.first().submit_url, "https://e-hentai.org/g/60003/ddd333/")
        self.assertEqual(TaskQueue.objects.filter(task_type="send_email_worker").count(), 1)

    def test_anonymous_submit_on_private_or_denied_gallery_does_not_leak_status_in_messages(self):
        Profile.objects.update_or_create(user=self.admin, defaults={"notify_new_submissions": True})
        denied_g = Gallery.objects.create(
            gid="70001",
            token="eee111",
            provider="panda",
            title="Private Denied Secret Title",
            public=False,
            status=Gallery.StatusChoices.DENIED,
        )

        resp = self.client.post(
            self.submit_url,
            {"submit": "1", "urls": "https://e-hentai.org/g/70001/eee111/", "reason": "anon submit"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("has been submitted for review", body)
        for leaked in ("denied", "deleted", "being reviewed", "Private Denied Secret Title"):
            self.assertNotIn(leaked, body)

        # Admins still receive the GallerySubmitEntry marked already present and full email details
        entry = GallerySubmitEntry.objects.get(gallery=denied_g)
        self.assertEqual(entry.resolved_status, GallerySubmitEntry.RESOLVED_ALREADY_PRESENT)
        email_task = TaskQueue.objects.filter(task_type="send_email_worker").latest("pk")
        self.assertIn("denied", email_task.options["messages"][0][1].lower())

    def test_classic_non_js_check_post_respects_public_visibility(self):
        pub_g = Gallery.objects.create(
            gid="80001",
            token="fff111",
            provider="panda",
            title="Visible Classic Check Gallery",
            public=True,
            status=Gallery.StatusChoices.NORMAL,
        )
        pub_arc = self._create_archive(gallery=pub_g, title="Visible Classic Arc", public=True)
        Gallery.objects.create(
            gid="80002",
            token="fff222",
            provider="panda",
            title="Hidden Classic Check Gallery",
            public=False,
            status=Gallery.StatusChoices.NORMAL,
        )

        resp = self.client.post(
            self.submit_url,
            {
                "check": "1",
                "urls": "https://e-hentai.org/g/80001/fff111/\nhttps://e-hentai.org/g/80002/fff222/",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Visible Classic Check Gallery")
        self.assertContains(resp, reverse("viewer:archive", args=[pub_arc.pk]))
        self.assertNotContains(resp, "Hidden Classic Check Gallery")
        self.assertNotContains(resp, "being reviewed")
