import datetime
import logging

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.workers.schedulers import BaseScheduler

logger = logging.getLogger(__name__)


class TimedAutoWanted(BaseScheduler):
    """Runs the active auto wanted sources (``AutoWantedSource``) when they're due, see ``viewer.services.auto_wanted``.

    Each source runs every ``frequency``, or every ``timer`` (``auto_wanted.cycle_timer`` hours) without one. The
    scheduler wakes up when the next source is due, and at least every ``CHECK_INTERVAL`` to see new or changed
    sources. A forced run (workers dashboard) runs every active source.
    """

    thread_name = "auto_wanted"
    CHECK_INTERVAL = 15 * 60
    # Floor between checks, so a source still running (due until its run ends) doesn't make the loop spin.
    MIN_WAIT = 60

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._forced_cycle = False

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 60 * 60

    def default_period(self) -> datetime.timedelta:
        return datetime.timedelta(seconds=self.timer)

    def seconds_until_due(self) -> float:
        """Seconds until the next active source is due, between MIN_WAIT and CHECK_INTERVAL."""
        if not self.settings.auto_wanted.enable:
            return self.CHECK_INTERVAL
        try:
            from viewer.services.auto_wanted import seconds_until_next_due

            seconds = seconds_until_next_due(self.default_period())
        except Exception:
            logger.exception("Could not check when the auto wanted sources are due")
            seconds = None
        if seconds is None:
            return self.CHECK_INTERVAL
        return min(max(seconds, self.MIN_WAIT), self.CHECK_INTERVAL)

    def wait_until_next_run(self) -> float:
        if self.force_run_once:
            self.force_run_once = False
            self._forced_cycle = True
            return 0
        return self.seconds_until_due()

    def update_last_run(self, last_run: datetime.datetime, persist: bool = True) -> None:
        self.last_run = last_run
        if not persist:
            return
        try:
            from workers.models import SchedulerState

            next_run = last_run + datetime.timedelta(seconds=self.seconds_until_due())
            SchedulerState.objects.filter(name=self.thread_name).update(last_run=last_run, next_run=next_run)
        except Exception:
            pass

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            forced = self._forced_cycle
            self._forced_cycle = False
            if self.stop.wait(timeout=seconds_to_wait):
                return
            if self.settings.auto_wanted.enable:
                close_old_connections()
                try:
                    from viewer.services.auto_wanted import run_active_sources_in_threads

                    threads = run_active_sources_in_threads(
                        self.settings,
                        default=self.default_period(),
                        only_due=not forced,
                        thread_name=self.thread_name,
                    )
                    if threads:
                        logger.info("Timed auto wanted: started the due sources of %d provider(s).", len(threads))
                except Exception:
                    logger.exception("Could not start the auto wanted sources")

            self.update_last_run(django_tz.now())
            try:
                close_old_connections()
            except Exception:
                pass
