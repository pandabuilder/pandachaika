import ftplib
import io
from unittest.mock import MagicMock, PropertyMock, call, patch

from django.test import SimpleTestCase

from core.base.utilities import plan_control_character_renames, remove_control_characters, replace_illegal_name
from core.downloaders.postdownload import FTPSource, SourceEntry
from core.downloaders.torrent.qbittorrent import qBittorrent
from core.downloaders.torrent.transmission import Transmission
from core.workers.download_progress import DownloadProgressChecker

# Real case: a torrent whose single file name has a newline before the extension.
BROKEN_NAME = "\\[Ohigetan\\] The Molester ♡ Study Guidance \\[English\\] \\[Striborg\\]\n.zip"
CLEAN_NAME = "\\[Ohigetan\\] The Molester ♡ Study Guidance \\[English\\] \\[Striborg\\].zip"
HASH = "a" * 40


class ControlCharacterNamesTest(SimpleTestCase):
    def test_remove_control_characters(self):
        self.assertEqual(remove_control_characters(BROKEN_NAME), CLEAN_NAME)
        self.assertEqual(remove_control_characters("Title\nSubtitle.zip"), "Title Subtitle.zip")
        self.assertEqual(remove_control_characters("Title \r\n\t Subtitle"), "Title Subtitle")
        self.assertEqual(remove_control_characters("\nTitle.zip\n"), "Title.zip")
        # Names without control characters are left as they are, spaces included.
        self.assertEqual(remove_control_characters(" Title .zip"), " Title .zip")

    def test_replace_illegal_name_removes_control_characters(self):
        self.assertEqual(replace_illegal_name("Title [Group]\n [931400]") + ".zip", "Title [Group] [931400].zip")
        self.assertEqual(replace_illegal_name(BROKEN_NAME), CLEAN_NAME.replace("\\", ""))

    def test_plan_renames_deepest_first(self):
        paths = ["root\n/sub\t/001\n.jpg", "root\n/sub\t/002.jpg", "root\n/003.jpg", "other/004.jpg"]
        self.assertEqual(
            plan_control_character_renames(paths),
            [("root\n/sub\t/001\n.jpg", "001.jpg"), ("root\n/sub\t", "sub"), ("root\n", "root")],
        )
        self.assertEqual(plan_control_character_renames(["root/001.jpg", "002.zip"]), [])
        # A name made only of control characters can't be cleaned into anything.
        self.assertEqual(plan_control_character_renames(["\n/001.jpg"]), [])


class TorrentClientCleanFileNamesTest(SimpleTestCase):
    def test_transmission_renames_paths_with_control_characters(self):
        client = Transmission()
        client.trans = MagicMock()
        client.trans.get_torrent.return_value = MagicMock(
            fields={"files": [{"name": "root\n/001.jpg"}, {"name": "root\n/002\n.jpg"}]}
        )

        renamed = client.clean_file_names(HASH)

        self.assertEqual(renamed, [("root\n/002\n.jpg", "002.jpg"), ("root\n", "root")])
        client.trans.get_torrent.assert_called_once_with(HASH, arguments=["id", "files"], timeout=25)
        self.assertEqual(
            client.trans.rename_torrent_path.call_args_list,
            [call(HASH, "root\n/002\n.jpg", "002.jpg", timeout=25), call(HASH, "root\n", "root", timeout=25)],
        )

    def test_transmission_legacy_numeric_id_and_failures(self):
        client = Transmission()
        client.trans = MagicMock()
        client.trans.get_torrent.return_value = MagicMock(fields={"files": [{"name": BROKEN_NAME}]})
        client.trans.rename_torrent_path.side_effect = Exception("Invalid argument")

        with self.assertLogs("core.base.types", level="WARNING"):
            self.assertEqual(client.clean_file_names("7"), [])
        client.trans.rename_torrent_path.assert_called_once_with(7, BROKEN_NAME, CLEAN_NAME, timeout=25)

        client.trans.get_torrent.side_effect = Exception("down")
        with self.assertLogs("core.downloaders.torrent.transmission", level="WARNING"):
            self.assertIsNone(client.clean_file_names(HASH))
        self.assertIsNone(client.clean_file_names("not an id"))

    def test_transmission_without_metadata_renames_nothing(self):
        client = Transmission()
        client.trans = MagicMock()
        client.trans.get_torrent.return_value = MagicMock(fields={})
        self.assertEqual(client.clean_file_names(HASH), [])
        client.trans.rename_torrent_path.assert_not_called()

    def test_qbittorrent_renames_files_and_folders(self):
        client = qBittorrent(address="http://localhost:8080")
        client.session = MagicMock()
        client.base_url = "http://localhost:8080"
        client.session.get.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value=[{"name": "root\n/001\n.jpg"}, {"name": "root\n/002.jpg"}])
        )
        client.session.post.return_value = MagicMock(status_code=200, text="")

        renamed = client.clean_file_names(HASH.upper())

        self.assertEqual(renamed, [("root\n/001\n.jpg", "001.jpg"), ("root\n", "root")])
        client.session.get.assert_called_once_with(
            "http://localhost:8080/api/v2/torrents/files", params={"hash": HASH}, timeout=25
        )
        self.assertEqual(
            client.session.post.call_args_list,
            [
                call(
                    "http://localhost:8080/api/v2/torrents/renameFile",
                    data={"hash": HASH, "oldPath": "root\n/001\n.jpg", "newPath": "root\n/001.jpg"},
                    timeout=25,
                ),
                call(
                    "http://localhost:8080/api/v2/torrents/renameFolder",
                    data={"hash": HASH, "oldPath": "root\n", "newPath": "root"},
                    timeout=25,
                ),
            ],
        )

    def test_qbittorrent_rename_conflict_is_reported(self):
        client = qBittorrent(address="http://localhost:8080")
        client.session = MagicMock()
        client.base_url = "http://localhost:8080"
        client.session.get.return_value = MagicMock(status_code=200, json=MagicMock(return_value=[{"name": BROKEN_NAME}]))
        client.session.post.return_value = MagicMock(status_code=409, text="Conflict")

        with self.assertLogs("core.base.types", level="WARNING") as logs:
            self.assertEqual(client.clean_file_names(HASH), [])
        self.assertIn("HTTP 409", logs.output[0])

    def test_unsupported_client_renames_nothing(self):
        from core.base.types import TorrentClient

        self.assertIsNone(TorrentClient().clean_file_names(HASH))


class FinishedTorrentCleanupTest(SimpleTestCase):
    def test_file_names_are_cleaned_before_the_transfer(self):
        checker = DownloadProgressChecker(MagicMock(max_torrent_lifespan=0))
        client = MagicMock()
        client.clean_file_names.return_value = [(BROKEN_NAME, CLEAN_NAME)]
        event = MagicMock(pk=1, completed=False, progress=100.0, archive_id=5, download_id=HASH)
        order = MagicMock()
        order.attach_mock(client.clean_file_names, "clean")

        with (
            patch("core.workers.download_progress.mark_completed_and_failed_for_indirect_downloads"),
            patch.object(checker, "_query_torrent_client", return_value=(client, {1: 100.0})),
            patch.object(checker, "check_torrent_progress"),
            patch.object(checker, "_start_transfer", side_effect=lambda *args: order.start(*args)),
            self.assertLogs("core.workers.download_progress", level="INFO") as logs,
        ):
            checker.check_torrent_downloads([event])

        self.assertEqual([x[0] for x in order.mock_calls], ["clean", "start"])
        client.clean_file_names.assert_called_once_with(HASH)
        self.assertIn("control characters", logs.output[0])

    def test_cleanup_errors_dont_block_the_transfer(self):
        checker = DownloadProgressChecker(MagicMock(max_torrent_lifespan=0))
        client = MagicMock()
        client.clean_file_names.side_effect = Exception("boom")
        event = MagicMock(pk=1, completed=False, progress=100.0, archive_id=5, download_id=HASH)

        with (
            patch("core.workers.download_progress.mark_completed_and_failed_for_indirect_downloads"),
            patch.object(checker, "_query_torrent_client", return_value=(client, {1: 100.0})),
            patch.object(checker, "check_torrent_progress"),
            patch.object(checker, "_start_transfer") as start,
            self.assertLogs("core.workers.download_progress", level="WARNING"),
        ):
            checker.check_torrent_downloads([event])

        start.assert_called_once()


def fake_ftp(raw_listing: str) -> ftplib.FTP:
    """FTP object whose MLSD returns ``raw_listing``, read line by line like ftplib.retrlines does."""
    ftp = ftplib.FTP()
    ftp.sendcmd = MagicMock(return_value="200")  # type: ignore[method-assign]

    def retrlines(cmd, callback):
        for line in io.StringIO(raw_listing, newline=""):
            if line.endswith("\r\n"):
                line = line[:-2]
            elif line.endswith("\n"):
                line = line[:-1]
            callback(line)
        return "226"

    ftp.retrlines = retrlines  # type: ignore[method-assign, assignment]
    return ftp


class FTPListingWithNewlinesTest(SimpleTestCase):
    def entries(self, raw_listing: str) -> list[SourceEntry]:
        with patch.object(FTPSource, "_connect"), patch.object(
            FTPSource, "ftps", new_callable=PropertyMock, return_value=fake_ftp(raw_listing)
        ):
            return FTPSource(MagicMock(), "torrent", "/downloads").entries("931400")

    def test_entry_split_by_a_newline_is_ignored_with_a_warning(self):
        listing = (
            "type=cdir; .\r\n"
            f"type=file;size=1000; {BROKEN_NAME}\r\n"
            "type=file;size=10; info.txt\r\n"
            "type=dir; folder\nwith two\nnewlines\r\n"
            "type=file;size=20; good.zip\r\n"
        )
        with self.assertLogs("core.downloaders.postdownload", level="WARNING") as logs:
            entries = self.entries(listing)

        self.assertEqual(entries, [SourceEntry("info.txt", False, 10), SourceEntry("good.zip", False, 20)])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("/downloads/931400", logs.output[0])
        self.assertIn("Striborg", logs.output[0])
        self.assertIn("'folder'", logs.output[0])

    def test_normal_listing_has_no_warning(self):
        with self.assertNoLogs("core.downloaders.postdownload", level="WARNING"):
            entries = self.entries("type=pdir; ..\r\ntype=dir; root\r\ntype=file;size=5; 001.jpg\r\n")
        self.assertEqual(entries, [SourceEntry("root", True, 0), SourceEntry("001.jpg", False, 5)])
