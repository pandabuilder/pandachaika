import logging
import os.path
import re
import signal
import threading
import typing
from functools import reduce

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.urls import reverse
from django.db.models import Avg, Max, Min, Sum, Count
from django.http import HttpResponseRedirect, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.dateparse import parse_date

from core.base.setup import Settings
from core.base.utilities import (
    get_thread_status,
    get_thread_status_bool,
    thread_exists,
    check_for_running_threads,
    get_schedulers_status)
from core.local.foldercrawlerthread import FolderCrawlerThread
from core.web.crawlerthread import CrawlerThread

from core.workers.imagework import ImageWorker

from viewer.models import (
    Archive, Tag, Gallery,
    ArchiveMatches,
    WantedGallery, FoundGallery
)
from viewer.utils.matching import (
    create_matches_wanted_galleries_from_providers,
    create_matches_wanted_galleries_from_providers_internal,
    generate_possible_matches_for_archives)
from django.conf import settings
from viewer.views.head import render_error

crawler_logger = logging.getLogger('viewer.webcrawler')
folder_logger = logging.getLogger('viewer.foldercrawler')
frontend_logger = logging.getLogger('viewer.frontend')

MAIN_LOGGER = settings.MAIN_LOGGER
crawler_settings = settings.CRAWLER_SETTINGS


@login_required
def stats_settings(request: HttpRequest) -> HttpResponse:
    """Display setting objects."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to view setting objects.")

    stats_dict = {
        "current_settings": crawler_settings,
    }

    d = {'stats': stats_dict}

    return render(request, "viewer/stats_settings.html", d)


@login_required
def stats_workers(request: HttpRequest) -> HttpResponse:
    """Display workers stats."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to view workers' status.")

    stats_dict = {
        "current_settings": crawler_settings,
        "thread_status": get_thread_status(),
        "web_queue": crawler_settings.workers.web_queue,
        "post_downloader": crawler_settings.workers.timed_downloader,
        "schedulers": get_schedulers_status([
            crawler_settings.workers.timed_crawler,
            crawler_settings.workers.timed_downloader,
            crawler_settings.workers.timed_auto_wanted,
            crawler_settings.workers.timed_updater
        ])
    }

    d = {'stats': stats_dict}

    return render(request, "viewer/stats_workers.html", d)


@login_required
def stats_collection(request: HttpRequest) -> HttpResponse:
    """Display galleries and archives stats."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to display the stats.")

    # General
    stats_dict = {
        "n_archives": Archive.objects.count(),
        "n_expanded_archives": Archive.objects.filter(extracted=True).count(),
        "n_to_download_archives": Archive.objects.filter_by_dl_remote().count(),
        "n_galleries": Gallery.objects.count(), "archive": Archive.objects.filter(filesize__gt=0).aggregate(
            Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize')),
        "gallery": Gallery.objects.filter(filesize__gt=0).aggregate(
            Avg('filesize'), Max('filesize'), Min('filesize'), Sum('filesize')),
        "hidden_galleries": Gallery.objects.filter(hidden=True).count(),
        "hidden_galleries_size": Gallery.objects.filter(
            filesize__gt=0, hidden=True).aggregate(Sum('filesize')),
        "fjord_galleries": Gallery.objects.filter(fjord=True).count(),
        "expunged_galleries": Gallery.objects.filter(expunged=True).count(),
        "n_tags": Tag.objects.count(),
        "n_custom_tags": Tag.objects.are_custom().count(),
        "top_10_tags": Tag.objects.annotate(num_gallery=Count('gallery')).order_by('-num_gallery')[:10],
        "top_10_parody_tags": Tag.objects.filter(scope='parody').annotate(
            num_gallery=Count('gallery')).order_by('-num_gallery')[:10],
        "wanted_galleries": {
            "total": WantedGallery.objects.all().count(),
            "found": WantedGallery.objects.filter(found=True).count(),
            "total_galleries_found": FoundGallery.objects.all().count(),
            "user_created": WantedGallery.objects.filter(book_type='user').count(),
        }
    }

    # Per provider
    providers = Gallery.objects.all().values_list('provider', flat=True).distinct()

    providers_dict = {}

    for provider in providers:
        providers_dict[provider] = {
            'galleries': Gallery.objects.filter(provider=provider).count(),
            'archives': Archive.objects.filter(gallery__provider=provider).count(),
            'wanted_galleries': WantedGallery.objects.filter(provider=provider).count(),

        }

    d = {'stats': stats_dict, 'providers': providers_dict}

    return render(request, "viewer/stats_collection.html", d)


@login_required
def queue_operations(request: HttpRequest, operation: str, arguments: str=''):

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to operate the queue.")
    if operation == "remove_by_index":
        if arguments:
            crawler_settings.workers.web_queue.remove_by_index(int(arguments))
        else:
            return render_error(request, "Unknown argument.")
    else:
        return render_error(request, "Unknown queue operation.")

    return HttpResponseRedirect(request.META["HTTP_REFERER"])


# TODO: Restart workers not working correctly. It sends the stop signal, but tries to start before it finishes.
@login_required
def tools(request: HttpRequest, tool: str = "main") -> HttpResponse:
    """Tools listing."""
    settings_text = ''
    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to use the tools.")
    if tool == "transfer_missing_downloads":
        crawler_thread = CrawlerThread(crawler_logger,
                                       crawler_settings,
                                       '-tmd'.split())
        crawler_thread.start()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "retry_failed":
        crawler_thread = CrawlerThread(
            crawler_logger, crawler_settings, '--retry-failed'.split())
        crawler_thread.start()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "update_newer_than":
        p = request.GET
        if p and 'newer_than' in p:
            newer_than_date = p['newer_than']
            try:
                if parse_date(newer_than_date) is not None:
                    crawler_settings.workers.web_queue.enqueue_args_list(('-unt', newer_than_date))
                    messages.success(
                        request,
                        'Updating galleries posted after ' + newer_than_date
                    )
                else:
                    messages.error(
                        request,
                        'Invalid date format.',
                        extra_tags='danger'
                    )
            except ValueError:
                messages.error(
                    request,
                    'Invalid date.',
                    extra_tags='danger'
                )
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "generate_missing_thumbs":
        archives = Archive.objects.filter(thumbnail='')
        for archive in archives:
            frontend_logger.info(
                'Generating thumbs for file: {}'.format(archive.zipped.name))
            archive.generate_thumbnails()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "calculate_missing_info":
        archives = Archive.objects.filter_by_missing_file_info()
        for archive in archives:
            frontend_logger.info(
                'Calculating file info for file: {}'.format(archive.zipped.name))
            archive.recalc_fileinfo()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "recalc_all_file_info":
        if thread_exists('fileinfo_worker'):
            return render_error(request, "File info worker is already running.")

        archives = Archive.objects.all()
        frontend_logger.info(
            'Recalculating file info for all archives, count: {}'.format(archives.count())
        )

        image_worker_thread = ImageWorker(crawler_logger, 4)
        for archive in archives:
            if os.path.exists(archive.zipped.path):
                image_worker_thread.enqueue_archive(archive)
        fileinfo_thread = threading.Thread(
            name='fileinfo_worker', target=image_worker_thread.start_info_thread)
        fileinfo_thread.start()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "set_all_hidden_as_public":

        archives = Archive.objects.filter(gallery__hidden=True)

        for archive in archives:
            if os.path.isfile(archive.zipped.path):
                archive.public = True
                archive.save()
                archive.gallery.public = True
                archive.gallery.save()

        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "regenerate_all_thumbs":
        if thread_exists('thumbnails_worker'):
            return render_error(request, "Thumbnails worker is already running.")

        archives = Archive.objects.all()
        frontend_logger.info(
            'Generating thumbs for all archives, count {}'.format(archives.count()))

        image_worker_thread = ImageWorker(crawler_logger, 4)
        for archive in archives:
            if os.path.exists(archive.zipped.path):
                image_worker_thread.enqueue_archive(archive)
        thumbnails_thread = threading.Thread(
            name='thumbnails_worker',
            target=image_worker_thread.start_thumbs_thread
        )
        thumbnails_thread.start()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "generate_possible_matches_internally":
        if thread_exists('match_unmatched_worker'):
            return render_error(request, "Matching worker is already running.")
        provider = request.GET.get('provider', '')
        try:
            cutoff = float(request.GET.get('cutoff', '0.4'))
        except ValueError:
            cutoff = 0.4
        try:
            max_matches = int(request.GET.get('max-matches', '10'))
        except ValueError:
            max_matches = 10
        frontend_logger.info(
            'Looking for possible matches in gallery database '
            'for non-matched archives (cutoff: {}, max matches: {}) '
            'using provider filter "{}"'.format(cutoff, max_matches, provider)
        )
        matching_thread = threading.Thread(
            name='match_unmatched_worker',
            target=generate_possible_matches_for_archives,
            args=(None,),
            kwargs={
                'logger': frontend_logger, 'cutoff': cutoff, 'max_matches': max_matches, 'filters': (provider,),
                'match_local': True, 'match_web': False
            })
        matching_thread.daemon = True
        matching_thread.start()
        messages.success(
            request,
            'Looking for possible matches, filtering providers: {}, cutoff: {}.'.format(provider, cutoff))
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "clear_all_archive_possible_matches":
        ArchiveMatches.objects.all().delete()
        frontend_logger.info('Clearing all possible matches for archives.')
        messages.success(
            request,
            'Clearing all possible matches for archives.')
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "search_wanted_galleries_provider_titles":
        if thread_exists('web_search_worker'):
            messages.error(
                request,
                'Web search worker is already running.',
                extra_tags='danger'
            )
            return HttpResponseRedirect(request.META["HTTP_REFERER"])
        results = WantedGallery.objects.eligible_to_search()

        if not results:
            frontend_logger.info('No wanted galleries eligible to search.')
            messages.success(request, 'No wanted galleries eligible to search.')
            return HttpResponseRedirect(request.META["HTTP_REFERER"])

        provider = request.GET.get('provider', '')

        frontend_logger.info(
            'Searching for gallery matches in panda for wanted galleries, starting thread.')
        messages.success(
            request,
            'Searching for gallery matches in panda for wanted galleries, starting thread.')

        panda_search_thread = threading.Thread(
            name='web_search_worker',
            target=create_matches_wanted_galleries_from_providers,
            args=(results, provider),
            kwargs={'logger': frontend_logger})
        panda_search_thread.daemon = True
        panda_search_thread.start()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "wanted_galleries_possible_matches":
        if thread_exists('wanted_local_search_worker'):
            return render_error(request, "Wanted local matching worker is already running.")

        non_match_wanted = WantedGallery.objects.eligible_to_search()

        if not non_match_wanted:
            frontend_logger.info('No wanted galleries eligible to search.')
            messages.success(request, 'No wanted galleries eligible to search.')
            return HttpResponseRedirect(request.META["HTTP_REFERER"])

        frontend_logger.info(
            'Looking for possible matches in gallery database '
            'for wanted galleries (fixed 0.4 cutoff)'
        )

        provider = request.GET.get('provider', '')

        try:
            cutoff = float(request.GET.get('cutoff', '0.4'))
        except ValueError:
            cutoff = 0.4
        try:
            max_matches = int(request.GET.get('max-matches', '10'))
        except ValueError:
            max_matches = 10

        matching_thread = threading.Thread(
            name='wanted_local_search_worker',
            target=create_matches_wanted_galleries_from_providers_internal,
            args=(non_match_wanted, ),
            kwargs={'logger': frontend_logger, 'provider_filter': provider, 'cutoff': cutoff, 'max_matches': max_matches})
        matching_thread.daemon = True
        matching_thread.start()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "restart_viewer":
        crawler_settings.workers.stop_workers_and_wait()
        if hasattr(signal, 'SIGUSR2'):
            os.kill(os.getpid(), signal.SIGUSR2)
        else:
            return render_error(request, "This OS does not support signal SIGUSR2.")
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "modify_settings":
        p = request.POST
        if p:
            settings_text = p['settings_file']
            if(os.path.isfile(
                os.path.join(crawler_settings.default_dir, "settings.ini"))
               ):
                with open(os.path.join(crawler_settings.default_dir,
                                       "settings.ini"),
                          "w",
                          encoding="utf-8"
                          ) as f:
                    f.write(settings_text)
                    frontend_logger.info(
                        'Modified settings file for Panda Backup')
                    messages.success(
                        request,
                        'Modified settings file for Panda Backup')
                    return HttpResponseRedirect(request.META["HTTP_REFERER"])
        else:
            if(os.path.isfile(
                os.path.join(crawler_settings.default_dir, "settings.ini"))
               ):
                with open(os.path.join(crawler_settings.default_dir, "settings.ini"), "r", encoding="utf-8") as f:
                    first = f.read(1)
                    if first != '\ufeff':
                        # not a BOM, rewind
                        f.seek(0)
                    settings_text = f.read()
    elif tool == "reload_settings":
        if not request.user.is_staff:
            return render_error(request, "You need to be an admin to reload the config.")
        crawler_settings.load_config_from_file()
        frontend_logger.info(
            'Reloaded settings file for Panda Backup')
        messages.success(
            request,
            'Reloaded settings file for Panda Backup')
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "start_timed_dl":
        crawler_settings.workers.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "stop_timed_dl":
        crawler_settings.workers.timed_downloader.stop_running()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "force_run_timed_dl":
        crawler_settings.workers.timed_downloader.stop_running()
        crawler_settings.workers.timed_downloader.force_run_once = True
        crawler_settings.workers.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "start_timed_crawler":
        crawler_settings.workers.timed_crawler.start_running(timer=crawler_settings.autochecker.cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "stop_timed_crawler":
        crawler_settings.workers.timed_crawler.stop_running()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "force_run_timed_crawler":
        crawler_settings.workers.timed_crawler.stop_running()
        crawler_settings.workers.timed_crawler.force_run_once = True
        crawler_settings.workers.timed_crawler.start_running(timer=crawler_settings.autochecker.cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "start_timed_updater":
        crawler_settings.workers.timed_updater.start_running(timer=crawler_settings.autoupdater.cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "stop_timed_updater":
        crawler_settings.workers.timed_updater.stop_running()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "force_run_timed_updater":
        crawler_settings.workers.timed_updater.stop_running()
        crawler_settings.workers.timed_updater.force_run_once = True
        crawler_settings.workers.timed_updater.start_running(timer=crawler_settings.autoupdater.cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "start_timed_auto_wanted":
        crawler_settings.workers.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "stop_timed_auto_wanted":
        crawler_settings.workers.timed_auto_wanted.stop_running()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "force_run_timed_auto_wanted":
        crawler_settings.workers.timed_auto_wanted.stop_running()
        crawler_settings.workers.timed_auto_wanted.force_run_once = True
        crawler_settings.workers.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)
        return HttpResponseRedirect(request.META["HTTP_REFERER"])
    elif tool == "start_web_queue":
        crawler_settings.workers.web_queue.start_running()
        return HttpResponseRedirect(request.META["HTTP_REFERER"])

    threads_status = get_thread_status_bool()

    d = {'tool': tool, 'settings_text': settings_text, 'threads_status': threads_status}

    return render(request, "viewer/tools.html", d)


@login_required
def logs(request: HttpRequest, tool: str = "all") -> HttpResponse:
    """Logs listing."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to see logs.")

    log_lines: typing.List[str] = []

    if tool == "all" or tool == "webcrawler":

        f = open(MAIN_LOGGER, 'rt', encoding='utf8')
        log_lines = f.read().split('[0m\n')
        f.close()
        log_lines.pop()
        log_lines.reverse()

    log_filter = request.GET.get('filter', '')
    if log_filter:
        log_lines = [x for x in log_lines if log_filter.lower() in x.lower()]

    current_base_uri = re.escape('{scheme}://{host}'.format(scheme=request.scheme, host=request.get_host()))
    # Build complete URL for relative internal URLs (some)
    if crawler_settings.urls.viewer_main_url:
        patterns = [
            r'(?!' + current_base_uri + r')/' + crawler_settings.urls.viewer_main_url + r"archive/\d+/?",
            r'(?!' + current_base_uri + r')/' + crawler_settings.urls.viewer_main_url + r"gallery/\d+/?",
            r'(?!' + current_base_uri + r')/' + crawler_settings.urls.viewer_main_url + r"wanted-gallery/\d+/?",
        ]
    else:
        patterns = [
            r'(?<!' + current_base_uri + r')/archive/\d+/?',
            r'(?<!' + current_base_uri + r')/gallery/\d+/?',
            r'(?<!' + current_base_uri + r')/wanted-gallery/\d+/?',
        ]

    def build_request(match_obj: typing.Match) -> str:
        return request.build_absolute_uri(match_obj.group(0))

    log_lines = [reduce(lambda v, pattern: re.sub(pattern, build_request, v), patterns, line) for line in log_lines]

    paginator = Paginator(log_lines, 100)
    try:
        page = int(request.GET.get("page", '1'))
    except ValueError:
        page = 1

    try:
        log_lines = paginator.page(page)
    except (InvalidPage, EmptyPage):
        log_lines = paginator.page(paginator.num_pages)

    d = {'log_lines': log_lines}
    return render(request, "viewer/logs.html", d)


@login_required
def crawler(request: HttpRequest) -> HttpResponse:
    """Crawl given URLs."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to crawl a link.")

    d = {}

    p = request.POST

    if p:
        if 'keep_this_settings' in p:
            current_settings = crawler_settings
        else:
            current_settings = Settings(load_from_config=crawler_settings.config)
        url_set = set()
        # create dictionary of properties for each archive
        current_settings.replace_metadata = False
        current_settings.config['allowed']['replace_metadata'] = 'no'
        for k, v in p.items():
            if k.startswith("downloaders"):
                k, dl = k.split('-')
                current_settings.config['downloaders'][dl] = v
                current_settings.downloaders[dl] = int(v)
            elif k == "replace_metadata":
                current_settings.config['allowed'][k] = 'yes'
                current_settings.replace_metadata = True
            elif k == "urls":
                url_list = v.split("\n")
                for item in url_list:
                    url_set.add(item.rstrip('\r'))
        urls = list(url_set)

        if 'reason' in p and p['reason'] != '':
            reason = p['reason']
            # Force limit string length (reason field max_length)
            current_settings.archive_reason = reason[:200]

        if 'keep_this_settings' in p:
            current_settings.write()
            current_settings.load_config_from_file()
        if 'run_separate' in p:
            crawler_thread = CrawlerThread(crawler_logger, current_settings, urls)
            crawler_thread.start()
        else:
            current_settings.workers.web_queue.enqueue_args_list(urls, override_options=current_settings)
        messages.success(request, 'Starting Crawler, check the logs for a report.')
        # Not really optimal when there's many commands being queued
        # for command in url_list:
        #     messages.success(request, command)
        return HttpResponseRedirect(reverse('viewer:main-page'))

    d.update({
        'settings': crawler_settings,
        'downloaders': crawler_settings.provider_context.get_downloaders_name_priority(crawler_settings)
    })

    return render(request, "viewer/crawler.html", d)


@login_required
def foldercrawler(request: HttpRequest) -> HttpResponse:
    """Folder crawler."""
    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to use the tools.")

    d = {'media_root': os.path.realpath(crawler_settings.MEDIA_ROOT)}

    p = request.POST

    if p:
        if 'keep_this_settings' in p:
            current_settings = crawler_settings
        else:
            current_settings = Settings(load_from_config=crawler_settings.config)
        commands = set()
        # create dictionary of properties for each command
        for k, v in p.items():
            if k.startswith("matchers"):
                k, matcher = k.split('-')
                current_settings.config['matchers'][matcher] = v
                current_settings.matchers[matcher] = int(v)
            elif k == "commands":
                command_list = v.split("\n")
                for item in command_list:
                    commands.add(item.rstrip('\r'))
            elif k == "internal_matches":
                current_settings.internal_matches_for_non_matches = True

        if 'reason' in p and p['reason'] != '':
            reason = p['reason']
            # Force limit string length (reason field max_length)
            current_settings.archive_reason = reason[:200]

        if 'source' in p and p['source'] != '':
            source = p['source']
            # Force limit string length (reason field max_length)
            current_settings.archive_source = source[:50]

        if 'keep_this_settings' in p:
            current_settings.write()
            current_settings.load_config_from_file()
        folder_crawler = FolderCrawlerThread(folder_logger, current_settings, list(commands))
        folder_crawler.start()
        messages.success(request, 'Starting Folder Crawler, check the logs for a report.')
        # Not really optimal when there's many commands being queued
        # for command in commands:
        #     messages.success(request, command)
        return HttpResponseRedirect(reverse('viewer:main-page'))

    d.update({
        'settings': crawler_settings,
        'matchers': crawler_settings.provider_context.get_matchers_name_priority(crawler_settings)
    })

    return render(request, "viewer/foldercrawler.html", d)
