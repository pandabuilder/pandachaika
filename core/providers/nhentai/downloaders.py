from typing import Any, Optional
from urllib.parse import urljoin

from core.base.types import DataDict
from core.downloaders.handlers import BaseInfoDownloader, BaseTorrentDownloader
from core.downloaders.torrent import get_torrent_client
from viewer.models import Archive
from . import constants


class TorrentDownloader(BaseTorrentDownloader):

    provider = constants.provider_name

    @staticmethod
    def get_download_link(url: str) -> str:
        return urljoin(url, 'download')

    def start_download(self) -> None:

        if not self.gallery or not self.gallery.link:
            return

        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            self.logger.error("No torrent client was found")
            return

        if not self.gallery.link:
            self.return_code = 0
            self.logger.error("No link on gallery")
            return

        torrent_link = self.get_download_link(self.gallery.link)

        self.logger.info("Adding torrent to client.")
        self.connect_and_download(client, torrent_link)

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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ''


class InfoDownloader(BaseInfoDownloader):

    provider = constants.provider_name


API = (
    TorrentDownloader,
    InfoDownloader,
)
