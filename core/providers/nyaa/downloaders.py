import logging
import base64
import os
from typing import cast

from core.base.utilities import replace_illegal_name, available_filename
from core.downloaders.torrent import get_torrent_client
from core.providers.generic.downloaders import GenericTorrentDownloader

from . import constants, utilities

logger = logging.getLogger(__name__)


class NyaaTorrentDownloader(GenericTorrentDownloader):

    provider = constants.provider_name
    archive_only = False
    no_metadata = True

    @staticmethod
    def get_download_link(url: str) -> str:
        # We only assume there's a torrent ready, doesn't deal with crawling the nyaa torrent page go get a possible
        # magnet link
        # Start from https://sukebei.nyaa.si/view/3129346 -> to get https://sukebei.nyaa.si/download/3129346.torrent
        return utilities.download_link_from_view_link(url)

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
        if client.send_url or torrent_link.startswith("magnet:"):
            result, torrent_id = client.add_url(torrent_link, download_dir=self.settings.torrent["download_dir"])
        else:
            torrent_data = self.general_utils.get_torrent(
                torrent_link, self.own_settings.cookies, convert_to_base64=client.convert_to_base64
            )

            result, torrent_id = client.add_torrent(torrent_data, download_dir=self.settings.torrent["download_dir"])
            if client.expected_torrent_name == "":
                from core.libs.bencoding import Decoder

                try:
                    if client.convert_to_base64 and isinstance(torrent_data, str):
                        torrent_metadata = Decoder(base64.decodebytes(torrent_data.encode("utf-8"))).decode()
                    else:
                        torrent_data = cast(bytes, torrent_data)
                        torrent_metadata = Decoder(torrent_data).decode()
                    client.expected_torrent_name = os.path.splitext(torrent_metadata[b"info"][b"name"])[0]
                    client.expected_torrent_extension = os.path.splitext(torrent_metadata[b"info"][b"name"])[1]
                except (RuntimeError, EOFError):
                    self.return_code = 0
                    logger.error("Error decoding torrent data: {!r}".format(torrent_data))
                    return

        if result:
            self.download_id = torrent_id
            if client.expected_torrent_name:
                self.expected_torrent_name = "{}".format(client.expected_torrent_name)
            else:
                self.expected_torrent_name = "{}".format(replace_illegal_name(self.gallery.link))
            if client.expected_torrent_extension:
                self.expected_torrent_extension = client.expected_torrent_extension
            self.fileDownloaded = 1
            self.return_code = 1
            if client.total_size > 0:
                self.gallery.filesize = client.total_size
            else:
                self.gallery.filesize = 0

            if not self.expected_torrent_name.endswith(self.expected_torrent_extension):
                final_name = replace_illegal_name(self.expected_torrent_name) + self.expected_torrent_extension
            else:
                final_name = replace_illegal_name(self.expected_torrent_name)
            self.gallery.filename = available_filename(
                self.settings.MEDIA_ROOT, os.path.join(self.own_settings.torrent_dl_folder, final_name)
            )
            # This being a no_metadata downloader, we force the Gallery fields from the Archive
            if self.original_gallery:
                self.original_gallery.title = os.path.splitext(self.expected_torrent_name)[0]
                self.original_gallery.filesize = self.gallery.filesize
        else:
            self.return_code = 0
            logger.error(
                "There was an error adding the torrent to the client, torrent link: {}, error in client {}:".format(
                    torrent_link, client.error
                )
            )


API = (NyaaTorrentDownloader,)
