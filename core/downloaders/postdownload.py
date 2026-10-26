import os
import queue
import shutil
import socket
import ssl
import time
import traceback
import zlib
from collections.abc import Iterable, Callable
from dataclasses import dataclass
from ftplib import FTP_TLS
from tempfile import mkdtemp
from typing import Any
from urllib.parse import urljoin
from zipfile import ZipFile, BadZipFile
import ftplib

import django.utils.timezone as django_tz
import threading

import logging
from django.template.defaultfilters import filesizeformat
import re

from core.base.setup import Settings
from core.base.utilities import convert_rar_to_zip, replace_illegal_name, convert_7z_to_zip
from core.providers.chaika import constants as chaika_constants
from core.providers.panda.parsers import Parser as PandaParser
from core.workers.schedulers import BaseScheduler
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
            dl_model = getattr(self.settings, "download_event_model", None)
            if dl_model is not None:
                try:
                    download_event = (
                        dl_model.objects.filter(archive=archive)
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
        archive.save()

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
                        return False
                    else:
                        logger.info("For archive: {} local SHA1 matches with gallery page sha1, validation passes.".format(archive))
                else:
                    logger.warning("For archive: {} could not obtain the needed parser.".format(archive))
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

    def download_all_missing(self, archives: Iterable[Archive] | None = None) -> None:

        ftp_key_torrent = self.ftp_key_torrent
        ftp_key_hath = self.ftp_key_hath

        files_torrent = []
        files_hath = []

        if not archives:
            found_archives: Iterable[Archive] = list(Archive.objects.filter_by_dl_remote())
        else:
            found_archives = archives

        if not found_archives:
            return None

        for archive in found_archives:
            if archive.match_type:
                if "torrent" in archive.match_type:
                    files_torrent.append(archive)
                elif "hath" in archive.match_type:
                    files_hath.append(archive)

        if len(files_torrent) + len(files_hath) == 0:
            return None

        if ftp_key_hath == ftp_key_torrent:
            self.start_connection(ftp_key_hath)

        # Hath downloads
        if len(files_hath) > 0:

            if ftp_key_hath != ftp_key_torrent:
                self.start_connection(ftp_key_hath)

            ftp_data_hath = self.ftp_datas[ftp_key_hath]
            if not ftp_data_hath.ftps:
                logger.error("Cannot download the archives, the Hath FTP connection is not initialized.")
                return None

            self.set_current_dir(ftp_key_hath, self.settings.providers["panda"].remote_hath_dir)
            # self.ftps.encoding = 'utf8'

            files_matched_hath = []
            for line in ftp_data_hath.ftps.mlsd(facts=["type"]):
                if line[1]["type"] != "dir":
                    continue
                m = re.search(r".*?\[(\d+)]$", line[0])
                if m:
                    for archive in files_hath:
                        if archive.gallery and archive.filesize and m.group(1) == archive.gallery.gid:
                            files_matched_hath.append((line[0], archive.zipped.path, int(archive.filesize), archive))

            for matched_file_hath in files_matched_hath:
                total_remote_size = 0
                remote_ftp_tuples = []
                for img_file_tuple in ftp_data_hath.ftps.mlsd(path=matched_file_hath[0], facts=["type", "size"]):
                    if img_file_tuple[1]["type"] != "file" or img_file_tuple[0] == "galleryinfo.txt":
                        continue
                    total_remote_size += int(img_file_tuple[1]["size"])
                    remote_ftp_tuples.append((img_file_tuple[0], img_file_tuple[1]["size"]))
                if total_remote_size != matched_file_hath[2]:
                    logger.info(
                        "For archive: {archive}, remote folder: {folder} "
                        "has not completed the download ({current}/{total}), skipping".format(
                            archive=matched_file_hath[3],
                            folder=matched_file_hath[0],
                            current=filesizeformat(total_remote_size),
                            total=filesizeformat(matched_file_hath[2]),
                        )
                    )
                    continue
                logger.info(
                    "For archive: {archive}, downloading and creating zip "
                    "for folder {filename}, {image_count} images".format(
                        archive=matched_file_hath[3], filename=matched_file_hath[1], image_count=len(remote_ftp_tuples)
                    )
                )
                dir_path = mkdtemp(dir=self.settings.temp_directory_path)
                self.current_download.total = len(remote_ftp_tuples)
                for count, remote_file in enumerate(sorted(remote_ftp_tuples), start=1):
                    for retry_count in range(10):
                        try:
                            with open(os.path.join(dir_path, remote_file[0]), "wb") as file:
                                self.current_download.index = count
                                self.write_file_update_progress(
                                    "RETR %s" % (str(matched_file_hath[0]) + "/" + remote_file[0]),
                                    file.write,
                                    ftp_key_hath,
                                    int(remote_file[1]),
                                )
                        except (ConnectionResetError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError):
                            logger.warning(
                                "Hath download failed for file {} of {}, restarting connection...".format(
                                    count, len(remote_ftp_tuples)
                                )
                            )
                            if ftp_data_hath.ftps is not None:
                                try:
                                    ftp_data_hath.ftps.close()
                                except Exception:
                                    pass
                            self.start_connection(ftp_key_hath)
                            if not ftp_data_hath.ftps:
                                logger.error(
                                    "Failed to reconnect to Hath FTP server for file {} of {}.".format(
                                        count, len(remote_ftp_tuples)
                                    )
                                )
                                break
                            self.set_current_dir(ftp_key_hath, self.settings.providers["panda"].remote_hath_dir)
                        else:
                            break
                with ZipFile(os.path.join(self.settings.MEDIA_ROOT, matched_file_hath[1]), "w") as archive_file:
                    for root_path, _, file_names in os.walk(dir_path):
                        for current_file in file_names:
                            archive_file.write(
                                os.path.join(root_path, current_file), arcname=os.path.basename(current_file)
                            )
                shutil.rmtree(dir_path, ignore_errors=True)

                self.process_downloaded_archive(matched_file_hath[3])

            if ftp_key_hath != ftp_key_torrent and ftp_data_hath.ftps is not None:
                try:
                    ftp_data_hath.ftps.close()
                except Exception:
                    pass

            if ftp_key_hath == ftp_key_torrent and ftp_data_hath.ftps is not None and len(files_torrent) == 0:
                try:
                    ftp_data_hath.ftps.close()
                except Exception:
                    pass

        # Torrent downloads

        if len(files_torrent) > 0:

            if ftp_key_hath != ftp_key_torrent:
                self.start_connection(ftp_key_torrent)

            ftp_data_torrent = self.ftp_datas[ftp_key_torrent]

            if not ftp_data_torrent.ftps:
                logger.error("Cannot download the archives, the Torrent FTP connection is not initialized.")
                return None

            self.set_current_dir(ftp_key_torrent, self.settings.ftps["remote_torrent_dir"])
            ftp_data_torrent.ftps.encoding = "utf8"
            files_matched_torrent = []
            for archive in files_torrent:
                if not archive.gallery:
                    continue
                gid_str = str(archive.gallery.gid)
                
                try:
                    lines = list(ftp_data_torrent.ftps.mlsd(path=gid_str, facts=["type", "size"]))
                except ftplib.error_perm:
                    continue
                except Exception:
                    continue
                    
                cleaned_torrent_name = os.path.splitext(os.path.basename(archive.zipped.path))[0].replace(
                    " [" + archive.gallery.gid + "]", ""
                )
                
                for line in lines:
                    if not line[0]:
                        continue
                    if "type" not in line[1]:
                        continue
                    if line[1]["type"] != "dir" and line[1]["type"] != "file":
                        continue
                        
                    if replace_illegal_name(os.path.splitext(line[0])[0]) == cleaned_torrent_name:
                        matched_path = gid_str + "/" + line[0]
                        if line[1]["type"] == "dir":
                            files_matched_torrent.append((matched_path, line[1]["type"], 0, archive))
                        else:
                            files_matched_torrent.append((matched_path, line[1]["type"], int(line[1]["size"]), archive))
            for matched_file_torrent in files_matched_torrent:
                if matched_file_torrent[1] == "dir":
                    dir_path = mkdtemp(dir=self.settings.temp_directory_path)
                    remote_ftp_files = list(
                        ftp_data_torrent.ftps.mlsd(path=matched_file_torrent[0], facts=["type", "size"])
                    )
                    self.current_download.total = len(remote_ftp_files)
                    logger.info(
                        "For archive: {archive}, downloading and creating zip "
                        "for folder {filename}, {image_count} images".format(
                            archive=matched_file_torrent[3],
                            filename=matched_file_torrent[0],
                            image_count=len(remote_ftp_files),
                        )
                    )
                    for count, img_file_tuple in enumerate(remote_ftp_files):
                        if img_file_tuple[1]["type"] != "file":
                            continue
                        for retry_count in range(10):
                            try:
                                with open(os.path.join(dir_path, img_file_tuple[0]), "wb") as file:
                                    self.current_download.index = count
                                    self.write_file_update_progress(
                                        "RETR %s" % (str(matched_file_torrent[0]) + "/" + img_file_tuple[0]),
                                        file.write,
                                        ftp_key_torrent,
                                        int(img_file_tuple[1]["size"]),
                                    )
                            except (ConnectionResetError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError):
                                logger.warning("Torrent download failed for folder, restarting connection...")
                                if ftp_data_torrent.ftps is not None:
                                    try:
                                        ftp_data_torrent.ftps.close()
                                    except Exception:
                                        pass
                                self.start_connection(ftp_key_torrent)
                                if not ftp_data_torrent.ftps:
                                    logger.error("Failed to reconnect to Torrent FTP server for folder.")
                                    break
                                self.set_current_dir(ftp_key_torrent, self.settings.ftps["remote_torrent_dir"])
                            else:
                                break
                    with ZipFile(matched_file_torrent[3].zipped.path, "w") as archive_file:
                        for root_path, _, file_names in os.walk(dir_path):
                            for current_file in file_names:
                                archive_file.write(
                                    os.path.join(root_path, current_file), arcname=os.path.basename(current_file)
                                )
                    shutil.rmtree(dir_path, ignore_errors=True)
                else:
                    logger.info(
                        "For archive: {archive} downloading remote file: {remote} to local file: {local}".format(
                            archive=matched_file_torrent[3],
                            remote=matched_file_torrent[0],
                            local=matched_file_torrent[3].zipped.path,
                        )
                    )
                    self.current_download.total = 1
                    for retry_count in range(10):
                        try:
                            with open(matched_file_torrent[3].zipped.path, "wb") as file:
                                self.current_download.index = 1
                                self.write_file_update_progress(
                                    "RETR %s" % matched_file_torrent[0],
                                    file.write,
                                    ftp_key_torrent,
                                    matched_file_torrent[2],
                                )
                        except (ConnectionResetError, socket.timeout, TimeoutError, ConnectionError, ssl.SSLError):
                            logger.warning("Torrent download failed for archive, restarting connection...")
                            if ftp_data_torrent.ftps is not None:
                                try:
                                    ftp_data_torrent.ftps.close()
                                except Exception:
                                    pass
                            self.start_connection(ftp_key_torrent)
                            if not ftp_data_torrent.ftps:
                                logger.error("Failed to reconnect to Torrent FTP server for archive.")
                                break
                            self.set_current_dir(ftp_key_torrent, self.settings.ftps["remote_torrent_dir"])
                        else:
                            break
                    if self.settings.convert_others_to_zip:
                        if os.path.splitext(matched_file_torrent[0])[1].lower() == ".rar":
                            logger.info(
                                "For archive: {}, converting rar: {} to zip".format(
                                    matched_file_torrent[3], matched_file_torrent[3].zipped.path
                                )
                            )
                            convert_rar_to_zip(
                                matched_file_torrent[3].zipped.path, temp_path=self.settings.temp_directory_path
                            )
                        elif os.path.splitext(matched_file_torrent[0])[1].lower() == ".7z":
                            logger.info(
                                "For archive: {}, converting 7z: {} to zip".format(
                                    matched_file_torrent[3], matched_file_torrent[3].zipped.path
                                )
                            )
                            convert_7z_to_zip(
                                matched_file_torrent[3].zipped.path, temp_path=self.settings.temp_directory_path
                            )

                self.process_downloaded_archive(matched_file_torrent[3])

            if ftp_key_hath != ftp_key_torrent and ftp_data_torrent.ftps is not None:
                try:
                    ftp_data_torrent.ftps.close()
                except Exception:
                    pass

            if ftp_key_hath == ftp_key_torrent and ftp_data_torrent.ftps is not None:
                try:
                    ftp_data_torrent.ftps.close()
                except Exception:
                    pass
                return None
            return None
        return None

    def copy_all_missing(self, mode, archives: Iterable[Archive] | None = None):
        files_torrent = []
        files_hath = []

        if not archives:
            found_archives: Iterable[Archive] = list(Archive.objects.filter_by_dl_remote())
        else:
            found_archives = archives

        logger.debug("Processing {} found archives to download.".format(len(list(found_archives))))

        if not found_archives:
            return

        for archive in found_archives:
            if not os.path.isfile(archive.zipped.path) and archive.match_type:
                if "torrent" in archive.match_type:
                    files_torrent.append(archive)
                elif "hath" in archive.match_type:
                    files_hath.append(archive)

        logger.debug("{} torrent-based, {} hath-based.".format(len(files_torrent), len(files_hath)))

        if len(files_torrent) + len(files_hath) == 0:
            return

        # Hath downloads
        if len(files_hath) > 0:
            files_matched_hath: list[tuple[str, str, int, Archive]] = []
            for matched_file in os.listdir(self.settings.providers["panda"].local_hath_folder):
                if os.path.isfile(os.path.join(self.settings.providers["panda"].local_hath_folder, matched_file)):
                    continue
                m = re.search(r".*?\[(\d+)]$", matched_file)
                if m:
                    for archive in files_hath:
                        if archive.gallery and archive.filesize and m.group(1) == archive.gallery.gid:
                            files_matched_hath.append(
                                (matched_file, archive.zipped.path, int(archive.filesize), archive)
                            )

            for img_dir in files_matched_hath:
                total_remote_size = 0
                remote_files = []
                directory = os.path.join(self.settings.providers["panda"].local_hath_folder, img_dir[0])
                for img_file in os.listdir(directory):
                    if not os.path.isfile(os.path.join(directory, img_file)) or img_file == "galleryinfo.txt":
                        continue
                    total_remote_size += os.stat(os.path.join(directory, img_file)).st_size
                    remote_files.append(os.path.join(directory, img_file))
                if total_remote_size != img_dir[2]:
                    logger.info(
                        "For archive: {archive}, folder: {folder} "
                        "has not completed the download ({current}/{total}), skipping".format(
                            archive=img_dir[3],
                            folder=img_dir[0],
                            current=filesizeformat(total_remote_size),
                            total=filesizeformat(img_dir[2]),
                        )
                    )
                    continue
                logger.info(
                    "For archive: {archive}, creating zip "
                    "for folder {filename}, {image_count} images".format(
                        archive=img_dir[3], filename=img_dir[1], image_count=len(remote_files)
                    )
                )
                dir_path = mkdtemp(dir=self.settings.temp_directory_path)
                for img_file_original in remote_files:
                    img_file = os.path.split(img_file_original)[1]
                    if mode == "local_move":
                        shutil.move(img_file_original, os.path.join(dir_path, img_file))
                    # Disabled since we can't hardlink to a temp filesystem
                    elif mode == "local_hardlink":
                        # os.link(img_file_original, os.path.join(dir_path, img_file))
                        shutil.copy(img_file_original, os.path.join(dir_path, img_file))
                    else:
                        shutil.copy(img_file_original, os.path.join(dir_path, img_file))
                with ZipFile(os.path.join(self.settings.MEDIA_ROOT, img_dir[1]), "w") as archive_file:
                    for root_path, _, file_names in os.walk(dir_path):
                        for current_file in file_names:
                            archive_file.write(
                                os.path.join(root_path, current_file), arcname=os.path.basename(current_file)
                            )
                shutil.rmtree(dir_path, ignore_errors=True)

                self.process_downloaded_archive(img_dir[3])

        # Torrent downloads
        if len(files_torrent) > 0:
            files_matched_torrent: list[tuple[str, bool, Archive]] = []
            logger.debug(
                "Looking for {} torrent downloaded files on folder {}.".format(
                    files_torrent, self.settings.torrent["download_dir"]
                )
            )
            for archive in files_torrent:
                if not archive.gallery:
                    continue
                
                gid_folder_path = os.path.join(self.settings.torrent["download_dir"], str(archive.gallery.gid))
                if not os.path.isdir(gid_folder_path):
                    continue

                cleaned_torrent_name = os.path.splitext(os.path.basename(archive.zipped.path))[0].replace(
                    " [" + archive.gallery.gid + "]", ""
                )

                for filename in os.listdir(gid_folder_path):
                    logger.debug(
                        "Checking if cleaned expected file name {0} is equal to found "
                        "torrent name, with replaced illegal characters (original name: {1}): {2}.".format(
                            cleaned_torrent_name, filename, replace_illegal_name(os.path.splitext(filename)[0])
                        )
                    )
                    if replace_illegal_name(os.path.splitext(filename)[0]) == cleaned_torrent_name:
                        matched_path = os.path.join(str(archive.gallery.gid), filename)
                        files_matched_torrent.append(
                            (
                                matched_path,
                                not os.path.isfile(os.path.join(self.settings.torrent["download_dir"], matched_path)),
                                archive,
                            )
                        )

            for matched_name, matched_bool, matched_archive in files_matched_torrent:
                target = os.path.join(self.settings.torrent["download_dir"], matched_name)
                if matched_bool:
                    logger.info(
                        "For archive: {archive}, creating zip for folder: {filename}".format(
                            archive=matched_archive,
                            filename=matched_name,
                        )
                    )
                    dir_path = mkdtemp(dir=self.settings.temp_directory_path)
                    for img_file in os.listdir(target):
                        if not os.path.isfile(os.path.join(target, img_file)):
                            continue
                        if mode == "local_move":
                            shutil.move(os.path.join(target, img_file), os.path.join(dir_path, img_file))
                        # Disabled since we can't hardlink to a temp filesystem
                        elif mode == "local_hardlink":
                            # os.link(os.path.join(target, img_file), os.path.join(dir_path, img_file))
                            shutil.copy(os.path.join(target, img_file), os.path.join(dir_path, img_file))
                        else:
                            shutil.copy(os.path.join(target, img_file), os.path.join(dir_path, img_file))

                    with ZipFile(matched_archive.zipped.path, "w") as archive_file:
                        for root_path, _, file_names in os.walk(dir_path):
                            for current_file in file_names:
                                archive_file.write(
                                    os.path.join(root_path, current_file), arcname=os.path.basename(current_file)
                                )
                    shutil.rmtree(dir_path, ignore_errors=True)
                else:
                    logger.info(
                        "For archive: {archive}, downloading file: {filename}".format(
                            archive=matched_archive,
                            filename=matched_name,
                        )
                    )
                    if mode == "local_move":
                        shutil.move(target, matched_archive.zipped.path)
                    elif mode == "local_hardlink":
                        os.link(target, matched_archive.zipped.path)
                    else:
                        shutil.copy(target, matched_archive.zipped.path)
                    if self.settings.convert_others_to_zip:
                        if os.path.splitext(matched_name)[1].lower() == ".rar":
                            logger.info(
                                "For archive: {}, converting rar: {} to zip".format(
                                    matched_archive, matched_archive.zipped.path
                                )
                            )
                            convert_rar_to_zip(matched_archive.zipped.path, temp_path=self.settings.temp_directory_path)
                        elif os.path.splitext(matched_name)[1].lower() == ".7z":
                            logger.info(
                                "For archive: {}, converting 7z: {} to zip".format(
                                    matched_archive, matched_archive.zipped.path
                                )
                            )
                            convert_7z_to_zip(matched_archive.zipped.path, temp_path=self.settings.temp_directory_path)

                self.process_downloaded_archive(matched_archive)

    def transfer_all_missing(self, archives: Iterable[Archive] | None = None) -> None:

        method_for_torrents = self.settings.download_handler_torrent or self.settings.download_handler
        method_for_hath = self.settings.download_handler_hath or self.settings.download_handler

        if method_for_torrents != method_for_hath and archives is not None:
            archives_from_torrents = [x for x in archives if x.match_type and "torrent" in x.match_type]
            archives_from_haths = [x for x in archives if x.match_type and "hath" in x.match_type]

            if archives_from_torrents:
                self.do_transfer_by_method(method_for_torrents, archives_from_torrents)
            if archives_from_haths:
                self.do_transfer_by_method(method_for_hath, archives_from_haths)

        else:
            self.do_transfer_by_method(method_for_torrents, archives)

    def do_transfer_by_method(self, method_to_use: str, archives: Iterable[Archive] | None = None) -> None:
        if method_to_use.startswith("local"):
            self.copy_all_missing(method_to_use, archives)
        else:
            for retry_count in range(3):
                try:
                    self.download_all_missing(archives)
                except (
                    ConnectionResetError,
                    socket.timeout,
                    TimeoutError,
                    ConnectionRefusedError,
                    ConnectionError,
                    ssl.SSLError,
                    socket.gaierror,
                    ftplib.Error,
                ) as e:
                    logger.warning(
                        "Download failed, restarting connection. Retry: {} of 3. Error: {}".format(retry_count + 1, e)
                    )
                else:
                    return
            logger.error("Download failed, restart limit reached (3), ending")

    def check_download_progress_archives[ArchiveKey](
        self, archives: Iterable[tuple[Archive, ArchiveKey]]
    ) -> dict[ArchiveKey, float]:

        progress_per_archive: dict[ArchiveKey, float] = {}

        self.start_connection(self.ftp_key_hath)

        ftp_data_hath = self.ftp_datas[self.ftp_key_hath]
        if not ftp_data_hath.ftps:
            logger.error("Cannot download the archives, the FTP connection is not initialized.")
            return progress_per_archive

        self.set_current_dir(self.ftp_key_hath, self.settings.providers["panda"].remote_hath_dir)
        # self.ftps.encoding = 'utf8'

        files_matched_hath = []
        for line in ftp_data_hath.ftps.mlsd(facts=["type"]):
            if line[1]["type"] != "dir":
                continue
            m = re.search(r".*?\[(\d+)]$", line[0])
            if m:
                for archive, key in archives:
                    if archive.gallery and archive.filesize and m.group(1) == archive.gallery.gid:
                        files_matched_hath.append((line[0], archive.zipped.path, int(archive.filesize), key))

        for matched_file_hath in files_matched_hath:
            total_remote_size = 0
            remote_ftp_tuples = []
            for img_file_tuple in ftp_data_hath.ftps.mlsd(path=matched_file_hath[0], facts=["type", "size"]):
                if img_file_tuple[1]["type"] != "file" or img_file_tuple[0] == "galleryinfo.txt":
                    continue
                total_remote_size += int(img_file_tuple[1]["size"])
                remote_ftp_tuples.append((img_file_tuple[0], img_file_tuple[1]["size"]))
            progress_per_archive[matched_file_hath[3]] = total_remote_size / matched_file_hath[2]

        return progress_per_archive


class TimedPostDownloader(BaseScheduler):

    thread_name = "post_downloader"

    def __init__(self, *args: Any, parallel_post_downloaders: int = 4, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.post_downloader: dict[int, PostDownloader] = {}
        self.post_queue: queue.Queue = queue.Queue()
        self.parallel_post_downloaders = parallel_post_downloaders

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 60

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                self.post_downloader = {}
                return

            found_archives = Archive.objects.filter_by_dl_remote()
            if found_archives:

                logger.info(
                    "Looking for missing files downloaded by hath ({:d}) and torrent ({:d}).".format(
                        len([x for x in found_archives if x.match_type and "hath" in x.match_type]),
                        len([x for x in found_archives if x.match_type and "torrent" in x.match_type]),
                    )
                )
                for archive in found_archives:
                    self.post_queue.put(archive)
                thread_array = []

                for x in range(1, self.parallel_post_downloaders + 1):
                    post_downloader = PostDownloader(self.settings, web_queue=self.web_queue)
                    self.post_downloader[x] = post_downloader
                    post_download_thread = threading.Thread(
                        name="{}-{}".format(self.thread_name, x),
                        target=self.start_post_downloader,
                        args=(post_downloader,),
                    )
                    post_download_thread.daemon = True
                    post_download_thread.start()
                    thread_array.append(post_download_thread)

                for thread in thread_array:
                    thread.join()

                self.post_downloader = {}
                logger.info("All downloader threads finished.")

            self.update_last_run(django_tz.now())

    def start_post_downloader(self, post_downloader: PostDownloader) -> None:
        while True:
            try:
                item = self.post_queue.get_nowait()
            except queue.Empty:
                return
            try:
                post_downloader.transfer_all_missing((item,))
                self.post_queue.task_done()
            except BaseException:
                logger.critical("Error downloading Archive: {}\n{}".format(item.title, traceback.format_exc()))

    def current_download(self) -> list[CurrentDownload]:
        if self.is_running_locally():
            return [x.current_download for x in self.post_downloader.values()]
        try:
            from workers.models import SchedulerState
            state = SchedulerState.objects.filter(name=self.thread_name).first()
            if not state or not state.is_running:
                return []
            if state and state.extra_data and "current_download" in state.extra_data:
                return [
                    CurrentDownload(
                        filename=item.get("filename", ""),
                        speed=float(item.get("speed", 0.0)),
                        index=int(item.get("index", 0)),
                        total=int(item.get("total", 0)),
                        downloaded=int(item.get("downloaded", 0)),
                        filesize=int(item.get("filesize", 0)),
                    )
                    for item in state.extra_data["current_download"]
                ]
        except Exception:
            pass
        return []
