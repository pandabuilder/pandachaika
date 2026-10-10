import threading
import queue

import logging
import traceback
import typing

if typing.TYPE_CHECKING:
    from core.base.setup import Settings
    from viewer.models import Archive

logger = logging.getLogger(__name__)


class ArchiveWorker(object):
    """Worker for recalculating file info and generating thumbnails for archives."""

    def __init__(self, settings: typing.Union[int, "Settings", None] = None, worker_number: int = 4) -> None:
        if isinstance(settings, int):
            self.worker_number = settings + 1
            self.settings = None
        else:
            self.settings = settings
            self.worker_number = worker_number + 1

        if self.settings is None:
            try:
                from django.conf import settings as django_settings
                self.settings = getattr(django_settings, "CRAWLER_SETTINGS", None)
            except Exception:
                pass

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

    def recalc_all_file_info(self, archives: typing.Any = None, user: typing.Any = None, force_local: bool = False) -> None:
        """Enqueue or run file info recalculation for all or specific archives."""
        is_worker = force_local or (self.settings and getattr(self.settings, "is_worker_process", False))
        if is_worker:
            import os
            from viewer.models import Archive
            archive_qs = archives if archives is not None else Archive.objects.all()
            for archive in archive_qs:
                try:
                    if archive.zipped and os.path.exists(archive.zipped.path):
                        self.enqueue_archive(archive)
                except (ValueError, FileNotFoundError):
                    pass
            self.start_info_thread(force_local=True)
        else:
            from workers.client import enqueue
            archive_pks = [a.pk if hasattr(a, "pk") else a for a in archives] if archives is not None else []
            enqueue(
                args=archive_pks,
                task_type="fileinfo_worker",
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
            )
            logger.info("Enqueued fileinfo_worker task to worker process (archive count: %d)", len(archive_pks) if archive_pks else -1)

    def regenerate_all_thumbs(self, archives: typing.Any = None, user: typing.Any = None, force_local: bool = False) -> None:
        """Enqueue or run thumbnail regeneration for all or specific archives."""
        is_worker = force_local or (self.settings and getattr(self.settings, "is_worker_process", False))
        if is_worker:
            import os
            from viewer.models import Archive
            archive_qs = archives if archives is not None else Archive.objects.all()
            for archive in archive_qs:
                try:
                    if archive.zipped and os.path.exists(archive.zipped.path):
                        self.enqueue_archive(archive)
                except (ValueError, FileNotFoundError):
                    pass
            self.start_thumbs_thread(force_local=True)
        else:
            from workers.client import enqueue
            archive_pks = [a.pk if hasattr(a, "pk") else a for a in archives] if archives is not None else []
            enqueue(
                args=archive_pks,
                task_type="thumbnails_worker",
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
            )
            logger.info("Enqueued thumbnails_worker task to worker process (archive count: %d)", len(archive_pks) if archive_pks else -1)

    def start_info_thread(self, force_local: bool = False) -> None:
        is_worker = force_local or (self.settings and getattr(self.settings, "is_worker_process", False))
        if is_worker:
            thread_array = []

            for x in range(1, self.worker_number):
                file_info_thread = threading.Thread(name="fi_worker_" + str(x), target=self.file_info_worker)
                file_info_thread.daemon = True
                file_info_thread.start()
                thread_array.append(file_info_thread)

            for thread in thread_array:
                thread.join()

            logger.info("All file info threads finished")
        else:
            from workers.client import enqueue
            archive_pks = []
            while not self.web_queue.empty():
                try:
                    item = self.web_queue.get_nowait()
                    if hasattr(item, "pk"):
                        archive_pks.append(item.pk)
                    elif isinstance(item, (int, str)):
                        archive_pks.append(int(item))
                except Exception:
                    break
            enqueue(
                args=archive_pks,
                task_type="fileinfo_worker",
                override_options=self.settings,
                user=getattr(self.settings, "archive_user", None) if self.settings else None,
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
            )
            logger.info("Enqueued fileinfo_worker task to worker process (archive count: %d)", len(archive_pks) if archive_pks else -1)

    def start_thumbs_thread(self, force_local: bool = False) -> None:
        is_worker = force_local or (self.settings and getattr(self.settings, "is_worker_process", False))
        if is_worker:
            thread_array = []

            for x in range(1, self.worker_number):
                thumbnail_thread = threading.Thread(name="tn_worker_" + str(x), target=self.thumbnails_worker)
                thumbnail_thread.daemon = True
                thumbnail_thread.start()
                thread_array.append(thumbnail_thread)

            for thread in thread_array:
                thread.join()

            logger.info("All thumbnail threads finished")
        else:
            from workers.client import enqueue
            archive_pks = []
            while not self.web_queue.empty():
                try:
                    item = self.web_queue.get_nowait()
                    if hasattr(item, "pk"):
                        archive_pks.append(item.pk)
                    elif isinstance(item, (int, str)):
                        archive_pks.append(int(item))
                except Exception:
                    break
            enqueue(
                args=archive_pks,
                task_type="thumbnails_worker",
                override_options=self.settings,
                user=getattr(self.settings, "archive_user", None) if self.settings else None,
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
            )
            logger.info("Enqueued thumbnails_worker task to worker process (archive count: %d)", len(archive_pks) if archive_pks else -1)

    def enqueue_archive_method_call(self, archive: "Archive", method_name: str, args, kwargs) -> None:

        self.web_queue.put((archive, method_name, args, kwargs))

    def enqueue_archive(self, archive: "Archive") -> None:

        self.web_queue.put(archive)
