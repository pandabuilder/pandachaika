import logging
import os
from typing import Any
from zipfile import BadZipFile, ZipFile

from django.conf import settings as django_settings
from django.db.models import Q
from django.utils import timezone as django_tz

from viewer.models.archive import Archive
from viewer.models.events import DownloadEvent

logger = logging.getLogger(__name__)


def reconcile_downloads(
    dry_run: bool = False,
    requeue_failed: bool = True,
    recycle_orphaned: bool = True,
    recycle_failed: bool = True,
    check_disk: bool = True,
    settings: Any = None,
) -> dict[str, int]:
    """
    Reconciles out-of-sync Archives and DownloadEvents.

    Finds archives matching indirect download filters (torrent/hath) with empty CRC32
    and synchronizes their state with disk/staging storage and DownloadEvents:
    - Rescues archives whose files exist on disk or in the torrent download dir (calculates CRC32).
    - Recycles empty archives whose linked DownloadEvent has failed or been cancelled.
    - Recycles empty archives that were falsely marked completed but have no physical files.
    - Recycles orphaned archives that have no DownloadEvent and no file.
    - Optionally requeues failed archives if requeue_failed_downloads is enabled.
    - Leaves legitimately in-progress downloads untouched.
    """
    if settings is None:
        settings = getattr(django_settings, "CRAWLER_SETTINGS", None)

    candidates = (
        Archive.objects.filter(
            Q(crc32="") & (Q(match_type__startswith="torrent") | Q(match_type__startswith="hath")) & Q(binned=False)
        )
        .select_related("gallery")
        .prefetch_related("downloadevent_set")
    )

    stats = {
        "total_checked": 0,
        "rescued_from_disk": 0,
        "rescued_from_staging": 0,
        "recycled_failed": 0,
        "recycled_orphaned": 0,
        "recycled_false_completed": 0,
        "requeued": 0,
        "kept_in_progress": 0,
    }

    torrent_download_dir = ""
    if settings and hasattr(settings, "torrent") and isinstance(settings.torrent, dict):
        torrent_download_dir = settings.torrent.get("download_dir", "")

    post_downloader = None
    if settings:
        try:
            from core.downloaders.postdownload import PostDownloader

            post_downloader = PostDownloader(settings, web_queue=getattr(settings.workers, "web_queue", None))
        except Exception as e:
            logger.warning("Could not initialize PostDownloader for reconciliation: %s", e)

    for archive in candidates:
        stats["total_checked"] += 1
        archive_rescued = False

        # 1. Check if the file is already on local disk
        if check_disk and archive.zipped and archive.zipped.name:
            try:
                zip_path = archive.zipped.path
                if os.path.isfile(zip_path) and os.path.getsize(zip_path) > 0:
                    try:
                        with ZipFile(zip_path, "r") as zf:
                            if zf.testzip() is None:
                                archive_rescued = True
                    except (BadZipFile, Exception):
                        pass

                    if archive_rescued:
                        if not dry_run:
                            archive.recalc_fileinfo()
                            archive.ingest()
                            DownloadEvent.objects.filter(archive=archive, completed=False).update(
                                progress=100.0,
                                completed=True,
                                status=DownloadEvent.Status.COMPLETED,
                                completed_date=django_tz.now(),
                            )
                        stats["rescued_from_disk"] += 1
                        logger.info("Reconciliation: Rescued archive #%s from local disk (%s)", archive.pk, zip_path)
                        continue
            except Exception as e:
                logger.debug("Reconciliation disk check error for archive #%s: %s", archive.pk, e)

        # 2. Check if the file exists in the torrent staging dir
        if (
            check_disk
            and torrent_download_dir
            and archive.gallery
            and archive.match_type
            and "torrent" in archive.match_type
        ):
            gid_folder = os.path.join(torrent_download_dir, str(archive.gallery.gid))
            if os.path.isdir(gid_folder) and os.listdir(gid_folder):
                if post_downloader and not dry_run:
                    try:
                        post_downloader.transfer_all_missing((archive,))
                        archive.refresh_from_db(fields=["crc32"])
                        if archive.crc32:
                            stats["rescued_from_staging"] += 1
                            logger.info(
                                "Reconciliation: Rescued archive #%s from torrent staging dir (%s)",
                                archive.pk,
                                gid_folder,
                            )
                            continue
                    except Exception as e:
                        logger.warning("Reconciliation transfer error for archive #%s: %s", archive.pk, e)

        # 3. Analyze DownloadEvents
        events = list(archive.downloadevent_set.all())

        if not events:
            # Orphaned archive: no download event, no file
            if recycle_orphaned:
                if not dry_run:
                    archive.move_to_recycle_bin(
                        reason="Reconciliation: orphaned archive without download event or file"
                    )
                stats["recycled_orphaned"] += 1
                logger.info("Reconciliation: Recycled orphaned archive #%s", archive.pk)
            continue

        # Check for active / in-progress events
        active_events = [
            ev
            for ev in events
            if not ev.completed
            and ev.status
            not in (DownloadEvent.Status.COMPLETED, DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED)
        ]

        if active_events:
            # Genuinely in progress
            stats["kept_in_progress"] += 1
            continue

        # Sort by create_date descending to find the latest event
        events.sort(key=lambda x: x.create_date or django_tz.now(), reverse=True)
        latest_event = events[0]

        effective_status = latest_event.effective_status

        # Failed or Cancelled event
        if effective_status in (DownloadEvent.Status.FAILED, DownloadEvent.Status.CANCELLED) or latest_event.failed:
            if recycle_failed:
                if not dry_run:
                    archive.move_to_recycle_bin(
                        reason=f"Reconciliation: linked download event #{latest_event.id} was {effective_status}"
                    )
                    if requeue_failed and post_downloader and archive.gallery:
                        if getattr(settings, "requeue_failed_downloads", True):
                            try:
                                requeue_dl = post_downloader.get_requeue_downloader()
                                post_downloader._requeue_archive(archive, requeue_dl)
                                stats["requeued"] += 1
                                logger.info("Reconciliation: Requeued archive #%s via fallback", archive.pk)
                            except Exception as e:
                                logger.error("Failed to requeue archive #%s: %s", archive.pk, e)
                stats["recycled_failed"] += 1
                logger.info("Reconciliation: Recycled failed archive #%s (event #%s)", archive.pk, latest_event.id)
            continue

        # False completion: event is completed, but archive has no file and crc32 is empty
        if effective_status == DownloadEvent.Status.COMPLETED or latest_event.completed:
            if not dry_run:
                latest_event.set_as_failed("Torrent completed in client but file was missing from storage")
                latest_event.save()
                archive.move_to_recycle_bin(
                    reason=f"Reconciliation: download event #{latest_event.id} completed but file was missing"
                )
                if requeue_failed and post_downloader and archive.gallery:
                    if getattr(settings, "requeue_failed_downloads", True):
                        try:
                            requeue_dl = post_downloader.get_requeue_downloader()
                            post_downloader._requeue_archive(archive, requeue_dl)
                            stats["requeued"] += 1
                            logger.info("Reconciliation: Requeued archive #%s via fallback", archive.pk)
                        except Exception as e:
                            logger.error("Failed to requeue archive #%s: %s", archive.pk, e)
            stats["recycled_false_completed"] += 1
            logger.info("Reconciliation: Fixed false-completed archive #%s (event #%s)", archive.pk, latest_event.id)
            continue

    return stats
