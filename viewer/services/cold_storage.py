"""Archive cold tier in S3-compatible object storage.

An offloaded archive keeps a file name under ``cold_storage.local_prefix`` (``galleries/cold/<original name>``), so
the name alone says where the file is. That keeps a single source of truth (no extra column), keeps ``archive_auth``
and the ``zipped`` lookups working, and lets the bucket be mounted read-only at ``MEDIA_ROOT/<local_prefix>`` for the
tools that open the file directly (extraction, hashing, thumbnails...).

The object key is ``<key_prefix><original name>``, so restoring an archive puts it back at its original path.
"""

from __future__ import annotations

import logging
import os
import time
import typing
from dataclasses import dataclass
from os.path import basename
from urllib.parse import quote

from django.conf import settings
from django.db.models import Q

from core.base.cancellation import ping_activity
from core.base.s3 import S3Client, S3Error, S3ObjectInfo

if typing.TYPE_CHECKING:
    from django.db.models import QuerySet

    from core.base.config_models import ColdStorageSettings
    from viewer.models import Archive

logger = logging.getLogger(__name__)

ARCHIVE_CONTENT_TYPE = "application/vnd.comicbook+zip"

# Worker tasks (one archive each) and event log actions of the transfers.
OFFLOAD_TASK = "cold_storage_offload_worker"
RESTORE_TASK = "cold_storage_restore_worker"
OFFLOAD_EVENT = "COLD_STORAGE_OFFLOAD"
RESTORE_EVENT = "COLD_STORAGE_RESTORE"


class ColdStorageError(Exception):
    pass


def cold_settings() -> "ColdStorageSettings":
    return settings.CRAWLER_SETTINGS.cold_storage


def _prefix() -> str:
    return cold_settings().local_prefix.strip("/") + "/"


def is_cold_name(name: str | None) -> bool:
    """Whether an archive file name (relative to MEDIA_ROOT) belongs to the cold tier.

    Always False while the cold storage is disabled, so installs that don't use it are unaffected (even with a
    folder named like ``local_prefix``). Restore the archives before disabling it.
    """
    if not name or not cold_settings().enable:
        return False
    return name.replace("\\", "/").startswith(_prefix())


def is_available() -> bool:
    """Whether the cold storage is enabled and configured, so archives can be moved and offloaded ones served."""
    return cold_settings().is_configured()


def cold_name_q() -> Q:
    """Archives whose file is offloaded. Matches nothing while the cold storage is disabled (see ``is_cold_name``)."""
    if not cold_settings().enable:
        return Q(pk__in=[])
    return Q(zipped__startswith=_prefix())


def filter_by_storage(archives: "QuerySet[Archive]", storage: str | None) -> "QuerySet[Archive]":
    """``storage``: "cold" keeps the offloaded archives, "local" the others, anything else doesn't filter."""
    if storage == "cold":
        return archives.filter(cold_name_q())
    if storage == "local":
        return archives.exclude(cold_name_q())
    return archives


def offloadable(archives: "QuerySet[Archive]") -> "QuerySet[Archive]":
    """Archives that ``ColdStorageService.offload`` would move: a local file, processed (CRC32), not deleted."""
    return archives.exclude(cold_name_q()).exclude(zipped="").exclude(crc32="").filter(file_deleted=False)


def cold_name_for(local_name: str) -> str:
    """Archive file name an archive gets once offloaded."""
    local_name = local_name.replace("\\", "/").lstrip("/")
    if is_cold_name(local_name):
        raise ColdStorageError("{} is already in the cold tier".format(local_name))
    return _prefix() + local_name


def local_name_for(cold_name: str | None) -> str:
    """Original (local) file name of an offloaded archive."""
    if not cold_name or not is_cold_name(cold_name):
        raise ColdStorageError("{} is not in the cold tier".format(cold_name))
    return cold_name.replace("\\", "/")[len(_prefix()) :]


def object_key_for(cold_name: str | None) -> str:
    """Bucket key of an offloaded archive."""
    return cold_settings().key_prefix + local_name_for(cold_name)


_client_cache: dict[tuple[typing.Any, ...], S3Client] = {}


def get_client() -> S3Client:
    """S3 client for the configured bucket. Raises ColdStorageError if the cold storage isn't enabled/configured."""
    conf = cold_settings()
    if not conf.is_configured():
        raise ColdStorageError(
            "Cold storage is not enabled or configured (cold_storage: enable, endpoint_url, bucket, credentials)"
        )
    cache_key = (
        conf.endpoint_url,
        conf.bucket,
        conf.region,
        conf.access_key_id,
        conf.secret_access_key,
        conf.addressing_style,
        conf.timeout,
    )
    client = _client_cache.get(cache_key)
    if client is None:
        _client_cache.clear()
        client = S3Client(
            endpoint_url=conf.endpoint_url,
            bucket=conf.bucket,
            access_key_id=conf.access_key_id,
            secret_access_key=conf.secret_access_key,
            region=conf.region,
            addressing_style=conf.addressing_style,
            timeout=conf.timeout,
        )
        _client_cache[cache_key] = client
    return client


def should_redirect_download(archive: "Archive") -> bool:
    conf = cold_settings()
    return (
        conf.redirect_downloads
        and conf.is_configured()
        and is_cold_name(archive.zipped.name)
        and not archive.file_deleted
    )


def download_filename(archive: "Archive", original: bool) -> str:
    """Percent-encoded file name for a Content-Disposition ``filename*=UTF-8''`` value."""
    if original:
        return quote(basename(archive.zipped.name or ""))
    return archive.pretty_name


def presigned_download_url(archive: "Archive", original: bool = False) -> str:
    """Short-lived URL that downloads the archive straight from the bucket, with the usual file name."""
    conf = cold_settings()
    return get_client().presigned_get_url(
        object_key_for(archive.zipped.name),
        expires=conf.presigned_url_expiry,
        response_headers={
            "response-content-disposition": "attachment; filename*=UTF-8''{}".format(
                download_filename(archive, original)
            ),
            "response-content-type": ARCHIVE_CONTENT_TYPE,
        },
    )


@dataclass
class TransferResult:
    archive_id: int
    old_name: str
    new_name: str
    size: int
    skipped: bool = False
    message: str = ""


class ColdStorageService:
    """Moves archive files between the local media root and the bucket."""

    @staticmethod
    def _media_path(name: str) -> str:
        return os.path.join(settings.MEDIA_ROOT, name)

    @staticmethod
    def _swap_name(archive: "Archive", old_name: str, new_name: str) -> bool:
        """Point the archive at ``new_name``, only if it still points at ``old_name`` (another process could have
        renamed or deleted it meanwhile). A plain UPDATE: the file moved, nothing else changed."""
        from viewer.models import Archive

        updated = Archive.objects.filter(pk=archive.pk, zipped=old_name).update(zipped=new_name)
        if updated:
            archive.zipped.name = new_name
        return bool(updated)

    @classmethod
    def offload(
        cls,
        archive: "Archive",
        keep_local: bool = False,
        dry_run: bool = False,
        progress: typing.Callable[[int], None] | None = None,
    ) -> TransferResult:
        """Upload the archive's file to the bucket, check it arrived whole, point the archive at it and delete the
        local file (unless ``keep_local``)."""
        archive.refresh_from_db()
        old_name = archive.zipped.name or ""
        if not old_name:
            return TransferResult(archive.pk, old_name, old_name, 0, skipped=True, message="no file name")
        if is_cold_name(old_name):
            return TransferResult(archive.pk, old_name, old_name, 0, skipped=True, message="already offloaded")
        if not archive.crc32:
            # Still downloading or not processed yet.
            return TransferResult(archive.pk, old_name, old_name, 0, skipped=True, message="no CRC32 yet")
        if archive.file_deleted:
            return TransferResult(archive.pk, old_name, old_name, 0, skipped=True, message="file marked as deleted")
        local_path = cls._media_path(old_name)
        if not os.path.isfile(local_path):
            return TransferResult(archive.pk, old_name, old_name, 0, skipped=True, message="local file missing")

        new_name = cold_name_for(old_name)
        key = object_key_for(new_name)
        size = os.path.getsize(local_path)
        if dry_run:
            return TransferResult(archive.pk, old_name, new_name, size, message="dry run")

        conf = cold_settings()
        client = get_client()
        existing = client.head_object(key)
        if (
            existing is not None
            and existing.size == size
            and archive.crc32
            and existing.metadata.get("crc32", "").upper() == archive.crc32.upper()
        ):
            # Left by an earlier run that uploaded the file but didn't finish (e.g. interrupted).
            logger.info("Archive %s: %s already in the bucket with the same size and CRC32, not uploading", archive.pk, key)
        else:
            part_size = conf.part_size_mb * 1024 * 1024
            client.upload_file(
                local_path,
                key,
                content_type=ARCHIVE_CONTENT_TYPE,
                metadata={"archive-id": str(archive.pk), "crc32": archive.crc32 or ""},
                part_size=part_size,
                multipart_threshold=part_size,
                progress=progress,
            )
        cls._check_remote_size(client.head_object(key), key, size)

        if os.path.getsize(local_path) != size:
            raise ColdStorageError("{} changed while it was uploaded, not offloading it".format(local_path))

        if not cls._swap_name(archive, old_name, new_name):
            raise ColdStorageError(
                "Archive {} changed its file while it was uploaded, left it local (the object {} stays in the "
                "bucket and is reused or overwritten on the next offload)".format(archive.pk, key)
            )
        if not keep_local:
            try:
                os.remove(local_path)
            except OSError as e:
                logger.warning("Archive %s offloaded, but the local file %s couldn't be removed: %s", archive.pk, local_path, e)
        return TransferResult(archive.pk, old_name, new_name, size)

    @classmethod
    def restore(
        cls,
        archive: "Archive",
        keep_remote: bool = False,
        dry_run: bool = False,
        progress: typing.Callable[[int], None] | None = None,
    ) -> TransferResult:
        """Download an offloaded archive back to its original path, point the archive at it and delete the object
        (unless ``keep_remote``)."""
        archive.refresh_from_db()
        old_name = archive.zipped.name or ""
        if not is_cold_name(old_name):
            return TransferResult(archive.pk, old_name, old_name, 0, skipped=True, message="not offloaded")
        new_name = local_name_for(old_name)
        key = object_key_for(old_name)
        client = get_client()
        info = client.head_object(key)
        local_path = cls._media_path(new_name)
        if info is None:
            # Offloaded with keep_local and then lost from the bucket: the local copy is all there is.
            if os.path.isfile(local_path):
                if not dry_run and not cls._swap_name(archive, old_name, new_name):
                    raise ColdStorageError("Archive {} changed its file meanwhile".format(archive.pk))
                return TransferResult(
                    archive.pk, old_name, new_name, os.path.getsize(local_path), message="object missing, used local copy"
                )
            raise ColdStorageError("Archive {}: object {} is not in the bucket".format(archive.pk, key))
        if dry_run:
            return TransferResult(archive.pk, old_name, new_name, info.size, message="dry run")

        if os.path.exists(local_path):
            if not os.path.isfile(local_path) or os.path.getsize(local_path) != info.size:
                raise ColdStorageError(
                    "Archive {}: {} already exists and differs from the offloaded file".format(archive.pk, local_path)
                )
            logger.info("Archive %s: %s already exists with the same size, not downloading", archive.pk, local_path)
        else:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            partial_path = local_path + ".cold-part"
            try:
                written = client.download_file(key, partial_path, progress=progress)
                if written != info.size:
                    raise ColdStorageError(
                        "Archive {}: downloaded {} bytes of {} from {}".format(archive.pk, written, info.size, key)
                    )
                os.replace(partial_path, local_path)
            finally:
                if os.path.exists(partial_path):
                    os.remove(partial_path)

        if not cls._swap_name(archive, old_name, new_name):
            raise ColdStorageError(
                "Archive {} changed its file while it was restored, the downloaded file {} was left in place".format(
                    archive.pk, local_path
                )
            )
        if not keep_remote:
            try:
                client.delete_object(key)
            except S3Error as e:
                logger.warning("Archive %s restored, but the object %s couldn't be deleted: %s", archive.pk, key, e)
        return TransferResult(archive.pk, old_name, new_name, info.size)

    @staticmethod
    def _check_remote_size(info: S3ObjectInfo | None, key: str, size: int) -> None:
        if info is None:
            raise ColdStorageError("{} is not in the bucket after uploading it".format(key))
        if info.size != size:
            raise ColdStorageError("{} has {} bytes in the bucket, the local file has {}".format(key, info.size, size))

    @staticmethod
    def remote_info(archive: "Archive") -> S3ObjectInfo | None:
        return get_client().head_object(object_key_for(archive.zipped.name))

    @staticmethod
    def delete_remote(archive: "Archive") -> None:
        """Delete an offloaded archive's object. Raises ColdStorageError if the bucket isn't configured."""
        get_client().delete_object(object_key_for(archive.zipped.name))


def _activity_progress(
    activity_cb: typing.Callable[[], None] | None, interval: float = 5.0
) -> typing.Callable[[int], None] | None:
    """Progress callback that tells the worker's stall watchdog the transfer is alive (and stops it if the task was
    cancelled), at most every ``interval`` seconds."""
    if activity_cb is None:
        return None
    last: list[float | None] = [None]

    def progress(_: int) -> None:
        now = time.monotonic()
        if last[0] is None or now - last[0] >= interval:
            last[0] = now
            ping_activity(activity_cb)

    return progress


def run_transfer_task(
    archive_id: int,
    restore: bool,
    user: typing.Any = None,
    reason: str = "",
    activity_cb: typing.Callable[[], None] | None = None,
) -> TransferResult | None:
    """Offload (or restore) one archive from a worker task, and record it in the event log.

    Archives that don't need it (already moved, still downloading...) are skipped. Errors are raised, so the task is
    marked as failed with the message.
    """
    from viewer.models import Archive
    from viewer.utils.actions import event_log

    archive = Archive.objects.filter(pk=archive_id).first()
    if archive is None:
        logger.warning("Cold storage: archive %s doesn't exist anymore", archive_id)
        return None
    progress = _activity_progress(activity_cb)
    try:
        if restore:
            result = ColdStorageService.restore(archive, progress=progress)
        else:
            result = ColdStorageService.offload(archive, progress=progress)
    except S3Error as e:
        raise ColdStorageError("Archive {}: {}".format(archive_id, e)) from e
    if result.skipped:
        logger.info(
            "Cold storage: %s of archive %s skipped: %s", "restore" if restore else "offload", archive_id, result.message
        )
        return result
    logger.info(
        "Cold storage: archive %s %s, %s -> %s (%d bytes)",
        archive_id,
        "restored" if restore else "offloaded",
        result.old_name,
        result.new_name,
        result.size,
    )
    event_log(
        user,
        RESTORE_EVENT if restore else OFFLOAD_EVENT,
        reason=reason or None,
        content_object=archive,
        data="{} -> {}".format(result.old_name, result.new_name),
        result=result.message or "success",
    )
    return result
