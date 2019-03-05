# -*- coding: utf-8 -*-
import copy
import json
import re
import time
import typing
import urllib.parse
from collections import defaultdict
from typing import Optional, Tuple, Iterable, List, Dict, Set
from urllib.request import ProxyHandler

import feedparser
import requests
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import (
    chunks, request_with_retries)
from core.base.types import GalleryData, DataDict
from viewer.models import Gallery, Archive, FoundGallery
from viewer.signals import wanted_gallery_found
from .utilities import link_from_gid_token_fjord, map_external_gallery_data_to_internal, \
    get_gid_token_from_link, fjord_gid_token_from_link, GalleryHTMLParser, SearchHTMLParser
from . import constants
from . import utilities

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.ex_page_short, constants.ge_page_short, constants.rss_url]

    # Panda only methods
    def get_galleries_from_page_links(self, page_links: Iterable[str], page_links_results: List[DataDict]) -> None:

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
                time.sleep(self.settings.wait_timer)

            data = {
                'method': 'gtoken',
                'pagelist': [x['data'] for x in group]}

            headers = {'Content-Type': 'application/json'}

            response = request_with_retries(
                constants.ge_api_url,
                {
                    'data': json.dumps(data),
                    'headers': {**headers, **self.settings.requests_headers},
                    'timeout': self.settings.timeout_timer
                },
                post=True,
                logger=self.logger
            )

            if not response:
                continue
            try:
                response_data = response.json()
            except(ValueError, KeyError):
                self.logger.error("Error parsing response to JSON: {}".format(response.text))
                continue

            for gid_token_pair in response_data['tokenlist']:

                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery_id=gid_token_pair['gid'],
                    link=link_from_gid_token_fjord(gid_token_pair['gid'], gid_token_pair['token'], False)
                )

                if discard_approved:
                    if not self.settings.silent_processing:
                        self.logger.info(discard_message)
                    continue

                page_links_results.append(
                    {'data': (gid_token_pair['gid'], gid_token_pair['token']),
                     'link': link_from_gid_token_fjord(gid_token_pair['gid'], gid_token_pair['token'], False)})

    def get_galleries_from_main_page_link(self, url: str) -> Set[str]:

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

            main_page_text = requests.get(
                url,
                cookies=self.own_settings.cookies,
                headers=self.settings.requests_headers,
                timeout=self.settings.timeout_timer
            ).text

            response = request_with_retries(
                url,
                {
                    'headers': self.settings.requests_headers,
                    'timeout': self.settings.timeout_timer,
                    'cookies': self.own_settings.cookies
                },
                post=False,
                logger=self.logger
            )

            if not response:
                self.logger.info("Got no response, stopping")
                break

            response.encoding = 'utf-8'

            if 'No hits found' in response.text:
                self.logger.info("Empty page found, ending")
                break
            else:
                self.logger.info(
                    "Got content on search page {}, looking for galleries and jumping "
                    "to the next page. Link: {}".format(current_page, url)
                )
                main_page_parser = SearchHTMLParser()
                main_page_parser.feed(main_page_text)
                self.logger.info("number of galleries found: {}".format(len(main_page_parser.galleries)))
                if len(main_page_parser.galleries) >= 1:
                    for gallery_url in main_page_parser.galleries:
                        unique_urls.add(gallery_url)
                if 0 < self.own_settings.stop_page_number <= current_page:
                    self.logger.info(
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
                time.sleep(self.settings.wait_timer)

        return unique_urls

    def get_final_gallery_from_link(self, link: str) -> Tuple[int, Optional[GalleryData]]:

        time.sleep(self.settings.wait_timer)
        gallery_page_text = requests.get(
            link,
            cookies=self.own_settings.cookies,
            headers=self.settings.requests_headers,
            timeout=self.settings.timeout_timer
        ).text
        fjord, gid, token = fjord_gid_token_from_link(link)
        time.sleep(self.settings.wait_timer)
        gallery = self.fetch_gallery_data(link)
        if not gallery or not gallery.token:
            return 0, None
        discard_approved, discard_message = self.discard_gallery_by_internal_checks(
            gallery_id=gid,
            link=link
        )

        if discard_approved:
            if not self.settings.silent_processing:
                self.logger.info(discard_message)
            return 2, None

        gallery.link = link
        if fjord:
            gallery.root = constants.ex_page
        else:
            gallery.root = constants.ge_page
        if 'Gallery Not Available' in gallery_page_text:
            if not fjord:
                time.sleep(self.settings.wait_timer)
                gallery.root = constants.ex_page
                gallery.link = link_from_gid_token_fjord(gallery.gid, gallery.token, True)
                gallery_page_text = requests.get(
                    gallery.link,
                    cookies=self.own_settings.cookies,
                    headers=self.settings.requests_headers,
                    timeout=self.settings.timeout_timer
                ).text
                time.sleep(self.settings.wait_timer)

        gallery_parser = GalleryHTMLParser()
        gallery_parser.feed(gallery_page_text)
        if gallery_parser.found_non_final_gallery == 2:
            return self.get_final_gallery_from_link(gallery_parser.non_final_gallery)
        return 1, gallery

    def get_values_from_gallery_link_list(self, url_list: Iterable[str]) -> List[GalleryData]:

        gid_token_chunks = list(chunks([get_gid_token_from_link(link) for link in url_list], 25))

        galleries_data = []

        for i, group in enumerate(gid_token_chunks):

            if i % 3 == 2:
                time.sleep(self.settings.wait_timer)
            if not self.settings.silent_processing:
                self.logger.info(
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

            response = request_with_retries(
                constants.ex_api_url,
                {
                    'data': json.dumps(data),
                    'headers': {**headers, **self.settings.requests_headers},
                    'cookies': self.own_settings.cookies,
                    'timeout': self.settings.timeout_timer
                },
                post=True,
                logger=self.logger
            )

            if not response:
                continue

            try:
                response_data = response.json()
            except(ValueError, KeyError):
                self.logger.error("Error parsing response to JSON: {}".format(response.text))
                continue

            for gallery_data in response_data['gmetadata']:
                if 'error' in gallery_data:
                    self.logger.error(
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

        if fjord:
            api_page = constants.ex_api_url
        else:
            api_page = constants.ge_api_url

        data = utilities.request_data_from_gid_token_iterable([(gid, token)])

        headers = {'Content-Type': 'application/json'}

        response = request_with_retries(
            api_page,
            {
                'data': json.dumps(data),
                'headers': {**headers, **self.settings.requests_headers},
                'cookies': self.own_settings.cookies,
                'timeout': self.settings.timeout_timer
            },
            post=True,
            logger=self.logger
        )

        if not response:
            return None
        try:
            response_data = response.json()
        except(ValueError, KeyError):
            self.logger.error("Error parsing response to JSON: {}".format(response.text))
            return None
        for gallery_data in response_data['gmetadata']:
            if 'error' in gallery_data:
                self.logger.error(
                    "Adding gallery {}: "
                    "failed with error: {}".format(gallery_data['gid'], gallery_data['error'])
                )
                return None
            internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
            return internal_gallery_data
        return None

    @staticmethod
    def get_feed_urls() -> List[str]:
        return [constants.rss_url, ]

    def crawl_feed(self, feed_url: str = None) -> List[str]:

        urls = []

        if not feed_url:
            feed_url = constants.rss_url
        feed = feedparser.parse(
            feed_url,
            handlers=ProxyHandler,
            request_headers=self.settings.requests_headers
        )

        for item in feed['items']:
            if any([item['title'].startswith(category) for category in self.own_settings.accepted_rss_categories]):
                urls.append(item['link'])
        return urls

    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: List[str]) -> List[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        m = re.search(r'(.+)/g/(\d+)/(\w+)', url)
        if m and m.group(2):
            return m.group(2)
        else:
            return None

    def crawl_urls(self, urls: List[str], wanted_filters: QuerySet = None, wanted_only: bool = False) -> None:

        unique_urls = set()
        gallery_data_list = []
        fetch_format_galleries: List[DataDict] = []
        unique_page_urls = set()
        gallery_wanted_lists: Dict[str, List['WantedGallery']] = defaultdict(list)

        if not self.downloaders:
            self.logger.warning('No downloaders enabled, returning.')
            return

        for url in urls:

            if constants.rss_url in url:
                feed_links = self.crawl_feed(url)
                unique_urls.update(feed_links)
                self.logger.info("Provided RSS URL, adding {} found links".format(len(feed_links)))
                continue

            if(constants.ex_page_short not in url
                    and constants.ge_page_short not in url):
                self.logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if '/g/' in url:
                if not self.settings.silent_processing:
                    self.logger.info("Provided URL {} is a gallery link, adding".format(url))
                unique_urls.add(url)
                continue

            if '/s/' in url:
                if not self.settings.silent_processing:
                    self.logger.info("Provided URL {} is a page link, adding".format(url))
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
                        self.logger.info(discard_message)
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
                    self.logger.info(
                        "Gallery {} will be processed. "
                        "Total galleries: {}".format(gallery_tuple[0], len(fetch_format_galleries))
                    )

        if len(unique_page_urls) > 0:
            self.logger.info("Getting gallery links from page links...")
            page_links_results: List[DataDict] = []
            self.get_galleries_from_page_links(unique_page_urls, page_links_results)
            fetch_format_galleries += page_links_results

        if len(fetch_format_galleries) == 0:
            self.logger.info("No galleries need downloading, returning.")
            return

        fetch_format_galleries_chunks = list(chunks(fetch_format_galleries, 25))
        fjord_galleries = []
        for i, group in enumerate(fetch_format_galleries_chunks):
            # Set based on recommendation in official documentation
            if i % 3 == 2:
                time.sleep(self.settings.wait_timer)
            if not self.settings.silent_processing:
                self.logger.info(
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

            response = request_with_retries(
                constants.ge_api_url,
                {
                    'data': json.dumps(data),
                    'headers': {**headers, **self.settings.requests_headers},
                    'timeout': self.settings.timeout_timer
                },
                post=True,
                logger=self.logger
            )

            if not response:
                continue

            try:
                response_data = response.json()
            except(ValueError, KeyError):
                self.logger.error("Error parsing response to JSON: {}".format(response.text))
                continue

            for gallery_data in response_data['gmetadata']:
                if 'error' in gallery_data:
                    self.logger.error(
                        "Adding gallery {}: "
                        "failed with error: {}".format(gallery_data['gid'], gallery_data['error'])
                    )
                    continue
                internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
                link = link_from_gid_token_fjord(gallery_data['gid'], gallery_data['token'], False)

                if self.general_utils.discard_by_tag_list(internal_gallery_data.tags):
                    if not self.settings.silent_processing:
                        self.logger.info(
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

                m = re.search(constants.default_fjord_tags, ",".join(internal_gallery_data.tags))

                if m and self.own_settings.cookies:
                    fjord_galleries.append(link_from_gid_token_fjord(gallery_data['gid'], gallery_data['token'], True))
                else:
                    gallery_data_list.append(internal_gallery_data)

        fjord_galleries_data = self.fetch_multiple_gallery_data(fjord_galleries)

        if fjord_galleries_data:
            gallery_data_list.extend(fjord_galleries_data)

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)

    def work_gallery_data(self, gallery: GalleryData, gallery_wanted_lists) -> None:

        if not gallery.token:
            return

        m = re.search(constants.default_fjord_tags, ",".join(gallery.tags))
        if m:
            gallery.root = constants.ex_page
            gallery.link = link_from_gid_token_fjord(gallery.gid, gallery.token, True)
        else:
            gallery.root = constants.ge_page
            gallery.link = link_from_gid_token_fjord(gallery.gid, gallery.token, False)

        self.logger.info("Title: {}. Link: {}".format(gallery.title, gallery.link))

        gallery_is_hidden = False
        gallery.hidden = False

        if self.own_settings.crawl_gallery_page:
            gallery_page_text = requests.get(
                gallery.link,
                cookies=self.own_settings.cookies,
                headers=self.settings.requests_headers,
                timeout=self.settings.timeout_timer
            ).text

            if 'Gallery Not Available' in gallery_page_text:
                if gallery.root == constants.ex_page:
                    self.logger.warning('EX Gallery not available, probably taken down/hidden')
                    gallery_is_hidden = True
                    gallery.hidden = True
                elif gallery.root == constants.ge_page:
                    time.sleep(self.settings.wait_timer)
                    gallery.root = constants.ex_page
                    gallery.link = link_from_gid_token_fjord(gallery.gid, gallery.token, True)
                    retry_gallery_page_text = requests.get(
                        gallery.link,
                        cookies=self.own_settings.cookies,
                        headers=self.settings.requests_headers,
                        timeout=self.settings.timeout_timer).text
                    if 'Gallery Not Available' in retry_gallery_page_text:
                        self.logger.warning(
                            'Tried EX gallery instead of E-H for '
                            'arbitrary hidden galleries, also not '
                            'available, probably taken down/hidden'
                        )
                        gallery_is_hidden = True
                        gallery.hidden = True
                    else:
                        self.logger.info("Changed from E-H to EX, was a arbitrary hidden gallery")
                        gallery_page_text = retry_gallery_page_text
                        time.sleep(self.settings.wait_timer)
                        new_gallery = self.fetch_gallery_data(gallery.link)
                        if new_gallery and new_gallery.token:
                            gallery = new_gallery
                            gallery.root = constants.ex_page
                            gallery.link = link_from_gid_token_fjord(new_gallery.gid, new_gallery.token, True)

            time.sleep(self.settings.wait_timer)

            if not gallery_is_hidden:
                gallery_parser = GalleryHTMLParser()
                gallery_parser.feed(gallery_page_text)

                # What this does is log a parent gallery if we are downloading a newer one.
                # Maybe this should be an option, because we are deleting files from disk.
                if gallery_parser.found_parent_gallery:
                    parent_gid, parent_token = get_gid_token_from_link(gallery_parser.parent_gallery)
                    archives = Archive.objects.filter(
                        gallery__gid__exact=parent_gid,
                        gallery__token__exact=parent_token
                    )
                    for archive in archives:
                        self.logger.warning(
                            "Gallery: {} has a parent gallery {}, "
                            "which is matched with archive: {}. You might want to delete them.".format(
                                gallery.link,
                                archive.gallery.get_absolute_url(),
                                archive.get_absolute_url()
                            )
                        )

                if(gallery_parser.found_non_final_gallery == 2
                        and self.own_settings.get_newer_gallery):
                    self.logger.info("Found non final gallery, next is: {}".format(gallery_parser.non_final_gallery))
                    (exists_next, new_gallery) = self.get_final_gallery_from_link(
                        gallery_parser.non_final_gallery)
                    if exists_next == 1 and new_gallery and new_gallery.link:
                        gallery.dl_type = 'skipped:non_final'
                        Gallery.objects.update_or_create_from_values(gallery)
                        discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                            gallery_id=new_gallery.gid,
                            link=new_gallery.link
                        )
                        if discard_approved:
                            if not self.settings.silent_processing:
                                self.logger.info(discard_message)
                            return
                        gallery = new_gallery
                        self.logger.info("Final gallery is: {}".format(new_gallery.link))
                    elif exists_next == 0:
                        self.logger.info(
                            "Last in sequence: {} is not available, using current one".format(gallery.link)
                        )
                    elif exists_next == 2:
                        self.logger.info(
                            "Last in sequence: {} was discarded by global tags, "
                            "skipping this gallery altogether".format(gallery.link)
                        )
                        gallery.dl_type = 'skipped:final_replaced'
                        Gallery.objects.update_or_create_from_values(gallery)
                        return

        retry_attempt = True
        while retry_attempt:

            for cnt, downloader in enumerate(self.downloaders):
                if not gallery_is_hidden or not downloader[0].skip_if_hidden:
                    downloader[0].init_download(copy.deepcopy(gallery))
                else:
                    downloader[0].return_code = 0
                if downloader[0].return_code == 1:
                    for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                        FoundGallery.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery=downloader[0].gallery_db_entry
                        )
                        if wanted_gallery.add_as_hidden and downloader[0].gallery_db_entry:
                            downloader[0].gallery_db_entry.hidden = True
                            downloader[0].gallery_db_entry.save()
                        if downloader[0].archive_db_entry and wanted_gallery.reason:
                            downloader[0].archive_db_entry.reason = wanted_gallery.reason
                            downloader[0].archive_db_entry.simple_save()

                    if len(gallery_wanted_lists[gallery.gid]) > 0:
                        wanted_gallery_found.send(
                            sender=self.settings.gallery_model,
                            gallery=downloader[0].gallery_db_entry,
                            wanted_gallery_list=gallery_wanted_lists[gallery.gid]
                        )

                    self.last_used_downloader = str(downloader[0])
                    if downloader[0].gallery_db_entry:
                        if downloader[0].archive_db_entry:
                            self.logger.info(
                                "Download completed successfully, using {}. Archive link: {}. Gallery link: {}".format(
                                    downloader[0],
                                    downloader[0].archive_db_entry.get_absolute_url(),
                                    downloader[0].gallery_db_entry.get_absolute_url()
                                )
                            )
                        else:
                            self.logger.info(
                                "Download completed successfully (gallery only), using {}. Gallery link: {}".format(
                                    downloader[0],
                                    downloader[0].gallery_db_entry.get_absolute_url()
                                )
                            )
                    return
                if(downloader[0].return_code == 0
                        and (cnt + 1) == len(self.downloaders)):
                    if gallery.root == constants.ge_page and not gallery_is_hidden and gallery.token:
                        gallery.root = constants.ex_page
                        gallery.link = link_from_gid_token_fjord(gallery.gid, gallery.token, True)
                        # fetch archiver key again when retrying.
                        new_gallery_data = self.fetch_gallery_data(gallery.link)
                        if new_gallery_data:
                            gallery.archiver_key = new_gallery_data.archiver_key
                            self.logger.info("Retrying with fjord link, might be hidden.")
                            break
                        else:
                            self.logger.error("Could not fetch fjord link.")
                    else:
                        self.logger.error("Finished retrying using fjord link.")
                    downloader[0].original_gallery = gallery
                    downloader[0].original_gallery.hidden = True
                    downloader[0].original_gallery.dl_type = 'failed'
                    downloader[0].update_gallery_db()
                    if downloader[0].gallery_db_entry:
                        self.last_used_downloader = 'none'
                        self.logger.warning("Download completed unsuccessfully, set as failed. Gallery link: {}".format(
                            downloader[0].gallery_db_entry.get_absolute_url()
                        ))
                        for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                            FoundGallery.objects.get_or_create(
                                wanted_gallery=wanted_gallery,
                                gallery=downloader[0].gallery_db_entry
                            )
                    retry_attempt = False


API = (
    Parser,
)
