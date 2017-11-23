import json
from datetime import datetime, timezone
import time
from typing import List

import django.utils.timezone as django_tz
import logging

import traceback

from collections import defaultdict

from core.base.setup import Settings
from core.base import utilities
from core.base.utilities import send_pushover_notification, chunks, FakeLogger, \
    compare_search_title_with_strings, OptionalLogger
from viewer.models import Gallery, FoundGallery


class BaseParser:
    name = ''
    ignore = False
    accepted_urls: List[str] = []

    # We need this dispatcher because some provider have multiple ways of getting data (single, multiple),
    # or some have priorities (json fetch, crawl gallery page).
    # Each provider should set in this method how it needs to call everything, and could even check against a setting
    # to decide (cookie is set, page is available, etc).
    # It should at least check for str (URL) and list (list of URLs).
    def fetch_gallery_data(self, url):
        return None

    def fetch_multiple_gallery_data(self, url_list):
        return None

    # TODO: Every time this method is used, it should remove the accepted urls from the original list,
    # to avoid 2 or more parsers from using the same url.
    @classmethod
    def filter_accepted_urls(cls, urls):
        return [x for x in urls if any(word in x for word in cls.accepted_urls)]

    # The idea here is: if it failed and 'retry_failed is not set, don't process
    # If it has at least 1 archive, to force redownload, 'redownload' must be set
    # If it has no archives, to force processing, 'replace_metadata' must be set
    # Skipped galleries are not processed again.
    # We don't log directly here because some methods would spam otherwise (feed crawling)
    def discard_gallery_by_internal_checks(self, gallery_id=None, link='', gallery=None):

        if self.settings.update_metadata_mode:
            return False, 'Gallery link {ext_link} running in update metadata mode, processing.'.format(
                ext_link=link,
            )
        if not gallery and gallery_id:
            gallery = Gallery.objects.filter_first(gid=gallery_id)
        if not gallery:
            return False, 'Gallery link {ext_link} has not been added, processing.'.format(
                ext_link=link,
            )

        # TODO: The setup right now will send 2 notifications if it matches a wanted gallery, on submit and reprocess.
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
    def compare_gallery_with_wanted_filters(self, gallery, link, wanted_filters, gallery_wanted_lists):
        for wanted_filter in wanted_filters:
            # Skip wanted_filter that's already found.
            already_found = FoundGallery.objects.filter(
                wanted_gallery__pk=wanted_filter.pk,
                gallery__gid=gallery['gid'],
                gallery__provider=self.name
            ).first()
            if already_found:
                continue
            # Skip wanted_filter that's not a global filter or is not for this provider.
            if wanted_filter.provider and wanted_filter.provider != self.name:
                continue
            accepted = True
            if wanted_filter.search_title:
                titles = []
                if 'title' in gallery and gallery['title']:
                    titles.append(gallery['title'])
                if 'title_jpn' in gallery and gallery['title_jpn']:
                    titles.append(gallery['title_jpn'])
                accepted = compare_search_title_with_strings(
                    wanted_filter.search_title, titles
                )
            if accepted and wanted_filter.unwanted_title:
                titles = []
                if 'title' in gallery and gallery['title']:
                    titles.append(gallery['title'])
                if 'title_jpn' in gallery and gallery['title_jpn']:
                    titles.append(gallery['title_jpn'])
                accepted = not compare_search_title_with_strings(
                    wanted_filter.unwanted_title, titles
                )
            if accepted & bool(wanted_filter.wanted_tags.all()):
                if not set(wanted_filter.wanted_tags_list()).issubset(set(gallery['tags'])):
                    accepted = False
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & wanted_filter.wanted_tags_exclusive_scope:
                    accepted_tags = set(wanted_filter.wanted_tags_list()).intersection(set(gallery['tags']))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in gallery['tags'] if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count = {}
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
                if set(wanted_filter.unwanted_tags_list()).issubset(set(gallery['tags'])):
                    accepted = False
            if accepted and wanted_filter.wanted_page_count_lower and 'filecount' in gallery and gallery['filecount']:
                accepted = gallery['filecount'] >= wanted_filter.wanted_page_count_lower
            if accepted and wanted_filter.wanted_page_count_upper and 'filecount' in gallery and gallery['filecount']:
                accepted = gallery['filecount'] <= wanted_filter.wanted_page_count_upper
            if accepted and wanted_filter.category and 'category' in gallery and gallery['category']:
                accepted = (wanted_filter.category.lower() == gallery['category'].lower())

            if accepted:
                gallery_wanted_lists[gallery['gid']].append(wanted_filter)
                wanted_filter.found = True
                wanted_filter.date_found = django_tz.now()
                wanted_filter.save()
        if len(gallery_wanted_lists[gallery['gid']]) > 0:
            self.logger.info("Gallery link: {}, title: {}, matched filters: {}.".format(
                link,
                gallery['title'],
                ", ".join([x.get_absolute_url() for x in gallery_wanted_lists[gallery['gid']]])
            ))

            notify_wanted_filters = [x.title or 'not set' for x in gallery_wanted_lists[gallery['gid']] if
                                     x.notify_when_found]

            if notify_wanted_filters and self.settings.pushover.enable:

                message = "Title: {}, link: {}, filters titles: {}".format(
                    gallery['title'],
                    link,
                    ', '.join(notify_wanted_filters)
                )

                send_pushover_notification(
                    self.settings.pushover.user_key,
                    self.settings.pushover.token,
                    message,
                    title="Wanted Gallery match found"
                )
            return

    # @staticmethod
    # def id_from_url(url):
    #     pass
    #
    # @staticmethod
    # def resolve_url(gallery):
    #     pass

    def crawl_urls_caller(self, urls, wanted_filters=None, wanted_only=False):
        try:
            self.crawl_urls(urls, wanted_filters=wanted_filters, wanted_only=wanted_only)
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())

    def crawl_urls(self, urls, wanted_filters=None, wanted_only=False):
        pass

    def pass_gallery_data_to_downloaders(self, gallery_data_list, gallery_wanted_lists):
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
                gallery['public'] = True
            self.work_gallery_data(gallery, gallery_wanted_lists)

    def work_gallery_data(self, gallery, gallery_wanted_lists):

        if 'title' in gallery:
            self.logger.info("Title: {}. Link: {}".format(gallery['title'], gallery['link']))
        else:
            self.logger.info("Link: {}".format(gallery['link']))

        for cnt, downloader in enumerate(self.downloaders):
            downloader[0].init_download(gallery.copy())
            if downloader[0].return_code == 1:
                self.last_used_downloader = str(downloader[0])
                if not downloader[0].archive_only:
                    for wanted_gallery in gallery_wanted_lists[gallery['gid']]:
                        FoundGallery.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery=downloader[0].gallery_db_entry
                        )
                        if wanted_gallery.add_as_hidden:
                            downloader[0].gallery_db_entry.hidden = True
                            downloader[0].gallery_db_entry.save()
                        if downloader[0].archive_db_entry and wanted_gallery.reason:
                            downloader[0].archive_db_entry.reason = wanted_gallery.reason
                            downloader[0].archive_db_entry.simple_save()
                if downloader[0].archive_db_entry:
                    if not downloader[0].archive_only:
                        self.logger.info("Download complete, using {}. Archive link: {}. Gallery link: {}".format(
                            downloader[0],
                            downloader[0].archive_db_entry.get_absolute_url(),
                            downloader[0].gallery_db_entry.get_absolute_url()
                        ))
                    else:
                        self.logger.info("Download complete, using {}. Archive link: {}. No gallery associated".format(
                            downloader[0],
                            downloader[0].archive_db_entry.get_absolute_url(),
                        ))
                else:
                    self.logger.info(
                        "Download completed successfully (gallery only), using {}. Gallery link: {}".format(
                            downloader[0],
                            downloader[0].gallery_db_entry.get_absolute_url()
                        )
                    )
                return
            if(downloader[0].return_code == 0 and
                    (cnt + 1) == len(self.downloaders)):
                self.last_used_downloader = 'none'
                if not downloader[0].archive_only:
                    downloader[0].original_gallery = gallery
                    downloader[0].original_gallery['dl_type'] = 'failed'
                    downloader[0].update_gallery_db()
                    self.logger.warning("Download completed unsuccessfully, set as failed. Gallery link: {}".format(
                        downloader[0].gallery_db_entry.get_absolute_url()
                    ))
                    for wanted_gallery in gallery_wanted_lists[gallery['gid']]:
                        FoundGallery.objects.get_or_create(
                            wanted_gallery=wanted_gallery,
                            gallery=downloader[0].gallery_db_entry
                        )
                else:
                    self.logger.warning("Download completed unsuccessfully, no entry was updated on the database".format(
                    ))

    def __init__(self, settings: Settings, logger: OptionalLogger=None) -> None:
        self.settings = settings
        if not logger:
            self.logger: OptionalLogger = FakeLogger()
        else:
            self.logger = logger
        if self.name in settings.providers:
            self.own_settings = settings.providers[self.name]
        else:
            self.own_settings = None
        self.general_utils = utilities.GeneralUtils(self.settings)
        self.downloaders = self.settings.provider_context.get_downloaders(self.settings, self.logger, self.general_utils, filter_name=self.name)
        self.last_used_downloader = 'none'


# This assumes we got the data in the format that the API uses ("gc" format).
class InternalParser(BaseParser):
    name = ''
    ignore = True

    def crawl_json(self, json_string, wanted_filters=None, wanted_only=False):
        dict_list = []
        json_decoded = json.loads(json_string)

        if type(json_decoded) == dict:
            dict_list.append(json_decoded)
        elif type(json_decoded) == list:
            dict_list = json_decoded

        galleries_gids = []
        found_galleries = set()
        total_galleries_filtered = []
        gallery_wanted_lists = defaultdict(list)

        for gallery in dict_list:
            galleries_gids.append(gallery['gid'])
            total_galleries_filtered.append(gallery)

        for galleries_gid_group in list(chunks(galleries_gids, 900)):
            for found_gallery in Gallery.objects.filter(gid__in=galleries_gid_group):
                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery=found_gallery,
                    link=found_gallery.get_link()
                )

                if discard_approved:
                    self.logger.info(discard_message)
                    found_galleries.add(found_gallery.gid)

        for gallery in total_galleries_filtered:

            if gallery['gid'] in found_galleries:
                continue

            if self.general_utils.discard_by_tag_list(gallery['tags']):
                self.logger.info(
                    "Skipping gallery {}, because it's tagged with global discarded tags".format(gallery['title'])
                )
                continue

            if wanted_filters:
                self.compare_gallery_with_wanted_filters(
                    gallery,
                    gallery['link'],
                    wanted_filters,
                    gallery_wanted_lists
                )
                if wanted_only and not gallery_wanted_lists[gallery['gid']]:
                    continue

            self.logger.info("Gallery {} will be processed.".format(gallery['title']))
            gallery['posted'] = datetime.fromtimestamp(int(gallery['posted']), timezone.utc)

            if gallery['thumbnail']:
                original_thumbnail_url = gallery['thumbnail_url']

                gallery['thumbnail_url'] = gallery['thumbnail']

                gallery = Gallery.objects.update_or_create_from_values(gallery)

                gallery.thumbnail_url = original_thumbnail_url

                gallery.save()
            else:
                Gallery.objects.update_or_create_from_values(gallery)
