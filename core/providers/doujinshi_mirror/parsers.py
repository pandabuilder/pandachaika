import logging
import typing
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.types import GalleryData
from core.base.utilities import chunks, construct_request_dict
from . import constants, utilities
from .settings import OwnSettings

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


class Parser(BaseParser[OwnSettings]):
    """Book links (on the mirror or the original site) and the mirror's search pages, through the JSON API."""

    name = constants.provider_name
    accepted_urls = constants.original_book_urls

    @property
    def base_url(self) -> str:
        return utilities.base_url(self.own_settings.url) if self.own_settings else ""

    def api_get(self, url: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET a JSON API URL. None (logged) when there's no usable response."""
        request_dict = construct_request_dict(self.settings, self.own_settings)
        if params:
            request_dict["params"] = params
        response = self.web_request(url, request_dict)
        if response is None:
            logger.error("{}: no response from {}".format(self.name, url))
            return None
        try:
            data = response.json()
        except ValueError:
            logger.error(
                "{}: response from {} (HTTP {}) is not JSON: {}".format(
                    self.name, url, response.status_code, response.text[:200]
                )
            )
            return None
        if response.status_code != 200 or not isinstance(data, dict):
            message = data.get("error") if isinstance(data, dict) else data
            logger.error("{}: HTTP {} from {}: {}".format(self.name, response.status_code, url, message))
            return None
        return data

    def is_book_url(self, url: str) -> bool:
        if utilities.book_id_from_url(url) is None:
            return False
        return utilities.is_original_book_url(url) or (
            utilities.is_mirror_url(url, self.base_url) and utilities.search_api_request(url, self.base_url) is None
        )

    def is_search_url(self, url: str) -> bool:
        return utilities.search_api_request(url, self.base_url) is not None

    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if self.is_book_url(x) or self.is_search_url(x)]

    def filter_update_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if self.is_book_url(x)]

    @staticmethod
    def id_from_url(url: str) -> str | None:
        book_id = utilities.book_id_from_url(url)
        if book_id is None:
            return None
        return utilities.gid_from_book_id(book_id)

    # Metadata.

    def fetch_books(self, book_ids: list[int]) -> list[GalleryData]:
        base = self.base_url
        if not base:
            logger.error("{}: providers.{}.general.url is not set.".format(self.name, self.name))
            return []
        galleries: dict[int, GalleryData] = {}
        groups = list(chunks(list(dict.fromkeys(book_ids)), constants.ids_per_request))
        for i, group in enumerate(groups):
            logger.info(
                "Calling API ({}). Gallery group: {}, galleries in group: {}, total groups: {}".format(
                    self.name, i + 1, len(group), len(groups)
                )
            )
            data = self.api_get(
                base + constants.books_path,
                {"ids": ",".join(str(x) for x in group), "per_page": len(group), "count": 0},
            )
            if not data:
                continue
            for book in data.get("results") or []:
                gallery = utilities.book_to_gallery_data(book, base)
                if gallery is not None:
                    galleries[book["id"]] = gallery
        return [galleries[x] for x in book_ids if x in galleries]

    def fetch_gallery_data(self, url: str) -> GalleryData | None:
        galleries = self.fetch_multiple_gallery_data([url])
        return galleries[0] if galleries else None

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData]:
        book_ids = [x for x in (utilities.book_id_from_url(url) for url in url_list) if x is not None]
        return self.fetch_books(book_ids)

    # Search pages.

    def search_pages(
        self, url: str, max_pages: int | None = None, *, clean_query: bool = False
    ) -> typing.Iterator[list[GalleryData]]:
        """The books of a search page link, a page at a time (the search response has the full metadata)."""
        search = utilities.search_api_request(url, self.base_url, clean_query=clean_query)
        if search is None:
            return
        api_url, search_params = search
        params: dict[str, Any] = {**search_params, "count": 0}
        params.setdefault("per_page", str(constants.page_size))
        per_page = int(params["per_page"]) if str(params["per_page"]).isdigit() else constants.page_size
        page = int(params["page"]) if str(params.get("page", "")).isdigit() else 1
        pages = 0
        while max_pages is None or pages < max_pages:
            data = self.api_get(api_url, {**params, "page": page})
            if not data:
                return
            results = data.get("results") or []
            galleries = [x for x in (utilities.book_to_gallery_data(b, self.base_url) for b in results) if x]
            if galleries:
                yield galleries
            pages += 1
            if len(results) < per_page:
                return
            page += 1

    def links_from_search_page(self, url: str) -> list[str]:
        for galleries in self.search_pages(url, max_pages=1, clean_query=True):
            return [x.link for x in galleries if x.link]
        return []

    # Crawling.

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters: QuerySet | None = None,
        wanted_only: bool = False,
        preselected_wanted_matches: dict[str, list["WantedGallery"]] | None = None,
    ) -> None:

        gallery_wanted_lists: dict[str, list["WantedGallery"]] = preselected_wanted_matches or defaultdict(list)

        if not self.base_url:
            logger.error("{}: providers.{}.general.url is not set, skipping.".format(self.name, self.name))
            return

        if not self.downloaders:
            logger.warning("No downloaders enabled, returning.")
            return

        max_pages = self.own_settings.stop_page_number or constants.default_search_pages

        # Book id: its link, and the metadata when it came from a search page.
        book_links: dict[int, str] = {}
        found_data: dict[int, GalleryData] = {}

        for url in urls:
            if self.is_search_url(url):
                logger.info("{}: crawling search page (up to {} pages): {}".format(self.name, max_pages, url))
                for galleries in self.search_pages(url, max_pages=max_pages):
                    for gallery_data in galleries:
                        book_id = utilities.book_id_from_gid(gallery_data.gid)
                        if book_id is not None and gallery_data.link:
                            book_links.setdefault(book_id, gallery_data.link)
                            found_data[book_id] = gallery_data
                continue
            if not self.is_book_url(url):
                logger.warning("Invalid URL, skipping: {}".format(url))
                continue
            book_id = utilities.book_id_from_url(url)
            if book_id is not None:
                book_links.setdefault(book_id, url)

        to_process: list[int] = []
        discarded_count = 0
        for book_id, link in book_links.items():
            discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                gallery_id=utilities.gid_from_book_id(book_id), link=link
            )
            if discard_approved:
                discarded_count += 1
                if not self.settings.silent_processing:
                    logger.info(discard_message)
                continue
            to_process.append(book_id)

        self.log_database_check_summary(
            total=len(book_links), existing=discarded_count, discarded=discarded_count, to_update=0
        )

        summary_log = logger.debug if getattr(self.settings, "crawl_batch_collector", None) is not None else logger.info

        if not to_process:
            summary_log("No galleries need downloading, returning.")
            return

        summary_log("Queued %d galleries to fetch metadata (out of %d total).", len(to_process), len(book_links))

        to_fetch = [x for x in to_process if x not in found_data]
        fetched = {utilities.book_id_from_gid(x.gid): x for x in self.fetch_books(to_fetch)} if to_fetch else {}
        galleries_data = [found_data.get(x) or fetched.get(x) for x in to_process]

        gallery_data_list = []
        banned_count = 0
        unmatched_wanted_count = 0
        matched_wanted_count = 0
        errors = 0

        for internal_gallery_data in galleries_data:

            if internal_gallery_data is None or not internal_gallery_data.link:
                errors += 1
                continue

            banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                internal_gallery_data.tags, internal_gallery_data.uploader, gallery_data=internal_gallery_data
            )

            if banned_result:
                banned_count += 1
                if self.gallery_callback:
                    self.gallery_callback(None, internal_gallery_data.link, "banned_data")
                if not self.settings.silent_processing:
                    logger.info(
                        "Skipping gallery link {}, discarded reasons: {}".format(
                            internal_gallery_data.link, banned_reasons
                        )
                    )
                continue

            if wanted_filters:
                self.compare_gallery_with_wanted_filters(
                    internal_gallery_data, internal_gallery_data.link, wanted_filters, gallery_wanted_lists
                )
                if wanted_only and not gallery_wanted_lists[internal_gallery_data.gid]:
                    unmatched_wanted_count += 1
                    continue

            if len(gallery_wanted_lists.get(internal_gallery_data.gid, [])) > 0:
                matched_wanted_count += 1

            gallery_data_list.append(internal_gallery_data)

        self.log_metadata_evaluation_summary(
            total_evaluated=len(to_process),
            accepted=len(gallery_data_list),
            matched_wanted=matched_wanted_count,
            unmatched_wanted=unmatched_wanted_count,
            banned=banned_count,
            errors=errors,
            wanted_filters=bool(wanted_filters),
            wanted_only=wanted_only,
        )

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)


API = (Parser,)
