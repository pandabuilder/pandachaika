import os
import queue
import shutil
import socket
import ssl
import time
import traceback
from ftplib import FTP_TLS, _SSLSocket  # type: ignore
from tempfile import mkdtemp
from typing import Any, List, Dict, Callable, Iterable, Optional
from zipfile import ZipFile, BadZipFile

import django.utils.timezone as django_tz
import threading

import logging
from django.template.defaultfilters import filesizeformat
import re

from core.base.setup import Settings
from core.base.types import DataDict
from core.base.utilities import (
    calc_crc32, get_zip_filesize,
    filecount_in_zip, convert_rar_to_zip,
    replace_illegal_name
)
from core.workers.schedulers import BaseScheduler
from viewer.models import Archive


class PostDownloader(object):

    def __init__(self, settings: 'Settings', logger, web_queue=None) -> None:
        self.settings = settings
        self.web_queue = web_queue
        self.logger = logger
        self.ftps: Optional[FTP_TLS] = None
        self.current_ftp_dir: Optional[str] = None
        self.current_download: DataDict = {
            'filename': '',
            'blocksize': 0,
            'speed': 0,
            'index': 0,
            'total': 0,
        }

    def process_downloaded_archive(self, archive: Archive) -> None:
        if os.path.isfile(archive.zipped.path):
            except_at_open = False
            return_error = None
            try:
                my_zip = ZipFile(
                    archive.zipped.path, 'r')
                return_error = my_zip.testzip()
                my_zip.close()
            except (BadZipFile, NotImplementedError):
                except_at_open = True
            if except_at_open or return_error:
                if 'panda' in archive.source_type:
                    self.logger.error(
                        "For archive: {}, file check on downloaded zipfile failed on file: {}, "
                        "forcing download as panda_archive to fix it.".format(archive, archive.zipped.path)
                    )
                    crc32 = calc_crc32(
                        archive.zipped.path)
                    Archive.objects.add_or_update_from_values({'crc32': crc32}, pk=archive.pk)
                    if self.web_queue and archive.gallery:
                        temp_settings = Settings(load_from_config=self.settings.config)
                        temp_settings.allow_downloaders_only(['panda_archive'], True, True, True)
                        self.web_queue.enqueue_args_list((archive.gallery.get_link(),), override_options=temp_settings)
                        return
                else:
                    self.logger.warning(
                        "For archive: {}, File check on downloaded zipfile: {}. "
                        "Check the file manually.".format(archive, archive.zipped.path)
                    )
            crc32 = calc_crc32(
                archive.zipped.path)
            filesize = get_zip_filesize(
                archive.zipped.path)
            filecount = filecount_in_zip(
                archive.zipped.path)
            values = {'crc32': crc32,
                      'filesize': filesize,
                      'filecount': filecount,
                      }
            updated_archive = Archive.objects.add_or_update_from_values(
                values, pk=archive.pk)
            if archive.gallery and updated_archive.filesize != updated_archive.gallery.filesize:
                if Archive.objects.filter(gallery=updated_archive.gallery, filesize=updated_archive.gallery.filesize):
                    self.logger.info(
                        "For archive: {} size does not match gallery, "
                        "but there's already another archive that matches.".format(updated_archive)
                    )
                    return
                if 'panda' in archive.source_type:
                    self.logger.info(
                        "For archive: [} size does not match gallery, "
                        "downloading again from panda_archive.".format(updated_archive)
                    )
                    if self.web_queue:
                        temp_settings = Settings(load_from_config=self.settings.config)
                        temp_settings.allow_downloaders_only(['panda_archive'], True, True, True)
                        self.web_queue.enqueue_args_list(
                            (updated_archive.gallery.get_link(), ),
                            override_options=temp_settings
                        )
                else:
                    self.logger.warning(
                        "For archive: {} size does not match gallery. Check the file manually.".format(archive)
                    )

    def write_file_update_progress(self, cmd: str, callback: Callable, filesize: int = 0, blocksize: int = 8192, rest: bool = None) -> str:
        self.ftps.voidcmd('TYPE I')  # type: ignore
        with self.ftps.transfercmd(cmd, rest) as conn:  # type: ignore
            self.current_download['filesize'] = filesize
            self.current_download['downloaded'] = 0
            self.current_download['filename'] = cmd.replace('RETR ', '')
            start = time.clock()
            while 1:
                data = conn.recv(blocksize)
                if not data:
                    break
                downloaded = len(data)
                self.current_download['downloaded'] += downloaded
                current = time.clock()
                if current > start:
                    self.current_download['speed'] = self.current_download['downloaded'] / ((current - start) * 1024)
                callback(data)
            self.current_download['filename'] = ''
            self.current_download['speed'] = 0
            self.current_download['filesize'] = 0
            # shutdown ssl layer
            if _SSLSocket is not None and isinstance(conn, _SSLSocket):
                conn.unwrap()
        return self.ftps.voidresp()  # type: ignore

    def start_connection(self) -> None:
        if self.settings.ftps['no_certificate_check']:
            context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            context.verify_mode = ssl.CERT_NONE
            context.check_hostname = False
        else:
            context = ssl.create_default_context()
        self.ftps = FTP_TLS(
            host=self.settings.ftps['address'],
            user=self.settings.ftps['user'],
            passwd=self.settings.ftps['passwd'],
            context=context,
            source_address=self.settings.ftps['source_address'],
            timeout=self.settings.timeout_timer
        )

        # Hath downloads
        self.ftps.prot_p()

    def set_current_dir(self, self_dir: str) -> None:
        self.current_ftp_dir = self_dir
        if not self.ftps:
            return None
        self.ftps.cwd(self_dir)

    def download_all_missing(self, archives: Iterable[Archive] = None) -> None:

        files_torrent = []
        files_hath = []

        if not archives:
            found_archives: Iterable[Archive] = list(Archive.objects.filter_by_dl_remote())
        else:
            found_archives = archives

        if not found_archives:
            return

        for archive in found_archives:
            if 'torrent' in archive.match_type:
                files_torrent.append(archive)
            elif 'hath' in archive.match_type:
                files_hath.append(archive)

        if len(files_torrent) + len(files_hath) == 0:
            return

        self.start_connection()

        if not self.ftps:
            self.logger.error(
                "Cannot download the archives, the FTP connection is not initialized."
            )
            return None

        # Hath downloads
        if len(files_hath) > 0:
            self.set_current_dir(self.settings.providers['panda'].remote_hath_dir)
            # self.ftps.encoding = 'utf8'

            files_matched_hath = []
            for line in self.ftps.mlsd(facts=["type"]):
                if line[1]["type"] != 'dir':
                    continue
                m = re.search(r'.*?\[(\d+)\]$', line[0])
                if m:
                    for archive in files_hath:
                        if m.group(1) == archive.gallery.gid:
                            files_matched_hath.append(
                                (line[0], archive.zipped.path, int(archive.filesize), archive))

            for matched_file_hath in files_matched_hath:
                total_remote_size = 0
                remote_ftp_tuples = []
                for img_file_tuple in self.ftps.mlsd(path=matched_file_hath[0], facts=["type", "size"]):
                    if img_file_tuple[1]["type"] != 'file' or img_file_tuple[0] == 'galleryinfo.txt':
                        continue
                    total_remote_size += int(img_file_tuple[1]["size"])
                    remote_ftp_tuples.append((img_file_tuple[0], img_file_tuple[1]["size"]))
                if total_remote_size != matched_file_hath[2]:
                    self.logger.info(
                        "For archive: {archive}, remote folder: {folder} "
                        "has not completed the download ({current}/{total}), skipping".format(
                            archive=matched_file_hath[3],
                            folder=matched_file_hath[0],
                            current=filesizeformat(total_remote_size),
                            total=filesizeformat(matched_file_hath[2])
                        )
                    )
                    continue
                self.logger.info(
                    "For archive: {archive}, downloading and creating zip "
                    "for folder {filename}, {image_count} images".format(
                        archive=matched_file_hath[3],
                        filename=matched_file_hath[1],
                        image_count=len(remote_ftp_tuples)
                    ))
                dir_path = mkdtemp()
                self.current_download['total'] = len(remote_ftp_tuples)
                for count, remote_file in enumerate(sorted(remote_ftp_tuples), start=1):
                    for retry_count in range(10):
                        try:
                            with open(os.path.join(dir_path, remote_file[0]), "wb") as file:
                                self.current_download['index'] = count
                                self.write_file_update_progress(
                                    'RETR %s' % (str(matched_file_hath[0]) + "/" + remote_file[0]),
                                    file.write,
                                    int(remote_file[1])
                                )
                        except (ConnectionResetError, socket.timeout, TimeoutError):
                            self.logger.error("Hath download failed for file {} of {}, restarting connection...".format(
                                count,
                                len(remote_ftp_tuples))
                            )
                            self.ftps.close()
                            self.start_connection()
                            self.set_current_dir(self.settings.providers['panda'].remote_hath_dir)
                        else:
                            break
                with ZipFile(os.path.join(self.settings.MEDIA_ROOT,
                                          matched_file_hath[1]),
                             'w') as archive_file:
                    for (root_path, _, file_names) in os.walk(dir_path):
                        for current_file in file_names:
                            archive_file.write(
                                os.path.join(root_path, current_file), arcname=os.path.basename(current_file))
                shutil.rmtree(dir_path, ignore_errors=True)

                self.process_downloaded_archive(matched_file_hath[3])

        # Torrent downloads
        if len(files_torrent) > 0:
            self.set_current_dir(self.settings.ftps['remote_torrent_dir'])
            self.ftps.encoding = 'utf8'
            files_matched_torrent = []
            for line in self.ftps.mlsd(facts=["type", "size"]):
                if not line[0]:
                    continue
                if 'type' not in line[1]:
                    continue
                if line[1]["type"] != 'dir' and line[1]["type"] != 'file':
                    continue
                for archive in files_torrent:
                    if archive.gallery:
                        cleaned_torrent_name = os.path.splitext(
                            os.path.basename(archive.zipped.path))[0].replace(' [' + archive.gallery.gid + ']', '')
                    else:
                        cleaned_torrent_name = os.path.splitext(os.path.basename(archive.zipped.path))[0]
                    if replace_illegal_name(os.path.splitext(line[0])[0]) in cleaned_torrent_name:
                        if line[1]["type"] == 'dir':
                            files_matched_torrent.append((line[0], line[1]["type"], 0, archive))
                        else:
                            files_matched_torrent.append((line[0], line[1]["type"], int(line[1]["size"]), archive))
            for matched_file_torrent in files_matched_torrent:
                if matched_file_torrent[1] == 'dir':
                    dir_path = mkdtemp()
                    remote_ftp_files = list(self.ftps.mlsd(path=matched_file_torrent[0], facts=["type", "size"]))
                    self.current_download['total'] = len(remote_ftp_files)
                    self.logger.info(
                        "For archive: {archive}, downloading and creating zip "
                        "for folder {filename}, {image_count} images".format(
                            archive=matched_file_torrent[3],
                            filename=matched_file_torrent[0],
                            image_count=len(remote_ftp_files)
                        ))
                    for count, img_file_tuple in enumerate(remote_ftp_files):
                        if img_file_tuple[1]["type"] != 'file':
                            continue
                        for retry_count in range(10):
                            try:
                                with open(os.path.join(dir_path, img_file_tuple[0]), "wb") as file:
                                    self.current_download['index'] = count
                                    self.write_file_update_progress(
                                        'RETR %s' % (str(matched_file_torrent[0]) + "/" + img_file_tuple[0]),
                                        file.write,
                                        int(img_file_tuple[1]["size"])
                                    )
                            except (ConnectionResetError, socket.timeout, TimeoutError):
                                self.logger.error("Torrent download failed for folder, restarting connection...")
                                self.ftps.close()
                                self.start_connection()
                                self.set_current_dir(self.settings.ftps['remote_torrent_dir'])
                            else:
                                break
                    with ZipFile(matched_file_torrent[3].zipped.path, 'w') as archive_file:
                        for (root_path, _, file_names) in os.walk(dir_path):
                            for current_file in file_names:
                                archive_file.write(
                                    os.path.join(root_path, current_file), arcname=os.path.basename(current_file))
                    shutil.rmtree(dir_path, ignore_errors=True)
                else:
                    self.logger.info(
                        "For archive: {archive} downloading remote file: {remote} to local file: {local}".format(
                            archive=matched_file_torrent[3],
                            remote=matched_file_torrent[0],
                            local=matched_file_torrent[3].zipped.path
                        )
                    )
                    self.current_download['total'] = 1
                    for retry_count in range(10):
                        try:
                            with open(matched_file_torrent[3].zipped.path, "wb") as file:
                                self.current_download['index'] = 1
                                self.write_file_update_progress(
                                    'RETR %s' % matched_file_torrent[0], file.write, matched_file_torrent[2])
                        except (ConnectionResetError, socket.timeout, TimeoutError):
                            self.logger.error("Torrent download failed for archive, restarting connection...")
                            self.ftps.close()
                            self.start_connection()
                            self.set_current_dir(self.settings.ftps['remote_torrent_dir'])
                        else:
                            break
                    if self.settings.convert_rar_to_zip and os.path.splitext(matched_file_torrent[0])[1].lower() == ".rar":
                        self.logger.info(
                            "For archive: {}, converting rar: {} to zip".format(
                                matched_file_torrent[3],
                                matched_file_torrent[3].zipped.path
                            )
                        )
                        convert_rar_to_zip(matched_file_torrent[3].zipped.path)

                self.process_downloaded_archive(matched_file_torrent[3])

        self.ftps.close()

    def copy_all_missing(self, mode, archives: Iterable[Archive] = None):
        files_torrent = []
        files_hath = []

        if not archives:
            found_archives: Iterable[Archive] = list(Archive.objects.filter_by_dl_remote())
        else:
            found_archives = archives

        if not found_archives:
            return

        for archive in found_archives:
            if not os.path.isfile(archive.zipped.path):
                if 'torrent' in archive.match_type:
                    files_torrent.append(archive)
                elif 'hath' in archive.match_type:
                    files_hath.append(archive)

        if len(files_torrent) + len(files_hath) == 0:
            return

        # Hath downloads
        if len(files_hath) > 0:
            files_matched_hath = []
            for matched_file in os.listdir(self.settings.providers['panda'].local_hath_folder):
                if os.path.isfile(os.path.join(self.settings.providers['panda'].local_hath_folder, matched_file)):
                    continue
                m = re.search(r'.*?\[(\d+)\]$', matched_file)
                if m:
                    for archive in files_hath:
                        if m.group(1) == archive.gallery.gid:
                            files_matched_hath.append(
                                [matched_file, archive.zipped.path, int(archive.filesize), archive])

            for img_dir in files_matched_hath:
                total_remote_size = 0
                remote_files = []
                directory = os.path.join(self.settings.providers['panda'].local_hath_folder, img_dir[0])
                for img_file in os.listdir(directory):
                    if not os.path.isfile(os.path.join(directory, img_file)) or img_file == 'galleryinfo.txt':
                        continue
                    total_remote_size += os.stat(
                        os.path.join(directory, img_file)).st_size
                    remote_files.append(
                        os.path.join(directory, img_file))
                if total_remote_size != img_dir[2]:
                    self.logger.info(
                        "For archive: {archive}, folder: {folder} "
                        "has not completed the download ({current}/{total}), skipping".format(
                            archive=img_dir[3],
                            folder=img_dir[0],
                            current=filesizeformat(total_remote_size),
                            total=filesizeformat(img_dir[2])
                        )
                    )
                    continue
                self.logger.info(
                    "For archive: {archive}, creating zip "
                    "for folder {filename}, {image_count} images".format(
                        archive=img_dir[3],
                        filename=img_dir[1],
                        image_count=len(remote_files)
                    ))
                dir_path = mkdtemp()
                for img_file_original in remote_files:
                    img_file = os.path.split(img_file_original)[1]
                    if mode == 'local_move':
                        shutil.move(img_file_original, os.path.join(dir_path, img_file))
                    else:
                        shutil.copy(img_file_original, os.path.join(dir_path, img_file))
                with ZipFile(os.path.join(self.settings.MEDIA_ROOT,
                                          img_dir[1]),
                             'w') as archive_file:
                    for (root_path, _, file_names) in os.walk(dir_path):
                        for current_file in file_names:
                            archive_file.write(
                                os.path.join(root_path, current_file), arcname=os.path.basename(current_file))
                shutil.rmtree(dir_path, ignore_errors=True)

                self.process_downloaded_archive(img_dir[3])

        # Torrent downloads
        if len(files_torrent) > 0:
            files_matched_torrent = []
            for filename in os.listdir(self.settings.torrent['download_dir']):
                for archive in files_torrent:
                    if archive.gallery:
                        cleaned_torrent_name = os.path.splitext(
                            os.path.basename(archive.zipped.path))[0].replace(' [' + archive.gallery.gid + ']', '')
                    else:
                        cleaned_torrent_name = os.path.splitext(os.path.basename(archive.zipped.path))[0]
                    if replace_illegal_name(os.path.splitext(filename)[0]) in cleaned_torrent_name:
                        files_matched_torrent.append([filename, not os.path.isfile(
                            os.path.join(self.settings.torrent['download_dir'], filename)), archive])

            for matched_file in files_matched_torrent:
                target = os.path.join(self.settings.torrent['download_dir'], matched_file[0])
                if matched_file[1]:
                    self.logger.info(
                        "For archive: {archive}, creating zip for folder: {filename}".format(
                            archive=matched_file[2],
                            filename=matched_file[0],
                        ))
                    dir_path = mkdtemp()
                    for img_file in os.listdir(target):
                        if not os.path.isfile(os.path.join(target, img_file)):
                            continue
                        if mode == 'local_move':
                            shutil.move(os.path.join(target, img_file), os.path.join(dir_path, img_file))
                        else:
                            shutil.copy(os.path.join(target, img_file), os.path.join(dir_path, img_file))

                    with ZipFile(matched_file[2].zipped.path, 'w') as archive_file:
                        for (root_path, _, file_names) in os.walk(dir_path):
                            for current_file in file_names:
                                archive_file.write(
                                    os.path.join(root_path, current_file), arcname=os.path.basename(current_file))
                    shutil.rmtree(dir_path, ignore_errors=True)
                else:
                    self.logger.info(
                        "For archive: {archive}, downloading file: {filename}".format(
                            archive=matched_file[2],
                            filename=matched_file[0],
                        ))
                    if mode == 'local_move':
                        shutil.move(target, matched_file[2].zipped.path)
                    else:
                        shutil.copy(target, matched_file[2].zipped.path)
                    if self.settings.convert_rar_to_zip and os.path.splitext(matched_file[0])[1].lower() == ".rar":
                        self.logger.info(
                            "For archive: {}, converting rar: {} to zip".format(
                                matched_file[2],
                                matched_file[2].zipped.path
                            )
                        )
                        convert_rar_to_zip(matched_file[2].zipped.path)

                self.process_downloaded_archive(matched_file[2])

    def transfer_all_missing(self, archives: Iterable[Archive] = None) -> None:

        if self.settings.download_handler.startswith('local'):
            self.copy_all_missing(self.settings.download_handler, archives)
        else:
            for retry_count in range(3):
                try:
                    self.download_all_missing(archives)
                except (ConnectionResetError, socket.timeout, TimeoutError):
                    self.logger.error("Download failed, restarting connection. Retry: {} of 3".format(retry_count + 1))
                else:
                    return
            self.logger.error("Download failed, restart limit reached (3), ending")


class TimedPostDownloader(BaseScheduler):

    thread_name = 'post_downloader'

    def __init__(self, *args: Any, parallel_post_downloaders: int = 4, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.post_downloader: Dict[int, PostDownloader] = {}
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

                if self.crawler_logger:
                    self.crawler_logger.info(
                        "Looking for missing files downloaded by hath ({:d}) and torrent ({:d}).".format(
                            len([x for x in found_archives if 'hath' in x.match_type]),
                            len([x for x in found_archives if 'torrent' in x.match_type]),
                        )
                    )
                for archive in found_archives:
                    self.post_queue.put(archive)
                thread_array = []

                for x in range(1, self.parallel_post_downloaders + 1):
                    post_downloader = PostDownloader(self.settings, self.crawler_logger, web_queue=self.web_queue)
                    self.post_downloader[x] = post_downloader
                    post_download_thread = threading.Thread(
                        name="{}-{}".format(self.thread_name, x),
                        target=self.start_post_downloader,
                        args=(post_downloader, )
                    )
                    post_download_thread.daemon = True
                    post_download_thread.start()
                    thread_array.append(post_download_thread)

                for thread in thread_array:
                    thread.join()

                self.post_downloader = {}
                if self.crawler_logger:
                    self.crawler_logger.info(
                        "All downloader threads finished."
                    )

            self.update_last_run(django_tz.now())

    def start_post_downloader(self, post_downloader: PostDownloader) -> None:
        while True:
            try:
                item = self.post_queue.get_nowait()
            except queue.Empty:
                return
            try:
                post_downloader.transfer_all_missing((item, ))
                self.post_queue.task_done()
            except BaseException:
                thread_logger = logging.getLogger('viewer.threads')
                thread_logger.error(traceback.format_exc())

    def current_download(self) -> List[Dict[str, Any]]:
        return [x.current_download for x in self.post_downloader.values()]
