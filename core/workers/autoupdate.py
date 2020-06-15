import logging
from datetime import timedelta

import django.utils.timezone as django_tz
from django.db import connection

from core.base.setup import Settings
from core.workers.schedulers import BaseScheduler
from viewer.models import Gallery

logger = logging.getLogger(__name__)


# We could call fetch_multiple_gallery_data directly, but we want to go through the queue so we don't go over limits
# with the providers
class TimedAutoUpdater(BaseScheduler):

    thread_name = 'auto_updater'

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 24 * 60 * 60

    def job(self) -> None:
        while not self.stop.is_set():

            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            if self.settings.autoupdater.enable:
                current_settings = Settings(load_from_config=self.settings.config)
                current_settings.keep_dl_type = True
                current_settings.silent_processing = True
                current_settings.config['allowed']['replace_metadata'] = 'yes'

                connection.close()

                start_date = django_tz.now() - timedelta(seconds=int(self.timer)) - timedelta(days=self.settings.autoupdater.buffer_back)
                end_date = django_tz.now() - timedelta(days=self.settings.autoupdater.buffer_after)
                to_update_providers = current_settings.autoupdater.providers

                galleries = Gallery.objects.eligible_for_use(
                    posted__gte=start_date,
                    posted__lte=end_date,
                    provider__in=to_update_providers
                )

                if not galleries:
                    logger.info(
                        "No galleries posted from {} to {} need updating. Providers: {}".format(
                            start_date,
                            end_date,
                            ", ".join(to_update_providers)
                        )
                    )
                else:
                    # Leave only info downloaders, then leave only enabled auto updated providers
                    downloaders = current_settings.provider_context.get_downloaders_name_priority(current_settings, filter_name='info')
                    downloaders_names = [x[0] for x in downloaders if x[0].replace("_info", "") in to_update_providers]

                    current_settings.allow_downloaders_only(downloaders_names, True, True, True)

                    url_list = [x.get_link() for x in galleries]

                    logger.info(
                        "Starting timed auto updater, updating {} galleries "
                        "posted from {} to {}. Providers: {}".format(
                            len(url_list),
                            start_date,
                            end_date,
                            ", ".join(to_update_providers)
                        )
                    )

                    url_list.append('--update-mode')

                    self.web_queue.enqueue_args_list(url_list, override_options=current_settings)

            self.update_last_run(django_tz.now())
