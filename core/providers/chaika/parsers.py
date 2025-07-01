# -*- coding: utf-8 -*-
import logging
import time
import typing
from typing import Optional
import urllib
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
import re
from urllib.parse import urljoin

from core.base.parsers import BaseParser
from viewer.models import Gallery
from core.base.utilities import chunks, request_with_retries, construct_request_dict

from .utilities import ChaikaGalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery

logger = logging.getLogger(__name__)


# Generic parser, meaning that only downloads archives, no metadata.
class Parser(BaseParser):
    name = constants.provider_name
    ignore = False
    accepted_urls = ["gs=", "as=", "gsp=", "gd=", "/archive/", "/gallery/", "/es-gallery-json/"]

    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if any(word in x for word in self.accepted_urls) and self.own_settings.url in x]

    def get_feed_urls(self) -> list[str]:
        return [
            self.own_settings.feed_url,
        ]

    def crawl_feed(self, feed_url: str = "") -> list[ChaikaGalleryData]:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = request_with_retries(
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

            response = request_with_retries(
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
            current_page += 1
            params = {"page": [str(current_page)]}
            query.update(params)
            new_query = urllib.parse.urlencode(query, doseq=True)
            feed_url = urllib.parse.urlunparse(list(parsed[0:4]) + [new_query] + list(parsed[5:]))
            time.sleep(self.own_settings.wait_timer)

        total_galleries_filtered: list[ChaikaGalleryData] = list(unique_urls.values())

        return total_galleries_filtered

    def crawl_elastic_json(self, feed_url: str = "") -> list[ChaikaGalleryData]:

        # Since this source only has metadata, we re-enable other downloaders

        request_dict = construct_request_dict(self.settings, self.own_settings)

        response = request_with_retries(
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

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters=None,
        wanted_only: bool = False,
        preselected_wanted_matches: Optional[dict[str, list["WantedGallery"]]] = None,
    ) -> None:

        # If we are crawling an url from a Wanted source (MonitoredLinks), force download using default downloaders
        # from each gallery's original provider, instead of just downloading the archive from chaika
        # This helps in providing a way of having a high frequency crawler that captures most links, then having
        # another instance that parses the results from that one.

        # TODO: The problem with this implementation is that we are forcing downloaders from other providers when using
        # WantedGallery checks, you could still want to just download metadata only.
        # we could expose an option per MonitoredLink (argument to crawler), that gets here through crawl_urls.
        # if wanted_only:
        #     force_provider = True
        # else:
        #     force_provider = False

        for url in urls:

            galleries_gids = []

            dict_list = []
            request_dict = construct_request_dict(self.settings, self.own_settings)
            total_galleries_filtered: list[ChaikaGalleryData] = []

            if "/archive/" in url:
                match_archive_pk = re.search(r"/archive/(\d+)/", url)
                if match_archive_pk:
                    api_url = urljoin(self.own_settings.url, constants.api_path)
                    request_dict["params"] = {"archive": match_archive_pk.group(1)}
                    archive_response = request_with_retries(
                        api_url,
                        request_dict,
                        post=False,
                    )
                    if not archive_response:
                        logger.error("Did not get a response from URL: {}".format(api_url))
                        continue
                    try:
                        json_decoded = archive_response.json()
                    except (ValueError, KeyError):
                        logger.error("Could not parse response to JSON: {}".format(archive_response.text))
                        continue
                    if json_decoded["gallery"]:
                        request_dict["params"] = {"gd": json_decoded["gallery"]}
                        gallery_response = request_with_retries(
                            api_url,
                            request_dict,
                            post=False,
                        )
                        if not gallery_response:
                            logger.error("Did not get a response from URL: {}".format(api_url))
                            continue
                        try:
                            json_decoded = gallery_response.json()
                            dict_list.append(json_decoded)
                        except (ValueError, KeyError):
                            logger.error("Could not parse response to JSON: {}".format(gallery_response.text))
                            continue
                    else:
                        logger.error("Archive: {} does not have an associated Gallery".format(url))
                        continue
            elif "/gallery/" in url:
                match_gallery_pk = re.search(r"/gallery/(\d+)/", url)
                if match_gallery_pk:
                    api_url = urljoin(self.own_settings.url, constants.api_path)
                    request_dict["params"] = {"gd": match_gallery_pk.group(1)}
                    gallery_response = request_with_retries(
                        api_url,
                        request_dict,
                        post=False,
                    )
                    if not gallery_response:
                        logger.error("Did not get a response from URL: {}".format(api_url))
                        continue
                    try:
                        json_decoded = gallery_response.json()
                        dict_list.append(json_decoded)
                    except (ValueError, KeyError):
                        logger.error("Could not parse response to JSON: {}".format(gallery_response.text))
                        continue
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
                response = request_with_retries(
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

            for gallery in dict_list:
                if "result" in gallery:
                    continue
                galleries_gids.append(gallery["gid"])
                gallery["posted"] = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
                gallery_data = ChaikaGalleryData(**gallery)
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

            for count, gallery in enumerate(total_galleries_filtered, start=1):

                if gallery.gid in found_galleries:
                    continue

                banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                    gallery.tags, gallery.uploader
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
                        for archive in gallery.archives:
                            gallery.temp_archive = archive
                            self.pass_gallery_data_to_downloaders([gallery], gallery_wanted_lists)

            # TODO: We are assuming that only when running wanted_only is when we'll want to use each gallery's own
            # provider. Might need a way to also force this when crawling a link.
            if wanted_only:
                # This doesn't work, specifically with providers that get extra data from their own APIs (panda)
                # conv_data_list = [x.to_gallery_data() for x in gallery_data_list]
                # self.pass_gallery_data_to_downloaders(conv_data_list, gallery_wanted_lists, force_provider=force_provider)

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
                    self.settings.workers.web_queue.enqueue_args_list(gallery_links)


API = (Parser,)
