# -*- coding: utf-8 -*-
import re
import time
import typing
from collections import defaultdict
from typing import Optional, List

import bs4
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import (
    translate_tag_list, request_with_retries)
from core.base.types import GalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.main_page, constants.rss_url]

    def get_values_from_gallery_link(self, link: str) -> Optional[GalleryData]:

        response = request_with_retries(
            link,
            {
                'headers': self.settings.requests_headers,
                'timeout': self.settings.timeout_timer,
            },
            post=False,
            logger=self.logger
        )

        if not response:
            self.logger.error("Got no response from: {}".format(link))
            return None

        response.encoding = 'utf-8'

        match_string = re.compile(constants.main_page + r"/view/(\d+)/*$")

        tags = []

        soup = BeautifulSoup(response.text, 'html.parser')

        content_container = soup.find_all("div", class_="container")[0]

        if not content_container:
            self.logger.error("Could not find content container")
            return None

        is_doujinshi = False

        artists_container = content_container.find_all("a", href=re.compile('/?q=artist:.*$'))

        for artist in artists_container:
            tags.append("artist:{}".format(artist.get_text()))

        language_container = content_container.find_all("a", href=re.compile('/?q=language:.*$'))

        for language in language_container:
            tags.append("language:{}".format(language.get_text()))

        magazine_container = content_container.find_all("a", href=re.compile('/?q=magazine:.*$'))

        for magazine in magazine_container:
            tags.append("magazine:{}".format(magazine.get_text()))

        parody_container = content_container.find_all("a", href=re.compile('/?q=parody:.*$'))

        for parody in parody_container:
            tags.append("parody:{}".format(parody.get_text()))

        publisher_container = content_container.find_all("a", href=re.compile('/?q=publisher:.*$'))

        for publisher in publisher_container:
            tags.append("publisher:{}".format(publisher.get_text()))

        tags_container = content_container.find_all("a", href=re.compile('/?q=tag:.*$'))

        for tag in tags_container:
            tag_cleaned = tag.get_text().replace("\t", "").replace("\n", "")
            tags.append(tag_cleaned)

            if tag_cleaned == 'doujin':
                is_doujinshi = True

        thumbnail_url = soup.find("meta", property="og:image").get('content')

        match_result = match_string.match(soup.find("meta", property="og:url").get('content'))
        if not match_result:
            self.logger.error("Could not find gallery info container")
            return None

        gallery_id = match_result.group(1)

        gallery = GalleryData(
            gallery_id,
            link=link,
            title=content_container.find("h1", class_="title").get_text(),
            thumbnail_url=thumbnail_url,
            provider=self.name,
            posted=None,
            filesize=0,
            expunged=False,
            tags=translate_tag_list(tags),
        )

        table_container = content_container.find("table", class_="view-page-details")

        if table_container:
            tr_container = table_container.find_all("tr")

            for tr in tr_container:

                if isinstance(tr, bs4.element.Tag):

                    td_container = tr.find_all("td")

                    is_description = False
                    is_pages = False

                    for td in td_container:
                        if is_description:
                            gallery.comment = td.get_text().replace("\t", "").replace("\n", "")
                            is_description = False
                        if isinstance(td, bs4.element.Tag) and td.get_text() == 'Description':
                            is_description = True

                        if is_pages:
                            right_text = td.get_text().replace("\t", "").replace("\n", "")
                            m = re.search(r'(\d+)', right_text)
                            if m:
                                gallery.filecount = int(m.group(1))
                            is_pages = False
                        if isinstance(td, bs4.element.Tag) and td.get_text() == 'Pages':
                            is_pages = True

        gallery.archiver_key = "{}/zip/{}".format(constants.main_page, gallery_id)

        if is_doujinshi:
            gallery.category = 'Doujinshi'
        else:
            gallery.category = 'Manga'

        return gallery

    # Even if we just call the single method, it allows to upgrade this easily in case group calls are supported
    # afterwards. Also, we can add a wait_timer here.
    def get_values_from_gallery_link_list(self, links: List[str]) -> List[GalleryData]:
        response = []
        for i, element in enumerate(links):
            if i > 0:
                time.sleep(self.settings.wait_timer)

            self.logger.info(
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
                self.logger.error("Failed fetching: {}, gallery might not exist".format(element))
                continue
        return response

    @staticmethod
    def get_feed_urls() -> List[str]:
        return [constants.rss_url, ]

    def crawl_feed(self, feed_url: str = None) -> List[str]:

        urls: List[str] = []

        if not feed_url:
            feed_url = constants.rss_url

        response = request_with_retries(
            feed_url,
            {
                'headers': self.settings.requests_headers,
                'timeout': self.settings.timeout_timer,
            },
            post=False,
            logger=self.logger
        )

        if not response:
            self.logger.error("No response from URL: {}, returning".format(feed_url))
            return urls

        response.encoding = 'utf-8'

        match_string = re.compile(r"/view/(\d+)/*$")

        soup = BeautifulSoup(response.text, 'html.parser')

        content_container = soup.find("div", class_="columns")

        if not content_container:
            self.logger.error("Content container not found, returning")
            return urls

        url_containers = content_container.find_all("a", href=match_string)

        for url_container in url_containers:

            url_link = url_container.get('href')

            complete_url = "{}{}".format(constants.main_page, url_link)

            urls.append(complete_url)

        return urls

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        response = self.get_values_from_gallery_link(url)
        if not response:
            self.logger.warning(
                "Could not fetch from API for gallery: {}, retrying from gallery page.".format(url)
            )
            response = self.get_values_from_gallery_link(url)
        return response

    def fetch_multiple_gallery_data(self, url_list: List[str]) -> List[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(constants.main_page + r'/view/(\d+)/*$', url)
        if m and m.group(1):
            return m.group(1)
        else:
            return None

    def crawl_urls(self, urls: List[str], wanted_filters: QuerySet = None, wanted_only: bool = False) -> None:

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries = []
        gallery_wanted_lists: typing.Dict[str, List['WantedGallery']] = defaultdict(list)

        if not self.downloaders:
            self.logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if constants.main_page not in url:
                self.logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if '/view/' in url or '/read/' in url or '/zip/' in url:
                if not self.settings.silent_processing:
                    self.logger.info("Provided URL {} is a gallery link, adding".format(url))
                unique_urls.add(url)
                continue

            if constants.rss_url in url:
                feed_links = self.crawl_feed(url)
                unique_urls.update(feed_links)
                self.logger.info("Provided RSS URL for provider ({}), adding {} found links".format(
                    self.name,
                    len(feed_links))
                )
                continue

        for gallery in unique_urls:

            gid = self.id_from_url(gallery)
            if not gid:
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(gid, link=gallery)

            if discard_approved:
                if not self.settings.silent_processing:
                    self.logger.info(discard_message)
                continue

            fetch_format_galleries.append(gallery)

        galleries_data = self.fetch_multiple_gallery_data(fetch_format_galleries)

        for internal_gallery_data in galleries_data:

            if not internal_gallery_data.link:
                continue

            if self.general_utils.discard_by_tag_list(internal_gallery_data.tags):
                if not self.settings.silent_processing:
                    self.logger.info(
                        "Skipping gallery link {} because it's tagged with global discarded tags".format(
                            internal_gallery_data.link
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
