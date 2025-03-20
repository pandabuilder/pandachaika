# -*- coding: utf-8 -*-
import logging
import typing
from typing import Optional
from collections import defaultdict

from core.base.parsers import BaseParser

from . import constants

# Generic parser, meaning that only downloads archives, no metadata.
from core.base.types import GalleryData

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.base_url, constants.old_base_url]

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters=None,
        wanted_only: bool = False,
        preselected_wanted_matches: Optional[dict[str, list["WantedGallery"]]] = None,
    ) -> None:

        unique_urls = set()
        gallery_data_list = []
        gallery_wanted_lists: dict[str, list["WantedGallery"]] = preselected_wanted_matches or defaultdict(list)

        if not self.downloaders:
            logger.warning("No downloaders enabled, returning.")
            return

        for url in urls:

            if not any(word in url for word in self.accepted_urls):
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if "/file/" in url:
                if constants.old_base_url in url:
                    url = url.replace(constants.old_base_url, constants.base_url)
            else:
                url = url.replace(constants.base_url, constants.old_base_url)

            unique_urls.add(url)

        for gallery_url in unique_urls:
            gallery_data = GalleryData(gallery_url, self.name, link=gallery_url)
            gallery_data_list.append(gallery_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (Parser,)
