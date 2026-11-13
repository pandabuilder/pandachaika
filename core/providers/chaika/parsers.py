# -*- coding: utf-8 -*-
import copy
import logging
import typing
import urllib
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
import re
from urllib.parse import urljoin

from core.base.parsers import BaseParser
from core.base.types import GalleryData
from viewer.models import Gallery
from core.base.utilities import chunks, request_by_provider, construct_request_dict

from .utilities import ChaikaGalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


# Generic parser, meaning that only downloads archives, no metadata.
class Parser(BaseParser):
    name = constants.provider_name
    ignore = False
    accepted_urls = ["gs=", "as=", "gsp=", "gd=", "/archive/", "/gallery/", "/es-gallery-json/"]

    def __init__(self, settings: "Settings") -> None:
        super().__init__(settings)
        self.web_request_function = request_by_provider(
            self.name + "_web", 1, self.own_settings.wait_timer
        )
        # gid -> (chaika thumbnail url, original thumbnail url), used by the metadata updater.
        self.pending_thumbnails: dict[str, tuple[str, str | None]] = {}

    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if any(word in x for word in self.accepted_urls) and self.own_settings.url in x]

    def get_feed_urls(self) -> list[str]:
        return [
            self.own_settings.feed_url,
        ]

    def crawl_feed(self, feed_url: str = "") -> list[ChaikaGalleryData]:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = self.web_request_function(
            feed_url,
            request_dict,
            post=False,
        )

        dict_list = []

        if not response:
            return []

        try:
            json_decoded = response.json()
        except (ValueError, KeyError):
            logger.error("Could not parse response to JSON: {}".format(response.text))
            return []

        if isinstance(json_decoded, dict):
            if "galleries" in json_decoded:
                dict_list = json_decoded["galleries"]
            else:
                dict_list.append(json_decoded)
        elif isinstance(json_decoded, list):
            dict_list = json_decoded

        total_galleries_filtered: list[ChaikaGalleryData] = []

        for gallery in dict_list:
            if "result" in gallery:
                continue
            gallery["posted"] = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
            gallery_data = ChaikaGalleryData(**gallery)
            total_galleries_filtered.append(gallery_data)

        return total_galleries_filtered

    def crawl_elastic_json_paginated(self, feed_url: str = "") -> list[ChaikaGalleryData]:

        unique_urls: dict[str, ChaikaGalleryData] = dict()

        while True:

            parsed = urllib.parse.urlparse(feed_url)
            query = urllib.parse.parse_qs(parsed.query)
            if "page" in query:
                current_page = int(query["page"][0])
            else:
                params = {"page": ["1"]}
                query.update(params)
                new_query = urllib.parse.urlencode(query, doseq=True)
                feed_url = urllib.parse.urlunparse(list(parsed[0:4]) + [new_query] + list(parsed[5:]))
                current_page = 1

            request_dict = construct_request_dict(self.settings, self.own_settings)

            response = self.web_request_function(
                feed_url,
                request_dict,
                post=False,
            )

            if not response:
                break

            try:
                json_decoded = response.json()
            except (ValueError, KeyError):
                logger.error("Could not parse response to JSON: {}".format(response.text))
                break

            dict_list = json_decoded["hits"]
            limit_page = json_decoded["paginator"]["num_pages"]

            for gallery in dict_list:
                gallery["posted"] = datetime.fromisoformat(gallery["posted_date"].replace("+0000", "+00:00"))
                gallery["link"] = gallery["source_url"]
                parser = self.settings.provider_context.get_parsers(self.settings, filter_name=gallery["provider"])[0]
                gid = parser.id_from_url(gallery["source_url"])
                if gid:
                    token = parser.token_from_url(gallery["source_url"])
                    gallery["token"] = token
                    gallery["tags"] = [x["full"] for x in gallery["tags"]]
                    if "gid" in gallery:
                        del gallery["gid"]
                    gallery_data = ChaikaGalleryData(gid, **gallery)
                    if "source_thumbnail" in gallery:
                        gallery_data.thumbnail_url = gallery["source_thumbnail"]
                    if gallery["source_url"] not in unique_urls:
                        unique_urls[gallery["source_url"]] = gallery_data

            if self.own_settings.stop_page_number is not None:
                if current_page >= self.own_settings.stop_page_number:
                    logger.info(
                        "Got to stop page number: {}, "
                        "ending (setting: provider.stop_page_number).".format(self.own_settings.stop_page_number)
                    )
                    break
                    
            if current_page >= limit_page:
                logger.info(
                    "Got to last page: {}.".format(
                        limit_page
                    )
                )
                break
                    
            current_page += 1
            params = {"page": [str(current_page)]}
            query.update(params)
            new_query = urllib.parse.urlencode(query, doseq=True)
            feed_url = urllib.parse.urlunparse(list(parsed[0:4]) + [new_query] + list(parsed[5:]))

        total_galleries_filtered: list[ChaikaGalleryData] = list(unique_urls.values())

        return total_galleries_filtered

    def crawl_elastic_json(self, feed_url: str = "") -> list[ChaikaGalleryData]:

        # Since this source only has metadata, we re-enable other downloaders

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = self.web_request_function(
            feed_url,
            request_dict,
            post=False,
        )

        if not response:
            return []

        try:
            json_decoded = response.json()
        except (ValueError, KeyError):
            logger.error("Could not parse response to JSON: {}".format(response.text))
            return []

        dict_list = json_decoded["hits"]

        total_galleries_filtered: list[ChaikaGalleryData] = []

        for gallery in dict_list:
            gallery["posted"] = datetime.fromisoformat(gallery["posted_date"].replace("+0000", "+00:00"))
            gallery["link"] = gallery["source_url"]
            parser = self.settings.provider_context.get_parsers(self.settings, filter_name=gallery["provider"])[0]
            gid = parser.id_from_url(gallery["source_url"])
            if gid:
                token = (parser.token_from_url(gallery["source_url"]),)
                gallery["token"] = token
                gallery["tags"] = [x["full"] for x in gallery["tags"]]
                if "gid" in gallery:
                    del gallery["gid"]
                gallery_data = ChaikaGalleryData(gid, **gallery)
                total_galleries_filtered.append(gallery_data)

        return total_galleries_filtered

    def fetch_gallery_dicts_from_link(
        self, url: str, request_dict: dict[str, typing.Any] | None = None
    ) -> list[dict[str, typing.Any]] | None:
        """Get the gallery data, as returned by the API, for a single /archive/<pk>/ or /gallery/<pk>/ link.

        Returns None if the request failed, or an empty list if the link does not point to a gallery.
        """
        if request_dict is None:
            request_dict = construct_request_dict(self.settings, self.own_settings)

        dict_list: list[dict[str, typing.Any]] = []
        api_url = urljoin(self.own_settings.url, constants.api_path)

        if "/archive/" in url:
            match_archive_pk = re.search(r"/archive/(\d+)/", url)
            if match_archive_pk:
                request_dict["params"] = {"archive": match_archive_pk.group(1)}
                archive_response = self.web_request_function(
                    api_url,
                    request_dict,
                    post=False,
                )
                if not archive_response:
                    logger.error("Did not get a response from URL: {}".format(api_url))
                    return None
                try:
                    json_decoded = archive_response.json()
                except (ValueError, KeyError):
                    logger.error("Could not parse response to JSON: {}".format(archive_response.text))
                    return None
                if json_decoded["gallery"]:
                    request_dict["params"] = {"gd": json_decoded["gallery"]}
                    gallery_response = self.web_request_function(
                        api_url,
                        request_dict,
                        post=False,
                    )
                    if not gallery_response:
                        logger.error("Did not get a response from URL: {}".format(api_url))
                        return None
                    try:
                        json_decoded = gallery_response.json()
                        dict_list.append(json_decoded)
                    except (ValueError, KeyError):
                        logger.error("Could not parse response to JSON: {}".format(gallery_response.text))
                        return None
                else:
                    logger.error("Archive: {} does not have an associated Gallery".format(url))
                    return None
        elif "/gallery/" in url:
            match_gallery_pk = re.search(r"/gallery/(\d+)/", url)
            if match_gallery_pk:
                request_dict["params"] = {"gd": match_gallery_pk.group(1)}
                gallery_response = self.web_request_function(
                    api_url,
                    request_dict,
                    post=False,
                )
                if not gallery_response:
                    logger.error("Did not get a response from URL: {}".format(api_url))
                    return None
                try:
                    json_decoded = gallery_response.json()
                    dict_list.append(json_decoded)
                except (ValueError, KeyError):
                    logger.error("Could not parse response to JSON: {}".format(gallery_response.text))
                    return None

        return dict_list

    # Support for core.web.gallery_updater.GalleryMetadataUpdater
    update_fetch_chunk_size = 10

    def filter_update_urls(self, urls: Iterable[str]) -> list[str]:
        return [
            x
            for x in super().filter_update_urls(urls)
            if re.search(r"/(gallery|archive)/\d+/", x)
        ]

    def fetch_gallery_data_for_update(self, url_list: list[str]) -> list[GalleryData]:
        galleries_data: list[GalleryData] = []
        request_dict = construct_request_dict(self.settings, self.own_settings)

        for url in url_list:
            api_dicts = self.fetch_gallery_dicts_from_link(url, dict(request_dict))
            if not api_dicts:
                continue
            for gallery in api_dicts:
                if "result" in gallery:
                    continue
                gallery["posted"] = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
                gallery_data = ChaikaGalleryData(**gallery)
                # Chaika serves its own copy of the thumbnail. As in crawl_urls, don't store its local path/URL
                # in the Gallery, the thumbnail is downloaded from there in after_gallery_update.
                if gallery_data.thumbnail:
                    self.pending_thumbnails[gallery_data.gid] = (str(gallery_data.thumbnail), gallery_data.thumbnail_url)
                    gallery_data.thumbnail = ""
                    gallery_data.thumbnail_url = ""
                galleries_data.append(gallery_data)

        return galleries_data

    def after_gallery_update(self, gallery: "Gallery", gallery_data: GalleryData) -> None:
        pending = self.pending_thumbnails.pop(gallery_data.gid, None)
        if not pending:
            return
        chaika_thumbnail_url, original_thumbnail_url = pending

        gallery.thumbnail_url = chaika_thumbnail_url
        gallery.fetch_thumbnail(force_provider=self.name)
        gallery.thumbnail_url = original_thumbnail_url or ""
        gallery.save()

    def do_get_gallery_discard_reason(
        self, gallery_id: str | None = None, link: str = "", gallery: "Gallery | None" = None
    ) -> tuple[bool, str]:
        if (
            not gallery
            and gallery_id
            and getattr(self.settings, "current_task_type", "")
            in ("gallery_creator", "gallery_worker", "create_galleries")
        ):
            gallery = Gallery.objects.filter_first(gid=gallery_id, provider=self.name)
            if gallery and not gallery.archive_set.all() and not self.settings.replace_metadata:
                return False, "Gallery {title}, {ext_link} pre-created by chaika crawl without archives, processing.".format(
                    ext_link=gallery.get_link(),
                    title=gallery.title,
                )
        return super().do_get_gallery_discard_reason(gallery_id=gallery_id, link=link, gallery=gallery)

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters=None,
        wanted_only: bool = False,
        preselected_wanted_matches: dict[str, list["WantedGallery"]] | None = None,
    ) -> None:

        # If we are crawling an url from a Wanted source (MonitoredLinks), force download using default downloaders
        # from each gallery's original provider, instead of just downloading the archive from chaika
        # This helps in providing a way of having a high frequency crawler that captures most links, then having
        # another instance that parses the results from that one.

        for url in urls:

            galleries_gids = []

            dict_list = []
            request_dict = construct_request_dict(self.settings, self.own_settings)
            total_galleries_filtered: list[ChaikaGalleryData] = []

            if "/archive/" in url or "/gallery/" in url:
                api_dicts = self.fetch_gallery_dicts_from_link(url, request_dict)
                if api_dicts is None:
                    continue
                dict_list.extend(api_dicts)
            elif "/es-gallery-json/" in url:
                parse_results = self.crawl_elastic_json_paginated(url)
                if parse_results:
                    galleries_gids.extend([x.gid for x in parse_results])
                    total_galleries_filtered.extend(parse_results)

                    logger.info(
                        "Provided JSON URL for provider ({}), found {} links".format(
                            self.name, len(total_galleries_filtered)
                        )
                    )
            else:
                response = self.web_request_function(
                    url,
                    request_dict,
                    post=False,
                )

                if not response:
                    logger.error("Did not get a response from URL: {}".format(url))
                    continue

                try:
                    json_decoded = response.json()
                except (ValueError, KeyError):
                    logger.error("Could not parse response to JSON: {}".format(response.text))
                    continue

                if isinstance(json_decoded, dict):
                    if "galleries" in json_decoded:
                        dict_list = json_decoded["galleries"]
                    else:
                        dict_list.append(json_decoded)
                elif isinstance(json_decoded, list):
                    dict_list = json_decoded

            found_galleries = set()
            gallery_wanted_lists: dict[str, list["WantedGallery"]] = preselected_wanted_matches or defaultdict(list)

            for gal_dict in dict_list:
                if "result" in gal_dict:
                    continue
                galleries_gids.append(gal_dict["gid"])
                gal_dict["posted"] = datetime.fromtimestamp(int(gal_dict["posted"]), timezone.utc)
                gallery_data = ChaikaGalleryData(**gal_dict)
                total_galleries_filtered.append(gallery_data)

            for galleries_gid_group in list(chunks(galleries_gids, 900)):
                for found_gallery in Gallery.objects.filter(gid__in=galleries_gid_group):
                    discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                        gallery=found_gallery, link=found_gallery.get_link()
                    )

                    if discard_approved:
                        if not self.settings.silent_processing:
                            logger.info("{} Real GID: {}".format(discard_message, found_gallery.gid))
                        found_galleries.add(found_gallery.gid)

            gallery_data_list: list[ChaikaGalleryData] = []

            logger.info("Already found galleries: {}".format(len(found_galleries)))

            for count, gallery in enumerate(total_galleries_filtered, start=1):

                if gallery.gid in found_galleries:
                    continue

                banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                    gallery.tags, gallery.uploader, gallery_data=gallery
                )

                if banned_result:
                    if self.gallery_callback:
                        self.gallery_callback(None, gallery.link, "banned_data")

                    if not self.settings.silent_processing:
                        logger.info(
                            "Skipping gallery link {}, discarded reasons: {}".format(gallery.title, banned_reasons)
                        )
                    continue

                if wanted_filters:
                    self.compare_gallery_with_wanted_filters(
                        gallery, gallery.link, wanted_filters, gallery_wanted_lists
                    )

                    if wanted_only and not gallery_wanted_lists[gallery.gid]:
                        continue

                logger.info(
                    "Gallery {} of {}: Gallery {} (Real GID: {}) will be processed.".format(
                        count, len(total_galleries_filtered), gallery.title, gallery.gid
                    )
                )

                # We predownload the thumbnail for not wanted to avoid adding
                # too much code to parsers to deal with an existing Gallery
                if not wanted_only:

                    if gallery.thumbnail:
                        original_thumbnail_url = gallery.thumbnail_url
                        chaika_thumbnail_url = gallery.thumbnail

                        gallery.thumbnail_url = ""
                        gallery.thumbnail = ""

                        gallery_obj = Gallery.objects.update_or_create_from_values(gallery)

                        gallery_obj.thumbnail_url = chaika_thumbnail_url

                        gallery_obj.fetch_thumbnail(force_provider=self.name)

                        gallery_obj.thumbnail_url = original_thumbnail_url

                        gallery_obj.save()
                    else:
                        Gallery.objects.update_or_create_from_values(gallery)

                if wanted_only:
                    gallery_data_list.append(gallery)
                else:
                    # we don't force providers if the link already has an own archive to download
                    if gallery.archives:
                        archive_galleries: list[GalleryData] = []
                        for archive in gallery.archives:
                            archive_gallery = copy.copy(gallery)
                            archive_gallery.temp_archive = archive
                            archive_galleries.append(archive_gallery)
                        self.pass_gallery_data_to_downloaders(archive_galleries, gallery_wanted_lists)

            if wanted_only:

                gallery_links = [x.link for x in gallery_data_list if x.link]

                gallery_links.append("--no-wanted-check")

                prev_matched = []

                for matched_gallery_id, matched_gallery_wgs in gallery_wanted_lists.items():
                    for gallery_wg in matched_gallery_wgs:
                        prev_matched.append("{},{}".format(matched_gallery_id, gallery_wg.pk))

                if len(prev_matched) > 0:
                    gallery_links.append("--preselect-wanted-match")
                    for prev_matched_string in prev_matched:
                        gallery_links.append(prev_matched_string)

                if self.settings.workers.web_queue:
                    self.settings.workers.web_queue.enqueue_args_list(gallery_links, override_options=self.settings)


API = (Parser,)
