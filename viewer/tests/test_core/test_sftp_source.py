import errno
import os
import shutil
import stat
import tempfile
from unittest.mock import patch

import paramiko
from django.test import SimpleTestCase

from core.base.config_models import FtpsSettings
from core.base.setup import Settings
from core.downloaders.postdownload import (
    FTPSource,
    PostDownloader,
    SFTPSource,
    TORRENT,
    HATH,
    parse_address,
)
from core.providers.panda.settings import OwnSettings as PandaSettings


def sftp_attr(name: str, mode: int, size: int = 0) -> paramiko.SFTPAttributes:
    attr = paramiko.SFTPAttributes()
    attr.filename = name
    attr.st_mode = mode
    attr.st_size = size
    return attr


class SFTPSourceTest(SimpleTestCase):
    def setUp(self):
        self.settings = Settings(
            load_from_config={
                "general": {"download_handler": "remote", "download_ftp_torrent": "seedbox"},
                "ftps": {"remote_torrent_dir": "/remote/torrent"},
                "ftp_configs": {
                    "seedbox": {
                        "protocol": "sftp",
                        "address": "seedbox.local:2222",
                        "user": "secret_user",
                        "passwd": "secret_password_123",
                    },
                },
                "providers": {},
            }
        )
        panda_settings = PandaSettings(self.settings, {})
        panda_settings.remote_hath_dir = "/remote/hath"
        self.settings.providers["panda"] = panda_settings
        self.downloader = PostDownloader(self.settings)

        ssh_patch = patch("core.downloaders.postdownload.paramiko.SSHClient")
        socket_patch = patch("core.downloaders.postdownload.socket.create_connection")
        self.ssh_cls = ssh_patch.start()
        self.create_connection = socket_patch.start()
        self.addCleanup(ssh_patch.stop)
        self.addCleanup(socket_patch.stop)
        self.ssh = self.ssh_cls.return_value
        self.sftp = self.ssh.open_sftp.return_value

    def test_protocol_selects_source(self):
        with self.downloader.open_source(TORRENT) as source:
            self.assertIsInstance(source, SFTPSource)
            self.assertEqual(source.root, "/remote/torrent")
        self.create_connection.assert_called_once_with(("seedbox.local", 2222), timeout=self.settings.timeout_timer, source_address=None)
        kwargs = self.ssh.connect.call_args.kwargs
        self.assertEqual(kwargs["username"], "secret_user")
        self.assertEqual(kwargs["password"], "secret_password_123")
        self.assertFalse(kwargs["look_for_keys"])
        self.sftp.close.assert_called_once()
        self.ssh.close.assert_called_once()

        # download_ftp_hath points to 'default', which is the plain 'ftps' section.
        with patch.object(PostDownloader, "start_connection", return_value=True), patch.object(
            PostDownloader, "set_current_dir"
        ):
            self.assertIsInstance(self.downloader.open_source(HATH), FTPSource)

        self.settings.ftp_configs["seedbox"]["protocol"] = "scp"
        with self.assertRaises(ValueError):
            self.downloader.open_source(TORRENT)

    def test_parse_address_ports(self):
        self.assertEqual(parse_address({"address": "host"}, 22), ("host", 22))
        self.assertEqual(parse_address({"address": "host:2200"}, 22), ("host", 2200))
        self.assertEqual(parse_address({"address": "host:2200", "port": 23}, 22), ("host", 23))

    def test_key_auth_and_host_key_policy(self):
        self.settings.ftp_configs["seedbox"].update(
            passwd="",
            key_filename="~/keys/id_ed25519",
            key_passphrase="key_pass",
            known_hosts_file="/keys/known_hosts",
            bind_host="10.0.0.2",
        )
        SFTPSource(self.downloader, "seedbox", "/remote").close()
        kwargs = self.ssh.connect.call_args.kwargs
        self.assertIsNone(kwargs["password"])
        self.assertEqual(kwargs["key_filename"], os.path.expanduser("~/keys/id_ed25519"))
        self.assertEqual(kwargs["passphrase"], "key_pass")
        self.ssh.load_host_keys.assert_called_once_with("/keys/known_hosts")
        self.assertIsInstance(self.ssh.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
        self.assertEqual(self.create_connection.call_args.kwargs["source_address"], ("10.0.0.2", 0))

        self.settings.ftp_configs["seedbox"]["no_certificate_check"] = True
        SFTPSource(self.downloader, "seedbox", "/remote").close()
        self.assertNotIsInstance(self.ssh.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)

    def test_connection_failure_logs_hint_without_credentials(self):
        self.ssh.connect.side_effect = paramiko.AuthenticationException("Authentication failed.")
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm, self.assertRaises(ConnectionError):
            SFTPSource(self.downloader, "seedbox", "/remote")
        log_output = "\n".join(cm.output)
        self.assertIn("seedbox.local:2222", log_output)
        self.assertIn("SSH authentication failed", log_output)
        self.assertNotIn("secret_user", log_output)
        self.assertNotIn("secret_password_123", log_output)
        self.ssh.close.assert_called()

        self.ssh.connect.side_effect = paramiko.SSHException("Server '[seedbox.local]:2222' not found in known_hosts")
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm, self.assertRaises(ConnectionError):
            SFTPSource(self.downloader, "seedbox", "/remote")
        self.assertIn("known_hosts_file", "\n".join(cm.output))

    def test_missing_config(self):
        with self.assertLogs("core.downloaders.postdownload", level="ERROR"), self.assertRaises(ConnectionError):
            SFTPSource(self.downloader, "nope", "/remote")

    def test_entries(self):
        self.sftp.listdir_attr.return_value = [
            sftp_attr("folder", stat.S_IFDIR | 0o755),
            sftp_attr("file.zip", stat.S_IFREG | 0o644, 1234),
            sftp_attr("link.jpg", stat.S_IFLNK | 0o777, 8),
            sftp_attr("broken.jpg", stat.S_IFLNK | 0o777, 8),
            sftp_attr("socket", stat.S_IFSOCK | 0o777),
        ]

        def fake_stat(path):
            if path.endswith("link.jpg"):
                return sftp_attr("", stat.S_IFREG | 0o644, 99)
            raise FileNotFoundError(errno.ENOENT, "No such file")

        self.sftp.stat.side_effect = fake_stat
        source = SFTPSource(self.downloader, "seedbox", "/remote")
        entries = source.entries("123")
        self.sftp.listdir_attr.assert_called_once_with("/remote/123")
        self.assertEqual(
            [(x.name, x.is_dir, x.size) for x in entries],
            [("folder", True, 0), ("file.zip", False, 1234), ("link.jpg", False, 99)],
        )
        source.entries()
        self.sftp.listdir_attr.assert_called_with("/remote")

        self.sftp.listdir_attr.side_effect = IOError(errno.ENOENT, "No such file")
        with self.assertRaises(FileNotFoundError):
            source.entries("missing")
        self.sftp.listdir_attr.side_effect = IOError(errno.EACCES, "Permission denied")
        with self.assertRaises(PermissionError):
            source.entries("forbidden")

    def test_fetch_reports_progress_and_reconnects(self):
        temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, temp_dir, ignore_errors=True)
        dest = os.path.join(temp_dir, "file.zip")
        progress_seen = []

        def fake_get(remote, local, callback):
            callback(5, 10)
            progress_seen.append(
                (self.downloader.current_download.filename, self.downloader.current_download.downloaded)
            )
            if len(progress_seen) == 1:
                raise OSError("Socket is closed")
            with open(local, "wb") as f:
                f.write(b"0123456789")

        self.sftp.get.side_effect = fake_get
        source = SFTPSource(self.downloader, "seedbox", "/remote")
        with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
            source.fetch("123/file.zip", dest, 10, final=True)
        self.assertIn("reconnecting", cm.output[0])
        self.assertEqual(self.sftp.get.call_args.args[:2], ("/remote/123/file.zip", dest))
        self.assertEqual(progress_seen, [("123/file.zip", 5), ("123/file.zip", 5)])
        self.assertEqual(self.ssh.connect.call_count, 2)
        self.assertEqual(self.downloader.current_download.filename, "")

        self.sftp.get.side_effect = FileNotFoundError(errno.ENOENT, "No such file")
        with self.assertRaises(FileNotFoundError):
            source.fetch("123/missing.zip", dest)
        self.assertEqual(self.ssh.connect.call_count, 2)

    def test_fetch_gives_up_after_retries(self):
        self.sftp.get.side_effect = EOFError()
        source = SFTPSource(self.downloader, "seedbox", "/remote")
        source.fetch_retries = 2
        with self.assertLogs("core.downloaders.postdownload", level="WARNING"), self.assertRaises(ConnectionError):
            source.fetch("file.zip", os.devnull)


class FtpsSettingsProtocolTest(SimpleTestCase):
    def test_protocol_is_validated(self):
        self.assertEqual(FtpsSettings().protocol, "ftps")
        self.assertEqual(FtpsSettings(protocol="SFTP").protocol, "sftp")
        with self.assertRaises(ValueError):
            FtpsSettings(protocol="scp")

    def test_settings_loads_sftp_section(self):
        settings = Settings(load_from_config={"ftps": {"protocol": "sftp", "key_filename": "/k"}, "providers": {}})
        self.assertEqual(settings.ftps["protocol"], "sftp")
        self.assertEqual(settings.ftp_configs["default"]["key_filename"], "/k")
        downloader = PostDownloader(settings)
        self.assertEqual(downloader.connection_protocol("default"), "sftp")
        self.assertEqual(downloader.connection_protocol("undefined"), "ftps")
