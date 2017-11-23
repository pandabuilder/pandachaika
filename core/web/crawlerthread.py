# -*- coding: utf-8 -*-
import threading

import logging
import traceback

from core.web.crawler import WebCrawler


class CrawlerThread(threading.Thread):

    def __init__(self, logger, settings, argv):

        threading.Thread.__init__(self, name='webcrawler')
        self.logger = logger
        self.settings = settings
        self.argv = argv

    def run(self):
        try:
            web_crawler = WebCrawler(self.settings, self.logger)
            web_crawler.start_crawling(self.argv)
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())
