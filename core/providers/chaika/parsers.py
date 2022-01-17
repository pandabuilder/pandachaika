# -*- coding: utf-8 -*-
import logging
import typing
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
    accepted_urls = ['gs=', 'as=', 'gsp=', 'gd=', '/archive/', '/gallery/', '/es-gallery-json/']

    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if any(word in x for word in self.accepted_urls) and self.own_settings.url in x]

    def get_feed_urls(self) -> list[str]:
        return [self.own_settings.feed_url, ]

    def crawl_feed(self, feed_url: str = '') -> list[ChaikaGalleryData]:

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
        except(ValueError, KeyError):
            logger.error("Could not parse response to JSON: {}".format(response.text))
            return []

        if type(json_decoded) == dict:
            if 'galleries' in json_decoded:
                dict_list = json_decoded['galleries']
            else:
                dict_list.append(json_decoded)
        elif type(json_decoded) == list:
            dict_list = json_decoded

        total_galleries_filtered: list[ChaikaGalleryData] = []

        for gallery in dict_list:
            if 'result' in gallery:
                continue
            gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
            gallery_data = ChaikaGalleryData(**gallery)
            total_galleries_filtered.append(gallery_data)

        return total_galleries_filtered

    def crawl_elastic_json(self, feed_url: str = '') -> list[ChaikaGalleryData]:

        # Since this source only has metadata, we reeable other downloaders

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
        except(ValueError, KeyError):
            logger.error("Could not parse response to JSON: {}".format(response.text))
            return []

        dict_list = json_decoded['hits']

        total_galleries_filtered: list[ChaikaGalleryData] = []

        for gallery in dict_list:
            gallery['posted'] = datetime.fromisoformat(gallery['posted_date'].replace("+0000", "+00:00"))
            parser = self.settings.provider_context.get_parsers(self.settings, filter_name=gallery['provider'])[0]
            gid = parser.id_from_url(gallery['source_url'])
            if gid:
                token = parser.token_from_url(gallery['source_url']),
                gallery['token'] = token
                gallery['tags'] = [x['full'] for x in gallery['tags']]
                gallery_data = ChaikaGalleryData(gid, **gallery)
                total_galleries_filtered.append(gallery_data)

        return total_galleries_filtered

    def crawl_urls(self, urls: list[str], wanted_filters=None, wanted_only: bool = False) -> None:

        for url in urls:

            dict_list = []
            request_dict = construct_request_dict(self.settings, self.own_settings)
            total_galleries_filtered: list[ChaikaGalleryData] = []

            if '/archive/' in url:
                match_archive_pk = re.search(r'/archive/(\d+)/', url)
                if match_archive_pk:
                    api_url = urljoin(self.own_settings.url, constants.api_path)
                    request_dict['params'] = {'archive': match_archive_pk.group(1)}
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
                    except(ValueError, KeyError):
                        logger.error("Could not parse response to JSON: {}".format(archive_response.text))
                        continue
                    if json_decoded['gallery']:
                        request_dict['params'] = {'gd': json_decoded['gallery']}
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
                        except(ValueError, KeyError):
                            logger.error("Could not parse response to JSON: {}".format(gallery_response.text))
                            continue
                    else:
                        logger.error("Archive: {} does not have an associated Gallery".format(url))
                        continue
            elif '/gallery/' in url:
                match_gallery_pk = re.search(r'/gallery/(\d+)/', url)
                if match_gallery_pk:
                    api_url = urljoin(self.own_settings.url, constants.api_path)
                    request_dict['params'] = {'gd': match_gallery_pk.group(1)}
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
                    except(ValueError, KeyError):
                        logger.error("Could not parse response to JSON: {}".format(gallery_response.text))
                        continue
            # TODO: This is disabled until there's a way to pass the metadata, to other providers' downloaders
            # elif '/es-gallery-json/' in url:
            #     parse_results = self.crawl_elastic_json()
            #     if parse_results:
            #         total_galleries_filtered.extend(parse_results)
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
                except(ValueError, KeyError):
                    logger.error("Could not parse response to JSON: {}".format(response.text))
                    continue

                if type(json_decoded) == dict:
                    if 'galleries' in json_decoded:
                        dict_list = json_decoded['galleries']
                    else:
                        dict_list.append(json_decoded)
                elif type(json_decoded) == list:
                    dict_list = json_decoded

            galleries_gids = []
            found_galleries = set()
            gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

            for gallery in dict_list:
                if 'result' in gallery:
                    continue
                galleries_gids.append(gallery['gid'])
                gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
                gallery_data = ChaikaGalleryData(**gallery)
                total_galleries_filtered.append(gallery_data)

            for galleries_gid_group in list(chunks(galleries_gids, 900)):
                for found_gallery in Gallery.objects.filter(gid__in=galleries_gid_group):
                    discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                        gallery=found_gallery,
                        link=found_gallery.get_link()
                    )

                    if discard_approved:
                        logger.info("{} Real GID: {}".format(discard_message, found_gallery.gid))
                        found_galleries.add(found_gallery.gid)

            for count, gallery in enumerate(total_galleries_filtered, start=1):

                if gallery.gid in found_galleries:
                    continue

                discarded_tags = self.general_utils.discard_by_tag_list(gallery.tags)

                if discarded_tags:
                    logger.info(
                        "Skipping gallery link {}, because it's tagged with global discarded tags: {}".format(
                            gallery.title,
                            discarded_tags
                        )
                    )
                    continue

                if wanted_filters:
                    self.compare_gallery_with_wanted_filters(
                        gallery,
                        gallery.link,
                        wanted_filters,
                        gallery_wanted_lists
                    )
                    if wanted_only and not gallery_wanted_lists[gallery.gid]:
                        continue

                logger.info(
                    "Gallery {} of {}: Gallery {} (Real GID: {}) will be processed.".format(
                        count,
                        len(total_galleries_filtered),
                        gallery.title,
                        gallery.gid
                    )
                )

                if gallery.thumbnail:
                    original_thumbnail_url = gallery.thumbnail_url

                    gallery.thumbnail_url = gallery.thumbnail

                    gallery_obj = Gallery.objects.update_or_create_from_values(gallery)

                    gallery_obj.thumbnail_url = original_thumbnail_url

                    gallery_obj.save()
                else:
                    Gallery.objects.update_or_create_from_values(gallery)

                for archive in gallery.archives:
                    gallery.temp_archive = archive
                    self.pass_gallery_data_to_downloaders([gallery], gallery_wanted_lists)


API = (
    Parser,
)
