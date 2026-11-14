import logging

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.workers.schedulers import BaseScheduler

logger = logging.getLogger(__name__)


class TimedAdminReporter(BaseScheduler):
    """Periodically emails the admin report to the staff users whose own report schedule is due."""

    thread_name = "timed_admin_reporter"

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        """The timer is in minutes (``admin_report.check_interval_minutes``)."""
        return timer * 60

    def job(self) -> None:
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            try:
                close_old_connections()
                from viewer.services.admin_report_delivery import send_due_admin_reports

                results = send_due_admin_reports(self.settings)
                if results:
                    logger.info(
                        "Admin reports processed: %s", ", ".join(f"{r.status} ({r.message})" for r in results)
                    )
            except Exception as e:
                logger.critical("Error while sending admin reports: %s", e)
            finally:
                # Always record the run, so a failing run doesn't make the scheduler spin.
                self.update_last_run(django_tz.now())
                try:
                    from django.db import connections

                    connections.close_all()
                except Exception:
                    pass
