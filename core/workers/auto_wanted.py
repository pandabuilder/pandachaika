import threading
import logging

import django.utils.timezone as django_tz
from django.db import connection

from core.workers.schedulers import BaseScheduler
from viewer.models import Attribute

logger = logging.getLogger(__name__)


class TimedAutoWanted(BaseScheduler):

    thread_name = 'auto_wanted'

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 60 * 60

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return
            if self.settings.auto_wanted.enable:
                logger.info("Starting timed auto wanted.")
                connection.close()
                for provider_name in self.settings.auto_wanted.providers:

                    attrs = Attribute.objects.filter(provider__slug=provider_name)

                    for count, wanted_generator in enumerate(self.settings.provider_context.get_wanted_generators(provider_name)):
                        # wanted_generator(self.settings, self.crawler_logger, attrs)
                        wanted_generator_thread = threading.Thread(
                            name="{}-{}-{}".format(self.thread_name, provider_name, count),
                            target=wanted_generator,
                            args=(self.settings, attrs)
                        )
                        wanted_generator_thread.daemon = True
                        wanted_generator_thread.start()

            self.update_last_run(django_tz.now())
