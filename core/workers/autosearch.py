import django.utils.timezone as django_tz
from django.db import connection

from core.base.setup import Settings
from core.workers.schedulers import BaseScheduler


class TimedAutoCrawler(BaseScheduler):

    thread_name = 'auto_search'

    @staticmethod
    def timer_to_seconds(timer):
        return timer * 60 * 60

    def job(self):
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return
            if self.settings.autochecker.enable:
                connection.close()
                self.crawler_logger.info("Starting timed auto search")
                current_settings = Settings(load_from_config=self.settings.config)
                current_settings.silent_processing = True
                current_settings.replace_metadata = True
                self.web_queue.enqueue_args_list(['-feed', '-wanted'], override_options=current_settings)

            self.update_last_run(django_tz.now())
