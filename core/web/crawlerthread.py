# -*- coding: utf-8 -*-
import threading

import logging
import traceback
import typing

from core.base.types import OptionalLogger
from core.web.crawler import WebCrawler

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class CrawlerThread(threading.Thread):

    def __init__(self, logger: OptionalLogger, settings: 'Settings', argv: typing.List[str]) -> None:

        threading.Thread.__init__(self, name='webcrawler')
        self.logger = logger
        self.settings = settings
        self.argv = argv

    def run(self) -> None:
        try:
            web_crawler = WebCrawler(self.settings, self.logger)
            web_crawler.start_crawling(self.argv)
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())
