import typing
from time import sleep
from typing import Optional

from core.base import setup
from core.base.utilities import check_for_running_threads

if typing.TYPE_CHECKING:
    from core.downloaders.postdownload import TimedPostDownloader
    from core.workers.autosearch import ProviderTimedAutoCrawler
    from core.workers.autoupdate import ProviderTimedAutoUpdater
    from core.workers.auto_wanted import TimedAutoWanted
    from core.workers.webqueue import WebQueue
    from core.workers.link_monitor import LinkMonitor
    from viewer.models import Scheduler, MonitoredLink


class WorkerContext:
    web_queue: Optional['WebQueue'] = None
    timed_auto_wanted: Optional['TimedAutoWanted'] = None
    timed_downloader: Optional['TimedPostDownloader'] = None
    timed_auto_crawlers: list['ProviderTimedAutoCrawler'] = []
    timed_auto_updaters: list['ProviderTimedAutoUpdater'] = []
    timed_link_monitors: list['LinkMonitor'] = []

    def get_active_initialized_workers(self):
        workers = []
        if self.timed_auto_wanted:
            workers.append(self.timed_auto_wanted)
        if self.timed_downloader:
            workers.append(self.timed_downloader)
        workers.extend(self.timed_auto_crawlers)
        workers.extend(self.timed_auto_updaters)
        workers.extend(self.timed_link_monitors)
        return workers

    def add_new_link_monitor(self, crawler_settings: 'setup.Settings', monitored_link: 'MonitoredLink'):

        from viewer.models import Scheduler
        from core.workers.link_monitor import LinkMonitor

        setup.GlobalInfo.worker_threads.append(
            (
                'link_monitor_' + str(monitored_link.pk),
                'Used as input for WantedGalleries to match against. Name: {}'.format(monitored_link.name),
                'scheduler'
            ),
        )
        link_monitor = LinkMonitor(
            crawler_settings,
            monitored_link.pk,
            monitored_link.name,
            monitored_link.frequency,
            monitored_link,
            web_queue=self.web_queue,
        )
        self.timed_link_monitors.append(link_monitor)
        obj = Scheduler.objects.get_or_create(
            name=link_monitor.thread_name,
        )
        link_monitor.last_run = obj[0].last_run
        link_monitor.pk = obj[0].pk
        if link_monitor.monitored_link.auto_start:
            link_monitor.start_running(timer=link_monitor.original_timer)

    def start_workers(self, crawler_settings: 'setup.Settings') -> None:

        from core.downloaders.postdownload import TimedPostDownloader
        from core.workers.autosearch import ProviderTimedAutoCrawler
        from core.workers.autoupdate import ProviderTimedAutoUpdater
        from core.workers.auto_wanted import TimedAutoWanted
        from core.workers.link_monitor import LinkMonitor
        from core.workers.webqueue import WebQueue
        from viewer.models import Scheduler, MonitoredLink

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

        for provider_name in crawler_settings.autoupdater.providers:
            setup.GlobalInfo.worker_threads.append(
                (
                    'auto_updater_' + provider_name,
                    'Auto updates metadata for galleries (provider: {})'.format(provider_name),
                    'scheduler'
                ),
            )
            provider_auto_updater = ProviderTimedAutoUpdater(
                crawler_settings,
                provider_name,
                web_queue=self.web_queue,
                timer=crawler_settings.providers[provider_name].autoupdater_timer
            )
            self.timed_auto_updaters.append(provider_auto_updater)

        obj = Scheduler.objects.get_or_create(
            name=self.timed_downloader.thread_name,
        )
        self.timed_downloader.last_run = obj[0].last_run
        self.timed_downloader.pk = obj[0].pk
        if crawler_settings.timed_downloader_startup:
            self.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)

        if crawler_settings.monitored_links.enable:
            monitored_links = MonitoredLink.objects.all()

            for monitored_link in monitored_links:
                setup.GlobalInfo.worker_threads.append(
                    (
                        'link_monitor_' + str(monitored_link.pk),
                        'Used as input for WantedGalleries to match against. Name: {}'.format(monitored_link.name),
                        'scheduler'
                    ),
                )
                link_monitor = LinkMonitor(
                    crawler_settings,
                    monitored_link.pk,
                    monitored_link.name,
                    monitored_link.frequency,
                    monitored_link,
                    web_queue=self.web_queue,
                )
                self.timed_link_monitors.append(link_monitor)

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

        for provider_auto_updater in self.timed_auto_updaters:
            obj = Scheduler.objects.get_or_create(
                name=provider_auto_updater.thread_name,
            )
            provider_auto_updater.last_run = obj[0].last_run
            provider_auto_updater.pk = obj[0].pk
            if crawler_settings.autoupdater.startup:
                provider_auto_updater.start_running(timer=provider_auto_updater.original_timer)

        for link_monitor in self.timed_link_monitors:
            obj = Scheduler.objects.get_or_create(
                name=link_monitor.thread_name,
            )
            link_monitor.last_run = obj[0].last_run
            link_monitor.pk = obj[0].pk
            if link_monitor.monitored_link.auto_start:
                link_monitor.start_running(timer=link_monitor.original_timer)

    def command_workers_to_stop(self) -> None:

        if self.timed_downloader:
            self.timed_downloader.stop_running()
        if self.timed_auto_wanted:
            self.timed_auto_wanted.stop_running()
        for provider_auto_crawler in self.timed_auto_crawlers:
            provider_auto_crawler.stop_running()
        for provider_auto_updater in self.timed_auto_updaters:
            provider_auto_updater.stop_running()
        for link_monitor in self.timed_link_monitors:
            link_monitor.stop_running()

    # TODO: improve this logic
    def stop_workers_and_wait(self) -> None:

        self.command_workers_to_stop()

        while check_for_running_threads():
            sleep(1)
