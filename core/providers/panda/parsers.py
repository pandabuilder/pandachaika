# -*- coding: utf-8 -*-
import json
import logging
import re
import time
import typing
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from typing import Optional

import feedparser
import requests
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import (
    chunks, request_with_retries, construct_request_dict)
from core.base.types import GalleryData, DataDict
from viewer.models import Gallery
from .utilities import link_from_gid_token_fjord, map_external_gallery_data_to_internal, \
    get_gid_token_from_link, fjord_gid_token_from_link, SearchHTMLParser
from . import constants
from . import utilities

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.ex_page_short, constants.ge_page_short, constants.rss_url]

    # Panda only methods
    def get_galleries_from_page_links(self, page_links: Iterable[str], page_links_results: list[DataDict]) -> None:

        api_page_links = []

        for page_link in page_links:

            m = re.search(r'(.+)/s/(\w+)/(\d+)-(\d+)', page_link)
            if not m:
                continue
            api_page_links.append(
                {'data': [m.group(3), m.group(2), m.group(4)]})

        api_page_links_chunks = list(chunks(api_page_links, 25))

        for i, group in enumerate(api_page_links_chunks):

            if i % 3 == 2:
                time.sleep(self.own_settings.wait_timer)

            data = {
                'method': 'gtoken',
                'pagelist': [x['data'] for x in group]}

            headers = {'Content-Type': 'application/json'}

            request_dict = construct_request_dict(self.settings, self.own_settings)
            request_dict['headers'] = {**headers, **self.settings.requests_headers}
            request_dict['data'] = json.dumps(data)

            response = request_with_retries(
                constants.ge_api_url,
                request_dict,
                post=True,
            )

            if not response:
                continue
            try:
                response_data = response.json()
            except(ValueError, KeyError):
                logger.error("Could not parse response to JSON: {}".format(response.text))
                continue

            for gid_token_pair in response_data['tokenlist']:

                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery_id=gid_token_pair['gid'],
                    link=link_from_gid_token_fjord(gid_token_pair['gid'], gid_token_pair['token'], False)
                )

                if discard_approved:
                    if not self.settings.silent_processing:
                        logger.info(discard_message)
                    continue

                page_links_results.append(
                    {'data': (gid_token_pair['gid'], gid_token_pair['token']),
                     'link': link_from_gid_token_fjord(gid_token_pair['gid'], gid_token_pair['token'], False)})

    def get_galleries_from_main_page_link(self, url: str) -> set[str]:

        unique_urls = set()

        while True:

            parsed = urllib.parse.urlparse(url)
            query = urllib.parse.parse_qs(parsed.query)
            if 'page' in query:
                current_page = int(query['page'][0])
            else:
                params = {'page': ['0']}
                query.update(params)
                new_query = urllib.parse.urlencode(query, doseq=True)
                url = urllib.parse.urlunparse(
                    list(parsed[0:4]) + [new_query] + list(parsed[5:]))
                current_page = 0

            request_dict = construct_request_dict(self.settings, self.own_settings)

            main_page_text = requests.get(
                url,
                **request_dict
            ).text

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = request_with_retries(
                url,
                request_dict,
                post=False,
            )

            if not response:
                logger.info("Got no response, stopping")
                break

            response.encoding = 'utf-8'

            if 'No hits found' in response.text:
                logger.info("Empty page found, ending")
                break
            else:
                logger.info(
                    "Got content on search page {}, looking for galleries and jumping "
                    "to the next page. Link: {}".format(current_page, url)
                )
                main_page_parser = SearchHTMLParser()
                main_page_parser.feed(main_page_text)
                logger.info("Number of galleries found: {}".format(len(main_page_parser.galleries)))
                if len(main_page_parser.galleries) >= 1:
                    for gallery_url in main_page_parser.galleries:
                        unique_urls.add(gallery_url)
                else:
                    logger.info("Empty page found, ending")
                    break
                if 0 < self.own_settings.stop_page_number <= current_page:
                    logger.info(
                        "Got to stop page number: {}, "
                        "ending (setting: provider.stop_page_number).".format(self.own_settings.stop_page_number)
                    )
                    break
                current_page += 1
                params = {'page': [str(current_page)]}
                query.update(params)
                new_query = urllib.parse.urlencode(query, doseq=True)
                url = urllib.parse.urlunparse(
                    list(parsed[0:4]) + [new_query] + list(parsed[5:]))
                time.sleep(self.own_settings.wait_timer)

        return unique_urls

    def get_values_from_gallery_link_list(self, url_list: Iterable[str]) -> list[GalleryData]:

        gid_token_chunks = list(chunks([get_gid_token_from_link(link) for link in url_list], 25))

        galleries_data = []

        for i, group in enumerate(gid_token_chunks):

            if i % 3 == 2:
                time.sleep(self.own_settings.wait_timer)
            if not self.settings.silent_processing:
                logger.info(
                    "Calling fjord API ({}). "
                    "Gallery group: {}, galleries in group: {}, total groups: {}".format(
                        self.name,
                        i + 1,
                        len(group),
                        len(gid_token_chunks)
                    )
                )

            data = utilities.request_data_from_gid_token_iterable(group)

            headers = {'Content-Type': 'application/json'}

            request_dict = construct_request_dict(self.settings, self.own_settings)
            request_dict['headers'] = {**headers, **self.settings.requests_headers}
            request_dict['data'] = json.dumps(data)

            response = request_with_retries(
                constants.ge_api_url,
                request_dict,
                post=True,
            )

            if not response:
                continue

            try:
                response_data = response.json()
            except(ValueError, KeyError):
                logger.error("Could not parse response to JSON: {}".format(response.text))
                continue

            for gallery_data in response_data['gmetadata']:
                if 'error' in gallery_data:
                    logger.error(
                        "Fetching gallery {}: "
                        "failed with error: {}".format(gallery_data['gid'], gallery_data['error'])
                    )
                    continue
                internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
                m = re.search(constants.default_fjord_tags, ",".join(internal_gallery_data.tags))
                if m:
                    internal_gallery_data.fjord = True
                else:
                    internal_gallery_data.fjord = False
                galleries_data.append(internal_gallery_data)

        return galleries_data

    def get_values_from_gallery_link(self, link: str) -> Optional[GalleryData]:

        fjord, gid, token = fjord_gid_token_from_link(link)

        if fjord is None or gid is None or token is None:
            return None

        data = utilities.request_data_from_gid_token_iterable([(gid, token)])

        headers = {'Content-Type': 'application/json'}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict['headers'] = {**headers, **self.settings.requests_headers}
        request_dict['data'] = json.dumps(data)

        response = request_with_retries(
            constants.ge_api_url,
            request_dict,
            post=True,
        )

        if not response:
            return None
        try:
            response_data = response.json()
        except(ValueError, KeyError):
            logger.error("Could not parse response to JSON: {}".format(response.text))
            return None
        for gallery_data in response_data['gmetadata']:
            if 'error' in gallery_data:
                logger.error(
                    "Adding gallery {}: "
                    "failed with error: {}".format(gallery_data['gid'], gallery_data['error'])
                )
                return None
            internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
            return internal_gallery_data
        return None

    def get_feed_urls(self) -> list[str]:
        return [constants.rss_url, ]

    def crawl_feed(self, feed_url: str = '') -> list[str]:

        urls: list[str] = []

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
            return urls

        response.encoding = 'utf-8'

        feed = feedparser.parse(
            response.text
        )

        for item in feed['items']:
            if any([item['title'].startswith(category) for category in self.own_settings.accepted_rss_categories]):
                urls.append(item['link'])
        return urls

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(r'(.+)/g/(\d+)/(\w+)', url)
        if m and m.group(2):
            return m.group(2)
        else:
            return None

    def crawl_urls(self, urls: list[str], wanted_filters: QuerySet = None, wanted_only: bool = False) -> None:

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries: list[DataDict] = []
        unique_page_urls = set()
        gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

        if not self.downloaders:
            logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if constants.rss_url in url:
                feed_links = self.crawl_feed(url)
                unique_urls.update(feed_links)
                logger.info("Provided RSS URL for provider ({}), adding {} found links".format(
                    self.name,
                    len(feed_links))
                )
                continue

            if(constants.ex_page_short not in url
                    and constants.ge_page_short not in url):
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if '/g/' in url:
                if not self.settings.silent_processing:
                    logger.info("Provided URL {} is a gallery link, adding".format(url))
                unique_urls.add(url)
                continue

            if '/s/' in url:
                if not self.settings.silent_processing:
                    logger.info("Provided URL {} is a page link, adding".format(url))
                unique_page_urls.add(url)
                continue

            # Do not crawl main page links if they were submitted anonymously, to prevent spam.
            if len(self.downloaders) == 1 and self.downloaders[0][0].type == 'submit':
                continue

            # assuming main page URLs
            unique_urls.update(self.get_galleries_from_main_page_link(url))

        gallery_ids = []
        found_galleries = set()
        total_galleries_filtered = []
        for gallery_url in unique_urls:

            m = re.search(r'(.+)/g/(\d+)/(\w+)', gallery_url)
            if m:
                gallery_ids.append(m.group(2))
                total_galleries_filtered.append((gallery_url, m.group(1), m.group(2), m.group(3)))

        for galleries_gid_group in list(chunks(gallery_ids, 900)):
            for found_gallery in Gallery.objects.filter(gid__in=galleries_gid_group):
                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery=found_gallery,
                    link=found_gallery.get_link()
                )

                if discard_approved:
                    if not self.settings.silent_processing:
                        logger.info(discard_message)
                    found_galleries.add(found_gallery.gid)

        for gallery_tuple in total_galleries_filtered:

            if gallery_tuple[2] not in found_galleries:
                fetch_format_galleries.append(
                    {
                        'data': (gallery_tuple[2], gallery_tuple[3]),
                        'root': gallery_tuple[1],
                        'link': gallery_tuple[0]
                    }
                )
                if not self.settings.silent_processing:
                    logger.info(
                        "Gallery {} will be processed. "
                        "Total galleries: {}".format(gallery_tuple[0], len(fetch_format_galleries))
                    )

        if len(unique_page_urls) > 0:
            logger.info("Getting gallery links from page links...")
            page_links_results: list[DataDict] = []
            self.get_galleries_from_page_links(unique_page_urls, page_links_results)
            fetch_format_galleries += page_links_results

        if len(fetch_format_galleries) == 0:
            logger.info("No galleries need downloading, returning.")
            return

        fetch_format_galleries_chunks = list(chunks(fetch_format_galleries, 25))
        for i, group in enumerate(fetch_format_galleries_chunks):
            # Set based on recommendation in official documentation
            if i % 3 == 2:
                time.sleep(self.own_settings.wait_timer)
            if not self.settings.silent_processing:
                logger.info(
                    "Calling non-fjord API ({}). "
                    "Gallery group: {}, galleries in group: {}, total groups: {}".format(
                        self.name,
                        i + 1,
                        len(group),
                        len(fetch_format_galleries_chunks)
                    )
                )

            data = utilities.request_data_from_gid_token_iterable([x['data'] for x in group])

            headers = {'Content-Type': 'application/json'}

            request_dict = construct_request_dict(self.settings, self.own_settings)
            request_dict['headers'] = {**headers, **self.settings.requests_headers}
            request_dict['data'] = json.dumps(data)

            response = request_with_retries(
                constants.ge_api_url,
                request_dict,
                post=True,
            )

            if not response:
                continue

            try:
                response_data = response.json()
            except(ValueError, KeyError):
                logger.error("Could not parse response to JSON: {}".format(response.text))
                continue

            for gallery_data in response_data['gmetadata']:
                if 'error' in gallery_data:
                    logger.error(
                        "Adding gallery {}: "
                        "failed with error: {}".format(gallery_data['gid'], gallery_data['error'])
                    )
                    continue
                internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
                link = link_from_gid_token_fjord(gallery_data['gid'], gallery_data['token'], False)
                internal_gallery_data.link = link

                if self.general_utils.discard_by_tag_list(internal_gallery_data.tags):
                    if not self.settings.silent_processing:
                        logger.info(
                            "Skipping gallery {}, because it's tagged with global discarded tags".format(link)
                        )
                    continue

                if wanted_filters:
                    self.compare_gallery_with_wanted_filters(
                        internal_gallery_data,
                        link,
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
