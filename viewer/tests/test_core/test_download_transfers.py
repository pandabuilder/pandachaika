import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from django.test import TestCase, override_settings

from core.base.setup import Settings
from core.downloaders.postdownload import PostDownloader, TransferResult
from core.providers.panda.settings import OwnSettings as PandaSettings
from core.workers.download_progress import DownloadProgressChecker, TransferReason
from viewer.models import Archive, DownloadEvent, Gallery


def fake_process_success(archive):
    Archive.objects.filter(pk=archive.pk).update(crc32="ABCD1234")
    return True


class DownloadTransferTest(TestCase):
    """DownloadProgressChecker follows torrent/H@H downloads and transfers them when finished."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.media_dir = os.path.join(self.temp_dir, "media")
        self.torrent_dir = os.path.join(self.temp_dir, "torrents")
        self.hath_dir = os.path.join(self.temp_dir, "hath")
        for path in (self.media_dir, self.torrent_dir, self.hath_dir):
            os.makedirs(path)

        self.settings = Settings(load_from_disk=False)
        self.settings.MEDIA_ROOT = self.media_dir
        self.settings.download_handler = "local_copy"
        self.settings.torrent["download_dir"] = self.torrent_dir
        panda_settings = PandaSettings(self.settings, {})
        panda_settings.local_hath_folder = self.hath_dir
        self.settings.providers["panda"] = panda_settings

        self.web_queue = MagicMock()
        self.checker = DownloadProgressChecker(self.settings, web_queue=self.web_queue)
        # Run transfers inline instead of in the background pool.
        self.start_transfer = patch.object(
            self.checker,
            "_start_transfer",
            side_effect=lambda event, reason: self.checker.run_transfer(event.pk, reason),
        )

        media_override = override_settings(MEDIA_ROOT=self.media_dir)
        media_override.enable()
        self.addCleanup(media_override.disable)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_download(self, method, gid="123", filesize=0, download_id="abc"):
        gallery = Gallery.objects.create(gid=gid, provider="panda", title="Title", filesize=filesize)
        folder = "torrent" if method.startswith("torrent") else "hath"
        archive = Archive.objects.create(
            title="Title",
            zipped=f"galleries/{folder}/Title [{gid}].zip",
            crc32="",
            filesize=filesize,
            match_type=method,
            gallery=gallery,
            user=None,
        )
        event = DownloadEvent.objects.create(
            name=f"https://e-hentai.org/g/{gid}/token/",
            method=method,
            download_id=download_id,
            archive=archive,
            gallery=gallery,
        )
        return archive, event

    def write_torrent_file(self, gid="123", name="Title.zip"):
        os.makedirs(os.path.join(self.torrent_dir, gid), exist_ok=True)
        with ZipFile(os.path.join(self.torrent_dir, gid, name), "w") as zf:
            zf.writestr("001.jpg", b"image")

    def write_hath_folder(self, gid="123", data=b"0123456789"):
        folder = os.path.join(self.hath_dir, f"Title [{gid}]")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "001.jpg"), "wb") as f:
            f.write(data)
        with open(os.path.join(folder, "galleryinfo.txt"), "w") as f:
            f.write("info")

    def run_with_torrent_client(self, progress_result):
        client = MagicMock()
        client.connect.return_value = True
        client.get_download_progress.side_effect = lambda download_list: (
            progress_result(download_list) if callable(progress_result) else progress_result
        )
        with patch("core.workers.download_progress.get_torrent_client", return_value=client), self.start_transfer as start:
            self.checker.check_downloads()
        return start

    # Torrents

    def test_torrent_in_progress_only_updates_progress(self):
        _, event = self.create_download("torrent")
        start = self.run_with_torrent_client(lambda dl: [(dl[0][1], 40.0)])
        event.refresh_from_db()
        self.assertEqual(event.progress, 40.0)
        self.assertFalse(event.completed)
        start.assert_not_called()

    @patch.object(PostDownloader, "process_downloaded_archive", side_effect=fake_process_success)
    def test_torrent_finished_in_client_is_transferred(self, mock_process):
        archive, event = self.create_download("torrent")
        self.write_torrent_file()

        start = self.run_with_torrent_client(lambda dl: [(dl[0][1], 100.0)])

        start.assert_called_once()
        self.assertEqual(start.call_args.args[1], TransferReason.FINISHED)
        mock_process.assert_called_once()
        archive.refresh_from_db()
        self.assertTrue(os.path.isfile(archive.zipped.path))
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertFalse(event.failed)

    @patch.object(PostDownloader, "process_downloaded_archive", side_effect=fake_process_success)
    def test_torrent_removed_from_client_with_files_is_transferred(self, mock_process):
        _, event = self.create_download("torrent")
        self.write_torrent_file()

        start = self.run_with_torrent_client([])

        self.assertEqual(start.call_args.args[1], TransferReason.REMOVED_FROM_CLIENT)
        mock_process.assert_called_once()
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertFalse(event.failed)

    def test_torrent_removed_from_client_without_files_is_failed(self):
        archive, event = self.create_download("torrent")

        self.run_with_torrent_client([])

        event.refresh_from_db()
        self.assertTrue(event.failed)
        self.assertIn("removed from the client", event.error_message)
        archive.refresh_from_db()
        self.assertTrue(archive.binned)
        self.web_queue.enqueue_args_list.assert_not_called()

    def test_torrent_client_unreachable_keeps_download_in_progress(self):
        _, event = self.create_download("torrent")

        start = self.run_with_torrent_client(None)

        start.assert_not_called()
        event.refresh_from_db()
        self.assertFalse(event.completed)

    def test_transmission_ids_are_info_hashes_and_legacy_ids_still_work(self):
        from core.downloaders.torrent.transmission import Transmission

        client = Transmission()
        client.trans = MagicMock()
        added = MagicMock(id=7, hashString="a" * 40)
        added.name = "Title"
        added.get_files.return_value = []
        client.trans.add_torrent.return_value = added
        self.assertEqual(client.add_torrent(b"data"), (True, "a" * 40))

        by_hash = MagicMock(id=7, hashString="a" * 40, progress=50.0)
        legacy = MagicMock(id=3, hashString="b" * 40, progress=20.0)
        client.trans.get_torrents.return_value = [by_hash, legacy]
        results = client.get_download_progress([("A" * 40, "new"), ("3", "old")])
        self.assertEqual(sorted(results), [("new", 50.0), ("old", 20.0)])

        client.trans.get_torrents.side_effect = Exception("down")
        self.assertIsNone(client.get_download_progress([("3", "old")]))

    # H@H

    def test_hath_partial_folder_updates_progress(self):
        _, event = self.create_download("hath", filesize=20)
        self.write_hath_folder(data=b"0123456789")

        with self.start_transfer as start:
            self.checker.check_downloads()

        start.assert_not_called()
        event.refresh_from_db()
        self.assertEqual(event.progress, 50.0)
        self.assertFalse(event.completed)

    @patch.object(PostDownloader, "process_downloaded_archive", side_effect=fake_process_success)
    def test_hath_complete_folder_is_transferred(self, mock_process):
        archive, event = self.create_download("hath", filesize=10)
        self.write_hath_folder(data=b"0123456789")

        with self.start_transfer:
            self.checker.check_downloads()

        mock_process.assert_called_once()
        archive.refresh_from_db()
        with ZipFile(archive.zipped.path) as zf:
            self.assertEqual(zf.namelist(), ["001.jpg"])
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertEqual(event.progress, 100.0)

    def test_hath_folder_not_created_yet_keeps_waiting(self):
        _, event = self.create_download("hath", filesize=10)

        with self.start_transfer as start:
            self.checker.check_downloads()

        start.assert_not_called()
        event.refresh_from_db()
        self.assertFalse(event.completed)
        self.assertEqual(event.progress, 0.0)

    # Transfer outcomes

    @patch.object(PostDownloader, "process_downloaded_archive", return_value=False)
    def test_transfer_failing_validation_marks_download_failed(self, mock_process):
        _, event = self.create_download("hath", filesize=10)
        self.write_hath_folder(data=b"0123456789")

        self.checker.run_transfer(event.pk, TransferReason.FINISHED)

        event.refresh_from_db()
        self.assertTrue(event.failed)
        self.assertEqual(event.error_message, "Downloaded file failed validation")

    def test_transfer_error_is_retried_later(self):
        _, event = self.create_download("hath", filesize=10)

        with patch.object(PostDownloader, "transfer_archive", side_effect=ConnectionError("ftp down")):
            self.checker.run_transfer(event.pk, TransferReason.FINISHED)

        event.refresh_from_db()
        self.assertFalse(event.completed)
        self.assertTrue(self.checker._is_transferring(event))
        self.assertEqual(self.checker._transfers, {})

    def test_start_transfer_does_not_duplicate_running_transfer(self):
        _, event = self.create_download("hath", filesize=10)
        self.checker._executor = MagicMock()

        self.checker._start_transfer(event, TransferReason.FINISHED)
        self.checker._start_transfer(event, TransferReason.FINISHED)

        self.checker._executor.submit.assert_called_once()
        self.assertTrue(self.checker._is_transferring(event))

    def test_transfer_hath_reports_incomplete_folder(self):
        archive, _ = self.create_download("hath", filesize=100)
        self.write_hath_folder(data=b"0123456789")
        self.assertEqual(PostDownloader(self.settings).transfer_hath(archive), TransferResult.INCOMPLETE)
