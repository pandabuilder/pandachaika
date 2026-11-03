import io
import os
import tempfile
import zipfile
from unittest.mock import patch, MagicMock

from django.conf import settings
from django.core.files import File
from django.db import transaction
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from PIL import Image as PImage

from core.base.types import GalleryData
from viewer.models import Archive, ArchiveMatches, ArchiveStatistics, Gallery, Image, ItemProperties
from viewer.services import (
    ArchiveExtractionService,
    ArchiveHashingService,
    ArchiveThumbnailService,
    ArchiveCloningService,
    ArchiveIngestionService,
    GalleryIngestionService,
)


class DomainServicesTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="servicetester", password="password123")
        cls.archive = Archive.objects.create(title="Domain Test Archive", user=cls.user, extracted=False)

    def test_extraction_service_reduce_when_not_extracted(self):
        # Reducing a non-extracted archive should safely return False
        result = ArchiveExtractionService.reduce(self.archive)
        self.assertFalse(result)

    def test_extraction_service_extract_when_already_extracted(self):
        self.archive.extracted = True
        self.archive.save()
        result = ArchiveExtractionService.extract(self.archive)
        self.assertFalse(result)

    def test_extraction_service_validate_zip_missing_file(self):
        valid, msg = ArchiveExtractionService.validate_zip_archive("/nonexistent/file.zip")
        self.assertFalse(valid)
        self.assertIn("does not exist", msg)

    @patch("viewer.services.archive_thumbnail.os.path.exists", return_value=False)
    def test_thumbnail_service_generate_thumbnails_missing_file(self, mock_exists):
        result = ArchiveThumbnailService.generate_thumbnails(self.archive)
        self.assertFalse(result)

    def test_thumbnail_service_get_available_thumbnail_plus_size_default(self):
        url, h, w = ArchiveThumbnailService.get_available_thumbnail_plus_size(self.archive)
        self.assertIn("no_cover.png", url)
        self.assertEqual(h, 290)
        self.assertEqual(w, 196)

    @patch("viewer.services.archive_hashing.os.path.isfile", return_value=False)
    def test_hashing_service_calculate_crc32_missing_file(self, mock_isfile):
        crc = ArchiveHashingService.calculate_archive_crc32(self.archive)
        self.assertEqual(crc, "")

    def test_cloning_service_create_new_archive_sha1_mismatch(self):
        self.archive.filecount = 5
        self.archive.save()
        new_archive, msg = ArchiveCloningService.create_new_archive_ordered_by_sha1(
            self.archive, ["abc", "def"]
        )
        self.assertIsNone(new_archive)
        self.assertIn("different", msg)

    @patch("zipfile.ZipFile")
    def test_archive_save_does_not_open_zip_or_duplicate_es_updates(self, mock_zipfile):
        mock_es = MagicMock()
        with override_settings(ES_CLIENT=mock_es, ES_AUTOREFRESH=True, ES_ONLY_INDEX_PUBLIC=False):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                archive = Archive.objects.create(
                    title="Decoupled Save Archive",
                    zipped="fake_test.zip",
                    user=self.user,
                )
                # Saving again within the same transaction should deduplicate on_commit ES updates
                archive.title = "Updated Decoupled Save Archive"
                archive.save()

            mock_zipfile.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(mock_es.update.call_count, 1)

    def test_archive_ingestion_service_delegates_to_hashing_service(self):
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        archive = None
        try:
            with zipfile.ZipFile(temp_zip.name, "w") as zf:
                for i in range(1, 3):
                    img = PImage.new("RGB", (200, 300), color=(i * 40, i * 50, i * 60))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    zf.writestr(f"img_{i:03d}.jpg", buf.getvalue())

            archive = Archive.objects.create(title="Ingest Test Archive", user=self.user)
            with open(temp_zip.name, "rb") as f:
                archive.zipped.save("ingest_test.zip", File(f), save=False)

            mock_es = MagicMock()
            prev_auto_hash = settings.CRAWLER_SETTINGS.auto_hash_images
            settings.CRAWLER_SETTINGS.auto_hash_images = True
            try:
                with override_settings(ES_CLIENT=mock_es, ES_AUTOREFRESH=True, ES_ONLY_INDEX_PUBLIC=False):
                    with patch.object(
                        ArchiveHashingService,
                        "calculate_sha1_and_data_for_images",
                        wraps=ArchiveHashingService.calculate_sha1_and_data_for_images,
                    ) as mock_hash_service:
                        with self.captureOnCommitCallbacks(execute=True):
                            result = ArchiveIngestionService.ingest(archive)

                        self.assertTrue(result)
                        mock_hash_service.assert_called_once_with(
                            archive,
                            process_image_data=True,
                            process_other_data=False,
                            process_archive_statistics=True,
                            skip_zip_check=True,
                        )
                        self.assertEqual(mock_es.update.call_count, 1)
            finally:
                settings.CRAWLER_SETTINGS.auto_hash_images = prev_auto_hash

            archive.refresh_from_db()
            self.assertNotEqual(archive.crc32, "")
            self.assertEqual(archive.filecount, 2)
            self.assertEqual(archive.image_set.count(), 2)
            self.assertTrue(ArchiveStatistics.objects.filter(archive=archive).exists())
            for img_obj in archive.image_set.all():
                self.assertTrue(bool(img_obj.sha1))
        finally:
            if os.path.exists(temp_zip.name):
                os.unlink(temp_zip.name)
            if archive and archive.zipped and archive.zipped.name and os.path.exists(archive.zipped.path):
                os.unlink(archive.zipped.path)
            if archive and archive.thumbnail and archive.thumbnail.name and os.path.exists(archive.thumbnail.path):
                os.unlink(archive.thumbnail.path)

    @patch("viewer.services.gallery_ingestion.request_with_retries")
    def test_gallery_save_does_not_fetch_thumbnail_synchronously(self, mock_request):
        mock_es = MagicMock()
        with override_settings(ES_CLIENT=mock_es, ES_AUTOREFRESH_GALLERY=True, ES_ONLY_INDEX_PUBLIC=False):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                gallery = Gallery.objects.create(
                    gid="decoupled_gal_1",
                    provider="panda",
                    title="Decoupled Gallery",
                    thumbnail_url="https://example.com/thumb.jpg",
                )
                gallery.title = "Decoupled Gallery Updated"
                gallery.save()

            # Gallery.save() must not trigger HTTP thumbnail download
            mock_request.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(mock_es.update.call_count, 1)

    @patch("viewer.services.gallery_ingestion.request_with_retries")
    def test_gallery_manager_ingestion_schedules_thumbnail_fetch_on_commit(self, mock_request):
        img = PImage.new("RGB", (100, 140), color=(120, 80, 40))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [buf.getvalue()]
        mock_request.return_value = mock_resp

        gallery_data = GalleryData(
            gid="ingest_gal_2",
            provider="panda",
            title="Ingested Gallery",
            thumbnail_url="https://example.com/cover.jpg",
            tags=["artist:someone"],
        )
        gallery = None
        try:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                gallery = Gallery.objects.update_or_create_from_values(gallery_data)
                # Before commit callbacks run, HTTP request has not been executed
                mock_request.assert_not_called()

            for cb in callbacks:
                cb()

            mock_request.assert_called_once()
            gallery.refresh_from_db()
            self.assertTrue(bool(gallery.thumbnail))
        finally:
            if gallery and gallery.thumbnail and gallery.thumbnail.name and os.path.exists(gallery.thumbnail.path):
                os.unlink(gallery.thumbnail.path)

    def test_archive_generate_possible_matches_streams_titles(self):
        archive = Archive.objects.create(
            title="Non-Match Archive",
            match_type="non-match",
            user=self.user,
        )
        archive.zipped.name = "Awesome Adventure Manga.zip"
        archive.save()

        gal1 = Gallery.objects.create(
            gid="stream_gal_1",
            provider="panda",
            title="Awesome Adventure Manga",
            status=Gallery.StatusChoices.NORMAL,
        )
        Gallery.objects.create(
            gid="stream_gal_2",
            provider="panda",
            title="Unrelated Story",
            title_jpn="Awesome Adventure Manga JPN",
            status=Gallery.StatusChoices.NORMAL,
        )
        gal_deleted = Gallery.objects.create(
            gid="stream_gal_3",
            provider="panda",
            title="Awesome Adventure Manga Deleted",
            status=Gallery.StatusChoices.DELETED,
        )

        with patch.object(
            Gallery.objects, "eligible_for_use", wraps=Gallery.objects.eligible_for_use
        ) as mock_eligible:
            archive.generate_possible_matches(cutoff=0.4)
            mock_eligible.assert_called_once()

        matches = ArchiveMatches.objects.filter(archive=archive)
        matched_gallery_ids = set(matches.values_list("gallery_id", flat=True))
        self.assertIn(gal1.pk, matched_gallery_ids)
        self.assertNotIn(gal_deleted.pk, matched_gallery_ids)

    def test_archive_generate_possible_matches_values_list_iterator_called(self):
        archive = Archive.objects.create(
            title="Stream Test Archive",
            match_type="non-match",
            user=self.user,
        )
        archive.zipped.name = "Streaming Title Test.zip"
        archive.save()

        gal = Gallery.objects.create(
            gid="stream_gal_val_1",
            provider="panda",
            title="Streaming Title Test",
            status=Gallery.StatusChoices.NORMAL,
        )

        with patch("django.db.models.query.QuerySet.iterator", autospec=True) as mock_iterator:
            mock_iterator.return_value = iter([("Streaming Title Test", None, gal.pk)])
            archive.generate_possible_matches(cutoff=0.4)

            mock_iterator.assert_called_once()
            _, kwargs = mock_iterator.call_args
            self.assertEqual(kwargs.get("chunk_size"), 5000)

    def test_archive_generate_possible_matches_with_provider_filter(self):
        archive = Archive.objects.create(
            title="Provider Filter Archive",
            match_type="non-match",
            user=self.user,
        )
        archive.zipped.name = "Filtered Provider Title.zip"
        archive.save()

        gal_panda = Gallery.objects.create(
            gid="stream_gal_prov_1",
            provider="panda",
            title="Filtered Provider Title",
            status=Gallery.StatusChoices.NORMAL,
        )
        gal_fakku = Gallery.objects.create(
            gid="stream_gal_prov_2",
            provider="fakku",
            title="Filtered Provider Title",
            status=Gallery.StatusChoices.NORMAL,
        )

        with patch.object(
            Gallery.objects, "eligible_for_use", wraps=Gallery.objects.eligible_for_use
        ) as mock_eligible:
            archive.generate_possible_matches(cutoff=0.4, provider_filter="panda")
            mock_eligible.assert_called_once_with(provider__contains="panda")

        matches = ArchiveMatches.objects.filter(archive=archive)
        matched_gallery_ids = set(matches.values_list("gallery_id", flat=True))
        self.assertIn(gal_panda.pk, matched_gallery_ids)
        self.assertNotIn(gal_fakku.pk, matched_gallery_ids)

    def test_fix_image_positions_uses_bulk_update(self):
        archive = Archive.objects.create(title="Fix Positions Archive", user=self.user)
        img1 = Image.objects.create(archive=archive, archive_position=1, position=99)
        img2 = Image.objects.create(archive=archive, archive_position=2, position=88)
        img3 = Image.objects.create(archive=archive, archive_position=3, position=77)

        with (
            patch.object(Image.objects, "bulk_update", wraps=Image.objects.bulk_update) as mock_bulk_update,
            patch.object(Image, "save") as mock_save,
        ):
            archive.fix_image_positions()
            mock_bulk_update.assert_called_once()
            _, kwargs_or_args = mock_bulk_update.call_args
            # Verify fields argument is ['position']
            args = mock_bulk_update.call_args[0]
            self.assertEqual(args[1], ["position"])
            mock_save.assert_not_called()

        img1.refresh_from_db()
        img2.refresh_from_db()
        img3.refresh_from_db()
        self.assertEqual(img3.position, 1)
        self.assertEqual(img2.position, 2)
        self.assertEqual(img1.position, 3)

    def test_extraction_service_reduce_and_extract_delete_bulk_update(self):
        archive = Archive.objects.create(title="Reduce Bulk Archive", user=self.user, extracted=True)
        img1 = Image.objects.create(archive=archive, archive_position=1, position=1, extracted=True)
        img2 = Image.objects.create(archive=archive, archive_position=2, position=2, extracted=True)

        with (
            patch.object(Image.objects, "bulk_update", wraps=Image.objects.bulk_update) as mock_bulk_update,
            patch.object(Image, "save") as mock_save,
        ):
            res = ArchiveExtractionService.reduce(archive)
            self.assertTrue(res)
            mock_bulk_update.assert_called_once()
            mock_save.assert_not_called()

        img1.refresh_from_db()
        img2.refresh_from_db()
        self.assertFalse(img1.extracted)
        self.assertFalse(img2.extracted)

    def test_archive_ingestion_service_calls_testzip_once_per_ingestion(self):
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        archive = None
        try:
            with zipfile.ZipFile(temp_zip.name, "w") as zf:
                for i in range(1, 3):
                    img = PImage.new("RGB", (100, 100), color=(i * 10, i * 20, i * 30))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    zf.writestr(f"p_{i:02d}.jpg", buf.getvalue())

            archive = Archive.objects.create(title="Single Testzip Archive", user=self.user)
            with open(temp_zip.name, "rb") as f:
                archive.zipped.save("single_testzip.zip", File(f), save=False)

            prev_auto_hash = settings.CRAWLER_SETTINGS.auto_hash_images
            settings.CRAWLER_SETTINGS.auto_hash_images = True
            try:
                with patch("zipfile.ZipFile.testzip", autospec=True, return_value=None) as mock_testzip:
                    res = ArchiveIngestionService.ingest(archive)
                    self.assertTrue(res)
                    # testzip must be called exactly once during ingestion
                    self.assertEqual(mock_testzip.call_count, 1)
            finally:
                settings.CRAWLER_SETTINGS.auto_hash_images = prev_auto_hash
        finally:
            if os.path.exists(temp_zip.name):
                os.unlink(temp_zip.name)
            if archive and archive.zipped and archive.zipped.name and os.path.exists(archive.zipped.path):
                os.unlink(archive.zipped.path)
            if archive and archive.thumbnail and archive.thumbnail.name and os.path.exists(archive.thumbnail.path):
                os.unlink(archive.thumbnail.path)

    def test_archive_ingestion_service_skip_zip_check(self):
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        archive = None
        try:
            with zipfile.ZipFile(temp_zip.name, "w") as zf:
                for i in range(1, 3):
                    img = PImage.new("RGB", (100, 100), color=(i * 10, i * 20, i * 30))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    zf.writestr(f"p_{i:02d}.jpg", buf.getvalue())

            archive = Archive.objects.create(title="Skip Testzip Archive", user=self.user)
            with open(temp_zip.name, "rb") as f:
                archive.zipped.save("skip_testzip.zip", File(f), save=False)

            prev_auto_hash = settings.CRAWLER_SETTINGS.auto_hash_images
            settings.CRAWLER_SETTINGS.auto_hash_images = True
            try:
                with patch("zipfile.ZipFile.testzip", autospec=True, return_value=None) as mock_testzip:
                    res = ArchiveIngestionService.ingest(archive, skip_zip_check=True)
                    self.assertTrue(res)
                    # When skip_zip_check=True, testzip must not be called at all
                    mock_testzip.assert_not_called()
            finally:
                settings.CRAWLER_SETTINGS.auto_hash_images = prev_auto_hash
        finally:
            if os.path.exists(temp_zip.name):
                os.unlink(temp_zip.name)
            if archive and archive.zipped and archive.zipped.name and os.path.exists(archive.zipped.path):
                os.unlink(archive.zipped.path)
            if archive and archive.thumbnail and archive.thumbnail.name and os.path.exists(archive.thumbnail.path):
                os.unlink(archive.thumbnail.path)

    def test_generate_image_set_bulk_delete_and_create(self):
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        archive = None
        try:
            with zipfile.ZipFile(temp_zip.name, "w") as zf:
                for i in range(1, 4):
                    img = PImage.new("RGB", (80, 80), color=(i * 20, i * 20, i * 20))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    zf.writestr(f"img_{i:02d}.jpg", buf.getvalue())

            archive = Archive.objects.create(title="Gen Image Set Archive", user=self.user)
            with open(temp_zip.name, "rb") as f:
                archive.zipped.save("gen_image_set.zip", File(f), save=False)

            with patch.object(Image.objects, "bulk_create", wraps=Image.objects.bulk_create) as mock_bulk_create:
                res = archive.generate_image_set()
                self.assertTrue(res)
                mock_bulk_create.assert_called_once()
                self.assertEqual(archive.image_set.count(), 3)

            # Re-running with force=True should bulk delete and bulk create again
            with (
                patch.object(Image.objects, "bulk_create", wraps=Image.objects.bulk_create) as mock_bulk_create,
                patch.object(Image, "delete") as mock_img_delete,
            ):
                res = archive.generate_image_set(force=True)
                self.assertTrue(res)
                mock_bulk_create.assert_called_once()
                # per-row img.delete() must not be called
                mock_img_delete.assert_not_called()
                self.assertEqual(archive.image_set.count(), 3)
        finally:
            if os.path.exists(temp_zip.name):
                os.unlink(temp_zip.name)
            if archive and archive.zipped and archive.zipped.name and os.path.exists(archive.zipped.path):
                os.unlink(archive.zipped.path)

    def test_extraction_service_extract_bulk_update(self):
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        archive = None
        try:
            with zipfile.ZipFile(temp_zip.name, "w") as zf:
                for i in range(1, 3):
                    img = PImage.new("RGB", (60, 60), color=(i * 30, i * 30, i * 30))
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    zf.writestr(f"ext_{i:02d}.jpg", buf.getvalue())

            archive = Archive.objects.create(title="Extract Bulk Archive", user=self.user)
            with open(temp_zip.name, "rb") as f:
                archive.zipped.save("extract_bulk.zip", File(f), save=False)

            archive.generate_image_set()
            self.assertEqual(archive.image_set.count(), 2)

            with (
                patch.object(Image.objects, "bulk_update", wraps=Image.objects.bulk_update) as mock_bulk_update,
                patch.object(Image, "save") as mock_save,
            ):
                res = ArchiveExtractionService.extract(archive)
                self.assertTrue(res)
                mock_bulk_update.assert_called_once()
                mock_save.assert_not_called()

            archive.refresh_from_db()
            self.assertTrue(archive.extracted)
            for img_obj in archive.image_set.all():
                self.assertTrue(img_obj.extracted)
                self.assertTrue(bool(img_obj.image))
                self.assertTrue(bool(img_obj.thumbnail))
        finally:
            if os.path.exists(temp_zip.name):
                os.unlink(temp_zip.name)
            if archive and archive.zipped and archive.zipped.name and os.path.exists(archive.zipped.path):
                os.unlink(archive.zipped.path)
            if archive:
                for img_obj in archive.image_set.all():
                    if img_obj.image and os.path.exists(img_obj.image.path):
                        os.unlink(img_obj.image.path)
                    if img_obj.thumbnail and os.path.exists(img_obj.thumbnail.path):
                        os.unlink(img_obj.thumbnail.path)
