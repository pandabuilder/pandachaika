import os
import ssl
import tempfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

import django.utils.timezone as django_tz
from django.test import TestCase

from core.base.setup import Settings
from core.downloaders.postdownload import PostDownloader
from core.workers.download_progress import DownloadProgressChecker
from viewer.models import Archive, DownloadEvent, Gallery




class PostDownloaderFTPTest(TestCase):
    def setUp(self):
        config = {
            "ftp_configs": {
                "hath": {
                    "address": "ftp.secret-server.local",
                    "user": "super_secret_user",
                    "passwd": "ultra_secret_password_123",
                    "no_certificate_check": False,
                },
                "custom_port": {
                    "address": "ftp.custom-port.local:2121",
                    "user": "another_user",
                    "passwd": "another_password",
                    "no_certificate_check": True,
                },
            },
            "download_ftp_hath": "hath",
            "download_ftp_torrent": "hath",
            "providers": {},
        }
        self.settings = Settings(load_from_config=config)
        self.downloader = PostDownloader(self.settings)

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_start_connection_ssleof_error(self, mock_ftp_cls):
        mock_ftp_cls.side_effect = ssl.SSLEOFError(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1082)"
        )
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
            result = self.downloader.start_connection("hath")
            self.assertFalse(result)
            self.assertIsNone(self.downloader.ftp_datas["hath"].ftps)

        log_output = "\n".join(cm.output)
        self.assertIn("ftp.secret-server.local:21", log_output)
        self.assertIn("SSLEOFError", log_output)
        self.assertIn("Remote server closed the connection unexpectedly", log_output)
        # Verify credentials are NOT logged
        self.assertNotIn("super_secret_user", log_output)
        self.assertNotIn("ultra_secret_password_123", log_output)

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_start_connection_connection_refused(self, mock_ftp_cls):
        mock_ftp_cls.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
            result = self.downloader.start_connection("hath")
            self.assertFalse(result)
            self.assertIsNone(self.downloader.ftp_datas["hath"].ftps)

        log_output = "\n".join(cm.output)
        self.assertIn("ftp.secret-server.local:21", log_output)
        self.assertIn("ConnectionRefusedError", log_output)
        self.assertIn("Connection was refused", log_output)
        self.assertNotIn("super_secret_user", log_output)
        self.assertNotIn("ultra_secret_password_123", log_output)

    def test_start_connection_missing_config(self):
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
            result = self.downloader.start_connection("non_existent")
            self.assertFalse(result)
            self.assertIsNone(self.downloader.ftp_datas["non_existent"].ftps)
        log_output = "\n".join(cm.output)
        self.assertIn("non_existent", log_output)

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_start_connection_success(self, mock_ftp_cls):
        mock_instance = MagicMock()
        mock_ftp_cls.return_value = mock_instance
        result = self.downloader.start_connection("hath")
        self.assertTrue(result)
        self.assertEqual(self.downloader.ftp_datas["hath"].ftps, mock_instance)
        mock_instance.prot_p.assert_called_once()

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_ftp_source_raises_when_connection_fails(self, mock_ftp_cls):
        from core.downloaders.postdownload import FTPSource

        mock_ftp_cls.side_effect = ConnectionRefusedError("Connection refused")
        with self.assertLogs("core.downloaders.postdownload", level="ERROR"):
            with self.assertRaises(ConnectionError):
                FTPSource(self.downloader, "hath", "/remote")

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_ftp_source_reconnects_on_transfer_error(self, mock_ftp_cls):
        from core.downloaders.postdownload import FTPSource

        source = FTPSource(self.downloader, "hath", "/remote")
        dest = os.path.join(tempfile.mkdtemp(), "001.jpg")
        with patch.object(
            self.downloader, "write_file_update_progress", side_effect=[ConnectionResetError("reset"), "226"]
        ) as mock_write:
            with self.assertLogs("core.downloaders.postdownload", level="WARNING"):
                source.fetch("Title [1]/001.jpg", dest, 10)
        self.assertEqual(mock_write.call_count, 2)
        self.assertEqual(mock_ftp_cls.call_count, 2)

    def test_torrent_lifespan_settings_defaults(self):
        settings = Settings(load_from_disk=False)
        self.assertEqual(settings.max_torrent_lifespan, 0)
        self.assertEqual(settings.max_torrent_lifespan_type, "days")
        self.assertTrue(settings.download_progress_checker_startup)

    def test_torrent_lifespan_settings_from_config(self):
        config = {
            "general": {
                "max_torrent_lifespan": 12,
                "max_torrent_lifespan_type": "hours",
            }
        }
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.max_torrent_lifespan, 12)
        self.assertEqual(settings.max_torrent_lifespan_type, "hours")
        self.assertTrue(settings.download_progress_checker_startup)

    def test_download_progress_checker_startup_disabled_in_config(self):
        config = {
            "general": {
                "download_progress_checker_startup": False,
            }
        }
        settings = Settings(load_from_config=config)
        self.assertFalse(settings.download_progress_checker_startup)

    def test_check_torrent_progress_finish_on_100_percent(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 60
        settings.max_torrent_lifespan_type = "seconds"
        checker = DownloadProgressChecker(settings)

        event = DownloadEvent.objects.create(
            name="test_torrent_100",
            method="torrent",
            download_id="dl_100",
            progress=50.0,
        )
        checker.check_torrent_progress(event, 100.0)
        event.refresh_from_db()
        self.assertEqual(event.progress, 100.0)
        self.assertTrue(event.completed)
        self.assertFalse(event.failed)

    def test_check_torrent_progress_finish_when_archive_crc32_set(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 10
        settings.max_torrent_lifespan_type = "seconds"
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(
            title="completed archive",
            crc32="ABCD1234",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_crc32",
            method="torrent",
            download_id="dl_crc32",
            archive=archive,
            progress=30.0,
        )
        # Simulate create_date in the past (exceeding lifespan)
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 40.0)
        event.refresh_from_db()
        # Because archive.crc32 is set, it should finish download, NOT purge!
        self.assertTrue(event.completed)
        self.assertFalse(event.failed)
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())
        self.assertEqual(event.archive_id, archive.pk)

    def test_check_torrent_progress_default_disabled_does_not_purge(self):
        settings = Settings(load_from_disk=False)
        self.assertEqual(settings.max_torrent_lifespan, 0)
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(
            title="pending archive",
            crc32="",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_default",
            method="torrent",
            download_id="dl_default",
            archive=archive,
            progress=20.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(days=30)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 25.0)
        event.refresh_from_db()
        self.assertFalse(event.completed)
        self.assertFalse(event.failed)
        self.assertEqual(event.progress, 25.0)
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())

    def test_check_torrent_progress_not_exceeded_does_not_purge(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 10
        settings.max_torrent_lifespan_type = "hours"
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(
            title="active archive",
            crc32="",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_active",
            method="torrent",
            download_id="dl_active",
            archive=archive,
            progress=15.0,
        )
        # Only 5 minutes passed, limit is 10 hours
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(minutes=5)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 20.0)
        event.refresh_from_db()
        self.assertFalse(event.completed)
        self.assertFalse(event.failed)
        self.assertEqual(event.progress, 20.0)
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())

    def test_check_torrent_progress_lifespan_exceeded_purges_and_requeues(self):
        config = {
            "general": {
                "max_torrent_lifespan": 60,
                "max_torrent_lifespan_type": "seconds",
            },
            "downloaders": {
                "panda_torrent": 1,
                "panda_torrent_api": 2,
                "panda_archive": 3,
            },
        }
        settings = Settings(load_from_config=config)
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery for dead torrent", gid="999", provider="panda")
        archive = Archive.objects.create(
            title="dead archive",
            crc32="",
            gallery=gallery,
            reason="backup_reason",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_dead",
            method="torrent",
            download_id="dl_dead",
            archive=archive,
            gallery=gallery,
            progress=10.0,
        )
        # Exceeded: 120s passed, limit is 60s
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=120)
        )
        event.refresh_from_db()

        with patch.object(archive, "delete_all_files") as mock_delete_files:
            checker.check_torrent_progress(event, 10.0)

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertIsNone(event.archive)
        # Archive should have been deleted from DB
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())

        # Check requeue call
        mock_web_queue.enqueue_args_list.assert_called_once()
        call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
        self.assertEqual(call_args[0], (gallery.get_link(),))
        override_options = call_kwargs["override_options"]
        self.assertTrue(override_options.redownload)
        self.assertTrue(override_options.replace_metadata)
        self.assertTrue(override_options.retry_failed)
        self.assertEqual(override_options.downloaders.get("panda_torrent"), -1)
        self.assertEqual(override_options.downloaders.get("panda_torrent_api"), -1)
        self.assertEqual(override_options.archive_reason, "backup_reason")

    def test_check_torrent_progress_lifespan_exceeded_without_archive(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 30
        settings.max_torrent_lifespan_type = "sec"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery without archive", gid="888", provider="panda")
        event = DownloadEvent.objects.create(
            name="test_torrent_no_archive",
            method="torrent",
            download_id="dl_no_archive",
            archive=None,
            gallery=gallery,
            progress=5.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=60)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 5.0)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        mock_web_queue.enqueue_args_list.assert_called_once()

    def test_check_torrent_progress_lifespan_units(self):
        settings = Settings(load_from_disk=False)
        checker = DownloadProgressChecker(settings)

        # 2 minutes = 120 seconds
        settings.max_torrent_lifespan = 2
        settings.max_torrent_lifespan_type = "minutes"
        event = DownloadEvent.objects.create(
            name="test_torrent_minutes",
            method="torrent",
            download_id="dl_min",
            progress=10.0,
        )
        # 90 seconds passed -> not exceeded
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=90)
        )
        event.refresh_from_db()
        checker.check_torrent_progress(event, 10.0)
        event.refresh_from_db()
        self.assertFalse(event.completed)

        # 150 seconds passed -> exceeded
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=150)
        )
        event.refresh_from_db()
        checker.check_torrent_progress(event, 10.0)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)

    @patch("core.workers.download_progress.close_old_connections")
    @patch("core.workers.download_progress.get_torrent_client")
    def test_job_integration_with_torrent_lifespan(self, mock_get_torrent_client, mock_close_old):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 50
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery job integration", gid="777", provider="panda")
        archive = Archive.objects.create(
            title="job archive",
            crc32="",
            gallery=gallery,
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_job_torrent",
            method="torrent",
            download_id="dl_job",
            archive=archive,
            gallery=gallery,
            progress=20.0,
        )
        # Exceeded: 100s passed, limit is 50s
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        mock_client = MagicMock()
        mock_client.connect.return_value = True
        mock_client.get_download_progress.return_value = [(event, 25.0)]
        mock_get_torrent_client.return_value = mock_client

        # Run job for exactly 1 cycle
        checker.force_run_once = True
        with patch.object(checker.stop, "wait", side_effect=[False, True]), \
             patch.object(checker.stop, "is_set", side_effect=[False, True]):
            checker.job()

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())
        mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("core.workers.download_progress.close_old_connections")
    @patch("core.workers.download_progress.get_torrent_client")
    def test_job_integration_torrent_not_in_client_still_expires_and_requeues(
        self, mock_get_torrent_client, mock_close_old
    ):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 50
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery missing in client", gid="666", provider="panda")
        archive = Archive.objects.create(
            title="job archive missing", crc32="", gallery=gallery, user=None, match_type="torrent"
        )
        event = DownloadEvent.objects.create(
            name="test_missing_torrent",
            method="torrent",
            download_id="dl_missing",
            archive=archive,
            gallery=gallery,
            progress=10.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        mock_client = MagicMock()
        mock_client.connect.return_value = True
        # Torrent client returns nothing (torrent removed or ID not found)
        mock_client.get_download_progress.return_value = []
        mock_get_torrent_client.return_value = mock_client
        settings.torrent["download_dir"] = tempfile.mkdtemp()

        checker.force_run_once = True
        # Nothing in the download folder, and the lifespan expired: purged and requeued.
        with patch.object(checker.stop, "wait", side_effect=[False, True]), \
             patch.object(checker.stop, "is_set", side_effect=[False, True]), \
             patch.object(
                 checker, "_start_transfer", side_effect=lambda ev, reason: checker.run_transfer(ev.pk, reason)
             ):
            checker.job()

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())
        mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("core.workers.download_progress.close_old_connections")
    @patch("core.workers.download_progress.get_torrent_client")
    def test_job_integration_client_connect_failure_still_expires_and_requeues(
        self, mock_get_torrent_client, mock_close_old
    ):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 50
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery client failure", gid="555", provider="panda")
        archive = Archive.objects.create(title="job archive client failure", crc32="", gallery=gallery, user=None)
        event = DownloadEvent.objects.create(
            name="test_connect_failure_torrent",
            method="torrent",
            download_id="dl_failure",
            archive=archive,
            gallery=gallery,
            progress=10.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        mock_client = MagicMock()
        mock_client.connect.return_value = False
        mock_get_torrent_client.return_value = mock_client

        checker.force_run_once = True
        with patch.object(checker.stop, "wait", side_effect=[False, True]), \
             patch.object(checker.stop, "is_set", side_effect=[False, True]):
            checker.job()

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())
        mock_web_queue.enqueue_args_list.assert_called_once()

    def test_check_torrent_progress_fallback_to_event_name_url(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 30
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        event = DownloadEvent.objects.create(
            name="https://e-hentai.org/g/12345/67890abcdef/",
            method="torrent",
            download_id="dl_no_gallery",
            archive=None,
            gallery=None,
            progress=5.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=60)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 5.0)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        mock_web_queue.enqueue_args_list.assert_called_once()
        call_args, _ = mock_web_queue.enqueue_args_list.call_args
        self.assertEqual(call_args[0], ("https://e-hentai.org/g/12345/67890abcdef/",))

    def test_check_torrent_progress_disables_custom_downloader_wrapping_torrent(self):
        config = {
            "general": {
                "max_torrent_lifespan": 30,
                "max_torrent_lifespan_type": "seconds",
            },
            "downloaders": {
                "custom_with_torrent": 1,
                "panda_torrent": 2,
                "panda_archive": 3,
            },
        }
        settings = Settings(load_from_config=config)
        settings.custom_downloaders = {
            "custom_with_torrent": {"panda_torrent": 1, "panda_archive": 2}
        }
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery custom dl", gid="444", provider="panda")
        event = DownloadEvent.objects.create(
            name="test_custom_dl_event",
            method="torrent",
            download_id="dl_custom",
            archive=None,
            gallery=gallery,
            progress=5.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=60)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 5.0)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        mock_web_queue.enqueue_args_list.assert_called_once()
        _, call_kwargs = mock_web_queue.enqueue_args_list.call_args
        override = call_kwargs["override_options"]
        self.assertEqual(override.downloaders.get("panda_torrent"), -1)
        self.assertEqual(override.downloaders.get("custom_with_torrent"), -1)
        self.assertEqual(override.downloaders.get("panda_archive"), 3)

    def test_transmission_get_download_progress_handles_invalid_ids(self):
        from core.downloaders.torrent.transmission import Transmission
        client = Transmission()
        client.trans = MagicMock()
        # transmission mock returns progress for id 10
        mock_torr = MagicMock()
        mock_torr.id = 10
        mock_torr.progress = 75.0
        client.trans.get_torrents.return_value = [mock_torr]

        dummy_key = MagicMock()
        results = client.get_download_progress([
            ("", dummy_key),
            ("not_an_int", dummy_key),
            ("10", dummy_key),
        ])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], (dummy_key, 75.0))
        # Ensure get_torrents was called only with valid integer IDs: [10]
        client.trans.get_torrents.assert_called_once_with([10], timeout=25)

    def test_qbittorrent_initialization_and_url_building(self):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        client = qBittorrent(address="localhost", port=8080)
        self.assertEqual(client.name, "qbittorrent")
        self.assertEqual(str(client), "qbittorrent")
        self.assertFalse(client.convert_to_base64)
        self.assertFalse(client.send_url)
        self.assertEqual(client._build_base_url(), "http://localhost:8080")

        client2 = qBittorrent(address="http://127.0.0.1:9090/", port=8080)
        self.assertEqual(client2._build_base_url(), "http://127.0.0.1:9090")

        client3 = qBittorrent(address="https://seedbox.local/qbt/", port=8080)
        self.assertEqual(client3._build_base_url(), "https://seedbox.local:8080/qbt")

    @patch("requests.Session")
    def test_qbittorrent_connect_with_auth_success(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        mock_session.post.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080", user="admin", password="password123")
        self.assertTrue(client.connect())
        mock_session.post.assert_called_once_with(
            "http://localhost:8080/api/v2/auth/login",
            data={"username": "admin", "password": "password123"},
            timeout=25,
        )

    @patch("requests.Session")
    def test_qbittorrent_connect_with_auth_failure(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Fails."
        mock_session.post.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080", user="admin", password="wrongpassword")
        self.assertFalse(client.connect())
        self.assertIn("Authentication failed", client.error)

    @patch("requests.Session")
    def test_qbittorrent_connect_without_auth(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session.get.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080", user="", password="")
        self.assertTrue(client.connect())
        mock_session.get.assert_called_once_with("http://localhost:8080/api/v2/app/version", timeout=25)

    @patch("requests.Session")
    def test_qbittorrent_add_torrent_success(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent
        from core.libs.bencoding import Encoder

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        mock_session.post.return_value = mock_resp

        torrent_payload = {
            b"announce": b"http://tracker.example.com",
            b"info": {
                b"name": b"sample_archive.zip",
                b"length": 1048576,
                b"piece length": 32768,
                b"pieces": b"12345678901234567890",
            },
        }
        torrent_bytes = bytes(Encoder(torrent_payload).encode())

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        success, info_hash = client.add_torrent(torrent_bytes, download_dir="/downloads/100")
        self.assertTrue(success)
        self.assertIsNotNone(info_hash)
        assert info_hash is not None
        self.assertEqual(len(info_hash), 40)
        self.assertEqual(client.expected_torrent_name, "sample_archive")
        self.assertEqual(client.expected_torrent_extension, ".zip")
        self.assertEqual(client.total_size, 1048576)

        mock_session.post.assert_called_once()
        call_args, call_kwargs = mock_session.post.call_args
        self.assertEqual(call_args[0], "http://localhost:8080/api/v2/torrents/add")
        self.assertEqual(call_kwargs["data"], {"savepath": "/downloads/100"})
        self.assertIn("torrents", call_kwargs["files"])

    @patch("requests.Session")
    def test_qbittorrent_add_torrent_duplicate(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent
        from core.libs.bencoding import Encoder

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "Conflict"
        mock_session.post.return_value = mock_resp

        torrent_payload = {
            b"info": {
                b"name": b"dup_archive.zip",
                b"length": 2048,
                b"piece length": 2048,
                b"pieces": b"12345678901234567890",
            },
        }
        torrent_bytes = bytes(Encoder(torrent_payload).encode())

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        success, info_hash = client.add_torrent(torrent_bytes)
        self.assertTrue(success)
        self.assertIsNotNone(info_hash)

    @patch("requests.Session")
    def test_qbittorrent_add_url_magnet(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        mock_session.post.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        magnet_link = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&dn=Sample%20Gallery.zip&tr=http%3A%2F%2Ftracker"
        )
        success, info_hash = client.add_url(magnet_link, download_dir="/downloads/200")
        self.assertTrue(success)
        self.assertEqual(info_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(client.expected_torrent_name, "Sample Gallery")
        self.assertEqual(client.expected_torrent_extension, ".zip")

        mock_session.post.assert_called_once_with(
            "http://localhost:8080/api/v2/torrents/add",
            data={"urls": magnet_link, "savepath": "/downloads/200"},
            timeout=25,
        )

    @patch("requests.Session")
    def test_qbittorrent_get_download_progress(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"hash": "0123456789abcdef0123456789abcdef01234567", "progress": 0.852},
            {"hash": "fedcba9876543210fedcba9876543210fedcba98", "progress": 1.0},
        ]
        mock_session.get.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        key1 = MagicMock()
        key2 = MagicMock()
        results = client.get_download_progress([
            ("0123456789abcdef0123456789abcdef01234567", key1),
            ("fedcba9876543210fedcba9876543210fedcba98", key2),
            ("invalid_hash", MagicMock()),
            ("", MagicMock()),
        ])

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], (key1, 85.2))
        self.assertEqual(results[1], (key2, 100.0))

        mock_session.get.assert_called_once()
        call_args, call_kwargs = mock_session.get.call_args
        self.assertEqual(call_args[0], "http://localhost:8080/api/v2/torrents/info")
        self.assertIn("hashes", call_kwargs["params"])
        self.assertIn("0123456789abcdef0123456789abcdef01234567", call_kwargs["params"]["hashes"])
        self.assertIn("fedcba9876543210fedcba9876543210fedcba98", call_kwargs["params"]["hashes"])

    def test_get_torrent_client_factory(self):
        from core.downloaders.torrent import get_torrent_client
        from core.downloaders.torrent.qbittorrent import qBittorrent
        from core.downloaders.torrent.transmission import Transmission

        qbt = get_torrent_client({"client": "qbittorrent", "address": "localhost", "port": 8080})
        self.assertIsInstance(qbt, qBittorrent)

        qbt_case = get_torrent_client({"client": "qBittorrent", "address": "localhost", "port": 8080})
        self.assertIsInstance(qbt_case, qBittorrent)

        trans = get_torrent_client({"client": "transmission", "address": "http://localhost", "port": 9091})
        self.assertIsInstance(trans, Transmission)

        # uTorrent is removed and must return None
        utorrent_client = get_torrent_client({"client": "utorrent", "address": "localhost", "port": 8080})
        self.assertIsNone(utorrent_client)

        unknown = get_torrent_client({"client": "unknown_client"})
        self.assertIsNone(unknown)

    @patch("zipfile.ZipFile")
    def test_archive_save_with_unsupported_zip_compression(self, mock_zipfile):
        mock_zip_instance = MagicMock()
        mock_zip_instance.testzip.side_effect = NotImplementedError("That compression method is not supported")
        mock_zipfile.return_value = mock_zip_instance

        with patch("os.path.isfile", return_value=True):
            archive = Archive.objects.create(
                title="unsupported zip archive",
                zipped="test.zip",
                crc32="12345678",
                user=None,
            )
            # ingest() should catch NotImplementedError without crashing, while save() does not open zip files
            with self.assertLogs("viewer.models", level="ERROR") as cm:
                archive.ingest()
            self.assertTrue(any("That compression method is not supported" in msg for msg in cm.output))

    @patch("core.downloaders.postdownload.ZipFile")
    def test_postdownload_process_archive_unsupported_compression(self, mock_zipfile):
        mock_zip_instance = MagicMock()
        mock_zip_instance.testzip.side_effect = NotImplementedError("That compression method is not supported")
        mock_zipfile.return_value = mock_zip_instance

        settings = Settings(load_from_disk=False)
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="888", provider="panda")
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="87654321",
            source_type="panda",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save") as mock_save:
            with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("NotImplementedError: That compression method is not supported" in msg for msg in cm.output))
            mock_save.assert_called_once()
            mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("time.sleep", return_value=None)
    @patch("requests.get")
    def test_request_with_retries_on_503_and_success(self, mock_get, mock_sleep):
        from core.base.utilities import request_with_retries

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_200 = MagicMock()
        resp_200.status_code = 200

        mock_get.side_effect = [resp_503, resp_200]

        res = request_with_retries("https://example.com", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep", return_value=None)
    @patch("requests.post")
    def test_request_with_retries_on_continuous_504(self, mock_post, mock_sleep):
        from core.base.utilities import request_with_retries

        resp_504 = MagicMock()
        resp_504.status_code = 504

        mock_post.return_value = resp_504

        res = request_with_retries("https://example.com", {}, post=True, retries=3)
        self.assertEqual(res.status_code, 504)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    def test_hath_downloader_retries_on_cache_error(self, mock_sleep):
        from core.providers.panda.downloaders import HathDownloader
        from core.providers.panda.settings import OwnSettings as PandaSettings
        from core.base.types import GalleryData

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        general_utils = MagicMock()
        downloader = HathDownloader(settings, general_utils)

        gallery = GalleryData("123456", "panda")
        gallery.root = "https://exhentai.org"
        gallery.token = "abcdef1234"
        gallery.title = "Test Gallery"
        downloader.gallery = gallery

        resp_cache_error = MagicMock()
        resp_cache_error.status_code = 200
        resp_cache_error.content = b"<html><body><p>Cache error</p></body></html>"
        resp_cache_error.text = "Cache error"

        resp_success = MagicMock()
        resp_success.status_code = 200
        resp_success.content = b"<html><body><div><p>An original resolution download has been queued for client <strong>12345</strong></p></div></body></html>"
        resp_success.text = "Queued"

        with patch.object(downloader, "request_hath_download", side_effect=[resp_cache_error, resp_success]) as mock_req, \
             patch("core.providers.panda.downloaders.available_filename", return_value="/tmp/test.zip"):
            downloader.start_download()

            self.assertEqual(downloader.return_code, 1)
            self.assertEqual(downloader.fileDownloaded, 1)
            self.assertEqual(mock_req.call_count, 2)
            mock_sleep.assert_called_once_with(2)


