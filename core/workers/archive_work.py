import threading
import queue

import logging
import traceback
import typing

if typing.TYPE_CHECKING:
    from viewer.models import Archive

logger = logging.getLogger(__name__)


class ArchiveWorker(object):
    """description of class"""

    def __init__(self, worker_number: int) -> None:

        self.worker_number = worker_number + 1
        self.web_queue: queue.Queue = queue.Queue()

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
                logger.critical(traceback.format_exc())

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
                logger.critical(traceback.format_exc())

    def generic_archive_method_worker(self) -> None:

        while True:
            try:
                item = self.web_queue.get_nowait()
            except queue.Empty:
                return
            try:
                archive, method_name, args, kwargs = item
                method = getattr(archive, method_name)
                method(*args, **kwargs)
                self.web_queue.task_done()
            except BaseException:
                logger.critical(traceback.format_exc())

    def generic_archive_method_thread(self) -> None:

        thread_array = []

        for x in range(1, self.worker_number):
            generic_archive_thread = threading.Thread(
                name="generic_archive_worker_" + str(x), target=self.generic_archive_method_worker
            )
            generic_archive_thread.daemon = True
            generic_archive_thread.start()
            thread_array.append(generic_archive_thread)

        for thread in thread_array:
            thread.join()

        logger.info("All generic threads finished")

    def start_info_thread(self) -> None:

        thread_array = []

        for x in range(1, self.worker_number):
            file_info_thread = threading.Thread(name="fi_worker_" + str(x), target=self.file_info_worker)
            file_info_thread.daemon = True
            file_info_thread.start()
            thread_array.append(file_info_thread)

        for thread in thread_array:
            thread.join()

        logger.info("All file info threads finished")

    def start_thumbs_thread(self) -> None:

        thread_array = []

        for x in range(1, self.worker_number):
            thumbnail_thread = threading.Thread(name="tn_worker_" + str(x), target=self.thumbnails_worker)
            thumbnail_thread.daemon = True
            thumbnail_thread.start()
            thread_array.append(thumbnail_thread)

        for thread in thread_array:
            thread.join()

        logger.info("All thumbnail threads finished")

    def enqueue_archive_method_call(self, archive: "Archive", method_name: str, args, kwargs) -> None:

        self.web_queue.put((archive, method_name, args, kwargs))

    def enqueue_archive(self, archive: "Archive") -> None:

        self.web_queue.put(archive)
