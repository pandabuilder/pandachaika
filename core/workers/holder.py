import typing
from time import sleep

from core.base import setup
from core.base.setup_utilities import check_for_running_threads

if typing.TYPE_CHECKING:
    from core.downloaders.postdownload import TimedPostDownloader
    from core.workers.download_progress import DownloadProgressChecker
    from core.workers.archive_download_queue import ArchiveDownloadQueue
    from core.workers.autoupdate import ProviderTimedAutoUpdater
    from core.workers.auto_wanted import TimedAutoWanted
    from core.workers.webqueue import WebQueue
    from core.workers.link_monitor import LinkMonitor
    from core.workers.archive_work import ArchiveWorker
    from core.local.foldercrawlerthread import FolderCrawlerThread
    from viewer.models import MonitoredLink
    from workers.models import SchedulerState


class WorkerContext:
    crawler_settings: "setup.Settings | None" = None
    web_queue: "WebQueue | None" = None
    archive_worker: "ArchiveWorker | None" = None
    folder_crawler: "FolderCrawlerThread | None" = None
    timed_auto_wanted: "TimedAutoWanted | None" = None
    timed_downloader: "TimedPostDownloader | None" = None
    download_progress_checker: "DownloadProgressChecker | None" = None
    archive_download_queue: "ArchiveDownloadQueue | None" = None
    timed_auto_updaters: list["ProviderTimedAutoUpdater"] = []
    timed_link_monitors: list["LinkMonitor"] = []

    def get_active_initialized_workers(self):
        workers = []
        if self.timed_auto_wanted:
            workers.append(self.timed_auto_wanted)
        if self.timed_downloader:
            workers.append(self.timed_downloader)
        if self.download_progress_checker:
            workers.append(self.download_progress_checker)
        if self.archive_download_queue:
            workers.append(self.archive_download_queue)
        workers.extend(self.timed_auto_updaters)
        workers.extend(self.timed_link_monitors)
        return workers

    def get_active_initialized_workers_from_db(self):
        """Query SchedulerState objects from the database for webserver status display,
        falling back to in-memory workers if not yet synced in DB.
        """
        try:
            from workers.models import SchedulerState
            states_by_name = {s.name: s for s in SchedulerState.objects.all()}
            if states_by_name:
                alias_map = {
                    "post_downloader": "timed_downloader",
                    "timed_downloader": "post_downloader",
                }
                workers = []
                initialized = self.get_active_initialized_workers()
                seen_names = set()
                for w in initialized:
                    tname = getattr(w, "thread_name", "")
                    s = states_by_name.get(tname) or states_by_name.get(alias_map.get(tname, ""))
                    if s:
                        workers.append(s)
                        seen_names.add(s.name)
                    else:
                        workers.append(w)
                    seen_names.add(tname)
                for name, s in states_by_name.items():
                    if name not in seen_names and alias_map.get(name) not in seen_names:
                        workers.append(s)
                        seen_names.add(name)
                return workers
            states = list(SchedulerState.objects.all())
            if states:
                return states
        except Exception:
            pass
        return self.get_active_initialized_workers()

    get_schedulers_from_db = get_active_initialized_workers_from_db

    def add_new_link_monitor(
        self, crawler_settings: "setup.Settings", monitored_link: "MonitoredLink", start_thread: bool = False
    ) -> "LinkMonitor":

        from workers.models import SchedulerState
        from core.workers.link_monitor import LinkMonitor

        for existing in self.timed_link_monitors:
            if existing.monitored_link.pk == monitored_link.pk:
                existing.monitored_link = monitored_link
                existing.timer = monitored_link.frequency.total_seconds()
                existing.original_timer = monitored_link.frequency
                existing.link_name = monitored_link.name
                if start_thread and not existing.is_running_locally() and monitored_link.enabled and monitored_link.auto_start:
                    existing.start_running(timer=existing.timer, force_local=True)
                return existing

        setup.GlobalInfo.worker_threads.append(
            (
                "link_monitor_" + str(monitored_link.pk),
                "Used as input for WantedGalleries to match against. Name: {}".format(monitored_link.name),
                "scheduler",
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
        obj, _ = SchedulerState.objects.get_or_create(
            name=link_monitor.thread_name,
            defaults={"schedule_type": "scheduler"},
        )
        link_monitor.last_run = obj.last_run
        link_monitor.pk = obj.name
        if start_thread and link_monitor.monitored_link.auto_start and link_monitor.monitored_link.enabled:
            link_monitor.start_running(timer=link_monitor.original_timer, force_local=True)
        return link_monitor

    def remove_link_monitor(self, link_pk: int) -> None:
        for lm in list(self.timed_link_monitors):
            if lm.monitored_link.pk == link_pk:
                lm.stop_running(force_local=True)
                self.timed_link_monitors.remove(lm)

    def start_workers(self, crawler_settings: "setup.Settings", start_threads: bool = False) -> None:

        self.crawler_settings = crawler_settings

        from core.downloaders.postdownload import TimedPostDownloader
        from core.workers.autoupdate import ProviderTimedAutoUpdater
        from core.workers.download_progress import DownloadProgressChecker
        from core.workers.auto_wanted import TimedAutoWanted
        from core.workers.link_monitor import LinkMonitor
        from core.workers.webqueue import WebQueue
        from core.workers.archive_work import ArchiveWorker
        from core.local.foldercrawlerthread import FolderCrawlerThread
        from viewer.models import MonitoredLink
        from workers.models import SchedulerState

        self.web_queue = WebQueue(crawler_settings)
        self.archive_worker = ArchiveWorker(crawler_settings, 4)
        self.folder_crawler = FolderCrawlerThread(crawler_settings, [])
        self.timed_downloader = TimedPostDownloader(
            crawler_settings,
            web_queue=self.web_queue,
            timer=5,
            parallel_post_downloaders=crawler_settings.parallel_post_downloaders,
        )

        self.timed_auto_wanted = TimedAutoWanted(crawler_settings, timer=crawler_settings.auto_wanted.cycle_timer)

        for provider_name in crawler_settings.autoupdater.providers:
            if provider_name not in crawler_settings.providers:
                continue
            setup.GlobalInfo.worker_threads.append(
                (
                    "auto_updater_" + provider_name,
                    "Auto updates metadata for galleries (provider: {})".format(provider_name),
                    "scheduler",
                ),
            )
            provider_auto_updater = ProviderTimedAutoUpdater(
                crawler_settings,
                provider_name,
                web_queue=self.web_queue,
                timer=crawler_settings.providers[provider_name].autoupdater_timer,
            )
            self.timed_auto_updaters.append(provider_auto_updater)

        obj, _ = SchedulerState.objects.get_or_create(
            name=self.timed_downloader.thread_name,
            defaults={"schedule_type": "scheduler"},
        )
        self.timed_downloader.last_run = obj.last_run
        self.timed_downloader.pk = obj.name
        if start_threads and crawler_settings.timed_downloader_startup:
            self.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)

        if crawler_settings.monitored_links.enable:
            monitored_links = MonitoredLink.objects.all()

            for monitored_link in monitored_links:
                setup.GlobalInfo.worker_threads.append(
                    (
                        "link_monitor_" + str(monitored_link.pk),
                        "Used as input for WantedGalleries to match against. Name: {}".format(monitored_link.name),
                        "scheduler",
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

        obj, _ = SchedulerState.objects.get_or_create(
            name=self.timed_auto_wanted.thread_name,
            defaults={"schedule_type": "scheduler"},
        )
        self.timed_auto_wanted.last_run = obj.last_run
        self.timed_auto_wanted.pk = obj.name
        if start_threads and crawler_settings.auto_wanted.startup:
            self.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)

        for provider_auto_updater in self.timed_auto_updaters:
            obj, _ = SchedulerState.objects.get_or_create(
                name=provider_auto_updater.thread_name,
                defaults={"schedule_type": "scheduler"},
            )
            provider_auto_updater.last_run = obj.last_run
            provider_auto_updater.pk = obj.name
            if start_threads and crawler_settings.autoupdater.startup:
                provider_auto_updater.start_running(timer=provider_auto_updater.original_timer)

        for link_monitor in self.timed_link_monitors:
            obj, _ = SchedulerState.objects.get_or_create(
                name=link_monitor.thread_name,
                defaults={"schedule_type": "scheduler"},
            )
            link_monitor.last_run = obj.last_run
            link_monitor.pk = obj.name
            if start_threads and link_monitor.monitored_link.auto_start and link_monitor.monitored_link.enabled:
                link_monitor.start_running(timer=link_monitor.original_timer)

        self.download_progress_checker = DownloadProgressChecker(crawler_settings, web_queue=self.web_queue, timer=10)

        obj, _ = SchedulerState.objects.get_or_create(
            name=self.download_progress_checker.thread_name,
            defaults={"schedule_type": "scheduler"},
        )
        self.download_progress_checker.last_run = obj.last_run
        self.download_progress_checker.pk = obj.name
        if start_threads and crawler_settings.download_progress_checker_startup:
            self.download_progress_checker.start_running(timer=crawler_settings.download_progress_checker_cycle_timer)

        from core.workers.archive_download_queue import ArchiveDownloadQueue

        self.archive_download_queue = ArchiveDownloadQueue(
            crawler_settings,
            web_queue=self.web_queue,
            timer=crawler_settings.archive_download_queue_cycle_timer,
        )

        obj, _ = SchedulerState.objects.get_or_create(
            name=self.archive_download_queue.thread_name,
            defaults={"schedule_type": "scheduler"},
        )
        self.archive_download_queue.last_run = obj.last_run
        self.archive_download_queue.pk = obj.name
        if start_threads and crawler_settings.archive_download_queue_startup:
            self.archive_download_queue.start_running(timer=crawler_settings.archive_download_queue_cycle_timer)

    @property
    def is_worker_process(self) -> bool:
        return bool(self.crawler_settings and getattr(self.crawler_settings, "is_worker_process", False))

    def command_workers_to_stop(self, force_local: bool = False) -> None:
        if not (force_local or self.is_worker_process):
            return

        if self.timed_downloader:
            self.timed_downloader.stop_running(force_local=True)
        if self.download_progress_checker:
            self.download_progress_checker.stop_running(force_local=True)
        if self.archive_download_queue:
            self.archive_download_queue.stop_running(force_local=True)
        if self.timed_auto_wanted:
            self.timed_auto_wanted.stop_running(force_local=True)
        for provider_auto_updater in self.timed_auto_updaters:
            provider_auto_updater.stop_running(force_local=True)
        for link_monitor in self.timed_link_monitors:
            link_monitor.stop_running(force_local=True)

    def stop_workers_and_wait(self, force_local: bool = False) -> None:
        if not (force_local or self.is_worker_process):
            return

        self.command_workers_to_stop(force_local=force_local)

        while check_for_running_threads():
            sleep(1)
