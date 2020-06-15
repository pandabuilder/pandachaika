import threading
import traceback
from collections import deque

import logging
import typing
from typing import Iterable, Optional, Callable, List

from core.base.setup import Settings
from core.base.types import QueueItem
from core.web.crawlerthread import WebCrawler

if typing.TYPE_CHECKING:
    from viewer.models import Gallery, Archive

logger = logging.getLogger(__name__)


class WebQueue(object):

    """Queue handler for downloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue: deque = deque()
        self.web_queue_thread: Optional[threading.Thread] = None
        self.current_processing_items: List[QueueItem] = []
        self.thread_name = 'web_queue'

    def web_worker(self) -> None:
        while True:
            try:
                item = self.queue.popleft()
            except IndexError:
                return
            try:
                self.current_processing_items = item
                web_crawler = WebCrawler(self.settings)
                web_crawler.start_crawling(
                    item['args'],
                    override_options=item['override_options'],
                    archive_callback=item.get('archive_callback', None),
                    gallery_callback=item.get('gallery_callback', None),
                    use_argparser=item.get('use_argparser', True)
                )
                self.current_processing_items = []
            except BaseException:
                logger.error(traceback.format_exc())

    def start_running(self) -> None:

        if self.is_running():
            return
        if self.current_processing_items:
            self.queue.append(self.current_processing_items)
            self.current_processing_items = []
        self.web_queue_thread = threading.Thread(
            name=self.thread_name, target=self.web_worker)
        self.web_queue_thread.daemon = True
        self.web_queue_thread.start()

    def is_running(self) -> bool:

        thread_list = threading.enumerate()
        for thread in thread_list:
            if thread.name == self.thread_name:
                return True

        return False

    def queue_size(self) -> int:

        return len(self.queue)

    def remove_by_index(self, index: int) -> bool:

        try:
            del self.queue[index]
            return True
        except IndexError:
            return False

    def enqueue_args(self, args: str) -> None:

        self.queue.append({'args': args.split(), 'override_options': None})
        self.start_running()

    def enqueue_args_list(
            self, args: Iterable[str],
            override_options: Settings = None,
            archive_callback: Callable[[Optional['Archive'], Optional[str], str], None] = None,
            gallery_callback: Callable[[Optional['Gallery'], Optional[str], str], None] = None,
            use_argparser: bool = True
    ) -> None:

        self.queue.append(
            {
                'args': args,
                'override_options': override_options,
                'archive_callback': archive_callback,
                'gallery_callback': gallery_callback,
                'use_argparser': use_argparser,
            }
        )
        self.start_running()
