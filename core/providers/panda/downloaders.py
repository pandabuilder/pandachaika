import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional
import html

import requests
from bs4 import BeautifulSoup

from core.base.types import DataDict
from core.base.utilities import (
    calc_crc32,
    request_with_retries,
    get_base_filename_string_from_gallery_data,
    get_zip_fileinfo_for_gallery,
    construct_request_dict,
)
from core.downloaders.handlers import BaseDownloader, BaseInfoDownloader, BaseFakeDownloader, BaseTorrentDownloader
from core.downloaders.torrent import get_torrent_client
from core.providers.panda.utilities import TorrentHTMLParser, get_archive_link_from_html_page
from viewer.models import Archive
from core.base.utilities import available_filename, replace_illegal_name
from . import constants

logger = logging.getLogger(__name__)


class ArchiveDownloader(BaseDownloader):

    type = "archive"
    provider = constants.provider_name

    def request_archive_download(self, root: str, gid: str, token: str) -> Optional[requests.models.Response]:

        url = root + "/archiver.php"

        params = {"gid": gid, "token": token}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = params
        request_dict["data"] = constants.archive_download_data

        response = request_with_retries(
            url,
            request_dict,
            post=True,
        )

        return response

    def start_download(self) -> None:

        if not self.gallery:
            return

        to_use_filename = get_base_filename_string_from_gallery_data(self.gallery)

        to_use_filename = replace_illegal_name(to_use_filename)

        self.gallery.filename = available_filename(
            self.settings.MEDIA_ROOT, os.path.join(self.own_settings.archive_dl_folder, to_use_filename + ".zip")
        )

        if not (self.gallery.root and self.gallery.gid and self.gallery.token and self.gallery):
            logger.error(
                "Missing required data -> root: {}, gid: {}, token: {}.".format(
                    self.gallery.root, self.gallery.gid, self.gallery.token
                )
            )
            self.return_code = 0
            return

        r = self.request_archive_download(self.gallery.root, self.gallery.gid, self.gallery.token)

        if not r:
            logger.error("Could not get download link.")
            self.return_code = 0
            return

        r.encoding = "utf-8"

        if "Invalid archiver key" in r.text:
            logger.error("Invalid archiver key received.")
            self.return_code = 0
        else:

            archive_link = get_archive_link_from_html_page(r.text)

            if archive_link == "":
                logger.error("Could not find archive link, page text: {}".format(r.text))
                self.return_code = 0
            else:
                m = re.match(r"(.*?)(\?.*?)", archive_link)
                if m:
                    archive_link = m.group(1)

                logger.info("Got link: {}, from url: {}".format(archive_link, r.url))

                request_dict = construct_request_dict(self.settings, self.own_settings)
                request_dict['stream'] = True

                request_file = request_with_retries(archive_link + "?start=1", request_dict)

                if r and r.status_code == 200 and request_file:
                    logger.info("Downloading gallery: {}.zip".format(to_use_filename))
                    filepath = os.path.join(self.settings.MEDIA_ROOT, self.gallery.filename)
                    total_size = int(request_file.headers.get("Content-Length", 0))
                    self.download_event = self.create_download_event(
                        self.gallery.link, self.type, filepath, total_size=total_size
                    )
                    with open(filepath, "wb") as fo:
                        for chunk in request_file.iter_content(4096):
                            fo.write(chunk)

                    self.gallery.filesize, self.gallery.filecount = get_zip_fileinfo_for_gallery(filepath)
                    if self.gallery.filesize > 0:
                        self.crc32 = calc_crc32(filepath)

                        self.fileDownloaded = 1
                        self.return_code = 1

                else:
                    logger.error("Could not download archive")
                    self.return_code = 0

    def update_archive_db(self, default_values: DataDict) -> Optional["Archive"]:

        if not self.gallery:
            return None

        values = {
            "title": self.gallery.title,
            "title_jpn": self.gallery.title_jpn,
            "zipped": self.gallery.filename,
            "crc32": self.crc32,
            "filesize": self.gallery.filesize,
            "filecount": self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values, (self.gallery.gid, self.gallery.provider), zipped=self.gallery.filename
        )


class TorrentDownloader(BaseTorrentDownloader):

    provider = constants.provider_name

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def request_torrent_download(self, root: str, gid: str, token: str) -> Optional[requests.models.Response]:

        url = root + "/gallerytorrents.php"

        params = {"gid": gid, "t": token}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = params

        response = request_with_retries(
            url,
            request_dict,
            post=True,
        )

        return response

    @staticmethod
    def validate_torrent(
        torrent_link: str, seeds: int, posted_date: str, gallery_posted_date: Optional[datetime]
    ) -> tuple[bool, list[str]]:
        validated = True
        reasons = []
        if not torrent_link:
            validated = False
            reasons.append("Invalid URL.")
        else:
            if seeds <= 0:
                validated = False
                reasons.append("Less than 1 seed.")
            if not posted_date or not gallery_posted_date:
                validated = False
                reasons.append("Did not get a correct posted time.")
            else:
                parsed_posted_date = datetime.strptime(posted_date, "%Y-%m-%d %H:%M %z")
                if parsed_posted_date < gallery_posted_date:
                    validated = False
                    reasons.append("Posted before gallery posted time.")
        return validated, reasons

    def start_download(self) -> None:

        if not self.gallery:
            return

        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            logger.error("No torrent client was found")
            return

        if not (self.gallery.root and self.gallery.gid and self.gallery.token):
            logger.error(
                "Missing required data -> root: {}, gid: {}, token: {}.".format(
                    self.gallery.root,
                    self.gallery.gid,
                    self.gallery.token,
                )
            )
            self.return_code = 0
            return

        r = self.request_torrent_download(self.gallery.root, self.gallery.gid, self.gallery.token)

        if not r:
            logger.error("Could not get download link.")
            self.return_code = 0
            return

        torrent_page_parser = TorrentHTMLParser()
        torrent_page_parser.feed(r.text)

        torrent_link = torrent_page_parser.torrent

        if not torrent_link:
            logger.error("Could not get torrent link.")
            self.return_code = 0
            return

        validated, reasons = self.validate_torrent(
            torrent_link, torrent_page_parser.seeds, torrent_page_parser.posted_date, self.gallery.posted
        )

        if not validated:
            logger.error(
                "Torrent for gallery: {} for did not pass validation, reasons: {}"
                ", skipping.".format(self.gallery.link, " ".join(reasons))
            )
            self.return_code = 0
            return

        m = re.match(r"(.*?)(\?p=\d+)", torrent_link)
        if m and m.group(1):
            torrent_link = m.group(1)

        logger.info("Adding torrent to client, seeds: {}".format(torrent_page_parser.seeds))
        self.connect_and_download(client, torrent_link)

    def update_archive_db(self, default_values: DataDict) -> Optional["Archive"]:

        if not self.gallery:
            return None

        values = {
            "title": self.gallery.title,
            "title_jpn": self.gallery.title_jpn,
            "zipped": self.gallery.filename,
            "crc32": self.crc32,
            "filesize": self.gallery.filesize,
            "filecount": self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values, (self.gallery.gid, self.gallery.provider), zipped=self.gallery.filename
        )


class TorrentAPIDownloader(BaseTorrentDownloader):

    type = "torrent_api"
    provider = constants.provider_name
    mark_hidden_if_last = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    def choose_torrent(self, torrents: list[dict]) -> Optional[dict]:

        if not self.gallery:
            return None

        chosen_torrent_date: Optional[dict] = None
        chosen_torrent_size: Optional[dict] = None
        chosen_size_difference: int = -1
        chosen_date_difference: timedelta = timedelta.max

        for torrent_info in torrents:
            if torrent_info["added"] and self.gallery.posted is not None:
                if datetime.fromtimestamp(int(torrent_info["added"]), timezone.utc) < self.gallery.posted:
                    continue
                date_difference = datetime.fromtimestamp(int(torrent_info["added"]), timezone.utc) - self.gallery.posted
                if date_difference < chosen_date_difference:
                    chosen_torrent_date = torrent_info
                    chosen_date_difference = date_difference
            if torrent_info["fsize"] and self.gallery.filesize is not None:
                size_different = abs(int(torrent_info["fsize"]) - self.gallery.filesize)
                if chosen_size_difference == -1 or size_different < chosen_size_difference:
                    chosen_torrent_size = torrent_info
                    chosen_size_difference = size_different

        if chosen_torrent_size is not None and chosen_torrent_date is not None:
            if chosen_torrent_size["hash"] == chosen_torrent_date["hash"]:
                return chosen_torrent_size
            else:
                return chosen_torrent_size
        elif chosen_torrent_size is not None:
            return chosen_torrent_size
        else:
            return chosen_torrent_date

    @staticmethod
    def format_torrent_magnet_url(torrent_hash: str) -> str:
        torrent_magnet = "magnet:?xt=urn:btih:{}&tr={}".format(torrent_hash, constants.ge_torrent_tracker_announce)
        return torrent_magnet

    def start_download(self) -> None:

        if not self.gallery:
            return

        if "torrentcount" not in self.gallery.extra_data or "torrents" not in self.gallery.extra_data:
            logger.error("Missing required extra data -> torrentcount, torrents.")
            self.return_code = 0
            return

        if int(self.gallery.extra_data["torrentcount"]) <= 0:
            logger.error("No torrents reported in API response.")
            self.return_code = 0
            return

        client = get_torrent_client(self.settings.torrent)
        if not client:
            self.return_code = 0
            logger.error("No torrent client was found")
            return

        chosen_torrent = self.choose_torrent(self.gallery.extra_data["torrents"])

        if not chosen_torrent:
            self.return_code = 0
            logger.error(
                "Could not find a suitable torrent from {}.".format(str(len(self.gallery.extra_data["torrents"])))
            )
            return

        client.send_url = True
        client.set_expected = False

        torrent_magnet_url = self.format_torrent_magnet_url(chosen_torrent["hash"])
        client.expected_torrent_name = html.unescape(chosen_torrent["name"])

        logger.info(
            "Adding torrent to client, magnet link: {}, filesize: {}, date: {}, expected name: {}".format(
                torrent_magnet_url,
                chosen_torrent["fsize"],
                datetime.fromtimestamp(int(chosen_torrent["added"]), timezone.utc),
                html.unescape(chosen_torrent["name"]),
            )
        )
        self.connect_and_download(client, torrent_magnet_url)

    def update_archive_db(self, default_values: DataDict) -> Optional["Archive"]:

        if not self.gallery:
            return None

        values = {
            "title": self.gallery.title,
            "title_jpn": self.gallery.title_jpn,
            "zipped": self.gallery.filename,
            "crc32": self.crc32,
            "filesize": self.gallery.filesize,
            "filecount": self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values, (self.gallery.gid, self.gallery.provider), zipped=self.gallery.filename
        )


class HathDownloader(BaseDownloader):

    type = "hath"
    provider = constants.provider_name
    direct_downloader = False

    def start_download(self) -> None:

        if not self.gallery:
            return

        if not (self.gallery.root and self.gallery.gid and self.gallery.token):
            logger.error(
                "Missing required data -> root: {}, gid: {}, token: {}.".format(
                    self.gallery.root, self.gallery.gid, self.gallery.token
                )
            )
            self.return_code = 0
            return

        r = self.request_hath_download(self.gallery.root, self.gallery.gid, self.gallery.token)

        if r and r.status_code == 200:

            r.encoding = "utf-8"
            soup = BeautifulSoup(r.content, "html.parser")

            container = soup.find(text=re.compile("An original resolution download has been queued for client"))

            if not container:
                logger.error("Could not find expected text in response.")
                self.return_code = 0
                return
            client_parent = container.parent
            if not client_parent:
                logger.error("Could not find expected HTML structure.")
                self.return_code = 0
                return
            client_id = client_parent.find("strong")
            if client_id:
                logger.info("Queued download to client: {}".format(client_id.get_text()))

            to_use_filename = get_base_filename_string_from_gallery_data(self.gallery)

            self.gallery.filename = available_filename(
                self.settings.MEDIA_ROOT,
                os.path.join(
                    self.own_settings.hath_dl_folder,
                    replace_illegal_name(to_use_filename + " [" + str(self.gallery.gid) + "]") + ".zip",
                ),
            )

            self.download_id = self.gallery.gid
            self.fileDownloaded = 1
            self.return_code = 1
        else:
            if r:
                logger.error("Did not get a 200 response, text: {}".format(r.text))
            else:
                logger.error("Did not get a response")
            self.return_code = 0

    def request_hath_download(self, root: str, gid: str, token: str) -> Optional[requests.models.Response]:

        url = root + "/archiver.php"

        params = {"gid": gid, "token": token}

        # logger.info("Requesting hath download to URL: {}".format(url))

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = params
        request_dict["data"] = {"hathdl_xres": "org"}

        for retry_count in range(3):
            try:
                r = requests.post(url, **request_dict)
                return r
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if retry_count < 2:
                    logger.warning("Request failed, retrying {} of {}: {}".format(retry_count, 3, str(e)))
                    continue
                else:
                    return None
        return None

    def update_archive_db(self, default_values: DataDict) -> Optional["Archive"]:

        if not self.gallery:
            return None

        values = {
            "title": self.gallery.title,
            "title_jpn": self.gallery.title_jpn,
            "zipped": self.gallery.filename,
            "crc32": self.crc32,
            "filesize": self.gallery.filesize,
            "filecount": self.gallery.filecount,
        }
        default_values.update(values)
        return Archive.objects.update_or_create_by_values_and_gid(
            default_values, (self.gallery.gid, self.gallery.provider), zipped=self.gallery.filename
        )


class InfoDownloader(BaseInfoDownloader):

    provider = constants.provider_name


class FakeDownloader(BaseFakeDownloader):

    provider = constants.provider_name


class UrlSubmitDownloader(BaseDownloader):

    type = "submit"
    provider = constants.provider_name

    def start_download(self) -> None:

        if not self.original_gallery:
            return

        logger.info("Adding gallery submission info to database")

        self.return_code = 1


API = (
    ArchiveDownloader,
    TorrentDownloader,
    TorrentAPIDownloader,
    HathDownloader,
    InfoDownloader,
    FakeDownloader,
    UrlSubmitDownloader,
)
