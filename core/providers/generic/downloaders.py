import logging
import os
from typing import Any, Optional

import requests

from core.base.types import DataDict
from core.base.utilities import replace_illegal_name, available_filename, get_filename_from_cd, get_zip_fileinfo, \
    calc_crc32, construct_request_dict
from core.downloaders.torrent import get_torrent_client

from core.downloaders.handlers import BaseDownloader
from viewer.models import Archive

logger = logging.getLogger(__name__)


class GenericTorrentDownloader(BaseDownloader):

    type = 'torrent'
    provider = 'generic'
    archive_only = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ''

    @staticmethod
    def get_download_link(url: str) -> str:
        return url

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.link:
            return

        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            logger.error("No torrent client was found")
            return

        torrent_link = self.get_download_link(self.gallery.link)

        logger.info("Adding torrent to client.")
        client.connect()
        if client.send_url or torrent_link.startswith('magnet:'):
            result = client.add_url(
                torrent_link,
                download_dir=self.settings.torrent['download_dir']
            )
        else:
            torrent_data = self.general_utils.get_torrent(
                torrent_link,
                self.own_settings.cookies,
                convert_to_base64=client.convert_to_base64
            )

            result = client.add_torrent(
                torrent_data,
                download_dir=self.settings.torrent['download_dir']
            )
            if client.expected_torrent_name == '':
                from core.libs.bencoding import Decoder
                torrent_metadata = Decoder(torrent_data).decode()
                client.expected_torrent_name = os.path.splitext(torrent_metadata[b'info'][b'name'])[0]

        if result:
            if client.expected_torrent_name:
                self.expected_torrent_name = "{}".format(client.expected_torrent_name)
            else:
                self.expected_torrent_name = "{}".format(
                    replace_illegal_name(self.gallery.link)
                )
            self.fileDownloaded = 1
            self.return_code = 1
            if client.total_size > 0:
                self.gallery.filesize = client.total_size
            else:
                self.gallery.filesize = 0
            self.gallery.filename = available_filename(
                self.settings.MEDIA_ROOT,
                os.path.join(
                    self.own_settings.torrent_dl_folder,
                    replace_illegal_name(self.expected_torrent_name) + '.zip'
                )
            )
        else:
            self.return_code = 0
            logger.error("There was an error adding the torrent to the client")

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:

        if not self.gallery:
            return None

        values = {
            'title': self.expected_torrent_name,
            'title_jpn': '',
            'zipped': self.gallery.filename,
            'crc32': self.crc32,
            'filesize': self.gallery.filesize,
            'filecount': 0,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            None,
            zipped=self.gallery.filename
        )


class GenericArchiveDownloader(BaseDownloader):

    type = 'archive'
    provider = 'generic'
    archive_only = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    @staticmethod
    def get_download_link(url: str) -> str:
        return url

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.link:
            return

        logger.info("Downloading an archive from a generic HTTP server: {}".format(
            self.gallery.link
        ))

        request_dict = construct_request_dict(self.settings, self.own_settings)

        request_file = requests.get(
            self.gallery.link,
            stream='True',
            **request_dict
        )

        filename = get_filename_from_cd(request_file.headers.get('content-disposition'))

        if not filename:
            if self.gallery.link.find('/'):
                filename = self.gallery.link.rsplit('/', 1)[1]

        if not filename:
            logger.error("Could not find a filename for link: {}".format(
                self.gallery.link
            ))
            self.return_code = 0

        self.gallery.title = filename.replace(".zip", "")
        self.gallery.filename = replace_illegal_name(available_filename(
            self.settings.MEDIA_ROOT,
            os.path.join(
                self.own_settings.archive_dl_folder,
                filename)))

        filepath = os.path.join(self.settings.MEDIA_ROOT,
                                self.gallery.filename)
        with open(filepath, 'wb') as fo:
            for chunk in request_file.iter_content(4096):
                fo.write(chunk)

        self.gallery.filesize, self.gallery.filecount = get_zip_fileinfo(filepath)
        if self.gallery.filesize > 0:
            self.crc32 = calc_crc32(filepath)

            self.fileDownloaded = 1
            self.return_code = 1

        else:
            logger.error("Could not download archive")
            self.return_code = 0

    def update_archive_db(self, default_values: DataDict) -> Optional['Archive']:

        if not self.gallery:
            return None

        values = {
            'title': self.gallery.title,
            'title_jpn': self.gallery.title_jpn,
            'zipped': self.gallery.filename,
            'crc32': self.crc32,
            'filesize': self.gallery.filesize,
            'filecount': self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            self.gallery.gid,
            zipped=self.gallery.filename
        )


API = (
    GenericTorrentDownloader,
    GenericArchiveDownloader,
)
