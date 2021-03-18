import typing
from time import sleep
from typing import Optional

from core.base import setup
from core.base.utilities import check_for_running_threads

if typing.TYPE_CHECKING:
    from core.downloaders.postdownload import TimedPostDownloader
    from core.workers.autosearch import ProviderTimedAutoCrawler
    from core.workers.autoupdate import TimedAutoUpdater
    from core.workers.auto_wanted import TimedAutoWanted
    from core.workers.webqueue import WebQueue
    from viewer.models import Scheduler


class WorkerContext:
    web_queue: Optional['WebQueue'] = None
    timed_auto_wanted: Optional['TimedAutoWanted'] = None
    timed_updater: Optional['TimedAutoUpdater'] = None
    timed_downloader: Optional['TimedPostDownloader'] = None
    timed_auto_crawlers: list['ProviderTimedAutoCrawler'] = []

    def get_active_initialized_workers(self):
        workers = []
        if self.timed_auto_wanted:
            workers.append(self.timed_auto_wanted)
        if self.timed_updater:
            workers.append(self.timed_updater)
        if self.timed_downloader:
            workers.append(self.timed_downloader)
        workers.extend(self.timed_auto_crawlers)
        return workers

    def start_workers(self, crawler_settings: 'setup.Settings') -> None:

        from core.downloaders.postdownload import TimedPostDownloader
        from core.workers.autosearch import ProviderTimedAutoCrawler
        from core.workers.autoupdate import TimedAutoUpdater
        from core.workers.auto_wanted import TimedAutoWanted
        from core.workers.webqueue import WebQueue
        from viewer.models import Scheduler

        self.web_queue = WebQueue(crawler_settings)
        self.timed_downloader = TimedPostDownloader(
            crawler_settings,
            web_queue=self.web_queue,
            timer=5,
            parallel_post_downloaders=crawler_settings.parallel_post_downloaders,
        )

        for provider_name in crawler_settings.autochecker.providers:
            setup.GlobalInfo.worker_threads.append(
                (
                    'auto_search_' + provider_name,
                    'Searches for new galleries that matches wanted galleries (provider: {})'.format(provider_name),
                    'scheduler'
                ),
            )
            provider_auto_crawler = ProviderTimedAutoCrawler(
                crawler_settings,
                provider_name,
                web_queue=self.web_queue,
                timer=crawler_settings.providers[provider_name].autochecker_timer
            )
            self.timed_auto_crawlers.append(provider_auto_crawler)

        self.timed_auto_wanted = TimedAutoWanted(
            crawler_settings,
            timer=crawler_settings.auto_wanted.cycle_timer
        )
        self.timed_updater = TimedAutoUpdater(
            crawler_settings,
            web_queue=self.web_queue,
            timer=crawler_settings.autoupdater.cycle_timer
        )

        obj = Scheduler.objects.get_or_create(
            name=self.timed_downloader.thread_name,
        )
        self.timed_downloader.last_run = obj[0].last_run
        self.timed_downloader.pk = obj[0].pk
        if crawler_settings.timed_downloader_startup:
            self.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)

        for provider_auto_crawler in self.timed_auto_crawlers:
            obj = Scheduler.objects.get_or_create(
                name=provider_auto_crawler.thread_name,
            )
            provider_auto_crawler.last_run = obj[0].last_run
            provider_auto_crawler.pk = obj[0].pk
            if crawler_settings.autochecker.startup:
                provider_auto_crawler.start_running(timer=provider_auto_crawler.original_timer)

        obj = Scheduler.objects.get_or_create(
            name=self.timed_auto_wanted.thread_name,
        )
        self.timed_auto_wanted.last_run = obj[0].last_run
        self.timed_auto_wanted.pk = obj[0].pk
        if crawler_settings.auto_wanted.startup:
            self.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)

        obj = Scheduler.objects.get_or_create(
            name=self.timed_updater.thread_name,
        )
        self.timed_updater.last_run = obj[0].last_run
        self.timed_updater.pk = obj[0].pk
        if crawler_settings.autoupdater.startup:
            self.timed_updater.start_running(timer=crawler_settings.autoupdater.cycle_timer)

    def command_workers_to_stop(self) -> None:

        if self.timed_downloader:
            self.timed_downloader.stop_running()
        if self.timed_auto_wanted:
            self.timed_auto_wanted.stop_running()
        if self.timed_updater:
            self.timed_updater.stop_running()
        for provider_auto_crawler in self.timed_auto_crawlers:
            provider_auto_crawler.stop_running()

    # TODO: improve this logic
    def stop_workers_and_wait(self) -> None:

        self.command_workers_to_stop()

        while check_for_running_threads():
            sleep(1)
