# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from typing import Optional

import bs4
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import (
    translate_tag_list, request_with_retries, construct_request_dict)
from core.base.types import GalleryData
from . import constants
from viewer.models import Gallery

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.main_page, constants.rss_url]

    def get_values_from_gallery_link(self, link: str) -> Optional[GalleryData]:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = request_with_retries(
            link,
            request_dict,
            post=False,
        )

        if not response:
            logger.error("Got no response from: {}".format(link))
            return None

        response.encoding = 'utf-8'

        match_string = re.compile(constants.main_page + r"/view/(\d+)/*$")

        tags = []

        soup = BeautifulSoup(response.text, 'html.parser')

        content_containers = soup.find_all("div", class_="container")

        if not content_containers:
            logger.error("Could not find content containers")
            return None

        content_container = None

        for box_container in content_containers:
            content_container = box_container.find("div", class_="box")
            if content_container:
                break

        if not content_container:
            logger.error("Could not find content container")
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
            logger.error("Could not find gallery info container")
            return None

        gallery_id = match_result.group(1)

        gallery_title_container = content_container.find("h1", class_="title")

        if gallery_title_container:
            gallery_title = gallery_title_container.get_text()
        else:
            gallery_title = ""

        gallery = GalleryData(
            gallery_id,
            self.name,
            link=link,
            title=gallery_title,
            thumbnail_url=thumbnail_url,
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

    def get_feed_urls(self) -> list[str]:
        return [constants.rss_url, ]

    def crawl_feed(self, feed_url: str = '') -> list[str]:

        urls: list[str] = []

        if not feed_url:
            feed_url = constants.rss_url

        current_page = 1

        m = re.search(r".*?/page/(\d+)", feed_url)
        if m and m.group(1):
            current_page = int(m.group(1))
            feed_url = constants.rss_url

        while True:

            paged_feed_url = "{}/page/{}".format(feed_url, current_page)

            if current_page > 1:
                time.sleep(self.own_settings.wait_timer)

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = request_with_retries(
                paged_feed_url,
                request_dict,
                post=False,
            )

            if not response:
                logger.error("No response from URL: {}, returning".format(paged_feed_url))
                return urls

            response.encoding = 'utf-8'

            match_string = re.compile(r"/view/(\d+)/*$")

            soup = BeautifulSoup(response.text, 'html.parser')

            content_container = soup.find("div", class_="columns")

            if not content_container:
                logger.error("Content container not found, returning")
                return urls

            url_containers = content_container.find_all("a", href=match_string)

            current_gids: list[str] = []

            for url_container in url_containers:

                url_link = url_container.get('href')

                complete_url = "{}{}".format(constants.main_page, url_link)

                gid = self.id_from_url(complete_url)

                if gid:
                    current_gids.append(gid)

            if len(current_gids) < 1:
                logger.info(
                    'Got to page {}, and we got less than 1 gallery, '
                    'meaning there is no more pages, stopping'.format(current_page)
                )
                break

            used = Gallery.objects.filter(gid__in=current_gids, provider=constants.provider_name)

            if used.count() == len(current_gids):
                logger.info(
                    'Got to page {}, it has already been processed entirely, stopping'.format(current_page)
                )
                break

            used_gids = used.values_list('gid', flat=True)

            current_urls: list[str] = [
                '{}/view/{}'.format(constants.main_page, x) for x in list(set(current_gids).difference(used_gids))
            ]

            urls.extend(current_urls)

            current_page += 1

        return urls

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(constants.main_page + r'/view/(\d+)/*$', url)
        if m and m.group(1):
            return m.group(1)
        else:
            return None

    def crawl_urls(self, urls: list[str], wanted_filters: QuerySet = None, wanted_only: bool = False,
                   preselected_wanted_matches: dict[str, list['WantedGallery']] = None) -> None:

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries = []
        gallery_wanted_lists: dict[str, list['WantedGallery']] = preselected_wanted_matches or defaultdict(list)

        if not self.downloaders:
            logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if constants.main_page not in url:
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if '/view/' in url or '/read/' in url or '/zip/' in url:
                if not self.settings.silent_processing:
                    logger.info("Provided URL {} is a gallery link, adding".format(url))
                unique_urls.add(url)
                continue

            if constants.rss_url in url:
                feed_links = self.crawl_feed(url)
                unique_urls.update(feed_links)
                logger.info("Provided RSS URL for provider ({}), adding {} found links".format(
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
                    logger.info(discard_message)
                continue

            fetch_format_galleries.append(gallery)

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
