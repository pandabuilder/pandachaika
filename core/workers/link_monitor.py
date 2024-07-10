import logging
from datetime import timedelta

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.base.setup import Settings
from core.workers.schedulers import BaseScheduler
from viewer.models import Archive, MonitoredLink

logger = logging.getLogger(__name__)


class LinkMonitor(BaseScheduler):

    thread_name = 'link_monitor'

    def __init__(
            self, settings: Settings, link_monitor_id: int, name: str, timer: timedelta, monitored_link: MonitoredLink,
            web_queue=None, pk=None
    ):
        self.thread_name = 'link_monitor_' + str(link_monitor_id)
        self.link_name = name
        self.monitored_link = monitored_link
        super().__init__(settings, web_queue, timer.total_seconds(), pk)

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return
            close_old_connections()
            try:
                monitored_link: MonitoredLink = MonitoredLink.objects.get(pk=self.monitored_link.pk)
            except MonitoredLink.DoesNotExist:
                logger.error("Did not find the expected MonitoredLink: {}, id: {}".format(self.link_name, self.monitored_link.pk))
                return
            self.monitored_link = monitored_link
            if not monitored_link.enabled:
                return
            logger.info("Starting link monitor for URL: {}".format(monitored_link.url))
            current_settings = Settings(load_from_config=self.settings.config)
            current_settings.silent_processing = True
            current_settings.replace_metadata = True
            current_settings.archive_origin = Archive.ORIGIN_WANTED_GALLERY
            arguments_to_crawler = [monitored_link.url, '-wanted']
            if monitored_link.provider:
                arguments_to_crawler.extend(['--include-providers', monitored_link.provider.slug])
            if monitored_link.use_limited_wanted_galleries:
                for wanted_gallery in monitored_link.limited_wanted_galleries.all():
                    arguments_to_crawler.extend(['--restrict-wanted-galleries', str(wanted_gallery.pk)])
            # TODO: This currently sets the proxy for both the queried page and the resulting downloads.
            # Could be beneficial to have a separate setting.
            if monitored_link.proxy:
                logger.info("Using proxy: {}".format(monitored_link.proxy))
                for provider_settings in current_settings.providers.values():
                    provider_settings.proxy = monitored_link.proxy
            if monitored_link.stop_page is not None:
                logger.info("Using stop page: {}".format(monitored_link.stop_page))
                for provider_settings in current_settings.providers.values():
                    provider_settings.stop_page_number = monitored_link.stop_page
            self.web_queue.enqueue_args_list(arguments_to_crawler, override_options=current_settings)

            self.timer = monitored_link.frequency.total_seconds()

            self.update_last_run(django_tz.now())
