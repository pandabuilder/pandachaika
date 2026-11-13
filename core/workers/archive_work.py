import threading
import queue

import logging
import traceback
import typing

from core.base.cancellation import TaskCancelledError

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

    def recalc_all_file_info(
        self,
        archives: typing.Any = None,
        user: typing.Any = None,
        force_local: bool = False,
        full: bool = False,
    ) -> None:
        """Enqueue or run file info recalculation for all or specific archives."""
        if force_local:
            import os
            import zipfile
            from viewer.models import Archive
            archive_qs: typing.Iterable[Archive]
            if isinstance(archives, (list, tuple, set)) and all(isinstance(x, (int, str)) for x in archives):
                archive_qs = Archive.objects.filter(pk__in=[int(x) for x in archives])
            elif hasattr(archives, "filter"):
                archive_qs = archives
            elif archives is not None:
                archive_qs = list(archives)
            else:
                archive_qs = Archive.objects.all()

            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None

            if full:
                for archive in archive_qs:
                    if activity_cb:
                        activity_cb()
                    try:
                        archive.refresh_from_db()
                        if not archive.zipped or not os.path.exists(archive.zipped.path):
                            continue
                        archive.recalc_fileinfo()
                        try:
                            archive.test_zip_file()
                        except (FileNotFoundError, zipfile.BadZipFile) as e:
                            logger.error("For Archive: %s, %s", archive.get_absolute_url(), str(e))
                            continue
                        archive.generate_image_set(force=False, skip_zip_check=True)
                        archive.fix_image_positions()
                        archive.generate_thumbnails(skip_zip_check=True)
                    except Exception:
                        logger.critical(
                            "Error recalculating full fileinfo for archive #%s:\n%s",
                            getattr(archive, "pk", "?"),
                            traceback.format_exc(),
                        )
            else:
                for archive in archive_qs:
                    try:
                        if archive.zipped and os.path.exists(archive.zipped.path):
                            self.enqueue_archive(archive)
                    except (ValueError, FileNotFoundError):
                        pass
                self.start_info_thread(force_local=True)
        else:
            from workers.client import enqueue
            archive_pks = [a.pk if hasattr(a, "pk") else int(a) for a in archives] if archives is not None else []
            if archives is not None and not archive_pks:
                logger.info("No archives to recalculate file info for, not enqueueing fileinfo_worker.")
                return
            extra_options: dict[str, typing.Any] = {"all": True} if archives is None else {}
            if full:
                extra_options["full"] = True
            enqueue(
                args=archive_pks,
                task_type="fileinfo_worker",
                override_options=self.settings,
                extra_options=extra_options,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                event_action="RECALCULATE_FILEINFO" if full else "",
            )
            logger.info(
                "Enqueued fileinfo_worker task to worker process (archives: %s, full: %s)",
                len(archive_pks) if archives is not None else "all",
                full,
            )

    def regenerate_all_thumbs(self, archives: typing.Any = None, user: typing.Any = None, force_local: bool = False) -> None:
        """Enqueue or run thumbnail regeneration for all or specific archives."""
        if force_local:
            import os
            from viewer.models import Archive
            archive_qs: typing.Iterable[Archive]
            if isinstance(archives, (list, tuple, set)) and all(isinstance(x, (int, str)) for x in archives):
                archive_qs = Archive.objects.filter(pk__in=[int(x) for x in archives])
            elif hasattr(archives, "filter"):
                archive_qs = archives
            elif archives is not None:
                archive_qs = list(archives)
            else:
                archive_qs = Archive.objects.all()
            for archive in archive_qs:
                try:
                    if archive.zipped and os.path.exists(archive.zipped.path):
                        self.enqueue_archive(archive)
                except (ValueError, FileNotFoundError):
                    pass
            self.start_thumbs_thread(force_local=True)
        else:
            from workers.client import enqueue
            archive_pks = [a.pk if hasattr(a, "pk") else int(a) for a in archives] if archives is not None else []
            if archives is not None and not archive_pks:
                logger.info("No archives to regenerate thumbnails for, not enqueueing thumbnails_worker.")
                return
            enqueue(
                args=archive_pks,
                task_type="thumbnails_worker",
                override_options=self.settings,
                extra_options={"all": True} if archives is None else None,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
            )
            logger.info(
                "Enqueued thumbnails_worker task to worker process (archives: %s)",
                len(archive_pks) if archives is not None else "all",
            )

    def generate_missing_thumbs(
        self,
        archives: typing.Any = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run thumbnail generation for archives missing thumbnails."""
        if archives is None:
            from viewer.models import Archive
            archives = Archive.objects.filter(thumbnail="")
        return self.regenerate_all_thumbs(archives=archives, user=user, force_local=force_local)

    def calculate_missing_info(
        self,
        archives: typing.Any = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run file info calculation for archives with missing file info."""
        if archives is None:
            from viewer.models import Archive
            archives = Archive.objects.filter_by_missing_file_info()
        return self.recalc_all_file_info(archives=archives, user=user, force_local=force_local)

    def set_all_hidden_as_public(
        self,
        archives: typing.Any = None,
        reason: str | None = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run publishing for archives associated with hidden galleries."""
        if archives is None:
            from viewer.models import Archive
            archives = Archive.objects.filter(gallery__hidden=True)
        return self.publish_archives(archives=archives, reason=reason, user=user, force_local=force_local)

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

    def reduce_archives(
        self,
        archives: typing.Any = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run reduction (deleting extracted images) for one or more archives."""
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
            action = (getattr(self.settings, "event_action", None) if self.settings else None) or "REDUCE_ARCHIVE"
            reason = (getattr(self.settings, "archive_reason", None) if self.settings else None) or None

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    if not archive.extracted:
                        continue
                    archive.reduce()
                    if effective_user:
                        event_log(
                            effective_user,
                            action,
                            reason=reason,
                            content_object=archive,
                            result="success",
                        )
                except Exception:
                    logger.critical(
                        "Error reducing archive #%s:\n%s",
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
                task_type="reduce_worker",
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
                event_action="REDUCE_ARCHIVE",
            )
            logger.info("Enqueued reduce_worker task to worker process (archive count: %d)", len(archive_pks))

    def calculate_images_sha1(
        self,
        archives: typing.Any = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run images SHA1 calculation for one or more archives."""
        if force_local:
            from viewer.models import Archive

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

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    archive.calculate_sha1_and_data_for_images()
                except Exception:
                    logger.critical(
                        "Error calculating images SHA1 for archive #%s:\n%s",
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
                task_type="calculate_sha1_worker",
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
                event_action="CALCULATE_IMAGES_SHA1",
            )
            logger.info("Enqueued calculate_sha1_worker task to worker process (archive count: %d)", len(archive_pks))

    def check_and_convert_filetype(
        self,
        archives: typing.Any = None,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run filetype check and conversion to ZIP."""
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
            action = (getattr(self.settings, "event_action", None) if self.settings else None) or "CONVERT_TO_ZIP"

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    extension, result = archive.check_and_convert_to_zip()
                    if result == 2:
                        result_message = "success"
                    elif result == 0:
                        result_message = "skipped"
                    elif result == 1:
                        result_message = "failed"
                    else:
                        result_message = "unknown"

                    if effective_user:
                        event_log(
                            effective_user,
                            action,
                            content_object=archive,
                            result=result_message,
                            data=extension,
                        )
                except Exception:
                    logger.critical(
                        "Error converting filetype for archive #%s:\n%s",
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
                task_type="convert_archive_worker",
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
                event_action="CONVERT_TO_ZIP",
            )
            logger.info("Enqueued convert_archive_worker task to worker process (archive count: %d)", len(archive_pks))

    def mark_similar_archives(
        self,
        archives: typing.Any = None,
        use_recycled: bool = False,
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run marking similar archives."""
        if force_local:
            from viewer.models import Archive

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

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    archive.create_marks_for_similar_archives(use_recycled_archives=use_recycled)
                except Exception:
                    logger.critical(
                        "Error marking similar archives for archive #%s:\n%s",
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
                task_type="mark_similar_worker",
                override_options={"use_recycled": use_recycled},
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                event_action="MARK_SIMILAR",
            )
            logger.info("Enqueued mark_similar_worker task to worker process (archive count: %d)", len(archive_pks))

    def clone_archive(
        self,
        archive_id: int,
        sha1_list: list[str] | None = None,
        run_image_tool: bool = False,
        bin_original: bool = False,
        user_reason: str = "",
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run clone plus operation."""
        if force_local:
            from django.db import transaction
            from viewer.models import Archive, ArchiveRecycleEntry
            from viewer.utils.actions import event_log
            from django.conf import settings as django_settings

            crawler_settings = getattr(django_settings, "CRAWLER_SETTINGS", None)
            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None
            effective_user = user or (getattr(self.settings, "archive_user", None) if self.settings else None)

            if activity_cb:
                activity_cb()
            try:
                with transaction.atomic():
                    archive = Archive.objects.select_for_update().get(pk=archive_id)
                    original_archive_url = archive.get_absolute_url()

                    if run_image_tool:
                        image_tool = getattr(crawler_settings, "cloning_image_tool", None) if crawler_settings else None
                        image_tool = image_tool if (image_tool and getattr(image_tool, "enable", False)) else None
                        new_archive, error_message = archive.clone_archive_plus(sha1_list, image_tool)
                    elif sha1_list:
                        new_archive, error_message = archive.create_new_archive_ordered_by_sha1(sha1_list)
                    else:
                        new_archive, error_message = archive.clone_archive_plus(sha1_list)

                    if new_archive:
                        result_message = "success"
                        if effective_user:
                            event_log(
                                effective_user,
                                "CLONE_ARCHIVE",
                                reason=user_reason,
                                content_object=new_archive,
                                result=result_message,
                                data=original_archive_url,
                            )

                        old_archive = Archive.objects.select_for_update().get(pk=archive_id)
                        if bin_original and not old_archive.is_recycled():
                            r = ArchiveRecycleEntry(
                                archive=old_archive,
                                reason=user_reason,
                                user=effective_user,
                                origin=ArchiveRecycleEntry.ORIGIN_USER,
                            )
                            r.save()
                            old_archive.binned = True
                            old_archive.simple_save()
                            if effective_user:
                                event_log(
                                    effective_user,
                                    "MOVE_TO_RECYCLE_BIN",
                                    content_object=old_archive,
                                    result="recycled",
                                    reason=user_reason,
                                )
                                if old_archive.public:
                                    old_archive.set_private()
                                    event_log(
                                        effective_user,
                                        "UNPUBLISH_ARCHIVE",
                                        content_object=old_archive,
                                        result="unpublished",
                                    )
                    else:
                        result_message = "failed"
                        if effective_user:
                            event_log(
                                effective_user,
                                "CLONE_ARCHIVE",
                                reason=user_reason,
                                content_object=archive,
                                result=result_message,
                                data=error_message,
                            )
            except Exception:
                logger.critical(
                    "Error cloning archive #%s:\n%s",
                    archive_id,
                    traceback.format_exc(),
                )
        else:
            from workers.client import enqueue

            options = {
                "sha1_list": sha1_list,
                "run_image_tool": run_image_tool,
                "bin_original": bin_original,
                "user_reason": user_reason,
            }
            enqueue(
                args=[archive_id],
                task_type="clone_archive_worker",
                override_options=options,
                user=user,
                reason=user_reason,
                event_action="CLONE_ARCHIVE",
            )
            logger.info("Enqueued clone_archive_worker task for archive #%s", archive_id)

    def split_archive(
        self,
        archive_id: int,
        cleaned_data: list | None = None,
        split_nested: bool = False,
        bin_original: bool = False,
        user_reason: str = "",
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run split archive operation."""
        if force_local:
            from django.db import transaction
            from viewer.models import Archive, ArchiveRecycleEntry
            from viewer.utils.actions import event_log

            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None
            effective_user = user or (getattr(self.settings, "archive_user", None) if self.settings else None)

            if activity_cb:
                activity_cb()
            try:
                with transaction.atomic():
                    archive = Archive.objects.select_for_update().get(pk=archive_id)
                    original_archive_url = archive.get_absolute_url()

                    if split_nested:
                        new_archives, error_message = archive.split_archive([], split_from_nested=True)
                    else:
                        new_archives, error_message = archive.split_archive(cleaned_data or [])

                    if new_archives:
                        result_message = "success"
                        for new_archive in new_archives:
                            if effective_user:
                                event_log(
                                    effective_user,
                                    "SPLIT_ARCHIVE",
                                    reason=user_reason,
                                    content_object=new_archive,
                                    result=result_message,
                                    data=original_archive_url,
                                )

                        old_archive = Archive.objects.select_for_update().get(pk=archive_id)
                        if bin_original and not old_archive.is_recycled():
                            r = ArchiveRecycleEntry(
                                archive=old_archive,
                                reason=user_reason,
                                user=effective_user,
                                origin=ArchiveRecycleEntry.ORIGIN_USER,
                            )
                            r.save()
                            old_archive.binned = True
                            old_archive.simple_save()
                            if effective_user:
                                event_log(
                                    effective_user,
                                    "MOVE_TO_RECYCLE_BIN",
                                    content_object=old_archive,
                                    result="recycled",
                                    reason=user_reason,
                                )
                                if old_archive.public:
                                    old_archive.set_private()
                                    event_log(
                                        effective_user,
                                        "UNPUBLISH_ARCHIVE",
                                        content_object=old_archive,
                                        result="unpublished",
                                    )
                    else:
                        result_message = "failed"
                        if effective_user:
                            event_log(
                                effective_user,
                                "SPLIT_ARCHIVE",
                                reason=user_reason,
                                content_object=archive,
                                result=result_message,
                                data=error_message,
                            )
            except Exception:
                logger.critical(
                    "Error splitting archive #%s:\n%s",
                    archive_id,
                    traceback.format_exc(),
                )
        else:
            from workers.client import enqueue

            options = {
                "cleaned_data": cleaned_data,
                "split_nested": split_nested,
                "bin_original": bin_original,
                "user_reason": user_reason,
            }
            enqueue(
                args=[archive_id],
                task_type="split_archive_worker",
                override_options=options,
                user=user,
                reason=user_reason,
                event_action="SPLIT_ARCHIVE",
            )
            logger.info("Enqueued split_archive_worker task for archive #%s", archive_id)

    def generate_matches(
        self,
        archives: typing.Any = None,
        clear_title: bool = False,
        provider_filter: str = "",
        cutoff: float = 0.4,
        max_matches: int = 10,
        user: typing.Any = None,
        force_local: bool = False,
        auto_link: bool = False,
        auto_link_cutoff: float | None = None,
    ) -> None:
        """Enqueue or run offline matching (possible matches, or auto-link) for non-match archives."""
        if force_local:
            from viewer.models import Archive

            if archives is None:
                return
            archive_qs: typing.Iterable[Archive]
            if isinstance(archives, (list, tuple, set)) and all(isinstance(x, (int, str)) for x in archives):
                archive_qs = Archive.objects.filter(pk__in=[int(x) for x in archives])
            elif hasattr(archives, "filter"):
                archive_qs = archives
            else:
                archive_qs = list(archives)

            from django.conf import settings as django_settings

            from core.matching.engine import MatchingEngine, MatchingOptions
            from core.matching.types import MatchingMode

            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None
            # One engine for the whole batch, so the local gallery titles are loaded once.
            engine = MatchingEngine(
                self.settings or django_settings.CRAWLER_SETTINGS,
                MatchingOptions(
                    mode=MatchingMode.OFFLINE,
                    filters=(provider_filter,),
                    cutoff=cutoff,
                    max_matches=max_matches,
                    clean_title=clear_title,
                    clear_previous=True,
                    auto_link=auto_link,
                    auto_link_cutoff=auto_link_cutoff,
                ),
            )

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                try:
                    archive.refresh_from_db()
                    if archive.gallery or archive.match_type != "non-match" or not archive.zipped.name:
                        continue
                    engine.match_archive(archive)
                except TaskCancelledError:
                    raise
                except Exception:
                    logger.critical(
                        "Error generating matches for archive #%s:\n%s",
                        getattr(archive, "pk", "?"),
                        traceback.format_exc(),
                    )
        else:
            from workers.client import enqueue

            archive_pks = [a.pk if hasattr(a, "pk") else int(a) for a in archives] if archives is not None else []
            if not archive_pks:
                return
            options = {
                "clear_title": clear_title,
                "provider_filter": provider_filter,
                "cutoff": cutoff,
                "max_matches": max_matches,
                "auto_link": auto_link,
                "auto_link_cutoff": auto_link_cutoff,
            }
            enqueue(
                args=archive_pks,
                task_type="match_unmatched_worker",
                override_options=options,
                user=user,
                event_action="GENERATE_MATCHES",
            )
            logger.info("Enqueued generate_matches task to worker process (archive count: %d)", len(archive_pks))

    def delete_archive(
        self,
        archive_id: int,
        delete_zipped: bool = False,
        delete_archive: bool = False,
        delete_file: bool = False,
        delete_gallery: bool = False,
        mark_gallery_deleted: bool = False,
        mark_gallery_denied: bool = False,
        user_reason: str = "",
        user: typing.Any = None,
        force_local: bool = False,
    ) -> None:
        """Enqueue or run deletion of archive and related gallery/files."""
        if force_local:
            from django.db import transaction
            from viewer.models import Archive, ArchiveRecycleEntry
            from viewer.utils.actions import event_log

            activity_cb = getattr(self.settings, "activity_callback", None) if self.settings else None
            effective_user = user or (getattr(self.settings, "archive_user", None) if self.settings else None)

            if activity_cb:
                activity_cb()
            try:
                archive = Archive.objects.filter(pk=archive_id).first()
                if not archive:
                    return

                gallery = archive.gallery
                archive_report = archive.delete_text_report()
                old_gallery_link = None

                if mark_gallery_deleted and archive.gallery:
                    archive.gallery.mark_as_deleted()
                    if not delete_zipped:
                        archive.gallery = None
                    if effective_user:
                        event_log(
                            effective_user,
                            "MARK_DELETE_GALLERY",
                            reason=user_reason,
                            data=archive_report,
                            content_object=gallery,
                            result="success",
                        )
                elif mark_gallery_denied and archive.gallery:
                    archive.gallery.mark_as_denied()
                    if not delete_zipped:
                        archive.gallery = None
                    if effective_user:
                        event_log(
                            effective_user,
                            "DENY_GALLERY",
                            reason=user_reason,
                            data=archive_report,
                            content_object=gallery,
                            result="success",
                        )
                elif delete_gallery and archive.gallery:
                    old_gallery_link = archive.gallery.get_link()
                    archive.gallery.delete()
                    archive.gallery = None
                    if effective_user:
                        event_log(
                            effective_user,
                            "DELETE_GALLERY",
                            reason=user_reason,
                            data=old_gallery_link,
                            result="deleted",
                        )

                actual_action = None

                if delete_zipped:
                    actual_action = "DELETE_ZIPPED_PRESERVE_IMAGE_DATA"
                    archive.delete_all_files(create_mark=True, preserve_image_data=True)
                    if not archive.is_recycled():
                        with transaction.atomic():
                            r = ArchiveRecycleEntry(
                                archive=archive,
                                reason=user_reason,
                                user=effective_user,
                                origin=ArchiveRecycleEntry.ORIGIN_USER,
                            )
                            r.save()
                            archive.binned = True
                            archive.simple_save()
                            if effective_user:
                                event_log(
                                    effective_user,
                                    "MOVE_TO_RECYCLE_BIN",
                                    content_object=archive,
                                    result="recycled",
                                    reason=user_reason,
                                )
                                if archive.public:
                                    archive.set_private()
                                    event_log(
                                        effective_user,
                                        "UNPUBLISH_ARCHIVE",
                                        content_object=archive,
                                        result="unpublished",
                                    )
                else:
                    if delete_file:
                        create_delete_mark = False if delete_archive else True
                        archive.delete_all_files(create_mark=create_delete_mark)
                        actual_action = "DELETE_ZIPPED"
                    if delete_archive:
                        archive.delete_files_but_archive()
                        archive.delete()
                        actual_action = "DELETE_ARCHIVE"

                if actual_action is not None and effective_user:
                    if old_gallery_link:
                        event_log(
                            effective_user,
                            actual_action,
                            reason=user_reason,
                            result="deleted",
                            data=archive_report,
                        )
                    else:
                        event_log(
                            effective_user,
                            actual_action,
                            reason=user_reason,
                            content_object=gallery,
                            result="deleted",
                            data=archive_report,
                        )
            except Exception:
                logger.critical(
                    "Error deleting archive #%s:\n%s",
                    archive_id,
                    traceback.format_exc(),
                )
        else:
            from workers.client import enqueue

            options = {
                "delete_zipped": delete_zipped,
                "delete_archive": delete_archive,
                "delete_file": delete_file,
                "delete_gallery": delete_gallery,
                "mark_gallery_deleted": mark_gallery_deleted,
                "mark_gallery_denied": mark_gallery_denied,
                "user_reason": user_reason,
            }
            enqueue(
                args=[archive_id],
                task_type="delete_archive_worker",
                override_options=options,
                user=user,
                reason=user_reason,
                event_action="DELETE_ARCHIVE",
            )
            logger.info("Enqueued delete_archive_worker task for archive #%s", archive_id)

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
                        if archive.gallery and not archive.gallery.public:
                            archive.gallery.public = True
                            archive.gallery.save()
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
                    if not archive.crc32:
                        archive.recalc_fileinfo()
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

    def extract_contextual_info(
        self,
        archives: typing.Any = None,
        reason: str | None = None,
        user: typing.Any = None,
        force_local: bool = False,
        profile: str | None = None,
    ) -> None:
        """Enqueue or run contextual extraction via manga-extractor for one or more archives."""
        selected_profile = profile or getattr(self.settings, "manga_extractor_profile", None)
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
            is_cancelled_cb = getattr(self.settings, "is_cancelled", None) if self.settings else None
            effective_user = user or (getattr(self.settings, "archive_user", None) if self.settings else None)
            action = (getattr(self.settings, "event_action", None) if self.settings else None) or "EXTRACT_CONTEXTUAL_INFO"
            effective_reason = (
                reason
                if reason is not None
                else (getattr(self.settings, "archive_reason", None) if self.settings else None)
            ) or ""

            for archive in archive_qs:
                if activity_cb:
                    activity_cb()
                if callable(is_cancelled_cb) and is_cancelled_cb():
                    break
                try:
                    archive.refresh_from_db()
                    if activity_cb:
                        setattr(archive, "_activity_callback", activity_cb)
                    if is_cancelled_cb:
                        setattr(archive, "_is_cancelled", is_cancelled_cb)
                    res = archive.extract_contextual_info(settings_override=self.settings, profile=selected_profile)
                    if res and effective_user:
                        event_log(
                            effective_user,
                            action,
                            reason=effective_reason or None,
                            content_object=archive,
                            result="extracted_context",
                        )
                except Exception:
                    logger.critical(
                        "Error extracting contextual info for archive #%s:\n%s",
                        getattr(archive, "pk", "?"),
                        traceback.format_exc(),
                    )
        else:
            from workers.client import enqueue_contextual_extraction

            effective_reason = (
                reason
                if reason is not None
                else (getattr(self.settings, "archive_reason", "") if self.settings else "")
            ) or ""
            tasks = enqueue_contextual_extraction(
                archives=archives,
                override_options=self.settings,
                user=user or (getattr(self.settings, "archive_user", None) if self.settings else None),
                reason=effective_reason,
                event_action="EXTRACT_CONTEXTUAL_INFO",
                profile=selected_profile,
            )
            logger.info(
                "Enqueued %d contextual_extractor_worker task(s) to worker process",
                len(tasks),
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
            if not archive_pks:
                return
            enqueue(
                args=archive_pks,
                task_type="fileinfo_worker",
                override_options=self.settings,
                user=getattr(self.settings, "archive_user", None) if self.settings else None,
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
            )
            logger.info("Enqueued fileinfo_worker task to worker process (archive count: %d)", len(archive_pks))

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
            if not archive_pks:
                return
            enqueue(
                args=archive_pks,
                task_type="thumbnails_worker",
                override_options=self.settings,
                user=getattr(self.settings, "archive_user", None) if self.settings else None,
                reason=getattr(self.settings, "archive_reason", "") if self.settings else "",
            )
            logger.info("Enqueued thumbnails_worker task to worker process (archive count: %d)", len(archive_pks))

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
