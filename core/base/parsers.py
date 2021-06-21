import copy
import json
import typing
from datetime import datetime, timezone, timedelta
import time
from typing import Optional

import django.utils.timezone as django_tz
import logging

import traceback

from collections import defaultdict
from collections.abc import Callable, Iterable

from django.db.models import QuerySet, Q, Value, CharField, F
from django.db.models.functions import Concat, Replace

from core.base import utilities
from core.base.utilities import send_pushover_notification, chunks
from core.base.types import GalleryData
from viewer.signals import wanted_gallery_found

if typing.TYPE_CHECKING:
    from core.downloaders.handlers import BaseDownloader
    from core.base.setup import Settings
    from viewer.models import Gallery, WantedGallery, Archive

logger = logging.getLogger(__name__)


def aggregate_wanted_time_taken(func):
    def wrapper_save_time_taken(*args, **kwargs):
        start = time.perf_counter()
        func(*args, **kwargs)
        end = time.perf_counter()
        args[0].time_taken_wanted += end - start

    return wrapper_save_time_taken


class BaseParser:
    name = ''
    ignore = False
    accepted_urls: list[str] = []

    def __init__(self, settings: 'Settings') -> None:
        self.settings = settings
        if self.name in settings.providers:
            self.own_settings = settings.providers[self.name]
        else:
            self.own_settings = None
        self.general_utils = utilities.GeneralUtils(self.settings)
        self.downloaders: list[tuple['BaseDownloader', int]] = self.settings.provider_context.get_downloaders(self.settings, self.general_utils, filter_name=self.name)
        self.last_used_downloader: str = 'none'
        self.time_taken_wanted: float = 0
        self.archive_callback: Optional[Callable[[Optional['Archive'], Optional[str], str], None]] = None
        self.gallery_callback: Optional[Callable[[Optional['Gallery'], Optional[str], str], None]] = None

    # We need this dispatcher because some provider have multiple ways of getting data (single, multiple),
    # or some have priorities (json fetch, crawl gallery page).
    # Each provider should set in this method how it needs to call everything, and could even check against a setting
    # to decide (cookie is set, page is available, etc).
    # It should at least check for str (URL) and list (list of URLs).
    def fetch_gallery_data(self, url: str) -> Optional[GalleryData]:
        return None

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> Optional[list[GalleryData]]:
        return None

    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if any(word in x for word in self.accepted_urls)]

    # The idea here is: if it failed and 'retry_failed is not set, don't process
    # If it has at least 1 archive, to force redownload, 'redownload' must be set
    # If it has no archives, to force processing, 'replace_metadata' must be set
    # Skipped galleries are not processed again.
    # We don't log directly here because some methods would spam otherwise (feed crawling)
    def discard_gallery_by_internal_checks(self, gallery_id: str = None, link: str = '', gallery: 'Gallery' = None) -> tuple[bool, str]:

        if self.settings.update_metadata_mode:
            return False, 'Gallery link {ext_link} running in update metadata mode, processing.'.format(
                ext_link=link,
            )
        if not gallery and (gallery_id and self.settings.gallery_model):
            gallery = self.settings.gallery_model.objects.filter_first(gid=gallery_id, provider=self.name)
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
    @aggregate_wanted_time_taken
    def compare_gallery_with_wanted_filters(self, gallery: GalleryData, link: str, wanted_filters: QuerySet, gallery_wanted_lists: dict[str, list['WantedGallery']]) -> None:

        if not self.settings.found_gallery_model:
            logger.error("FoundGallery model has not been initiated.")
            return

        if gallery.title or gallery.title_jpn:
            q_objects = Q()
            q_objects_unwanted = Q()
            q_objects_regexp = Q()
            q_objects_unwanted_regexp = Q()
            if gallery.title:
                wanted_filters = wanted_filters.annotate(g_title=Value(gallery.title, output_field=CharField()))

                q_objects.add(Q(g_title__ss=Concat(Value('%'), Replace(F('search_title'), Value(' '), Value('%')), Value('%'))), Q.OR)
                q_objects_unwanted.add(~Q(g_title__ss=Concat(Value('%'), Replace(F('unwanted_title'), Value(' '), Value('%')), Value('%'))), Q.AND)

                q_objects_regexp.add(Q(g_title__regex=F('search_title')), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title__regex=F('unwanted_title')), Q.AND)

            if gallery.title_jpn:
                wanted_filters = wanted_filters.annotate(g_title_jpn=Value(gallery.title_jpn, output_field=CharField()))
                q_objects.add(Q(g_title_jpn__ss=Concat(Value('%'), Replace(F('search_title'), Value(' '), Value('%')), Value('%'))), Q.OR)
                q_objects_unwanted.add(~Q(g_title_jpn__ss=Concat(Value('%'), Replace(F('unwanted_title'), Value(' '), Value('%')), Value('%'))), Q.AND)

                q_objects_regexp.add(Q(g_title_jpn__regex=F('search_title')), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title_jpn__regex=F('unwanted_title')), Q.AND)

            filtered_wanted = wanted_filters.filter(
                Q(search_title__isnull=True)
                | Q(search_title='')
                | Q(Q(regexp_search_title=False), q_objects)
                | Q(Q(regexp_search_title=True), q_objects_regexp)
            ).filter(
                Q(unwanted_title__isnull=True)
                | Q(unwanted_title='')
                | Q(Q(regexp_unwanted_title=False), q_objects_unwanted)
                | Q(Q(regexp_unwanted_title=True), q_objects_unwanted_regexp)
            )

        else:
            filtered_wanted = wanted_filters.filter(
                Q(search_title__isnull=True) | Q(search_title='')
            ).filter(
                Q(unwanted_title__isnull=True) | Q(unwanted_title='')
            )

        if gallery.posted:
            filtered_wanted = filtered_wanted.filter(
                Q(wait_for_time__isnull=True) | Q(wait_for_time__lte=django_tz.now() - gallery.posted)
            )

        for wanted_filter in filtered_wanted:
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
            if wanted_filter.wanted_providers.count():
                if not wanted_filter.wanted_providers.filter(slug=self.name).first():
                    continue
            if wanted_filter.unwanted_providers.count():
                if wanted_filter.unwanted_providers.filter(slug=self.name).first():
                    continue
            accepted = True
            if bool(wanted_filter.wanted_tags.all()):
                if not set(wanted_filter.wanted_tags_list()).issubset(set(gallery.tags)):
                    accepted = False
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & wanted_filter.wanted_tags_exclusive_scope:
                    accepted_tags = set(wanted_filter.wanted_tags_list()).intersection(set(gallery.tags))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in gallery.tags if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: dict[str, int] = defaultdict(int)
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if wanted_filter.exclusive_scope_name:
                                if wanted_filter.exclusive_scope_name == scope_name:
                                    scope_count[scope_name] += 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
                # Review based on 'accept if none' scope.
                if not accepted and wanted_filter.wanted_tags_accept_if_none_scope:
                    missing_tags = set(wanted_filter.wanted_tags_list()).difference(set(gallery.tags))
                    # If all the missing tags start with the parameter,
                    # and no other tag is in gallery with this parameter, mark as accepted
                    scope_formatted = wanted_filter.wanted_tags_accept_if_none_scope + ":"
                    if all(x.startswith(scope_formatted) for x in missing_tags)\
                            and not any(x.startswith(scope_formatted) for x in gallery.tags):
                        accepted = True
            if accepted & bool(wanted_filter.unwanted_tags.all()):
                if any(item in gallery.tags for item in wanted_filter.unwanted_tags_list()):
                    accepted = False
            if accepted and wanted_filter.wanted_page_count_lower and gallery.filecount is not None and gallery.filecount:
                accepted = int(gallery.filecount) >= wanted_filter.wanted_page_count_lower
            if accepted and wanted_filter.wanted_page_count_upper and gallery.filecount is not None and gallery.filecount:
                accepted = int(gallery.filecount) <= wanted_filter.wanted_page_count_upper
            if accepted and wanted_filter.category and gallery.category is not None and gallery.category:
                accepted = (wanted_filter.category.lower() == gallery.category.lower())

            if accepted:
                gallery_wanted_lists[gallery.gid].append(wanted_filter)
                wanted_filter.found = True
                wanted_filter.date_found = django_tz.now()
                wanted_filter.save()
        if len(gallery_wanted_lists[gallery.gid]) > 0:
            logger.info("Gallery link: {}, title: {}, matched filters: {}.".format(
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

    def get_feed_urls(self) -> list[str]:
        pass

    def crawl_feed(self, feed_url: str = '') -> typing.Union[list[typing.Any]]:
        pass

    def feed_urls_implemented(self) -> bool:
        if type(self).crawl_feed is not BaseParser.crawl_feed and type(self).get_feed_urls is not BaseParser.get_feed_urls:
            return True
        return False

    def crawl_urls_caller(
            self, urls: list[str],
            wanted_filters: QuerySet = None, wanted_only: bool = False
    ):
        try:
            self.crawl_urls(
                urls,
                wanted_filters=wanted_filters, wanted_only=wanted_only
            )
        except BaseException:
            logger.critical(traceback.format_exc())

    def crawl_urls(
            self, urls: list[str],
            wanted_filters: QuerySet = None, wanted_only: bool = False
    ) -> None:
        pass

    def pass_gallery_data_to_downloaders(self, gallery_data_list: list[GalleryData], gallery_wanted_lists: dict[str, list['WantedGallery']]):
        gallery_count = len(gallery_data_list)

        if self.time_taken_wanted:
            logger.info("Time taken to compare with WantedGallery: {} seconds.".format(int(self.time_taken_wanted + 0.5)))

        if gallery_count == 0:
            logger.info("No galleries need downloading, returning.")
            return
        else:
            logger.info("{} galleries for downloaders to work with.".format(gallery_count))

        if not self.settings.update_metadata_mode:
            downloaders_msg = 'Downloaders: (name, priority)'

            for downloader in self.downloaders:
                downloaders_msg += " ({}, {})".format(downloader[0], downloader[1])
            logger.info(downloaders_msg)

        for i, gallery in enumerate(gallery_data_list, start=1):
            if not self.last_used_downloader.endswith('info') and not self.last_used_downloader.endswith('none'):
                if self.own_settings:
                    time.sleep(self.own_settings.wait_timer)
                else:
                    time.sleep(self.settings.wait_timer)
            logger.info("Working with gallery {} of {}".format(i, gallery_count))
            if self.settings.add_as_public:
                gallery.public = True
            self.work_gallery_data(gallery, gallery_wanted_lists)

    def work_gallery_data(self, gallery: GalleryData, gallery_wanted_lists: dict[str, list['WantedGallery']]) -> None:

        if not self.settings.found_gallery_model:
            logger.error("FoundGallery model has not been initiated.")
            return

        if gallery.title is not None:
            logger.info("Title: {}. Link: {}".format(gallery.title, gallery.link))
        else:
            logger.info("Link: {}".format(gallery.link))

        for cnt, downloader in enumerate(self.downloaders):
            downloader[0].init_download(copy.deepcopy(gallery))

            if downloader[0].return_code == 1:

                if (cnt + 1) == len(self.downloaders) and downloader[0].mark_hidden_if_last:
                    if downloader[0].gallery_db_entry:
                        downloader[0].gallery_db_entry.hidden = True
                        downloader[0].gallery_db_entry.simple_save()

                self.last_used_downloader = str(downloader[0])
                if not downloader[0].archive_only:
                    for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                        self.settings.found_gallery_model.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery=downloader[0].gallery_db_entry
                        )
                        if wanted_gallery.add_as_hidden and downloader[0].gallery_db_entry:
                            downloader[0].gallery_db_entry.hidden = True
                            downloader[0].gallery_db_entry.simple_save()
                        if downloader[0].archive_db_entry and wanted_gallery.reason:
                            downloader[0].archive_db_entry.reason = wanted_gallery.reason
                            downloader[0].archive_db_entry.simple_save()

                    if len(gallery_wanted_lists[gallery.gid]) > 0:
                        wanted_gallery_found.send(
                            sender=self.settings.gallery_model,
                            gallery=downloader[0].gallery_db_entry,
                            archive=downloader[0].archive_db_entry,
                            wanted_gallery_list=gallery_wanted_lists[gallery.gid]
                        )
                if downloader[0].archive_db_entry:
                    if not downloader[0].archive_only and downloader[0].gallery_db_entry:
                        logger.info(
                            "Download complete, using downloader: {}. Archive link: {}. Gallery link: {}".format(
                                downloader[0],
                                downloader[0].archive_db_entry.get_absolute_url(),
                                downloader[0].gallery_db_entry.get_absolute_url()
                            )
                        )
                        if self.gallery_callback:
                            self.gallery_callback(downloader[0].gallery_db_entry, gallery.link, 'success')
                        if self.archive_callback:
                            self.archive_callback(downloader[0].archive_db_entry, gallery.link, 'success')
                    else:
                        logger.info(
                            "Download complete, using downloader: {}. Archive link: {}. No gallery associated".format(
                                downloader[0],
                                downloader[0].archive_db_entry.get_absolute_url(),
                            )
                        )
                        if self.archive_callback:
                            self.archive_callback(downloader[0].archive_db_entry, gallery.link, 'success')
                elif downloader[0].gallery_db_entry:
                    logger.info(
                        "Download completed successfully (gallery only), using downloader: {}. Gallery link: {}".format(
                            downloader[0],
                            downloader[0].gallery_db_entry.get_absolute_url()
                        )
                    )
                    if self.gallery_callback:
                        self.gallery_callback(downloader[0].gallery_db_entry, gallery.link, 'success')

                    # Process possible nested galleries (contained, magazine)
                    # To avoid downloading extra Archives, it will only be possible to autoadd Gallery only downloads
                    # Second, to avoid keeping check of already processed galleries, considering they could be
                    # downloaded from different queues, it will only work with 1 level deep, so that no infinite
                    # nesting happens
                    # Note that we have a filter here to not add galleries that already exist.
                    # If the gallery already exists, the relationship will be set backwards, without
                    # needing to process the Gallery directly
                    if not self.settings.stop_nested and self.settings.auto_download_nested and self.settings.workers.web_queue and self.settings.gallery_model:
                        if gallery.gallery_contains_gids:
                            existing_gids = self.settings.gallery_model.objects.filter(
                                gid__in=gallery.gallery_contains_gids,
                                provider=gallery.provider
                            ).values_list('gid', flat=True)

                            gallery_urls = [
                                self.settings.gallery_model(gid=x, provider=gallery.provider).get_link() for x in gallery.gallery_contains_gids if x not in existing_gids
                            ]
                            gallery_urls.append("--stop-nested")

                            self.settings.workers.web_queue.enqueue_args_list(gallery_urls)

                        if gallery.magazine_chapters_gids:
                            existing_gids = self.settings.gallery_model.objects.filter(
                                gid__in=gallery.magazine_chapters_gids,
                                provider=gallery.provider
                            ).values_list('gid', flat=True)

                            gallery_urls = [
                                self.settings.gallery_model(gid=x, provider=gallery.provider).get_link() for x in gallery.magazine_chapters_gids if x not in existing_gids
                            ]
                            gallery_urls.append("--stop-nested")

                            self.settings.workers.web_queue.enqueue_args_list(gallery_urls)

                        if gallery.magazine_gid and not self.settings.gallery_model.objects.filter(gid=gallery.magazine_gid, provider=gallery.provider):
                            gallery_url = self.settings.gallery_model(gid=gallery.magazine_gid, provider=gallery.provider).get_link()
                            self.settings.workers.web_queue.enqueue_args_list([gallery_url, "--stop-nested"])

                        if gallery.gallery_container_gid and not self.settings.gallery_model.objects.filter(gid=gallery.gallery_container_gid, provider=gallery.provider):
                            gallery_url = self.settings.gallery_model(gid=gallery.gallery_container_gid, provider=gallery.provider).get_link()
                            self.settings.workers.web_queue.enqueue_args_list([gallery_url, "--stop-nested"])

                return
            elif downloader[0].return_code == 0 and (cnt + 1) == len(self.downloaders):
                self.last_used_downloader = 'none'
                if not downloader[0].archive_only:
                    downloader[0].original_gallery = gallery
                    downloader[0].original_gallery.dl_type = 'failed'
                    downloader[0].original_gallery.hidden = True
                    downloader[0].update_gallery_db()
                    if downloader[0].gallery_db_entry:
                        logger.warning(
                            "Download completed unsuccessfully using downloader: {},"
                            " set as failed as it\'s the last one. Gallery link: {}".format(
                                downloader[0],
                                downloader[0].gallery_db_entry.get_absolute_url()
                            )
                        )
                        if self.gallery_callback:
                            self.gallery_callback(downloader[0].gallery_db_entry, gallery.link, 'failed')
                        for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                            self.settings.found_gallery_model.objects.get_or_create(
                                wanted_gallery=wanted_gallery,
                                gallery=downloader[0].gallery_db_entry
                            )
                    else:
                        logger.warning(
                            "Download completed unsuccessfully using downloader: {},"
                            " could not set as failed, no entry was updated on the database".format(
                                downloader[0]
                            )
                        )
                else:
                    logger.warning(
                        "Download completed unsuccessfully using downloader: {},"
                        " no entry was updated on the database".format(downloader[0])
                    )
                    if self.gallery_callback:
                        self.gallery_callback(None, gallery.link, 'failed')
            else:
                logger.info(
                    "Download was unsuccessful, using downloader {}. Trying with the next downloader.".format(
                        downloader[0],
                    )
                )


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
        total_galleries_filtered: list[GalleryData] = []
        gallery_wanted_lists: dict[str, list['WantedGallery']] = defaultdict(list)

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
                    logger.info(discard_message)
                    found_galleries.add(found_gallery.gid)

        for count, gallery in enumerate(total_galleries_filtered):

            if gallery.gid in found_galleries:
                continue

            discarded_tags = self.general_utils.discard_by_tag_list(gallery.tags)

            if discarded_tags:
                logger.info(
                    "Gallery {} of {}: Skipping gallery link {}, because it's tagged with global discarded tags: {}".format(
                        count,
                        len(total_galleries_filtered),
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
                "Gallery {} of {}:  Gallery {} will be processed.".format(
                    count,
                    len(total_galleries_filtered),
                    gallery.title
                )
            )

            if gallery.thumbnail:
                original_thumbnail_url = gallery.thumbnail_url

                gallery.thumbnail_url = gallery.thumbnail

                gallery_instance = self.settings.gallery_model.objects.update_or_create_from_values(gallery)

                gallery_instance.thumbnail_url = original_thumbnail_url

                gallery_instance.save()
            else:
                self.settings.gallery_model.objects.update_or_create_from_values(gallery)
