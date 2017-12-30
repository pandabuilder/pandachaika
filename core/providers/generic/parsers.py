# -*- coding: utf-8 -*-
from collections import defaultdict
from typing import List

from core.base.parsers import BaseParser


# Generic parser, meaning that only downloads archives, no metadata.
from core.base.types import GalleryData


class GenericParser(BaseParser):
    name = 'generic'
    ignore = False

    # Accepts anything, doesn't check if it's a valid link for any of the downloader.
    @classmethod
    def filter_accepted_urls(cls, urls: List) -> List:
        return urls

    def crawl_urls(self, urls: List[str], wanted_filters=None, wanted_only: bool=False) -> None:

        unique_urls = set()
        gallery_data_list = []
        gallery_wanted_lists = defaultdict(list)

        if not self.downloaders:
            self.logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:
            unique_urls.add(url)

        for gallery_url in unique_urls:
            gallery_data = GalleryData(gallery_url, link=gallery_url)
            gallery_data_list.append(gallery_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    GenericParser,
)
