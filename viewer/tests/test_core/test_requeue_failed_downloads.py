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




class RequeueFailedDownloadTest(TestCase):
    def test_settings_requeue_defaults(self):
        settings = Settings(load_from_disk=False)
        self.assertTrue(settings.requeue_failed_downloads)
        self.assertEqual(settings.failed_download_downloader, "panda_archive")
        self.assertEqual(settings.requeue_downloader, "panda_archive")

    def test_settings_requeue_from_config(self):
        config = {
            "general": {
                "requeue_failed_downloads": False,
                "failed_download_downloader": "panda_hath",
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertFalse(settings.requeue_failed_downloads)
        self.assertEqual(settings.failed_download_downloader, "panda_hath")
        self.assertEqual(settings.requeue_downloader, "panda_hath")

    def test_settings_requeue_from_allowed_and_aliases(self):
        config = {
            "allowed": {
                "requeue_failed": "no",
                "requeue_downloader": "custom_dl",
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertFalse(settings.requeue_failed_downloads)
        self.assertEqual(settings.failed_download_downloader, "custom_dl")
        self.assertEqual(settings.requeue_downloader, "custom_dl")

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_requeue_disabled(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.side_effect = NotImplementedError("Corrupt zip")
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.requeue_failed_downloads = False
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="111", provider="panda")
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="11111111",
            source_type="panda",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("Check the file manually" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_requeue_custom_downloader(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.side_effect = NotImplementedError("Corrupt zip")
        mock_zipfile.return_value = mock_zip

        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive_then_hath",
            },
            "downloaders": {
                "panda_archive": 4,
                "panda_torrent": 3,
                "chaika_archive": 5,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="222", provider="panda")
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="22222222",
            source_type="panda",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"):
            with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("forcing download as panda_archive_then_hath to fix it" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_called_once()
            call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
            override_options = call_kwargs.get("override_options")
            self.assertIsNotNone(override_options)
            self.assertEqual(override_options.downloaders["panda_archive"], 1)
            self.assertEqual(override_options.downloaders["panda_hath"], 2)
            self.assertEqual(override_options.downloaders.get("chaika_archive"), -1)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_provider_mismatch_no_requeue(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.side_effect = NotImplementedError("Corrupt zip")
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.requeue_failed_downloads = True
        settings.failed_download_downloader = "panda_archive"
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery chaika", gid="333", provider="chaika")
        archive = Archive.objects.create(
            title="chaika archive",
            zipped="chaika_dl.zip",
            crc32="33333333",
            source_type="chaika",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("Check the file manually" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_filesize_mismatch_custom_downloader(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive_then_hath",
            },
            "downloaders": {
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="444", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="44444444",
            source_type="panda",
            gallery=gallery,
            filesize=1000,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 1000
            with self.assertLogs("core.downloaders.postdownload", level="INFO") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("downloading again from panda_archive_then_hath" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_called_once()
            call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
            override_options = call_kwargs.get("override_options")
            self.assertIsNotNone(override_options)
            self.assertEqual(override_options.downloaders["panda_archive"], 1)
            self.assertEqual(override_options.downloaders["panda_hath"], 2)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_sha1_mismatch_custom_downloader(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive_then_hath",
            },
            "downloaders": {
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        self.addCleanup(setattr, settings.provider_context, "get_parsers", settings.provider_context.get_parsers)
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="555", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="55555555",
            source_type="panda",
            match_type="torrent",
            gallery=gallery,
            filesize=2000,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("forcing download as panda_archive_then_hath to fix it" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_called_once()
            call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
            override_options = call_kwargs.get("override_options")
            self.assertIsNotNone(override_options)
            self.assertEqual(override_options.downloaders["panda_archive"], 1)
            self.assertEqual(override_options.downloaders["panda_hath"], 2)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_sha1_mismatch_requeue_disabled(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.requeue_failed_downloads = False
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        self.addCleanup(setattr, settings.provider_context, "get_parsers", settings.provider_context.get_parsers)
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="666", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="66666666",
            source_type="panda",
            match_type="torrent",
            gallery=gallery,
            filesize=2000,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("Check the file manually" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_chaika_sha1_mismatch_with_links(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        config = {
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive",
            },
            "downloaders": {"panda_archive": 1},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings
        chaika_settings = MagicMock()
        chaika_settings.url = "https://panda.chaika.moe/"
        settings.providers["chaika"] = chaika_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        self.addCleanup(setattr, settings.provider_context, "get_parsers", settings.provider_context.get_parsers)
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda chaika", gid="777", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda chaika archive",
            zipped="chaika_dl.zip",
            crc32="77777777",
            source_type="panda",
            match_type="chaika",
            gallery=gallery,
            filesize=2000,
            user=None,
        )
        DownloadEvent.objects.create(
            name=gallery.get_link(),
            method="chaika",
            download_id="chaika_dl.zip",
            archive=archive,
            gallery=gallery,
            download_url="https://panda.chaika.moe/archive/42/download/",
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                result = post_downloader.process_downloaded_archive(archive)

            self.assertFalse(result)
            self.assertTrue(any("forcing download as panda_archive to fix it" in msg for msg in cm.output))
            self.assertTrue(
                any(
                    "SHA1 mismatch detected for Chaika download" in msg
                    and "https://panda.chaika.moe/archive/42/" in msg
                    and gallery.get_link() in msg
                    for msg in cm.output
                )
            )
            mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_chaika_sha1_match(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_match"]
        self.addCleanup(setattr, settings.provider_context, "get_parsers", settings.provider_context.get_parsers)
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda chaika match", gid="888", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda chaika archive match",
            zipped="chaika_dl.zip",
            crc32="88888888",
            source_type="panda",
            match_type="chaika",
            gallery=gallery,
            filesize=2000,
            user=None,
        )
        Image.objects.create(archive=archive, position=1, archive_position=1, sha1="hash_match")

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="INFO") as cm:
                result = post_downloader.process_downloaded_archive(archive)

            self.assertTrue(result)
            self.assertTrue(any("validation passes" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.providers.panda.downloaders.request_by_provider")
    @patch("core.downloaders.postdownload.ZipFile")
    def test_panda_chaika_downloader_direct_sha1_check_mismatch(self, mock_zipfile, mock_req_by_prov):
        from core.base.setup_utilities import GeneralUtils
        from core.providers.panda.downloaders import PandaChaikaDownloader

        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.queue_archive_downloads = False
        settings.parallel_archive_downloaders = 0
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        panda_settings.queue_archive_downloads = False
        settings.providers["panda"] = panda_settings
        chaika_settings = MagicMock()
        chaika_settings.queue_archive_downloads = False
        chaika_settings.url = "https://panda.chaika.moe/"
        chaika_settings.archive_dl_folder = "archives/chaika"
        chaika_settings.wait_timer = 0.01
        chaika_settings.headers = {}
        chaika_settings.cookies = {}
        chaika_settings.proxy = None
        settings.providers["chaika"] = chaika_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        self.addCleanup(setattr, settings.provider_context, "get_parsers", settings.provider_context.get_parsers)
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_api_resp = MagicMock()
        mock_api_resp.status_code = 200
        mock_api_resp.json.return_value = {
            "gid": "999",
            "archives": [{"id": 99, "download": "/archive/99/download/"}],
        }
        mock_file_resp = MagicMock()
        mock_file_resp.status_code = 200
        mock_file_resp.headers = {"Content-Length": "100"}
        mock_file_resp.iter_content.return_value = [b"dummycontent"]

        def req_side_effect(name, calls, period):
            def req_call(url, req_dict, post=False):
                if "api/" in url:
                    return mock_api_resp
                return mock_file_resp
            return req_call

        mock_req_by_prov.side_effect = req_side_effect

        from django.contrib.auth import get_user_model
        get_user_model().objects.get_or_create(id=1, defaults={"username": "test_user"})

        mock_web_queue = MagicMock()
        settings.workers.web_queue = mock_web_queue

        Gallery.objects.create(gid="999", title="Test Gallery 999", provider="panda", filesize=100)
        gallery_data = GalleryData(gid="999", title="Test Gallery 999", provider="panda")
        gallery_data.link = "https://exhentai.org/g/999/abcdef/"
        gallery_data.filesize = 100
        gallery_data.filecount = 1

        downloader = PandaChaikaDownloader(settings, GeneralUtils(settings))

        with patch("builtins.open", MagicMock()), \
             patch("core.providers.panda.downloaders.available_filename", return_value="archives/chaika/test999.zip"), \
             patch("os.path.isfile", return_value=True), \
             patch.object(Archive, "save", lambda self, *a, **kw: self.simple_save(*a, **kw)), \
             patch("core.providers.panda.downloaders.get_zip_fileinfo_for_gallery", return_value=(100, 1)), \
             patch("core.providers.panda.downloaders.calc_crc32", return_value="99999999"), \
             patch.object(Archive, "recalc_fileinfo"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                downloader.init_download(gallery_data)

            self.assertEqual(downloader.return_code, 0)
            self.assertTrue(any("SHA1 mismatch detected for Chaika download" in msg for msg in cm.output))

    @patch("core.workers.archive_download_queue.request_by_provider")
    @patch("core.downloaders.postdownload.ZipFile")
    def test_archive_download_queue_chaika_sha1_check_mismatch(self, mock_zipfile, mock_req_prov):
        from core.workers.archive_download_queue import ArchiveDownloadQueue

        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings
        chaika_settings = MagicMock()
        chaika_settings.url = "https://panda.chaika.moe/"
        settings.providers["chaika"] = chaika_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        self.addCleanup(setattr, settings.provider_context, "get_parsers", settings.provider_context.get_parsers)
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        queue_worker = ArchiveDownloadQueue(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery q", gid="1010", provider="panda", filesize=50)
        archive = Archive.objects.create(
            title="archive q",
            zipped="q.zip",
            crc32="10101010",
            source_type="panda",
            match_type="chaika",
            gallery=gallery,
            filesize=50,
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="event_chaika",
            method="chaika",
            download_id="/fake/q.zip",
            provider="chaika",
            download_url="https://panda.chaika.moe/archive/55/download/",
            archive=archive,
            gallery=gallery,
            total_size=50,
            status=DownloadEvent.Status.IN_PROGRESS,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": "50"}
        mock_resp.iter_content.return_value = [b"dummy"]
        mock_req_prov.return_value = lambda url, d: mock_resp

        with patch("builtins.open", MagicMock()), \
             patch("os.makedirs"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=50), \
             patch("os.path.isfile", return_value=True), \
             patch("core.workers.archive_download_queue.zipfile.is_zipfile", return_value=True), \
             patch.object(Archive, "save", lambda self, *a, **kw: self.simple_save(*a, **kw)), \
             patch("os.remove"), \
             patch("os.replace"), \
             patch.object(Archive, "recalc_fileinfo"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                queue_worker._download_worker(event.id)

            event.refresh_from_db()
            self.assertTrue(event.failed)
            self.assertTrue(any("SHA1 mismatch detected for Chaika download" in msg for msg in cm.output))

    def test_gallery_dl_downloader_stderr_and_cleanup(self):
        from core.base.setup_utilities import GeneralUtils
        from core.providers.generic.downloaders import GenericGalleryDLDownloader

        settings = Settings(load_from_disk=True)
        gen_utils = GeneralUtils(settings)
        downloader = GenericGalleryDLDownloader(settings, gen_utils)
        downloader.gallery = GalleryData(
            gid="123",
            provider="generic",
            link="https://example.com/gallery/123",
            title="Sample Generic Gallery",
        )

        captured_dir = []

        def mock_subprocess_run(cmd, **kwargs):
            dest_idx = cmd.index("--dest") + 1
            dest_dir = cmd[dest_idx]
            captured_dir.append(dest_dir)
            mock_zip = os.path.join(dest_dir, "test_gallery.zip")
            with open(mock_zip, "w") as f:
                f.write("mock zip content")
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Downloaded 1 item",
                stderr="WARNING: non-fatal warning occurred",
            )

        with patch("shutil.which", return_value="/usr/bin/gallery-dl"), \
             patch("subprocess.run", side_effect=mock_subprocess_run), \
             patch("shutil.move"), \
             patch("core.downloaders.handlers.get_zip_fileinfo_for_gallery", return_value=(100, 5)), \
             patch("core.downloaders.handlers.calc_crc32", return_value="1234abcd"):
            downloader.start_download()

        # Non-fatal stderr warning should NOT cause download failure
        self.assertEqual(downloader.return_code, 1)
        self.assertEqual(downloader.fileDownloaded, 1)
        # Temporary directory must be cleaned up
        self.assertTrue(len(captured_dir) > 0)
        self.assertFalse(os.path.exists(captured_dir[0]))

        # Failure with non-zero returncode should clean up directory as well
        failed_dir = []

        def mock_subprocess_failed(cmd, **kwargs):
            dest_idx = cmd.index("--dest") + 1
            dest_dir = cmd[dest_idx]
            failed_dir.append(dest_dir)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="ERROR: Failed to download",
            )

        downloader.return_code = 0
        downloader.fileDownloaded = 0
        with patch("shutil.which", return_value="/usr/bin/gallery-dl"), \
             patch("subprocess.run", side_effect=mock_subprocess_failed):
            downloader.start_download()

        self.assertEqual(downloader.return_code, 0)
        self.assertEqual(downloader.fileDownloaded, 0)
        self.assertTrue(len(failed_dir) > 0)
        self.assertFalse(os.path.exists(failed_dir[0]))

    def test_autoupdate_provider_name_formatting(self):
        from core.workers.autoupdate import ProviderTimedAutoUpdater

        settings = Settings(load_from_disk=True)
        updater = ProviderTimedAutoUpdater(settings, provider_name="panda")
        start_date = django_tz.now()
        end_date = django_tz.now()
        msg = "No galleries posted from {} to {} need updating. Provider: {}".format(
            start_date, end_date, updater.provider_name
        )
        self.assertIn("Provider: panda", msg)
        self.assertNotIn("p, a, n, d, a", msg)

    def test_settings_load_elasticsearch_from_environment(self):
        settings = Settings(load_from_disk=False)
        self.assertFalse(settings.elasticsearch.enable)

        env = {
            "ELASTICSEARCH_ENABLE": "true",
            "ELASTICSEARCH_ENABLE_MATCH": "1",
            "ELASTICSEARCH_URL": "http://elasticsearch:9200/",
            "ELASTICSEARCH_INDEX_NAME": "custom_viewer",
            "ELASTICSEARCH_GALLERY_INDEX_NAME": "custom_gallery",
            "ELASTICSEARCH_TIMEOUT": "45",
        }
        with patch.dict(os.environ, env):
            settings.load_from_environment()

        self.assertTrue(settings.elasticsearch.enable)
        self.assertTrue(settings.elasticsearch.enable_match)
        self.assertEqual(settings.elasticsearch.url, "http://elasticsearch:9200/")
        self.assertEqual(settings.elasticsearch.index_name, "custom_viewer")
        self.assertEqual(settings.elasticsearch.gallery_index_name, "custom_gallery")
        self.assertEqual(settings.elasticsearch.timeout, 45)

    def test_settings_load_elasticsearch_empty_environment_ignored(self):
        settings = Settings(load_from_disk=False)
        settings.elasticsearch.enable = False
        settings.elasticsearch.url = "http://localhost:9200/"

        env = {
            "ELASTICSEARCH_ENABLE": "",
            "ELASTICSEARCH_URL": "",
        }
        with patch.dict(os.environ, env):
            settings.load_from_environment()

        self.assertFalse(settings.elasticsearch.enable)
        self.assertEqual(settings.elasticsearch.url, "http://localhost:9200/")


