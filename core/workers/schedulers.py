import threading
import logging

import datetime
import traceback
from typing import Optional

import django.utils.timezone as django_tz

from core.base.setup import Settings
from viewer.models import Scheduler

logger = logging.getLogger(__name__)


class BaseScheduler(object):

    thread_name = "task"

    def __init__(self, settings: Settings, web_queue=None, timer=1, pk=None) -> None:
        self.settings = settings
        self.stop = threading.Event()
        self.web_queue = web_queue
        self.original_timer = timer
        self.timer = self.timer_to_seconds(timer)
        self.job_thread: Optional[threading.Thread] = None
        self.last_run: Optional[datetime.datetime] = None
        self.force_run_once: bool = False
        self.pk = pk

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 60 * 60

    def wait_until_next_run(self) -> float:
        if self.force_run_once:
            self.force_run_once = False
            return 0
        if self.last_run:
            seconds_until = (
                self.last_run + datetime.timedelta(seconds=int(self.timer)) - django_tz.now()
            ).total_seconds()
        else:
            seconds_until = 0
        if seconds_until < 0:
            seconds_until = 0
        return seconds_until

    def update_last_run(self, last_run: datetime.datetime) -> None:
        try:
            schedule = Scheduler.objects.get(pk=self.pk)
            if schedule:
                schedule.last_run = last_run
                schedule.save(update_fields=["last_run"])
        except Exception:
            pass
        self.last_run = last_run
        try:
            from workers.models import SchedulerState
            next_run = last_run + datetime.timedelta(seconds=self.timer)
            SchedulerState.objects.filter(name=self.thread_name).update(last_run=last_run, next_run=next_run)
        except Exception:
            pass

    def job_container(self) -> None:
        try:
            self.job()
        except BaseException:
            logger.critical(traceback.format_exc())

    def job(self) -> None:
        raise NotImplementedError

    def is_running_locally(self) -> bool:
        """Check if scheduler thread is alive in the current process."""
        for thread in threading.enumerate():
            if thread.name == self.thread_name and thread.is_alive() and not self.stop.is_set():
                return True
        return False

    def is_running(self) -> bool:
        """Check if scheduler is running locally or in a background worker process."""
        if self.is_running_locally():
            return True
        try:
            from workers.models import SchedulerState
            return SchedulerState.is_scheduler_running(self.thread_name)
        except Exception:
            return False

    def start_running(self, timer=None, force_local: bool = False) -> None:
        """Start running the scheduler. If in web process, requests the worker process to start it."""
        is_worker = force_local or getattr(self.settings, "is_worker_process", False)

        if is_worker:
            if self.is_running_locally():
                return

            try:
                schedule = Scheduler.objects.get(pk=self.pk)
                if schedule:
                    self.last_run = schedule.last_run
            except Exception:
                pass

            if timer:
                self.timer = self.timer_to_seconds(timer)
            self.stop.clear()
            self.job_thread = threading.Thread(name=self.thread_name, target=self.job_container)
            self.job_thread.daemon = True
            self.job_thread.start()

            try:
                from workers.models import SchedulerState
                SchedulerState.objects.update_or_create(
                    name=self.thread_name,
                    defaults={
                        "is_running": True,
                        "cycle_timer": self.timer,
                        "last_heartbeat": django_tz.now(),
                        "command_requested": "",
                    },
                )
            except Exception:
                pass
        else:
            try:
                from workers.models import SchedulerState
                timer_arg = str(timer) if timer else ""
                SchedulerState.request_command(self.thread_name, "start", timer_arg)
                logger.info("Requested worker process to start scheduler '%s'", self.thread_name)
            except Exception as e:
                logger.warning("Failed to dispatch scheduler start command: %s", e)

    def stop_running(self, force_local: bool = False) -> None:
        """Stop running the scheduler. If in web process, requests the worker process to stop it."""
        is_worker = force_local or getattr(self.settings, "is_worker_process", False)

        if is_worker:
            self.stop.set()
            try:
                from workers.models import SchedulerState
                SchedulerState.objects.filter(name=self.thread_name).update(is_running=False, command_requested="")
            except Exception:
                pass
        else:
            try:
                from workers.models import SchedulerState
                SchedulerState.request_command(self.thread_name, "stop")
                logger.info("Requested worker process to stop scheduler '%s'", self.thread_name)
            except Exception as e:
                logger.warning("Failed to dispatch scheduler stop command: %s", e)

    def force_run(self, force_local: bool = False) -> None:
        """Force immediate run of the scheduler."""
        is_worker = force_local or getattr(self.settings, "is_worker_process", False)

        if is_worker:
            self.stop_running(force_local=True)
            self.force_run_once = True
            self.start_running(force_local=True)
        else:
            try:
                from workers.models import SchedulerState
                SchedulerState.request_command(self.thread_name, "force_run")
                logger.info("Requested worker process to force-run scheduler '%s'", self.thread_name)
            except Exception as e:
                logger.warning("Failed to dispatch scheduler force_run command: %s", e)

    @property
    def current_last_run(self) -> Optional[datetime.datetime]:
        try:
            from workers.models import SchedulerState
            state = SchedulerState.objects.filter(name=self.thread_name).first()
            if state and state.last_run:
                return state.last_run
        except Exception:
            pass
        try:
            if self.pk:
                return Scheduler.objects.filter(pk=self.pk).values_list("last_run", flat=True).first()
            elif self.thread_name:
                return Scheduler.objects.filter(name=self.thread_name).values_list("last_run", flat=True).first()
        except Exception:
            pass
        return self.last_run

