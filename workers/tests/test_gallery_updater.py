# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.setup import Settings
from core.base.types import GalleryData
from core.web.gallery_updater import GalleryMetadataUpdater, UpdateSummary
from viewer.models import Archive, ArchiveManageEntry, Gallery, Tag, WantedGallery
from workers import client
from workers.models import TaskQueue
from workers.runner import TaskCancelledError, WorkerRunner

User = get_user_model()


class GalleryMetadataUpdaterTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="updater_user", password="password123")
        self.settings = Settings(load_from_disk=False)
        self.settings.archive_user = self.user
        self.settings.event_action = "UPDATE_METADATA"

    def test_update_empty_links(self):
        updater = GalleryMetadataUpdater(self.settings)
        summary = updater.update([])
        self.assertEqual(summary.requested, 0)
        self.assertEqual(summary.updated, 0)

    def test_update_gallery_syncs_archive_titles_and_tags(self):
        # 1. Create a gallery and associated archive
        posted_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        gallery = Gallery.objects.create(
            gid="12345",
            token="tok123",
            provider="panda",
            title="Old Title",
            title_jpn="Old Title JPN",
            posted=posted_time,
        )
        tag1, _ = Tag.objects.get_or_create(name="tag1", scope="artist")
        gallery.tags.add(tag1)

        archive = Archive.objects.create(
            title="Old Archive Title",
            gallery=gallery,
            user=self.user,
        )
        archive.tags.add(tag1)

        # 2. Mock fresh GalleryData returned from provider
        new_posted = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
        fresh_data = GalleryData(
            gid="12345",
            token="tok123",
            provider="panda",
            title="Refreshed Title",
            title_jpn="Refreshed Title JPN",
            posted=new_posted,
            tags=["artist:new_artist", "female:sole_female"],
            link="https://e-hentai.org/g/12345/tok123/",
        )
        # Add related gallery GIDs to verify they are NOT queued
        fresh_data.parent_gallery_gid = "99999"
        fresh_data.first_gallery_gid = "88888"
        fresh_data.extra_data["current_gid"] = "77777"

        updater = GalleryMetadataUpdater(self.settings)
        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = [fresh_data.link]
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = [fresh_data.link]
        mock_parser.update_fetch_chunk_size = 25
        mock_parser.fetch_gallery_data_for_update.return_value = [fresh_data]
        mock_parser.general_utils = MagicMock()
        mock_parser.general_utils.discard_by_gallery_data.return_value = (False, [])
        mock_parser.is_current_link_non_current.return_value = False

        with patch.object(self.settings.provider_context, "get_parsers", return_value=[mock_parser]):
            summary = updater.update([fresh_data.link])

        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.fetched, 1)
        self.assertEqual(summary.updated, 1)
        self.assertEqual(summary.failed, 0)

        # 3. Verify gallery DB row updated
        gallery.refresh_from_db()
        self.assertEqual(gallery.title, "Refreshed Title")
        self.assertEqual(gallery.title_jpn, "Refreshed Title JPN")

        # 4. Verify related archive synced titles and tags
        archive.refresh_from_db()
        self.assertEqual(archive.title, "Refreshed Title")
        self.assertEqual(archive.title_jpn, "Refreshed Title JPN")
        archive_tag_names = set(archive.tags.values_list("name", flat=True))
        self.assertIn("new_artist", archive_tag_names)
        self.assertIn("sole_female", archive_tag_names)

        # 5. Verify NO child/parent tasks were queued
        queued_count = TaskQueue.objects.filter(task_type="web_crawler").count()
        self.assertEqual(queued_count, 0)

    def test_update_gallery_detects_banned_data_and_creates_manage_entry(self):
        gallery = Gallery.objects.create(
            gid="23456",
            token="tok234",
            provider="panda",
            title="Test Gallery",
        )
        archive = Archive.objects.create(
            title="Test Gallery Archive",
            gallery=gallery,
            user=self.user,
        )

        fresh_data = GalleryData(
            gid="23456",
            token="tok234",
            provider="panda",
            title="Test Gallery Refreshed",
            tags=["banned:bad_tag"],
            link="https://e-hentai.org/g/23456/tok234/",
        )

        updater = GalleryMetadataUpdater(self.settings)
        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = [fresh_data.link]
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = [fresh_data.link]
        mock_parser.update_fetch_chunk_size = 25
        mock_parser.fetch_gallery_data_for_update.return_value = [fresh_data]
        # Simulate general_utils finding banned data on update
        mock_parser.general_utils = MagicMock()
        mock_parser.general_utils.discard_by_gallery_data.side_effect = [
            (True, ["Banned tags: banned:bad_tag"]),  # for gallery check
            (False, []),                             # for archive check before tags set
        ]
        mock_parser.is_current_link_non_current.return_value = False

        with patch.object(self.settings.provider_context, "get_parsers", return_value=[mock_parser]):
            summary = updater.update([fresh_data.link])

        self.assertEqual(summary.updated, 1)
        # ArchiveManageEntry created for banned data
        entry = ArchiveManageEntry.objects.filter(archive=archive, mark_reason="banned_data").first()
        self.assertIsNotNone(entry)
        self.assertIn("banned:bad_tag", entry.mark_comment)

    def test_update_gallery_invalidates_wanted_match(self):
        self.settings.recheck_wanted_on_update = True
        gallery = Gallery.objects.create(
            gid="34567",
            token="tok345",
            provider="panda",
            title="Wanted Match Gallery",
        )
        archive = Archive.objects.create(
            title="Wanted Match Archive",
            gallery=gallery,
            user=self.user,
        )
        wg = WantedGallery.objects.create(
            title="Wanted Criteria",
        )
        # Link via FoundGallery
        from viewer.models import FoundGallery
        FoundGallery.objects.create(wanted_gallery=wg, gallery=gallery)

        fresh_data = GalleryData(
            gid="34567",
            token="tok345",
            provider="panda",
            title="Different Title Now",
            link="https://e-hentai.org/g/34567/tok345/",
        )

        updater = GalleryMetadataUpdater(self.settings)
        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = [fresh_data.link]
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = [fresh_data.link]
        mock_parser.update_fetch_chunk_size = 25
        mock_parser.fetch_gallery_data_for_update.return_value = [fresh_data]
        mock_parser.general_utils = MagicMock()
        mock_parser.general_utils.discard_by_gallery_data.return_value = (False, [])
        mock_parser.is_current_link_non_current.return_value = False

        # Mock match_against_wanted_galleries to return empty (meaning WG match was invalidated)
        with (
            patch.object(self.settings.provider_context, "get_parsers", return_value=[mock_parser]),
            patch.object(Gallery, "match_against_wanted_galleries", return_value=[]),
        ):
            updater.update([fresh_data.link])

        entry = ArchiveManageEntry.objects.filter(archive=archive, mark_reason="invalidated_wanted").first()
        self.assertIsNotNone(entry)
        self.assertIn(wg.title, entry.mark_comment)

    def test_update_cancellation_stops_promptly(self):
        cancelled = True
        self.settings.is_cancelled = lambda: cancelled

        updater = GalleryMetadataUpdater(self.settings)
        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = ["https://e-hentai.org/g/1/tok1/"]
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = ["https://e-hentai.org/g/1/tok1/"]
        mock_parser.update_fetch_chunk_size = 25

        with patch.object(self.settings.provider_context, "get_parsers", return_value=[mock_parser]):
            with self.assertRaises(TaskCancelledError):
                updater.update(["https://e-hentai.org/g/1/tok1/"])


    def test_update_does_not_change_visibility_of_refreshed_gallery(self):
        self.settings.add_as_public = True
        updater = GalleryMetadataUpdater(self.settings)
        fresh = GalleryData(gid="77", token="tok77", provider="panda", link="https://e-hentai.org/g/77/tok77/", title="T")
        parser = MagicMock()
        parser.general_utils = MagicMock()
        with patch("core.web.gallery_updater.sync_gallery_db_entry", return_value=MagicMock()) as sync_mock:
            self.assertTrue(updater.apply_gallery_data(parser, fresh))
        self.assertFalse(sync_mock.call_args.args[2].public)

    def test_update_continues_after_a_chunk_fails_to_fetch(self):
        updater = GalleryMetadataUpdater(self.settings)
        urls = [f"https://e-hentai.org/g/{i}/tok{i}/" for i in range(1, 5)]
        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = urls
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = urls
        mock_parser.update_fetch_chunk_size = 2
        fresh = GalleryData(gid="3", token="tok3", provider="panda", link=urls[2], title="Fresh")
        mock_parser.fetch_gallery_data_for_update.side_effect = [ConnectionError("network down"), [fresh]]

        with (
            patch.object(self.settings.provider_context, "get_parsers", return_value=[mock_parser]),
            patch.object(updater, "apply_gallery_data", return_value=True) as apply_mock,
        ):
            summary = updater.update(urls)

        self.assertEqual(mock_parser.fetch_gallery_data_for_update.call_count, 2)
        apply_mock.assert_called_once()
        self.assertEqual(summary.requested, 4)
        self.assertEqual(summary.fetched, 1)
        self.assertEqual(summary.updated, 1)
        # Two links of the failed chunk, plus the link of the second chunk that came back empty.
        self.assertEqual(summary.failed, 3)

    def test_update_does_not_swallow_cancellation_from_fetch(self):
        updater = GalleryMetadataUpdater(self.settings)
        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = ["https://e-hentai.org/g/1/tok1/"]
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = ["https://e-hentai.org/g/1/tok1/"]
        mock_parser.update_fetch_chunk_size = 25
        mock_parser.fetch_gallery_data_for_update.side_effect = TaskCancelledError("stop")
        with patch.object(self.settings.provider_context, "get_parsers", return_value=[mock_parser]):
            with self.assertRaises(TaskCancelledError):
                updater.update(["https://e-hentai.org/g/1/tok1/"])


class GalleryUpdaterWorkerIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="worker_updater", password="password123")
        self.settings = Settings(load_from_disk=False)
        self.runner = WorkerRunner(self.settings, enable_schedulers=False)

    def test_enqueue_gallery_update_client(self):
        task = client.enqueue_gallery_update(
            ["https://e-hentai.org/g/555/tok555/"],
            user=self.user,
            reason="Test Reason",
        )
        self.assertIsNotNone(task)
        self.assertEqual(task.task_type, "gallery_updater")
        self.assertEqual(task.args, ["https://e-hentai.org/g/555/tok555/"])
        self.assertEqual(task.reason, "Test Reason")
        self.assertEqual(task.event_action, "UPDATE_METADATA")

    def test_enqueue_gallery_update_skips_links_already_queued(self):
        first = client.enqueue_gallery_update(["https://e-hentai.org/g/1/a/", "https://e-hentai.org/g/2/b/"])

        # Same links in another order, or a subset: nothing new is queued.
        again = client.enqueue_gallery_update(["https://e-hentai.org/g/2/b/", "https://e-hentai.org/g/1/a/"])
        subset = client.enqueue_gallery_update(["https://e-hentai.org/g/2/b/"])
        self.assertEqual(again.pk, first.pk)
        self.assertEqual(subset.pk, first.pk)
        self.assertEqual(TaskQueue.objects.filter(task_type="gallery_updater").count(), 1)

        # Overlapping links: only the new one is queued.
        overlap = client.enqueue_gallery_update(["https://e-hentai.org/g/2/b/", "https://e-hentai.org/g/3/c/"])
        self.assertNotEqual(overlap.pk, first.pk)
        self.assertEqual(overlap.args, ["https://e-hentai.org/g/3/c/"])

        # A task that already started doesn't block a new refresh of the same link.
        TaskQueue.objects.filter(pk=first.pk).update(status=TaskQueue.Status.PROCESSING)
        fresh = client.enqueue_gallery_update(["https://e-hentai.org/g/1/a/"])
        self.assertNotEqual(fresh.pk, first.pk)

    def test_runner_claims_and_executes_gallery_updater_job(self):
        link = "https://e-hentai.org/g/666/tok666/"
        task = client.enqueue_gallery_update([link], user=self.user)

        claimed = self.runner.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, task.pk)
        self.assertEqual(claimed.status, TaskQueue.Status.PROCESSING)

        with patch("core.web.gallery_updater.GalleryMetadataUpdater.update") as mock_update:
            mock_update.return_value = UpdateSummary(requested=1, updated=1)
            self.runner.execute_job(claimed)
            mock_update.assert_called_once_with([link])

        claimed.refresh_from_db()
        self.assertEqual(claimed.status, TaskQueue.Status.COMPLETED)

    def test_gallery_updater_and_web_crawler_can_run_independently(self):
        # 1. Enqueue both a gallery_updater and a web_crawler task
        update_task = client.enqueue_gallery_update(["https://e-hentai.org/g/10/tok10/"], priority=5)
        crawler_task = client.enqueue(["https://e-hentai.org/g/20/tok20/"], task_type="web_crawler", priority=10)

        # 2. Claim first job -> gallery_updater (priority 5 < 10)
        claimed_update = self.runner.claim_next_job()
        self.assertIsNotNone(claimed_update)
        self.assertEqual(claimed_update.pk, update_task.pk)
        self.assertEqual(claimed_update.task_type, "gallery_updater")

        # 3. While gallery_updater is PROCESSING, web_crawler CAN be claimed concurrently!
        claimed_crawler = self.runner.claim_next_job()
        self.assertIsNotNone(claimed_crawler)
        self.assertEqual(claimed_crawler.pk, crawler_task.pk)
        self.assertEqual(claimed_crawler.task_type, "web_crawler")


class ProviderParserUpdateHooksTests(TestCase):
    def setUp(self):
        from django.conf import settings as django_settings

        self.settings = Settings(load_from_config=django_settings.CRAWLER_SETTINGS.config)

    def test_panda_parser_hooks(self):
        from core.providers.panda.parsers import Parser as PandaParser

        parser = PandaParser(self.settings)
        self.assertTrue(parser.supports_gallery_update())

        valid_link = "https://e-hentai.org/g/12345/abcdef1234/"
        other_link = "https://e-hentai.org/tag/artist"
        filtered = parser.filter_update_urls([valid_link, other_link])
        self.assertEqual(filtered, [valid_link])

        fake_gdata = GalleryData(gid="12345", provider="panda", token="abcdef1234", link=valid_link)
        with patch.object(parser, "get_values_from_gallery_link_list", return_value=[fake_gdata]):
            results = parser.fetch_gallery_data_for_update([valid_link])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].gid, "12345")

    def test_chaika_parser_hooks(self):
        from core.providers.chaika.parsers import Parser as ChaikaParser

        parser = ChaikaParser(self.settings)
        self.assertTrue(parser.supports_gallery_update())

        chaika_url = parser.own_settings.url.rstrip("/")
        valid_link = f"{chaika_url}/gallery/98765/"
        other_link = f"{chaika_url}/random_page/"
        filtered = parser.filter_update_urls([valid_link, other_link])
        self.assertEqual(filtered, [valid_link])

        fake_dict = {
            "gid": "98765",
            "provider": "chaika",
            "title": "Chaika Test Gallery",
            "posted": 1700000000,
        }
        with patch.object(parser, "fetch_gallery_dicts_from_link", return_value=[fake_dict]):
            results = parser.fetch_gallery_data_for_update([valid_link])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].gid, "98765")
            self.assertEqual(results[0].title, "Chaika Test Gallery")
