# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import bs4
from bs4 import BeautifulSoup
from dateutil import parser
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
    accepted_urls = [constants.manga_no_scheme_url]

    def get_values_from_gallery_link(self, link: str) -> Optional[GalleryData]:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        id_slug = self.id_from_url(link)
        if id_slug is None:
            return None
        id_slug_parts = id_slug.split("/")
        if len(id_slug_parts) < 2:
            return None

        response = request_with_retries(
            urljoin(constants.main_url, "{}/{}".format(constants.gallery_data_path, id_slug_parts[0])),
            request_dict,
            post=False,
        )

        if not response:
            return None

        try:
            response_data = response.json()
        except (ValueError, KeyError):
            logger.warning("Error parsing response from server: {}".format(response.text))
            return None

        gallery_response = response_data["data"]

        gallery = GalleryData(id_slug, self.name)
        gallery.link = link
        gallery.tags = [translate_tag(x["name"]) for x in gallery_response["tags"]]

        gallery.title = gallery_response["title"]
        gallery.comment = gallery_response["description"]
        gallery.posted = parser.parse(gallery_response["published_at"])

        if "creator_name" in gallery_response:
            gallery.tags.append(translate_tag("artist:" + gallery_response["creator_name"]))

        gallery.category = "Doujinshi"

        gallery.thumbnail_url = gallery_response["thumb"]

        gallery.tags.append(translate_tag("language:english"))

        gallery.provider_metadata = gallery_response

        return gallery

    # Even if we just call the single method, it allows to upgrade this easily in case group calls are supported
    # afterward. Also, we can add a wait_timer here.
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
        return url.replace(constants.main_url, "").replace("/manga/", "")

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
