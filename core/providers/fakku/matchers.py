import logging
import re
from urllib.parse import urljoin, quote

import bs4
from bs4 import BeautifulSoup

from core.base.matchers import Matcher
from core.base.utilities import construct_request_dict
from core.matching.types import SearchResult
from . import constants
from .utilities import clean_title, clean_for_online_search_title

logger = logging.getLogger(__name__)


class TitleMatcher(Matcher):

    name = "title"
    provider = constants.provider_name
    type = "title"
    default_cutoff = 0.6

    def clean_search_title(self, title: str) -> str:
        return clean_for_online_search_title(title)

    def clean_compare_title(self, title: str) -> str:
        return clean_title(title)

    def search(self, query: str) -> SearchResult:
        full_url = urljoin(constants.main_url, "search/") + quote(query)

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = self.request(full_url, request_dict)

        if not response:
            logger.info("Matcher {}: got no response from server".format(self))
            return SearchResult()

        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        matches_links = set()

        for chapter_container in soup.find_all("div", class_=re.compile("overflow-hidden relative")):
            if isinstance(chapter_container, bs4.element.Tag):
                chapter_title_container = chapter_container.find("a")
                if isinstance(chapter_title_container, bs4.element.Tag):
                    found_href = chapter_title_container.get("href")
                    if isinstance(found_href, str):
                        matches_links.add(constants.main_url + found_href)

        return SearchResult(links=sorted(matches_links))


API = (TitleMatcher,)
