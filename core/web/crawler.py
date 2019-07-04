import argparse
import threading

import os
from typing import List, Union, Callable, Optional

import requests
from django.db.models import QuerySet

from core.base.setup import Settings
from core.base.types import OptionalLogger, FakeLogger, RealLogger
from core.downloaders.postdownload import PostDownloader
from core.base.parsers import InternalParser
from viewer.models import Gallery, WantedGallery, Archive


class ArgumentParserError(Exception):
    pass


class YieldingArgumentParser(argparse.ArgumentParser):

    def error(self, message: str) -> None:  # type: ignore
        raise ArgumentParserError(message)


class WebCrawler(object):

    def __init__(self, settings: Settings, logger: OptionalLogger) -> None:
        self.settings = settings
        if not logger:
            self.logger: RealLogger = FakeLogger()
        else:
            self.logger = logger
        self.parse_error = False

    def get_args(self, arg_line: List[str]) -> Union[argparse.Namespace, ArgumentParserError]:
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

        parser.add_argument('-um', '--update-mode',
                            required=False,
                            action='store_true',
                            help='Mode set when updating existing galleries, so that filters are not applied.')

        parser.add_argument('-ip', '--include-provider',
                            required=False,
                            action='store',
                            nargs='*',
                            help='List of provider names to include (exclusively) from some of the tools.')

        parser.add_argument('-ep', '--exclude-provider',
                            required=False,
                            action='store',
                            nargs='*',
                            help='List of provider names to exclude from some of the tools.')

        parser.add_argument('-wt', '--wait-timer',
                            type=int,
                            required=False,
                            action='store',
                            help='Override the wait timer.')

        parser.add_argument('-nv', '--non-verbose',
                            required=False,
                            action='store_true',
                            help='don\'t log each parser output')

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
            arg_line: List[str],
            override_options: Settings = None,
            archive_callback: Callable[[Optional['Archive'], Optional[str], str], None] = None,
            gallery_callback: Callable[[Optional['Gallery'], Optional[str], str], None] = None,
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

    def crawl_json_source(self, args, current_settings, parser_logger, wanted_filters):
        if os.path.isfile(args.json_source):
            with open(args.json_source, 'r', encoding="utf8") as f:
                json_string = f.read()
                self.logger.info('Crawling a Panda Backup JSON string from a file.')
        else:
            response = requests.get(
                args.json_source
            )
            json_string = response.text
            self.logger.info('Crawling a Panda Backup JSON string from a URL.')
        crawler = InternalParser(current_settings, parser_logger)
        crawler.crawl_json(json_string, wanted_filters=wanted_filters, wanted_only=args.wanted_only)

    def start_crawling_parse_args(
            self,
            arg_line: List[str],
            override_options: Settings = None,
            archive_callback: Callable[[Optional['Archive'], Optional[str], str], None] = None,
            gallery_callback: Callable[[Optional['Gallery'], Optional[str], str], None] = None,
    ):

        args = self.get_args(arg_line)

        if isinstance(args, ArgumentParserError):
            self.logger.info(str(args))
            return

        if override_options:
            current_settings = override_options
        else:
            current_settings = self.settings

        if args.non_verbose:
            parser_logger: OptionalLogger = FakeLogger()
        else:
            parser_logger = self.logger

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

            self.update_galleries_newer_than(args, current_settings, parser_logger)

        if args.transfer_missing_downloads:
            post_downloader = PostDownloader(current_settings, self.logger)
            post_downloader.transfer_all_missing()

        if args.set_reason:
            current_settings.archive_reason = args.set_reason
            current_settings.gallery_reason = args.set_reason

        if args.set_details:
            current_settings.archive_details = args.set_details

        parsers = current_settings.provider_context.get_parsers(current_settings, parser_logger)

        if archive_callback or gallery_callback:
            for parser in parsers:
                if archive_callback:
                    parser.archive_callback = archive_callback
                if gallery_callback:
                    parser.gallery_callback = gallery_callback

        if args.crawl_from_feed:
            for parser in parsers:
                if parser.name in current_settings.autochecker.providers:
                    if parser.feed_urls_implemented():
                        args.url.extend(parser.get_feed_urls())

        if len(args.url) == 0 and not args.json_source:
            self.logger.info('No urls to crawl, Web Crawler done.')
            return

        to_use_urls = set(args.url)

        if args.update_mode or current_settings.update_metadata_mode:
            wanted_filters: QuerySet = []
            current_settings.update_metadata_mode = True
        else:
            wanted_filters = WantedGallery.objects.eligible_to_search()
        if args.wanted_only and not wanted_filters:
            self.logger.info('Started wanted galleries only mode, but no eligible filter was found.')
            return

        # This parser get imported directly since it's for JSON data, not for crawling URLs yet.
        if args.json_source:

            self.crawl_json_source(args, current_settings, parser_logger, wanted_filters)

        # This threading implementation waits for all providers to end to return.
        # A better solution would be that we listen to the queue here and send a new URL to crawl
        # when the provider queue is free.
        if current_settings.db_engine != "sqlite":
            provider_threads = []

            for parser in parsers:
                urls = parser.filter_accepted_urls(list(to_use_urls))
                if urls:
                    to_use_urls = to_use_urls.difference(set(urls))
                    self.logger.info('Crawling {} links from provider {}.'.format(len(urls), parser.name))
                    provider_thread = threading.Thread(
                        name='provider_{}_thread'.format(parser.name),
                        target=parser.crawl_urls_caller,
                        args=(urls,),
                        kwargs={
                            'wanted_filters': wanted_filters, 'wanted_only': args.wanted_only
                        }
                    )
                    provider_thread.start()
                    provider_threads.append(provider_thread)

            for provider_thread in provider_threads:
                provider_thread.join()
        else:
            for parser in parsers:
                urls = parser.filter_accepted_urls(to_use_urls)
                if urls:
                    to_use_urls = to_use_urls.difference(set(urls))
                    self.logger.info('Crawling {} links from provider {}.'.format(len(urls), parser.name))
                    parser.crawl_urls(
                        urls,
                        wanted_filters=wanted_filters,
                        wanted_only=args.wanted_only,
                    )

        self.logger.info('Web Crawler links crawling done.')
        return

    def start_crawling_no_argparser(
            self,
            arg_line: List[str],
            override_options: Settings = None,
            archive_callback: Callable[[Optional['Archive'], Optional[str], str], None] = None,
            gallery_callback: Callable[[Optional['Gallery'], Optional[str], str], None] = None,
    ):

        if override_options:
            current_settings = override_options
        else:
            current_settings = self.settings

        parser_logger = self.logger

        parsers = current_settings.provider_context.get_parsers(current_settings, parser_logger)

        if archive_callback or gallery_callback:
            for parser in parsers:
                if archive_callback:
                    parser.archive_callback = archive_callback
                if gallery_callback:
                    parser.gallery_callback = gallery_callback

        if len(arg_line) == 0:
            self.logger.info('No urls to crawl, Web Crawler done.')
            return

        to_use_urls = set(arg_line)

        if current_settings.update_metadata_mode:
            wanted_filters: QuerySet = []
            current_settings.update_metadata_mode = True
        else:
            wanted_filters = WantedGallery.objects.eligible_to_search()

        # This threading implementation waits for all providers to end to return.
        # A better solution would be that we listen to the queue here and send a new URL to crawl
        # when the provider queue is free.
        if current_settings.db_engine != "sqlite":
            provider_threads = []

            for parser in parsers:
                urls = parser.filter_accepted_urls(list(to_use_urls))
                if urls:
                    to_use_urls = to_use_urls.difference(set(urls))
                    self.logger.info('Crawling {} links from provider {}.'.format(len(urls), parser.name))
                    provider_thread = threading.Thread(
                        name='provider_{}_thread'.format(parser.name),
                        target=parser.crawl_urls_caller,
                        args=(urls,),
                        kwargs={
                            'wanted_filters': wanted_filters, 'wanted_only': False
                        }
                    )
                    provider_thread.start()
                    provider_threads.append(provider_thread)

            for provider_thread in provider_threads:
                provider_thread.join()
        else:
            for parser in parsers:
                urls = parser.filter_accepted_urls(to_use_urls)
                if urls:
                    to_use_urls = to_use_urls.difference(set(urls))
                    self.logger.info('Crawling {} links from provider {}.'.format(len(urls), parser.name))
                    parser.crawl_urls(
                        urls,
                        wanted_filters=wanted_filters,
                        wanted_only=False,
                    )

        self.logger.info('Web Crawler links crawling done.')
        return

    def update_galleries_newer_than(self, args, current_settings, parser_logger):
        galleries = Gallery.objects.filter(
            posted__gte=args.update_newer_than
        )
        if args.include_providers:
            galleries = galleries.filter(provider__in=args.include_providers)
        if args.exclude_providers:
            galleries = galleries.exclude(provider__in=args.exclude_providers)
        if galleries:
            self.logger.info("Updating metadata for {} galleries posted after {}".format(
                galleries.count(), args.update_newer_than)
            )

            gallery_links = [gallery.get_link() for gallery in galleries]

            parsers = current_settings.provider_context.get_parsers(current_settings, parser_logger)

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
