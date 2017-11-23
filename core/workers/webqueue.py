import threading
import traceback
from collections import deque

import logging

from core.base.setup import Settings
from core.web.crawlerthread import WebCrawler


class WebQueue(object):

    """Queue handler for downloads."""

    def web_worker(self):
        while True:
            try:
                item = self.queue.popleft()
            except IndexError:
                return
            try:
                self.current_processing_items = item
                web_crawler = WebCrawler(self.settings, self.crawler_logger)
                web_crawler.start_crawling(
                    item['args'],
                    override_options=item['override_options']
                )
                self.current_processing_items = []
            except BaseException:
                thread_logger = logging.getLogger('viewer.threads')
                thread_logger.error(traceback.format_exc())

    def __init__(self, settings: Settings, crawler_logger=None) -> None:
        self.settings = settings
        self.crawler_logger = crawler_logger
        self.queue: deque = deque()
        self.web_queue_thread = None
        self.current_processing_items = []
        self.thread_name = 'web_queue'

    def start_running(self):

        if self.is_running():
            return
        if self.current_processing_items:
            self.queue.append(self.current_processing_items)
            self.current_processing_items = []
        self.web_queue_thread = threading.Thread(
            name=self.thread_name, target=self.web_worker)
        self.web_queue_thread.daemon = True
        self.web_queue_thread.start()

    def is_running(self):

        thread_list = threading.enumerate()
        for thread in thread_list:
            if thread.name == self.thread_name:
                return True

        return False

    def queue_size(self):

        return len(self.queue)

    def remove_by_index(self, index):

        try:
            del self.queue[index]
            return True
        except IndexError:
            return False

    def enqueue_args(self, args):

        self.queue.append({'args': args.split(), 'override_options': None})
        self.start_running()

    def enqueue_args_list(self, args, override_options=None):

        self.queue.append({'args': args, 'override_options': override_options})
        self.start_running()
