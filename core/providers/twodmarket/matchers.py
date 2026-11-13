import logging
import re
from urllib.parse import urljoin

import bs4
from bs4 import BeautifulSoup

from core.base.matchers import Matcher
from core.base.utilities import construct_request_dict
from core.matching.types import SearchResult
from . import constants
from .utilities import clean_title

logger = logging.getLogger(__name__)


class TitleMatcher(Matcher):

    name = "title"
    provider = constants.provider_name
    type = "title"
    default_cutoff = 0.6

    def clean_search_title(self, title: str) -> str:
        return clean_title(title)

    def clean_compare_title(self, title: str) -> str:
        return clean_title(title)

    def search(self, query: str) -> SearchResult:
        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = {"type": "title", "search_value": query}

        response = self.request(urljoin(constants.main_url, "Search"), request_dict)

        if not response:
            logger.info("Matcher {}: got no response from server".format(self))
            return SearchResult()

        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        matches_links = set()

        for gallery in soup.find_all("div", class_=re.compile("showcase_comics_product_image_box")):
            if isinstance(gallery, bs4.element.Tag):
                link_container = gallery.find("a")
                if isinstance(link_container, bs4.element.Tag) and isinstance(link_container["href"], str):
                    matches_links.add(urljoin(constants.main_url, link_container["href"]))

        return SearchResult(links=sorted(matches_links))


API = (TitleMatcher,)
