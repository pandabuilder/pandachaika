# -*- coding: utf-8 -*-
import logging
import re
import time
import typing
from collections import defaultdict
from datetime import datetime
from typing import Optional

import feedparser
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
    accepted_urls = [constants.no_scheme_url]

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
        new_text = re.sub(r'(<div class="right">\d+?)</b>', r'\1', response.text)

        if constants.main_url + '/magazines/' in link:
            return self.process_magazine_page(link, new_text)
        else:
            return self.process_regular_gallery_page(link, new_text)

    # This is the next best thing, since we don't have a different way to get the posted date.
    # Will only work if we crawl the gallery early.
    def parse_posted_date_from_feed(self, link: str, gid: str) -> Optional[datetime]:
        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = request_with_retries(
            link,
            request_dict,
            post=False,
        )

        if not response:
            return None

        response.encoding = 'utf-8'

        feed = feedparser.parse(
            response.text
        )

        for item in feed['items']:
            if gid in item['id']:
                return date_parser.parse(item['published'], tzinfos=constants.extra_feed_url_timezone)
        return None

    def process_magazine_page(self, link: str, response_text: str) -> Optional[GalleryData]:
        soup = BeautifulSoup(response_text, 'html.parser')
        magazine_container = soup.find("div", class_="wrap")

        comic_regex = re.compile("content-comic")

        if magazine_container:
            gid = link.replace(constants.main_url + '/', '')
            gallery = GalleryData(gid, self.name)
            if self.own_settings.get_posted_date_from_feed:
                gallery.posted = self.parse_posted_date_from_feed(constants.aux_feed_url, gallery.gid)
            gallery.link = link
            gallery.tags = []
            gallery.magazine_chapters_gids = []
            gallery.title = magazine_container.find("a", itemprop="item", href=re.compile("/" + gid)).span.get_text()
            gallery.category = 'Manga'  # We assume every magazine is commercial, and we keep using panda definition

            thumbnail_container = magazine_container.find("img", class_="content-poster")
            if thumbnail_container:
                gallery.thumbnail_url = thumbnail_container.get("src")
                if gallery.thumbnail_url and gallery.thumbnail_url.startswith('//'):
                    gallery.thumbnail_url = 'https:' + gallery.thumbnail_url

            description_container = magazine_container.find("p", class_=re.compile("attribute-description"))

            if description_container:
                comment_text = description_container.decode_contents().replace("\n", "").replace("<br/>", "\n")
                comment_soup = BeautifulSoup(comment_text, 'html.parser')
                gallery.comment = comment_soup.get_text()

            chapters_container = magazine_container.find_all("div", class_=comic_regex)

            tags_set = set()
            artists_set = set()

            for chapter_container in chapters_container:
                chapter_title_container = chapter_container.find("a", class_="content-title")
                if chapter_title_container:
                    # chapter_title = chapter_title_container.get_text()
                    chapter_link = chapter_title_container.get('href').replace(constants.main_url + '/', '')
                    chapter_gid = chapter_link[1:] if chapter_link[0] == '/' else chapter_link
                    gallery.magazine_chapters_gids.append(chapter_gid)

                tags_container = chapter_container.find("div", {"class": "tags"})

                for tag_a in tags_container.find_all("a", href=lambda x: x and '/tags/' in x):
                    tags_set.add(
                        translate_tag(tag_a.get_text().strip()))

                artist = chapter_container.find("a", href=lambda x: x and '/artists/' in x)

                if artist:
                    artists_set.add("artist:" + translate_tag(artist.get_text().strip()))

            gallery.tags = list(tags_set)
            gallery.tags.extend(list(artists_set))

            return gallery
        else:
            return None

    def process_regular_gallery_page(self, link: str, response_text: str) -> Optional[GalleryData]:

        soup = BeautifulSoup(response_text, 'html.parser')
        gallery_container = soup.find("div", class_=re.compile("content-wrap"))
        if gallery_container:
            gallery = GalleryData(link.replace(constants.main_url + '/', '').replace('manga/', 'hentai/'), self.name)
            gallery.link = link
            gallery.tags = []
            if self.own_settings.get_posted_date_from_feed:
                gallery.posted = self.parse_posted_date_from_feed(constants.aux_feed_url, gallery.gid)
            gallery.title = gallery_container.find("div", class_="content-name").h1.get_text()

            if gallery.gid.startswith('manga') or gallery.gid.startswith('hentai'):
                gallery.category = 'Manga'
            elif gallery.gid.startswith('doujinshi'):
                gallery.category = 'Doujinshi'

            thumbnail_container = gallery_container.find("img", class_="tablet-50")
            if thumbnail_container:
                gallery.thumbnail_url = thumbnail_container.get("src")
                if gallery.thumbnail_url and gallery.thumbnail_url.startswith('//'):
                    gallery.thumbnail_url = 'https:' + gallery.thumbnail_url

            is_doujinshi = False
            for gallery_row in gallery_container.find_all("div", {"class": "row"}):
                left_text = gallery_row.find("div", {"class": "row-left"}).get_text()
                right_div = gallery_row.find("div", {"class": "row-right"})
                if left_text == "Series" or left_text == "Parody":
                    right_text = right_div.get_text().strip()
                    # if not right_text == "Original Work":
                    gallery.tags.append(
                        translate_tag("parody:" + right_text))
                elif left_text == "Artist":
                    for artist in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("artist:" + artist.get_text().strip())
                        )
                elif left_text == "Author":
                    for author in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("author:" + author.get_text().strip())
                        )
                elif left_text == "Magazine":
                    gallery.tags.append(
                        translate_tag("magazine:" + right_div.get_text().strip()))
                    belongs_to_magazine = right_div.find("a")
                    if belongs_to_magazine:
                        gallery.magazine_gid = belongs_to_magazine.get("href")[1:]
                elif left_text == "Publisher":
                    gallery.tags.append(
                        translate_tag("publisher:" + right_div.get_text().strip()))
                elif left_text == "Circle":
                    gallery.tags.append(
                        translate_tag("group:" + right_div.get_text().strip()))
                elif left_text == "Event":
                    gallery.tags.append(
                        translate_tag("event:" + right_div.get_text().strip()))
                elif left_text == "Book":
                    belongs_to_container = right_div.find("a")
                    if belongs_to_container:
                        gallery.gallery_container_gid = belongs_to_container.get("href")[1:]
                elif left_text == "Language":
                    gallery.tags.append(
                        translate_tag("language:" + right_div.get_text().strip()))
                elif left_text == "Pages":
                    right_text = right_div.get_text().strip()
                    m = re.search(r'^(\d+)', right_text)
                    if m:
                        gallery.filecount = int(m.group(1))
                elif left_text == "Uploader":
                    gallery.uploader, right_date_text = right_div.get_text().strip().split(' on ')
                    right_date_text = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', right_date_text)
                    gallery.posted = datetime.strptime(right_date_text, "%B %d, %Y")
                elif left_text == "Description":
                    gallery.comment = right_div.get_text()
                elif left_text == "Tags":
                    for tag_a in right_div.find_all("a", href=lambda x: x and '/tags/' in x):
                        if tag_a.get_text().strip() == 'doujin':
                            is_doujinshi = True
                        gallery.tags.append(
                            translate_tag(tag_a.get_text().strip()))
            if is_doujinshi:
                gallery.category = 'Doujinshi'
            else:
                gallery.category = 'Manga'

            return gallery
        else:
            return None

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

    # We disable FAKKU json here (single point) until it's enabled again.
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
            url = url.replace('/manga/', '/hentai/')
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

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (
    Parser,
)
