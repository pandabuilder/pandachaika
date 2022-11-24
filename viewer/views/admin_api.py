import os.path
import json
import logging
import signal
import threading

from django.http import HttpResponse, HttpRequest
from django.conf import settings
from django.utils.dateparse import parse_date

from core.base.utilities import (
    get_thread_status_bool,
    thread_exists)
from core.web.crawlerthread import CrawlerThread
from core.workers.imagework import ImageWorker

from viewer.models import (
    Archive,
    ArchiveMatches,
    WantedGallery)
from viewer.utils.matching import (
    create_matches_wanted_galleries_from_providers,
    create_matches_wanted_galleries_from_providers_internal,
    generate_possible_matches_for_archives)


MAIN_LOGGER = settings.MAIN_LOGGER
crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


# Admin related APIs
def tools(request: HttpRequest, tool: str = "main", tool_arg: str = '') -> HttpResponse:
    """Tools listing."""
    response = {}
    if not request.user.is_staff:
        response['error'] = 'Not allowed'
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
    if tool == "transfer_missing_downloads":
        crawler_thread = CrawlerThread(crawler_settings, '-tmd'.split())
        crawler_thread.start()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "retry_failed":
        crawler_thread = CrawlerThread(crawler_settings, '--retry-failed'.split())
        crawler_thread.start()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "update_newer_than":
        p = request.GET
        if p and 'newer_than' in p and crawler_settings.workers.web_queue:
            newer_than_date = p['newer_than']
            try:
                if parse_date(newer_than_date) is not None:
                    crawler_settings.workers.web_queue.enqueue_args_list(('-unt', newer_than_date))
                    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
                else:
                    response['error'] = 'Invalid date format.'
            except ValueError:
                response['error'] = 'Invalid date.'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
    elif tool == "update_missing_thumbnails":
        p = request.GET
        if p and 'limit_number' in p and crawler_settings.workers.web_queue:

            try:
                limit_number = int(p['limit_number'])

                provider = request.GET.get('provider', '')
                if provider:
                    crawler_settings.workers.web_queue.enqueue_args_list(
                        ('-umt', str(limit_number), '-ip', provider)
                    )
                else:
                    crawler_settings.workers.web_queue.enqueue_args_list(
                        ('-umt', str(limit_number))
                    )
                return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")

            except ValueError:
                response['error'] = 'Invalid limit.'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
    elif tool == "generate_missing_thumbs":
        archives = Archive.objects.filter(thumbnail='')
        for archive in archives:
            logger.info(
                'Generating thumbs for file: {}'.format(archive.zipped.name))
            archive.generate_thumbnails()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "calculate_missing_info":
        archives_missing_file_info = Archive.objects.filter_by_missing_file_info()
        for archive in archives_missing_file_info:
            logger.info(
                'Calculating file info for file: {}'.format(archive.zipped.name))
            archive.recalc_fileinfo()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "recalc_all_file_info":
        if thread_exists('fileinfo_worker'):
            response['error'] = 'File info worker is already running.'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)

        archives = Archive.objects.all()
        logger.info(
            'Recalculating file info for all archives, count: {}'.format(archives.count())
        )

        image_worker_thread = ImageWorker(4)
        for archive in archives:
            if os.path.exists(archive.zipped.path):
                image_worker_thread.enqueue_archive(archive)
        fileinfo_thread = threading.Thread(
            name='fileinfo_worker', target=image_worker_thread.start_info_thread)
        fileinfo_thread.start()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "set_all_hidden_as_public":

        archives = Archive.objects.filter(gallery__hidden=True)

        for archive in archives:
            if os.path.isfile(archive.zipped.path):
                archive.public = True
                archive.save()
                if archive.gallery:
                    archive.gallery.public = True
                    archive.gallery.save()

        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "regenerate_all_thumbs":
        if thread_exists('thumbnails_worker'):
            response['error'] = "Thumbnails worker is already running."
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")

        archives = Archive.objects.all()
        logger.info(
            'Generating thumbs for all archives, count {}'.format(archives.count()))

        image_worker_thread = ImageWorker(4)
        for archive in archives:
            if os.path.exists(archive.zipped.path):
                image_worker_thread.enqueue_archive(archive)
        thumbnails_thread = threading.Thread(
            name='thumbnails_worker',
            target=image_worker_thread.start_thumbs_thread
        )
        thumbnails_thread.start()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "generate_possible_matches_internally":
        if thread_exists('match_unmatched_worker'):
            response['error'] = "Matching worker is already running."
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
        provider = request.GET.get('provider', '')
        try:
            cutoff = float(request.GET.get('cutoff', '0.4'))
        except ValueError:
            cutoff = 0.4
        try:
            max_matches = int(request.GET.get('max-matches', '10'))
        except ValueError:
            max_matches = 10
        logger.info(
            'Looking for possible matches in gallery database '
            'for non-matched archives (cutoff: {}, max matches: {}) '
            'using provider filter "{}"'.format(cutoff, max_matches, provider)
        )
        matching_thread = threading.Thread(
            name='match_unmatched_worker',
            target=generate_possible_matches_for_archives,
            args=(None,),
            kwargs={
                'cutoff': cutoff, 'max_matches': max_matches, 'filters': (provider,),
                'match_local': True, 'match_web': False
            })
        matching_thread.daemon = True
        matching_thread.start()
        response['message'] = 'Looking for possible matches, filtering providers: {}, cutoff: {}.'.format(
            provider, cutoff
        )
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "clear_all_archive_possible_matches":
        ArchiveMatches.objects.all().delete()
        logger.info('Clearing all possible matches for archives.')
        response['message'] = 'Clearing all possible matches for archives.'
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "search_wanted_galleries_provider_titles":
        if thread_exists('web_search_worker'):
            response['error'] = 'Web search worker is already running.'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
        results = WantedGallery.objects.eligible_to_search()

        if not results:
            logger.info('No wanted galleries eligible to search.')
            response['message'] = 'No wanted galleries eligible to search.'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")

        provider = request.GET.get('provider', '')

        logger.info(
            'Searching for gallery matches in panda for wanted galleries, starting thread.')
        response['message'] = 'Searching for gallery matches in panda for wanted galleries, starting thread.'
        panda_search_thread = threading.Thread(
            name='web_search_worker',
            target=create_matches_wanted_galleries_from_providers,
            args=(results, provider),
        )
        panda_search_thread.daemon = True
        panda_search_thread.start()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "wanted_galleries_possible_matches":
        if thread_exists('wanted_local_search_worker'):
            response['error'] = "Wanted local matching worker is already running."
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)

        non_match_wanted = WantedGallery.objects.eligible_to_search()

        if not non_match_wanted:
            logger.info('No wanted galleries eligible to search.')
            response['message'] = 'No wanted galleries eligible to search.'
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")

        logger.info(
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
            kwargs={'provider_filter': provider, 'cutoff': cutoff, 'max_matches': max_matches})
        matching_thread.daemon = True
        matching_thread.start()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "restart_viewer":
        crawler_settings.workers.stop_workers_and_wait()
        if hasattr(signal, 'SIGUSR2'):
            os.kill(os.getpid(), signal.SIGUSR2)  # type: ignore
        else:
            response['error'] = "This OS does not support signal SIGUSR2"
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "settings":
        if request.method == 'POST':
            if not request.body:
                response['error'] = 'Empty body'
                return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
            data = json.loads(request.body.decode("utf-8"))
            if 'data' not in data:
                response['error'] = 'Missing data'
                return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8", status_code=401)
            settings_text = data['data']
            if os.path.isfile(os.path.join(crawler_settings.default_dir, "settings.ini")):
                with open(os.path.join(crawler_settings.default_dir,
                                       "settings.ini"),
                          "w",
                          encoding="utf-8"
                          ) as f:
                    f.write(settings_text)
                    logger.info(
                        'Modified settings file for Panda Backup')
                    response['message'] = 'Modified settings file for Panda Backup'
                    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        else:
            if os.path.isfile(os.path.join(crawler_settings.default_dir, "settings.ini")):
                with open(os.path.join(crawler_settings.default_dir, "settings.ini"), "r", encoding="utf-8") as f:
                    first = f.read(1)
                    if first != '\ufeff':
                        # not a BOM, rewind
                        f.seek(0)
                    settings_text = f.read()
                    response['data'] = settings_text
                    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "reload_settings":
        crawler_settings.load_config_from_file()
        logger.info(
            'Reloaded settings file for Panda Backup')
        response['message'] = 'Reloaded settings file for Panda Backup'
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "start_timed_dl":
        if crawler_settings.workers.timed_downloader:
            crawler_settings.workers.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "stop_timed_dl":
        if crawler_settings.workers.timed_downloader:
            crawler_settings.workers.timed_downloader.stop_running()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "force_run_timed_dl":
        if crawler_settings.workers.timed_downloader:
            crawler_settings.workers.timed_downloader.stop_running()
            crawler_settings.workers.timed_downloader.force_run_once = True
            crawler_settings.workers.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "start_timed_crawler":
        if tool_arg:
            for provider_auto_crawler in crawler_settings.workers.timed_auto_crawlers:
                if provider_auto_crawler.provider_name == tool_arg:
                    provider_auto_crawler.start_running(
                        timer=crawler_settings.providers[provider_auto_crawler.provider_name].autochecker_timer
                    )
                    break
        else:
            for provider_auto_crawler in crawler_settings.workers.timed_auto_crawlers:
                provider_auto_crawler.start_running(
                    timer=crawler_settings.providers[provider_auto_crawler.provider_name].autochecker_timer
                )
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "stop_timed_crawler":
        if tool_arg:
            for provider_auto_crawler in crawler_settings.workers.timed_auto_crawlers:
                if provider_auto_crawler.provider_name == tool_arg:
                    provider_auto_crawler.stop_running()
                    break
        else:
            for provider_auto_crawler in crawler_settings.workers.timed_auto_crawlers:
                provider_auto_crawler.stop_running()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "force_run_timed_crawler":
        if tool_arg:
            for provider_auto_crawler in crawler_settings.workers.timed_auto_crawlers:
                if provider_auto_crawler.provider_name == tool_arg:
                    provider_auto_crawler.stop_running()
                    provider_auto_crawler.force_run_once = True
                    provider_auto_crawler.start_running(
                        timer=crawler_settings.providers[provider_auto_crawler.provider_name].autochecker_timer
                    )
                    break
        else:
            for provider_auto_crawler in crawler_settings.workers.timed_auto_crawlers:
                provider_auto_crawler.stop_running()
                provider_auto_crawler.force_run_once = True
                provider_auto_crawler.start_running(
                    timer=crawler_settings.providers[provider_auto_crawler.provider_name].autochecker_timer
                )
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "start_timed_updater":
        if tool_arg:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                if provider_auto_updater.provider_name == tool_arg:
                    provider_auto_updater.start_running(
                        timer=crawler_settings.providers[provider_auto_updater.provider_name].autoupdater_timer
                    )
                    break
        else:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                provider_auto_updater.start_running(
                    timer=crawler_settings.providers[provider_auto_updater.provider_name].autoupdater_timer
                )
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "stop_timed_updater":
        if tool_arg:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                if provider_auto_updater.provider_name == tool_arg:
                    provider_auto_updater.stop_running()
                    break
        else:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                provider_auto_updater.stop_running()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "force_run_timed_updater":
        if tool_arg:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                if provider_auto_updater.provider_name == tool_arg:
                    provider_auto_updater.stop_running()
                    provider_auto_updater.force_run_once = True
                    provider_auto_updater.start_running(
                        timer=crawler_settings.providers[provider_auto_updater.provider_name].autoupdater_timer
                    )
                    break
        else:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                provider_auto_updater.stop_running()
                provider_auto_updater.force_run_once = True
                provider_auto_updater.start_running(
                    timer=crawler_settings.providers[provider_auto_updater.provider_name].autoupdater_timer
                )
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "start_timed_auto_wanted":
        if crawler_settings.workers.timed_auto_wanted:
            crawler_settings.workers.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "stop_timed_auto_wanted":
        if crawler_settings.workers.timed_auto_wanted:
            crawler_settings.workers.timed_auto_wanted.stop_running()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "force_run_timed_auto_wanted":
        if crawler_settings.workers.timed_auto_wanted:
            crawler_settings.workers.timed_auto_wanted.stop_running()
            crawler_settings.workers.timed_auto_wanted.force_run_once = True
            crawler_settings.workers.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "start_web_queue":
        if crawler_settings.workers.web_queue:
            crawler_settings.workers.web_queue.start_running()
        return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
    elif tool == "threads_status":
        threads_status = get_thread_status_bool()
        return HttpResponse(json.dumps({'data': threads_status}), content_type="application/json; charset=utf-8")
    elif tool == "autochecker_providers":
        threads_status = get_thread_status_bool()
        autochecker_providers = (
            (provider_name, threads_status['auto_search_' + provider_name]) for provider_name in
            crawler_settings.autochecker.providers if 'auto_search_' + provider_name in threads_status
        )
        return HttpResponse(json.dumps({'data': autochecker_providers}), content_type="application/json; charset=utf-8")

    response['error'] = 'Missing parameters'
    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
