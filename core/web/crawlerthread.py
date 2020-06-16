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

    def __init__(self, settings: 'Settings', argv: typing.List[str]) -> None:

        threading.Thread.__init__(self, name='webcrawler')
        self.settings = settings
        self.argv = argv

    def run(self) -> None:
        try:
            web_crawler = WebCrawler(self.settings)
            web_crawler.start_crawling(self.argv)
        except BaseException:
            logger.critical(traceback.format_exc())
