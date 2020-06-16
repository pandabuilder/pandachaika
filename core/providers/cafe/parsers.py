# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from datetime import datetime
from typing import Optional, List, Union, Type
from urllib.request import ProxyHandler

import feedparser
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import (
    translate_tag_list, unescape, request_with_retries, construct_request_dict)
from core.base.types import GalleryData
from . import constants

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
            return None

        response.encoding = 'utf-8'

        match_string = re.compile(constants.main_page + '/(.+)/$')

        tags = []

        soup = BeautifulSoup(response.text, 'html.parser')

        content_container = soup.find("div", class_="content")

        if not content_container:
            return None

        artists_container = content_container.find_all("a", href=re.compile(constants.main_page + '/artist/.*/$'))

        for artist in artists_container:
            tags.append("artist:{}".format(artist.get_text()))

        tags_container = content_container.find_all("a", href=re.compile(constants.main_page + '/tag/.*/$'))

        for tag in tags_container:
            tags.append(tag.get_text())

        # thumbnail_small_container = content_container.find("img")
        # if thumbnail_small_container:
        #     thumbnail_url = thumbnail_small_container.get('src')
        thumbnail_url = soup.find("meta", property="og:image")

        match_result = match_string.match(soup.find("meta", property="og:link"))
        if not match_result:
            return None

        gallery = GalleryData(
            match_result.group(1),
            link=link,
            title=soup.find("meta", property="og:title"),
            comment='',
            thumbnail_url=thumbnail_url,
            category='Manga',
            uploader='',
            posted=None,
            filecount=0,
            filesize=0,
            expunged=False,
            rating='',
            tags=translate_tag_list(tags),
            content=content_container.encode_contents(),
        )

        return gallery

    def get_values_from_gallery_link_json(self, link: str) -> Optional[GalleryData]:

        match_string = re.compile(constants.main_page + '/(.+)/$')

        m = match_string.match(link)

        if m:
            gallery_slug = m.group(1)
        else:
            return None

        api_link = constants.posts_api_url

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['params'] = {'slug': gallery_slug}

        response = request_with_retries(
            api_link,
            request_dict,
            post=False,
        )

        if not response:
            return None

        response.encoding = 'utf-8'
        try:
            response_data = response.json()
        except(ValueError, KeyError):
            logger.error("Could not parse response to JSON: {}".format(api_link))
            return None

        tags = []
        thumbnail_url = ''

        if len(response_data) < 1:
            return None

        api_gallery = response_data[0]

        soup = BeautifulSoup(api_gallery['content']['rendered'], 'html.parser')

        artists_container = soup.find_all("a", href=re.compile(constants.main_page + '/artist/.*/$'))

        for artist in artists_container:
            tags.append("artist:{}".format(artist.get_text()))

        tags_container = soup.find_all("a", href=re.compile(constants.main_page + '/tag/.*/$'))

        for tag in tags_container:
            tags.append(tag.get_text())

        thumbnail_small_container = soup.find("img")
        if thumbnail_small_container:
            thumbnail_url = thumbnail_small_container.get('src')

        gallery = GalleryData(
            gallery_slug,
            link=link,
            title=unescape(api_gallery['title']['rendered']),
            comment='',
            thumbnail_url=thumbnail_url,
            provider=self.name,
            category='Manga',
            uploader='',
            posted=datetime.strptime(api_gallery['date_gmt'] + '+0000', "%Y-%m-%dT%H:%M:%S%z"),
            filecount=0,
            filesize=0,
            expunged=False,
            rating='',
            tags=translate_tag_list(tags),
            content=api_gallery['content']['rendered'],
        )

        return gallery

    # Even if we just call the single method, it allows to upgrade this easily in case group calls are supported
    # afterwards. Also, we can add a wait_timer here.
    def get_values_from_gallery_link_list(self, links: List[str]) -> List[GalleryData]:
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

    @staticmethod
    def get_feed_urls() -> List[str]:
        return [constants.rss_url, ]

    def crawl_feed(self, feed_url: str = '') -> List[GalleryData]:

        if not feed_url:
            feed_url = constants.rss_url

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = request_with_retries(
            feed_url,
            request_dict,
            post=False,
        )

        if not response:
            logger.error("Got no response from feed URL: {}".format(feed_url))
            return []

        response.encoding = 'utf-8'

        feed = feedparser.parse(
            response.text
        )

        galleries = []

        match_string = re.compile(constants.main_page + '/(.+)/$')
        skip_tags = ['Uncategorized']

        logger.info("Provided RSS URL for provider ({}), adding {} found links".format(
            self.name, len(feed['items']))
        )

        for item in feed['items']:
            tags = [x.term for x in item['tags'] if x.term not in skip_tags]

            thumbnail_url = ''

            for content in item['content']:
                soup = BeautifulSoup(content.value, 'html.parser')

                artists_container = soup.find_all("a", href=re.compile(constants.main_page + '/artist/.*/$'))

                for artist in artists_container:
                    tags.append("artist:{}".format(artist.get_text()))

                thumbnail_small_container = soup.find("img")
                if thumbnail_small_container:
                    thumbnail_url = thumbnail_small_container.get('src')

            match_result = match_string.match(item['link'])
            if not match_result:
                continue

            gallery = GalleryData(
                match_result.group(1),
                title=item['title'],
                comment=item['description'],
                thumbnail_url=thumbnail_url,
                provider=self.name,
                category='Manga',
                uploader=item['author'],
                posted=datetime.strptime(item['published'], "%a, %d %b %Y %H:%M:%S %z"),
                filecount=0,
                filesize=0,
                expunged=False,
                rating='',
                tags=translate_tag_list(tags),
                content=item['content'][0].value,
                link=item['link']
            )

            # Must check here since this method is called after the main check in crawl_urls
            if self.general_utils.discard_by_tag_list(gallery.tags):
                continue

            if not gallery.link:
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                gallery.gid, link=gallery.link
            )
            if discard_approved:
                if not self.settings.silent_processing:
                    logger.info(discard_message)
                continue

            galleries.append(gallery)

        return galleries

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        response = self.get_values_from_gallery_link_json(url)
        if not response:
            logger.warning(
                "Could not fetch from API for gallery: {}, retrying from gallery page.".format(url)
            )
            response = self.get_values_from_gallery_link(url)
        return response

    def fetch_multiple_gallery_data(self, url_list: List[str]) -> List[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(constants.main_page + '/(.+)/$', url)
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
            logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if constants.rss_url in url:
                continue

            if constants.main_page not in url:
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

        galleries_data = self.fetch_multiple_gallery_data(fetch_format_galleries)

        for internal_gallery_data in galleries_data:

            if not internal_gallery_data.link:
                continue

            if self.general_utils.discard_by_tag_list(internal_gallery_data.tags):
                if not self.settings.silent_processing:
                    logger.info(
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

        if constants.rss_url in urls:
            accepted_feed_data = []
            found_feed_data = self.crawl_feed(constants.rss_url)

            if found_feed_data:
                logger.info("Processing {} galleries from RSS.".format(len(found_feed_data)))

            for feed_gallery in found_feed_data:

                if not feed_gallery.link:
                    continue

                if wanted_filters:
                    self.compare_gallery_with_wanted_filters(
                        feed_gallery,
                        feed_gallery.link,
                        wanted_filters,
                        gallery_wanted_lists
                    )
                    if wanted_only and not gallery_wanted_lists[feed_gallery.gid]:
                        continue
                accepted_feed_data.append(feed_gallery)

            gallery_data_list.extend(accepted_feed_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    Parser,
)
