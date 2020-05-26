# -*- coding: utf-8 -*-
import typing
from collections import defaultdict
from datetime import datetime, timezone
from typing import List, Dict, Optional, Iterable

from core.base.parsers import BaseParser


# Generic parser, meaning that only downloads archives, no metadata.
from core.base.utilities import chunks, request_with_retries, construct_request_dict

from .utilities import ChaikaGalleryData
from . import constants

if typing.TYPE_CHECKING:
    from viewer.models import WantedGallery


class Parser(BaseParser):
    name = constants.provider_name
    ignore = False
    accepted_urls = ['gs=', 'gsp=', 'gd=']

    def filter_accepted_urls(self, urls: Iterable[str]) -> List[str]:
        return [x for x in urls if any(word in x for word in self.accepted_urls) and self.own_settings.url in x]

    def get_feed_urls(self) -> List[str]:
        return [self.own_settings.feed_url, ]

    def crawl_feed(self, feed_url: str = None) -> Optional[str]:

        return feed_url

    def crawl_urls(self, urls: List[str], wanted_filters=None, wanted_only: bool = False) -> None:

        request_dict = construct_request_dict(self.settings, self.own_settings)

        for url in urls:
            response = request_with_retries(
                url,
                request_dict,
                post=False,
                logger=self.logger
            )

            dict_list = []

            try:
                json_decoded = response.json()
            except(ValueError, KeyError):
                self.logger.error("Error parsing response to JSON: {}".format(response.text))
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
            total_galleries_filtered: List[ChaikaGalleryData] = []
            gallery_wanted_lists: Dict[str, List['WantedGallery']] = defaultdict(list)

            for gallery in dict_list:
                if 'result' in gallery:
                    continue
                galleries_gids.append(gallery['gid'])
                gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
                gallery_data = ChaikaGalleryData(**gallery)
                total_galleries_filtered.append(gallery_data)

            for galleries_gid_group in list(chunks(galleries_gids, 900)):
                for found_gallery in self.settings.gallery_model.objects.filter(gid__in=galleries_gid_group):
                    discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                        gallery=found_gallery,
                        link=found_gallery.get_link()
                    )

                    if discard_approved:
                        self.logger.info("{} Real GID: {}".format(discard_message, found_gallery.gid))
                        found_galleries.add(found_gallery.gid)

            for count, gallery in enumerate(total_galleries_filtered):

                if gallery.gid in found_galleries:
                    continue

                if self.general_utils.discard_by_tag_list(gallery.tags):
                    self.logger.info(
                        "Skipping gallery {}, because it's tagged with global discarded tags".format(gallery.title)
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

                self.logger.info(
                    "Gallery {} of {}: Gallery {} (GID: {}) will be processed.".format(
                        count,
                        len(total_galleries_filtered),
                        gallery.title,
                        gallery.gid
                    )
                )

                if gallery.thumbnail:
                    original_thumbnail_url = gallery.thumbnail_url

                    gallery.thumbnail_url = gallery.thumbnail

                    gallery_obj = self.settings.gallery_model.objects.update_or_create_from_values(gallery)

                    gallery_obj.thumbnail_url = original_thumbnail_url

                    gallery_obj.save()
                else:
                    self.settings.gallery_model.objects.update_or_create_from_values(gallery)

                for archive in gallery.archives:
                    gallery.archiver_key = archive
                    self.pass_gallery_data_to_downloaders([gallery], gallery_wanted_lists)


API = (
    Parser,
)
