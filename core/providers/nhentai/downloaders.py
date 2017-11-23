import os
from urllib.parse import urljoin

from core.downloaders.handlers import BaseDownloader, BaseInfoDownloader
from core.downloaders.torrent import get_torrent_client
from viewer.models import Archive
from core.base.utilities import replace_illegal_name
from . import constants


class TorrentDownloader(BaseDownloader):

    type = 'torrent'
    provider = constants.provider_name

    @staticmethod
    def get_download_link(url):
        return urljoin(url, 'download')

    def start_download(self):
        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            self.logger.error("No torrent client was found")
            return

        torrent_link = self.get_download_link(self.gallery['link'])

        self.logger.info("Adding torrent to client.")
        client.connect()
        if client.send_url:
            result = client.add_url(
                torrent_link,
                download_dir=self.settings.torrent['download_dir']
            )
        else:
            result = client.add_torrent(
                self.general_utils.get_torrent(
                    torrent_link,
                    self.own_settings.cookies,
                    convert_to_base64=client.convert_to_base64
                ),
                download_dir=self.settings.torrent['download_dir']
            )
        if result:
            if client.expected_torrent_name:
                self.expected_torrent_name = "{} [{}]".format(client.expected_torrent_name, self.gallery['gid'])
            else:
                self.expected_torrent_name = "{} [{}]".format(
                    replace_illegal_name(self.gallery['title']), self.gallery['gid']
                )
            self.fileDownloaded = 1
            self.return_code = 1
            if client.total_size > 0:
                self.gallery['filesize'] = client.total_size
            self.gallery['filename'] = os.path.join(
                self.own_settings.torrent_dl_folder,
                replace_illegal_name(self.expected_torrent_name) + '.zip'
            )
        else:
            self.return_code = 0
            self.logger.error("There was an error adding the torrent to the client")

    def update_archive_db(self, default_values):

        values = {
            'title': self.gallery['title'],
            'title_jpn': self.gallery['title_jpn'],
            'zipped': self.gallery['filename'],
            'crc32': self.crc32,
            'filesize': self.gallery['filesize'],
            'filecount': self.gallery['filecount'],
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values,
            self.gallery['gid'],
            zipped=self.gallery['filename']
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected_torrent_name = ''


class InfoDownloader(BaseInfoDownloader):

    provider = constants.provider_name


API = (
    TorrentDownloader,
    InfoDownloader,
)
