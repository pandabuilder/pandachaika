# -*- coding: utf-8 -*-
import logging
import typing
from collections import defaultdict
from collections.abc import Iterable

from core.base.parsers import BaseParser


# Generic parser, meaning that only downloads archives, no metadata.
from core.base.types import GalleryData

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class GenericParser(BaseParser):
    name = 'generic'
    ignore = False

    # Accepts anything, doesn't check if it's a valid link for any of the downloader.
    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return list(urls)

    def crawl_urls(self, urls: list[str], wanted_filters=None, wanted_only: bool = False,
                   preselected_wanted_matches: dict[str, list['WantedGallery']] = None) -> None:

        unique_urls = set()
        gallery_data_list = []
        gallery_wanted_lists: dict[str, list['WantedGallery']] = preselected_wanted_matches or defaultdict(list)

        if not self.downloaders:
            logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:
            unique_urls.add(url)

        for gallery_url in unique_urls:
            gallery_data = GalleryData(gallery_url, self.name, link=gallery_url)
            gallery_data_list.append(gallery_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    GenericParser,
)
