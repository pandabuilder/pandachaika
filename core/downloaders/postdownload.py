import os
import posixpath
import shutil
import socket
import ssl
import time
import zlib
from collections.abc import Iterable, Callable
from dataclasses import dataclass
from enum import StrEnum
from ftplib import FTP_TLS
from tempfile import mkdtemp
from typing import Any
from urllib.parse import urljoin
from zipfile import ZipFile, BadZipFile
import ftplib

import django.utils.timezone as django_tz

import logging
from django.template.defaultfilters import filesizeformat
import re

from core.base.setup import Settings
from core.base.utilities import (
    convert_rar_to_zip,
    convert_7z_to_zip,
    ensure_archive_filename_within_limits,
    find_hath_directory,
)
from core.providers.chaika import constants as chaika_constants
from core.providers.panda.parsers import Parser as PandaParser
from viewer.models import Archive, ArchiveManageEntry

logger = logging.getLogger(__name__)


@dataclass
class CurrentDownload:
    filename: str = ""
    speed: float = 0.0
    index: int = 0
    total: int = 0
    downloaded: int = 0
    filesize: int = 0


@dataclass
class FTPData:
    ftps: FTP_TLS | None = None
    current_ftp_dir: str | None = None


class PostDownloader(object):

    def __init__(self, settings: "Settings", web_queue=None) -> None:
        self.settings = settings
        self.web_queue = web_queue or getattr(settings.workers, "web_queue", None)
        self.ftp_datas: dict[str, FTPData] = {}
        self.current_download: CurrentDownload = CurrentDownload()
        self.ftp_key_torrent = self.settings.download_ftp_torrent
        self.ftp_key_hath = self.settings.download_ftp_hath

    def get_requeue_downloader(self) -> str | list[str]:
        return (
            getattr(self.settings, "failed_download_downloader", None)
            or getattr(self.settings, "requeue_downloader", None)
            or "panda_archive"
        )

    def _should_requeue_download(self, archive: Archive) -> bool:
        if not getattr(self.settings, "requeue_failed_downloads", True):
            return False

        downloader = self.get_requeue_downloader()
        downloaders = downloader if isinstance(downloader, list) else [downloader]

        archive_provider = (
            getattr(archive.gallery, "provider", None)
            or getattr(archive, "source_type", None)
            or ""
        )

        for dl in downloaders:
            dl_provider = self.settings.get_downloader_provider(dl)
            if not dl_provider or not archive_provider:
                return True
            if dl_provider in archive_provider or archive_provider in dl_provider:
                return True

        return False

    def _requeue_archive(self, archive: Archive, downloader: str | list[str]) -> None:
        if self.web_queue and archive.gallery:
            config = getattr(self.settings, "config", None)
            temp_settings = Settings(load_from_config=config) if config else Settings(load_from_disk=False)
            if hasattr(self.settings, "custom_downloaders") and self.settings.custom_downloaders:
                temp_settings.custom_downloaders.update(self.settings.custom_downloaders)
            if hasattr(self.settings, "custom_downloader_providers") and self.settings.custom_downloader_providers:
                temp_settings.custom_downloader_providers.update(self.settings.custom_downloader_providers)
            if hasattr(self.settings, "downloaders") and self.settings.downloaders:
                for k, v in self.settings.downloaders.items():
                    temp_settings.downloaders.setdefault(k, v)

            downloaders = downloader if isinstance(downloader, list) else [downloader]
            temp_settings.allow_downloaders_only(downloaders, True, True, True)
            if archive.reason:
                temp_settings.archive_reason = archive.reason
            self.web_queue.enqueue_args_list((archive.gallery.get_link(),), override_options=temp_settings)

    def _get_chaika_archive_link(self, archive: Archive) -> str:
        download_event = None
        if hasattr(archive, "downloadevent_set"):
            try:
                download_event = (
                    archive.downloadevent_set.exclude(download_url="").order_by("id").last()
                )
            except Exception:
                pass
        if not download_event:
            try:
                from viewer.models import DownloadEvent

                download_event = (
                    DownloadEvent.objects.filter(archive=archive)
                    .exclude(download_url="")
                    .order_by("id")
                    .last()
                )
            except Exception:
                pass
        if not download_event and archive.gallery:
            try:
                from viewer.models import DownloadEvent
                download_event = (
                    DownloadEvent.objects.filter(gallery=archive.gallery)
                    .exclude(download_url="")
                    .order_by("id")
                    .last()
                )
            except Exception:
                pass

        chaika_provider_settings = (
            self.settings.providers.get("chaika") if hasattr(self.settings, "providers") else None
        )
        chaika_base_url = (
            getattr(chaika_provider_settings, "url", None)
            or chaika_constants.base_url
        )

        if download_event and download_event.download_url:
            dl_url = download_event.download_url
            m = re.search(r"/archive/(\d+)/", dl_url)
            if m:
                archive_id = m.group(1)
                archive_page_url = urljoin(dl_url, f"/archive/{archive_id}/")
                if archive_page_url != dl_url:
                    return f"{archive_page_url} (download: {dl_url})"
            return dl_url

        if archive.gallery and archive.gallery.gid:
            gid = archive.gallery.gid
            provider = archive.gallery.provider or "panda"
            return urljoin(chaika_base_url, f"?gid={gid}&provider={provider}")

        return chaika_base_url

    def process_downloaded_archive(self, archive: Archive) -> bool:
        if not os.path.isfile(archive.zipped.path):
            return False

        except_at_open = False
        return_error = None
        error_details = ""
        try:
            my_zip = ZipFile(archive.zipped.path, "r")
            return_error = my_zip.testzip()
            my_zip.close()
        except (BadZipFile, NotImplementedError, zlib.error) as e:
            except_at_open = True
            error_details = f"{type(e).__name__}: {e}"
        if return_error:
            error_details = f"Corrupt file: {return_error}"
        if except_at_open or return_error:
            if self._should_requeue_download(archive):
                requeue_dl = self.get_requeue_downloader()
                dl_name = ", ".join(requeue_dl) if isinstance(requeue_dl, list) else requeue_dl
                logger.error(
                    "For archive: {}, file check on downloaded zipfile failed ({}) on file: {}, "
                    "forcing download as {} to fix it.".format(
                        archive, error_details or "unknown error", archive.zipped.path, dl_name
                    )
                )
                try:
                    archive.save()
                except Exception as e:
                    logger.error("Error saving archive %s after failed zip check: %s", archive, e)
                self._requeue_archive(archive, requeue_dl)
            else:
                logger.warning(
                    "For archive: {}, file check on downloaded zipfile failed ({}): {}. "
                    "Check the file manually.".format(
                        archive, error_details or "unknown error", archive.zipped.path
                    )
                )
            if self.settings.recycle_failed_checks_downloads:
                archive.move_to_recycle_bin(reason="Failed on ZIP file opening")
                try:
                    from viewer.models import DownloadEvent
                    DownloadEvent.objects.filter(archive=archive, completed=False).update(
                        completed=True,
                        failed=True,
                        status=DownloadEvent.Status.FAILED,
                        completed_date=django_tz.now(),
                        error_message="Failed on ZIP file opening",
                    )
                except Exception:
                    pass
            return False
        # crc32 = calc_crc32(archive.zipped.path)
        # filesize, filecount = get_zip_fileinfo(archive.zipped.path)
        # values = {
        #     'crc32': crc32,
        #     'filesize': filesize,
        #     'filecount': filecount,
        # }
        # updated_archive = Archive.objects.add_or_update_from_values(values, pk=archive.pk)

        archive.recalc_fileinfo()
        archive.ingest(skip_zip_check=True)

        if self.settings.mark_similar_new_archives:
            archive.create_marks_for_similar_archives()

        is_chaika = bool(
            archive.match_type and ("chaika" in archive.match_type or archive.match_type == "panda_chaika")
        )
        is_torrent = bool(
            archive.match_type and "torrent" in archive.match_type
        )

        if archive.gallery and archive.filesize != archive.gallery.filesize:
            source_name = "Chaika" if is_chaika else "Torrent"
            mark_comment = (
                f"{source_name} downloaded has not the same file as the matched gallery"
                " (different filesize or filecount). This file must be replaced if the correct one"
                " is found."
            )
            manager_entry, _ = ArchiveManageEntry.objects.update_or_create(
                archive=archive,
                mark_reason="wrong_file",
                defaults={
                    "mark_comment": mark_comment,
                    "mark_priority": 4.3,
                    "mark_check": True,
                    "origin": ArchiveManageEntry.ORIGIN_SYSTEM,
                },
            )
            if Archive.objects.filter(gallery=archive.gallery, filesize=archive.gallery.filesize):
                logger.info(
                    "For archive: {} size does not match gallery, "
                    "but there's already another archive that matches.".format(archive)
                )
                return False
            if self._should_requeue_download(archive):
                requeue_dl = self.get_requeue_downloader()
                dl_name = ", ".join(requeue_dl) if isinstance(requeue_dl, list) else requeue_dl
                logger.info(
                    "For archive: {} size does not match gallery, "
                    "downloading again from {}.".format(archive, dl_name)
                )
                self._requeue_archive(archive, requeue_dl)
            else:
                logger.warning(
                    "For archive: {} size does not match gallery. Check the file manually.".format(archive)
                )
            if self.settings.recycle_failed_checks_downloads:
                archive.move_to_recycle_bin(reason="Size does not match gallery")
                try:
                    from viewer.models import DownloadEvent
                    DownloadEvent.objects.filter(archive=archive, completed=False).update(
                        completed=True,
                        failed=True,
                        status=DownloadEvent.Status.FAILED,
                        completed_date=django_tz.now(),
                        error_message="Size does not match gallery",
                    )
                except Exception:
                    pass
            return False
        elif (
            archive.gallery
            and archive.filesize == archive.gallery.filesize
            and (is_torrent or is_chaika)
        ):
            panda_provider_settings = (
                self.settings.providers.get("panda")
                if hasattr(self.settings, "providers")
                else None
            )

            if panda_provider_settings and getattr(panda_provider_settings, "confirm_files_after_download", False):
                logger.info(
                    "For archive: {} checking if the SHA1 for each downloaded image matches with panda's page data.".format(archive)
                )

                image_set = archive.image_set.all()
                if hasattr(image_set, "order_by"):
                    downloaded_sha1_list = [x.sha1 for x in image_set.order_by("archive_position")]
                else:
                    downloaded_sha1_list = [x.sha1 for x in image_set]

                gallery_url = archive.gallery.get_link()

                parsers = self.settings.provider_context.get_parsers(self.settings, filter_name='panda')

                if len(parsers) > 0 and isinstance(parsers[0], PandaParser):
                    panda_parser = parsers[0]
                    panda_sha1_list = panda_parser.get_sha1_hashes_from_panda(gallery_url)

                    if panda_sha1_list != downloaded_sha1_list:
                        if is_chaika:
                            chaika_link = self._get_chaika_archive_link(archive)
                            logger.warning(
                                "For archive: {}, SHA1 mismatch detected for Chaika download. Relevant links: Panda: {}, Chaika: {}".format(
                                    archive, gallery_url, chaika_link
                                )
                            )

                        if self._should_requeue_download(archive):
                            requeue_dl = self.get_requeue_downloader()
                            dl_name = ", ".join(requeue_dl) if isinstance(requeue_dl, list) else requeue_dl
                            logger.warning(
                                "For archive: {}, SHA1 mismatch on downloaded zipfile failed on file: {}, "
                                "forcing download as {} to fix it.".format(archive, archive.zipped.path, dl_name)
                            )
                            try:
                                archive.save()
                            except Exception as e:
                                logger.error("Error saving archive %s after failed SHA1 check: %s", archive, e)
                            self._requeue_archive(archive, requeue_dl)
                        else:
                            logger.warning(
                                "For archive: {}, SHA1 mismatch on downloaded zipfile: {}. "
                                "Check the file manually.".format(archive, archive.zipped.path)
                            )
                        if self.settings.recycle_failed_checks_downloads:
                            archive.move_to_recycle_bin(reason="SHA1 mismatch on downloaded zipfile")
                            try:
                                from viewer.models import DownloadEvent
                                DownloadEvent.objects.filter(archive=archive, completed=False).update(
                                    completed=True,
                                    failed=True,
                                    status=DownloadEvent.Status.FAILED,
                                    completed_date=django_tz.now(),
                                    error_message="SHA1 mismatch on downloaded zipfile",
                                )
                            except Exception:
                                pass
                        return False
                    else:
                        logger.info("For archive: {} local SHA1 matches with gallery page sha1, validation passes.".format(archive))
                else:
                    logger.warning("For archive: {} could not obtain the needed parser.".format(archive))
        try:
            from viewer.models import DownloadEvent
            DownloadEvent.objects.filter(archive=archive, completed=False).update(
                progress=100.0,
                completed=True,
                status=DownloadEvent.Status.COMPLETED,
                completed_date=django_tz.now(),
            )
        except Exception as e:
            logger.warning("Failed to complete DownloadEvent for archive %s: %s", archive, e)
        return True


    def write_file_update_progress(
        self,
        cmd: str,
        callback: Callable,
        ftp_key: str,
        filesize: int = 0,
        blocksize: int = 8192,
        rest: bool | None = None,
    ) -> str:
        ftp_data = self.ftp_datas[ftp_key]
        if ftp_data.ftps is None:
            raise ConnectionError
        ftp_data.ftps.voidcmd("TYPE I")
        with ftp_data.ftps.transfercmd(cmd, rest) as conn:
            self.current_download.filesize = filesize
            self.current_download.downloaded = 0
            self.current_download.filename = cmd.replace("RETR ", "")
            start = time.perf_counter()
            while 1:
                data = conn.recv(blocksize)
                if not data:
                    break
                downloaded = len(data)
                self.current_download.downloaded += downloaded
                current = time.perf_counter()
                if current > start:
                    self.current_download.speed = self.current_download.downloaded / (current - start)
                callback(data)
            self.current_download.filename = ""
            self.current_download.speed = 0
            self.current_download.filesize = 0
            # shutdown ssl layer
            if isinstance(conn, ssl.SSLSocket):
                conn.unwrap()
        return ftp_data.ftps.voidresp()

    def start_connection(self, ftp_config_name: str) -> bool:
        if ftp_config_name not in self.settings.ftp_configs:
            logger.error(
                "FTP configuration '%s' not found. Available configurations: %s",
                ftp_config_name,
                list(self.settings.ftp_configs.keys()),
            )
            self.ftp_datas[ftp_config_name] = FTPData()
            return False

        ftp_config = self.settings.ftp_configs[ftp_config_name]
        no_cert_check = ftp_config.get("no_certificate_check", False)
        if no_cert_check:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
        else:
            context = ssl.create_default_context()

        if "bind_host" in ftp_config and ftp_config["bind_host"]:
            if ftp_config.get("bind_port") is None:
                bind_port = 0
            else:
                bind_port = ftp_config["bind_port"]
            source_address = (ftp_config["bind_host"], bind_port)
        else:
            source_address = None

        ftp_data = FTPData()
        raw_address = ftp_config.get("address", "<unknown>")
        host = raw_address
        port = 21

        # Handle host:port formatting if present in address
        if ":" in host and not host.startswith("["):
            host_part, _, port_part = host.partition(":")
            if port_part.isdigit():
                host = host_part
                port = int(port_part)

        # Explicit port in ftp_config overrides default or parsed port
        if "port" in ftp_config and ftp_config["port"] is not None:
            try:
                port = int(ftp_config["port"])
            except (ValueError, TypeError):
                pass

        timeout = self.settings.timeout_timer
        source_addr_str = f", source_address={source_address}" if source_address else ""

        try:
            if port != 21:
                ftp_data.ftps = FTP_TLS(
                    context=context,
                    source_address=source_address,
                    timeout=timeout,
                )
                ftp_data.ftps.connect(host, port, timeout=timeout)
                ftp_data.ftps.login(
                    user=ftp_config.get("user", ""),
                    passwd=ftp_config.get("passwd", ""),
                )
            else:
                ftp_data.ftps = FTP_TLS(
                    host=host,
                    user=ftp_config.get("user", ""),
                    passwd=ftp_config.get("passwd", ""),
                    context=context,
                    source_address=source_address,
                    timeout=timeout,
                )

            # Protect data channel (Hath downloads and secure transfers)
            ftp_data.ftps.prot_p()
            self.ftp_datas[ftp_config_name] = ftp_data
            return True

        except Exception as e:
            ftp_data.ftps = None
            self.ftp_datas[ftp_config_name] = ftp_data

            # Diagnostic hint for common FTPS connection issues
            hint = ""
            if isinstance(e, ssl.SSLEOFError):
                hint = (
                    " Remote server closed the connection unexpectedly during TLS handshake. "
                    "Common causes: the FTP server at this address:port does not support FTPS / explicit TLS "
                    "(e.g. it is plain unencrypted FTP instead of FTPS), a TLS version or cipher mismatch occurred, "
                    "or a firewall/proxy dropped the TLS handshake."
                )
            elif isinstance(e, ConnectionRefusedError):
                hint = (
                    f" Connection was refused by {host}:{port}. "
                    "Please verify that the FTP service is running on the remote host and port, "
                    "and that firewall rules allow the connection."
                )
            elif isinstance(e, (ssl.SSLCertVerificationError, ssl.CertificateError)):
                hint = (
                    f" SSL certificate verification failed for {host}:{port}. "
                    f"If the server uses a self-signed certificate, consider setting "
                    f"'no_certificate_check: true' in the '{ftp_config_name}' FTP configuration."
                )
            elif isinstance(e, (socket.gaierror, socket.herror)):
                hint = f" Hostname resolution failed for '{host}'. Please verify the hostname and DNS configuration."
            elif isinstance(e, (socket.timeout, TimeoutError)):
                hint = f" Connection to {host}:{port} timed out after {timeout} seconds. Check network connectivity."
            elif isinstance(e, ftplib.error_perm):
                hint = f" FTP authentication or permission error received from {host}:{port}. Please verify credentials."
            elif isinstance(e, ftplib.all_errors):
                hint = f" FTP protocol error received from {host}:{port}."

            logger.error(
                "Failed to connect via FTPS to '%s:%s' (config: '%s', no_certificate_check: %s, "
                "timeout: %ss%s): %s: %s.%s",
                host,
                port,
                ftp_config_name,
                no_cert_check,
                timeout,
                source_addr_str,
                type(e).__name__,
                e,
                hint,
            )
            logger.debug(
                "FTPS connection traceback for %s:%s (config: '%s'):",
                host,
                port,
                ftp_config_name,
                exc_info=True,
            )
            return False

    def set_current_dir(self, ftp_key: str, self_dir: str) -> None:
        ftp_data = self.ftp_datas[ftp_key]
        ftp_data.current_ftp_dir = self_dir
        if not ftp_data.ftps:
            return None
        ftp_data.ftps.cwd(self_dir)


    # ------------------------------------------------------------------
    # Transfers from external download programs (torrent client, H@H)
    # ------------------------------------------------------------------

    def handler_for(self, kind: str) -> str:
        """Transfer handler ('local', 'local_copy', 'local_move', 'local_hardlink' or 'remote') for a download kind."""
        specific = self.settings.download_handler_torrent if kind == TORRENT else self.settings.download_handler_hath
        return specific or self.settings.download_handler

    def open_source(self, kind: str) -> "TransferSource":
        """Open the folder where the external program for ``kind`` leaves its downloads."""
        handler = self.handler_for(kind)
        if handler.startswith("local"):
            if kind == TORRENT:
                return LocalSource(self.settings.torrent["download_dir"], handler)
            return LocalSource(self.settings.providers["panda"].local_hath_folder, handler)
        if kind == TORRENT:
            return FTPSource(self, self.ftp_key_torrent, self.settings.ftps["remote_torrent_dir"])
        return FTPSource(self, self.ftp_key_hath, self.settings.providers["panda"].remote_hath_dir)

    def hath_progress(self, archives: Iterable[Archive]) -> dict[int, float]:
        """Fraction of each archive's expected size already present in the H@H download folder, keyed by archive pk.

        Archives whose folder does not exist yet are left out. Uses a single connection/listing for all archives.
        """
        archives = [a for a in archives if a.gallery and a.gallery.gid and a.filesize]
        if not archives:
            return {}
        progress: dict[int, float] = {}
        with self.open_source(HATH) as source:
            folders = source.folders()
            for archive in archives:
                if not (archive.gallery and archive.filesize):
                    continue
                folder = find_hath_directory(folders, archive.gallery.gid)
                if folder:
                    images = _hath_images(source.entries(folder))
                    progress[archive.pk] = sum(x.size for x in images) / archive.filesize
        return progress

    def transfer_archive(self, archive: Archive) -> TransferResult:
        """Bring a finished torrent/H@H download into media storage and process it."""
        if not archive.zipped:
            return TransferResult.NOT_FOUND
        kind = download_kind(archive.match_type)
        if kind == TORRENT:
            return self.transfer_torrent(archive)
        if kind == HATH:
            return self.transfer_hath(archive)
        return TransferResult.NOT_FOUND

    def transfer_hath(self, archive: Archive) -> TransferResult:
        if not (archive.gallery and archive.gallery.gid and archive.filesize):
            return TransferResult.NOT_FOUND
        with self.open_source(HATH) as source:
            folder = find_hath_directory(source.folders(), archive.gallery.gid)
            if not folder:
                return TransferResult.NOT_FOUND
            images = _hath_images(source.entries(folder))
            downloaded = sum(x.size for x in images)
            if downloaded < int(archive.filesize):
                logger.info(
                    "For archive: {}, folder: {} has not completed the download ({}/{}), skipping".format(
                        archive, folder, filesizeformat(downloaded), filesizeformat(archive.filesize)
                    )
                )
                return TransferResult.INCOMPLETE
            logger.info("For archive: {}, creating zip for folder {}, {} images".format(archive, folder, len(images)))
            self._zip_from_source(source, archive, folder, images)
        self.process_downloaded_archive(archive)
        return TransferResult.PROCESSED

    def transfer_torrent(self, archive: Archive) -> TransferResult:
        """Torrents are queued with <download dir>/<gid> as save path, so whatever is inside that folder is the
        download: a single archive file, a folder (the torrent's root folder) or the files themselves."""
        if not (archive.gallery and archive.gallery.gid):
            return TransferResult.NOT_FOUND
        gid = str(archive.gallery.gid)
        with self.open_source(TORRENT) as source:
            try:
                content = find_torrent_content(source, gid)
            except FileNotFoundError:
                return TransferResult.NOT_FOUND
            if isinstance(content, TransferResult):
                logger.debug("For archive: {}, torrent folder {}: {}".format(archive, gid, content.value))
                return content
            path, entry, files = content
            if entry is None:
                logger.info("For archive: {}, creating zip for folder: {}, {} files".format(archive, path, len(files)))
                self._zip_from_source(source, archive, path, files)
            else:
                target_path = ensure_archive_filename_within_limits(archive, self.settings.MEDIA_ROOT)
                logger.info("For archive: {}, transferring file: {} to: {}".format(archive, path, target_path))
                self.current_download.index, self.current_download.total = 1, 1
                try:
                    source.fetch(path, target_path, entry.size, final=True)
                finally:
                    self.current_download = CurrentDownload()
                extension = os.path.splitext(entry.name)[1].lower()
                converter = ARCHIVE_CONVERTERS.get(extension)
                if self.settings.convert_others_to_zip and converter:
                    logger.info("For archive: {}, converting {}: {} to zip".format(archive, extension, target_path))
                    converter(target_path, temp_path=self.settings.temp_directory_path)
        self.process_downloaded_archive(archive)
        return TransferResult.PROCESSED

    def _zip_from_source(
        self, source: "TransferSource", archive: Archive, folder: str, files: list["SourceEntry"]
    ) -> None:
        files = sorted(files, key=lambda x: x.name)
        staging_dir = mkdtemp(dir=self.settings.temp_directory_path)
        try:
            self.current_download.total = len(files)
            for index, entry in enumerate(files, start=1):
                self.current_download.index = index
                self.current_download.filename = entry.name
                source.fetch(posixpath.join(folder, entry.name), os.path.join(staging_dir, entry.name), entry.size)
            target_zip_path = ensure_archive_filename_within_limits(archive, self.settings.MEDIA_ROOT)
            with ZipFile(target_zip_path, "w") as archive_file:
                for entry in files:
                    archive_file.write(os.path.join(staging_dir, entry.name), arcname=entry.name)
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
            self.current_download = CurrentDownload()


TORRENT = "torrent"
HATH = "hath"


def download_kind(method_or_match_type: str | None) -> str | None:
    """Map a DownloadEvent method or Archive match_type ('torrent', 'torrent_api', 'hath', ...) to TORRENT/HATH."""
    if not method_or_match_type:
        return None
    if method_or_match_type.startswith(TORRENT):
        return TORRENT
    if method_or_match_type.startswith(HATH):
        return HATH
    return None


class TransferResult(StrEnum):
    PROCESSED = "processed"  # Moved into media storage and run through process_downloaded_archive.
    INCOMPLETE = "incomplete"  # Found, but the external program has not finished writing it.
    NOT_FOUND = "not_found"  # Nothing matching in the download folder.


@dataclass
class SourceEntry:
    name: str
    is_dir: bool
    size: int = 0


def _hath_images(entries: list[SourceEntry]) -> list[SourceEntry]:
    return [x for x in entries if not x.is_dir and x.name != "galleryinfo.txt"]


ARCHIVE_EXTENSIONS = (".zip", ".cbz", ".rar", ".cbr", ".7z", ".cb7")
ARCHIVE_CONVERTERS: dict[str, Callable[..., int]] = {
    ".rar": convert_rar_to_zip,
    ".cbr": convert_rar_to_zip,
    ".7z": convert_7z_to_zip,
    ".cb7": convert_7z_to_zip,
}
# Suffixes torrent clients give to files still being written (qBittorrent, Transmission, generic).
PARTIAL_DOWNLOAD_SUFFIXES = (".!qb", ".part", ".incomplete")


def find_torrent_content(
    source: "TransferSource", folder: str
) -> "tuple[str, SourceEntry | None, list[SourceEntry]] | TransferResult":
    """Decide what to transfer from a torrent's save folder, without relying on file names.

    Returns (path, entry, []) for a single archive file at ``path``, (path, None, files) for a folder whose files
    must be zipped, or a TransferResult when there is nothing usable. A folder holding only one subfolder (the
    torrent's root folder) is followed into. Raises FileNotFoundError when ``folder`` doesn't exist.
    """
    entries = source.entries(folder)
    files = [x for x in entries if not x.is_dir]
    dirs = [x for x in entries if x.is_dir]
    if any(x.name.lower().endswith(PARTIAL_DOWNLOAD_SUFFIXES) for x in files):
        return TransferResult.INCOMPLETE
    if not files:
        if len(dirs) == 1:
            return find_torrent_content(source, posixpath.join(folder, dirs[0].name))
        if dirs:
            logger.warning(
                "Torrent folder {} has several subfolders ({}), can't tell which one is the download".format(
                    folder, ", ".join(x.name for x in dirs)
                )
            )
        return TransferResult.NOT_FOUND
    archives = [x for x in files if os.path.splitext(x.name)[1].lower() in ARCHIVE_EXTENSIONS]
    if len(files) == 1 and archives:
        return posixpath.join(folder, archives[0].name), archives[0], []
    if archives:
        logger.warning(
            "Torrent folder {} has archives mixed with other files ({}), can't tell which one is the download".format(
                folder, ", ".join(x.name for x in files)
            )
        )
        return TransferResult.NOT_FOUND
    return folder, None, files


class TransferSource:
    """Folder where an external program leaves finished downloads. Paths are relative to its root."""

    def entries(self, path: str = "") -> list[SourceEntry]:
        """List a folder. Raises FileNotFoundError when it doesn't exist."""
        raise NotImplementedError

    def folders(self) -> list[str]:
        try:
            return [x.name for x in self.entries() if x.is_dir]
        except FileNotFoundError:
            logger.warning("Download folder of {} does not exist".format(self))
            return []

    def fetch(self, path: str, dest: str, size: int = 0, final: bool = False) -> None:
        """Bring ``path`` to the local ``dest``. ``final`` means dest is the archive itself, not a staging file."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "TransferSource":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


class LocalSource(TransferSource):
    def __init__(self, root: str, mode: str) -> None:
        self.root = root
        self.mode = mode

    def __str__(self) -> str:
        return "local folder {} ({})".format(self.root, self.mode)

    def entries(self, path: str = "") -> list[SourceEntry]:
        with os.scandir(os.path.join(self.root, path)) as iterator:
            return [
                SourceEntry(x.name, x.is_dir(), 0 if x.is_dir() else x.stat().st_size)
                for x in iterator
                if x.is_dir() or x.is_file()
            ]

    def fetch(self, path: str, dest: str, size: int = 0, final: bool = False) -> None:
        source_path = os.path.join(self.root, path)
        if self.mode == "local_move":
            shutil.move(source_path, dest)
        elif self.mode == "local_hardlink" and final:
            # Staging files live in the temp folder, usually another filesystem, so only the final file is linked.
            if os.path.lexists(dest):
                os.remove(dest)
            os.link(source_path, dest)
        else:
            shutil.copy(source_path, dest)


class FTPSource(TransferSource):
    fetch_retries = 10

    def __init__(self, post_downloader: PostDownloader, ftp_key: str, root: str) -> None:
        self.post_downloader = post_downloader
        self.ftp_key = ftp_key
        self.root = root
        self._connect()

    def __str__(self) -> str:
        return "FTP config '{}' folder {}".format(self.ftp_key, self.root)

    def _connect(self) -> None:
        if not self.post_downloader.start_connection(self.ftp_key):
            raise ConnectionError("Could not connect to FTP config '{}'".format(self.ftp_key))
        self.post_downloader.set_current_dir(self.ftp_key, self.root)

    @property
    def ftps(self) -> FTP_TLS:
        ftps = self.post_downloader.ftp_datas[self.ftp_key].ftps
        if ftps is None:
            raise ConnectionError("FTP connection '{}' is not initialized".format(self.ftp_key))
        return ftps

    def entries(self, path: str = "") -> list[SourceEntry]:
        try:
            lines = list(self.ftps.mlsd(path=path, facts=["type", "size"]))
        except ftplib.error_perm as e:
            raise FileNotFoundError(path) from e
        return [
            SourceEntry(name, facts["type"] == "dir", int(facts.get("size") or 0) if facts["type"] == "file" else 0)
            for name, facts in lines
            if name and facts.get("type") in ("file", "dir")
        ]

    def fetch(self, path: str, dest: str, size: int = 0, final: bool = False) -> None:
        for attempt in range(1, self.fetch_retries + 1):
            try:
                with open(dest, "wb") as file:
                    self.post_downloader.write_file_update_progress("RETR %s" % path, file.write, self.ftp_key, size)
                return
            except (ConnectionResetError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError) as e:
                logger.warning(
                    "FTP transfer of {} failed (attempt {} of {}): {}, reconnecting...".format(
                        path, attempt, self.fetch_retries, e
                    )
                )
                self.close()
                self._connect()
        raise ConnectionError("FTP transfer of {} failed after {} attempts".format(path, self.fetch_retries))

    def close(self) -> None:
        ftp_data = self.post_downloader.ftp_datas.get(self.ftp_key)
        if ftp_data and ftp_data.ftps is not None:
            try:
                ftp_data.ftps.close()
            except Exception:
                pass
            ftp_data.ftps = None
