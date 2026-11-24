import datetime
import logging

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.workers.schedulers import BaseScheduler

logger = logging.getLogger(__name__)


# The updates themselves run in the gallery_updater queue of the workers, which uses the rate limited provider requests.
class TimedAutoUpdater(BaseScheduler):
    """Runs the active auto updater schedules (``AutoUpdaterSchedule``, one per provider) when they're due.

    Each schedule queues, every ``queue_every``, the galleries that reached one of its update ages since its previous
    run, see ``viewer.services.auto_updater``. The scheduler wakes up when the next schedule is due, and at least every
    ``timer`` (``CHECK_INTERVAL`` by default) to see new or changed schedules. A forced run (workers dashboard) runs
    every active schedule.
    """

    thread_name = "auto_updater"
    CHECK_INTERVAL = 15 * 60
    MIN_WAIT = 60

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("timer", self.CHECK_INTERVAL / 60)
        super().__init__(*args, **kwargs)
        self._forced_cycle = False
        self._warned_no_schedules = False

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        # In minutes: the longest wait between two checks.
        return timer * 60

    def check_interval(self) -> float:
        return self.timer if self.timer and self.timer > 0 else self.CHECK_INTERVAL

    def seconds_until_due(self) -> float:
        """Seconds until the next active schedule is due, between MIN_WAIT and the check interval."""
        interval = self.check_interval()
        if not self.settings.autoupdater.enable:
            return interval
        try:
            from viewer.services.auto_updater import seconds_until_next_due

            seconds = seconds_until_next_due(providers=self.settings.providers.keys())
        except Exception:
            logger.exception("Could not check when the auto updater schedules are due")
            seconds = None
        if seconds is None:
            return interval
        return min(max(seconds, self.MIN_WAIT), interval)

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

    def warn_without_schedules(self) -> None:
        if self._warned_no_schedules:
            return
        from viewer.models import AutoUpdaterSchedule

        if not AutoUpdaterSchedule.objects.exists() and self.settings.autoupdater.providers:
            self._warned_no_schedules = True
            logger.warning(
                "Auto updater: no schedules yet. The autoupdater.providers setting (%s) isn't read anymore, import it "
                "as schedules on the Auto updater page.",
                ", ".join(self.settings.autoupdater.providers),
            )

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            forced = self._forced_cycle
            self._forced_cycle = False
            if self.stop.wait(timeout=seconds_to_wait):
                return
            if self.settings.autoupdater.enable:
                close_old_connections()
                try:
                    from viewer.services.auto_updater import run_due

                    self.warn_without_schedules()
                    run_due(self.settings, force=forced)
                except Exception:
                    logger.exception("Auto updater run failed")

            self.update_last_run(django_tz.now())
            try:
                close_old_connections()
            except Exception:
                pass
