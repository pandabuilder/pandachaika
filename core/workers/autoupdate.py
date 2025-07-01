import logging
from datetime import timedelta

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.base.setup import Settings
from core.workers.schedulers import BaseScheduler
from viewer.models import Gallery

logger = logging.getLogger(__name__)


# We could call fetch_multiple_gallery_data directly, but we want to go through the queue so we don't go over limits
# with the providers
class ProviderTimedAutoUpdater(BaseScheduler):

    thread_name = "auto_updater"

    def __init__(self, settings: Settings, provider_name: str, web_queue=None, timer=1, pk=None):
        self.provider_name = provider_name
        self.thread_name = "auto_updater_" + provider_name
        super().__init__(settings, web_queue, timer, pk)

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 24 * 60 * 60

    def job(self) -> None:
        while not self.stop.is_set():

            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            if self.settings.providers[self.provider_name].autoupdater_enable:
                current_settings = Settings(load_from_config=self.settings.config)
                current_settings.keep_dl_type = True
                current_settings.silent_processing = True
                current_settings.config["allowed"]["replace_metadata"] = "yes"

                close_old_connections()

                start_date = (
                    django_tz.now()
                    - timedelta(seconds=int(self.timer))
                    - timedelta(days=self.settings.providers[self.provider_name].autoupdater_buffer_back)
                )
                end_date = django_tz.now() - timedelta(
                    days=self.settings.providers[self.provider_name].autoupdater_buffer_after
                )

                galleries = Gallery.objects.eligible_for_use(
                    posted__gte=start_date, posted__lte=end_date, provider=self.provider_name
                )

                if not galleries:
                    logger.info(
                        "No galleries posted from {} to {} need updating. Provider: {}".format(
                            start_date, end_date, ", ".join(self.provider_name)
                        )
                    )
                else:
                    # Leave only info downloaders, then leave only enabled auto updated providers
                    downloaders = current_settings.provider_context.get_downloaders_name_priority(
                        current_settings, filter_type="info"
                    )
                    downloaders_names = [x[0] for x in downloaders if x[0].replace("_info", "") == self.provider_name]

                    current_settings.allow_downloaders_only(downloaders_names, True, True, True)

                    url_list = [x.get_link() for x in galleries]

                    logger.info(
                        "Starting timed auto updater, updating {} galleries "
                        "posted from {} to {}. Provider: {}".format(
                            len(url_list), start_date, end_date, self.provider_name
                        )
                    )

                    url_list.append("--update-mode")

                    self.web_queue.enqueue_args_list(url_list, override_options=current_settings)

            self.update_last_run(django_tz.now())
