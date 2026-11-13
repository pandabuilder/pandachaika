import json
import os
import signal
import unittest
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
import django.utils.timezone as django_tz

from core.base.setup import Settings
from viewer.views import admin
from workers.models import SchedulerState, WorkerProcess
from workers.runner import WorkerRunner

User = get_user_model()


class WebCrawlerViewTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_crawler",
            email="admin_crawler@example.com",
            password="testpassword",
        )
        self.non_staff_user = User.objects.create_user(
            username="normal_user",
            email="normal_user@example.com",
            password="testpassword",
        )

    def test_crawler_page_get_requires_staff(self):
        self.client.login(username="normal_user", password="testpassword")
        resp = self.client.get(reverse("viewer:crawler"), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "You need to be an admin")

    def test_crawler_page_renders_options_and_priority(self):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.get(reverse("viewer:crawler"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "viewer/crawler.html")
        # Check priority input exists with default value 10
        self.assertContains(resp, 'name="priority"')
        self.assertContains(resp, 'value="10"')
        self.assertContains(resp, "Priority (1-10)")
        # Check replace metadata explanation
        self.assertContains(resp, "Replace metadata")
        self.assertContains(resp, "Re-fetches and overwrites existing gallery metadata")
        # Check ignore global filters checkbox
        self.assertContains(resp, 'name="ignore_global_filters"')
        self.assertContains(resp, "Ignore global filters")
        # Check Keep this settings note about restarting background workers
        self.assertContains(resp, "Keep this settings")
        self.assertContains(resp, "Settings saved to <code>settings.yaml</code> will only be applied to background workers when restarted")
        # Check localStorage buttons
        self.assertContains(resp, "btn-save-local-settings")
        self.assertContains(resp, "Save current settings")
        self.assertContains(resp, "btn-clear-local-settings")
        # Check multipart form and file upload input
        self.assertContains(resp, 'enctype="multipart/form-data"')
        self.assertContains(resp, 'name="urls_file"')
        self.assertContains(resp, 'type="file"')
        # Check split into groups option
        self.assertContains(resp, 'name="split_size"')
        self.assertContains(resp, "Split into groups")

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_from_uploaded_file(self, mock_enqueue):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.login(username="admin_crawler", password="testpassword")
        file_content = b"https://example.com/g/file1/abc\nhttps://example.com/g/file2/def\n\n"
        uploaded = SimpleUploadedFile("urls.txt", file_content, content_type="text/plain")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "",
                "urls_file": uploaded,
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["priority"], 10)
        self.assertEqual(kwargs["task_type"], "web_crawler")
        self.assertEqual(
            sorted(kwargs["args"]),
            ["https://example.com/g/file1/abc", "https://example.com/g/file2/def"],
        )

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_from_both_textarea_and_uploaded_file(self, mock_enqueue):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.login(username="admin_crawler", password="testpassword")
        file_content = "https://example.com/g/file/123\r\nhttps://example.com/g/shared/dup\r\n".encode("utf-8-sig")
        uploaded = SimpleUploadedFile("links.txt", file_content, content_type="text/plain")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/textarea/456\nhttps://example.com/g/shared/dup",
                "urls_file": uploaded,
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(
            sorted(kwargs["args"]),
            [
                "https://example.com/g/file/123",
                "https://example.com/g/shared/dup",
                "https://example.com/g/textarea/456",
            ],
        )

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_split_links_from_textarea(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": (
                    "https://example.com/g/1\n"
                    "https://example.com/g/2\n"
                    "https://example.com/g/3\n"
                    "https://example.com/g/4\n"
                    "https://example.com/g/5"
                ),
                "split_size": "2",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mock_enqueue.call_count, 3)
        calls = mock_enqueue.call_args_list
        self.assertEqual(calls[0].kwargs["args"], ["https://example.com/g/1", "https://example.com/g/2"])
        self.assertEqual(calls[1].kwargs["args"], ["https://example.com/g/3", "https://example.com/g/4"])
        self.assertEqual(calls[2].kwargs["args"], ["https://example.com/g/5"])
        for call in calls:
            self.assertEqual(call.kwargs["priority"], 10)
            self.assertEqual(call.kwargs["task_type"], "web_crawler")

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_split_links_from_uploaded_file(self, mock_enqueue):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.login(username="admin_crawler", password="testpassword")
        file_content = (
            b"https://example.com/f/1\n"
            b"https://example.com/f/2\n"
            b"https://example.com/f/3\n"
            b"https://example.com/f/4\n"
            b"https://example.com/f/5\n"
            b"https://example.com/f/6\n"
        )
        uploaded = SimpleUploadedFile("urls.txt", file_content, content_type="text/plain")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "",
                "urls_file": uploaded,
                "split_size": "3",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mock_enqueue.call_count, 2)
        calls = mock_enqueue.call_args_list
        self.assertEqual(
            calls[0].kwargs["args"],
            ["https://example.com/f/1", "https://example.com/f/2", "https://example.com/f/3"],
        )
        self.assertEqual(
            calls[1].kwargs["args"],
            ["https://example.com/f/4", "https://example.com/f/5", "https://example.com/f/6"],
        )

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_split_links_from_both_sources_with_flags(self, mock_enqueue):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.login(username="admin_crawler", password="testpassword")
        file_content = b"https://example.com/f/1\nhttps://example.com/f/2\nhttps://example.com/f/3\n"
        uploaded = SimpleUploadedFile("urls.txt", file_content, content_type="text/plain")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/t/1\nhttps://example.com/t/2",
                "urls_file": uploaded,
                "split_size": "2",
                "wanted_only": "1",
                "ignore_global_filters": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(mock_enqueue.call_count, 3)
        calls = mock_enqueue.call_args_list
        # Each call must have --wanted-only at start, --ignore-global-filters at end, and chunk links in between
        self.assertEqual(
            calls[0].kwargs["args"],
            ["--wanted-only", "https://example.com/t/1", "https://example.com/t/2", "--ignore-global-filters"],
        )
        self.assertEqual(
            calls[1].kwargs["args"],
            ["--wanted-only", "https://example.com/f/1", "https://example.com/f/2", "--ignore-global-filters"],
        )
        self.assertEqual(
            calls[2].kwargs["args"],
            ["--wanted-only", "https://example.com/f/3", "--ignore-global-filters"],
        )

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_split_size_zero_or_invalid_does_not_split(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        for invalid_size in ("0", "-3", "not_a_number", ""):
            mock_enqueue.reset_mock()
            resp = self.client.post(
                reverse("viewer:crawler"),
                data={
                    "urls": "https://example.com/g/1\nhttps://example.com/g/2",
                    "split_size": invalid_size,
                },
            )
            self.assertEqual(resp.status_code, 302)
            mock_enqueue.assert_called_once()
            self.assertEqual(
                sorted(mock_enqueue.call_args.kwargs["args"]),
                ["https://example.com/g/1", "https://example.com/g/2"],
            )

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_default_priority_10(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc\nhttps://example.com/g/456/def",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertEqual(kwargs["priority"], 10)
        self.assertEqual(kwargs["task_type"], "web_crawler")
        self.assertEqual(sorted(kwargs["args"]), ["https://example.com/g/123/abc", "https://example.com/g/456/def"])

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_custom_priority(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "priority": "3",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["priority"], 3)

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_legacy_run_separate_priority_5(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "run_separate": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["priority"], 5)

    @patch("workers.client.enqueue")
    @patch.object(admin.crawler_settings, "write")
    @patch.object(admin.crawler_settings, "load_config_from_file")
    def test_crawler_keep_this_settings_saves_transferable_options(self, mock_reload, mock_write, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "priority": "2",
                "keep_this_settings": "1",
                "replace_metadata": "1",
                "skip-non-current": "1",
                "downloaders-chaika_archive": "8",
                "reason": "Test crawler reason",
                "gallery_only": "1",
                "wanted_only": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        # Verify write was called
        mock_write.assert_called_once()
        mock_reload.assert_called_once()

        # Check transferable options updated in crawler_settings.config
        cfg = admin.crawler_settings.config
        self.assertTrue(cfg["allowed"]["replace_metadata"])
        self.assertTrue(cfg["allowed"]["non_current_links_as_deleted"])
        self.assertEqual(cfg["downloaders"]["chaika_archive"], 8)

        # Non-transferable options like gallery_only / wanted_only modify job options, not settings.yaml
        mock_enqueue.assert_called_once()
        queued_options = mock_enqueue.call_args.kwargs["override_options"]
        self.assertEqual(queued_options.archive_reason, "Test crawler reason")
        self.assertTrue(queued_options.non_current_links_as_deleted)
        self.assertTrue(queued_options.replace_metadata)
        self.assertIn("--wanted-only", mock_enqueue.call_args.kwargs["args"])

    def test_settings_load_from_config_non_current_links_as_deleted_precedence(self):
        """Ensure settings loaded from config preserve allowed.non_current_links_as_deleted even when general section exists."""
        s = Settings(
            load_from_config={
                "allowed": {"non_current_links_as_deleted": True},
                "general": {"non_current_links_as_deleted": False},
            }
        )
        self.assertTrue(s.non_current_links_as_deleted)

        s2 = Settings(
            load_from_config={
                "allowed": {"non_current_links_as_deleted": True},
                "general": {},
            }
        )
        self.assertTrue(s2.non_current_links_as_deleted)

    def test_panda_parser_post_processing_propagates_override_options_to_chain(self):
        """Ensure chained parent/first galleries enqueued by panda parser preserve caller's override_options."""
        from core.base.types import GalleryData
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        settings.non_current_links_as_deleted = True
        mock_queue = MagicMock()
        # settings.workers is shared by every Settings instance; restore web_queue after the test.
        web_queue_patcher = patch.object(settings.workers, "web_queue", mock_queue)
        web_queue_patcher.start()
        self.addCleanup(web_queue_patcher.stop)

        parser = PandaParser(settings)
        parser.own_settings.auto_process_parent = True
        parser.own_settings.auto_process_first = True

        mock_gallery_entry = MagicMock()
        mock_gallery_entry.gid = 999
        mock_gallery_entry.get_absolute_url.return_value = "https://example.com/g/999"

        gallery_data = GalleryData(
            gid="999",
            provider="panda",
            link="https://example.com/g/999",
        )
        gallery_data.extra_data = {
            "parent_gid": "888",
            "parent_key": "abc",
            "first_gid": "777",
            "first_key": "def",
        }

        with patch("core.providers.panda.parsers.logger"):
            parser.post_gallery_processing(mock_gallery_entry, gallery_data)

        self.assertEqual(mock_queue.enqueue_args_list.call_count, 2)
        for call in mock_queue.enqueue_args_list.call_args_list:
            self.assertIn("override_options", call.kwargs)
            self.assertIs(call.kwargs["override_options"], settings)

    def test_panda_parser_post_processing_skips_enqueuing_existing_galleries_and_links_directly(self):
        """Ensure post_gallery_processing skips enqueuing galleries that already exist in DB and links them directly."""
        from core.base.types import GalleryData
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings
        from viewer.models import Gallery

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        mock_queue = MagicMock()
        # settings.workers is shared by every Settings instance; restore web_queue after the test.
        web_queue_patcher = patch.object(settings.workers, "web_queue", mock_queue)
        web_queue_patcher.start()
        self.addCleanup(web_queue_patcher.stop)

        parser = PandaParser(settings)
        parser.own_settings.auto_process_newer = True
        parser.own_settings.auto_process_parent = True
        parser.own_settings.auto_process_first = True

        parent_gallery = Gallery.objects.create(gid="888", provider="panda", title="Parent Gallery")
        first_gallery = Gallery.objects.create(gid="777", provider="panda", title="First Gallery")
        current_gallery = Gallery.objects.create(gid="999", provider="panda", title="Current Gallery")
        entry = Gallery.objects.create(gid="800", provider="panda", title="Child Gallery")

        gallery_data = GalleryData(
            gid="800",
            provider="panda",
            link="https://e-hentai.org/g/800/token800/",
        )
        gallery_data.extra_data = {
            "current_gid": "999",
            "current_key": "key999",
            "parent_gid": "888",
            "parent_key": "key888",
            "first_gid": "777",
            "first_key": "key777",
        }

        with patch("core.providers.panda.parsers.logger"):
            parser.post_gallery_processing(entry, gallery_data)

        # Nothing should be enqueued since all three exist in DB
        self.assertEqual(mock_queue.enqueue_args_list.call_count, 0)

        # Relationships should be linked directly on entry
        entry.refresh_from_db()
        self.assertEqual(entry.parent_gallery_id, parent_gallery.id)
        self.assertEqual(entry.first_gallery_id, first_gallery.id)

    def test_panda_parser_post_processing_skips_caller_and_self_requeue(self):
        """Ensure post_gallery_processing does not re-enqueue caller or self to avoid infinite loops."""
        from core.base.types import GalleryData
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings
        from viewer.models import Gallery

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        # Simulate caller was child gallery 999
        settings.link_child = "999"
        mock_queue = MagicMock()
        # settings.workers is shared by every Settings instance; restore web_queue after the test.
        web_queue_patcher = patch.object(settings.workers, "web_queue", mock_queue)
        web_queue_patcher.start()
        self.addCleanup(web_queue_patcher.stop)

        parser = PandaParser(settings)
        parser.own_settings.auto_process_newer = True
        parser.own_settings.auto_process_parent = True
        parser.own_settings.auto_process_first = True

        entry = Gallery.objects.create(gid="888", provider="panda", title="Parent Gallery")

        gallery_data = GalleryData(
            gid="888",
            provider="panda",
            link="https://e-hentai.org/g/888/key888/",
        )
        # current_gid points to caller 999 (should be skipped due to is_caller check),
        # first_gid is self 888 (should be skipped due to is_self check)
        gallery_data.extra_data = {
            "current_gid": "999",
            "current_key": "key999",
            "first_gid": "888",
            "first_key": "key888",
        }

        with patch("core.providers.panda.parsers.logger"):
            parser.post_gallery_processing(entry, gallery_data)

        self.assertEqual(mock_queue.enqueue_args_list.call_count, 0)

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_with_ignore_global_filters(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "ignore_global_filters": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertIn("--ignore-global-filters", kwargs["args"])
        self.assertTrue(kwargs["override_options"].ignore_global_filters)

    def test_user_crawler_page_renders_ignore_global_filters(self):
        from django.contrib.auth.models import Permission
        perm = Permission.objects.get(codename="crawler_adder")
        self.non_staff_user.user_permissions.add(perm)
        self.client.login(username="normal_user", password="testpassword")
        resp = self.client.get(reverse("viewer:user-crawler"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="ignore-global-filters"')
        self.assertContains(resp, "Ignore global filters")

    @patch("workers.client.enqueue")
    def test_user_crawler_enqueue_with_ignore_global_filters(self, mock_enqueue):
        from django.contrib.auth.models import Permission
        perm = Permission.objects.get(codename="crawler_adder")
        self.non_staff_user.user_permissions.add(perm)
        self.client.login(username="normal_user", password="testpassword")
        resp = self.client.post(
            reverse("viewer:user-crawler"),
            data={
                "urls": "https://example.com/g/123/abc",
                "ignore-global-filters": "1",
            },
            HTTP_REFERER="/upload/",
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertIn("--ignore-global-filters", kwargs["args"])
        self.assertTrue(kwargs["override_options"].ignore_global_filters)

    @patch("workers.client.enqueue")
    def test_crawler_enqueue_with_silent_processing(self, mock_enqueue):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.post(
            reverse("viewer:crawler"),
            data={
                "urls": "https://e-hentai.org/g/123/abc",
                "silent_processing": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once()
        kwargs = mock_enqueue.call_args.kwargs
        self.assertTrue(kwargs["override_options"].silent_processing)

    def test_crawler_page_renders_silent_processing(self):
        self.client.login(username="admin_crawler", password="testpassword")
        resp = self.client.get(reverse("viewer:crawler"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="silent_processing"')
        self.assertContains(resp, "Fast bulk mode (silent processing)")

    def test_panda_parser_crawl_urls_small_batch_logs_individual_links(self):
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        parser = PandaParser(settings)
        parser.downloaders = [MagicMock()]
        parser.api_request_function = MagicMock(return_value=None)

        urls = [
            "https://e-hentai.org/g/1001/abc1001",
            "https://e-hentai.org/g/1002/abc1002",
        ]

        with patch("core.providers.panda.parsers.logger") as mock_logger:
            parser.crawl_urls(urls)
            info_calls = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
            self.assertTrue(any("Provided URL https://e-hentai.org/g/1001/abc1001 is a gallery link, adding" in str(msg) for msg in info_calls))

    def test_panda_parser_crawl_urls_bulk_batch_summarizes_logging(self):
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        parser = PandaParser(settings)
        parser.downloaders = [MagicMock()]
        parser.api_request_function = MagicMock(return_value=None)

        urls = [f"https://e-hentai.org/g/{i}/abc{i}" for i in range(100, 200)]

        with patch("core.providers.panda.parsers.logger") as mock_logger:
            parser.crawl_urls(urls)
            formatted_messages = [
                (c.args[0] % c.args[1:] if len(c.args) > 1 else c.args[0])
                for c in mock_logger.info.call_args_list
                if c.args
            ]
            # Should NOT log individual gallery link additions in info
            self.assertFalse(any("is a gallery link, adding" in str(msg) for msg in formatted_messages))
            # Should log summarized classification
            self.assertTrue(any("Extracted 100 unique gallery links" in str(msg) for msg in formatted_messages))

    def test_panda_parser_crawl_urls_silent_processing_suppresses_logs(self):
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings

        settings = Settings(load_from_disk=False)
        settings.silent_processing = True
        settings.providers["panda"] = PandaSettings(settings, {})
        parser = PandaParser(settings)
        parser.downloaders = [MagicMock()]
        parser.api_request_function = MagicMock(return_value=None)

        urls = [
            "https://e-hentai.org/g/1001/abc1001",
            "https://e-hentai.org/g/1002/abc1002",
        ]

        with patch("core.providers.panda.parsers.logger") as mock_logger:
            parser.crawl_urls(urls)
            info_calls = [c.args[0] for c in mock_logger.info.call_args_list if c.args]
            self.assertFalse(any("is a gallery link, adding" in str(msg) for msg in info_calls))

    def test_panda_parser_crawl_urls_logging_flow_summary(self):
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings
        import json

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        parser = PandaParser(settings)
        parser.downloaders = [MagicMock()]

        urls = [f"https://e-hentai.org/g/{1000 + i}/tok{1000 + i}" for i in range(100)]

        def make_entry(gid, token, tags):
            return {
                "gid": str(gid),
                "token": token,
                "title": f"Gallery {gid}",
                "title_jpn": f"Gallery {gid} JPN",
                "thumb": "https://example.com/thumb.jpg",
                "category": "Manga",
                "uploader": "test_uploader",
                "posted": 1600000000,
                "filecount": 10,
                "filesize": 1000,
                "expunged": False,
                "rating": "4.5",
                "tags": tags,
                "torrents": [],
                "torrentcount": 0,
            }

        def fake_api(url, request_dict, post=True):
            req_data = json.loads(request_dict["data"])
            glist = req_data.get("gidlist", [])
            gmetadata = []
            for gid, token in glist:
                gid_int = int(gid)
                if gid_int == 1000:
                    gmetadata.append({"gid": str(gid), "error": "Key missing"})
                elif gid_int <= 1014:
                    gmetadata.append(make_entry(gid, token, ["female:sole_female"]))
                else:
                    gmetadata.append(make_entry(gid, token, ["misc:random"]))
            mock_resp = MagicMock()
            mock_resp.text = json.dumps({"gmetadata": gmetadata})
            mock_resp.json.return_value = {"gmetadata": gmetadata}
            return mock_resp

        parser.api_request_function = fake_api

        wanted_filters = MagicMock()
        def mock_compare(gallery_data, link, wf, gallery_wanted_lists):
            if "female:sole_female" in gallery_data.tags:
                gallery_wanted_lists[gallery_data.gid].append(MagicMock())
        parser.compare_gallery_with_wanted_filters = mock_compare

        with patch("core.providers.panda.parsers.logger") as mock_logger:
            parser.crawl_urls(urls, wanted_filters=wanted_filters, wanted_only=True)
            formatted = [
                (c.args[0] % c.args[1:] if len(c.args) > 1 else c.args[0])
                for c in mock_logger.info.call_args_list
                if c.args
            ]
            self.assertTrue(
                any(
                    "Database check for 100 galleries: 0 existing in database (all 100 new)." in msg
                    for msg in formatted
                )
            )
            self.assertTrue(any("Queued 100 galleries to fetch metadata" in msg for msg in formatted))
            self.assertTrue(
                any(
                    "Metadata check for 100 galleries: 14 matched wanted filters, 85 skipped as non-matching (wanted-only mode), 1 API/network errors."
                    in msg
                    for msg in formatted
                )
            )

    def test_panda_parser_crawl_urls_monitored_link_silent_processing_logs_summaries(self):
        from core.providers.panda.parsers import Parser as PandaParser
        from core.providers.panda.settings import OwnSettings as PandaSettings
        import json

        settings = Settings(load_from_disk=False)
        settings.silent_processing = True
        mock_ml = MagicMock()
        mock_ml.name = "My EH Monitored Feed"
        settings.monitored_link = mock_ml
        settings.providers["panda"] = PandaSettings(settings, {})
        parser = PandaParser(settings)
        parser.downloaders = [MagicMock()]

        urls = [f"https://e-hentai.org/g/{2000 + i}/tok{2000 + i}" for i in range(20)]

        def make_entry(gid, token, tags):
            return {
                "gid": str(gid),
                "token": token,
                "title": f"Gallery {gid}",
                "title_jpn": f"Gallery {gid} JPN",
                "thumb": "https://example.com/thumb.jpg",
                "category": "Manga",
                "uploader": "test_uploader",
                "posted": 1600000000,
                "filecount": 10,
                "filesize": 1000,
                "expunged": False,
                "rating": "4.5",
                "tags": tags,
                "torrents": [],
                "torrentcount": 0,
            }

        def fake_api(url, request_dict, post=True):
            req_data = json.loads(request_dict["data"])
            glist = req_data.get("gidlist", [])
            gmetadata = [make_entry(gid, token, ["female:sole_female"]) for gid, token in glist]
            mock_resp = MagicMock()
            mock_resp.text = json.dumps({"gmetadata": gmetadata})
            mock_resp.json.return_value = {"gmetadata": gmetadata}
            return mock_resp

        parser.api_request_function = fake_api

        with patch("core.providers.panda.parsers.logger") as mock_logger:
            parser.crawl_urls(urls)
            formatted = [
                (c.args[0] % c.args[1:] if len(c.args) > 1 else c.args[0])
                for c in mock_logger.info.call_args_list
                if c.args
            ]
            self.assertTrue(any("for monitored link 'My EH Monitored Feed'" in msg for msg in formatted))
            self.assertTrue(any("Database check for 20 galleries:" in msg for msg in formatted))
            self.assertTrue(any("Queued 20 galleries to fetch metadata" in msg for msg in formatted))
            self.assertTrue(any("Metadata check for 20 galleries: 20 to process." in msg for msg in formatted))
            self.assertFalse(any("is a gallery link, adding" in msg for msg in formatted))


class WebCrawlerArgsAndFiltersTest(TestCase):
    def test_webcrawler_cli_argument_parsing(self):
        from core.web.crawler import WebCrawler
        settings = Settings(load_from_disk=False)
        crawler = WebCrawler(settings)

        # Long option
        args = crawler.get_args(["--ignore-global-filters", "https://example.com/g/123/abc"])
        self.assertTrue(args.ignore_global_filters)
        self.assertEqual(args.url, ["https://example.com/g/123/abc"])

        # Short option
        args_short = crawler.get_args(["-igf", "https://example.com/g/456/def"])
        self.assertTrue(args_short.ignore_global_filters)
        self.assertEqual(args_short.url, ["https://example.com/g/456/def"])

        # Default (not set)
        args_default = crawler.get_args(["https://example.com/g/789/ghi"])
        self.assertFalse(args_default.ignore_global_filters)

    def test_discard_by_gallery_data_respects_ignore_global_filters(self):
        from core.base.setup_utilities import GeneralUtils

        settings = Settings(load_from_disk=False)
        settings.banned_tags = ["banned_tag_1", "banned_tag_2"]
        settings.banned_uploaders = ["bad_uploader"]
        utils = GeneralUtils(settings)

        # Normal mode: matching tags should discard
        discarded, reasons = utils.discard_by_gallery_data(["banned_tag_1", "safe_tag"], uploader="normal_user")
        self.assertTrue(discarded)
        self.assertIn("Banned tags: banned_tag_1", reasons[0])

        # Normal mode: matching uploader should discard
        discarded, reasons = utils.discard_by_gallery_data(["safe_tag"], uploader="bad_uploader")
        self.assertTrue(discarded)
        self.assertEqual(reasons, ["Banned uploader: bad_uploader"])

        # When ignore_global_filters is True: neither tags nor uploader discard
        settings.ignore_global_filters = True
        discarded, reasons = utils.discard_by_gallery_data(["banned_tag_1", "safe_tag"], uploader="bad_uploader")
        self.assertFalse(discarded)
        self.assertEqual(reasons, [])

    def test_start_crawling_parse_args_sets_ignore_global_filters_on_settings(self):
        from core.web.crawler import WebCrawler
        settings = Settings(load_from_disk=False)
        crawler = WebCrawler(settings)

        with patch.object(crawler, "get_args") as mock_get_args:
            mock_args = MagicMock()
            mock_args.ignore_global_filters = True
            mock_args.wait_timer = None
            mock_args.queue_archive_downloads = None
            mock_args.retry_failed = False
            mock_args.retry_dl_type = False
            mock_args.retry_wrong_filesize = False
            mock_args.update_newer_than = None
            mock_args.update_missing_thumbnails = None
            mock_args.set_reason = None
            mock_args.set_details = None
            mock_args.stop_nested = False
            mock_args.link_child = None
            mock_args.link_newer = None
            mock_args.preselect_wanted_match = None
            mock_args.include_providers = None
            mock_args.crawl_from_feed = False
            mock_args.url = []
            mock_args.json_source = None
            mock_get_args.return_value = mock_args

            with patch.object(settings.provider_context, "get_parsers", return_value=[]):
                crawler.start_crawling_parse_args(["--ignore-global-filters"])
            self.assertTrue(settings.ignore_global_filters)


class RestartWorkersApiAndToolsTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_restart",
            email="admin_restart@example.com",
            password="testpassword",
        )
        self.normal_user = User.objects.create_user(
            username="normal_restart",
            email="normal_restart@example.com",
            password="testpassword",
        )

    def test_api_restart_workers_unauthorized(self):
        self.client.login(username="normal_restart", password="testpassword")
        resp = self.client.post(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp.status_code, 401)
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "Not allowed")

    def test_restart_workers_require_post(self):
        self.client.login(username="admin_restart", password="testpassword")
        resp_api = self.client.get(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp_api.status_code, 405)
        resp_id = self.client.get(reverse("viewer:tools-id", args=["restart_workers"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp_id.status_code, 405)

    def test_api_restart_workers_no_running_workers(self):
        self.client.login(username="admin_restart", password="testpassword")
        SchedulerState.objects.filter(name="worker_runner").delete()
        WorkerProcess.objects.all().delete()

        resp = self.client.post(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("Restart command queued", data["message"])
        self.assertEqual(data["worker_pids"], [])

        # Verify command queued in SchedulerState
        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "restart")

    @patch("os.kill")
    def test_api_restart_workers_with_active_workers(self, mock_os_kill):
        import socket
        self.client.login(username="admin_restart", password="testpassword")
        now = django_tz.now()
        wp = WorkerProcess.objects.create(
            pid=98765,
            hostname=socket.gethostname(),
            status="running",
            last_heartbeat=now,
        )

        resp = self.client.post(reverse("viewer:tools-api", args=["restart_workers"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("Restart requested for 1 running worker process", data["message"])
        self.assertEqual(data["worker_pids"], [98765])

        if hasattr(signal, "SIGHUP"):
            mock_os_kill.assert_called_with(98765, signal.SIGHUP)

        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "restart")

    @patch("os.kill")
    def test_admin_view_tools_restart_workers(self, mock_os_kill):
        import socket
        self.client.login(username="admin_restart", password="testpassword")
        WorkerProcess.objects.create(
            pid=54321,
            hostname=socket.gethostname(),
            status="running",
            last_heartbeat=django_tz.now(),
        )
        resp = self.client.post(
            reverse("viewer:tools-id", args=["restart_workers"]),
            HTTP_REFERER="/tools/",
        )
        self.assertEqual(resp.status_code, 302)
        if hasattr(signal, "SIGHUP"):
            mock_os_kill.assert_called_with(54321, signal.SIGHUP)

        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "restart")

    def test_tools_page_contains_restart_workers_link(self):
        self.client.login(username="admin_restart", password="testpassword")
        resp = self.client.get(reverse("viewer:tools"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("viewer:tools-id", args=["restart_workers"]))
        self.assertContains(resp, "Restart Workers")


class WorkerRunnerRestartTests(TestCase):
    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.runner = WorkerRunner(self.settings, enable_schedulers=False)

    def test_check_scheduler_commands_handles_restart_ipc(self):
        SchedulerState.request_command("worker_runner", "restart")
        self.runner.running = True
        self.runner.should_restart = False

        self.runner.check_scheduler_commands()

        self.assertTrue(self.runner.should_restart)
        self.assertFalse(self.runner.running)
        state = SchedulerState.objects.get(name="worker_runner")
        self.assertEqual(state.command_requested, "")

    def test_handle_restart_signal(self):
        self.runner.running = True
        self.runner.should_restart = False

        self.runner._handle_restart(signal.SIGHUP if hasattr(signal, "SIGHUP") else 1, None)

        self.assertTrue(self.runner.should_restart)
        self.assertFalse(self.runner.running)

    @patch.object(WorkerRunner, "_restart_process")
    def test_runner_run_invokes_restart_process_when_requested(self, mock_restart_process):
        runner = WorkerRunner(self.settings, enable_schedulers=False)
        runner.should_restart = True

        def stop_loop():
            runner.running = False
            return None

        with patch.object(runner, "claim_next_job", side_effect=stop_loop):
            runner.run()

        mock_restart_process.assert_called_once()


class RestartWebServerTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_webserver",
            email="admin_webserver@example.com",
            password="testpassword",
        )
        self.client.login(username="admin_webserver", password="testpassword")

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "requires SIGHUP")
    @patch("os.getppid", return_value=4321)
    @patch("os.kill")
    def test_restart_viewer_under_gunicorn_signals_master(self, mock_os_kill, _mock_getppid):
        resp = self.client.post(
            reverse("viewer:tools-id", args=["restart_viewer"]),
            HTTP_REFERER="/tools/",
            SERVER_SOFTWARE="gunicorn/26.2.0",
        )
        self.assertEqual(resp.status_code, 302)
        mock_os_kill.assert_called_once_with(4321, signal.SIGHUP)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "requires SIGHUP")
    @patch("os.getppid", return_value=4321)
    @patch("os.kill")
    def test_api_restart_viewer_under_gunicorn_signals_master(self, mock_os_kill, _mock_getppid):
        resp = self.client.post(
            reverse("viewer:tools-api", args=["restart_viewer"]),
            SERVER_SOFTWARE="gunicorn/26.2.0",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Gunicorn", json.loads(resp.content)["message"])
        mock_os_kill.assert_called_once_with(4321, signal.SIGHUP)

    @unittest.skipUnless(hasattr(signal, "SIGUSR2"), "requires SIGUSR2")
    @patch("os.kill")
    def test_restart_viewer_without_gunicorn_signals_current_process(self, mock_os_kill):
        resp = self.client.post(reverse("viewer:tools-id", args=["restart_viewer"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        mock_os_kill.assert_called_once_with(os.getpid(), signal.SIGUSR2)


class ToolsPageCleanupTests(TestCase):
    def setUp(self):
        self.client = Client()
        User.objects.create_superuser(
            username="admin_tools_page",
            email="admin_tools_page@example.com",
            password="testpassword",
        )
        self.client.login(username="admin_tools_page", password="testpassword")

    def test_tools_page_no_longer_lists_scheduler_controls_or_transfer(self):
        resp = self.client.get(reverse("viewer:tools"))
        self.assertEqual(resp.status_code, 200)
        for removed in (
            "transfer_missing_downloads",
            "start_web_queue",
            "start_timed_dl",
            "force_run_timed_updater",
            "start_archive_download_queue",
            "Stop Web queue",
            "Restart Viewer",
        ):
            self.assertNotContains(resp, removed)

    def test_tools_page_links_to_workers_dashboard_and_web_server_restart(self):
        resp = self.client.get(reverse("viewer:tools"))
        self.assertContains(resp, reverse("viewer:stats-workers"))
        self.assertContains(resp, reverse("viewer:tools-id", args=["restart_viewer"]))
        self.assertContains(resp, "Restart Web Server")

    def test_removed_scheduler_tools_are_not_actions_anymore(self):
        resp = self.client.post(reverse("viewer:tools-api", args=["start_timed_dl"]))
        self.assertEqual(json.loads(resp.content).get("error"), "Missing parameters")
