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

    def __init__(self, settings: "int | Settings | None" = None, worker_number: int = 4) -> None:
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
        if force_local:
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
        if force_local:
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

    def extract_archives(
        self,
        archives: typing.Any = None,
        resized: bool = False,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run image extraction for one or more archives."""
        if force_local:
            from viewer.models import Archive
            from viewer.utils.actions import event_log

            if archives is None:
                return
            archive_qs: typing.Iterable[Archive]
            if isinstance(archives, (list, tuple, set)) and all(isinstance(x, (int, str)) for x in archives):
                archive_qs = Archive.objects.filter(pk__in=[int(x) for x in archives])
            elif hasattr(archives, "filter"):
                archive_qs = archives
            else:
                archive_qs = list(archives)

            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None
            effective_user = user or (getattr(self.settings, "archive_user", None) if self.settings else None)
            action = (getattr(self.settings, "event_action", None) if self.settings else None) or "EXPAND_ARCHIVE"
            reason = (getattr(self.settings, "archive_reason", None) if self.settings else None) or None

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    if archive.extracted:
                        continue
                    if activity_cb:
                        setattr(archive, "_activity_callback", activity_cb)
                    res = archive.extract(resized=resized)
                    if (res or archive.extracted) and effective_user:
                        event_log(
                            effective_user,
                            action,
                            reason=reason,
                            content_object=archive,
                            result="success",
                        )
                except Exception:
                    logger.critical(
                        "Error extracting archive #%s:\n%s",
                        getattr(archive, "pk", "?"),
                        traceback.format_exc(),
                    )
        else:
            from workers.client import enqueue

            archive_pks = [a.pk if hasattr(a, "pk") else int(a) for a in archives] if archives is not None else []
            if not archive_pks:
                return
            enqueue(
                args=archive_pks,
                task_type="extract_worker",
                override_options={"resized": bool(resized)},
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
                event_action="EXPAND_ARCHIVE",
            )
            logger.info(
                "Enqueued extract_worker task to worker process (archive count: %d, resized: %s)",
                len(archive_pks),
                resized,
            )

    def publish_archives(
        self,
        archives: typing.Any = None,
        reason: str | None = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run archive publishing (zip test, image set, thumbnails, SHA1, public state) for one or more archives."""
        if force_local:
            import zipfile
            from viewer.models import Archive
            from viewer.utils.actions import event_log

            if archives is None:
                return
            archive_qs: typing.Iterable[Archive]
            if isinstance(archives, (list, tuple, set)) and all(isinstance(x, (int, str)) for x in archives):
                archive_qs = Archive.objects.filter(pk__in=[int(x) for x in archives])
            elif hasattr(archives, "filter"):
                archive_qs = archives
            else:
                archive_qs = list(archives)

            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None
            effective_user = user or (getattr(self.settings, "archive_user", None) if self.settings else None)
            action = (getattr(self.settings, "event_action", None) if self.settings else None) or "PUBLISH_ARCHIVE"
            effective_reason = (
                reason
                if reason is not None
                else (getattr(self.settings, "archive_reason", None) if self.settings else None)
            ) or ""

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    if archive.public:
                        continue
                    if activity_cb:
                        setattr(archive, "_activity_callback", activity_cb)
                    try:
                        archive.test_zip_file()
                    except (FileNotFoundError, zipfile.BadZipFile) as e:
                        logger.error("For Archive: %s, %s", archive.get_absolute_url(), str(e))
                        continue
                    if activity_cb:
                        activity_cb()
                    res = archive.set_public(reason=effective_reason, skip_zip_check=True)
                    logger.info("Setting public status to public for Archive: %s", archive.get_absolute_url())
                    if (res or archive.public) and effective_user:
                        event_log(
                            effective_user,
                            action,
                            reason=effective_reason or None,
                            content_object=archive,
                            result="published",
                        )
                except Exception:
                    logger.critical(
                        "Error publishing archive #%s:\n%s",
                        getattr(archive, "pk", "?"),
                        traceback.format_exc(),
                    )
        else:
            from workers.client import enqueue

            archive_pks = [a.pk if hasattr(a, "pk") else int(a) for a in archives] if archives is not None else []
            if not archive_pks:
                return
            effective_reason = (
                reason
                if reason is not None
                else (getattr(self.settings, "archive_reason", "") if self.settings else "")
            ) or ""
            enqueue(
                args=archive_pks,
                task_type="publish_worker",
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                reason=effective_reason,
                event_action="PUBLISH_ARCHIVE",
            )
            logger.info(
                "Enqueued publish_worker task to worker process (archive count: %d)",
                len(archive_pks),
            )

    def start_info_thread(self, force_local: bool = False) -> None:
        if force_local:
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
        if force_local:
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

    def recalc_fileinfo_batch(self, archives: typing.Sequence["Archive"]) -> None:
        """Recalculate file info and CRC32 for a sequence of archives offloading to the process pool."""
        import os

        for archive in archives:
            try:
                if archive.zipped and os.path.isfile(archive.zipped.path):
                    archive.recalc_fileinfo(use_pool=True)
            except Exception as e:
                logger.error("Error in recalc_fileinfo_batch for archive %s: %s", getattr(archive, "pk", "?"), e)

    def regenerate_thumbs_batch(self, archives: typing.Sequence["Archive"]) -> None:
        """Regenerate thumbnails for a sequence of archives offloading to the process pool."""
        import os

        for archive in archives:
            try:
                if archive.zipped and os.path.isfile(archive.zipped.path):
                    archive.generate_thumbnails(use_pool=True)
            except Exception as e:
                logger.error("Error in regenerate_thumbs_batch for archive %s: %s", getattr(archive, "pk", "?"), e)
