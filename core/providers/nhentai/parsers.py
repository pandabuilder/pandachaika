# -*- coding: utf-8 -*-
import re
import time
from collections import defaultdict
from typing import Dict, Any

import dateutil.parser
from bs4 import BeautifulSoup

from core.base.parsers import BaseParser
from core.base.utilities import translate_tag, request_with_retries
from . import constants
from . import utilities


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.gallery_container_url]

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
        soup = BeautifulSoup(response.text, 'html.parser')

        if soup:
            title_jpn_match = soup.find("div", id=re.compile("info")).h2

            gallery: Dict[str, Any] = {
                'gid': 'nh-' + re.search(r'{}(\d+)'.format(constants.gallery_container_url), link).group(1),
                'title': soup.h1.get_text(),
                'title_jpn': title_jpn_match.get_text() if title_jpn_match else '',
                'filecount': int(re.search('<div>(\d+) page(s*)</div>', response.text).group(1)),
                'tags': [],
                'provider': self.name,
                'link': link,
                'posted': dateutil.parser.parse(soup.find("time")['datetime'])
            }

            for tag_container in soup.find_all("a", {"class": "tag"}):
                tag_name = [text for text in tag_container.stripped_strings][0]
                tag_name = tag_name.split(" | ")[0]
                tag_scope = tag_container.parent.parent.get_text()
                tag_ext = tag_container.parent.get_text()
                tag_scope = tag_scope.replace(tag_ext, "").replace("\t", "").replace("\n", "").replace(":", "").lower()
                if tag_scope == 'tags':
                    gallery['tags'].append(translate_tag(tag_name))
                elif tag_scope == 'categories':
                    gallery['category'] = tag_name.capitalize()
                else:
                    gallery['tags'].append(translate_tag(tag_scope + ":" + tag_name))

        else:
            gallery = None
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

    def fetch_gallery_data(self, url):
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list):
        return self.get_values_from_gallery_link_list(url_list)

    @staticmethod
    def id_from_url(url):
        m = re.search('(.+)/g/(\d+)/*(\d*)', url)
        if m and m.group(2):
            return 'nh-' + m.group(2)
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

            if not(constants.gallery_container_url in url):
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

            fetch_format_galleries.append(gallery)

        if len(fetch_format_galleries) == 0:
            self.logger.info("No galleries need downloading, returning.")
            return

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

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    Parser,
)
