# -*- coding: utf-8 -*-
import threading

import logging
import traceback
import typing

from core.web.crawler import WebCrawler

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


class CrawlerThread(threading.Thread):

    def __init__(self, settings: "Settings", argv: typing.Optional[list[str]] = None) -> None:
        super().__init__(name="webcrawler")
        self.settings = settings
        self.argv = argv if argv is not None else []

    def start(self, force_local: bool = False) -> None:
        is_worker = force_local or getattr(self.settings, "is_worker_process", False)
        if is_worker:
            super().start()
        else:
            from workers.client import enqueue
            enqueue(
                args=self.argv,
                task_type="web_crawler",
                override_options=self.settings,
                user=getattr(self.settings, "archive_user", None),
                reason=getattr(self.settings, "archive_reason", ""),
                event_action=getattr(self.settings, "event_action", ""),
            )
            logger.info("Enqueued web_crawler task to worker process with args: %s", self.argv)

    def run(self) -> None:
        try:
            web_crawler = WebCrawler(self.settings)
            web_crawler.start_crawling(self.argv)
        except BaseException:
            logger.critical(traceback.format_exc())

    def crawl(self, argv: list[str], override_options: typing.Any = None, user: typing.Any = None) -> typing.Any:
        """Enqueue or run a crawler task."""
        self.argv = argv
        if override_options:
            self.settings = override_options
        if user:
            self.settings.archive_user = user
        self.start()
