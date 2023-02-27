import argparse
import threading
import logging

import os
import typing
from collections import defaultdict
from collections.abc import Callable
from typing import Union, Optional, NoReturn

import requests
from django.db.models import QuerySet, Q

from core.downloaders.postdownload import PostDownloader
from core.base.parsers import InternalParser
from viewer.models import Gallery, WantedGallery, Archive

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


class ArgumentParserError(Exception):
    pass


class YieldingArgumentParser(argparse.ArgumentParser):

    def error(self, message: str) -> NoReturn:
        raise ArgumentParserError(message)


class WebCrawler(object):

    def __init__(self, settings: 'Settings') -> None:
        self.settings = settings
        self.parse_error = False

    def get_args(self, arg_line: list[str]) -> Union[argparse.Namespace, ArgumentParserError]:
        parser = YieldingArgumentParser(prog='PandaBackupLinks')

        parser.add_argument('url',
                            nargs='*',
                            default=[],
                            help='URLs to crawl. check the providers folder to see supported links')

        parser.add_argument('-f', '--file',
                            required=False,
                            action='store',
                            help='Read links to crawl from a text file')

        parser.add_argument('-rf', '--retry-failed',
                            required=False,
                            action='store',
                            help="Retry download of galleries marked as failed. Can filter by provider")

        parser.add_argument('-rwf', '--retry-wrong-filesize',
                            required=False,
                            action='store_true',
                            help='Retry download of galleries that differ '
                                 'in filesize, gallery versus archive (unpacked)')

        parser.add_argument('-rd', '--retry-dl-type',
                            required=False,
                            action='store',
                            help='Retry download of galleries by a certain dl_type')

        parser.add_argument('-unt', '--update-newer-than',
                            required=False,
                            action='store',
                            help='Update metadata for galleries newer than a created date')

        parser.add_argument('-umt', '--update-missing-thumbnails',
                            type=int,
                            required=False,
                            action='store',
                            help='Update metadata for galleries with missing thumbnails, limiting by older ones')

        parser.add_argument('-tmd', '--transfer-missing-downloads',
                            required=False,
                            action='store_true',
                            help='Transfer completed downloads')

        parser.add_argument('-json', '--json-source',
                            required=False,
                            action='store',
                            help='Obtain gallery metadata from a JSON file or URL (own API "gc" type format)')

        parser.add_argument('-feed', '--crawl-from-feed',
                            required=False,
                            action='store_true',
                            help='Tell each provider to crawl their feed source for new galleries')

        parser.add_argument('-reason', '--set-reason',
                            required=False,
                            action='store',
                            help='Most parameters are set from the gallery, but reason is user-defined.'
                                 'Can be set afterwards, but this option sets it when crawling')

        parser.add_argument('-details', '--set-details',
                            required=False,
                            action='store',
                            help='Most parameters are set from the gallery, but details is user-defined.'
                                 'Can be set afterwards, but this option sets it when crawling')

        parser.add_argument('-wanted', '--wanted-only',
                            required=False,
                            action='store_true',
                            help='A mode to accept only links that match any of the wanted gallery filters.'
                                 'This is set automatically when running the autosearch.')

        parser.add_argument('-nwc', '--no-wanted-check',
                            required=False,
                            action='store_true',
                            help='A mode to crawl galleries without checking against wanted galleries.')

        parser.add_argument('-frm', '--force-replace-metadata',
                            required=False,
                            action='store_true',
                            help='A mode to force processing galleries with no archives associated.'
                                 'Without this active, galleries already in the system won\'t download archives.')

        parser.add_argument('-ff', '--force-failed',
                            required=False,
                            action='store_true',
                            help='A mode to force processing galleries with retry failed as status.')

        parser.add_argument('-um', '--update-mode',
                            required=False,
                            action='store_true',
                            help='Mode set when updating existing galleries, so that filters are not applied.')

        parser.add_argument('-ip', '--include-providers',
                            required=False,
                            action='store',
                            nargs='*',
                            help='List of provider names to include (exclusively) from some of the tools.')

        parser.add_argument('-ep', '--exclude-providers',
                            required=False,
                            action='store',
                            nargs='*',
                            help='List of provider names to exclude from some of the tools.')

        parser.add_argument('-wt', '--wait-timer',
                            type=int,
                            required=False,
                            action='store',
                            help='Override the wait timer.')

        parser.add_argument('-sn', '--stop-nested',
                            required=False,
                            action='store_true',
                            help='Flag to indicate the parsers that in case of nested links, don\'t process them.')

        parser.add_argument('-lp', '--link-child',
                            required=False,
                            action='store',
                            help='Link backwards a child gallery with its parent')

        parser.add_argument('-ln', '--link-newer',
                            required=False,
                            action='store',
                            help='Link backwards a newer gallery with its first')

        parser.add_argument('-pwm', '--preselect-wanted-match',
                            required=False,
                            action='store',
                            nargs='*',
                            help='List of url, WantedGallery ids pre-matched to be stored on the database.')

        try:
            args = parser.parse_args(arg_line)
            self.parse_error = False
        except ArgumentParserError as e:
            self.parse_error = True
            return e

        if args.file:
            f = open(args.file, 'r')
            urls = f.readlines()
            args.url.extend([x.rstrip('\n') for x in urls])

        return args

    def start_crawling(
            self,
            arg_line: list[str],
            override_options: 'Optional[Settings]' = None,
            archive_callback: 'Optional[Callable[[Optional[Archive], Optional[str], str], None]]' = None,
            gallery_callback: 'Optional[Callable[[Optional[Gallery], Optional[str], str], None]]' = None,
            use_argparser: bool = True,
    ):

        if use_argparser:
            self.start_crawling_parse_args(
                arg_line, override_options=override_options,
                archive_callback=archive_callback, gallery_callback=gallery_callback
            )
        else:
            self.start_crawling_no_argparser(
                arg_line, override_options=override_options,
                archive_callback=archive_callback, gallery_callback=gallery_callback
            )

    def crawl_json_source(self, args, current_settings, wanted_filters):
        if os.path.isfile(args.json_source):
            with open(args.json_source, 'r', encoding="utf8") as f:
                json_string = f.read()
                logger.info('Crawling a Panda Backup JSON string from a file.')
        else:
            response = requests.get(
                args.json_source,
                timeout=current_settings.timeout_timer
            )
            json_string = response.text
            logger.info('Crawling a Panda Backup JSON string from a URL.')
        crawler = InternalParser(current_settings)
        crawler.crawl_json(json_string, wanted_filters=wanted_filters, wanted_only=args.wanted_only)

    def start_crawling_parse_args(
            self,
            arg_line: list[str],
            override_options: 'Optional[Settings]' = None,
            archive_callback: 'Optional[Callable[[Optional[Archive], Optional[str], str], None]]' = None,
            gallery_callback: 'Optional[Callable[[Optional[Gallery], Optional[str], str], None]]' = None,
    ):

        args = self.get_args(arg_line)

        if isinstance(args, ArgumentParserError):
            logger.info(str(args))
            return

        if override_options:
            current_settings = override_options
        else:
            current_settings = self.settings

        if args.wait_timer:
            current_settings.wait_timer = args.wait_timer

        if args.retry_failed:
            current_settings.retry_failed = True
            current_settings.replace_metadata = True
            found_galleries = Gallery.objects.filter_dl_type('failed', provider=args.retry_failed)
            if found_galleries:
                for gallery in found_galleries:
                    args.url.append(gallery.get_link())

        if args.retry_dl_type:
            current_settings.retry_failed = True
            current_settings.replace_metadata = True
            found_galleries = Gallery.objects.filter_dl_type(args.retry_dl_type)
            if found_galleries:
                for gallery in found_galleries:
                    args.url.append(gallery.get_link())

        if args.retry_wrong_filesize:
            current_settings.retry_failed = True
            current_settings.replace_metadata = True
            all_galleries = Gallery.objects.different_filesize_archive()
            if all_galleries:
                for gallery in all_galleries:
                    args.url.append(gallery.get_link())

        if args.update_newer_than:

            self.update_galleries_newer_than(args, current_settings)

        if args.update_missing_thumbnails is not None:

            self.update_galleries_missing_thumbnails(args, current_settings)

        if args.transfer_missing_downloads:
            post_downloader = PostDownloader(current_settings)
            post_downloader.transfer_all_missing()

        if args.set_reason:
            current_settings.archive_reason = args.set_reason
            current_settings.gallery_reason = args.set_reason

        if args.set_details:
            current_settings.archive_details = args.set_details

        if args.stop_nested:
            current_settings.stop_nested = args.stop_nested

        if args.link_child:
            current_settings.link_child = args.link_child
        else:
            current_settings.link_child = None

        if args.link_newer:
            current_settings.link_newer = args.link_newer
        else:
            current_settings.link_newer = None

        preselected_wanted_matches: Optional[dict[str, list['WantedGallery']]] = None

        if args.preselect_wanted_match:
            logger.info('Note: Preselected WantedGallery matches were provided.')
            preselected_wanted_matches = defaultdict(list)
            for url_wanted_gallery_id_pair in args.preselect_wanted_match:
                preselected_gid, preselected_wg_id = url_wanted_gallery_id_pair.rsplit(",", maxsplit=1)
                wg = WantedGallery.objects.filter(pk=int(preselected_wg_id)).first()
                if wg:
                    preselected_wanted_matches[preselected_gid].append(wg)

        provider_filter_list: list[str] = []
        if args.include_providers:
            provider_filter_list.extend(args.include_providers)

        parsers = current_settings.provider_context.get_parsers(current_settings, filter_names=provider_filter_list)

        if archive_callback or gallery_callback:
            for parser in parsers:
                if archive_callback:
                    parser.archive_callback = archive_callback
                if gallery_callback:
                    parser.gallery_callback = gallery_callback

        if args.crawl_from_feed:
            for parser in parsers:
                if parser.feed_urls_implemented():
                    args.url.extend(parser.get_feed_urls())

        if len(args.url) == 0 and not args.json_source:
            logger.info('No urls to crawl, Web Crawler done.')
            return

        to_use_urls = set(args.url)

        if args.update_mode or current_settings.update_metadata_mode:
            wanted_filters: Optional[QuerySet] = None
            current_settings.update_metadata_mode = True
        elif args.no_wanted_check:
            wanted_filters = None
        else:
            wanted_filters = WantedGallery.objects.eligible_to_search()
        if args.wanted_only and not wanted_filters:
            logger.info('Started wanted galleries only mode, but no eligible filter was found.')
            return

        if args.force_replace_metadata:
            current_settings.replace_metadata = True

        if args.force_failed:
            current_settings.retry_failed = True

        # This parser get imported directly since it's for JSON data, not for crawling URLs yet.
        if args.json_source:
            self.crawl_json_source(args, current_settings, wanted_filters)

        # This threading implementation waits for all providers to end to return.
        # A better solution would be that we listen to the queue here and send a new URL to crawl
        # when the provider queue is free.
        provider_threads = []

        for parser in parsers:
            urls = parser.filter_accepted_urls(list(to_use_urls))
            if urls:
                to_use_urls = to_use_urls.difference(set(urls))
                logger.info(
                    'Crawling {} links from provider {}. Wanted galleries to check: {}'.format(
                        len(urls),
                        parser.name,
                        wanted_filters.count() if wanted_filters else 0
                    )
                )
                provider_thread = threading.Thread(
                    name='provider_{}_thread'.format(parser.name),
                    target=parser.crawl_urls_caller,
                    args=(urls,),
                    kwargs={
                        'wanted_filters': wanted_filters, 'wanted_only': args.wanted_only,
                        'preselected_wanted_matches': preselected_wanted_matches
                    }
                )
                provider_thread.daemon = True
                provider_thread.start()
                provider_threads.append(provider_thread)

        for provider_thread in provider_threads:
            provider_thread.join()

        logger.info('Web Crawler links crawling done.')
        return

    def start_crawling_no_argparser(
            self,
            arg_line: list[str],
            override_options: 'Optional[Settings]' = None,
            archive_callback: 'Optional[Callable[[Optional[Archive], Optional[str], str], None]]' = None,
            gallery_callback: 'Optional[Callable[[Optional[Gallery], Optional[str], str], None]]' = None,
    ):

        if override_options:
            current_settings = override_options
        else:
            current_settings = self.settings

        parsers = current_settings.provider_context.get_parsers(current_settings)

        if archive_callback or gallery_callback:
            for parser in parsers:
                if archive_callback:
                    parser.archive_callback = archive_callback
                if gallery_callback:
                    parser.gallery_callback = gallery_callback

        if len(arg_line) == 0:
            logger.info('No urls to crawl, Web Crawler done.')
            return

        to_use_urls = set(arg_line)

        if current_settings.update_metadata_mode:
            wanted_filters: Optional[QuerySet] = None
            current_settings.update_metadata_mode = True
        else:
            wanted_filters = WantedGallery.objects.eligible_to_search()

        # This threading implementation waits for all providers to end to return.
        # A better solution would be that we listen to the queue here and send a new URL to crawl
        # when the provider queue is free.
        provider_threads = []

        for parser in parsers:
            urls = parser.filter_accepted_urls(list(to_use_urls))
            if urls:
                to_use_urls = to_use_urls.difference(set(urls))
                logger.info('Crawling {} links from provider {}.'.format(len(urls), parser.name))
                provider_thread = threading.Thread(
                    name='provider_{}_thread'.format(parser.name),
                    target=parser.crawl_urls_caller,
                    args=(urls,),
                    kwargs={
                        'wanted_filters': wanted_filters, 'wanted_only': False
                    }
                )
                provider_thread.daemon = True
                provider_thread.start()
                provider_threads.append(provider_thread)

        for provider_thread in provider_threads:
            provider_thread.join()

        logger.info('Web Crawler links crawling done.')
        return

    def update_galleries_newer_than(self, args, current_settings):
        galleries = Gallery.objects.filter(
            posted__gte=args.update_newer_than
        )
        if args.include_providers:
            galleries = galleries.filter(provider__in=args.include_providers)
        if args.exclude_providers:
            galleries = galleries.exclude(provider__in=args.exclude_providers)
        if galleries:
            logger.info("Updating metadata for {} galleries posted after {}".format(
                galleries.count(), args.update_newer_than)
            )

            gallery_links = [gallery.get_link() for gallery in galleries]

            parsers = current_settings.provider_context.get_parsers(current_settings)

            for parser in parsers:
                urls = parser.filter_accepted_urls(gallery_links)
                galleries_data = parser.fetch_multiple_gallery_data(urls)
                if galleries_data:
                    for gallery_data in galleries_data:

                        single_gallery = Gallery.objects.update_or_create_from_values(gallery_data)
                        for archive in single_gallery.archive_set.all():
                            archive.title = archive.gallery.title
                            archive.title_jpn = archive.gallery.title_jpn
                            archive.simple_save()
                            archive.tags.set(archive.gallery.tags.all())

    def update_galleries_missing_thumbnails(self, args, current_settings):
        galleries = Gallery.objects.filter(
            Q(thumbnail_url='') | Q(thumbnail='')
        ).order_by('id')

        if args.include_providers:
            galleries = galleries.filter(provider__in=args.include_providers)
        if args.exclude_providers:
            galleries = galleries.exclude(provider__in=args.exclude_providers)

        if args.update_missing_thumbnails:
            galleries = galleries[:args.update_missing_thumbnails]

        if galleries:
            logger.info("Updating metadata for {} galleries with missing thumbnail".format(
                galleries.count())
            )

            gallery_links = [gallery.get_link() for gallery in galleries]

            parsers = current_settings.provider_context.get_parsers(current_settings)

            for parser in parsers:
                urls = parser.filter_accepted_urls(gallery_links)
                galleries_data = parser.fetch_multiple_gallery_data(urls)
                if galleries_data:
                    for gallery_data in galleries_data:

                        gallery = self.settings.gallery_model.objects.update_or_create_from_values(gallery_data)

                        for archive in gallery.archive_set.all():
                            archive.title = archive.gallery.title
                            archive.title_jpn = archive.gallery.title_jpn
                            archive.simple_save()
                            archive.tags.set(archive.gallery.tags.all())
        else:
            logger.info("No galleries with missing thumbnail after applying filters")
