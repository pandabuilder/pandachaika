import logging
import os
from typing import Optional
from urllib.parse import urljoin, unquote

from core.base.matchers import Matcher
from core.base.types import DataDict
from core.base.utilities import request_with_retries, construct_request_dict, file_matches_any_filter, get_zip_fileinfo
from . import constants
from .utilities import clean_title

logger = logging.getLogger(__name__)


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
            return clean_title(self.get_title_from_path(file_name))
        else:
            return clean_title(file_name)

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

        xsrf_token = request_dict["cookies"].get('XSRF-TOKEN', None)

        if xsrf_token is not None:

            token_decoded = unquote(xsrf_token)
            request_dict["headers"].update({"X-XSRF-TOKEN": token_decoded})

        request_dict["headers"].update({"Origin": "https://doujin.io"})

        request_dict["params"] = {
            "keyword": title,
            "tags": [],
            "page": 1,
            "sort": "published_at",
            "sort_dir": "desc"
        }

        r = request_with_retries(
            urljoin(constants.main_url, constants.search_path),
            request_dict,
            post=True
        )

        if not r:
            logger.warning("Got no response from server")
            return False

        try:
            response_data = r.json()
        except (ValueError, KeyError):
            logger.warning("Error parsing response from server: {}".format(r.text))
            return False

        matches_links = set()

        for gallery in response_data["data"]["data"]:
            matches_links.add(urljoin(constants.main_url, "/{}/{}".format(gallery["optimus_id"], gallery["slug"])))

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (TitleMatcher,)
