import logging
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

from core.base.matchers import Matcher
from core.base.types import GalleryData
from core.base.utilities import (
    calc_crc32,
    construct_request_dict,
    get_zip_filesize,
)
from core.matching.types import SearchResult
from viewer.models import Gallery

from . import constants
from .utilities import clean_title as chaika_clean_title

logger = logging.getLogger(__name__)


class ChaikaMatcher(Matcher):
    """Chaika's match API returns the full gallery metadata, so no second fetch is needed."""

    provider = constants.provider_name
    default_cutoff = 0.6
    # Whether to query metadata_url instead of url.
    use_metadata_url = False

    def api_url(self) -> str:
        base = self.own_settings.metadata_url if self.use_metadata_url else self.own_settings.url
        return urljoin(base, constants.api_path)

    def query_api(self, params: dict[str, Any]) -> SearchResult:
        api_url = self.api_url()
        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = {"match": True, **params}

        response = self.request(api_url, request_dict)

        if not response:
            logger.info("Matcher {}: got no response from server {}".format(self, api_url))
            return SearchResult()

        try:
            response_data = response.json()
        except ValueError:
            logger.warning("Matcher {}: could not parse response from {} as JSON".format(self, api_url))
            return SearchResult()

        if isinstance(response_data, dict):
            if "error" in response_data:
                logger.info("Matcher {}: got error from server: {}".format(self, response_data["error"]))
            return SearchResult()

        return self.galleries_from_response(response_data)

    @staticmethod
    def galleries_from_response(response_data: list[dict[str, Any]]) -> SearchResult:
        links: set[str] = set()
        galleries: list[GalleryData] = []

        for gallery in response_data:
            if "link" not in gallery:
                continue
            links.add(gallery["link"])
            if gallery.get("gallery_container"):
                container = Gallery.objects.filter(gid=gallery["gallery_container"], provider=gallery["provider"]).first()
                if container:
                    gallery["gallery_container_gid"] = container.gid
            if gallery.get("magazine"):
                magazine = Gallery.objects.filter(gid=gallery["magazine"], provider=gallery["provider"]).first()
                if magazine:
                    gallery["magazine_gid"] = magazine.gid
            if "posted" in gallery:
                if gallery["posted"]:
                    gallery["posted"] = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
                else:
                    gallery["posted"] = None
            galleries.append(GalleryData(**gallery))

        return SearchResult(links=sorted(links), galleries=galleries)


class ExactChaikaMatcher(ChaikaMatcher):
    exact = True
    uses_file = True
    default_cutoff = 0.0

    def format_to_compare_title(self, text: str) -> str:
        return self.get_title_from_path(text)


class HashMatcher(ExactChaikaMatcher):

    name = "hash"
    type = "hash"

    def search(self, query: str) -> SearchResult:
        if not os.path.isfile(query):
            return SearchResult()
        return self.query_api({"crc32": calc_crc32(query)})


class FileSizeMatcher(ExactChaikaMatcher):

    name = "size"
    type = "size"
    use_metadata_url = True

    def search(self, query: str) -> SearchResult:
        if not os.path.isfile(query):
            return SearchResult()
        filesize = get_zip_filesize(query)
        return self.query_api({"filesize_from": filesize, "filesize_to": filesize})


class TitleMatcher(ChaikaMatcher):

    name = "main_title"
    type = "title"

    def search(self, query: str) -> SearchResult:
        return self.query_api({"title": query})


class CleanTitleMatcher(TitleMatcher):

    name = "main_clean_title"

    def clean_search_title(self, title: str) -> str:
        return chaika_clean_title(title)

    def clean_compare_title(self, title: str) -> str:
        return chaika_clean_title(title)


class TitleMetaMatcher(TitleMatcher):

    name = "meta_title"
    use_metadata_url = True


API = (
    HashMatcher,
    TitleMatcher,
    CleanTitleMatcher,
    TitleMetaMatcher,
    FileSizeMatcher,
)
