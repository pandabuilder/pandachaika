# -*- coding: utf-8 -*-
import json
import logging
import re
import typing
from typing import Any
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from datetime import timedelta

import requests
import feedparser
import bs4
from bs4 import BeautifulSoup
from django.db.models import QuerySet

from core.base.parsers import BaseParser
from core.base.utilities import (
    chunks,
    construct_request_dict,
    request_by_provider,
    parse_panda_ban_duration,
    set_provider_cooldown,
)
from core.base.types import GalleryData, DataDict
from viewer.models import Gallery
from .utilities import (
    link_from_gid_token_fjord,
    map_external_gallery_data_to_internal,
    get_gid_token_from_link,
    root_gid_token_from_link,
    SearchHTMLParser,
)
from . import constants
from . import utilities

if typing.TYPE_CHECKING:
    from viewer.models import Provider, WantedGallery
    from core.base.setup import Settings

logger = logging.getLogger(__name__)

RE_PANDA_GALLERY = re.compile(r"(.+)/g/(\d+)/(\w+)")

# Captured links that fail to be processed this many times are marked as failed, instead of being retried forever.
MAX_LINK_PROCESS_ATTEMPTS = 5


class Parser(BaseParser):
    name = constants.provider_name
    accepted_urls = [constants.ex_page_short, constants.ge_page_short, constants.rss_url]
    supports_link_capture = True

    def __init__(self, settings: "Settings"):

        super().__init__(settings)

        self.api_request_function = request_by_provider(
            self.name, self.own_settings.api_concurrent_limit, self.own_settings.api_wait_limit
        )
        # Captured links being processed in this run (source id -> ProcessedLinks pk), and the ones already resolved.
        self._pending_links: dict[str, int] = {}
        self._resolved_links: set[str] = set()

    def _parse_api_response_json(
        self, response: requests.models.Response | None, url: str = ""
    ) -> dict[str, Any] | None:
        if not response:
            return None
        try:
            return response.json()
        except (ValueError, KeyError):
            ban_duration = parse_panda_ban_duration(getattr(response, "text", ""))
            if ban_duration is not None:
                cooldown_wait = ban_duration + 5.0
                set_provider_cooldown(self.name, cooldown_wait, url=url)
                logger.warning(
                    "Panda API temporary ban detected ({}s). Dynamic rate limit cooldown updated to {}s.".format(
                        ban_duration, cooldown_wait
                    )
                )
            logger.error("Could not parse response to JSON: {}".format(response.text))
            return None

    # Panda only methods
    def get_galleries_from_page_links(self, page_links: Iterable[str], page_links_results: list[DataDict]) -> None:

        api_page_links = []

        for page_link in page_links:

            m = re.search(r"(.+)/s/(\w+)/(\d+)-(\d+)", page_link)
            if not m:
                continue
            api_page_links.append({"data": [m.group(3), m.group(2), m.group(4)]})

        api_page_links_chunks = list(chunks(api_page_links, 25))

        for i, group in enumerate(api_page_links_chunks):

            data = {"method": "gtoken", "pagelist": [x["data"] for x in group]}

            headers = {"Content-Type": "application/json"}

            request_dict = construct_request_dict(self.settings, self.own_settings)
            request_dict["headers"] = request_dict["headers"] | headers
            request_dict["data"] = json.dumps(data)

            response = self.api_request_function(
                constants.ge_api_url,
                request_dict,
                post=True,
            )

            response_data = self._parse_api_response_json(response, constants.ge_api_url)
            if not response_data:
                continue

            for gid_token_pair in response_data["tokenlist"]:

                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery_id=gid_token_pair["gid"],
                    link=link_from_gid_token_fjord(gid_token_pair["gid"], gid_token_pair["token"], False),
                    gallery=None  # We don't have full gallery data here yet
                )

                if discard_approved:
                    if not self.settings.silent_processing:
                        logger.info(discard_message)
                    continue

                page_links_results.append(
                    {
                        "data": (gid_token_pair["gid"], gid_token_pair["token"]),
                        "link": link_from_gid_token_fjord(gid_token_pair["gid"], gid_token_pair["token"], False),
                    }
                )

    def _record_and_check_processed_links(
        self,
        gallery_urls: Iterable[str],
        unique_urls: set[str],
    ) -> bool:
        """Process gallery URLs from a search/lofi page.

        Only records ProcessedLinks and checks stop_on_seen_links for MonitoredLinks.
        On-demand page requests do not record ProcessedLinks or stop on seen links.

        When a seen link is encountered for a MonitoredLink and stop_on_seen_links is enabled,
        crawling stops, and if configured, previously processed links within the specified
        time window ("how long after already seen") are added to unique_urls.
        """
        monitored_link = getattr(self.own_settings, "monitored_link", None) or getattr(self.settings, "monitored_link", None)

        # On-demand pages (monitored_link is None):
        # Disable stop_on_seen_links interaction and link capture.
        if monitored_link is None:
            for gallery_url in gallery_urls:
                unique_urls.add(gallery_url)
            return False

        # Determine stop_on_seen_links setting for MonitoredLink
        stop_on_seen_val = getattr(monitored_link, "stop_on_seen_link", None)
        if stop_on_seen_val in ("on", "ON", True):
            stop_on_seen = True
        elif stop_on_seen_val in ("off", "OFF", False):
            stop_on_seen = False
        else:
            stop_on_seen = bool(
                getattr(self.own_settings, "stop_on_seen_links", False)
                or getattr(self.own_settings, "stop_on_seen_link", False)
                or getattr(self.settings, "stop_on_seen_links", False)
                or getattr(self.settings, "stop_on_seen_link", False)
            )

        capture_only = bool(getattr(self.settings, "capture_links_only", False))

        try:
            from viewer.models import ProcessedLinks

            provider = self._get_provider()
        except Exception as e:
            logger.warning("Could not access Provider or ProcessedLinks: %s", e)
            for gallery_url in gallery_urls:
                unique_urls.add(gallery_url)
            return False

        gallery_list = list(gallery_urls)
        source_id_map: list[tuple[str, str]] = []
        source_ids: list[str] = []
        for g_url in gallery_list:
            gid, _ = get_gid_token_from_link(g_url)
            s_id = gid if gid else g_url
            source_id_map.append((g_url, s_id))
            source_ids.append(s_id)

        query = ProcessedLinks.objects.filter(
            provider=provider,
            source_id__in=source_ids,
            monitored_link=monitored_link,
        )
        seen_source_ids = set(query.values_list("source_id", flat=True))

        to_create: list[ProcessedLinks] = []
        should_stop = False

        for g_url, s_id in source_id_map:
            if s_id in seen_source_ids:
                if stop_on_seen:
                    logger.info(
                        "Encountered previously processed link %s (source_id: %s) for monitored link %s, stopping (setting: stop_on_seen_links).",
                        g_url,
                        s_id,
                        getattr(monitored_link, "name", monitored_link),
                    )
                    should_stop = True
                    break
                else:
                    unique_urls.add(g_url)
            else:
                unique_urls.add(g_url)
                to_create.append(
                    ProcessedLinks(
                        provider=provider,
                        monitored_link=monitored_link,
                        source_id=s_id,
                        url=g_url,
                        # Captured links wait for the process run, otherwise they are processed in this run.
                        status=ProcessedLinks.Status.PENDING if capture_only else ProcessedLinks.Status.PROCESSED,
                    )
                )
                seen_source_ids.add(s_id)

        if to_create:
            try:
                ProcessedLinks.objects.bulk_create(to_create, ignore_conflicts=True)
            except Exception as e:
                logger.warning("Could not bulk create ProcessedLinks: %s", e)

        # In capture mode the recent links are added by the process run instead.
        if should_stop and not capture_only:
            unique_urls.update(self._recent_seen_urls(monitored_link, provider))

        return should_stop

    def _get_provider(self) -> "Provider":
        from viewer.models import Provider

        provider = Provider.objects.filter(slug=self.name).first()
        if not provider:
            provider, _ = Provider.objects.get_or_create(slug=self.name, defaults={"name": self.name.capitalize()})
        return provider

    def _recent_seen_urls(self, monitored_link: Any, provider: "Provider") -> set[str]:
        """Links seen within the monitored link's "how long after already seen" window, if configured to use them."""
        from django.utils import timezone
        from viewer.models import ProcessedLinks
        from viewer.models.events import MonitoredLink as MLModel

        already_seen_mode = (
            getattr(monitored_link, "already_seen_links_mode", None)
            or getattr(self.own_settings, "already_seen_links_mode", None)
            or getattr(self.settings, "already_seen_links_mode", None)
            or "dont_use"
        )
        if already_seen_mode not in (MLModel.AlreadySeenLinksMode.USE_BEFORE_NOW, "use_before_now", "use_x_time_before"):
            return set()
        window_duration = (
            getattr(monitored_link, "how_long_after_already_seen", None)
            or getattr(self.own_settings, "how_long_after_already_seen", None)
            or getattr(self.settings, "how_long_after_already_seen", None)
        )
        if isinstance(window_duration, (int, float)):
            window_duration = timedelta(seconds=window_duration)
        if not window_duration or window_duration.total_seconds() <= 0:
            return set()
        cutoff = timezone.now() - window_duration
        recent_urls = {
            r_url
            for r_url in ProcessedLinks.objects.filter(
                provider=provider,
                monitored_link=monitored_link,
                create_date__gte=cutoff,
            ).values_list("url", flat=True)
            if r_url
        }
        logger.info(
            "Adding %d links seen within the last %s for monitored link %s.",
            len(recent_urls),
            window_duration,
            getattr(monitored_link, "name", monitored_link),
        )
        return recent_urls

    def _load_pending_links(self, unique_urls: set[str]) -> None:
        """Add the links captured but not yet processed for the current monitored link to the URLs to process."""
        monitored_link = getattr(self.own_settings, "monitored_link", None) or getattr(self.settings, "monitored_link", None)
        if monitored_link is None or getattr(self.settings, "capture_links_only", False):
            return
        try:
            from viewer.models import ProcessedLinks

            provider = self._get_provider()
            pending = list(
                ProcessedLinks.objects.filter(
                    provider=provider, monitored_link=monitored_link, status=ProcessedLinks.Status.PENDING
                ).values_list("pk", "source_id", "url")
            )
            if getattr(self.settings, "skip_link_capture", False):
                # Separate frequencies: the window of recent links is applied here, since the capture run
                # doesn't call the API and can't tell which links are worth checking again.
                unique_urls.update(self._recent_seen_urls(monitored_link, provider))
        except Exception as e:
            logger.warning("Could not load the pending links: %s", e)
            return
        for pk, source_id, url in pending:
            self._pending_links[source_id] = pk
            unique_urls.add(url)
        if pending:
            logger.info(
                "Processing %d captured links for monitored link '%s'.", len(pending), getattr(monitored_link, "name", "")
            )

    def _finish_pending_links(self) -> None:
        """Mark the captured links as processed once their metadata was fetched and checked.
        The ones that couldn't be, stay pending to be retried by the next run, up to MAX_LINK_PROCESS_ATTEMPTS."""
        if not self._pending_links:
            return
        from django.db.models import F
        from django.utils import timezone
        from viewer.models import ProcessedLinks

        done = [pk for source_id, pk in self._pending_links.items() if source_id in self._resolved_links]
        failed = [pk for source_id, pk in self._pending_links.items() if source_id not in self._resolved_links]
        self._pending_links = {}
        self._resolved_links = set()
        try:
            now = timezone.now()
            for group in chunks(done, 900):
                ProcessedLinks.objects.filter(pk__in=group).update(
                    status=ProcessedLinks.Status.PROCESSED, processed_date=now
                )
            for group in chunks(failed, 900):
                ProcessedLinks.objects.filter(pk__in=group).update(process_attempts=F("process_attempts") + 1)
                ProcessedLinks.objects.filter(
                    pk__in=group, process_attempts__gte=MAX_LINK_PROCESS_ATTEMPTS, status=ProcessedLinks.Status.PENDING
                ).update(status=ProcessedLinks.Status.FAILED)
            if failed:
                logger.warning("%d captured links could not be processed and will be retried.", len(failed))
        except Exception as e:
            logger.warning("Could not update the status of the captured links: %s", e)

    def get_galleries_from_main_page_link(self, url: str) -> set[str]:

        unique_urls: set[str] = set()
        current_page = 0

        while True:

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = self.web_request(
                url,
                request_dict,
                post=False,
            )

            if not response:
                logger.info("Got no response, stopping")
                break

            response.encoding = "utf-8"

            if "No hits found" in response.text:
                logger.info("Empty page found, ending")
                break
            else:
                logger.info(
                    "Got content on search page {}, looking for galleries and jumping "
                    "to the next page. Link: {}".format(current_page, url)
                )
                main_page_parser = SearchHTMLParser()
                main_page_parser.feed(response.text)
                logger.info("Number of galleries found: {}".format(len(main_page_parser.galleries)))
                if len(main_page_parser.galleries) >= 1:
                    should_stop = self._record_and_check_processed_links(main_page_parser.galleries, unique_urls)
                    if should_stop:
                        break
                else:
                    logger.info("Empty page found, ending")
                    break
                if self.own_settings.stop_page_number is not None:
                    if current_page >= self.own_settings.stop_page_number:
                        logger.info(
                            "Got to stop page number: {}, "
                            "ending (setting: provider.stop_page_number).".format(self.own_settings.stop_page_number)
                        )
                        break

                last_gallery = main_page_parser.galleries[-1]
                last_gid, _ = get_gid_token_from_link(last_gallery)
                if not last_gid:
                    logger.warning("Could not extract gid from last gallery {}, ending".format(last_gallery))
                    break

                current_page += 1
                parsed = urllib.parse.urlparse(url)
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("next") == [last_gid]:
                    logger.info("Next gid did not change ({}), ending".format(last_gid))
                    break

                query.pop("page", None)
                query["next"] = [last_gid]
                new_query = urllib.parse.urlencode(query, doseq=True)
                url = urllib.parse.urlunparse(list(parsed[0:4]) + [new_query] + list(parsed[5:]))

        return unique_urls

    def get_galleries_from_lofi_page_link(self, url: str) -> set[str]:

        unique_urls: set[str] = set()
        current_page = 0

        while True:
            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = self.web_request(
                url,
                request_dict,
                post=False,
            )

            if not response:
                logger.info("Got no response, stopping")
                break

            response.encoding = "utf-8"

            if "No hits found" in response.text:
                logger.info("Empty page found, ending")
                break
            else:
                logger.info(
                    "Got content on search page {}, looking for galleries and jumping "
                    "to the next page. Link: {}".format(current_page, url)
                )
                current_found_links = []
                soup = BeautifulSoup(response.text, "html.parser")
                gallery_containers = soup.find_all("td", {"class": "ii"})
                for gallery_container in gallery_containers:
                    if isinstance(gallery_container, bs4.element.Tag):
                        link_container = gallery_container.find("a")
                        if isinstance(link_container, bs4.element.Tag):
                            href = link_container.get("href")
                            if isinstance(href, str):
                                found_link = href.replace("lofi/", "")
                                current_found_links.append(found_link)
                logger.info("Number of galleries found: {}".format(len(current_found_links)))
                if len(current_found_links) >= 1:
                    should_stop = self._record_and_check_processed_links(current_found_links, unique_urls)
                    if should_stop:
                        break
                else:
                    logger.info("Empty page found, ending")
                    break
                if self.own_settings.stop_page_number is not None:
                    if current_page >= self.own_settings.stop_page_number:
                        logger.info(
                            "Got to stop page number: {}, "
                            "ending (setting: provider.stop_page_number).".format(self.own_settings.stop_page_number)
                        )
                        break

                last_gallery = current_found_links[-1]
                last_gid, _ = get_gid_token_from_link(last_gallery)
                if not last_gid:
                    logger.warning("Could not extract gid from last gallery {}, ending".format(last_gallery))
                    break

                current_page += 1
                parsed = urllib.parse.urlparse(url)
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("next") == [last_gid]:
                    logger.info("Next gid did not change ({}), ending".format(last_gid))
                    break

                query.pop("page", None)
                query["next"] = [last_gid]
                new_query = urllib.parse.urlencode(query, doseq=True)
                url = urllib.parse.urlunparse(list(parsed[0:4]) + [new_query] + list(parsed[5:]))

        return unique_urls

    def get_values_from_gallery_link_list(self, url_list: Iterable[str], use_fjord: bool = False) -> list[GalleryData]:

        gid_token_chunks = list(chunks([get_gid_token_from_link(link) for link in url_list], 25))

        galleries_data = []

        if self.own_settings.cookies and use_fjord:
            api_page = constants.ex_api_url
        else:
            api_page = constants.ge_api_url

        for i, group in enumerate(gid_token_chunks):

            if not self.settings.silent_processing:
                logger.info(
                    "Calling API ({}), URL: {}. "
                    "Gallery group: {}, galleries in group: {}, total groups: {}".format(
                        self.name, api_page, i + 1, len(group), len(gid_token_chunks)
                    )
                )

            data = utilities.request_data_from_gid_token_iterable(group)

            headers = {"Content-Type": "application/json"}

            request_dict = construct_request_dict(self.settings, self.own_settings)
            request_dict["headers"] = request_dict["headers"] | headers
            request_dict["data"] = json.dumps(data)

            response = self.api_request_function(
                api_page,
                request_dict,
                post=True,
            )

            response_data = self._parse_api_response_json(response, api_page)
            if not response_data:
                continue

            for gallery_data in response_data["gmetadata"]:
                if "error" in gallery_data:
                    logger.error(
                        "Fetching gallery {}: "
                        "failed with error: {}".format(gallery_data["gid"], gallery_data["error"])
                    )
                    continue
                internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
                if use_fjord and internal_gallery_data.fjord:
                    internal_gallery_data.root = constants.ex_page
                    internal_gallery_data.link = link_from_gid_token_fjord(
                        gallery_data["gid"], gallery_data["token"], True
                    )
                else:
                    internal_gallery_data.root = constants.ge_page
                    internal_gallery_data.link = link_from_gid_token_fjord(
                        gallery_data["gid"], gallery_data["token"], False
                    )
                galleries_data.append(internal_gallery_data)

        return galleries_data

    def get_values_from_gallery_link(self, link: str) -> GalleryData | None:

        link_root, gid, token = root_gid_token_from_link(link)

        if link_root is None or gid is None or token is None:
            return None

        if self.own_settings.use_ex_for_fjord and self.own_settings.cookies and link_root == constants.ex_api_url:
            api_page = constants.ex_api_url
        else:
            api_page = constants.ge_api_url

        data = utilities.request_data_from_gid_token_iterable([(gid, token)])

        headers = {"Content-Type": "application/json"}

        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["headers"] = request_dict["headers"] | headers
        request_dict["data"] = json.dumps(data)

        response = self.api_request_function(
            api_page,
            request_dict,
            post=True,
        )

        response_data = self._parse_api_response_json(response, api_page)
        if not response_data:
            return None
        for gallery_data in response_data["gmetadata"]:
            if "error" in gallery_data:
                logger.error(
                    "Adding gallery {}: " "failed with error: {}".format(gallery_data["gid"], gallery_data["error"])
                )
                return None
            internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
            return internal_gallery_data
        return None

    def get_sha1_hashes_from_panda(self, gallery_url: str) -> list[str]:
        request_dict = construct_request_dict(self.settings, self.own_settings)
        request_dict["cookies"] = request_dict["cookies"] | {"datatags": "1", "nw": "1"} # nw is "never warn" with a redirection about certain content tags
        request_method = request_by_provider("panda_web", 1, self.own_settings.wait_timer)

        def get_panda_page_count(soup):
            pagination_table = soup.find("table", class_="ptt")
            if not pagination_table:
                return 1

            td_elements = pagination_table.find_all("td")
            page_numbers = []

            for td in td_elements:
                text = td.get_text(strip=True)
                if text.isdigit():
                    page_numbers.append(int(text))

            if page_numbers:
                return max(page_numbers)
            return 1

        def extract_all_hashes(base_url: str) -> list[str]:
            if not base_url.endswith("/"):
                base_url += "/"
            response = request_method(
                base_url,
                request_dict,
                post=False,
            )

            soup = BeautifulSoup(response.text, "html.parser")
            if not soup:
                logger.error(
                    f"Failed to fetch initial page for sha1 comparison. Status code: {response.status_code}"
                )
                return []

            total_pages = get_panda_page_count(soup)
            all_hashes: list[str] = []

            for page_index in range(total_pages):
                page_url = f"{base_url}?p={page_index}"

                if page_index == 0:
                    current_soup = soup
                else:
                    response = request_method(
                        page_url,
                        request_dict,
                        post=False,
                    )
                    current_soup = BeautifulSoup(response.text, "html.parser")
                    if not current_soup:
                        continue
                hash_elements = current_soup.find_all(None, attrs={"data-orghash": True})
                page_hashes = [str(el["data-orghash"]) for el in hash_elements]
                all_hashes.extend(page_hashes)
            return all_hashes

        panda_sha1_list = extract_all_hashes(gallery_url)

        return panda_sha1_list

    def get_feed_urls(self) -> list[str]:
        return [
            constants.rss_url,
        ]

    def crawl_feed(self, feed_url: str = "") -> list[str]:

        urls: list[str] = []

        if not feed_url:
            feed_url = constants.rss_url

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = self.web_request(
            feed_url,
            request_dict,
            post=False,
        )

        if not response:
            logger.error("Got no response from feed URL: {}".format(feed_url))
            return urls

        response.encoding = "utf-8"

        feed = feedparser.parse(response.text)

        for item in feed["items"]:
            if self.own_settings.accepted_rss_categories:
                if any([item["title"].startswith(category) for category in self.own_settings.accepted_rss_categories]):
                    urls.append(item["link"])
            else:
                urls.append(item["link"])
        return urls

    def fetch_gallery_data(self, url: str) -> GalleryData | None:
        return self.get_values_from_gallery_link(url)

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData]:
        return self.get_values_from_gallery_link_list(url_list)

    def filter_update_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in super().filter_update_urls(urls) if RE_PANDA_GALLERY.search(x)]

    def fetch_gallery_data_for_update(self, url_list: list[str]) -> list[GalleryData]:
        galleries_data = self.get_values_from_gallery_link_list(url_list)

        if not (self.own_settings.use_ex_for_fjord and self.own_settings.cookies):
            return galleries_data

        # Same as crawl_urls: galleries with fjord tags need to be fetched again from the ex API.
        regular_galleries: list[GalleryData] = []
        fjord_links: list[str] = []
        for gallery_data in galleries_data:
            if gallery_data.token and re.search(constants.default_fjord_tags, ",".join(gallery_data.tags or [])):
                fjord_links.append(link_from_gid_token_fjord(gallery_data.gid, gallery_data.token, True))
            else:
                regular_galleries.append(gallery_data)

        if fjord_links:
            regular_galleries.extend(self.get_values_from_gallery_link_list(fjord_links, True))

        return regular_galleries

    @staticmethod
    def id_from_url(url: str) -> str | None:
        m = re.search(r"(.+)/g/(\d+)/(\w+)", url)
        if m and m.group(2):
            return m.group(2)
        else:
            return None

    @staticmethod
    def token_from_url(url: str) -> str | None:
        m = re.search(r"(.+)/g/(\d+)/(\w+)", url)
        if m and m.group(3):
            return m.group(3)
        else:
            return None

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters: QuerySet | None = None,
        wanted_only: bool = False,
        preselected_wanted_matches: dict[str, list["WantedGallery"]] | None = None,
    ) -> None:
        self._pending_links = {}
        self._resolved_links = set()
        try:
            self._crawl_urls(urls, wanted_filters, wanted_only, preselected_wanted_matches)
        finally:
            self._finish_pending_links()

    def _crawl_urls(
        self,
        urls: list[str],
        wanted_filters: QuerySet | None = None,
        wanted_only: bool = False,
        preselected_wanted_matches: dict[str, list["WantedGallery"]] | None = None,
    ) -> None:

        capture_only = bool(getattr(self.settings, "capture_links_only", False))
        skip_capture = bool(getattr(self.settings, "skip_link_capture", False))
        unique_urls: set[str] = set()
        gallery_data_list = []
        fetch_format_galleries: list[DataDict] = []
        unique_page_urls = set()
        gallery_wanted_lists: dict[str, list["WantedGallery"]] = preselected_wanted_matches or defaultdict(list)

        if not self.downloaders:
            logger.warning("No downloaders enabled, returning.")
            return

        is_bulk = len(urls) > 50 or self.settings.silent_processing
        if is_bulk and not self.settings.silent_processing:
            logger.info("Classifying %d URLs for provider %s...", len(urls), self.name)

        skipped_invalid = 0
        for url in urls:

            if constants.rss_url in url:
                if skip_capture:
                    continue
                feed_links = self.crawl_feed(url)
                if capture_only:
                    self._record_and_check_processed_links(feed_links, unique_urls)
                    continue
                unique_urls.update(feed_links)
                logger.info(
                    "Provided RSS URL for provider ({}), adding {} found links".format(self.name, len(feed_links))
                )
                continue

            if constants.ex_page_short not in url and constants.ge_page_short not in url:
                skipped_invalid += 1
                if not is_bulk:
                    logger.warning("Invalid URL, skipping: {}".format(url))
                continue

            if capture_only and ("/g/" in url or "/s/" in url):
                # Only index pages have links to capture, single galleries are handled by the process run.
                continue

            if "/g/" in url:
                if not is_bulk and not self.settings.silent_processing:
                    logger.info("Provided URL {} is a gallery link, adding".format(url))
                unique_urls.add(url)
                continue

            if "/s/" in url:
                if not is_bulk and not self.settings.silent_processing:
                    logger.info("Provided URL {} is a page link, adding".format(url))
                unique_page_urls.add(url)
                continue

            # Do not crawl main page links if they were submitted anonymously, to prevent spam.
            if len(self.downloaders) == 1 and self.downloaders[0][0].type == "submit":
                continue

            # The index pages were already crawled by the capture run, only the stored links are processed.
            if skip_capture:
                continue

            if "/lofi/" in url:
                if not is_bulk and not self.settings.silent_processing:
                    logger.info("Provided URL {} is a lofi page link, adding".format(url))
                unique_urls.update(self.get_galleries_from_lofi_page_link(url))
                continue

            # assuming main page URLs
            unique_urls.update(self.get_galleries_from_main_page_link(url))

        if capture_only:
            logger.info("Link capture done for provider %s, %d links found.", self.name, len(unique_urls))
            return

        self._load_pending_links(unique_urls)

        monitored_link = getattr(self.own_settings, "monitored_link", None) or getattr(self.settings, "monitored_link", None)
        ml_suffix = f" for monitored link '{monitored_link.name}'" if monitored_link and hasattr(monitored_link, "name") else ""
        if (len(unique_urls) > 0 or len(unique_page_urls) > 0 or skipped_invalid > 0) and (
            is_bulk or len(urls) > 1 or len(unique_urls) > 1
        ):
            logger.info(
                "Extracted %d unique gallery links (%d page links%s) from %d URLs for provider %s%s.",
                len(unique_urls),
                len(unique_page_urls),
                f", {skipped_invalid} invalid skipped" if skipped_invalid else "",
                len(urls),
                self.name,
                ml_suffix,
            )

        gallery_ids = []
        found_galleries = set()
        total_galleries_filtered = []
        for gallery_url in unique_urls:

            m = RE_PANDA_GALLERY.search(gallery_url)
            if m:
                gallery_ids.append(m.group(2))
                total_galleries_filtered.append((gallery_url, m.group(1), m.group(2), m.group(3)))

        is_bulk_db = len(gallery_ids) > 50 or self.settings.silent_processing
        discarded_count = 0
        existing_galleries_in_db = set()

        for galleries_gid_group in list(chunks(gallery_ids, 900)):
            for found_gallery in Gallery.objects.filter(gid__in=galleries_gid_group).prefetch_related("archive_set"):
                existing_galleries_in_db.add(found_gallery.gid)
                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery=found_gallery, link=found_gallery.get_link()
                )

                if discard_approved:
                    discarded_count += 1
                    if not is_bulk_db and not self.settings.silent_processing:
                        logger.info(discard_message)
                    found_galleries.add(found_gallery.gid)

        self._resolved_links.update(found_galleries)

        self.log_database_check_summary(
            total=len(gallery_ids),
            existing=len(existing_galleries_in_db),
            discarded=discarded_count,
            to_update=len(existing_galleries_in_db) - discarded_count,
        )

        is_bulk_filtered = len(total_galleries_filtered) > 50 or self.settings.silent_processing
        for gallery_tuple in total_galleries_filtered:

            if gallery_tuple[2] not in found_galleries:
                fetch_format_galleries.append(
                    {"data": (gallery_tuple[2], gallery_tuple[3]), "root": gallery_tuple[1], "link": gallery_tuple[0]}
                )
                if not is_bulk_filtered and not self.settings.silent_processing:
                    logger.info(
                        "Gallery {} will be processed. "
                        "Total galleries: {}".format(gallery_tuple[0], len(fetch_format_galleries))
                    )

        if len(fetch_format_galleries) > 0:
            logger.info(
                "Queued %d galleries to fetch metadata (out of %d total filtered).",
                len(fetch_format_galleries),
                len(total_galleries_filtered),
            )

        if len(unique_page_urls) > 0:
            logger.info("Getting gallery links from page links...")
            page_links_results: list[DataDict] = []
            self.get_galleries_from_page_links(unique_page_urls, page_links_results)
            fetch_format_galleries += page_links_results

        if len(fetch_format_galleries) == 0:
            logger.info("No galleries need downloading, returning.")
            return

        fetch_format_galleries_chunks = list(chunks(fetch_format_galleries, 25))
        fjord_galleries: list[str] = []
        total_chunks = len(fetch_format_galleries_chunks)
        banned_count = 0
        api_error_count = 0
        unmatched_wanted_count = 0
        matched_wanted_count = 0
        for i, group in enumerate(fetch_format_galleries_chunks):
            if not self.settings.silent_processing:
                if total_chunks <= 50 or (i + 1) == 1 or (i + 1) == total_chunks or (i + 1) % 50 == 0:
                    logger.info(
                        "Calling non-fjord API ({}). "
                        "Gallery group: {}/{}, galleries in group: {}".format(
                            self.name, i + 1, total_chunks, len(group)
                        )
                    )

            data = utilities.request_data_from_gid_token_iterable([x["data"] for x in group])

            headers = {"Content-Type": "application/json"}

            request_dict = construct_request_dict(self.settings, self.own_settings)
            request_dict["headers"] = request_dict["headers"] | headers
            request_dict["data"] = json.dumps(data)

            response = self.api_request_function(
                constants.ge_api_url,
                request_dict,
                post=True,
            )

            response_data = self._parse_api_response_json(response, constants.ge_api_url)
            if not response_data:
                api_error_count += len(group)
                continue

            for gallery_data in response_data.get("gmetadata", []):
                if "error" in gallery_data:
                    api_error_count += 1
                    logger.error(
                        "Adding gallery {}: " "failed with error: {}".format(gallery_data["gid"], gallery_data["error"])
                    )
                    continue
                self._resolved_links.add(str(gallery_data["gid"]))
                internal_gallery_data = map_external_gallery_data_to_internal(gallery_data)
                link = link_from_gid_token_fjord(gallery_data["gid"], gallery_data["token"], False)
                internal_gallery_data.link = link

                banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                    internal_gallery_data.tags, internal_gallery_data.uploader, gallery_data=internal_gallery_data
                )

                if banned_result:
                    banned_count += 1
                    if self.gallery_callback:
                        self.gallery_callback(None, internal_gallery_data.link, "banned_data")

                    if not self.settings.silent_processing:
                        logger.info("Skipping gallery link {}, discarded reasons: {}".format(link, banned_reasons))
                    continue

                if wanted_filters:
                    self.compare_gallery_with_wanted_filters(
                        internal_gallery_data, link, wanted_filters, gallery_wanted_lists
                    )
                    if wanted_only and not gallery_wanted_lists[internal_gallery_data.gid]:
                        unmatched_wanted_count += 1
                        continue

                if len(gallery_wanted_lists.get(internal_gallery_data.gid, [])) > 0:
                    matched_wanted_count += 1

                if self.own_settings.use_ex_for_fjord:
                    m = re.search(constants.default_fjord_tags, ",".join(internal_gallery_data.tags))

                    if m and self.own_settings.cookies:
                        fjord_galleries.append(
                            link_from_gid_token_fjord(gallery_data["gid"], gallery_data["token"], True)
                        )
                    else:
                        gallery_data_list.append(internal_gallery_data)
                else:
                    gallery_data_list.append(internal_gallery_data)

        if self.own_settings.use_ex_for_fjord and fjord_galleries:
            fjord_galleries_data = self.get_values_from_gallery_link_list(fjord_galleries, True)

            if fjord_galleries_data:
                gallery_data_list.extend(fjord_galleries_data)

        self.log_metadata_evaluation_summary(
            total_evaluated=len(fetch_format_galleries),
            accepted=len(gallery_data_list),
            matched_wanted=matched_wanted_count,
            unmatched_wanted=unmatched_wanted_count,
            banned=banned_count,
            errors=api_error_count,
            wanted_filters=bool(wanted_filters),
            wanted_only=wanted_only,
        )

        self.pass_gallery_data_to_downloaders(gallery_data_list, gallery_wanted_lists)

    def post_gallery_processing(self, gallery_entry: "Gallery", gallery_data: "GalleryData"):

        extra_gal_data = gallery_data.extra_data

        galleries_to_add: dict[str, list[str]] = defaultdict(list)
        entry_needs_save = False

        if self.own_settings.auto_process_newer:
            if "current_gid" in extra_gal_data and "current_key" in extra_gal_data:
                current_gid = str(extra_gal_data["current_gid"])
                calling_gid = str(self.settings.link_child or self.settings.link_newer or "")
                is_caller = calling_gid == current_gid
                is_self = current_gid == str(gallery_entry.gid)

                if not is_self and not is_caller:
                    existing_current = Gallery.objects.filter(
                        gid=current_gid, provider=self.name
                    ).first()

                    if existing_current:
                        logger.info(
                            "Gallery: {} has current in: {}, already in database, skipping queue".format(
                                gallery_entry.get_absolute_url(), existing_current.get_absolute_url()
                            )
                        )
                    else:
                        current_url = link_from_gid_token_fjord(extra_gal_data["current_gid"], extra_gal_data["current_key"])

                        logger.info(
                            "Gallery: {} has current in: {}, adding to queue".format(
                                gallery_entry.get_absolute_url(), current_url
                            )
                        )
                        galleries_to_add[current_url] = []

        if self.own_settings.auto_process_first:
            if "first_gid" in extra_gal_data and "first_key" in extra_gal_data:
                first_gid = str(extra_gal_data["first_gid"])
                if first_gid != str(gallery_entry.gid):
                    existing_first = Gallery.objects.filter(
                        gid=first_gid, provider=self.name
                    ).first()

                    if existing_first:
                        if hasattr(gallery_entry, "first_gallery_id") and not gallery_entry.first_gallery_id:
                            gallery_entry.first_gallery = existing_first
                            entry_needs_save = True
                        logger.info(
                            "Gallery: {} has first in: {}, already in database, skipping queue".format(
                                gallery_entry.get_absolute_url(), existing_first.get_absolute_url()
                            )
                        )
                    else:
                        first_url = link_from_gid_token_fjord(extra_gal_data["first_gid"], extra_gal_data["first_key"])

                        logger.info(
                            "Gallery: {} has first in: {}, adding to queue".format(gallery_entry.get_absolute_url(), first_url)
                        )
                        galleries_to_add[first_url].extend(["--link-newer", str(gallery_entry.gid)])

        if self.own_settings.auto_process_parent:
            if "parent_gid" in extra_gal_data and "parent_key" in extra_gal_data:
                parent_gid = str(extra_gal_data["parent_gid"])
                if parent_gid != str(gallery_entry.gid):
                    existing_parent = Gallery.objects.filter(
                        gid=parent_gid, provider=self.name
                    ).first()

                    if existing_parent:
                        if hasattr(gallery_entry, "parent_gallery_id") and not gallery_entry.parent_gallery_id:
                            gallery_entry.parent_gallery = existing_parent
                            entry_needs_save = True
                        logger.info(
                            "Gallery: {} has parent in: {}, already in database, skipping queue".format(
                                gallery_entry.get_absolute_url(), existing_parent.get_absolute_url()
                            )
                        )
                    else:
                        parent_url = link_from_gid_token_fjord(extra_gal_data["parent_gid"], extra_gal_data["parent_key"])

                        logger.info(
                            "Gallery: {} has parent in: {}, adding to queue".format(
                                gallery_entry.get_absolute_url(), parent_url
                            )
                        )
                        galleries_to_add[parent_url].extend(["--link-child", str(gallery_entry.gid)])

        if entry_needs_save and hasattr(gallery_entry, "save"):
            gallery_entry.save()

        if self.settings.workers.web_queue and galleries_to_add:
            list_galleries_to_add = [[x] + galleries_to_add[x] for x in galleries_to_add.keys()]
            for gallery_to_add in list_galleries_to_add:
                self.settings.workers.web_queue.enqueue_args_list(
                    gallery_to_add,
                    override_options=self.settings,
                    gallery_callback=self.gallery_callback,
                    archive_callback=self.archive_callback
                )

    def is_current_link_non_current(self, gallery_data: "GalleryData") -> bool:
        if "current_gid" in gallery_data.extra_data:
            return True
        return False


API = (Parser,)
