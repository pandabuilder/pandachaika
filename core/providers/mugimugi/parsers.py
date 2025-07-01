# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.types import GalleryData
from core.base.utilities import chunks, request_with_retries, construct_request_dict
from core.providers.mugimugi.utilities import convert_api_response_text_to_gallery_dicts
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = constants.gallery_container_urls

    def get_galleries_from_xml(self, url_group: Iterable[str]) -> list[GalleryData]:

        possible_gallery_ids = [self.id_from_url(gallery_url) for gallery_url in url_group]

        galleries_ids = [gallery_id.replace("mugi-B", "B") for gallery_id in possible_gallery_ids if gallery_id]

        galleries = list()

        gallery_chunks = list(chunks(galleries_ids, 25))

        for i, group in enumerate(gallery_chunks):
            logger.info(
                "Calling API ({}). Gallery group: {}, galleries in group: {}, total groups: {}".format(
                    self.name, i + 1, len(group), len(gallery_chunks)
                )
            )

            # API doesn't say anything about needing to wait between requests, but we wait just in case.
            if i > 0:
                time.sleep(self.own_settings.wait_timer)

            link = constants.main_page + "/api/" + self.own_settings.api_key + "/?S=getID&ID=" + ",".join(galleries_ids)

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = request_with_retries(
                link,
                request_dict,
                post=False,
            )

            if not response:
                continue

            response.encoding = "utf-8"
            api_galleries = convert_api_response_text_to_gallery_dicts(response.text)

            if not api_galleries:
                continue
            galleries.extend(api_galleries)

        return galleries

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        # Ideally we always call with a link, since there is a daily limit in API calls
        response = self.get_galleries_from_xml((url,))
        if response:
            return response[0]
        return None

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData]:
        return self.get_galleries_from_xml(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(r"/book/(\w+)/.*", url)
        if m and m.group(1):
            return "mugi-B" + m.group(1)
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

        if not self.own_settings.api_key:
            logger.error(
                "Can't use {} API without an api key. Check {}/API_MANUAL.txt".format(self.name, constants.main_page)
            )
            return

        for url in urls:

            if not any(word in url for word in constants.gallery_container_urls):
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
