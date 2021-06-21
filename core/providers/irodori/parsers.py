# -*- coding: utf-8 -*-
import json
import logging
import re
import time
import typing
from collections import defaultdict
from typing import Optional
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup
from django.db.models import QuerySet
from dateutil import parser as date_parser

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
    accepted_urls = [constants.no_scheme_url + '/products/']

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

        return self.process_regular_gallery_page(link, response.text)

    PAGES_REGEX = re.compile(r'Pages:\s+(\d+)', re.IGNORECASE)
    JP_TITLE_REGEX = re.compile(r'Japanese Title:\s+(.+)', re.IGNORECASE)
    DATE_CONVENTION_REGEX = re.compile(r'Date/Convention:\s+(.+)', re.IGNORECASE)
    OTHER_LANGUAGE_REGEX = re.compile(r'\*This work is in (\w+). For the English version', re.IGNORECASE)

    def process_regular_gallery_page(self, link: str, response_text: str) -> Optional[GalleryData]:

        soup = BeautifulSoup(response_text, 'html.parser')
        gallery = GalleryData(link.replace(constants.main_url + '/', ''), self.name)
        gallery.link = link
        gallery.tags = []
        title_container = soup.find("meta", property="og:title")
        gallery.title = title_container['content'] if title_container else ''

        # Defaulting to Doujinshi (might need to change later)
        gallery.category = 'Doujinshi'

        description_container = soup.find("script", id="ProductJson-product-template")

        # We can get most data from this object
        parsed_json = json.loads(description_container.string)

        # gallery.title = parsed_json.get('title', '')
        gallery.posted = date_parser.parse(parsed_json.get('published_at', None))

        thumbnail_url = parsed_json.get('featured_image', None)
        if thumbnail_url:
            gallery.thumbnail_url = urljoin(thumbnail_url, urlparse(thumbnail_url).path)
            if gallery.thumbnail_url and gallery.thumbnail_url.startswith('//'):
                gallery.thumbnail_url = 'https:' + gallery.thumbnail_url

        if 'vendor' in parsed_json:
            gallery.tags.append(
                translate_tag("artist:" + parsed_json['vendor'])
            )

        if 'tags' in parsed_json:
            for tag in parsed_json['tags']:
                gallery.tags.append(translate_tag(tag))

        if 'description' in parsed_json:
            description_soup = BeautifulSoup(parsed_json['description'], 'html.parser')

            description_text = description_soup.get_text().replace("Synopsis:", "")
            description_text_newline = description_soup.get_text("\n").replace("Synopsis:", "")

            description_text_remove = description_text

            description_text_remove = self.PAGES_REGEX.sub('', description_text_remove)
            description_text_remove = self.JP_TITLE_REGEX.sub('', description_text_remove)
            description_text_remove = self.DATE_CONVENTION_REGEX.sub('', description_text_remove)

            other_language_found = self.OTHER_LANGUAGE_REGEX.search(description_text_newline)
            if other_language_found:
                gallery.tags.append(
                    translate_tag("language:" + other_language_found.group(1))
                )
            else:
                gallery.tags.append(
                    translate_tag("language:english")
                )

            pages_text_found = self.PAGES_REGEX.search(description_text_newline)
            if pages_text_found:
                gallery.filecount = int(pages_text_found.group(1))

            jp_text_found = self.JP_TITLE_REGEX.search(description_text_newline)
            if jp_text_found:
                gallery.title_jpn = jp_text_found.group(1)

            convention_text_found = self.DATE_CONVENTION_REGEX.search(description_text_newline)
            if convention_text_found:
                gallery.tags.append(
                    translate_tag("event:" + convention_text_found.group(1))
                )

            reparsed_text = ''

            # roundabout way to remove all after the used regexs.
            for char_count, char in enumerate(description_text_remove):
                if description_text_remove[char_count] == description_text[char_count]:
                    reparsed_text += char
                else:
                    break

            gallery.comment = reparsed_text.strip()

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

    def fetch_gallery_data(self, url) -> Optional[GalleryData]:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> Optional[list[GalleryData]]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(constants.main_url + '/(.+)', url)
        if m and m.group(1):
            return m.group(1)
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

            if constants.no_scheme_url not in url:
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue
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
