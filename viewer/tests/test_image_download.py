import io
import os
import tempfile
import zipfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files import File
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from PIL import Image as PImage

from viewer.models import Archive, Image
from viewer.services.archive_extraction import ArchiveExtractionService


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class ImageDownloadTests(TestCase):
    user: User
    admin_user: User
    auth_client: Client
    anon_client: Client

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="testuser", password="password123")
        cls.admin_user = User.objects.create_superuser(username="adminuser", password="password123")

    def setUp(self):
        self.auth_client = Client()
        self.auth_client.login(username="testuser", password="password123")
        self.anon_client = Client()

    def _create_archive_with_zip(
        self,
        images_info: list[tuple[str, tuple[int, int], str]] | None = None,
        public: bool = True,
    ) -> tuple[Archive, list[bytes], str]:
        """
        Create an Archive backed by a real zip file with specified image filenames and dimensions.
        Returns (archive, list_of_raw_image_bytes, zip_temp_path).
        """
        if images_info is None:
            images_info = [("ext_01.jpg", (80, 80), "JPEG"), ("ext_02.png", (100, 60), "PNG")]

        raw_bytes_list = []
        temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(temp_zip.name, "w") as zf:
            for fname, size, fmt in images_info:
                img = PImage.new("RGB", size, color=(120, 100, 80))
                buf = io.BytesIO()
                img.save(buf, format=fmt)
                raw_bytes = buf.getvalue()
                raw_bytes_list.append(raw_bytes)
                zf.writestr(fname, raw_bytes)

        archive = Archive.objects.create(title="Test Image Download Archive", user=self.user, public=public)
        with open(temp_zip.name, "rb") as f:
            archive.zipped.save("test_download.zip", File(f), save=True)

        archive.generate_image_set()
        archive.calculate_sha1_and_data_for_images()
        return archive, raw_bytes_list, temp_zip.name

    def tearDown(self):
        for archive in Archive.objects.all():
            for img_obj in archive.image_set.all():
                if img_obj.image and os.path.exists(img_obj.image.path):
                    try:
                        os.unlink(img_obj.image.path)
                    except OSError:
                        pass
                if img_obj.thumbnail and os.path.exists(img_obj.thumbnail.path):
                    try:
                        os.unlink(img_obj.thumbnail.path)
                    except OSError:
                        pass
            if archive.zipped and archive.zipped.name and os.path.exists(archive.zipped.path):
                try:
                    os.unlink(archive.zipped.path)
                except OSError:
                    pass

    def test_unauthenticated_user_redirected_to_login(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            image = archive.image_set.first()
            url = reverse("viewer:image-download", args=[image.pk])
            response = self.anon_client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response.headers.get("Location", ""))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_download_unextracted_streams_from_zip(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            image = archive.image_set.first()
            self.assertFalse(image.extracted)
            self.assertFalse(image.is_extracted_and_not_resized())

            url = reverse("viewer:image-download", args=[image.pk])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.streaming)
            self.assertIn('attachment; filename="ext_01.jpg"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            self.assertEqual(content, raw_bytes[0])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_download_extracted_not_resized_serves_from_filesystem(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            # Extract without resizing
            res = ArchiveExtractionService.extract(archive, resized=False)
            self.assertTrue(res)

            image = archive.image_set.first()
            self.assertTrue(image.extracted)
            self.assertTrue(image.is_extracted_and_not_resized())

            url = reverse("viewer:image-download", args=[image.pk])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment; filename="ext_01.jpg"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            self.assertEqual(content, raw_bytes[0])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_download_extracted_resized_streams_original_from_zip(self):
        # Create an archive with an image larger than horizontal max width (default 1500)
        archive, raw_bytes, tmp_path = self._create_archive_with_zip(
            images_info=[("large_01.jpg", (1800, 1200), "JPEG")]
        )
        try:
            # Extract with resizing
            res = ArchiveExtractionService.extract(archive, resized=True)
            self.assertTrue(res)

            image = archive.image_set.first()
            self.assertTrue(image.extracted)
            # Extracted image was downscaled to 1500 max width, so it is resized
            self.assertFalse(image.is_extracted_and_not_resized())

            url = reverse("viewer:image-download", args=[image.pk])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.streaming)
            self.assertIn('attachment; filename="large_01.jpg"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            # The downloaded content MUST be the original 1800x1200 image from the zip, NOT the resized version!
            self.assertEqual(content, raw_bytes[0])
            with PImage.open(io.BytesIO(content)) as im:
                self.assertEqual(im.size, (1800, 1200))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_archive_image_download_by_position(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            url = reverse("viewer:archive-image-download", args=[archive.pk, 2])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('attachment; filename="ext_02.png"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            self.assertEqual(content, raw_bytes[1])

            img_url = reverse("viewer:archive-img-download", args=[archive.pk, 2])
            img_response = self.auth_client.get(img_url)
            self.assertEqual(img_response.status_code, 200)
            img_content = b"".join(img_response.streaming_content)
            self.assertEqual(img_content, raw_bytes[1])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_download_x_accel_redirect_when_forwarded(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            ArchiveExtractionService.extract(archive, resized=False)
            image = archive.image_set.first()
            self.assertTrue(image.is_extracted_and_not_resized())

            url = reverse("viewer:image-download", args=[image.pk])
            response = self.auth_client.get(url, HTTP_X_FORWARDED_HOST="example.com")
            self.assertEqual(response.status_code, 200)
            self.assertIn("X-Accel-Redirect", response.headers)
            self.assertEqual(response.headers["X-Accel-Redirect"], f"/image/{image.image.name}")
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_data_list_page_logged_in_shows_download_option(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            url = reverse("viewer:image-data-list", args=[archive.pk])
            image = archive.image_set.first()
            download_url = reverse("viewer:image-download", args=[image.pk])
            view_url = reverse("viewer:image-view", args=[image.pk])

            # Authenticated user should see Download header, download link, and view link for File Name
            auth_response = self.auth_client.get(url)
            self.assertEqual(auth_response.status_code, 200)
            self.assertContains(auth_response, ">Download</th>")
            self.assertContains(auth_response, download_url)
            self.assertContains(auth_response, view_url)

            # Anonymous user should NOT see Download header, download link, or view link
            anon_response = self.anon_client.get(url)
            self.assertEqual(anon_response.status_code, 200)
            self.assertNotContains(anon_response, ">Download</th>")
            self.assertNotContains(anon_response, download_url)
            self.assertNotContains(anon_response, view_url)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_view_unauthenticated_user_redirected_to_login(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            image = archive.image_set.first()
            url = reverse("viewer:image-view", args=[image.pk])
            response = self.anon_client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/login/", response.headers.get("Location", ""))
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_view_unextracted_streams_inline(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            image = archive.image_set.first()
            self.assertFalse(image.extracted)
            self.assertFalse(image.is_extracted_and_not_resized())

            url = reverse("viewer:image-view", args=[image.pk])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.streaming)
            self.assertIn('inline; filename="ext_01.jpg"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            self.assertEqual(content, raw_bytes[0])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_view_extracted_not_resized_serves_inline(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            res = ArchiveExtractionService.extract(archive, resized=False)
            self.assertTrue(res)

            image = archive.image_set.first()
            self.assertTrue(image.extracted)
            self.assertTrue(image.is_extracted_and_not_resized())

            url = reverse("viewer:image-view", args=[image.pk])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('inline; filename="ext_01.jpg"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            self.assertEqual(content, raw_bytes[0])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_archive_image_view_by_position(self):
        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            url = reverse("viewer:archive-image-view", args=[archive.pk, 2])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn('inline; filename="ext_02.png"', response.headers.get("Content-Disposition", ""))
            content = b"".join(response.streaming_content)
            self.assertEqual(content, raw_bytes[1])

            img_url = reverse("viewer:archive-img-view", args=[archive.pk, 2])
            img_response = self.auth_client.get(img_url)
            self.assertEqual(img_response.status_code, 200)
            img_content = b"".join(img_response.streaming_content)
            self.assertEqual(img_content, raw_bytes[1])
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def test_image_download_nonexistent_and_deleted(self):
        url = reverse("viewer:image-download", args=[999999])
        response = self.auth_client.get(url)
        self.assertEqual(response.status_code, 404)

        archive, raw_bytes, tmp_path = self._create_archive_with_zip()
        try:
            archive.binned = True
            archive.save()
            image = archive.image_set.first()
            url = reverse("viewer:image-download", args=[image.pk])
            response = self.auth_client.get(url)
            self.assertEqual(response.status_code, 404)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
