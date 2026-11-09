import logging

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.workers.schedulers import BaseScheduler

logger = logging.getLogger(__name__)


class TimedDatabasePruner(BaseScheduler):
    """Periodic scheduler that prunes old database entities according to configured retention horizons."""

    thread_name = "timed_database_pruner"

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        """Convert hours to seconds for scheduler interval."""
        return timer * 60 * 60

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            retention_cfg = getattr(self.settings, "retention", None)
            if retention_cfg and retention_cfg.enabled:
                logger.info("Starting scheduled database pruning run.")
                close_old_connections()
                try:
                    from viewer.services.retention import prune_database

                    res = prune_database(
                        settings_obj=self.settings,
                        entity="all",
                        dry_run=False,
                    )
                    logger.info("Scheduled database pruning finished: %s", res)
                except Exception as e:
                    logger.critical("Error during scheduled database pruning: %s", e)
                finally:
                    self.update_last_run(django_tz.now())
                    try:
                        from django.db import connections

                        connections.close_all()
                    except Exception:
                        pass
