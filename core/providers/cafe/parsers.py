# -*- coding: utf-8 -*-
import re
import time
from collections import defaultdict
from datetime import datetime
from urllib.request import ProxyHandler

import feedparser
from bs4 import BeautifulSoup

from core.base.parsers import BaseParser
from core.base.utilities import (
    translate_tag_list, unescape, request_with_retries)
from . import constants
from . import utilities


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.main_page, constants.rss_url]

    def get_values_from_gallery_link(self, link):

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

        gallery = {
            'gid': match_string.match(soup.find("meta", property="og:link")).group(1),
            'link': link,
            'title': soup.find("meta", property="og:title"),
            'comment': '',
            'thumbnail_url': thumbnail_url,
            'category': 'Manga',
            'uploader': '',
            'posted': '',
            'filecount': 0,
            'filesize': 0,
            'expunged': False,
            'rating': '',
            'tags': translate_tag_list(tags),
            'content': content_container.encode_contents(),
        }

        return gallery

    def get_values_from_gallery_link_json(self, link):

        match_string = re.compile(constants.main_page + '/(.+)/$')

        m = match_string.match(link)

        if m:
            gallery_slug = m.group(1)
        else:
            return None

        api_link = constants.posts_api_url
        payload = {'slug': gallery_slug}

        response = request_with_retries(
            api_link,
            {
                'headers': self.settings.requests_headers,
                'timeout': self.settings.timeout_timer,
                'params': payload
            },
            post=False,
            logger=self.logger
        )

        if not response:
            return None

        response.encoding = 'utf-8'
        try:
            response_data = response.json()
        except(ValueError, KeyError):
            self.logger.error("Error parsing response from: {}".format(api_link))
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

        gallery = {
            'gid': gallery_slug,
            'link': link,
            'title': unescape(api_gallery['title']['rendered']),
            'comment': '',
            'thumbnail_url': thumbnail_url,
            'provider': self.name,
            'category': 'Manga',
            'uploader': '',
            'posted': datetime.strptime(api_gallery['date_gmt'] + '+0000', "%Y-%m-%dT%H:%M:%S%z"),
            'filecount': 0,
            'filesize': 0,
            'expunged': False,
            'rating': '',
            'tags': translate_tag_list(tags),
            'content': api_gallery['content']['rendered'],
        }

        return gallery

    # Even if we just call the single method, it allows to upgrade this easily in case group calls are supported
    # afterwards. Also, we can add a wait_timer here.
    def get_values_from_gallery_link_list(self, links):
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
    def get_feed_urls():
        return [constants.rss_url, ]

    def crawl_feed(self, feed_url=None):

        if not feed_url:
            feed_url = constants.rss_url
        feed = feedparser.parse(
            feed_url,
            handlers=ProxyHandler,
            request_headers=self.settings.requests_headers
        )

        galleries = []

        match_string = re.compile(constants.main_page + '/(.+)/$')
        skip_tags = ['Uncategorized']

        self.logger.info("Provided RSS URL, adding {} found links".format(len(feed['items'])))

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

            gallery = {
                'gid': match_string.match(item['link']).group(1),
                'title': item['title'],
                'comment': item['description'],
                'thumbnail_url': thumbnail_url,
                'provider': self.name,
                'category': 'Manga',
                'uploader': item['author'],
                'posted': datetime.strptime(item['published'], "%a, %d %b %Y %H:%M:%S %z"),
                'filecount': 0,
                'filesize': 0,
                'expunged': False,
                'rating': '',
                'tags': translate_tag_list(tags),
                'content': item['content'][0].value,
                'link': item['link']
            }

            # Must check here since this method is called after the main check in crawl_urls
            if self.general_utils.discard_by_tag_list(gallery['tags']):
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                gallery['gid'], link=gallery['link']
            )
            if discard_approved:
                if not self.settings.silent_processing:
                    self.logger.info(discard_message)
                continue

            galleries.append(gallery)

        return galleries

    def fetch_gallery_data(self, url):
        response = self.get_values_from_gallery_link_json(url)
        if not response:
            self.logger.warning(
                "Could not fetch from API for gallery: {}, retrying from gallery page.".format(url)
            )
            response = self.get_values_from_gallery_link(url)
        return response

    def fetch_multiple_gallery_data(self, url_list):
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url):
        m = re.search(constants.main_page + '/(.+)/$', url)
        if m and m.group(1):
            return m.group(1)
        else:
            return None

    @staticmethod
    def resolve_url(gallery):
        utilities.resolve_url(gallery)

    def crawl_urls(self, urls, wanted_filters=None, wanted_only=False):

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries = []
        gallery_wanted_lists = defaultdict(list)

        if not self.downloaders:
            self.logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if constants.rss_url in url:
                continue

            if constants.main_page not in url:
                self.logger.warning("Invalid URL, skipping: {}".format(url))
                continue
            unique_urls.add(url)

        for gallery in unique_urls:

            gid = self.id_from_url(gallery)
            if not gid:
                continue

            discard_approved, discard_message = self.discard_gallery_by_internal_checks(gid, link=gallery)

            if discard_approved:
                if not self.settings.silent_processing:
                    self.logger.info(discard_message)
                continue

            fetch_format_galleries.append({'link': gallery})

        galleries_data = self.fetch_multiple_gallery_data(fetch_format_galleries)

        for internal_gallery_data in galleries_data:
            if self.general_utils.discard_by_tag_list(internal_gallery_data['tags']):
                if not self.settings.silent_processing:
                    self.logger.info(
                        "Skipping gallery link {} because it's tagged with global discarded tags".format(
                            internal_gallery_data['link']
                        )
                    )
                continue

            if wanted_filters:
                self.compare_gallery_with_wanted_filters(
                    internal_gallery_data,
                    internal_gallery_data['link'],
                    wanted_filters,
                    gallery_wanted_lists
                )
                if wanted_only and not gallery_wanted_lists[internal_gallery_data['gid']]:
                    continue

            gallery_data_list.append(internal_gallery_data)

        if constants.rss_url in urls:
            accepted_feed_data = []
            found_feed_data = self.crawl_feed(constants.rss_url)

            if found_feed_data:
                self.logger.info("Processing {} galleries from RSS.".format(len(found_feed_data)))

            for feed_gallery in found_feed_data:

                if wanted_filters:
                    self.compare_gallery_with_wanted_filters(
                        feed_gallery,
                        feed_gallery['link'],
                        wanted_filters,
                        gallery_wanted_lists
                    )
                    if wanted_only and not gallery_wanted_lists[feed_gallery['gid']]:
                        continue
                accepted_feed_data.append(feed_gallery)

            gallery_data_list.extend(accepted_feed_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    Parser,
)
