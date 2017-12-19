import threading

import logging
import traceback
import typing
from typing import List

from core.base.types import OptionalLogger
from core.local.foldercrawler import FolderCrawler

if typing.TYPE_CHECKING:
    from core.base.setup import Settings


class FolderCrawlerThread(threading.Thread):

    def __init__(self, logger: OptionalLogger, settings: 'Settings', argv: List[str]) -> None:
        threading.Thread.__init__(self, name='foldercrawler')
        self.logger = logger
        self.settings = settings
        self.argv = argv

    def run(self) -> None:
        try:
            folder_crawler = FolderCrawler(self.settings, self.logger)
            folder_crawler.start_crawling(self.argv)
        except BaseException:
            thread_logger = logging.getLogger('viewer.threads')
            thread_logger.error(traceback.format_exc())
