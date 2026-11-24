import logging

from core.base.matchers import Matcher
from core.base.utilities import construct_request_dict
from core.matching.types import SearchResult
from . import constants, utilities

logger = logging.getLogger(__name__)


class TitleMatcher(Matcher):
    """Fuzzy title search (``/api/v1/books/match``). Its results have the full metadata, so no second fetch is needed.

    The whole title goes to the server, which takes the title out of names like
    ``(event) [circle (author)] title (parody) [lang]`` and ranks books whose circle or author appear in it higher.
    The candidates are then scored here like any other title matcher, against the title without the bracketed parts.
    """

    name = "title"
    provider = constants.provider_name
    type = "title"
    default_cutoff = 0.6

    def clean_search_title(self, title: str) -> str:
        return title.strip()

    def clean_compare_title(self, title: str) -> str:
        return utilities.title_part(title)

    def search(self, query: str) -> SearchResult:
        base = utilities.base_url(self.own_settings.url)
        if not base:
            logger.error("Matcher {}: providers.{}.general.url is not set.".format(self, self.provider))
            return SearchResult()

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = {"title": query, "limit": constants.match_limit}

        response = self.request(base + constants.match_path, request_dict)

        if not response:
            logger.info("Matcher {}: got no response from server".format(self))
            return SearchResult()

        try:
            data = response.json()
        except ValueError:
            logger.warning("Matcher {}: could not parse API response: {}".format(self, response.text[:200]))
            return SearchResult()

        if response.status_code != 200 or not isinstance(data, dict):
            logger.warning(
                "Matcher {}: HTTP {}: {}".format(
                    self, response.status_code, data.get("error") if isinstance(data, dict) else data
                )
            )
            return SearchResult()

        galleries = [
            x for x in (utilities.book_to_gallery_data(book, base) for book in data.get("results") or []) if x
        ]

        return SearchResult(links=[x.link for x in galleries if x.link], galleries=galleries)


API = (TitleMatcher,)
