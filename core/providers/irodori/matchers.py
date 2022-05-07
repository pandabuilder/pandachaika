import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core.base.matchers import Matcher
from core.base.types import DataDict
from core.base.utilities import (
    filecount_in_zip,
    get_zip_filesize,
    request_with_retries, construct_request_dict, file_matches_any_filter)
from . import constants
from .utilities import clean_title

logger = logging.getLogger(__name__)


class TitleMatcher(Matcher):

    name = 'title'
    provider = constants.provider_name
    type = 'title'
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
        values = {
            'title': self.match_title,
            'title_jpn': '',
            'zipped': self.file_path,
            'crc32': self.crc32,
            'match_type': self.found_by,
            'filesize': get_zip_filesize(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'filecount': filecount_in_zip(os.path.join(self.settings.MEDIA_ROOT, self.file_path)),
            'source_type': self.provider
        }

        return values

    def compare_by_title(self, title: str) -> bool:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        request_dict['params'] = {'q': title}

        r = request_with_retries(
            urljoin(constants.main_url, 'search/'),
            request_dict,
        )

        if not r:
            logger.info("Got no response from server")
            return False

        r.encoding = 'utf-8'
        soup_1 = BeautifulSoup(r.text, 'html.parser')

        matches_links = set()

        discard_titles = ["(SPANISH)", "(FRENCH)"]

        for link_container in soup_1.find_all("a", class_=re.compile("product-card")):
            # Discard spanish/french releases
            title_container = link_container.find("div", class_="product-card__name")
            if any(x for x in discard_titles if title_container.get_text().startswith(x)):
                continue
            matches_links.add(urljoin(constants.main_url, urljoin(link_container['href'], urlparse(link_container['href']).path)))

        self.gallery_links = list(matches_links)
        if len(self.gallery_links) > 0:
            self.found_by = self.name
            return True
        else:
            return False


API = (
    TitleMatcher,
)
