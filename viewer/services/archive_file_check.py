"""Check that the files of archives still exist on disk and, optionally, that their content still matches the CRC32.

The check itself runs in a worker (``archive_file_check`` task, see ``workers.runner``). The web page only creates
the ``ArchiveFileCheck`` run, enqueues it, and polls its progress.
"""

import logging
import os
import re
import stat
import time
import typing
import zipfile
import zlib
from collections.abc import Callable

from django.conf import settings
from django.db.models import Q, QuerySet, Sum
import django.utils.timezone as django_tz

from core.base.cancellation import TaskCancelledError, ping_activity
from core.base.utilities import get_zip_filesize
from viewer.models import Archive, ArchiveFileCheck, ArchiveFileCheckIssue

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import AbstractBaseUser, AnonymousUser, User

logger = logging.getLogger(__name__)

TASK_TYPE = "archive_file_check"
READ_CHUNK_SIZE = 4 * 1024 * 1024
BATCH_SIZE = 500
PROGRESS_FLUSH_SECONDS = 1.0
MAX_ARCHIVE_IDS = 100_000
MAX_RANGE_SIZE = 100_000

_ID_TOKEN = re.compile(r"^(\d+)(?:-(\d+))?$")


def crc32_matches(stored: str, actual: str) -> bool:
    """Compare CRC32 values as numbers: older versions stored them without leading zeros ("%X", e.g. 33AC069)."""
    try:
        return int(stored, 16) == int(actual, 16)
    except ValueError:
        return stored.strip().upper() == actual.strip().upper()


def parse_archive_ids(text: str) -> list[int]:
    """Parse archive ids separated by commas or whitespace. Ranges (``10-20``) and archive URLs are accepted.

    :raises ValueError: on a token that is not an id, a range or an archive URL.
    """
    ids: list[int] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        url_match = re.search(r"/archive/(\d+)", token)
        if url_match:
            ids.append(int(url_match.group(1)))
            continue
        match = _ID_TOKEN.match(token)
        if not match:
            raise ValueError(f"'{token}' is not an archive id, a range (10-20) or an archive link.")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            start, end = end, start
        if end - start + 1 > MAX_RANGE_SIZE:
            raise ValueError(f"Range '{token}' is too large (max {MAX_RANGE_SIZE} ids).")
        ids.extend(range(start, end + 1))
        if len(ids) > MAX_ARCHIVE_IDS:
            raise ValueError(f"Too many archive ids (max {MAX_ARCHIVE_IDS}).")
    return list(dict.fromkeys(ids))


def normalize_folder(folder: str, media_root: str | None = None) -> str:
    """Turn a folder given by the user into a path relative to MEDIA_ROOT, as stored in ``Archive.zipped``.

    :raises ValueError: if the folder is empty or outside MEDIA_ROOT.
    """
    media_root = os.path.realpath(media_root or settings.MEDIA_ROOT)
    folder = folder.strip()
    if not folder:
        raise ValueError("Empty folder.")
    if os.path.isabs(folder):
        absolute = os.path.normpath(folder)
        if os.path.commonpath([absolute, media_root]) != media_root:
            raise ValueError(f"'{folder}' is not inside the media folder.")
        folder = os.path.relpath(absolute, media_root)
    relative = os.path.normpath(folder).replace("\\", "/").strip("/")
    if relative in ("", ".") or relative == ".." or relative.startswith("../"):
        raise ValueError(f"'{folder}' is not a subfolder of the media folder.")
    return relative


def archives_for_check(check: ArchiveFileCheck) -> QuerySet[Archive]:
    qs: QuerySet[Archive] = Archive.objects.all()
    if check.scope == ArchiveFileCheck.Scope.ARCHIVES:
        # Explicitly chosen archives are always checked, even if binned or marked as deleted.
        return qs.filter(pk__in=check.archive_ids or [])
    if check.scope == ArchiveFileCheck.Scope.FILTER:
        # The filter decides about binned and deleted archives ("recycled", "file_deleted" filters).
        from viewer.services.archive_selection import archives_matching

        return archives_matching(check.filter_params or {})
    qs = qs.filter(file_deleted=False)
    if not check.include_binned:
        qs = qs.filter(binned=False)
    if check.scope == ArchiveFileCheck.Scope.FOLDERS:
        folder_filter = Q()
        for folder in check.folders or []:
            folder_filter |= Q(zipped__startswith=folder.rstrip("/") + "/")
        if not folder_filter:
            return qs.none()
        qs = qs.filter(folder_filter)
    return qs


def create_file_check(
    scope: str,
    archive_ids: list[int] | None = None,
    folders: list[str] | None = None,
    filter_params: dict[str, str] | None = None,
    check_crc32: bool = False,
    test_zip: bool = False,
    include_binned: bool = False,
    user: "AbstractBaseUser | AnonymousUser | None" = None,
    priority: int = 10,
) -> ArchiveFileCheck:
    """Create a check run and enqueue it for the workers.

    ``filter_params`` must already be cleaned for the user (``archive_selection.clean_filter``).
    """
    from workers import client

    if scope not in ArchiveFileCheck.Scope.values:
        raise ValueError(f"Unknown scope: {scope!r}")
    if scope == ArchiveFileCheck.Scope.ARCHIVES and not archive_ids:
        raise ValueError("No archives given to check.")
    if scope == ArchiveFileCheck.Scope.FOLDERS and not folders:
        raise ValueError("No folders given to check.")
    filter_count = None
    if scope == ArchiveFileCheck.Scope.FILTER:
        from viewer.services.archive_selection import archives_matching

        filter_count = archives_matching(filter_params or {}).count()
        if not filter_count:
            raise ValueError("No archives match the filter.")

    run_user = typing.cast("User", user) if user is not None and getattr(user, "is_authenticated", False) else None
    check = ArchiveFileCheck.objects.create(
        scope=scope,
        archive_ids=list(archive_ids or []) if scope == ArchiveFileCheck.Scope.ARCHIVES else [],
        folders=list(folders or []) if scope == ArchiveFileCheck.Scope.FOLDERS else [],
        filter_params=dict(filter_params or {}) if scope == ArchiveFileCheck.Scope.FILTER else {},
        filter_count=filter_count,
        check_crc32=check_crc32,
        test_zip=check_crc32 and test_zip,
        include_binned=include_binned,
        user=run_user,
    )
    mode = "files and CRC32" if check_crc32 else "files"
    task = client.enqueue(
        args=[check.pk],
        task_type=TASK_TYPE,
        extra_options={"check_id": check.pk},
        user=run_user,
        reason=f"Archive file check #{check.pk} ({mode}): {check.scope_description}"[:500],
        event_action="ARCHIVE_FILE_CHECK",
        priority=priority,
        deduplicate=False,
    )
    check.task = task
    check.save(update_fields=["task", "updated_at"])
    return check


class _CheckRunner:
    def __init__(
        self,
        check: ArchiveFileCheck,
        activity_cb: Callable[[], None] | None,
        is_cancelled: Callable[[], bool] | None,
    ) -> None:
        self.check = check
        self.activity_cb = activity_cb
        self.is_cancelled = is_cancelled
        self.counters: dict[str, int] = {
            "checked": 0,
            "ok_count": 0,
            "missing_count": 0,
            "size_mismatch_count": 0,
            "crc32_mismatch_count": 0,
            "error_count": 0,
            "no_crc32_count": 0,
            "bytes_done": 0,
        }
        self.current_item = ""
        self.last_flush = 0.0

    def ensure_active(self) -> None:
        if callable(self.is_cancelled) and self.is_cancelled():
            raise TaskCancelledError(f"Archive file check #{self.check.pk} cancelled")
        ping_activity(self.activity_cb)

    def flush(self, force: bool = False, stopping: bool = False) -> None:
        """Save the progress. Raises ``TaskCancelledError`` if the run was cancelled meanwhile (it is no longer
        RUNNING), unless the run is already stopping."""
        now = time.monotonic()
        if not force and now - self.last_flush < PROGRESS_FLUSH_SECONDS:
            return
        self.last_flush = now
        runs = ArchiveFileCheck.objects.filter(pk=self.check.pk)
        if not stopping:
            runs = runs.filter(status=ArchiveFileCheck.Status.RUNNING)
        updated = runs.update(current_item=self.current_item[:500], updated_at=django_tz.now(), **self.counters)
        if not updated and not stopping:
            raise TaskCancelledError(f"Archive file check #{self.check.pk} is no longer running")

    def add_issue(self, archive: Archive, kind: str, counter: str, path: str = "", **fields: typing.Any) -> None:
        self.counters[counter] += 1
        ArchiveFileCheckIssue.objects.update_or_create(
            check_run=self.check, archive=archive, defaults={"kind": kind, "path": path[:500], **fields}
        )

    def crc32_of(self, path: str, expected_bytes: int) -> str:
        crc = 0
        read_bytes = 0
        with open(path, "rb") as f:
            while chunk := f.read(READ_CHUNK_SIZE):
                crc = zlib.crc32(chunk, crc)
                read_bytes += len(chunk)
                # Don't count past the expected size, so the progress stays consistent with bytes_total.
                if read_bytes <= expected_bytes:
                    self.counters["bytes_done"] += len(chunk)
                self.ensure_active()
                self.flush()
        return "%08X" % (crc & 0xFFFFFFFF)

    def check_archive(self, archive: Archive) -> None:
        expected_bytes = max(0, archive.filesize or 0)
        bytes_before = self.counters["bytes_done"]
        name = (archive.zipped.name if archive.zipped else "") or ""
        self.current_item = f"#{archive.pk}: {name}"
        try:
            self._check_archive(archive, name, expected_bytes)
        finally:
            self.counters["checked"] += 1
            # Count skipped (missing, wrong size) or partly read files as done, for a steady progress bar.
            if self.check.check_crc32:
                self.counters["bytes_done"] = bytes_before + expected_bytes

    def _check_archive(self, archive: Archive, name: str, expected_bytes: int) -> None:
        if not name:
            self.add_issue(archive, ArchiveFileCheckIssue.Kind.MISSING, "missing_count", message="No file path.")
            return
        try:
            path = archive.zipped.path
        except Exception as e:
            self.add_issue(archive, ArchiveFileCheckIssue.Kind.ERROR, "error_count", name, message=str(e))
            return
        try:
            file_stat = os.stat(path)
        except FileNotFoundError:
            self.add_issue(archive, ArchiveFileCheckIssue.Kind.MISSING, "missing_count", path)
            return
        except OSError as e:
            self.add_issue(archive, ArchiveFileCheckIssue.Kind.ERROR, "error_count", path, message=str(e))
            return
        if not stat.S_ISREG(file_stat.st_mode):
            self.add_issue(
                archive, ArchiveFileCheckIssue.Kind.MISSING, "missing_count", path, message="Not a regular file."
            )
            return
        expected_crc = (archive.crc32 or "").strip().upper()
        if self.check.check_crc32 and expected_crc:
            # The CRC32 covers the whole file, so it's the only check needed.
            self._check_crc32(archive, path, expected_crc, expected_bytes)
            return
        if self.check.check_crc32:
            self.counters["no_crc32_count"] += 1
        self._check_image_size(archive, path)

    def _check_image_size(self, archive: Archive, path: str) -> None:
        """Compare the stored ``filesize`` with the file's.

        ``filesize`` is not the size on disk: it's the uncompressed size of the images in the zip (as calculated by
        ``get_zip_filesize`` when the file info is recalculated), or -1 for a file that couldn't be read as a zip.
        Only the zip's file list is read, not the whole file.
        """
        if archive.filesize is None:
            self.counters["ok_count"] += 1
            return
        try:
            image_size = get_zip_filesize(path)
        except Exception as e:
            self.add_issue(
                archive,
                ArchiveFileCheckIssue.Kind.ERROR,
                "error_count",
                path,
                expected_size=archive.filesize,
                message=f"Error reading the zip file: {e}",
            )
            return
        if image_size == archive.filesize:
            self.counters["ok_count"] += 1
        elif image_size < 0:
            self.add_issue(
                archive,
                ArchiveFileCheckIssue.Kind.ERROR,
                "error_count",
                path,
                expected_size=archive.filesize,
                message="The file can't be read as a zip file anymore.",
            )
        else:
            self.add_issue(
                archive,
                ArchiveFileCheckIssue.Kind.SIZE_MISMATCH,
                "size_mismatch_count",
                path,
                expected_size=archive.filesize,
                actual_size=image_size,
            )

    def _check_crc32(self, archive: Archive, path: str, expected_crc: str, expected_bytes: int) -> None:
        try:
            actual_crc = self.crc32_of(path, expected_bytes)
        except TaskCancelledError:
            raise
        except OSError as e:
            self.add_issue(
                archive,
                ArchiveFileCheckIssue.Kind.ERROR,
                "error_count",
                path,
                expected_crc32=expected_crc,
                message=f"Error reading the file: {e}",
            )
            return
        if not crc32_matches(expected_crc, actual_crc):
            zip_test, message = ("", "")
            if self.check.test_zip:
                zip_test, message = self.test_zip(path)
            self.add_issue(
                archive,
                ArchiveFileCheckIssue.Kind.CRC32_MISMATCH,
                "crc32_mismatch_count",
                path,
                expected_crc32=expected_crc,
                actual_crc32=actual_crc,
                zip_test=zip_test,
                message=message,
            )
            return
        self.counters["ok_count"] += 1

    def test_zip(self, path: str) -> tuple[str, str]:
        """Test the zip's integrity: read every file in it, which checks each one against the CRC32 the zip stores.

        Like ``zipfile.ZipFile.testzip``, but it reports activity and can be cancelled while reading big files.

        :return: (``ArchiveFileCheckIssue.ZipTest`` value, message)
        """
        Result = ArchiveFileCheckIssue.ZipTest
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    try:
                        with zf.open(info) as member:
                            while member.read(READ_CHUNK_SIZE):
                                self.ensure_active()
                    except TaskCancelledError:
                        raise
                    except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError) as e:
                        return Result.FAILED, f"The zip is corrupted, {info.filename}: {e}"
        except TaskCancelledError:
            raise
        except (zipfile.BadZipFile, OSError, NotImplementedError) as e:
            return Result.NOT_ZIP, f"The file can't be read as a zip file: {e}"
        return Result.PASSED, (
            "The zip's contents are intact: the stored CRC32 is probably wrong, or the file was changed (e.g. "
            "repacked). Recalculate the file info to accept the file as it is now."
        )

    def run(self) -> None:
        archives = archives_for_check(self.check)
        total = archives.count()
        bytes_total = 0
        if self.check.check_crc32:
            # Only an estimate of the bytes to read: the stored sizes are the images' uncompressed sizes, close to the
            # size on disk for zips of (already compressed) images. Used to weight the progress.
            bytes_total = archives.filter(filesize__gt=0).aggregate(total=Sum("filesize"))["total"] or 0
        # A task re-queued after its worker died starts over.
        self.check.issues.all().delete()
        ArchiveFileCheck.objects.filter(pk=self.check.pk).update(
            status=ArchiveFileCheck.Status.RUNNING,
            started_at=django_tz.now(),
            finished_at=None,
            error_message="",
            total=total,
            bytes_total=bytes_total,
            updated_at=django_tz.now(),
            **{k: 0 for k in self.counters},
        )
        logger.info(
            "Archive file check #%d started: %d archives, CRC32: %s", self.check.pk, total, self.check.check_crc32
        )

        last_pk = 0
        while True:
            batch = list(
                archives.filter(pk__gt=last_pk)
                .order_by("pk")
                .only("pk", "zipped", "filesize", "crc32")[:BATCH_SIZE]
            )
            if not batch:
                break
            for archive in batch:
                self.ensure_active()
                self.check_archive(archive)
                self.flush()
            last_pk = batch[-1].pk

        self.current_item = ""
        self.flush(force=True)
        ArchiveFileCheck.objects.filter(pk=self.check.pk).update(
            status=ArchiveFileCheck.Status.COMPLETED, finished_at=django_tz.now(), updated_at=django_tz.now()
        )
        logger.info("Archive file check #%d finished: %s", self.check.pk, self.counters)


def run_file_check(
    check_id: int,
    activity_cb: Callable[[], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> ArchiveFileCheck:
    """Run an archive file check. Called by the worker that claimed its task."""
    check = ArchiveFileCheck.objects.get(pk=check_id)
    if check.status == ArchiveFileCheck.Status.CANCELLED:
        logger.info("Archive file check #%d was cancelled before it started.", check_id)
        return check
    runner = _CheckRunner(check, activity_cb, is_cancelled)
    try:
        runner.run()
    except TaskCancelledError:
        runner.current_item = ""
        runner.flush(force=True, stopping=True)
        ArchiveFileCheck.objects.filter(pk=check.pk).update(
            status=ArchiveFileCheck.Status.CANCELLED, finished_at=django_tz.now(), updated_at=django_tz.now()
        )
        logger.info("Archive file check #%d cancelled after %d archives.", check.pk, runner.counters["checked"])
        raise
    except Exception as e:
        runner.current_item = ""
        runner.flush(force=True, stopping=True)
        ArchiveFileCheck.objects.filter(pk=check.pk).update(
            status=ArchiveFileCheck.Status.FAILED,
            finished_at=django_tz.now(),
            error_message=str(e)[:2000],
            updated_at=django_tz.now(),
        )
        raise
    check.refresh_from_db()
    return check
