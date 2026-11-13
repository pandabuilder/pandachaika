import logging
from datetime import datetime, timedelta

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.base.setup import Settings
from core.workers.schedulers import BaseScheduler
from viewer.models import Archive, MonitoredLink

logger = logging.getLogger(__name__)


class LinkMonitor(BaseScheduler):

    thread_name = "link_monitor"

    def __init__(
        self,
        settings: Settings,
        link_monitor_id: int,
        name: str,
        timer: timedelta,
        monitored_link: MonitoredLink,
        web_queue=None,
        pk=None,
    ):
        self.thread_name = "link_monitor_" + str(link_monitor_id)
        self.link_name = name
        self.monitored_link = monitored_link
        self.last_capture_run: datetime | None = monitored_link.last_capture_run
        self.last_process_run: datetime | None = monitored_link.last_process_run
        self._forced_cycle = False
        super().__init__(settings, web_queue, timer.total_seconds(), pk)

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer

    # Seconds to wait when a phase has never run. Zero means "run as soon as the scheduler starts".
    def _seconds_until(self, last: datetime | None, interval: timedelta) -> float:
        if last is None:
            return 0
        return max((last + interval - django_tz.now()).total_seconds(), 0)

    def wait_until_next_run(self) -> float:
        if self.force_run_once:
            self.force_run_once = False
            self._forced_cycle = True
            return 0
        link = self.monitored_link
        if not link.uses_separate_frequencies:
            return super().wait_until_next_run()
        return min(self.seconds_until_capture(), self.seconds_until_process())

    def seconds_until_capture(self) -> float:
        link = self.monitored_link
        return self._seconds_until(self.last_capture_run or self.last_run, link.capture_frequency or link.frequency)

    def seconds_until_process(self) -> float:
        link = self.monitored_link
        return self._seconds_until(self.last_process_run or self.last_run, link.frequency)

    def update_last_run(self, last_run: datetime) -> None:
        self.last_run = last_run
        if self.monitored_link.uses_separate_frequencies:
            next_in = min(self.seconds_until_capture(), self.seconds_until_process())
        else:
            next_in = self.timer
        try:
            from workers.models import SchedulerState

            SchedulerState.objects.filter(name=self.thread_name).update(
                last_run=last_run, next_run=last_run + timedelta(seconds=next_in)
            )
        except Exception:
            pass

    def _build_options(self, monitored_link: MonitoredLink) -> Settings:
        current_settings = Settings(load_from_config=self.settings.config)
        current_settings.silent_processing = True
        current_settings.replace_metadata = True
        current_settings.archive_origin = Archive.ORIGIN_WANTED_GALLERY
        if monitored_link.proxy:
            logger.info("Using proxy: {}".format(monitored_link.proxy))
            current_settings.proxy = monitored_link.proxy
            for provider_settings in current_settings.providers.values():
                provider_settings.proxy = monitored_link.proxy
        if monitored_link.stop_page is not None:
            logger.info("Using stop page: {}".format(monitored_link.stop_page))
            for provider_settings in current_settings.providers.values():
                provider_settings.stop_page_number = monitored_link.stop_page
        stop_on_seen_val = getattr(monitored_link, "stop_on_seen_link", None)
        if stop_on_seen_val == MonitoredLink.StopOnSeenChoices.ON or stop_on_seen_val in ("on", "ON", True):
            logger.info("Using stop on seen links: on for monitored link: {}".format(monitored_link.name))
            current_settings.stop_on_seen_links = True
            for provider_settings in current_settings.providers.values():
                provider_settings.stop_on_seen_links = True
        elif stop_on_seen_val == MonitoredLink.StopOnSeenChoices.OFF or stop_on_seen_val in ("off", "OFF", False):
            logger.info("Using stop on seen links: off for monitored link: {}".format(monitored_link.name))
            current_settings.stop_on_seen_links = False
            for provider_settings in current_settings.providers.values():
                provider_settings.stop_on_seen_links = False
        current_settings.monitored_link = monitored_link
        current_settings.already_seen_links_mode = monitored_link.already_seen_links_mode
        current_settings.how_long_after_already_seen = monitored_link.how_long_after_already_seen
        for provider_settings in current_settings.providers.values():
            provider_settings.monitored_link = monitored_link
            provider_settings.already_seen_links_mode = monitored_link.already_seen_links_mode
            provider_settings.how_long_after_already_seen = monitored_link.how_long_after_already_seen
        return current_settings

    def _enqueue_crawl(self, monitored_link: MonitoredLink, extra_args: list[str] | None = None) -> None:
        """Queue the crawler for this link. The extra args select the phase to run in separate frequencies mode:
        --capture-links-only only stores the new links, --skip-link-capture only processes the stored ones.
        With no extra args the crawler runs both in sequence, as it always did."""
        arguments_to_crawler = [monitored_link.url, "-wanted"]
        if monitored_link.provider:
            arguments_to_crawler.extend(["--include-providers", monitored_link.provider.slug])
        if monitored_link.use_limited_wanted_galleries:
            for wanted_gallery in monitored_link.limited_wanted_galleries.all():
                arguments_to_crawler.extend(["--restrict-wanted-galleries", str(wanted_gallery.pk)])
        if extra_args:
            arguments_to_crawler.extend(extra_args)
        self.web_queue.enqueue_args_list(
            arguments_to_crawler,
            override_options=self._build_options(monitored_link),
            priority=monitored_link.priority,
        )

    def _record_phase_runs(self, now: datetime, capture: bool, process: bool) -> None:
        fields: dict[str, datetime] = {}
        if capture:
            self.last_capture_run = now
            fields["last_capture_run"] = now
        if process:
            self.last_process_run = now
            fields["last_process_run"] = now
        if fields:
            # update() so we don't trigger MonitoredLink.save(), which would reload this very scheduler.
            MonitoredLink.objects.filter(pk=self.monitored_link.pk).update(**fields)

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return
            forced = self._forced_cycle
            self._forced_cycle = False
            close_old_connections()
            try:
                monitored_link: MonitoredLink = MonitoredLink.objects.get(pk=self.monitored_link.pk)
            except MonitoredLink.DoesNotExist:
                logger.error(
                    "Did not find the expected MonitoredLink: {}, id: {}".format(self.link_name, self.monitored_link.pk)
                )
                return
            self.monitored_link = monitored_link
            if not monitored_link.enabled:
                return
            now = django_tz.now()
            if monitored_link.uses_separate_frequencies:
                # Capture first, so a process run due at the same time already sees the new links.
                run_capture = forced or self.seconds_until_capture() < 1
                run_process = forced or self.seconds_until_process() < 1
                if run_capture:
                    logger.info("Starting link capture for URL: {}".format(monitored_link.url))
                    self._enqueue_crawl(monitored_link, ["--capture-links-only"])
                if run_process:
                    logger.info("Starting captured links processing for URL: {}".format(monitored_link.url))
                    self._enqueue_crawl(monitored_link, ["--skip-link-capture"])
                self._record_phase_runs(now, run_capture, run_process)
                self.timer = monitored_link.shortest_interval.total_seconds()
            else:
                logger.info("Starting link monitor for URL: {}".format(monitored_link.url))
                self._enqueue_crawl(monitored_link)
                self.timer = monitored_link.frequency.total_seconds()

            self.update_last_run(now)
            try:
                close_old_connections()
            except Exception:
                pass
