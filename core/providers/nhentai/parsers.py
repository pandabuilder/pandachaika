# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from typing import Optional

import dateutil.parser
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import translate_tag, request_with_retries, construct_request_dict
from core.base.types import GalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.gallery_container_url]

    def get_values_from_gallery_link(self, link: str) -> Optional[GalleryData]:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = request_with_retries(
            link,
            request_dict,
            post=False,
        )

        if not response:
            return None

        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        if soup:
            title_jpn_match = soup.find("div", id=re.compile("info")).h2

            gallery_id_match = re.search(r'{}(\d+)'.format(constants.gallery_container_url), link)

            if not gallery_id_match:
                return None
            gallery_id = 'nh-' + gallery_id_match.group(1)

            gallery = GalleryData(gallery_id, self.name)
            gallery.title = soup.h1.get_text()
            gallery.title_jpn = title_jpn_match.get_text() if title_jpn_match else ''
            gallery_filecount_match = re.search(r'<div>(\d+) page(s*)</div>', response.text)
            if gallery_filecount_match:
                gallery.filecount = int(gallery_filecount_match.group(1))
            else:
                gallery.filecount = 0
            gallery.tags = []
            gallery.link = link
            gallery.posted = dateutil.parser.parse(soup.find("time")['datetime'])

            for tag_container in soup.find_all("a", {"class": "tag"}):
                tag_name = [text for text in tag_container.stripped_strings][0]
                tag_name = tag_name.split(" | ")[0]
                tag_scope = tag_container.parent.parent.get_text()
                tag_ext = tag_container.parent.get_text()
                tag_scope = tag_scope.replace(tag_ext, "").replace("\t", "").replace("\n", "").replace(":", "").lower()
                if tag_scope == 'tags':
                    gallery.tags.append(translate_tag(tag_name))
                elif tag_scope == 'categories':
                    gallery.category = tag_name.capitalize()
                else:
                    gallery.tags.append(translate_tag(tag_scope + ":" + tag_name))

        else:
            return None
        return gallery

    # Even if we just call the single method, it allows to upgrade this easily in case group calls are supported
    # afterwards. Also, we can add a wait_timer here.
    def get_values_from_gallery_link_list(self, links: list[str]) -> list[GalleryData]:
        response = []
        for i, element in enumerate(links):
            if i > 0:
                time.sleep(self.own_settings.wait_timer)

            logger.info(
                "Calling API ({}). "
                "Gallery: {}, total galleries: {}".format(
                    self.name,
                    i + 1,
                    len(links)
                )
            )

            values = self.fetch_gallery_data(element)
            if values:
                response.append(values)
            else:
                logger.error("Failed fetching: {}, gallery might not exist".format(element))
                continue
        return response

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(r'(.+)/g/(\d+)/*(\d*)', url)
        if m and m.group(2):
            return 'nh-' + m.group(2)
        else:
            return None

    def crawl_urls(self, urls: list[str], wanted_filters: QuerySet = None, wanted_only: bool = False) -> None:

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries = []
        gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

        if not self.downloaders:
            logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if not(constants.gallery_container_url in url):
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue
            unique_urls.add(url)

        for gallery in unique_urls:
            gid = self.id_from_url(gallery)
            if not gid:
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(gid, link=gallery)

            if discard_approved:
                if not self.settings.silent_processing:
                    logger.info(discard_message)
                continue

            fetch_format_galleries.append(gallery)

        if len(fetch_format_galleries) == 0:
            logger.info("No galleries need downloading, returning.")
            return

        galleries_data = self.fetch_multiple_gallery_data(fetch_format_galleries)

        for internal_gallery_data in galleries_data:

            if not internal_gallery_data.link:
                continue

            discarded_tags = self.general_utils.discard_by_tag_list(internal_gallery_data.tags)

            if discarded_tags:
                if not self.settings.silent_processing:
                    logger.info(
                        "Skipping gallery link {}, because it's tagged with global discarded tags: {}".format(
                            internal_gallery_data.link,
                            discarded_tags
                        )
                    )
                continue

            if wanted_filters:
                self.compare_gallery_with_wanted_filters(
                    internal_gallery_data,
                    internal_gallery_data.link,
                    wanted_filters,
                    gallery_wanted_lists
                )
                if wanted_only and not gallery_wanted_lists[internal_gallery_data.gid]:
                    continue

            gallery_data_list.append(internal_gallery_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    Parser,
)
