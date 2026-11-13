import logging
import re
from urllib.parse import urljoin, urlparse

import bs4
from bs4 import BeautifulSoup

from core.base.matchers import Matcher
from core.base.utilities import construct_request_dict
from core.matching.types import SearchResult
from . import constants
from .utilities import clean_title

logger = logging.getLogger(__name__)

# Spanish/French releases are discarded.
DISCARD_TITLE_PREFIXES = ("(SPANISH)", "(FRENCH)")


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
        request_dict["params"] = {"q": query}

        response = self.request(urljoin(constants.main_url, "search/"), request_dict)

        if not response:
            logger.info("Matcher {}: got no response from server".format(self))
            return SearchResult()

        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")

        matches_links = set()

        for link_container in soup.find_all("a", class_=re.compile("product-card")):
            if isinstance(link_container, bs4.element.Tag):
                title_container = link_container.find("div", class_="product-card__name")
                if isinstance(title_container, bs4.element.Tag):
                    if title_container.get_text().startswith(DISCARD_TITLE_PREFIXES):
                        continue
                    found_href = link_container["href"]
                    if isinstance(found_href, str):
                        matches_links.add(urljoin(constants.main_url, urljoin(found_href, urlparse(found_href).path)))

        return SearchResult(links=sorted(matches_links))


API = (TitleMatcher,)
