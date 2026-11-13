import logging
from xml.etree.ElementTree import ParseError

from core.base.matchers import Matcher
from core.base.types import GalleryData
from core.base.utilities import construct_request_dict
from core.matching.types import SearchResult
from core.providers.mugimugi.utilities import convert_api_response_text_to_gallery_dicts
from . import constants

logger = logging.getLogger(__name__)

# The API manual says 25 results per page, but it returns 50.
API_PAGE_SIZE = 50


class TitleMatcher(Matcher):
    """The XML API returns the full metadata, so no second fetch is needed."""

    name = "title"
    provider = constants.provider_name
    type = "title"
    default_cutoff = 0.6

    def search(self, query: str) -> SearchResult:

        if not self.own_settings.api_key:
            logger.error(
                "Can't use {} API without an api key. Check {}/API_MANUAL.txt".format(self.name, constants.main_page)
            )
            return SearchResult()

        page = 1
        galleries: list[GalleryData] = []

        while True:
            link = "{}/api/{}/?S=objectSearch&sn={}&page={}".format(
                constants.main_page, self.own_settings.api_key, query, page
            )

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = self.request(link, request_dict)

            if not response:
                break

            response.encoding = "utf-8"
            # Based on: https://www.doujinshi.org/API_MANUAL.txt
            try:
                api_galleries = convert_api_response_text_to_gallery_dicts(response.text)
            except ParseError:
                logger.warning("Matcher {}: could not parse API response: {}".format(self, response.text[:200]))
                break

            if not api_galleries:
                break

            galleries.extend(api_galleries)

            if len(api_galleries) < API_PAGE_SIZE:
                break

            page += 1

        return SearchResult(links=[x.link for x in galleries if x.link], galleries=galleries)


API = (TitleMatcher,)
