import threading

import logging
import traceback

from core.local.foldercrawler import FolderCrawler


class FolderCrawlerThread(threading.Thread):

    def __init__(self, logger, settings, argv):
        threading.Thread.__init__(self, name='foldercrawler')
        self.logger = logger
        self.settings = settings
        self.argv = argv

    def run(self):
        try:
            folder_crawler = FolderCrawler(self.settings, self.logger)
            folder_crawler.start_crawling(self.argv)
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())
