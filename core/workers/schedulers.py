import threading

import datetime
import traceback
from typing import Optional

import django.utils.timezone as django_tz
import logging

from core.base.setup import Settings
from viewer.models import Scheduler


class BaseScheduler(object):

    thread_name = 'task'

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer * 60 * 60

    def wait_until_next_run(self) -> float:
        if self.force_run_once:
            self.force_run_once: bool = False
            return 0
        seconds_until = (self.last_run + datetime.timedelta(seconds=int(self.timer)) - django_tz.now()).total_seconds()
        if seconds_until < 0:
            seconds_until = 0
        return seconds_until

    def update_last_run(self, last_run: datetime.datetime) -> None:
        schedule = Scheduler.objects.get(pk=self.pk)
        if schedule:
            schedule.last_run = last_run
            schedule.save()
        self.last_run: Optional[datetime.datetime] = last_run

    def job_container(self) -> None:
        try:
            self.job()
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())

    def job(self) -> None:
        raise NotImplementedError

    def __init__(self, settings: Settings, web_queue=None, crawler_logger=None, timer=1, pk=None) -> None:

        self.crawler_logger = crawler_logger
        self.settings = settings
        self.stop = threading.Event()
        self.web_queue = web_queue
        self.timer = self.timer_to_seconds(timer)
        self.job_thread: threading.Thread = None
        self.last_run: Optional[datetime.datetime] = None
        self.force_run_once: bool = False
        self.pk = pk

    def start_running(self, timer=None) -> None:

        if self.is_running():
            return

        schedule = Scheduler.objects.get(pk=self.pk)
        if schedule:
            self.last_run: Optional[datetime.datetime] = schedule.last_run

        if timer:
            self.timer = self.timer_to_seconds(timer)
        self.stop.clear()
        self.job_thread = threading.Thread(
            name=self.thread_name, target=self.job_container)
        self.job_thread.daemon = True
        self.job_thread.start()

    def is_running(self) -> bool:

        thread_list = threading.enumerate()
        for thread in thread_list:
            if thread.name == self.thread_name:
                return True

        return False

    def stop_running(self) -> None:

        self.stop.set()
