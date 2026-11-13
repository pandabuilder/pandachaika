import logging
from urllib.parse import urljoin, unquote

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
        # Don't modify the shared settings headers.
        headers = dict(request_dict["headers"])

        xsrf_token = (request_dict.get("cookies") or {}).get("XSRF-TOKEN", None)
        if xsrf_token is not None:
            headers["X-XSRF-TOKEN"] = unquote(xsrf_token)
        headers["Origin"] = "https://doujin.io"
        request_dict["headers"] = headers

        request_dict["params"] = {
            "keyword": query,
            "tags": [],
            "page": 1,
            "sort": "published_at",
            "sort_dir": "desc",
        }

        response = self.request(urljoin(constants.main_url, constants.search_path), request_dict, post=True)

        if not response:
            logger.warning("Matcher {}: got no response from server".format(self))
            return SearchResult()

        try:
            response_data = response.json()
            found = response_data["data"]["data"]
        except (ValueError, KeyError, TypeError):
            logger.warning("Matcher {}: error parsing response from server: {}".format(self, response.text))
            return SearchResult()

        matches_links = {
            urljoin(constants.main_url, "/{}/{}".format(gallery["optimus_id"], gallery["slug"])) for gallery in found
        }

        return SearchResult(links=sorted(matches_links))


API = (TitleMatcher,)
