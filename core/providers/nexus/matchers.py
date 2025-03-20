import os
import re
from typing import Optional

from core.base.matchers import Matcher
from core.base.types import DataDict
from core.base.utilities import request_with_retries, construct_request_dict, file_matches_any_filter, get_zip_fileinfo
from . import constants
from .utilities import clean_title


class TitleMatcher(Matcher):

    name = "title"
    provider = constants.provider_name
    type = "title"
    time_to_wait_after_compare = 0
    default_cutoff = 0.6

    def format_to_search_title(self, file_name: str) -> str:
        if file_matches_any_filter(file_name, self.settings.filename_filter):
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

    def format_to_compare_title(self, file_name: str) -> str:
        if file_matches_any_filter(file_name, self.settings.filename_filter):
            return self.get_title_from_path(file_name)
        else:
            return file_name

    def search_method(self, title_to_search: str) -> bool:
        return self.compare_by_title(title_to_search)

    def format_match_values(self) -> Optional[DataDict]:

        if not self.match_values:
            return None

        self.match_gid = self.match_values.gid
        filesize, filecount, _ = get_zip_fileinfo(os.path.join(self.settings.MEDIA_ROOT, self.file_path))
        values = {
            "title": self.match_title,
            "title_jpn": "",
            "zipped": self.file_path,
            "crc32": self.crc32,
            "match_type": self.found_by,
            "filesize": filesize,
            "filecount": filecount,
            "source_type": self.provider,
        }

        return values

    def compare_by_title(self, title: str) -> bool:

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = {"q": title}

        response = request_with_retries(
            constants.main_page,
            request_dict,
            post=False,
        )

        if not response:
            return False
        response.encoding = "utf-8"

        m = re.finditer(r'a href="/view/(\d+)/*"', response.text)

        matches_links = set()

        if m:
            for match in m:
                matches_links.add("{}{}".format(constants.gallery_container_url, match.group(1)))

        self.gallery_links = list(matches_links)

        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (TitleMatcher,)
