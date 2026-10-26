import typing
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import QuerySet
import django.utils.timezone as django_tz

from core.base.types import DataDict


class EventLog(models.Model):
    content_type = models.ForeignKey(ContentType, null=True, on_delete=models.SET_NULL)
    object_id = models.PositiveIntegerField(null=True)
    content_object = GenericForeignKey("content_type", "object_id")
    action = models.CharField(max_length=50, db_index=True)
    reason = models.CharField(max_length=200, blank=True, null=True, default="")
    data = models.CharField(max_length=2000, blank=True, null=True, default="")
    result = models.CharField(max_length=200, blank=True, null=True, default="")
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    create_date = models.DateTimeField(default=django_tz.now, db_index=True)

    class Meta:
        verbose_name_plural = "Event logs"
        ordering = ["-create_date"]
        permissions = (
            ("read_all_logs", "Can view a general log from all users"),
            ("read_activity_logs", "Can view a general log for all activity, no users"),
        )


# Generalization of the Auto_Search system, allowing multiple links per provider, and different frequencies.
# If there's pagination, it's handled by the parser. In that case, you should specify a stop page to avoid parsing all
# pages available on a feed.
# For now, we only deal with a URL and proxy as attributes passed down to the crawler,
# so it can work with already existing parsers.
# There should be a generic parser, with that we can enable the more generic parameters: query object, post, etc

class MonitoredLink(models.Model):
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=2000)
    description = models.TextField(blank=True, default="")
    # Disabled until a generic handler for this parameters is implemented.
    # query_object = models.JSONField(blank=True, null=True)
    # post_request = models.BooleanField(default=False)
    # post_body = models.JSONField(blank=True, null=True)

    # This field could be considered redundant, each link should be associated automatically with a provider.
    provider = models.ForeignKey("viewer.Provider", on_delete=models.SET_NULL, null=True, blank=True)
    # Overrides whatever is setup on OwnSettings for each provider.
    proxy = models.CharField(max_length=200, blank=True, null=True)
    stop_page = models.IntegerField(blank=True, null=True)

    enabled = models.BooleanField(default=False)
    auto_start = models.BooleanField(default=False)
    frequency = models.DurationField()

    use_limited_wanted_galleries = models.BooleanField(default=False)
    limited_wanted_galleries = models.ManyToManyField("viewer.WantedGallery", blank=True)

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    create_date = models.DateTimeField(default=django_tz.now, db_index=True)
    last_modified = models.DateTimeField(auto_now=True, blank=True, null=True)

    class Meta:
        verbose_name_plural = "Monitored Links"
        ordering = ["-create_date"]
        permissions = (("view_monitored_links", "Can view a general view of all monitored links"),)

    def __str__(self) -> str:
        return self.name

    @property
    def scheduler_name(self) -> str:
        return f"link_monitor_{self.pk}"

    @property
    def is_running(self) -> bool:
        """Check whether this monitored link's scheduler is currently active."""
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if getattr(crawler_settings, "is_worker_process", False):
            workers = getattr(settings, "WORKERS", None)
            if workers:
                for lm in getattr(workers, "timed_link_monitors", []):
                    if lm.monitored_link.pk == self.pk:
                        return lm.is_running_locally()
        from workers.models import SchedulerState
        return SchedulerState.is_scheduler_running(self.scheduler_name)

    def get_scheduler_state(self) -> typing.Any | None:
        from workers.models import SchedulerState
        return SchedulerState.objects.filter(name=self.scheduler_name).first()

    def start_running(self) -> None:
        """Start running this monitored link. In webapp, dispatches command to worker process."""
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if getattr(crawler_settings, "is_worker_process", False):
            workers = getattr(settings, "WORKERS", None)
            if workers:
                for lm in getattr(workers, "timed_link_monitors", []):
                    if lm.monitored_link.pk == self.pk:
                        lm.start_running(timer=self.frequency.total_seconds(), force_local=True)
                        return
        from workers.models import SchedulerState
        SchedulerState.request_command(self.scheduler_name, "start", str(self.frequency.total_seconds()))

    def force_run(self) -> None:
        """Force an immediate execution of this monitored link."""
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if getattr(crawler_settings, "is_worker_process", False):
            workers = getattr(settings, "WORKERS", None)
            if workers:
                for lm in getattr(workers, "timed_link_monitors", []):
                    if lm.monitored_link.pk == self.pk:
                        lm.force_run(force_local=True)
                        return
        from workers.models import SchedulerState
        SchedulerState.request_command(self.scheduler_name, "force_run")

    def stop_running(self) -> None:
        """Stop running this monitored link."""
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if getattr(crawler_settings, "is_worker_process", False):
            workers = getattr(settings, "WORKERS", None)
            if workers:
                for lm in getattr(workers, "timed_link_monitors", []):
                    if lm.monitored_link.pk == self.pk:
                        lm.stop_running(force_local=True)
                        return
        from workers.models import SchedulerState
        SchedulerState.request_command(self.scheduler_name, "stop")

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        is_new = self.pk is None
        super().save(*args, **kwargs)
        from workers.models import SchedulerState

        SchedulerState.objects.update_or_create(
            name=self.scheduler_name,
            defaults={
                "display_name": f"MonitoredLink: {self.name}",
                "schedule_type": "scheduler",
                "cycle_timer": self.frequency.total_seconds(),
            },
        )

        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        is_worker = bool(getattr(crawler_settings, "is_worker_process", False))

        if is_worker:
            workers = getattr(settings, "WORKERS", None)
            if workers:
                is_present = False
                for timed_link_monitor in workers.timed_link_monitors:
                    if timed_link_monitor.monitored_link.pk == self.pk:
                        is_present = True
                        timed_link_monitor.monitored_link = self
                        timed_link_monitor.timer = self.frequency.total_seconds()
                        timed_link_monitor.original_timer = self.frequency
                        timed_link_monitor.link_name = self.name
                        if not self.enabled:
                            timed_link_monitor.stop_running(force_local=True)
                        elif timed_link_monitor.is_running_locally():
                            timed_link_monitor.stop_running(force_local=True)
                            timed_link_monitor.start_running(force_local=True)
                        break
                if not is_present and getattr(getattr(crawler_settings, "monitored_links", None), "enable", False):
                    workers.add_new_link_monitor(crawler_settings, self, start_thread=self.auto_start and self.enabled)
        else:
            if self.enabled:
                if is_new and self.auto_start:
                    SchedulerState.request_command(self.scheduler_name, "start", str(self.frequency.total_seconds()))
                else:
                    SchedulerState.request_command(self.scheduler_name, "reload", str(self.frequency.total_seconds()))
            else:
                SchedulerState.request_command(self.scheduler_name, "stop")

    def delete(self, *args: typing.Any, **kwargs: typing.Any) -> tuple[int, dict[str, int]]:
        sched_name = self.scheduler_name
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        is_worker = bool(getattr(crawler_settings, "is_worker_process", False))

        if is_worker:
            workers = getattr(settings, "WORKERS", None)
            if workers:
                for timed_link_monitor in list(workers.timed_link_monitors):
                    if timed_link_monitor.monitored_link.pk == self.pk:
                        timed_link_monitor.stop_running(force_local=True)
                        workers.timed_link_monitors.remove(timed_link_monitor)
            from workers.models import SchedulerState
            SchedulerState.objects.filter(name=sched_name).delete()
        else:
            from workers.models import SchedulerState
            SchedulerState.request_command(sched_name, "delete")

        deleted = super().delete(*args, **kwargs)
        return deleted



class DownloadEventQuerySet(models.QuerySet):
    def in_progress(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(completed=False, **kwargs)

    def queued(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(completed=False, failed=False, status=DownloadEvent.Status.QUEUED, **kwargs)

    def downloading(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(completed=False, failed=False, status=DownloadEvent.Status.IN_PROGRESS, **kwargs)

    def completed(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(completed=True, failed=False, **kwargs)

    def failed(self, **kwargs: typing.Any) -> QuerySet:
        return self.filter(failed=True, **kwargs)


class DownloadEventManager(models.Manager["DownloadEvent"]):
    def get_queryset(self) -> DownloadEventQuerySet:
        return DownloadEventQuerySet(self.model, using=self._db)

    def in_progress(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().in_progress(**kwargs)

    def queued(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().queued(**kwargs)

    def downloading(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().downloading(**kwargs)

    def completed(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().completed(**kwargs)

    def failed(self, **kwargs: typing.Any) -> QuerySet:
        return self.get_queryset().failed(**kwargs)



class DownloadEvent(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    name = models.CharField()
    archive = models.ForeignKey("viewer.Archive", on_delete=models.SET_NULL, blank=True, null=True)
    gallery = models.ForeignKey("viewer.Gallery", on_delete=models.SET_NULL, blank=True, null=True)
    progress = models.FloatField(default=0.0)
    total_size = models.BigIntegerField(default=0)
    failed = models.BooleanField(default=False)
    completed = models.BooleanField(default=False)
    method = models.CharField()
    agent = models.CharField()
    download_id = models.CharField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
        db_index=True,
    )
    provider = models.CharField(max_length=50, blank=True, default="", db_index=True)
    download_url = models.TextField(blank=True, default="")
    request_options = models.JSONField(default=dict, blank=True)
    attempts = models.IntegerField(default=0)
    max_attempts = models.IntegerField(default=3)
    error_message = models.TextField(blank=True, default="")
    priority = models.IntegerField(default=10, db_index=True)
    worker_pid = models.IntegerField(blank=True, null=True)
    create_date = models.DateTimeField(auto_now_add=True)
    started_date = models.DateTimeField("Started date", blank=True, null=True)
    completed_date = models.DateTimeField("Posted date", blank=True, null=True)

    objects = DownloadEventManager()

    @property
    def effective_status(self) -> str:
        """Return the effective status resolving any discrepancies with completed/failed booleans."""
        if self.completed:
            if self.failed:
                if self.status in (self.Status.FAILED, self.Status.CANCELLED):
                    return self.status
                return self.Status.FAILED
            if self.status != self.Status.COMPLETED:
                return self.Status.COMPLETED
            return self.status
        if self.failed:
            if self.status in (self.Status.FAILED, self.Status.CANCELLED):
                return self.status
            return self.Status.FAILED
        return self.status

    def sync_status_with_flags(self) -> None:
        """Synchronize status and completed/failed booleans."""
        if self.completed:
            if self.failed:
                if self.status not in (self.Status.FAILED, self.Status.CANCELLED):
                    self.status = self.Status.FAILED
            else:
                if self.status != self.Status.COMPLETED:
                    self.status = self.Status.COMPLETED
        elif self.failed:
            self.completed = True
            if self.status not in (self.Status.FAILED, self.Status.CANCELLED):
                self.status = self.Status.FAILED
        elif self.status == self.Status.COMPLETED:
            self.completed = True
        elif self.status in (self.Status.FAILED, self.Status.CANCELLED):
            self.completed = True
            self.failed = True

    def _recycle_empty_archive(self, reason: str = "") -> None:
        if self.archive_id:
            try:
                archive = self.archive
                if archive and not archive.crc32 and not archive.is_recycled():
                    qs = DownloadEvent.objects.filter(archive_id=archive.pk, completed=False).exclude(
                        status__in=[self.Status.COMPLETED, self.Status.FAILED, self.Status.CANCELLED]
                    )
                    if self.pk:
                        qs = qs.exclude(pk=self.pk)
                    if not qs.exists():
                        archive.move_to_recycle_bin(reason=reason or f"Download {self.status}")
            except Exception:
                pass

    def save(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        orig_status = self.status
        orig_completed = self.completed
        orig_failed = self.failed

        self.sync_status_with_flags()

        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            update_fields_set = set(update_fields)
            state_fields = {"status", "completed", "failed"}
            if (
                bool(update_fields_set & state_fields)
                or self.status != orig_status
                or self.completed != orig_completed
                or self.failed != orig_failed
            ):
                update_fields_set.update(state_fields)
            kwargs["update_fields"] = list(update_fields_set)

        super().save(*args, **kwargs)

        if self.status in (self.Status.FAILED, self.Status.CANCELLED) or self.failed:
            self._recycle_empty_archive(
                reason=f"Download {self.status}: {self.error_message or 'Download failed'}"
            )

    def finish_download(self):
        self.progress = 100
        self.completed = True
        self.status = self.Status.COMPLETED
        self.completed_date = django_tz.now()

    def set_as_failed(self, error_message: str = ""):
        self.failed = True
        self.completed = True
        self.status = self.Status.FAILED
        if error_message:
            self.error_message = error_message
        self.completed_date = django_tz.now()
        self._recycle_empty_archive(
            reason=f"Download failed: {error_message}" if error_message else "Download failed"
        )

    def set_as_cancelled(self):
        self.failed = True
        self.completed = True
        self.status = self.Status.CANCELLED
        self.completed_date = django_tz.now()
        self._recycle_empty_archive(reason="Download cancelled")

    def eta(self) -> datetime | None:
        ref_date = self.started_date or self.create_date
        if not ref_date:
            return None
        if ref_date.tzinfo is not None:
            now = datetime.now(ref_date.tzinfo)
        else:
            now = datetime.now()
        if self.progress <= 0:
            return None

        if self.progress >= 100:
            return now

        elapsed_time = now - ref_date

        remaining_seconds = elapsed_time.total_seconds() * ((100 / self.progress) - 1)

        return now + timedelta(seconds=remaining_seconds)


