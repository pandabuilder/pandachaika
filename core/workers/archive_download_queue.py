import logging
import os
import threading
import time
from typing import Any

import django.utils.timezone as django_tz
import requests
from django.db import close_old_connections, connection, models, transaction

from core.base.setup import Settings
from core.base.utilities import calc_crc32, request_by_provider
from core.downloaders.postdownload import CurrentDownload, PostDownloader
from core.workers.schedulers import BaseScheduler
from viewer.models import DownloadEvent

logger = logging.getLogger(__name__)


def _safe_close_old_connections() -> None:
    if threading.current_thread() is not threading.main_thread():
        close_old_connections()


class ArchiveDownloadQueue(BaseScheduler):
    """Background scheduler that processes queued archive HTTP downloads asynchronously."""

    thread_name = "archive_download_queue"

    def __init__(
        self,
        settings: Settings,
        web_queue: Any = None,
        timer: float = 2.0,
        pk: Any = None,
    ) -> None:
        super().__init__(settings, web_queue=web_queue, timer=timer, pk=pk)
        self._lock = threading.Lock()
        self._active_downloads: dict[int, CurrentDownload] = {}
        self._active_threads: dict[int, threading.Thread] = {}

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return max(0.5, float(timer))

    def current_download(self) -> list[CurrentDownload]:
        """Return list of active downloads, falling back to DB SchedulerState if running in worker."""
        if self.is_running_locally():
            with self._lock:
                return list(self._active_downloads.values())

        try:
            from workers.models import SchedulerState

            state = SchedulerState.objects.filter(name=self.thread_name).first()
            if not state or not state.is_running:
                return []

            # If there are no queued downloads currently in progress in DB, return empty
            if not DownloadEvent.objects.downloading().exclude(download_url="").exists():
                return []

            if state.extra_data and "current_download" in state.extra_data:
                return [
                    CurrentDownload(
                        filename=item.get("filename", ""),
                        speed=float(item.get("speed", 0.0)),
                        index=int(item.get("index", 0)),
                        total=int(item.get("total", 0)),
                        downloaded=int(item.get("downloaded", 0)),
                        filesize=int(item.get("filesize", 0)),
                    )
                    for item in state.extra_data["current_download"]
                    if item.get("filename")
                ]
        except Exception:
            pass

        return []

    def get_provider_max_concurrency(self, provider_name: str) -> int:
        """Get the maximum concurrent archive downloads allowed for a given provider."""
        if hasattr(self.settings, "providers") and provider_name in self.settings.providers:
            prov_settings = self.settings.providers[provider_name]
            max_p = getattr(prov_settings, "max_parallel_archive_downloads", 1)
            if max_p is not None:
                return max(1, int(max_p))
        return 1

    def claim_next_events(self, max_to_claim: int) -> list[int]:
        """Atomically claim up to max_to_claim queued DownloadEvent records using row-level locking."""
        if max_to_claim <= 0:
            return []

        supports_skip_locked = getattr(connection.features, "has_select_for_update_skip_locked", False)
        claimed_ids: list[int] = []

        try:
            with transaction.atomic():
                in_progress_counts = dict(
                    DownloadEvent.objects.filter(
                        status=DownloadEvent.Status.IN_PROGRESS,
                        completed=False,
                    )
                    .values("provider")
                    .annotate(count=models.Count("id"))
                    .values_list("provider", "count")
                )

                qs = DownloadEvent.objects.filter(
                    status=DownloadEvent.Status.QUEUED,
                    completed=False,
                    failed=False,
                ).exclude(download_url="")

                if supports_skip_locked:
                    qs = qs.select_for_update(skip_locked=True)
                else:
                    qs = qs.select_for_update()

                candidates = list(qs.order_by("-priority", "create_date")[: max_to_claim * 5])

                now = django_tz.now()
                pid = os.getpid()

                for event in candidates:
                    if len(claimed_ids) >= max_to_claim:
                        break

                    provider = event.provider or "unknown"
                    max_for_prov = self.get_provider_max_concurrency(provider)
                    current_prov_count = in_progress_counts.get(provider, 0)
                    if current_prov_count >= max_for_prov:
                        continue

                    event.status = DownloadEvent.Status.IN_PROGRESS
                    event.started_date = now
                    event.worker_pid = pid
                    event.attempts += 1
                    event.save(update_fields=["status", "started_date", "worker_pid", "attempts"])

                    in_progress_counts[provider] = current_prov_count + 1
                    claimed_ids.append(event.id)

        except Exception as e:
            logger.error("Failed to claim queued download events: %s", e, exc_info=True)

        return claimed_ids

    def process_queue(self) -> None:
        """Scan active threads, clean up finished ones, and dispatch free download slots."""
        with self._lock:
            finished = [eid for eid, th in self._active_threads.items() if not th.is_alive()]
            for eid in finished:
                del self._active_threads[eid]
            active_count = len(self._active_threads)

        max_concurrency = int(getattr(self.settings, "parallel_archive_downloaders", 0) or 0)
        if max_concurrency <= 0 and getattr(self.settings, "queue_archive_downloads", False):
            max_concurrency = 1
        free_slots = max(0, max_concurrency - active_count)
        if free_slots <= 0:
            return

        claimed_ids = self.claim_next_events(free_slots)
        if not claimed_ids:
            return

        for event_id in claimed_ids:
            th = threading.Thread(
                name=f"archive_downloader_{event_id}",
                target=self._download_worker,
                args=(event_id,),
                daemon=True,
            )
            with self._lock:
                self._active_threads[event_id] = th
            th.start()

    def _download_worker(self, event_id: int) -> None:
        """Execute the HTTP download for a single claimed DownloadEvent."""
        _safe_close_old_connections()
        event: DownloadEvent | None = None
        part_filepath: str = ""
        filepath: str = ""

        try:
            try:
                event = DownloadEvent.objects.select_related("archive", "gallery").get(pk=event_id)
            except DownloadEvent.DoesNotExist:
                return

            filepath = event.download_id
            if not filepath:
                logger.error("DownloadEvent #%s has no filepath (download_id)", event_id)
                event.set_as_failed("No filepath specified")
                event.save()
                return

            req_opts = dict(event.request_options or {})
            rate_limit_name = req_opts.get("rate_limit_name") or f"{event.provider}_web"
            rate_limit_calls = int(req_opts.get("rate_limit_calls", 1))
            rate_limit_period = float(req_opts.get("rate_limit_period", 1.0))

            web_request = request_by_provider(rate_limit_name, rate_limit_calls, rate_limit_period)

            req_dict: dict[str, Any] = {}
            for k in ("headers", "cookies", "proxies", "timeout", "params", "data"):
                if k in req_opts and req_opts[k] is not None:
                    req_dict[k] = req_opts[k]
            req_dict["stream"] = True

            filename = os.path.basename(filepath)
            cur_dl = CurrentDownload(
                filename=filename,
                speed=0.0,
                index=1,
                total=1,
                downloaded=0,
                filesize=event.total_size or 0,
            )
            with self._lock:
                self._active_downloads[event_id] = cur_dl

            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            part_filepath = f"{filepath}.part"

            logger.info(
                "Starting download for event #%s (archive %s, provider %s) from %s",
                event_id,
                getattr(event.archive, "pk", None),
                event.provider,
                event.download_url,
            )

            response = web_request(event.download_url, req_dict)
            if not response or response.status_code >= 400:
                code = getattr(response, "status_code", "None")
                raise requests.exceptions.RequestException(f"HTTP error {code} fetching {event.download_url}")

            content_length = int(response.headers.get("Content-Length", 0))
            if content_length > 0:
                cur_dl.filesize = content_length
                if event.total_size != content_length:
                    event.total_size = content_length
                    event.save(update_fields=["total_size"])

            downloaded = 0
            start_time = time.time()
            last_progress_save = time.time()

            with open(part_filepath, "wb") as fo:
                for chunk in response.iter_content(chunk_size=65536):
                    if self.stop.is_set():
                        logger.info("Download of event #%s stopped due to scheduler shutdown", event_id)
                        fo.close()
                        if os.path.exists(part_filepath):
                            try:
                                os.remove(part_filepath)
                            except Exception:
                                pass
                        event.status = DownloadEvent.Status.QUEUED
                        event.worker_pid = None
                        event.save(update_fields=["status", "worker_pid"])
                        return

                    if chunk:
                        fo.write(chunk)
                        downloaded += len(chunk)
                        cur_dl.downloaded = downloaded
                        now = time.time()
                        elapsed = now - start_time
                        if elapsed > 0:
                            cur_dl.speed = downloaded / elapsed

                        if now - last_progress_save >= 2.0 and cur_dl.filesize > 0:
                            last_progress_save = now
                            progress = min(99.0, (downloaded / cur_dl.filesize) * 100.0)
                            event.progress = round(progress, 1)
                            event.save(update_fields=["progress"])

            if not os.path.exists(part_filepath) or os.path.getsize(part_filepath) == 0:
                raise ValueError(f"Downloaded file for event #{event_id} is empty or missing")

            if os.path.exists(filepath):
                os.remove(filepath)
            os.replace(part_filepath, filepath)

            # Update archive metadata and calculate CRC32
            archive = event.archive
            if archive:
                archive.refresh_from_db()
                archive.recalc_fileinfo()
                archive.save()
                if not archive.thumbnail:
                    try:
                        archive.generate_thumbnails()
                    except Exception as e:
                        logger.warning("Failed to generate thumbnails for archive #%s: %s", archive.pk, e)
                if req_opts.get("mark_similar_new_archives"):
                    try:
                        archive.create_marks_for_similar_archives()
                    except Exception as e:
                        logger.warning("Failed to create marks for similar archives for #%s: %s", archive.pk, e)

                if req_opts.get("preserve_extracted") and not archive.extracted and archive.crc32:
                    try:
                        archive.extract()
                    except Exception as e:
                        logger.warning("Failed to extract archive #%s: %s", archive.pk, e)

                user_id = req_opts.get("archive_user_id")
                if user_id:
                    try:
                        from django.contrib.auth import get_user_model
                        from viewer.utils.actions import event_log

                        user_cls = get_user_model()
                        user = user_cls.objects.filter(pk=user_id).first()
                        if user:
                            action = req_opts.get("event_action") or "DOWNLOAD_ARCHIVE"
                            reason = req_opts.get("archive_reason") or None
                            event_log(
                                user,
                                action,
                                reason=reason,
                                content_object=archive,
                                result=1,
                                data=event.download_url,
                            )
                    except Exception as e:
                        logger.warning("Failed to log download action for archive #%s: %s", archive.pk, e)

                panda_settings = self.settings.providers.get("panda") if hasattr(self.settings, "providers") else None
                if (
                    panda_settings
                    and getattr(panda_settings, "confirm_files_after_download", False)
                    and archive.match_type
                    and ("chaika" in archive.match_type or archive.match_type == "panda_chaika")
                ):
                    post_downloader = PostDownloader(self.settings, web_queue=self.web_queue)
                    success = post_downloader.process_downloaded_archive(archive)
                    if not success:
                        event.set_as_failed(error_message="SHA1 mismatch on downloaded zipfile")
                        event.save()
                        return

            event.finish_download()
            event.save()
            logger.info("Successfully downloaded queued archive for event #%s -> %s", event_id, filepath)

        except Exception as e:
            logger.warning(
                "Failed download for event #%s (attempt %s/%s): %s",
                event_id,
                getattr(event, "attempts", 1),
                getattr(event, "max_attempts", 3),
                e,
            )
            if part_filepath and os.path.exists(part_filepath):
                try:
                    os.remove(part_filepath)
                except Exception:
                    pass

            if event:
                try:
                    event.refresh_from_db()
                    if event.attempts >= event.max_attempts:
                        event.set_as_failed(error_message=str(e))
                        event.save()
                    else:
                        event.status = DownloadEvent.Status.QUEUED
                        event.error_message = str(e)
                        event.worker_pid = None
                        event.save(update_fields=["status", "error_message", "worker_pid"])
                except Exception as save_err:
                    logger.error("Failed to update status for failed event #%s: %s", event_id, save_err)

        finally:
            with self._lock:
                self._active_downloads.pop(event_id, None)
                remaining_dls = [
                    {
                        "filename": getattr(d, "filename", ""),
                        "speed": getattr(d, "speed", 0.0),
                        "index": getattr(d, "index", 0),
                        "total": getattr(d, "total", 0),
                        "downloaded": getattr(d, "downloaded", 0),
                        "filesize": getattr(d, "filesize", 0),
                    }
                    for d in self._active_downloads.values()
                    if getattr(d, "filename", "")
                ]
            try:
                from workers.models import SchedulerState

                st = SchedulerState.objects.filter(name=self.thread_name).first()
                if st:
                    extra = dict(st.extra_data or {})
                    extra["current_download"] = remaining_dls
                    st.extra_data = extra
                    st.save(update_fields=["extra_data"])
            except Exception:
                pass
            _safe_close_old_connections()

    def job(self) -> None:
        """Main scheduler loop."""
        while not self.stop.is_set():
            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            _safe_close_old_connections()
            try:
                self.process_queue()
            except Exception as e:
                logger.error("Error processing archive download queue: %s", e, exc_info=True)

            self.update_last_run(django_tz.now())

    def stop_running(self, force_local: bool = False) -> None:
        """Stop running scheduler and reset any locally active downloads to QUEUED."""
        super().stop_running(force_local=force_local)
        is_worker = force_local or getattr(self.settings, "is_worker_process", False)
        if is_worker:
            try:
                from workers.models import SchedulerState

                with self._lock:
                    active_ids = list(self._active_downloads.keys())
                    self._active_downloads.clear()
                if active_ids:
                    DownloadEvent.objects.filter(
                        id__in=active_ids,
                        status=DownloadEvent.Status.IN_PROGRESS,
                    ).update(
                        status=DownloadEvent.Status.QUEUED,
                        worker_pid=None,
                    )
                st = SchedulerState.objects.filter(name=self.thread_name).first()
                if st:
                    extra = dict(st.extra_data or {})
                    extra["current_download"] = []
                    st.extra_data = extra
                    st.save(update_fields=["extra_data"])
            except Exception as e:
                logger.warning("Error resetting active download events on stop: %s", e)
