import django.utils.timezone as django_tz
from django.db import connection

from core.base.setup import Settings
from core.base.types import OptionalLogger
from core.workers.schedulers import BaseScheduler


class ProviderTimedAutoCrawler(BaseScheduler):

    thread_name = 'auto_search'

    def __init__(self, settings: Settings, provider_name: str, web_queue=None, crawler_logger: OptionalLogger = None, timer=1, pk=None):
        self.provider_name = provider_name
        self.thread_name = 'auto_search_' + provider_name
        super().__init__(settings, web_queue, crawler_logger, timer, pk)

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 60 * 60

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return
            if self.settings.providers[self.provider_name].autochecker_enable:
                connection.close()
                self.crawler_logger.info("Starting timed auto search for provider: {}".format(self.provider_name))
                current_settings = Settings(load_from_config=self.settings.config)
                current_settings.silent_processing = True
                current_settings.replace_metadata = True
                self.web_queue.enqueue_args_list(['-feed', '-wanted', '--include-providers', self.provider_name], override_options=current_settings)

            self.update_last_run(django_tz.now())
