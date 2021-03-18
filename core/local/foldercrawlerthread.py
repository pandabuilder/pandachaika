import threading

import logging
import traceback
import typing

from core.local.foldercrawler import FolderCrawler

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)


class FolderCrawlerThread(threading.Thread):

    def __init__(self, settings: 'Settings', argv: list[str]) -> None:
        threading.Thread.__init__(self, name='foldercrawler')
        self.settings = settings
        self.argv = argv

    def run(self) -> None:
        try:
            folder_crawler = FolderCrawler(self.settings)
            folder_crawler.start_crawling(self.argv)
        except BaseException:
            logger.critical(traceback.format_exc())
