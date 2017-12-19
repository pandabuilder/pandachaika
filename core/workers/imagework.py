import threading
import queue

import logging
import traceback
import typing

from core.base.types import OptionalLogger

if typing.TYPE_CHECKING:
    from viewer.models import Archive


class ImageWorker(object):

    """description of class"""

    def thumbnails_worker(self) -> None:

        while True:
            try:
                item = self.web_queue.get_nowait()
            except queue.Empty:
                return
            try:
                item.generate_thumbnails()
                self.web_queue.task_done()
            except BaseException:
                thread_logger = logging.getLogger('viewer.threads')
                thread_logger.error(traceback.format_exc())

    def file_info_worker(self) -> None:

        while True:
            try:
                item = self.web_queue.get_nowait()
            except queue.Empty:
                return
            try:
                item.recalc_fileinfo()
                self.web_queue.task_done()
            except BaseException:
                thread_logger = logging.getLogger('viewer.threads')
                thread_logger.error(traceback.format_exc())

    def __init__(self, crawler_logger: OptionalLogger, worker_number: int) -> None:

        self.worker_number = worker_number + 1
        self.crawler_logger = crawler_logger
        self.web_queue: queue.Queue = queue.Queue()

    def start_info_thread(self) -> None:

        thread_array = []

        for x in range(1, self.worker_number):
            file_info_thread = threading.Thread(
                name='fi_worker_' + str(x), target=self.file_info_worker)
            file_info_thread.daemon = True
            file_info_thread.start()
            thread_array.append(file_info_thread)

        for thread in thread_array:
            thread.join()

        self.crawler_logger.info("All file info threads finished")

    def start_thumbs_thread(self) -> None:

        thread_array = []

        for x in range(1, self.worker_number):
            thumbnail_thread = threading.Thread(
                name='tn_worker_' + str(x), target=self.thumbnails_worker)
            thumbnail_thread.daemon = True
            thumbnail_thread.start()
            thread_array.append(thumbnail_thread)

        for thread in thread_array:
            thread.join()

        self.crawler_logger.info("All thumbnail threads finished")

    def enqueue_archive(self, archive: 'Archive') -> None:

        self.web_queue.put(archive)
