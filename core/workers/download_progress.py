import logging
import os
from itertools import groupby
from typing import Optional

import django.utils.timezone as django_tz
from django.db import close_old_connections

from core.base.setup import Settings
from core.downloaders.postdownload import PostDownloader
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


class DownloadProgressChecker(BaseScheduler):

    thread_name = "download_progress_checker"
    TORRENT_METHODS = ["torrent", "torrent_api"]
    HATH_METHODS = ["hath"]
    ARCHIVE_METHODS = ["archive", "archive_js", "gallerydl"]

    def __init__(self, settings: Settings, web_queue=None, timer=1, pk=None):
        super().__init__(settings, web_queue, timer, pk)

    @staticmethod
    def timer_to_seconds(timer: float) -> float:
        return timer

    def check_torrent_progress(
        self, download_event: DownloadEvent, download_progress: Optional[float] = None
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

        is_completed = (download_event.progress >= 100 or archive_completed)

        if is_completed:
            download_event.finish_download()
        elif self.settings.max_torrent_lifespan > 0 and not archive_completed:
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
        while not self.stop.is_set():

            seconds_to_wait = self.wait_until_next_run()
            if self.stop.wait(timeout=seconds_to_wait):
                return

            close_old_connections()

            try:
                download_events_all = DownloadEvent.objects.in_progress().select_related("archive")

                for download_method, download_events_iter in groupby(
                    download_events_all.order_by("method", "download_id"), lambda x: x.method
                ):
                    download_events = list(download_events_iter)
                    if download_method in self.TORRENT_METHODS:
                        fail_missing = (self.settings.max_torrent_lifespan <= 0)
                        mark_completed_and_failed_for_indirect_downloads(
                            download_events, fail_missing_archive=fail_missing
                        )
                        events_to_check = [(x.download_id, x) for x in download_events if not x.completed]
                        if events_to_check:
                            client = get_torrent_client(self.settings.torrent)
                            checked_events = set()
                            if client:
                                try:
                                    if client.connect():
                                        download_progresses = client.get_download_progress(events_to_check)
                                        for download_event, download_progress in download_progresses:
                                            checked_events.add(download_event.pk)
                                            self.check_torrent_progress(download_event, download_progress)
                                except Exception as e:
                                    logger.error(f"Error querying torrent client for progress: {e}")

                            # For any events not returned by the client (e.g. client offline, torrent removed from client, etc.),
                            # check lifespan so expired/dead torrents are purged and requeued!
                            for _, download_event in events_to_check:
                                if download_event.pk not in checked_events and not download_event.completed:
                                    try:
                                        self.check_torrent_progress(download_event, None)
                                    except Exception as e:
                                        logger.error(f"Error checking torrent lifespan for {download_event.name}: {e}")
                    elif download_method in self.HATH_METHODS:
                        mark_completed_and_failed_for_indirect_downloads(download_events)
                        archives_to_check = [(x.archive, x) for x in download_events if x.archive and not x.completed]
                        if archives_to_check:
                            post_downloader = PostDownloader(self.settings)
                            post_download_progresses = post_downloader.check_download_progress_archives(archives_to_check)
                            for download_event, download_progress in post_download_progresses.items():
                                download_event.progress = download_progress * 100
                                if download_event.progress >= 100:
                                    download_event.finish_download()
                                download_event.save()
                    elif download_method in self.ARCHIVE_METHODS:
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
            except Exception as e:
                logger.error(f"Error in DownloadProgressChecker job cycle: {e}")

            self.update_last_run(django_tz.now())
