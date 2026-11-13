import logging
import re

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
        return title

    def search(self, query: str) -> SearchResult:
        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = {"q": query}

        response = self.request(constants.main_page, request_dict)

        if not response:
            logger.info("Matcher {}: got no response from server".format(self))
            return SearchResult()
        response.encoding = "utf-8"

        matches_links = {
            "{}{}".format(constants.gallery_container_url, match.group(1))
            for match in re.finditer(r'a href="/view/(\d+)/*"', response.text)
        }

        return SearchResult(links=sorted(matches_links))


API = (TitleMatcher,)
