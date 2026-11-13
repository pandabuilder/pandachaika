"""Base class for the online matchers that each provider defines in its ``matchers.py``.

A matcher searches one provider for the galleries a file or title belongs to. Matchers hold no per search state:
:meth:`Matcher.find_online` returns the candidates, and :mod:`core.matching.engine` decides what to do with them.

Every request a matcher makes must go through :meth:`Matcher.request`, which uses the shared database rate limiter
(the same buckets as the provider's parser and downloaders), so matching and crawling never hit a site harder than its
``wait_timer`` allows, and provider bans/cooldowns are respected.
"""

import logging
import os
import typing
from difflib import SequenceMatcher

import requests

from core.base.comparison import get_list_closer_gallery_titles_from_dict
from core.base.setup_utilities import GeneralUtils
from core.base.types import GalleryData
from core.base.utilities import (
    clean_title,
    file_matches_any_filter,
    get_title_from_path,
    request_by_provider,
)
from core.matching.types import MatchCandidate, MatchTarget, SearchResult, dedupe_candidates, pick_best

if typing.TYPE_CHECKING:
    from core.base.parsers import BaseParser
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


class Meta(type):
    name = ""
    provider = ""

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.name)


def title_similarity(text: str, gallery_data: GalleryData) -> float:
    if not text:
        return 0.0
    ratios = [
        SequenceMatcher(None, text, title).ratio() for title in (gallery_data.title, gallery_data.title_jpn) if title
    ]
    return max(ratios, default=0.0)


class Matcher(metaclass=Meta):

    name = ""
    provider = ""
    # Stored as ArchiveMatches.match_type, and used to filter matchers ("title", "hash", "size", "image", "gid").
    type = ""
    # Minimum accuracy for a title result to be linked automatically.
    default_cutoff = 0.5
    # Exact matchers (hash, size, image) trust every result the provider returns: they all get accuracy 1, and the
    # title similarity only picks the best one among them.
    exact = False
    # Exact matchers search with the file itself, so they need a target with a file.
    uses_file = False
    # Rate limit bucket for this matcher's requests. Defaults to "<provider>_web", the bucket the provider's parser
    # and downloaders use for the same site.
    rate_limit_name = ""

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.own_settings = settings.providers[self.provider]
        self.general_utils = GeneralUtils(global_settings=settings)
        self._parser: "BaseParser | None" = None
        # Responses shared between matchers, keyed by request. The engine gives all its matchers the same cache
        # and clears it for each target, so matchers sending the same request for a file only hit the site once.
        self.response_cache: dict[tuple[typing.Any, ...], requests.models.Response] | None = None

    def __str__(self) -> str:
        return "{}_{}".format(self.provider, self.name)

    @property
    def parser(self) -> "BaseParser":
        if self._parser is None:
            self._parser = self.settings.provider_context.get_parsers(self.settings, filter_name=self.provider)[0]
        return self._parser

    @property
    def rate_limit_key(self) -> str:
        return self.rate_limit_name or "{}_web".format(self.provider)

    def request(
        self, url: str, request_dict: dict[str, typing.Any], post: bool = False
    ) -> requests.models.Response | None:
        """Rate limited request. Raises ProviderBannedError if the provider is in a long cooldown."""
        key = self.request_key(url, request_dict, post)
        if self.response_cache is not None and key in self.response_cache:
            logger.info("Matcher {}: reusing response for {}".format(self, url))
            return self.response_cache[key]
        requester = request_by_provider(self.rate_limit_key, 1, float(self.own_settings.wait_timer))
        response = requester(url, request_dict, post=post)
        if self.response_cache is not None and response is not None:
            self.response_cache[key] = response
        return response

    @staticmethod
    def request_key(url: str, request_dict: dict[str, typing.Any], post: bool) -> tuple[typing.Any, ...]:
        """What makes two requests the same: method, URL and the query or body sent."""
        return (
            post,
            url,
            repr(sorted((request_dict.get("params") or {}).items())),
            repr(request_dict.get("data")),
            repr(request_dict.get("json")),
        )

    # Title formatting. Providers usually only change the cleaning functions.

    def title_from_text(self, text: str) -> str:
        """The text itself, or the file name without folders and extension if the text is a file path."""
        if file_matches_any_filter(text, self.settings.filename_filter):
            return get_title_from_path(text)
        return text

    def clean_search_title(self, title: str) -> str:
        return clean_title(title)

    def clean_compare_title(self, title: str) -> str:
        return clean_title(title)

    def format_to_search_title(self, text: str) -> str:
        return self.clean_search_title(self.title_from_text(text))

    def format_to_compare_title(self, text: str) -> str:
        return self.clean_compare_title(self.title_from_text(text))

    # Online search.

    def search_input(self, target: MatchTarget) -> str:
        """What :meth:`search` receives: the absolute file path for file matchers, the search title for the rest."""
        if self.uses_file:
            if not target.file_path:
                return ""
            return os.path.join(self.settings.MEDIA_ROOT, target.file_path)
        return self.format_to_search_title(target.text) if target.text else ""

    def search(self, query: str) -> SearchResult:
        """Search the provider. Must make its requests with :meth:`request`."""
        raise NotImplementedError

    def fetch_galleries(self, links: list[str]) -> list[GalleryData]:
        if not links:
            return []
        return self.parser.fetch_multiple_gallery_data(links) or []

    def filter_galleries(self, galleries: list[GalleryData]) -> list[GalleryData]:
        return [
            x
            for x in galleries
            if not self.general_utils.discard_by_gallery_data(x.tags, x.uploader, gallery_data=x)[0]
        ]

    def score(
        self, target: MatchTarget, galleries: list[GalleryData], cutoff: float, max_matches: int
    ) -> list[MatchCandidate]:
        compare_title = self.format_to_compare_title(target.text) if target.text else ""
        if self.exact:
            candidates = [
                self.make_candidate(gallery, 1.0, compare_title=compare_title) for gallery in galleries
            ]
        else:
            if not compare_title:
                return []
            candidates = [
                self.make_candidate(gallery, accuracy, title=matched_title)
                for matched_title, gallery, accuracy in get_list_closer_gallery_titles_from_dict(
                    compare_title, galleries, cutoff, max_matches
                )
            ]
        return dedupe_candidates(candidates)

    def make_candidate(
        self, gallery_data: GalleryData, accuracy: float, title: str = "", compare_title: str = ""
    ) -> MatchCandidate:
        return MatchCandidate(
            matcher=str(self),
            match_type=self.type,
            found_by=self.name,
            accuracy=accuracy,
            title=title or gallery_data.title or gallery_data.title_jpn or "",
            gallery_data=gallery_data,
            title_similarity=title_similarity(compare_title, gallery_data) if compare_title else accuracy,
        )

    def find_online(self, target: MatchTarget, cutoff: float | None = None, max_matches: int = 20) -> list[MatchCandidate]:
        """Search the provider and return the candidates for target, best first."""
        if cutoff is None:
            cutoff = self.default_cutoff
        query = self.search_input(target)
        if not query:
            return []
        logger.info("Matcher {}: searching for: {}".format(self, query))
        result = self.search(query)
        galleries = result.galleries if result.galleries is not None else self.fetch_galleries(result.links)
        found = len(galleries)
        galleries = self.filter_galleries(galleries)
        candidates = self.score(target, galleries, cutoff, max_matches)
        logger.info(
            "Matcher {}: {} search results, {} after filters, {} candidates.".format(
                self, found, len(galleries), len(candidates)
            )
        )
        return candidates

    def best_candidate(
        self, candidates: list[MatchCandidate], cutoff: float | None = None
    ) -> MatchCandidate | None:
        """The candidate to link automatically, if any is good enough."""
        return pick_best(candidates, self.exact, self.default_cutoff if cutoff is None else cutoff)

    @staticmethod
    def get_title_from_path(path: str) -> str:
        return get_title_from_path(path)
