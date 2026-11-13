import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from enum import StrEnum

import django.utils.timezone as django_tz
from django.db import close_old_connections, connection

from core.base.setup import Settings
from core.downloaders.postdownload import CurrentDownload, PostDownloader, TransferResult, download_kind
from core.downloaders.torrent import get_torrent_client
from core.workers.schedulers import BaseScheduler
from viewer.models import DownloadEvent

logger = logging.getLogger(__name__)


def mark_completed_and_failed_for_indirect_downloads(download_events, fail_missing_archive: bool = True):
    events_completed = [x for x in download_events if x.archive and x.archive.crc32]
    # If there's no archive, we assume it was deleted and considered a failed download for indirect methods (if enabled)
    if fail_missing_archive:
        events_failed = [x for x in download_events if not x.archive or x.archive.is_recycled()]
    else:
        events_failed = [x for x in download_events if x.archive and x.archive.is_recycled()]
    for download_event in events_completed:
        download_event.finish_download()
        download_event.save()
    for download_event in events_failed:
        download_event.set_as_failed()
        download_event.save()


class TransferReason(StrEnum):
    FINISHED = "finished"  # The external program reports the download as finished.
    REMOVED_FROM_CLIENT = "removed_from_client"  # The torrent disappeared from the client before we saw it finish.


class DownloadProgressChecker(BaseScheduler):
    """Follows every in-progress DownloadEvent until it is completed or failed.

    Torrent and H@H downloads are written by external programs. For those, this checker reads their progress
    (torrent client API, size of the H@H folder) and, once finished, transfers the files into media storage in a
    background pool (``parallel_post_downloaders`` transfers at a time) and validates them.
    A torrent that disappears from the client before being seen as finished is looked up in the download folder:
    whatever is there is transferred and validated, and if nothing is there the download is marked as failed.
    """

    thread_name = "download_progress_checker"
    TORRENT_METHODS = ["torrent", "torrent_api"]
    HATH_METHODS = ["hath"]
    ARCHIVE_METHODS = ["archive", "archive_js", "gallerydl"]
    # Wait before retrying a transfer that errored (connection problems) or found nothing yet.
    TRANSFER_RETRY_DELAY = timedelta(minutes=5)

    def __init__(self, settings: Settings, web_queue=None, timer=1, pk=None, parallel_transfers: int | None = None):
        super().__init__(settings, web_queue, timer, pk)
        if parallel_transfers is None:
            parallel_transfers = getattr(settings, "parallel_post_downloaders", 4)
        self.parallel_transfers = max(1, int(parallel_transfers or 1))
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        # Event pk -> PostDownloader doing its transfer. Also used to report the files being transferred.
        self._transfers: dict[int, PostDownloader] = {}
        self._retry_after: dict[int, datetime] = {}

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer

    def check_torrent_progress(
        self, download_event: DownloadEvent, download_progress: float | None = None
    ) -> None:
        if download_progress is not None:
            download_event.progress = download_progress

        archive_entry = None
        archive_completed = False
        try:
            archive_entry = download_event.archive
        except Exception:
            archive_entry = None

        if archive_entry:
            try:
                archive_entry.refresh_from_db(fields=["crc32"])
            except Exception:
                pass
            archive_completed = bool(archive_entry.crc32)

        if archive_entry is None:
            is_completed = (download_event.progress >= 100)
        else:
            is_completed = archive_completed

        if is_completed:
            download_event.finish_download()
        elif self.settings.max_torrent_lifespan > 0 and not archive_completed and download_event.progress < 100:
            # A finished torrent is only waiting for its transfer, so it can't be dead.
            if not self.settings.max_torrent_lifespan_type:
                self.settings.max_torrent_lifespan_type = "days"
            lifespan_type = str(self.settings.max_torrent_lifespan_type).strip().lower()
            match lifespan_type:
                case "sec" | "second" | "seconds" | "s":
                    max_torrent_lifespan_seconds = float(self.settings.max_torrent_lifespan)
                case "min" | "minute" | "minutes" | "m":
                    max_torrent_lifespan_seconds = float(self.settings.max_torrent_lifespan) * 60
                case "hour" | "hours" | "h":
                    max_torrent_lifespan_seconds = float(self.settings.max_torrent_lifespan) * 60 * 60
                case "days" | "day" | "d":
                    max_torrent_lifespan_seconds = float(self.settings.max_torrent_lifespan) * 60 * 60 * 24
                case _:
                    logger.warning(
                        f"Unknown max_torrent_lifespan_type '{self.settings.max_torrent_lifespan_type}', defaulting to days."
                    )
                    max_torrent_lifespan_seconds = float(self.settings.max_torrent_lifespan) * 60 * 60 * 24

            if download_event.create_date:
                now = django_tz.now()
                if django_tz.is_aware(download_event.create_date) and django_tz.is_naive(now):
                    now = django_tz.make_aware(now)
                elif django_tz.is_naive(download_event.create_date) and django_tz.is_aware(now):
                    now = django_tz.make_naive(now)
                time_passed = now - download_event.create_date
                time_passed_seconds = max(0.0, time_passed.total_seconds())
            else:
                time_passed_seconds = 0.0

            if time_passed_seconds > max_torrent_lifespan_seconds:
                logger.info(f"Torrent {download_event.name} has exceeded its lifespan.")
                bad_archive = archive_entry
                bad_archive_id = getattr(bad_archive, "id", None)

                # Extract gallery, user, and reason before deleting the archive
                gallery = None
                try:
                    gallery = download_event.gallery
                except Exception:
                    gallery = None
                if not gallery and bad_archive:
                    try:
                        gallery = bad_archive.gallery
                    except Exception:
                        gallery = None

                archive_reason = ""
                archive_user = None
                if bad_archive:
                    archive_reason = getattr(bad_archive, "reason", None) or ""
                    archive_user = getattr(bad_archive, "user", None)

                # Detach the archive from the event so we don't get ForeignKey errors when saving later
                download_event.archive = None
                if bad_archive:
                    try:
                        bad_archive.delete_all_files(create_mark=False)
                    except Exception as e:
                        logger.error(
                            f"Failed to delete physical files for stalled Archive {bad_archive_id}: {e}"
                        )
                    try:
                        bad_archive.delete()
                    except Exception as e:
                        logger.error(
                            f"Failed to delete stalled Archive {bad_archive_id}: {e}"
                        )
                download_event.set_as_failed()

                # Determine gallery URL to requeue: prioritize gallery.get_link(), fallback to download_event.name
                gallery_link = None
                if gallery:
                    try:
                        link = gallery.get_link()
                        if link and link != "Can't reconstruct URL":
                            gallery_link = link
                    except Exception:
                        gallery_link = None

                if not gallery_link and download_event.name:
                    if download_event.name.startswith("http://") or download_event.name.startswith("https://"):
                        gallery_link = download_event.name

                # Requeue with torrent disabled
                if hasattr(self, "web_queue") and self.web_queue and gallery_link:
                    config = getattr(self.settings, "config", None)
                    temp_settings = Settings(load_from_config=config) if config else Settings(load_from_disk=False)
                    if hasattr(self.settings, "custom_downloaders") and self.settings.custom_downloaders:
                        temp_settings.custom_downloaders.update(self.settings.custom_downloaders)
                    if hasattr(self.settings, "custom_downloader_providers") and self.settings.custom_downloader_providers:
                        temp_settings.custom_downloader_providers.update(self.settings.custom_downloader_providers)
                    if hasattr(self.settings, "downloaders") and self.settings.downloaders:
                        for k, v in self.settings.downloaders.items():
                            temp_settings.downloaders.setdefault(k, v)

                    if hasattr(temp_settings, "config") and isinstance(temp_settings.config, dict) and "downloaders" in temp_settings.config:
                        temp_settings.config["downloaders"]["panda_torrent"] = -1
                        temp_settings.config["downloaders"]["panda_torrent_api"] = -1
                    if hasattr(temp_settings, "downloaders"):
                        temp_settings.downloaders["panda_torrent"] = -1
                        temp_settings.downloaders["panda_torrent_api"] = -1

                    # Disable any custom downloaders that wrap torrent downloaders
                    if hasattr(temp_settings, "custom_downloaders") and temp_settings.custom_downloaders:
                        for custom_name, targets in temp_settings.custom_downloaders.items():
                            if any("torrent" in t for t in targets.keys()):
                                temp_settings.downloaders[custom_name] = -1
                                if hasattr(temp_settings, "config") and isinstance(temp_settings.config, dict) and "downloaders" in temp_settings.config:
                                    temp_settings.config["downloaders"][custom_name] = -1

                    # Ensure at least one non-torrent downloader is enabled
                    fallback_dl = (
                        getattr(self.settings, "failed_download_downloader", None)
                        or getattr(self.settings, "requeue_downloader", None)
                        or "panda_archive"
                    )
                    has_enabled_dl = any(v > 0 for k, v in temp_settings.downloaders.items() if "torrent" not in k)
                    if not has_enabled_dl:
                        temp_settings.downloaders[fallback_dl] = 1
                        if hasattr(temp_settings, "config") and isinstance(temp_settings.config, dict) and "downloaders" in temp_settings.config:
                            temp_settings.config["downloaders"][fallback_dl] = 1

                    temp_settings.redownload = True
                    temp_settings.replace_metadata = True
                    temp_settings.retry_failed = True
                    if archive_reason:
                        temp_settings.archive_reason = archive_reason
                    if archive_user:
                        temp_settings.archive_user = archive_user
                    try:
                        self.web_queue.enqueue_args_list(
                            (gallery_link,),
                            override_options=temp_settings,
                        )
                        logger.info(f"Requeued {download_event.name} because of a dead torrent.")
                    except Exception as e:
                        logger.error(f"Failed to requeue {download_event.name}: {e}")
        download_event.save()

    def job(self) -> None:
        try:
            while not self.stop.is_set():

                seconds_to_wait = self.wait_until_next_run()
                if self.stop.wait(timeout=seconds_to_wait):
                    return

                close_old_connections()

                try:
                    self.check_downloads()
                except Exception as e:
                    logger.error(f"Error in DownloadProgressChecker job cycle: {e}")

                self.update_last_run(django_tz.now())
                try:
                    close_old_connections()
                except Exception:
                    pass
        finally:
            # Running transfers finish on their own; queued ones are dropped and picked up again on the next start.
            with self._lock:
                executor, self._executor = self._executor, None
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)

    def check_downloads(self) -> None:
        torrent_events: list[DownloadEvent] = []
        hath_events: list[DownloadEvent] = []
        archive_events: list[DownloadEvent] = []
        for download_event in DownloadEvent.objects.in_progress().select_related(
            "archive", "archive__gallery", "gallery"
        ).order_by("method", "download_id"):
            if download_event.method in self.TORRENT_METHODS:
                torrent_events.append(download_event)
            elif download_event.method in self.HATH_METHODS:
                hath_events.append(download_event)
            elif download_event.method in self.ARCHIVE_METHODS:
                archive_events.append(download_event)

        if torrent_events:
            self.check_torrent_downloads(torrent_events)
        if hath_events:
            self.check_hath_downloads(hath_events)
        if archive_events:
            self.check_archive_downloads(archive_events)

    def check_torrent_downloads(self, download_events: list[DownloadEvent]) -> None:
        fail_missing = self.settings.max_torrent_lifespan <= 0
        mark_completed_and_failed_for_indirect_downloads(download_events, fail_missing_archive=fail_missing)
        pending = [x for x in download_events if not x.completed and not self._is_transferring(x)]
        if not pending:
            return

        progress_by_event = self._query_torrent_client(pending)

        for download_event in pending:
            try:
                if progress_by_event is None:
                    # Client unreachable: nothing is known about the torrent, only enforce its lifespan.
                    self.check_torrent_progress(download_event, None)
                elif download_event.pk in progress_by_event:
                    self.check_torrent_progress(download_event, progress_by_event[download_event.pk])
                    if not download_event.completed and download_event.progress >= 100 and download_event.archive_id:
                        self._start_transfer(download_event, TransferReason.FINISHED)
                elif download_event.archive_id:
                    self._start_transfer(download_event, TransferReason.REMOVED_FROM_CLIENT)
                else:
                    self.check_torrent_progress(download_event, None)
            except Exception as e:
                logger.error(f"Error checking torrent download {download_event.name}: {e}")

    def _query_torrent_client(self, download_events: list[DownloadEvent]) -> dict[int, float] | None:
        """Progress per event pk for the torrents present in the client, or None if the client can't be queried."""
        client = get_torrent_client(self.settings.torrent)
        if not client:
            return None
        try:
            if not client.connect():
                return None
            results = client.get_download_progress([(x.download_id, x) for x in download_events])
        except Exception as e:
            logger.error(f"Error querying torrent client for progress: {e}")
            return None
        if results is None:
            return None
        return {download_event.pk: progress for download_event, progress in results}

    def check_hath_downloads(self, download_events: list[DownloadEvent]) -> None:
        mark_completed_and_failed_for_indirect_downloads(download_events)
        pending = [x for x in download_events if not x.completed and x.archive and not self._is_transferring(x)]
        if not pending:
            return

        try:
            progress_by_archive = PostDownloader(self.settings).hath_progress([x.archive for x in pending if x.archive])
        except Exception as e:
            logger.error(f"Could not check the H@H download folder: {e}")
            return

        for download_event in pending:
            ratio = progress_by_archive.get(download_event.archive_id or 0)
            if ratio is None:
                continue
            new_progress = min(ratio, 1.0) * 100
            if new_progress != download_event.progress:
                download_event.progress = new_progress
                download_event.save(update_fields=["progress"])
            if ratio >= 1:
                self._start_transfer(download_event, TransferReason.FINISHED)

    def check_archive_downloads(self, download_events: list[DownloadEvent]) -> None:
        for download_event in download_events:
            if download_event.status == DownloadEvent.Status.QUEUED:
                continue
            if download_event.status == DownloadEvent.Status.IN_PROGRESS and download_event.download_url:
                continue
            if download_event.archive and download_event.archive.crc32:
                download_event.finish_download()
                download_event.save()
            elif (
                download_event.total_size > 0
                and download_event.download_id
                and os.path.isfile(download_event.download_id)
            ):
                file_stats = os.stat(download_event.download_id)
                filesize = file_stats.st_size
                download_progress = 100 * filesize / download_event.total_size
                download_event.progress = download_progress
                if download_progress >= 100:
                    download_event.finish_download()
                download_event.save()

    # Transfers

    def _is_transferring(self, download_event: DownloadEvent) -> bool:
        with self._lock:
            if download_event.pk in self._transfers:
                return True
            retry_after = self._retry_after.get(download_event.pk)
            return retry_after is not None and django_tz.now() < retry_after

    def _start_transfer(self, download_event: DownloadEvent, reason: TransferReason) -> None:
        with self._lock:
            if download_event.pk in self._transfers:
                return
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.parallel_transfers, thread_name_prefix=f"{self.thread_name}-transfer"
                )
            post_downloader = PostDownloader(self.settings, web_queue=self.web_queue)
            self._transfers[download_event.pk] = post_downloader
            self._retry_after.pop(download_event.pk, None)
            self._executor.submit(self.run_transfer, download_event.pk, reason, post_downloader)

    def run_transfer(
        self, event_pk: int, reason: TransferReason, post_downloader: PostDownloader | None = None
    ) -> None:
        post_downloader = post_downloader or PostDownloader(self.settings, web_queue=self.web_queue)
        try:
            download_event = (
                DownloadEvent.objects.select_related("archive", "archive__gallery", "gallery").filter(pk=event_pk).first()
            )
            if not download_event or download_event.completed or not download_event.archive:
                return
            archive = download_event.archive
            try:
                result = post_downloader.transfer_archive(archive)
            except Exception as e:
                logger.error(
                    f"Transfer for download {download_event.name} (archive {archive.pk}) failed, "
                    f"retrying in {self.TRANSFER_RETRY_DELAY}: {e}"
                )
                self._retry_later(event_pk)
                return

            download_event.refresh_from_db()
            if download_event.completed:
                # process_downloaded_archive already settled it.
                return

            if result == TransferResult.PROCESSED:
                archive.refresh_from_db(fields=["crc32"])
                if archive.crc32:
                    download_event.finish_download()
                else:
                    download_event.set_as_failed("Downloaded file failed validation")
                download_event.save()
            elif result == TransferResult.NOT_FOUND and reason == TransferReason.REMOVED_FROM_CLIENT:
                self._fail_removed_torrent(download_event)
            else:
                kind = download_kind(download_event.method) or download_event.method
                logger.warning(
                    f"Download {download_event.name} is finished, but its files were not found complete "
                    f"in the {kind} download folder ({result}), retrying in {self.TRANSFER_RETRY_DELAY}."
                )
                self._retry_later(event_pk)
        except Exception as e:
            logger.error(f"Error transferring download event {event_pk}: {e}")
            self._retry_later(event_pk)
        finally:
            with self._lock:
                self._transfers.pop(event_pk, None)
            try:
                if not connection.in_atomic_block:
                    connection.close()
            except Exception:
                pass

    def _fail_removed_torrent(self, download_event: DownloadEvent) -> None:
        # An expired torrent goes through the lifespan handling, which also requeues it with another method.
        self.check_torrent_progress(download_event, None)
        if download_event.completed:
            return
        logger.info(f"Torrent {download_event.name} was removed from the client before finishing, marking as failed.")
        download_event.set_as_failed(
            "Torrent was removed from the client before finishing, and no files were found in the download folder"
        )
        download_event.save()

    def _retry_later(self, event_pk: int) -> None:
        with self._lock:
            self._retry_after[event_pk] = django_tz.now() + self.TRANSFER_RETRY_DELAY

    def current_download(self) -> list[CurrentDownload]:
        if self.is_running_locally():
            with self._lock:
                return [x.current_download for x in self._transfers.values()]
        try:
            from workers.models import SchedulerState

            state = SchedulerState.objects.filter(name=self.thread_name).first()
            if not state or not state.is_running or not state.extra_data:
                return []
            return [
                CurrentDownload(
                    filename=item.get("filename", ""),
                    speed=float(item.get("speed", 0.0)),
                    index=int(item.get("index", 0)),
                    total=int(item.get("total", 0)),
                    downloaded=int(item.get("downloaded", 0)),
                    filesize=int(item.get("filesize", 0)),
                )
                for item in state.extra_data.get("current_download", [])
            ]
        except Exception:
            return []
