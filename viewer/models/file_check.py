import typing

from django.conf import settings
from django.db import models
import django.utils.timezone as django_tz


class ArchiveFileCheck(models.Model):
    """One run of the archive file check: whether the files of a set of archives exist and are intact.

    The run is done by a worker (``archive_file_check`` task), which keeps the progress fields up to date so the web
    page can poll them. Only problems are stored (as ``ArchiveFileCheckIssue``), not one row per checked archive.
    """

    class Scope(models.TextChoices):
        ALL = "all", "All archives"
        ARCHIVES = "archives", "Specific archives"
        FOLDERS = "folders", "Media subfolders"
        FILTER = "filter", "Archives matching a filter"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    FINISHED_STATUSES = (Status.COMPLETED, Status.FAILED, Status.CANCELLED)

    scope = models.CharField(max_length=20, choices=Scope.choices, default=Scope.ALL)
    archive_ids = models.JSONField(default=list, blank=True, help_text="Archive ids, for the 'archives' scope.")
    folders = models.JSONField(
        default=list, blank=True, help_text="Folders relative to MEDIA_ROOT, for the 'folders' scope."
    )
    filter_params = models.JSONField(
        default=dict, blank=True, help_text="Archive manager filter, for the 'filter' scope. Applied when the run starts."
    )
    filter_count = models.PositiveIntegerField(
        null=True, blank=True, help_text="Archives that matched the filter when the check was created."
    )
    check_crc32 = models.BooleanField(default=False, help_text="Read every file and compare its CRC32.")
    test_zip = models.BooleanField(
        default=False, help_text="When the CRC32 doesn't match, test the zip's integrity (the CRC32 of each file in it)."
    )
    include_binned = models.BooleanField(default=False)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED, db_index=True)
    task = models.ForeignKey(
        "workers.TaskQueue", null=True, blank=True, on_delete=models.SET_NULL, related_name="archive_file_checks"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    total = models.PositiveIntegerField(default=0)
    checked = models.PositiveIntegerField(default=0)
    ok_count = models.PositiveIntegerField(default=0)
    missing_count = models.PositiveIntegerField(default=0)
    size_mismatch_count = models.PositiveIntegerField(default=0)
    crc32_mismatch_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    no_crc32_count = models.PositiveIntegerField(
        default=0, help_text="Archives without a stored CRC32, so their content could not be verified."
    )
    bytes_total = models.BigIntegerField(default=0, help_text="Estimated bytes to read for a CRC32 check (sum of the stored image sizes), to weight the progress.")
    bytes_done = models.BigIntegerField(default=0)
    current_item = models.CharField(max_length=500, blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(default=django_tz.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"ArchiveFileCheck #{self.pk} ({self.scope}) - {self.status}"

    @property
    def is_finished(self) -> bool:
        return self.status in self.FINISHED_STATUSES

    @property
    def issue_count(self) -> int:
        return self.missing_count + self.size_mismatch_count + self.crc32_mismatch_count + self.error_count

    @property
    def scope_description(self) -> str:
        if self.scope == self.Scope.ARCHIVES:
            ids = [str(x) for x in self.archive_ids or []]
            shown = ", ".join(ids[:10])
            if len(ids) > 10:
                shown += f" (+{len(ids) - 10} more)"
            return f"{len(ids)} archive(s): {shown}"
        if self.scope == self.Scope.FOLDERS:
            return "Folders: " + ", ".join(self.folders or [])
        if self.scope == self.Scope.FILTER:
            from viewer.services.archive_selection import describe_filter

            matched = f" ({self.filter_count} when created)" if self.filter_count is not None else ""
            return f"Filter{matched}: {describe_filter(self.filter_params or {})}"
        return "All archives"

    def sync_with_task(self) -> bool:
        """Mark the run finished when its task ended without the worker updating it (worker died, task cancelled).

        :return: whether the status changed.
        """
        if self.is_finished or self.task is None:
            return False
        from workers.models import TaskQueue

        task_status = self.task.status
        new_status = None
        if task_status == TaskQueue.Status.CANCELLED:
            new_status = self.Status.CANCELLED
        elif task_status == TaskQueue.Status.FAILED:
            new_status = self.Status.FAILED
        elif task_status == TaskQueue.Status.COMPLETED:
            # The worker always finishes the run before the task, so this means it was interrupted.
            new_status = self.Status.FAILED
        if new_status is None:
            return False
        self.status = new_status
        self.finished_at = self.task.finished_at or django_tz.now()
        if new_status == self.Status.FAILED and not self.error_message:
            self.error_message = self.task.error_message or "The worker task ended before the check finished."
        self.current_item = ""
        self.save(update_fields=["status", "finished_at", "error_message", "current_item", "updated_at"])
        return True

    def progress_dict(self) -> dict[str, typing.Any]:
        percent = 0.0
        if self.check_crc32 and self.bytes_total > 0:
            percent = min(100.0, 100.0 * self.bytes_done / self.bytes_total)
        elif self.total > 0:
            percent = min(100.0, 100.0 * self.checked / self.total)
        if self.status == self.Status.COMPLETED:
            percent = 100.0
        elapsed = None
        if self.started_at:
            elapsed = ((self.finished_at or django_tz.now()) - self.started_at).total_seconds()
        return {
            "id": self.pk,
            "status": self.status,
            "status_display": self.get_status_display(),
            "is_finished": self.is_finished,
            "scope": self.scope,
            "scope_description": self.scope_description,
            "check_crc32": self.check_crc32,
            "test_zip": self.test_zip,
            "include_binned": self.include_binned,
            "filter_count": self.filter_count,
            "task_id": self.task_id,
            "task_status": self.task.status if self.task else None,
            "user": self.user.username if self.user else None,
            "total": self.total,
            "checked": self.checked,
            "ok_count": self.ok_count,
            "missing_count": self.missing_count,
            "size_mismatch_count": self.size_mismatch_count,
            "crc32_mismatch_count": self.crc32_mismatch_count,
            "error_count": self.error_count,
            "no_crc32_count": self.no_crc32_count,
            "issue_count": self.issue_count,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "percent": round(percent, 2),
            "current_item": self.current_item,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
        }


class ArchiveFileCheckIssue(models.Model):
    """A problem found with an archive's file during an ``ArchiveFileCheck``."""

    class Kind(models.TextChoices):
        MISSING = "missing", "File missing"
        # The uncompressed size of the images in the zip (Archive.filesize), not the size of the file on disk.
        SIZE_MISMATCH = "size_mismatch", "Image size mismatch"
        CRC32_MISMATCH = "crc32_mismatch", "CRC32 mismatch"
        ERROR = "error", "Read error"

    class ZipTest(models.TextChoices):
        PASSED = "passed", "Zip intact"
        FAILED = "failed", "Zip corrupted"
        NOT_ZIP = "not_zip", "Not a zip"

    check_run = models.ForeignKey(ArchiveFileCheck, on_delete=models.CASCADE, related_name="issues")
    archive = models.ForeignKey("viewer.Archive", on_delete=models.CASCADE, related_name="file_check_issues")
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    path = models.CharField(max_length=500, blank=True, default="")
    expected_size = models.BigIntegerField(null=True, blank=True)
    actual_size = models.BigIntegerField(null=True, blank=True)
    expected_crc32 = models.CharField(max_length=10, blank=True, default="")
    actual_crc32 = models.CharField(max_length=10, blank=True, default="")
    zip_test = models.CharField(
        max_length=10,
        choices=ZipTest.choices,
        blank=True,
        default="",
        db_index=True,
        help_text="Result of testing the zip's integrity, after a CRC32 mismatch.",
    )
    message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=django_tz.now)

    class Meta:
        ordering = ["-archive_id"]
        constraints = [
            models.UniqueConstraint(fields=["check_run", "archive"], name="unique_file_check_issue_per_archive"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: archive {self.archive_id} (check #{self.check_run_id})"
