import logging

crawler_logger = logging.getLogger('viewer.webcrawler')


class WorkerContext:
    web_queue = None
    timed_crawler = None
    timed_auto_wanted = None
    timed_updater = None
    timed_downloader = None

    def start_workers(self, crawler_settings):

        from core.downloaders.postdownload import TimedPostDownloader
        from core.workers.autosearch import TimedAutoCrawler
        from core.workers.autoupdate import TimedAutoUpdater
        from core.workers.auto_wanted import TimedAutoWanted
        from core.workers.webqueue import WebQueue
        from viewer.models import Scheduler

        self.web_queue = WebQueue(crawler_settings, crawler_logger=crawler_logger)
        self.timed_downloader = TimedPostDownloader(
            crawler_settings,
            web_queue=self.web_queue,
            crawler_logger=crawler_logger,
            timer=5,
            parallel_post_downloaders=crawler_settings.parallel_post_downloaders,
        )
        self.timed_crawler = TimedAutoCrawler(
            crawler_settings,
            web_queue=self.web_queue,
            crawler_logger=crawler_logger,
            timer=crawler_settings.autochecker.cycle_timer
        )
        self.timed_auto_wanted = TimedAutoWanted(
            crawler_settings,
            crawler_logger=crawler_logger,
            timer=crawler_settings.auto_wanted.cycle_timer
        )
        self.timed_updater = TimedAutoUpdater(
            crawler_settings,
            web_queue=self.web_queue,
            crawler_logger=crawler_logger,
            timer=crawler_settings.autoupdater.cycle_timer
        )

        obj = Scheduler.objects.get_or_create(
            name=self.timed_downloader.thread_name,
        )
        self.timed_downloader.last_run = obj[0].last_run
        self.timed_downloader.pk = obj[0].pk
        if crawler_settings.timed_downloader_startup:
            self.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)

        obj = Scheduler.objects.get_or_create(
            name=self.timed_crawler.thread_name,
        )
        self.timed_crawler.last_run = obj[0].last_run
        self.timed_crawler.pk = obj[0].pk
        if crawler_settings.autochecker.startup:
            self.timed_crawler.start_running(timer=crawler_settings.autochecker.cycle_timer)

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
