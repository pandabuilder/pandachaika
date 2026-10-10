# -*- coding: utf-8 -*-
from datetime import timedelta, datetime
from typing import Any, Optional

import django.utils.timezone as django_tz
from django.conf import settings
from django.db import models


class TaskQueue(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    task_type = models.CharField(max_length=50, default="web_crawler", db_index=True)
    args = models.JSONField(default=list, help_text="Command line arguments or URLs for the task.")
    options = models.JSONField(default=dict, blank=True, help_text="Serialized override options.")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="queued_tasks",
        help_text="User who initiated the task.",
    )
    reason = models.CharField(max_length=500, blank=True, default="")
    event_action = models.CharField(max_length=50, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    priority = models.IntegerField(default=10, db_index=True, help_text="Lower value means higher priority.")
    created_at = models.DateTimeField(default=django_tz.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")
    traceback = models.TextField(blank=True, default="")
    worker_pid = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["priority", "created_at", "id"]
        indexes = [
            models.Index(fields=["status", "priority", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"TaskQueue #{self.pk} ({self.task_type}) - {self.status}"

    @property
    def wanted(self) -> list[str]:
        """Extract wanted arguments for template display compatibility."""
        args_list = self.args if isinstance(self.args, list) else []
        wanted_items = []
        for i, arg in enumerate(args_list):
            if arg in ("-wanted", "--wanted") and i + 1 < len(args_list):
                wanted_items.append(args_list[i + 1])
            elif arg.startswith("-wanted="):
                wanted_items.append(arg.split("=", 1)[1])
        return wanted_items

    @property
    def override_options(self) -> dict[str, Any]:
        """Compatibility property for stats_workers template."""
        return self.options if isinstance(self.options, dict) else {}


class WorkerProcess(models.Model):
    pid = models.IntegerField(primary_key=True)
    hostname = models.CharField(max_length=150, blank=True, default="")
    worker_type = models.CharField(max_length=50, default="general")
    started_at = models.DateTimeField(default=django_tz.now)
    last_heartbeat = models.DateTimeField(default=django_tz.now, db_index=True)
    status = models.CharField(max_length=20, default="running")

    class Meta:
        ordering = ["-last_heartbeat"]

    def __str__(self) -> str:
        return f"Worker PID {self.pid} on {self.hostname} ({self.status})"

    @classmethod
    def is_any_worker_running(cls, threshold_seconds: int = 30) -> bool:
        cutoff = django_tz.now() - timedelta(seconds=threshold_seconds)
        return cls.objects.filter(status="running", last_heartbeat__gte=cutoff).exists()

    @classmethod
    def cleanup_dead_workers(cls, threshold_seconds: int = 60) -> int:
        cutoff = django_tz.now() - timedelta(seconds=threshold_seconds)
        dead_pids = list(cls.objects.filter(status="running", last_heartbeat__lt=cutoff).values_list("pid", flat=True))
        if dead_pids:
            SchedulerState.objects.filter(worker_pid__in=dead_pids).update(is_running=False)
        return cls.objects.filter(status="running", last_heartbeat__lt=cutoff).update(status="stopped")


class SchedulerState(models.Model):
    """Tracks state and control commands for background schedulers across process boundaries."""

    name = models.CharField(max_length=100, primary_key=True)
    display_name = models.CharField(max_length=200, blank=True, default="")
    schedule_type = models.CharField(max_length=50, default="scheduler")
    is_running = models.BooleanField(default=False, db_index=True)
    last_run = models.DateTimeField(null=True, blank=True)
    next_run = models.DateTimeField(null=True, blank=True)
    cycle_timer = models.FloatField(default=0.0)
    worker_pid = models.IntegerField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True, db_index=True)
    command_requested = models.CharField(max_length=50, blank=True, default="")
    command_arg = models.CharField(max_length=255, blank=True, default="")
    extra_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        status_str = "RUNNING" if self.is_running else "STOPPED"
        return f"SchedulerState {self.name} [{status_str}] (worker: {self.worker_pid})"

    @property
    def thread_name(self) -> str:
        return self.name

    @property
    def timer(self) -> float:
        return self.cycle_timer

    @property
    def current_last_run(self) -> Optional[datetime]:
        return self.last_run

    @property
    def is_active(self) -> bool:
        """Check if this scheduler is actively running based on heartbeat threshold."""
        if not self.is_running:
            return False
        if self.last_heartbeat:
            cutoff = django_tz.now() - timedelta(seconds=30)
            return self.last_heartbeat >= cutoff
        return self.is_running

    @classmethod
    def is_scheduler_running(cls, name: str, threshold_seconds: int = 30) -> bool:
        """Check if scheduler is actively running in a live worker process."""
        cutoff = django_tz.now() - timedelta(seconds=threshold_seconds)
        return cls.objects.filter(
            name=name,
            is_running=True,
            last_heartbeat__gte=cutoff,
        ).exists()

    @classmethod
    def request_command(cls, name: str, command: str, arg: str = "") -> None:
        """Request a control command (start, stop, force_run) to be processed by the worker runner."""
        cls.objects.update_or_create(
            name=name,
            defaults={
                "command_requested": command,
                "command_arg": arg,
            },
        )

    @classmethod
    def get_all_statuses(cls, threshold_seconds: int = 30) -> dict[str, bool]:
        """Return dict of {name: is_running} for all known schedulers."""
        cutoff = django_tz.now() - timedelta(seconds=threshold_seconds)
        active_names = set(
            cls.objects.filter(is_running=True, last_heartbeat__gte=cutoff).values_list("name", flat=True)
        )
        all_names = list(cls.objects.values_list("name", flat=True))
        return {name: (name in active_names) for name in all_names}
