import os
from typing import Any

from core.base.types import DataDict
from core.base.utilities import replace_illegal_name, available_filename
from core.downloaders.torrent import get_torrent_client

from core.downloaders.handlers import BaseDownloader
from viewer.models import Archive


class GenericTorrentDownloader(BaseDownloader):

    type = 'torrent'
    provider = 'generic'
    archive_only = True

    @staticmethod
    def get_download_link(url: str) -> str:
        return url

    def start_download(self) -> None:
        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            self.logger.error("No torrent client was found")
            return

        torrent_link = self.get_download_link(self.gallery.link)

        self.logger.info("Adding torrent to client.")
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
            self.logger.error("There was an error adding the torrent to the client")

    def update_archive_db(self, default_values: DataDict) -> Archive:

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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ''


API = (
    GenericTorrentDownloader,
)
