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
        magazine_container = soup.find("div", class_="grid")

        comic_regex = re.compile("col-comic")

        if magazine_container:
            gid = link.replace(constants.main_url + '/', '')
            gallery = GalleryData(gid, self.name)
            if self.own_settings.get_posted_date_from_feed:
                gallery.posted = self.parse_posted_date_from_feed(constants.aux_feed_url, gallery.gid)
            gallery.link = link
            gallery.provider_metadata = response_text
            gallery.tags = []
            gallery.magazine_chapters_gids = []
            gallery_title_container = magazine_container.find("ol", class_="table-cell")
            if gallery_title_container:
                possible_titles = list(gallery_title_container.findAll("li", itemprop="itemListElement"))
                if possible_titles:
                    gallery.title = possible_titles[-1].find("span", itemprop="name").get_text()
            gallery.category = 'Manga'  # We assume every magazine is commercial, and we keep using panda definition

            thumbnail_container = magazine_container.find("img", class_="object-cover")
            if thumbnail_container:
                gallery.thumbnail_url = thumbnail_container.get("src")
                if gallery.thumbnail_url and gallery.thumbnail_url.startswith('//'):
                    gallery.thumbnail_url = 'https:' + gallery.thumbnail_url

            description_container = soup.find("div", class_=re.compile("flex-auto align-top space-y-4 text-left"))

            if description_container:
                comment_text = description_container.decode_contents().replace("\n", "").replace("<br/>", "\n")
                comment_soup = BeautifulSoup(comment_text, 'html.parser')
                gallery.comment = comment_soup.get_text()

            chapters_container = magazine_container.find_all("div", class_=comic_regex)

            # tags_set = set()
            artists_set = set()

            for chapter_container in chapters_container:
                chapter_title_container = chapter_container.find("a", class_=re.compile("text-lg text-brand-light font-semibold"))
                if chapter_title_container:
                    chapter_link = chapter_title_container.get('href').replace(constants.main_url + '/', '')
                    chapter_gid = chapter_link[1:] if chapter_link[0] == '/' else chapter_link
                    gallery.magazine_chapters_gids.append(chapter_gid)

                # Note: They removed tags per chapter, so we can't aggregate this info.
                # tags_container = chapter_container.find("div", {"class": "tags"})
                # if tags_container:
                #
                #     for tag_a in tags_container.find_all("a", href=lambda x: x and '/tags/' in x):
                #         tags_set.add(
                #             translate_tag(tag_a.get_text().strip()))

                artist = chapter_container.find("a", href=lambda x: x and '/artists/' in x)

                if artist:
                    artists_set.add("artist:" + translate_tag(artist.get_text().strip()))

            # gallery.tags = list(tags_set)
            gallery.tags.extend(list(artists_set))

            return gallery
        else:
            return None

    def process_regular_gallery_page(self, link: str, response_text: str) -> Optional[GalleryData]:

        soup = BeautifulSoup(response_text, 'html.parser')
        gallery_container = soup.find("div", class_=re.compile("relative w-full table"))
        if gallery_container:
            gallery = GalleryData(link.replace(constants.main_url + '/', '').replace('manga/', 'hentai/'), self.name)
            gallery.link = link
            gallery.provider_metadata = response_text
            gallery.tags = []
            if self.own_settings.get_posted_date_from_feed:
                gallery.posted = self.parse_posted_date_from_feed(constants.aux_feed_url, gallery.gid)
            gallery.title = gallery_container.find("h1", class_="block col-span-full text-3xl py-2 font-bold text-brand-light text-left dark:text-white dark:link:text-white").get_text()

            description_container = soup.find("meta", property="og:description")
            if description_container:
                gallery.comment = description_container['content'].strip()
            thumbnail_container = soup.find("meta", property="og:image")
            if thumbnail_container:
                gallery.thumbnail_url = thumbnail_container['content']

            if gallery.gid.startswith('manga') or gallery.gid.startswith('hentai'):
                gallery.category = 'Manga'
            elif gallery.gid.startswith('doujinshi'):
                gallery.category = 'Doujinshi'

            is_doujinshi = False
            for gallery_row in gallery_container.find_all("div", {"class": "table text-sm w-full"}):
                left_container = gallery_row.find("div", class_=re.compile("inline-block w-24 text-left"))
                if left_container:
                    left_text = left_container.get_text()
                else:
                    left_text = ''
                right_div = gallery_row.find("div", class_=re.compile("table-cell w-full"))
                if left_text == "Series" or left_text == "Parody":
                    for parody in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("parody:" + parody.get_text().strip())
                        )
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
                    for magazine in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("magazine:" + magazine.get_text().strip())
                        )
                        # TODO: Since we only support many-to-1 gallery magazine/contained, we just link the first one.
                        # Both should be migrated to many-to-many.
                        gallery.magazine_gid = magazine.get("href")[1:]
                elif left_text == "Publisher":
                    for current_tag in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("publisher:" + current_tag.get_text().strip())
                        )
                elif left_text == "Circle":
                    for group_tag in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("group:" + group_tag.get_text().strip())
                        )
                elif left_text == "Event":
                    for current_tag in right_div.find_all("a"):
                        gallery.tags.append(
                            translate_tag("event:" + current_tag.get_text().strip())
                        )
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
                elif left_text == "":
                    for tag_a in right_div.find_all("a", href=lambda x: x and '/tags/' in x):
                        if tag_a.get_text().strip() == 'doujin':
                            is_doujinshi = True
                        gallery.tags.append(translate_tag(tag_a.get_text().strip()))
                    if right_div.find_all("a", href=lambda x: x and '/unlimited' == x):
                        gallery.tags.append(translate_tag('unlimited'))
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
