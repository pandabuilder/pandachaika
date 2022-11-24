# -*- coding: utf-8 -*-
import logging
import re
import typing
from typing import Optional
from urllib.parse import urljoin
from collections import defaultdict

from core.base.parsers import BaseParser

from . import constants, utilities

from core.base.types import GalleryData

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [
        urljoin(constants.base_url, constants.view_path),
        urljoin(constants.base_url, constants.torrent_download_path)
    ]

    @staticmethod
    def id_from_url(url: str) -> typing.Optional[str]:
        m = re.search(r"/view/(\d+)", url)
        if m and m.group(1):
            return m.group(1)
        else:
            return None

    def crawl_urls(self, urls: list[str], wanted_filters=None, wanted_only: bool = False,
                   preselected_wanted_matches: Optional[dict[str, list['WantedGallery']]] = None) -> None:

        unique_urls = set()
        gallery_data_list = []
        gallery_wanted_lists: dict[str, list['WantedGallery']] = preselected_wanted_matches or defaultdict(list)

        if not self.downloaders:
            logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if not any(word in url for word in self.accepted_urls):
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if constants.torrent_download_path in url:
                utilities.view_link_from_download_link(url)

            unique_urls.add(url)

        for gallery in unique_urls:
            gid = self.id_from_url(gallery)
            if not gid:
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                gallery_id=gid,
                link=gallery
            )

            if discard_approved:
                if not self.settings.silent_processing:
                    logger.info(discard_message)
                continue

            gallery_data = GalleryData(gid, self.name, link=gallery)
            gallery_data_list.append(gallery_data)

        if not gallery_data_list:
            return

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    Parser,
)
