from unittest.mock import patch, MagicMock

from django.test import TestCase
from django.contrib.auth.models import User

from viewer.models import Archive
from viewer.services import (
    ArchiveExtractionService,
    ArchiveHashingService,
    ArchiveThumbnailService,
    ArchiveCloningService,
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
