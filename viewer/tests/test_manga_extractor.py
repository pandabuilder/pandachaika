import io
import os
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.files import File
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image as PImage

from core.base.config_models import (
    MangaExtractorProfile,
    MangaExtractorRequestConfig,
    MangaExtractorSettings,
    MangaExtractorStorageConfig,
)
from core.base.setup import Settings
from viewer.models import Archive, Image, ItemProperties, Tag
from viewer.services import ArchiveContextualExtractorService, ArchiveIngestionService
from workers.models import TaskQueue
from workers.serialization import deserialize_settings_options, serialize_settings_options


@override_settings(COMPRESS_ENABLED=False, COMPRESS_OFFLINE=False)
class MangaExtractorIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ctx_user", password="password123")
        archive_ct = ContentType.objects.get_for_model(Archive)
        self.extract_perm, _ = Permission.objects.get_or_create(
            codename="extract_contextual_info",
            content_type=archive_ct,
            defaults={"name": "Can extract contextual information"},
        )
        self.manage_perm, _ = Permission.objects.get_or_create(
            codename="manage_archive",
            content_type=archive_ct,
            defaults={"name": "Can manage archive"},
        )
        self.user.user_permissions.add(self.extract_perm, self.manage_perm)

        self.prev_enable = settings.CRAWLER_SETTINGS.manga_extractor.enable
        self.prev_run_on_new = settings.CRAWLER_SETTINGS.manga_extractor.run_on_new_archive
        self.prev_url = settings.CRAWLER_SETTINGS.manga_extractor.url

        settings.CRAWLER_SETTINGS.manga_extractor.enable = True
        settings.CRAWLER_SETTINGS.manga_extractor.url = "http://127.0.0.1:8099"
        settings.CRAWLER_SETTINGS.manga_extractor.run_on_new_archive = False

    def tearDown(self):
        settings.CRAWLER_SETTINGS.manga_extractor.enable = self.prev_enable
        settings.CRAWLER_SETTINGS.manga_extractor.run_on_new_archive = self.prev_run_on_new
        settings.CRAWLER_SETTINGS.manga_extractor.url = self.prev_url

    def _create_zip_archive(self, title: str = "Contextual Test Archive", num_pages: int = 2) -> tuple[Archive, str]:
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(temp_zip.name, "w") as zf:
            for i in range(1, num_pages + 1):
                img = PImage.new("RGB", (200, 300), color=(i * 30, i * 40, i * 50))
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                zf.writestr(f"page_{i:03d}.jpg", buf.getvalue())

        archive = Archive.objects.create(title=title, user=self.user, public=True)
        with open(temp_zip.name, "rb") as f:
            archive.zipped.save(f"{title.lower().replace(' ', '_')}.zip", File(f), save=False)
        ArchiveIngestionService.ingest(archive)
        return archive, temp_zip.name

    def _cleanup_archive(self, archive: Archive, temp_zip_path: str):
        try:
            if archive.zipped and os.path.exists(archive.zipped.path):
                os.remove(archive.zipped.path)
        except Exception:
            pass
        try:
            if archive.thumbnail and os.path.exists(archive.thumbnail.path):
                os.remove(archive.thumbnail.path)
        except Exception:
            pass
        try:
            if os.path.exists(temp_zip_path):
                os.remove(temp_zip_path)
        except Exception:
            pass

    def test_settings_and_worker_serialization(self):
        cfg = {
            "manga_extractor": {
                "enable": True,
                "url": "http://manga-extractor:9000",
                "auth_token": "secret-token-xyz",
                "run_on_new_archive": True,
                "timeout": 420.0,
                "poll_interval": 1.5,
                "request_config": {
                    "preset": "stats",
                    "language_hint": "en",
                    "use_cache": False,
                },
                "storage_config": {
                    "store_archive_summary": True,
                    "archive_fields": ["total_pages", "dialogue_density"],
                    "store_image_results": True,
                    "image_fields": ["panel_count", "full_transcript"],
                },
            }
        }
        s = Settings(load_from_config=cfg)
        self.assertTrue(s.manga_extractor.enable)
        self.assertEqual(s.manga_extractor.url, "http://manga-extractor:9000")
        self.assertEqual(s.manga_extractor.auth_token, "secret-token-xyz")
        self.assertEqual(s.manga_extractor.get_headers(), {"Authorization": "Bearer secret-token-xyz"})
        self.assertTrue(s.manga_extractor.run_on_new_archive)
        self.assertEqual(s.manga_extractor.request_config.preset, "stats")
        self.assertEqual(s.manga_extractor.request_config.language_hint, "en")
        self.assertFalse(s.manga_extractor.request_config.use_cache)
        self.assertEqual(s.manga_extractor.storage_config.archive_fields, ["total_pages", "dialogue_density"])

        serialized = serialize_settings_options(s)
        base_s = Settings(load_from_disk=False)
        deserialized = deserialize_settings_options(base_s, serialized)
        self.assertTrue(deserialized.manga_extractor.enable)
        self.assertEqual(deserialized.manga_extractor.url, "http://manga-extractor:9000")
        self.assertEqual(deserialized.manga_extractor.auth_token, "secret-token-xyz")
        self.assertEqual(deserialized.manga_extractor.get_headers(), {"Authorization": "Bearer secret-token-xyz"})
        self.assertEqual(deserialized.manga_extractor.request_config.preset, "stats")
        self.assertEqual(deserialized.manga_extractor.storage_config.image_fields, ["panel_count", "full_transcript"])

    @patch("viewer.services.archive_contextual_extractor.requests.get")
    @patch("viewer.services.archive_contextual_extractor.requests.post")
    def test_extract_for_archive_stores_configured_item_properties(self, mock_post, mock_get):
        archive, temp_zip_path = self._create_zip_archive(num_pages=2)
        try:
            en_tag, _ = Tag.objects.get_or_create(scope="language", name="english")
            archive.tags.add(en_tag)

            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = {
                "job_id": "job-1234",
                "status": "processing",
            }
            mock_post.return_value = mock_post_resp

            long_transcript = "Hello world dialogue! " * 30  # > 100 chars to verify TextField widening
            mock_get_resp = MagicMock()
            mock_get_resp.json.return_value = {
                "job_id": "job-1234",
                "status": "completed",
                "summary": {
                    "total_pages": 2,
                    "spread_pages_count": 1,
                    "color_pages_count": 1,
                    "total_panels": 9,
                    "total_bubbles": 12,
                    "total_words": 110,
                    "total_characters": 500,
                    "average_bubbles_per_page": 6.0,
                    "average_dialogue_density": 0.08,
                    "estimated_reading_time_minutes": 0.45,
                    "unconfigured_summary_field": "should_not_be_saved",
                },
                "results": [
                    {
                        "image_index": 0,
                        "filename": "0000_page_001.jpg",
                        "is_spread": False,
                        "spread_confidence": 0.95,
                        "color_type": "full_color",
                        "visual_density": 0.25,
                        "panel_count": 5,
                        "panel_layout": "standard",
                        "bubble_count": 7,
                        "dialogue_density": 0.11,
                        "word_count": 65,
                        "char_count": 300,
                        "transcript": long_transcript,
                        "bubble_transcripts": ["Dialogue bubble 1", "Dialogue bubble 2"],
                        "japanese_metrics": {
                            "kanji_count": 10,
                            "hiragana_count": 20,
                            "katakana_count": 5,
                            "kanji_ratio": 0.28,
                            "estimated_jlpt_level": "Intermediate (N3)",
                        },
                        "extra_unselected": "ignored",
                    },
                    {
                        "image_index": 1,
                        "filename": "0001_page_002.jpg",
                        "is_spread": True,
                        "spread_confidence": 1.0,
                        "color_type": "monochrome",
                        "visual_density": 0.18,
                        "panel_count": 4,
                        "panel_layout": "standard",
                        "bubble_count": 5,
                        "dialogue_density": 0.05,
                        "word_count": 45,
                        "char_count": 200,
                        "transcript": "Page 2 text",
                        "bubble_transcripts": ["Page 2 bubble"],
                        "japanese_metrics": {
                            "kanji_count": 2,
                            "hiragana_count": 8,
                            "katakana_count": 0,
                            "kanji_ratio": 0.2,
                            "estimated_jlpt_level": "Beginner (N5/N4)",
                        },
                    },
                ],
            }
            mock_get.return_value = mock_get_resp

            me_cfg = MangaExtractorSettings()
            me_cfg.enable = True
            me_cfg.url = "http://127.0.0.1:8099"
            me_cfg.poll_interval = 0.01

            success = ArchiveContextualExtractorService.extract_for_archive(archive, settings_override=me_cfg)
            self.assertTrue(success)

            # Verify language hint auto-detection from language:english tag
            _, post_kwargs = mock_post.call_args
            self.assertEqual(post_kwargs["data"]["language_hint"], "en")

            archive_ct = ContentType.objects.get_for_model(Archive)
            archive_props = dict(
                ItemProperties.objects.filter(
                    content_type=archive_ct, object_id=archive.pk, tag="manga-extractor"
                ).values_list("name", "value")
            )
            self.assertEqual(
                archive_props,
                {
                    "total_pages": "2",
                    "spread_pages_count": "1",
                    "color_pages_count": "1",
                    "total_panels": "9",
                    "total_bubbles": "12",
                    "total_words": "110",
                    "total_characters": "500",
                    "average_bubbles_per_page": "6.0",
                    "average_dialogue_density": "0.08",
                    "estimated_reading_time_minutes": "0.45",
                },
            )
            self.assertNotIn("unconfigured_summary_field", archive_props)

            image_ct = ContentType.objects.get_for_model(Image)
            images = list(archive.image_set.order_by("archive_position"))
            img1_props = dict(
                ItemProperties.objects.filter(
                    content_type=image_ct, object_id=images[0].pk, tag="manga-extractor"
                ).values_list("name", "value")
            )
            self.assertEqual(img1_props["panel_count"], "5")
            self.assertEqual(img1_props["is_spread"], "false")
            self.assertEqual(img1_props["color_type"], "full_color")
            self.assertEqual(img1_props["bubble_count"], "7")
            self.assertEqual(img1_props["char_count"], "300")
            self.assertEqual(img1_props["japanese_metrics.kanji_count"], "10")
            self.assertEqual(img1_props["japanese_metrics.estimated_jlpt_level"], "Intermediate (N3)")
            self.assertEqual(img1_props["transcript"], long_transcript)
            self.assertGreater(len(img1_props["transcript"]), 100)
            self.assertEqual(img1_props["bubble_transcripts"], '["Dialogue bubble 1", "Dialogue bubble 2"]')

            # Verify image_data_list view renders formatted bubble_transcripts
            self.client.force_login(self.user)
            resp = self.client.get(reverse("viewer:image-data-list", args=[archive.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "bubble_transcripts (2):")
            self.assertContains(resp, "Dialogue bubble 1")
            self.assertContains(resp, "Dialogue bubble 2")
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_auto_run_on_new_archive_enqueues_task(self):
        settings.CRAWLER_SETTINGS.manga_extractor.enable = True
        settings.CRAWLER_SETTINGS.manga_extractor.run_on_new_archive = True

        archive, temp_zip_path = self._create_zip_archive(title="Auto Run Archive", num_pages=1)
        try:
            tasks = TaskQueue.objects.filter(task_type="contextual_extractor_worker")
            self.assertEqual(tasks.count(), 1)
            self.assertEqual(tasks.first().args, [archive.pk])
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_single_archive_extract_context_view(self):
        archive, temp_zip_path = self._create_zip_archive(title="Single Button Archive", num_pages=1)
        try:
            self.client.force_login(self.user)
            resp = self.client.post(reverse("viewer:archive-extract-context", args=[archive.pk]))
            self.assertEqual(resp.status_code, 302)

            tasks = TaskQueue.objects.filter(task_type="contextual_extractor_worker")
            self.assertEqual(tasks.count(), 1)
            self.assertEqual(tasks.first().args, [archive.pk])
            self.assertEqual(tasks.first().user, self.user)
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_manage_archives_batch_creates_one_task_per_archive(self):
        archive1, zip1 = self._create_zip_archive(title="Batch Archive 1", num_pages=1)
        archive2, zip2 = self._create_zip_archive(title="Batch Archive 2", num_pages=1)
        try:
            self.client.force_login(self.user)
            resp = self.client.post(
                reverse("viewer:manage-archives"),
                data={
                    f"sel-{archive1.pk}": str(archive1.pk),
                    f"sel-{archive2.pk}": str(archive2.pk),
                    "extract_contextual_info": "1",
                    "reason": "batch_context_test",
                },
            )
            self.assertEqual(resp.status_code, 200)

            tasks = list(TaskQueue.objects.filter(task_type="contextual_extractor_worker").order_by("id"))
            self.assertEqual(len(tasks), 2)
            self.assertEqual(tasks[0].args, [archive1.pk])
            self.assertEqual(tasks[1].args, [archive2.pk])
            self.assertEqual(tasks[0].reason, "batch_context_test")
            self.assertEqual(tasks[1].reason, "batch_context_test")
        finally:
            self._cleanup_archive(archive1, zip1)
            self._cleanup_archive(archive2, zip2)

    def test_profiles_configuration_and_serialization(self):
        cfg = {
            "manga_extractor": {
                "enable": True,
                "url": "http://manga-extractor:9000",
                "default_profile": "full",
                "background_profile": "stats",
                "profiles": {
                    "full": {
                        "name": "Full Extraction",
                        "description": "Deep full transcription",
                        "request_config": {
                            "preset": "full",
                            "ocr_max_dimension": 2000,
                            "ocr_space_threshold": 0.05,
                        },
                        "storage_config": {
                            "archive_fields": ["total_pages", "total_bubbles"],
                        },
                    },
                    "stats": {
                        "name": "Quick Stats",
                        "description": "Fast bubble stats",
                        "request_config": {
                            "preset": "stats",
                            "ocr_max_dimension": 1280,
                            "ocr_space_threshold": 0.04,
                        },
                        "storage_config": {
                            "archive_fields": ["total_pages"],
                            "save_image_results": False,
                        },
                    },
                },
            }
        }
        s = Settings(load_from_config=cfg)
        self.assertTrue(s.manga_extractor.enable)
        self.assertEqual(s.manga_extractor.default_profile, "full")
        self.assertEqual(s.manga_extractor.background_profile, "stats")
        self.assertEqual(
            s.manga_extractor.get_profile_choices(),
            {"full": "Full Extraction", "stats": "Quick Stats"},
        )

        full_p = s.manga_extractor.get_profile("full")
        self.assertEqual(full_p.request_config.preset, "full")
        self.assertEqual(full_p.request_config.ocr_max_dimension, 2000)
        self.assertEqual(full_p.request_config.ocr_space_threshold, 0.05)

        stats_p = s.manga_extractor.get_background_profile()
        self.assertEqual(stats_p.request_config.preset, "stats")
        self.assertEqual(stats_p.request_config.ocr_max_dimension, 1280)
        self.assertEqual(stats_p.request_config.ocr_space_threshold, 0.04)
        self.assertFalse(stats_p.storage_config.save_image_results)

        serialized = serialize_settings_options(s)
        base_s = Settings(load_from_disk=False)
        deserialized = deserialize_settings_options(base_s, serialized)
        self.assertTrue(deserialized.manga_extractor.enable)
        self.assertEqual(deserialized.manga_extractor.default_profile, "full")
        self.assertEqual(deserialized.manga_extractor.background_profile, "stats")
        deser_stats = deserialized.manga_extractor.get_profile("stats")
        self.assertEqual(deser_stats.request_config.ocr_space_threshold, 0.04)
        self.assertFalse(deser_stats.storage_config.save_image_results)

    @patch("viewer.services.archive_contextual_extractor.requests.get")
    @patch("viewer.services.archive_contextual_extractor.requests.post")
    def test_extract_with_selected_profile_and_ocr_space_threshold(self, mock_post, mock_get):
        archive, temp_zip_path = self._create_zip_archive(num_pages=1)
        try:
            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = {"job_id": "job-p1", "status": "processing"}
            mock_post.value = mock_post_resp
            mock_post.return_value = mock_post_resp

            mock_get_resp = MagicMock()
            mock_get_resp.json.return_value = {
                "job_id": "job-p1",
                "status": "completed",
                "summary": {"total_pages": 1, "total_bubbles": 4},
                "results": [],
            }
            mock_get.return_value = mock_get_resp

            me_cfg = MangaExtractorSettings()
            me_cfg.enable = True
            me_cfg.poll_interval = 0.01
            me_cfg.default_profile = "full"
            me_cfg.background_profile = "stats"
            me_cfg.profiles = {
                "full": MangaExtractorProfile(
                    name="Full",
                    request_config=MangaExtractorRequestConfig(preset="full", ocr_space_threshold=0.08),
                    storage_config=MangaExtractorStorageConfig(archive_fields=["total_pages", "total_bubbles"]),
                ),
                "stats": MangaExtractorProfile(
                    name="Stats",
                    request_config=MangaExtractorRequestConfig(
                        preset="stats",
                        ocr_max_dimension=1280,
                        ocr_space_threshold=0.02,
                    ),
                    storage_config=MangaExtractorStorageConfig(archive_fields=["total_pages"]),
                ),
            }

            # Extract specifying the "stats" profile
            success = ArchiveContextualExtractorService.extract_for_archive(
                archive, settings_override=me_cfg, profile="stats"
            )
            self.assertTrue(success)

            _, post_kwargs = mock_post.call_args
            self.assertEqual(post_kwargs["data"]["preset"], "stats")
            self.assertEqual(post_kwargs["data"]["ocr_max_dimension"], "1280")
            self.assertEqual(post_kwargs["data"]["ocr_space_threshold"], "0.02")

            archive_ct = ContentType.objects.get_for_model(Archive)
            props = dict(
                ItemProperties.objects.filter(
                    content_type=archive_ct, object_id=archive.pk, tag="manga-extractor"
                ).values_list("name", "value")
            )
            # 'stats' profile only saves 'total_pages', not 'total_bubbles'
            self.assertIn("total_pages", props)
            self.assertNotIn("total_bubbles", props)
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_extract_context_view_profile_selection_and_page_dropdown(self):
        archive, temp_zip_path = self._create_zip_archive(title="Profile View Archive", num_pages=1)
        try:
            settings.CRAWLER_SETTINGS.manga_extractor.enable = True
            settings.CRAWLER_SETTINGS.manga_extractor.default_profile = "full"
            settings.CRAWLER_SETTINGS.manga_extractor.background_profile = "quick"
            settings.CRAWLER_SETTINGS.manga_extractor.profiles = {
                "full": MangaExtractorProfile(name="Full OCR"),
                "quick": MangaExtractorProfile(name="Quick Stats"),
            }

            self.client.force_login(self.user)

            # Check archive details page context includes profiles and default profile
            resp = self.client.get(reverse("viewer:archive", args=[archive.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.context["manga_extractor_profiles"],
                {"full": "Full OCR", "quick": "Quick Stats"},
            )
            self.assertEqual(resp.context["manga_extractor_default_profile"], "full")
            self.assertContains(resp, "Quick Stats")
            self.assertContains(resp, "Full OCR")

            # Post without profile -> uses default_profile ("full")
            resp_def = self.client.post(reverse("viewer:archive-extract-context", args=[archive.pk]))
            self.assertEqual(resp_def.status_code, 302)
            task_def = TaskQueue.objects.filter(task_type="contextual_extractor_worker").first()
            self.assertIsNotNone(task_def)
            self.assertEqual(task_def.options.get("manga_extractor_profile"), "full")
            task_def.delete()

            # Post with specific profile -> uses that profile ("quick")
            resp_prof = self.client.post(
                reverse("viewer:archive-extract-context", args=[archive.pk]),
                data={"profile": "quick"},
            )
            self.assertEqual(resp_prof.status_code, 302)
            task_prof = TaskQueue.objects.filter(task_type="contextual_extractor_worker").first()
            self.assertIsNotNone(task_prof)
            self.assertEqual(task_prof.options.get("manga_extractor_profile"), "quick")
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_auto_run_on_new_archive_uses_background_profile(self):
        settings.CRAWLER_SETTINGS.manga_extractor.enable = True
        settings.CRAWLER_SETTINGS.manga_extractor.run_on_new_archive = True
        settings.CRAWLER_SETTINGS.manga_extractor.default_profile = "full"
        settings.CRAWLER_SETTINGS.manga_extractor.background_profile = "bg_stats"
        settings.CRAWLER_SETTINGS.manga_extractor.profiles = {
            "full": MangaExtractorProfile(name="Full"),
            "bg_stats": MangaExtractorProfile(name="Background Stats"),
        }

        archive, temp_zip_path = self._create_zip_archive(title="Auto Run Profile Archive", num_pages=1)
        try:
            task = TaskQueue.objects.filter(task_type="contextual_extractor_worker").first()
            self.assertIsNotNone(task)
            self.assertEqual(task.args, [archive.pk])
            self.assertEqual(task.options.get("manga_extractor_profile"), "bg_stats")
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_auth_token_configuration_and_headers(self):
        # 1. Direct configuration on MangaExtractorSettings
        cfg = MangaExtractorSettings(auth_token="token-abc-123")
        self.assertEqual(cfg.auth_token, "token-abc-123")
        self.assertEqual(cfg.get_auth_token(), "token-abc-123")
        self.assertEqual(cfg.get_headers(), {"Authorization": "Bearer token-abc-123"})

        # 2. update_from_dict with 'auth_token'
        cfg2 = MangaExtractorSettings()
        cfg2.update_from_dict({"auth_token": "updated-secret"})
        self.assertEqual(cfg2.auth_token, "updated-secret")
        self.assertEqual(cfg2.get_headers(), {"Authorization": "Bearer updated-secret"})

        # 3. update_from_dict with 'token' alias
        cfg3 = MangaExtractorSettings()
        cfg3.update_from_dict({"token": "token-via-alias"})
        self.assertEqual(cfg3.auth_token, "token-via-alias")
        self.assertEqual(cfg3.get_headers(), {"Authorization": "Bearer token-via-alias"})

        # 4. Fallback to MANGA_EXTRACTOR_AUTH_TOKEN environment variable
        empty_cfg = MangaExtractorSettings()
        self.assertEqual(empty_cfg.auth_token, "")
        with patch.dict(os.environ, {"MANGA_EXTRACTOR_AUTH_TOKEN": "env-token-456"}):
            self.assertEqual(empty_cfg.get_auth_token(), "env-token-456")
            self.assertEqual(empty_cfg.get_headers(), {"Authorization": "Bearer env-token-456"})

        # 5. Fallback to AUTH_TOKEN environment variable
        with patch.dict(os.environ, {"AUTH_TOKEN": "legacy-auth-token"}, clear=True):
            self.assertEqual(empty_cfg.get_auth_token(), "legacy-auth-token")
            self.assertEqual(empty_cfg.get_headers(), {"Authorization": "Bearer legacy-auth-token"})

        # 6. No token configured -> empty dict
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(empty_cfg.get_auth_token(), "")
            self.assertEqual(empty_cfg.get_headers(), {})

    def test_contextual_info_only_displayed_for_authenticated_users(self):
        archive, temp_zip_path = self._create_zip_archive(title="Contextual Visibility Archive", num_pages=1)
        try:
            archive_ct = ContentType.objects.get_for_model(Archive)
            ItemProperties.objects.create(
                content_type=archive_ct,
                object_id=archive.pk,
                tag="manga-extractor",
                name="total_pages",
                value="42",
            )
            image_ct = ContentType.objects.get_for_model(Image)
            img = archive.image_set.first()
            ItemProperties.objects.create(
                content_type=image_ct,
                object_id=img.pk,
                tag="manga-extractor",
                name="transcript",
                value="Secret manga transcript",
            )

            # 1. Unauthenticated requests should NOT display contextual info
            self.client.logout()

            # archive_old.html
            resp_old_anon = self.client.get(reverse("viewer:archive", args=[archive.pk]))
            self.assertEqual(resp_old_anon.status_code, 200)
            self.assertNotContains(resp_old_anon, "Contextual Metrics")
            self.assertNotContains(resp_old_anon, "total_pages")

            # archive_new.html
            resp_new_anon = self.client.get(f"{reverse('viewer:archive', args=[archive.pk])}?view_style=new")
            self.assertEqual(resp_new_anon.status_code, 200)
            self.assertNotContains(resp_new_anon, "Contextual Metrics")
            self.assertNotContains(resp_new_anon, "total_pages")

            # archive_image_data_list.html
            resp_img_anon = self.client.get(reverse("viewer:image-data-list", args=[archive.pk]))
            self.assertEqual(resp_img_anon.status_code, 200)
            self.assertNotContains(resp_img_anon, "<th>Contextual Info</th>")
            self.assertNotContains(resp_img_anon, "Secret manga transcript")

            # 2. Authenticated requests SHOULD display contextual info
            self.client.force_login(self.user)

            # archive_old.html
            resp_old_auth = self.client.get(reverse("viewer:archive", args=[archive.pk]))
            self.assertEqual(resp_old_auth.status_code, 200)
            self.assertContains(resp_old_auth, "Contextual Metrics")
            self.assertContains(resp_old_auth, "total_pages")
            self.assertContains(resp_old_auth, "42")

            # archive_new.html
            resp_new_auth = self.client.get(f"{reverse('viewer:archive', args=[archive.pk])}?view_style=new")
            self.assertEqual(resp_new_auth.status_code, 200)
            self.assertContains(resp_new_auth, "Contextual Metrics")
            self.assertContains(resp_new_auth, "total_pages")
            self.assertContains(resp_new_auth, "42")

            # archive_image_data_list.html
            resp_img_auth = self.client.get(reverse("viewer:image-data-list", args=[archive.pk]))
            self.assertEqual(resp_img_auth.status_code, 200)
            self.assertContains(resp_img_auth, "<th>Contextual Info</th>")
            self.assertContains(resp_img_auth, "Secret manga transcript")
        finally:
            self._cleanup_archive(archive, temp_zip_path)

    def test_setup_loads_auth_token_from_env(self):
        with patch.dict(os.environ, {"MANGA_EXTRACTOR_AUTH_TOKEN": "server-env-token"}):
            s = Settings(load_from_disk=False)
            s.load_from_environment()
            self.assertEqual(s.manga_extractor.auth_token, "server-env-token")
            self.assertEqual(s.manga_extractor.get_headers(), {"Authorization": "Bearer server-env-token"})

        with patch.dict(os.environ, {"AUTH_TOKEN": "fallback-env-token"}, clear=True):
            s2 = Settings(load_from_disk=False)
            s2.load_from_environment()
            self.assertEqual(s2.manga_extractor.auth_token, "fallback-env-token")
            self.assertEqual(s2.manga_extractor.get_headers(), {"Authorization": "Bearer fallback-env-token"})

    @patch("viewer.services.archive_contextual_extractor.requests.delete")
    @patch("viewer.services.archive_contextual_extractor.requests.get")
    @patch("viewer.services.archive_contextual_extractor.requests.post")
    def test_extract_for_archive_sends_authorization_headers(self, mock_post, mock_get, mock_delete):
        archive, temp_zip_path = self._create_zip_archive(num_pages=1)
        try:
            me_cfg = MangaExtractorSettings(
                enable=True,
                url="http://test-manga-extractor:8000",
                auth_token="bearer-secret-test-token",
            )

            mock_post_resp = MagicMock()
            mock_post_resp.json.return_value = {
                "job_id": "job-auth-999",
                "status": "completed",
                "summary": {"total_pages": 1},
                "pages": [{"page_index": 0, "panel_count": 4}],
            }
            mock_post.return_value = mock_post_resp

            success = ArchiveContextualExtractorService.extract_for_archive(archive, settings_override=me_cfg)
            self.assertTrue(success)

            # Verify post call passed Authorization header
            self.assertTrue(mock_post.called)
            post_kwargs = mock_post.call_args[1]
            self.assertIn("headers", post_kwargs)
            self.assertEqual(post_kwargs["headers"], {"Authorization": "Bearer bearer-secret-test-token"})

            # Verify polling call passed Authorization header
            mock_post_resp.json.return_value = {
                "job_id": "job-auth-poll",
                "status": "processing",
            }
            mock_get_resp = MagicMock()
            mock_get_resp.json.return_value = {
                "job_id": "job-auth-poll",
                "status": "completed",
                "summary": {"total_pages": 1},
                "pages": [{"page_index": 0}],
            }
            mock_get.return_value = mock_get_resp

            success2 = ArchiveContextualExtractorService.extract_for_archive(archive, settings_override=me_cfg)
            self.assertTrue(success2)

            self.assertTrue(mock_get.called)
            get_kwargs = mock_get.call_args[1]
            self.assertIn("headers", get_kwargs)
            self.assertEqual(get_kwargs["headers"], {"Authorization": "Bearer bearer-secret-test-token"})
        finally:
            self._cleanup_archive(archive, temp_zip_path)
