import logging
import os
from typing import Optional

from core.base.matchers import Matcher
from core.base.types import GalleryData, DataDict
from core.base.utilities import (
    clean_title,
    request_with_retries,
    construct_request_dict,
    file_matches_any_filter,
    get_zip_fileinfo,
)
from core.providers.mugimugi.utilities import convert_api_response_text_to_gallery_dicts
from . import constants

logger = logging.getLogger(__name__)


class TitleMatcher(Matcher):

    name = "title"
    provider = constants.provider_name
    type = "title"
    time_to_wait_after_compare = 0
    default_cutoff = 0.6

    def get_metadata_after_matching(self) -> list[GalleryData]:
        return self.values_array

    def format_to_search_title(self, file_name: str) -> str:
        if file_matches_any_filter(file_name, self.settings.filename_filter):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def format_to_compare_title(self, file_name: str) -> str:
        if file_matches_any_filter(file_name, self.settings.filename_filter):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def search_method(self, title_to_search: str) -> bool:
        return self.search_using_xml_api(title_to_search)

    def format_match_values(self) -> Optional[DataDict]:

        if not self.match_values:
            return None
        self.match_gid = self.match_values.gid
        filesize, filecount, _ = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, self.file_path))
        values = {
            "title": self.match_title,
            "title_jpn": self.match_values.title_jpn,
            "zipped": self.file_path,
            "crc32": self.crc32,
            "match_type": self.found_by,
            "filesize": filesize,
            "filecount": filecount,
            "source_type": self.provider,
        }

        return values

    def search_using_xml_api(self, title: str) -> bool:

        if not self.own_settings.api_key:
            logger.error(
                "Can't use {} API without an api key. Check {}/API_MANUAL.txt".format(self.name, constants.main_page)
            )
            return False

        page = 1
        galleries = []

        while True:
            link = "{}/api/{}/?S=objectSearch&sn={}&page={}".format(
                constants.main_page, self.own_settings.api_key, title, page
            )

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = request_with_retries(
                link,
                request_dict,
                post=False,
            )

            if not response:
                break

            response.encoding = "utf-8"
            # Based on: https://www.doujinshi.org/API_MANUAL.txt

            api_galleries = convert_api_response_text_to_gallery_dicts(response.text)

            if not api_galleries:
                break

            galleries.extend(api_galleries)

            # API returns 25 max results per query, so if we get 24 or less, means there's no more pages.
            # API Manual says 25, but we get 50 results normally!
            if len(api_galleries) < 50:
                break

            page += 1

        self.values_array = galleries

        self.gallery_links = [x.link for x in galleries if x.link]
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (TitleMatcher,)
