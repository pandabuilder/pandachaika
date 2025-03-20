# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from datetime import datetime
from typing import Optional

import bs4
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import request_with_retries, construct_request_dict
from core.base.types import GalleryData
from core.base.utilities import translate_tag
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.comic_no_scheme_url]

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
        gallery_container_head = soup.find("head")

        if isinstance(gallery_container_head, bs4.element.Tag):
            gid_container = gallery_container_head.find("meta", property="og:url")

            if isinstance(gid_container, bs4.element.Tag) and isinstance(gid_container["content"], str):
                url_parts = gid_container["content"].split("/")
                gid = url_parts[-1]
                gallery = GalleryData(gid, self.name)
                gallery.link = link
                gallery.tags = []
                gallery.category = "Doujinshi"

                title_container = gallery_container_head.find("meta", property="og:title")
                image_url_container = gallery_container_head.find("meta", property="og:image")
                tags_containers = gallery_container_head.find_all("meta", property="book:tag")
                page_count_container = gallery_container_head.find("meta", property="books:page_count")
                description_container = gallery_container_head.find("meta", property="og:description")
                author_container = soup.find("span", itemprop="author")
                section_container = soup.find("section", class_="showcase_comic_single_description")
                gallery_container_titles = soup.find("hgroup", class_="showcase_comic_single_main_titles")

                if isinstance(title_container, bs4.element.Tag) and isinstance(title_container["content"], str):
                    gallery.title = title_container["content"]
                if isinstance(gallery_container_titles, bs4.element.Tag):
                    title_jpn_container = gallery_container_titles.find("span", lang="ja")
                    if title_jpn_container:
                        gallery.title_jpn = title_jpn_container.get_text()
                if isinstance(image_url_container, bs4.element.Tag) and isinstance(image_url_container["content"], str):
                    gallery.thumbnail_url = image_url_container["content"]
                if isinstance(description_container, bs4.element.Tag) and isinstance(
                    description_container["content"], str
                ):
                    gallery.comment = description_container["content"]
                if isinstance(page_count_container, bs4.element.Tag) and isinstance(
                    page_count_container["content"], str
                ):
                    gallery.filecount = int(page_count_container["content"])
                if isinstance(author_container, bs4.element.Tag):
                    group_name = author_container.find("meta", itemprop="name")
                    if isinstance(group_name, bs4.element.Tag) and isinstance(group_name["content"], str):
                        gallery.tags.append(translate_tag("group:" + group_name["content"]))
                if isinstance(section_container, bs4.element.Tag):
                    time_container = section_container.find("time", itemprop="datePublished")
                    if isinstance(time_container, bs4.element.Tag) and isinstance(time_container["datetime"], str):
                        gallery.posted = datetime.fromisoformat(time_container["datetime"] + "+00:00")
                    p_containers = section_container.find_all("p")
                    for ps in p_containers:
                        p_text = ps.get_text()
                        if "Source:" in p_text:
                            parody_name = p_text.replace("Source: ", "").rstrip()
                            gallery.tags.append(translate_tag("parody:" + parody_name))

                for tag_container in tags_containers:
                    if isinstance(tag_container, bs4.element.Tag) and isinstance(tag_container["content"], str):
                        tag = translate_tag(tag_container["content"])
                        gallery.tags.append(tag)

                gallery.tags.append(translate_tag("language:english"))

            else:
                return None
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

    def fetch_gallery_data(self, url) -> Optional[GalleryData]:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> Optional[list[GalleryData]]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(constants.no_scheme_url + r"/Comic/(\d+)", url)
        if m and m.group(1):
            return m.group(1)
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

            if constants.no_scheme_url not in url:
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue
            unique_urls.add(url)

        for gallery in unique_urls:
            gid = self.id_from_url(gallery)
            if not gid:
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(gallery_id=gid, link=gallery)

            if discard_approved:
                if not self.settings.silent_processing:
                    logger.info(discard_message)
                continue

            fetch_format_galleries.append(gallery)

        if len(fetch_format_galleries) == 0:
            logger.info("No galleries need downloading, returning.")
            return

        galleries_data = self.fetch_multiple_gallery_data(fetch_format_galleries)

        if not galleries_data:
            return

        for internal_gallery_data in galleries_data:

            if not internal_gallery_data.link:
                continue

            banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                internal_gallery_data.tags, internal_gallery_data.uploader
            )

            if banned_result:
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
