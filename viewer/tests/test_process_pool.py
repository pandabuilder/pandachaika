import io
import os
import tempfile
import zipfile
from unittest.mock import patch

from django.core.files import File
from django.test import TestCase
from PIL import Image

from core.base.process_pool import (
    calc_crc32_in_pool,
    calculate_phash_for_zip_members_in_pool,
    generate_thumbnail_in_pool,
    get_default_max_workers,
    get_process_pool,
    map_in_pool,
    recalc_archive_fileinfo_in_pool,
    resize_image_in_pool,
    run_in_pool,
    shutdown_process_pool,
    worker_calc_crc32,
    worker_calc_image_hash,
    worker_calc_phash_for_zip_member,
    worker_calc_zip_fileinfo_and_crc,
    worker_generate_thumbnail_from_zip,
    worker_resize_image,
)
from core.base.utilities import calc_crc32
from core.workers.archive_work import ArchiveWorker
from viewer.models import Archive, Image as GalleryImage, ItemProperties


class ProcessPoolUnitTests(TestCase):

    def tearDown(self) -> None:
        shutdown_process_pool()
        super().tearDown()

    def test_get_default_max_workers(self) -> None:
        workers = get_default_max_workers()
        self.assertGreaterEqual(workers, 1)

    def test_get_process_pool_singleton(self) -> None:
        pool1 = get_process_pool(max_workers=2)
        pool2 = get_process_pool()
        self.assertIs(pool1, pool2)

    def test_worker_calc_crc32(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"Hello Panda Gallery!")
            temp_path = f.name
        try:
            crc = worker_calc_crc32(temp_path)
            self.assertEqual(len(crc), 8)
            # Compare with standard calc_crc32
            self.assertEqual(crc, calc_crc32(temp_path))
        finally:
            os.unlink(temp_path)

    def test_worker_calc_crc32_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            worker_calc_crc32("/nonexistent/file/path.zip")

    def test_worker_calc_zip_fileinfo_and_crc(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_path = f.name
        try:
            with zipfile.ZipFile(zip_path, "w") as zf:
                img = Image.new("RGB", (200, 300), color="blue")
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                img_data = buf.getvalue()
                zf.writestr("001.jpg", img_data)
                zf.writestr("002.jpg", img_data)
                zf.writestr("info.txt", b"Extra text file")

            filesize, filecount, crc32, other_data = worker_calc_zip_fileinfo_and_crc(zip_path, get_extra_data=True)
            self.assertEqual(filecount, 2)
            self.assertEqual(filesize, len(img_data) * 2)
            self.assertEqual(len(crc32), 8)
            self.assertIsNotNone(other_data)
            self.assertEqual(len(other_data), 1)
            self.assertEqual(other_data[0].file_name, "info.txt")
        finally:
            os.unlink(zip_path)

    def test_worker_calc_phash_for_zip_member(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_path = f.name
        try:
            with zipfile.ZipFile(zip_path, "w") as zf:
                img = Image.new("RGB", (300, 300), color="green")
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                zf.writestr("page_01.jpg", buf.getvalue())

            member_name, phash = worker_calc_phash_for_zip_member((zip_path, "page_01.jpg", None))
            self.assertEqual(member_name, "page_01.jpg")
            self.assertIsNotNone(phash)
            self.assertIsInstance(phash, str)
            assert phash is not None
            self.assertGreater(len(phash), 0)
        finally:
            os.unlink(zip_path)

    def test_worker_generate_thumbnail_from_zip(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as out:
            out_path = out.name
        try:
            with zipfile.ZipFile(zip_path, "w") as zf:
                img = Image.new("RGB", (500, 800), color="orange")
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                zf.writestr("cover.jpg", buf.getvalue())

            size = worker_generate_thumbnail_from_zip(zip_path, "cover.jpg", None, out_path, width=250, height=362.5)
            self.assertIsNotNone(size)
            self.assertTrue(os.path.exists(out_path))
            with Image.open(out_path) as thumb_im:
                self.assertLessEqual(thumb_im.size[0], 250)
                self.assertLessEqual(thumb_im.size[1], 363)
        finally:
            os.unlink(zip_path)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_worker_resize_image(self) -> None:
        img = Image.new("RGB", (600, 400), color="teal")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as out:
            out_path = out.name
        try:
            size = worker_resize_image(raw_bytes, out_path, width=250, height=250)
            self.assertIsNotNone(size)
            self.assertTrue(os.path.exists(out_path))
            with Image.open(out_path) as thumb_im:
                self.assertLessEqual(thumb_im.size[0], 250)
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_worker_calc_image_hash(self) -> None:
        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        raw = buf.getvalue()

        phash = worker_calc_image_hash(raw, algorithm="phash")
        self.assertIsNotNone(phash)
        sha1 = worker_calc_image_hash(raw, algorithm="sha1")
        self.assertIsNotNone(sha1)

    def test_run_in_pool_fallback_on_error(self) -> None:
        def failing_task(x: int) -> int:
            return x * 2

        with patch("core.base.process_pool.get_process_pool", side_effect=RuntimeError("Pool unavailable")):
            result = run_in_pool(failing_task, 21, fallback_in_process=True)
            self.assertEqual(result, 42)

    def test_map_in_pool_fallback_on_error(self) -> None:
        def square(x: int) -> int:
            return x * x

        with patch("core.base.process_pool.get_process_pool", side_effect=RuntimeError("Pool unavailable")):
            results = map_in_pool(square, [1, 2, 3], fallback_in_process=True)
            self.assertEqual(results, [1, 4, 9])

    def test_high_level_helpers_in_pool(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as out:
            thumb_out = out.name
        try:
            with zipfile.ZipFile(zip_path, "w") as zf:
                img = Image.new("RGB", (400, 600), color="magenta")
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                zf.writestr("01.jpg", buf.getvalue())

            crc = calc_crc32_in_pool(zip_path)
            self.assertEqual(len(crc), 8)

            filesize, filecount, crc32, other = recalc_archive_fileinfo_in_pool(zip_path)
            self.assertEqual(filecount, 1)
            self.assertEqual(crc32, crc)

            thumb_size = generate_thumbnail_in_pool(zip_path, "01.jpg", None, thumb_out)
            self.assertIsNotNone(thumb_size)
            self.assertTrue(os.path.exists(thumb_out))

            phashes = calculate_phash_for_zip_members_in_pool([(zip_path, "01.jpg", None)])
            self.assertIn("01.jpg", phashes)
        finally:
            shutdown_process_pool()
            os.unlink(zip_path)
            if os.path.exists(thumb_out):
                os.unlink(thumb_out)


class ProcessPoolArchiveIntegrationTests(TestCase):

    def setUp(self) -> None:
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(username="pool_test_user", password="secret_password")
        self.temp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        with zipfile.ZipFile(self.temp_zip.name, "w") as zf:
            for i in range(1, 4):
                img = Image.new("RGB", (300, 400), color=(i * 50, i * 30, i * 20))
                buf = io.BytesIO()
                img.save(buf, format="JPEG")
                zf.writestr(f"page_{i:03d}.jpg", buf.getvalue())
            zf.writestr("readme.txt", b"Test archive metadata")

        self.archive = Archive.objects.create(
            title="Process Pool Test Archive",
            user=self.user,
        )
        with open(self.temp_zip.name, "rb") as f:
            self.archive.zipped.save("test_archive.zip", File(f), save=False)
        self.archive.save()

    def tearDown(self) -> None:
        shutdown_process_pool()
        if os.path.exists(self.temp_zip.name):
            os.unlink(self.temp_zip.name)
        if self.archive.zipped and os.path.exists(self.archive.zipped.path):
            os.unlink(self.archive.zipped.path)
        if self.archive.thumbnail and os.path.exists(self.archive.thumbnail.path):
            os.unlink(self.archive.thumbnail.path)
        super().tearDown()

    def test_archive_recalc_fileinfo_uses_pool(self) -> None:
        self.archive.crc32 = ""
        self.archive.filesize = 0
        self.archive.filecount = 0
        self.archive.recalc_fileinfo(use_pool=True)

        self.archive.refresh_from_db()
        self.assertNotEqual(self.archive.crc32, "")
        self.assertEqual(self.archive.filecount, 3)
        self.assertGreater(self.archive.filesize, 0)

    def test_archive_generate_thumbnails_uses_pool(self) -> None:
        success = self.archive.generate_thumbnails(use_pool=True)
        self.assertTrue(success)
        self.archive.refresh_from_db()
        self.assertTrue(bool(self.archive.thumbnail))
        self.assertIsNotNone(self.archive.thumbnail_width)
        self.assertIsNotNone(self.archive.thumbnail_height)
        self.assertTrue(os.path.exists(self.archive.thumbnail.path))

    def test_calculate_sha1_and_data_for_images_with_phash_pool(self) -> None:
        # Create Image records for the archive
        GalleryImage.objects.filter(archive=self.archive).delete()
        for i in range(1, 4):
            GalleryImage.objects.create(
                archive=self.archive,
                archive_position=i,
                image_name=f"page_{i:03d}.jpg",
            )

        from django.conf import settings
        settings.CRAWLER_SETTINGS.auto_phash_images = True
        try:
            success = self.archive.calculate_sha1_and_data_for_images(
                process_image_data=True, process_other_data=True, process_archive_statistics=True
            )
            self.assertTrue(success)

            # Check that SHA1 and pHash properties were created
            images = list(self.archive.image_set.order_by("archive_position"))
            for img in images:
                self.assertIsNotNone(img.sha1)
                self.assertNotEqual(img.sha1, "")

            props = ItemProperties.objects.filter(
                tag="hash-compare", name="phash", object_id__in=[img.pk for img in images]
            )
            self.assertEqual(props.count(), 3)
        finally:
            settings.CRAWLER_SETTINGS.auto_phash_images = False

    def test_archive_worker_batch_methods(self) -> None:
        worker = ArchiveWorker()
        worker.recalc_fileinfo_batch([self.archive])
        self.archive.refresh_from_db()
        self.assertNotEqual(self.archive.crc32, "")

        worker.regenerate_thumbs_batch([self.archive])
        self.archive.refresh_from_db()
        self.assertTrue(bool(self.archive.thumbnail))
