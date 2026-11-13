import logging
import os
import re
import zipfile

from core.base.matchers import Matcher
from core.base.types import GalleryData
from core.base.utilities import (
    construct_request_dict,
    get_images_from_zip,
    sha1_from_file_object,
)
from core.matching.types import MatchCandidate, MatchTarget, SearchResult
from core.providers.panda.utilities import SearchHTMLParser
from . import constants

logger = logging.getLogger(__name__)


class PandaMatcher(Matcher):
    """Searches the site's gallery list. The links found are fetched with the parser (the "panda" API bucket)."""

    provider = constants.provider_name
    default_cutoff = 0.7

    def search_site(self, params: dict[str, str | int]) -> SearchResult:
        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["params"] = params

        response = self.request(constants.ex_page, request_dict)
        if not response:
            logger.info("Matcher {}: got no response from server".format(self))
            return SearchResult()

        parser = SearchHTMLParser()
        parser.feed(response.text)
        return SearchResult(links=list(parser.galleries))


class ImageMatcher(PandaMatcher):

    name = "image"
    type = "image"
    exact = True
    uses_file = True
    default_cutoff = 0.0
    only_cover = False

    def format_to_compare_title(self, text: str) -> str:
        return self.get_title_from_path(text)

    @staticmethod
    def first_image_sha1(zip_path: str) -> tuple[str, str] | None:
        """sha1 and file name of the archive's first image."""
        if os.path.splitext(zip_path)[1] != ".zip":
            return None
        try:
            with zipfile.ZipFile(zip_path, "r") as my_zip:
                filtered_files = get_images_from_zip(my_zip)
                if not filtered_files:
                    return None
                first_file = filtered_files[0]
                if first_file[1] is None:
                    with my_zip.open(first_file[0]) as current_img:
                        return sha1_from_file_object(current_img), first_file[0]
                with my_zip.open(first_file[1]) as current_zip:
                    with zipfile.ZipFile(current_zip) as my_nested_zip:
                        with my_nested_zip.open(first_file[0]) as current_img:
                            return sha1_from_file_object(current_img), first_file[0]
        except (OSError, zipfile.BadZipFile, NotImplementedError):
            return None

    def search(self, query: str) -> SearchResult:
        first_image = self.first_image_sha1(query)
        if not first_image:
            return SearchResult()
        sha1, file_name = first_image
        return self.search_site(
            {
                "f_shash": sha1,
                "fs_from": os.path.basename(file_name),
                "fs_covers": 1 if self.only_cover else 0,
                "fs_similar": 0,
            }
        )


class CoverMatcher(ImageMatcher):

    name = "cover"
    only_cover = True


class TitleMatcher(PandaMatcher):

    name = "title"
    type = "title"

    def search(self, query: str) -> SearchResult:
        return self.search_site({"f_search": '"' + query + '"'})


class TitleGidMatcher(TitleMatcher):
    """Files named like "<gid> <title>". Searches by title, and only accepts galleries with that gid."""

    name = "gid"
    type = "gid"
    exact = True

    @staticmethod
    def get_gid_from_title(title: str) -> str:
        result = re.search(r"^(\d+)", title)
        if result:
            return result.group(1)
        return ""

    def clean_compare_title(self, title: str) -> str:
        return self.get_gid_from_title(super().clean_compare_title(title))

    def score(
        self, target: MatchTarget, galleries: list[GalleryData], cutoff: float, max_matches: int
    ) -> list[MatchCandidate]:
        gid = self.format_to_compare_title(target.text) if target.text else ""
        if not gid:
            return []
        return [self.make_candidate(gallery, 1.0) for gallery in galleries if gallery.gid == gid]


API = (
    CoverMatcher,
    ImageMatcher,
    TitleMatcher,
    TitleGidMatcher,
)
