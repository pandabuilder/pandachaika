import copy
import json
import typing
from datetime import datetime, timezone
import time
from typing import List, Optional, Dict, Iterable, Tuple, Callable

import django.utils.timezone as django_tz
import logging

import traceback

from collections import defaultdict

from django.db.models import QuerySet

from core.base import utilities
from core.base.utilities import send_pushover_notification, chunks, compare_search_title_with_strings
from core.base.types import GalleryData, OptionalLogger, FakeLogger, RealLogger
from viewer.signals import wanted_gallery_found

if typing.TYPE_CHECKING:
    from core.downloaders.handlers import BaseDownloader
    from core.base.setup import Settings
    from viewer.models import Gallery, WantedGallery, Archive


class BaseParser:
    name = ''
    ignore = False
    accepted_urls: List[str] = []

    def __init__(self, settings: 'Settings', logger: OptionalLogger = None) -> None:
        self.settings = settings
        if not logger:
            self.logger: RealLogger = FakeLogger()
        else:
            self.logger = logger
        if self.name in settings.providers:
            self.own_settings = settings.providers[self.name]
        else:
            self.own_settings = None
        self.general_utils = utilities.GeneralUtils(self.settings)
        self.downloaders: List[Tuple['BaseDownloader', int]] = self.settings.provider_context.get_downloaders(self.settings, self.logger, self.general_utils, filter_name=self.name)
        self.last_used_downloader: str = 'none'
        self.archive_callback: Optional[Callable[[Optional['Archive'], Optional[str], str], None]] = None
        self.gallery_callback: Optional[Callable[[Optional['Gallery'], Optional[str], str], None]] = None

    # We need this dispatcher because some provider have multiple ways of getting data (single, multiple),
    # or some have priorities (json fetch, crawl gallery page).
    # Each provider should set in this method how it needs to call everything, and could even check against a setting
    # to decide (cookie is set, page is available, etc).
    # It should at least check for str (URL) and list (list of URLs).
    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        return None

    def fetch_multiple_gallery_data(self, url_list: List[str]) -> Optional[List[GalleryData]]:
        return None

    @classmethod
    def filter_accepted_urls(cls, urls: Iterable[str]) -> List[str]:
        return [x for x in urls if any(word in x for word in cls.accepted_urls)]

    # The idea here is: if it failed and 'retry_failed is not set, don't process
    # If it has at least 1 archive, to force redownload, 'redownload' must be set
    # If it has no archives, to force processing, 'replace_metadata' must be set
    # Skipped galleries are not processed again.
    # We don't log directly here because some methods would spam otherwise (feed crawling)
    def discard_gallery_by_internal_checks(self, gallery_id: str = None, link: str = '', gallery: 'Gallery' = None) -> Tuple[bool, str]:

        if self.settings.update_metadata_mode:
            return False, 'Gallery link {ext_link} running in update metadata mode, processing.'.format(
                ext_link=link,
            )
        if not gallery and (gallery_id and self.settings.gallery_model):
            gallery = self.settings.gallery_model.objects.filter_first(gid=gallery_id)
        if not gallery:
            return False, 'Gallery link {ext_link} has not been added, processing.'.format(
                ext_link=link,
            )

        if gallery.is_submitted():
            message = 'Gallery {title}, {ext_link} marked as submitted: {link}, reprocessing.'.format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return False, message

        if not self.settings.retry_failed and ('failed' in gallery.dl_type):
            message = 'Gallery {title}, {ext_link} failed in previous ' \
                      'run: {link}, skipping (setting: retry_failed).'.format(
                          ext_link=gallery.get_link(),
                          link=gallery.get_absolute_url(),
                          title=gallery.title,
                      )
            return True, message

        if gallery.archive_set.all() and not self.settings.redownload:
            message = 'Gallery {title}, {ext_link} already added, dl_type: {dl_type} ' \
                      'and has at least 1 archive: {link}, skipping (setting: redownload).'.format(
                          ext_link=gallery.get_link(),
                          link=gallery.get_absolute_url(),
                          title=gallery.title,
                          dl_type=gallery.dl_type
                      )
            return True, message

        if not gallery.archive_set.all() and not self.settings.replace_metadata:
            message = 'Gallery {title}, {ext_link} already added: {link}, skipping (setting: replace_metadata).'.format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return True, message

        if 'skipped' in gallery.dl_type:
            message = 'Gallery {title}, {ext_link} marked as skipped: {link}, skipping.'.format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return True, message

        if gallery.is_deleted():
            message = 'Gallery {title}, {ext_link} marked as deleted: {link}, skipping.'.format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return True, message

        message = 'Gallery {title}, {ext_link} already added, but was not discarded: {link}, processing.'.format(
            ext_link=gallery.get_link(),
            link=gallery.get_absolute_url(),
            title=gallery.title,
        )
        return False, message

    # Priorities are: title, tags then file count.
    def compare_gallery_with_wanted_filters(self, gallery: GalleryData, link: str, wanted_filters: QuerySet, gallery_wanted_lists: Dict[str, List['WantedGallery']]) -> None:

        if not self.settings.found_gallery_model:
            self.logger.error("FoundGallery model has not been initiated.")
            return

        for wanted_filter in wanted_filters:
            # Skip wanted_filter that's already found.
            already_found = self.settings.found_gallery_model.objects.filter(
                wanted_gallery__pk=wanted_filter.pk,
                gallery__gid=gallery.gid,
                gallery__provider=self.name
            ).first()
            # Skip already found unless it's a submitted gallery.
            if already_found and not already_found.gallery.is_submitted():
                continue
            # Skip wanted_filter that's not a global filter or is not for this provider.
            if wanted_filter.provider and wanted_filter.provider != self.name:
                continue
            if wanted_filter.wanted_providers:
                wanted_providers = wanted_filter.wanted_providers.split()
                found_wanted_provider = False
                for wanted_provider in wanted_providers:
                    wanted_provider = wanted_provider.strip()
                    if wanted_provider and wanted_provider != self.name:
                        found_wanted_provider = True
                        break
                if not found_wanted_provider:
                    continue
            accepted = True
            if wanted_filter.search_title:
                titles = []
                if gallery.title is not None and gallery.title:
                    titles.append(gallery.title)
                if gallery.title_jpn is not None and gallery.title_jpn:
                    titles.append(gallery.title_jpn)
                accepted = compare_search_title_with_strings(
                    wanted_filter.search_title, titles
                )
            if accepted and wanted_filter.unwanted_title:
                titles = []
                if gallery.title is not None and gallery.title:
                    titles.append(gallery.title)
                if gallery.title_jpn is not None and gallery.title_jpn:
                    titles.append(gallery.title_jpn)
                accepted = not compare_search_title_with_strings(
                    wanted_filter.unwanted_title, titles
                )
            if accepted & bool(wanted_filter.wanted_tags.all()):
                if not set(wanted_filter.wanted_tags_list()).issubset(set(gallery.tags)):
                    accepted = False
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & wanted_filter.wanted_tags_exclusive_scope:
                    accepted_tags = set(wanted_filter.wanted_tags_list()).intersection(set(gallery.tags))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in gallery.tags if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: Dict[str, int] = {}
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if scope_name not in scope_count:
                                scope_count[scope_name] = 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
            if accepted & bool(wanted_filter.unwanted_tags.all()):
                if set(wanted_filter.unwanted_tags_list()).issubset(set(gallery.tags)):
                    accepted = False
            if accepted and wanted_filter.wanted_page_count_lower and gallery.filecount is not None and gallery.filecount:
                accepted = gallery.filecount >= wanted_filter.wanted_page_count_lower
            if accepted and wanted_filter.wanted_page_count_upper and gallery.filecount is not None and gallery.filecount:
                accepted = gallery.filecount <= wanted_filter.wanted_page_count_upper
            if accepted and wanted_filter.category and gallery.category is not None and gallery.category:
                accepted = (wanted_filter.category.lower() == gallery.category.lower())

            if accepted:
                gallery_wanted_lists[gallery.gid].append(wanted_filter)
                wanted_filter.found = True
                wanted_filter.date_found = django_tz.now()
                wanted_filter.save()
        if len(gallery_wanted_lists[gallery.gid]) > 0:
            self.logger.info("Gallery link: {}, title: {}, matched filters: {}.".format(
                link,
                gallery.title,
                ", ".join([x.get_absolute_url() for x in gallery_wanted_lists[gallery.gid]])
            ))

            notify_wanted_filters = [
                "({}, {})".format((x.title or 'not set'), (x.reason or 'not set')) for x in
                gallery_wanted_lists[gallery.gid] if x.notify_when_found
            ]

            if notify_wanted_filters and self.settings.pushover.enable:

                message = "Title: {}, link: {}\nFilters title, reason: {}".format(
                    gallery.title,
                    link,
                    ', '.join(notify_wanted_filters)
                )

                send_pushover_notification(
                    self.settings.pushover.user_key,
                    self.settings.pushover.token,
                    message,
                    device=self.settings.pushover.device,
                    sound=self.settings.pushover.sound,
                    title="Wanted Gallery match found"
                )
            return

    @staticmethod
    def id_from_url(url: str) -> Optional[str]:
        pass

    @classmethod
    def id_from_url_implemented(cls) -> bool:
        if cls.id_from_url is not BaseParser.id_from_url:
            return True
        return False
    #
    # @staticmethod
    # def resolve_url(gallery):
    #     pass

    @staticmethod
    def get_feed_urls() -> List[str]:
        pass

    def crawl_feed(self, feed_url: str = '') -> typing.Union[List[str], List[GalleryData]]:
        pass

    def feed_urls_implemented(self) -> bool:
        if type(self).crawl_feed is not BaseParser.crawl_feed and type(self).get_feed_urls is not BaseParser.get_feed_urls:
            return True
        return False

    def crawl_urls_caller(
            self, urls: List[str],
            wanted_filters: QuerySet = None, wanted_only: bool = False
    ):
        try:
            self.crawl_urls(
                urls,
                wanted_filters=wanted_filters, wanted_only=wanted_only
            )
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())

    def crawl_urls(
            self, urls: List[str],
            wanted_filters: QuerySet = None, wanted_only: bool = False
    ) -> None:
        pass

    def pass_gallery_data_to_downloaders(self, gallery_data_list: List[GalleryData], gallery_wanted_lists):
        gallery_count = len(gallery_data_list)

        if gallery_count == 0:
            self.logger.info("No galleries need downloading, returning.")
            return
        else:
            self.logger.info("{} galleries for downloaders to work with.".format(gallery_count))

        if not self.settings.update_metadata_mode:
            downloaders_msg = 'Downloaders: (name, priority)'

            for downloader in self.downloaders:
                downloaders_msg += " ({}, {})".format(downloader[0], downloader[1])
            self.logger.info(downloaders_msg)

        for i, gallery in enumerate(gallery_data_list, start=1):
            if not self.last_used_downloader.endswith('info') and not self.last_used_downloader.endswith('none'):
                time.sleep(self.settings.wait_timer)
            self.logger.info("Working with gallery {} of {}".format(i, gallery_count))
            if self.settings.add_as_public:
                gallery.public = True
            self.work_gallery_data(gallery, gallery_wanted_lists)

    def work_gallery_data(self, gallery: GalleryData, gallery_wanted_lists) -> None:

        if not self.settings.found_gallery_model:
            self.logger.error("FoundGallery model has not been initiated.")
            return

        if gallery.title is not None:
            self.logger.info("Title: {}. Link: {}".format(gallery.title, gallery.link))
        else:
            self.logger.info("Link: {}".format(gallery.link))

        for cnt, downloader in enumerate(self.downloaders):
            downloader[0].init_download(copy.deepcopy(gallery))
            if downloader[0].return_code == 1:
                self.last_used_downloader = str(downloader[0])
                if not downloader[0].archive_only:
                    for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                        self.settings.found_gallery_model.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery=downloader[0].gallery_db_entry
                        )
                        if wanted_gallery.add_as_hidden and downloader[0].gallery_db_entry:
                            downloader[0].gallery_db_entry.hidden = True
                            downloader[0].gallery_db_entry.save()
                        if downloader[0].archive_db_entry and wanted_gallery.reason:
                            downloader[0].archive_db_entry.reason = wanted_gallery.reason
                            downloader[0].archive_db_entry.simple_save()

                    if len(gallery_wanted_lists[gallery.gid]) > 0:
                        wanted_gallery_found.send(
                            sender=self.settings.gallery_model,
                            gallery=downloader[0].gallery_db_entry,
                            wanted_gallery_list=gallery_wanted_lists[gallery.gid]
                        )
                if downloader[0].archive_db_entry:
                    if not downloader[0].archive_only and downloader[0].gallery_db_entry:
                        self.logger.info("Download complete, using {}. Archive link: {}. Gallery link: {}".format(
                            downloader[0],
                            downloader[0].archive_db_entry.get_absolute_url(),
                            downloader[0].gallery_db_entry.get_absolute_url()
                        ))
                        if self.gallery_callback:
                            self.gallery_callback(downloader[0].gallery_db_entry, gallery.link, 'success')
                        if self.archive_callback:
                            self.archive_callback(downloader[0].archive_db_entry, gallery.link, 'success')
                    else:
                        self.logger.info("Download complete, using {}. Archive link: {}. No gallery associated".format(
                            downloader[0],
                            downloader[0].archive_db_entry.get_absolute_url(),
                        ))
                        if self.archive_callback:
                            self.archive_callback(downloader[0].archive_db_entry, gallery.link, 'success')
                elif downloader[0].gallery_db_entry:
                    self.logger.info(
                        "Download completed successfully (gallery only), using {}. Gallery link: {}".format(
                            downloader[0],
                            downloader[0].gallery_db_entry.get_absolute_url()
                        )
                    )
                    if self.gallery_callback:
                        self.gallery_callback(downloader[0].gallery_db_entry, gallery.link, 'success')
                return
            if(downloader[0].return_code == 0 and (cnt + 1) == len(self.downloaders)):
                self.last_used_downloader = 'none'
                if not downloader[0].archive_only and downloader[0].gallery_db_entry:
                    downloader[0].original_gallery = gallery
                    downloader[0].original_gallery.dl_type = 'failed'
                    downloader[0].update_gallery_db()
                    self.logger.warning("Download completed unsuccessfully, set as failed. Gallery link: {}".format(
                        downloader[0].gallery_db_entry.get_absolute_url()
                    ))
                    if self.gallery_callback:
                        self.gallery_callback(downloader[0].gallery_db_entry, gallery.link, 'failed')
                    for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                        self.settings.found_gallery_model.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery=downloader[0].gallery_db_entry
                        )
                else:
                    self.logger.warning("Download completed unsuccessfully, no entry was updated on the database".format(
                    ))
                    if self.gallery_callback:
                        self.gallery_callback(None, gallery.link, 'failed')


# This assumes we got the data in the format that the API uses ("gc" format).
class InternalParser(BaseParser):
    name = ''
    ignore = True

    def crawl_json(self, json_string: str, wanted_filters: QuerySet = None, wanted_only: bool = False) -> None:

        if not self.settings.gallery_model:
            return

        dict_list = []
        json_decoded = json.loads(json_string)

        if type(json_decoded) == dict:
            dict_list.append(json_decoded)
        elif type(json_decoded) == list:
            dict_list = json_decoded

        galleries_gids = []
        found_galleries = set()
        total_galleries_filtered: List[GalleryData] = []
        gallery_wanted_lists: Dict[str, List['WantedGallery']] = defaultdict(list)

        for gallery in dict_list:
            galleries_gids.append(gallery['gid'])
            gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)
            gallery_data = GalleryData(**gallery)
            total_galleries_filtered.append(gallery_data)

        for galleries_gid_group in list(chunks(galleries_gids, 900)):
            for found_gallery in self.settings.gallery_model.objects.filter(gid__in=galleries_gid_group):
                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery=found_gallery,
                    link=found_gallery.get_link()
                )

                if discard_approved:
                    self.logger.info(discard_message)
                    found_galleries.add(found_gallery.gid)

        for count, gallery in enumerate(total_galleries_filtered):

            if gallery.gid in found_galleries:
                continue

            if self.general_utils.discard_by_tag_list(gallery.tags):
                self.logger.info(
                    "Gallery {} of {}: Skipping gallery {}, because it's tagged with global discarded tags".format(
                        count,
                        len(total_galleries_filtered),
                        gallery.title
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

            self.logger.info(
                "Gallery {} of {}:  Gallery {} will be processed.".format(
                    count,
                    len(total_galleries_filtered),
                    gallery.title
                )
            )

            if gallery.thumbnail:
                original_thumbnail_url = gallery.thumbnail_url

                gallery.thumbnail_url = gallery.thumbnail

                gallery = self.settings.gallery_model.objects.update_or_create_from_values(gallery)

                gallery.thumbnail_url = original_thumbnail_url

                gallery.save()
            else:
                self.settings.gallery_model.objects.update_or_create_from_values(gallery)
