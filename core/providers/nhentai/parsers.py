# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from typing import Optional

import bs4
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

        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        if soup:
            jpn_title_container = soup.find("div", id=re.compile("info"))
            if isinstance(jpn_title_container, bs4.element.Tag):
                title_jpn_match = jpn_title_container.h2
            else:
                title_jpn_match = None

            gallery_id_match = re.search(r"{}(\d+)".format(constants.gallery_container_url), link)

            if not gallery_id_match:
                return None
            gallery_id = "nh-" + gallery_id_match.group(1)

            gallery = GalleryData(gallery_id, self.name)
            title_h1 = soup.h1
            if title_h1:
                gallery.title = title_h1.get_text()
            gallery.title_jpn = title_jpn_match.get_text() if title_jpn_match else ""
            gallery_filecount_match = re.search(r"<div>(\d+) page(s*)</div>", response.text)
            if gallery_filecount_match:
                gallery.filecount = int(gallery_filecount_match.group(1))
            else:
                gallery.filecount = 0
            gallery.tags = []
            gallery.link = link
            posted_container = soup.find("time")
            if isinstance(posted_container, bs4.element.Tag) and isinstance(posted_container["datetime"], str):
                gallery.posted = dateutil.parser.parse(posted_container["datetime"])

            for tag_container in soup.find_all("a", {"class": "tag"}):
                tag_name = [text for text in tag_container.stripped_strings][0]
                tag_name = tag_name.split(" | ")[0]
                if tag_container.parent:
                    tag_ext = tag_container.parent.get_text()
                    if tag_container.parent.parent:
                        tag_scope = tag_container.parent.parent.get_text()
                    else:
                        tag_scope = "tags"
                else:
                    tag_scope = "tags"
                    tag_ext = ""

                tag_scope = tag_scope.replace(tag_ext, "").replace("\t", "").replace("\n", "").replace(":", "").lower()
                if tag_scope == "tags":
                    gallery.tags.append(translate_tag(tag_name))
                elif tag_scope == "categories":
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

            logger.info("Calling API ({}). " "Gallery: {}, total galleries: {}".format(self.name, i + 1, len(links)))

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
        m = re.search(r"(.+)/g/(\d+)/*(\d*)", url)
        if m and m.group(2):
            return "nh-" + m.group(2)
        else:
            return None

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters: Optional[QuerySet] = None,
        wanted_only: bool = False,
        preselected_wanted_matches: Optional[dict[str, list["WantedGallery"]]] = None,
    ) -> None:

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries = []
        gallery_wanted_lists: dict[str, list["WantedGallery"]] = preselected_wanted_matches or defaultdict(list)

        if not self.downloaders:
            logger.warning("No downloaders enabled, returning.")
            return

        for url in urls:

            if constants.gallery_container_url not in url:
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

            banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                internal_gallery_data.tags, internal_gallery_data.uploader
            )

            if banned_result:
                if self.gallery_callback:
                    self.gallery_callback(None, internal_gallery_data.link, "banned_data")

                if not self.settings.silent_processing:
                    logger.info(
                        "Skipping gallery link {}, discarded reasons: {}".format(
                            internal_gallery_data.link, banned_reasons
                        )
                    )
                continue

            if wanted_filters:
                self.compare_gallery_with_wanted_filters(
                    internal_gallery_data, internal_gallery_data.link, wanted_filters, gallery_wanted_lists
                )
                if wanted_only and not gallery_wanted_lists[internal_gallery_data.gid]:
                    continue

            gallery_data_list.append(internal_gallery_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (Parser,)
