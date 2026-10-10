# -*- coding: utf-8 -*-
import logging
from typing import Any, Iterable, Optional

from django.contrib.auth import get_user_model
from django.db import transaction

from workers.models import TaskQueue, WorkerProcess
from workers.serialization import serialize_settings_options

logger = logging.getLogger(__name__)


def enqueue(
    args: Iterable[str],
    task_type: str = "web_crawler",
    override_options: Any = None,
    user: Any = None,
    reason: Optional[str] = None,
    event_action: Optional[str] = None,
    priority: int = 10,
) -> TaskQueue:
    """Enqueue a new task into PostgreSQL TaskQueue."""
    serialized_options = serialize_settings_options(override_options)

    user_obj = None
    if user and getattr(user, "is_authenticated", False):
        user_obj = user
    elif "user_id" in serialized_options:
        User = get_user_model()
        try:
            user_obj = User.objects.get(pk=serialized_options["user_id"])
        except User.DoesNotExist:
            user_obj = None

    task_reason = reason or serialized_options.get("archive_reason") or serialized_options.get("gallery_reason") or ""
    action = event_action or serialized_options.get("event_action") or ""

    args_list = list(args)

    task = TaskQueue.objects.create(
        task_type=task_type,
        args=args_list,
        options=serialized_options,
        user=user_obj,
        reason=task_reason,
        event_action=action,
        priority=priority,
        status=TaskQueue.Status.PENDING,
    )
    logger.info("Enqueued task #%d (%s) with args: %s", task.pk, task_type, args_list)
    return task


def get_pending_tasks():
    """Return pending tasks ordered by priority and creation time."""
    return TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).order_by("priority", "created_at", "id")


def get_processing_task() -> Optional[TaskQueue]:
    """Return the currently processing task if any."""
    return TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).order_by("-started_at").first()


def queue_size() -> int:
    """Return the number of pending tasks."""
    return TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count()


def remove_by_index(index: int) -> bool:
    """Remove/cancel a pending task by its index in the queue."""
    pending = list(get_pending_tasks().values_list("id", flat=True))
    if 0 <= index < len(pending):
        task_id = pending[index]
        return cancel_by_id(task_id)
    return False


def cancel_by_id(task_id: int) -> bool:
    """Cancel a pending or processing task by its primary key."""
    with transaction.atomic():
        updated = TaskQueue.objects.filter(
            pk=task_id, status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING]
        ).update(status=TaskQueue.Status.CANCELLED)
        return updated > 0


def is_worker_running(threshold_seconds: int = 30) -> bool:
    """Check if any worker process is currently reporting a heartbeat."""
    return WorkerProcess.is_any_worker_running(threshold_seconds=threshold_seconds)
