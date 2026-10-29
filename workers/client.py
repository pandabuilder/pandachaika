# -*- coding: utf-8 -*-
import logging
from typing import Any
from collections.abc import Iterable

from django.contrib.auth import get_user_model
from django.db import transaction

from workers.models import TaskQueue, WorkerProcess
from workers.serialization import serialize_settings_options

logger = logging.getLogger(__name__)


def enqueue(
    args: Iterable[Any],
    task_type: str = "web_crawler",
    override_options: Any = None,
    user: Any = None,
    reason: str | None = None,
    event_action: str | None = None,
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


def enqueue_matching_task(
    task_type: str,
    args: Any = None,
    matching_kwargs: dict[str, Any] | None = None,
    provider: str | None = None,
    user: Any = None,
    priority: int = 10,
) -> TaskQueue:
    """Convenience helper to enqueue matching tasks to the database task queue."""
    pks = []
    if args is not None:
        if hasattr(args, "values_list"):
            pks = list(args.values_list("pk", flat=True))
        elif isinstance(args, (list, tuple, set)):
            pks = [getattr(x, "pk", x) for x in args]

    serializable_kwargs = {}
    if matching_kwargs:
        for k, v in matching_kwargs.items():
            if isinstance(v, (tuple, set, list)):
                serializable_kwargs[k] = list(v)
            else:
                serializable_kwargs[k] = v

    options: dict[str, Any] = {"matching_kwargs": serializable_kwargs}
    if provider:
        options["provider"] = provider

    return enqueue(
        args=pks,
        task_type=task_type,
        override_options=options,
        user=user,
        priority=priority,
    )


TASK_TYPE_ALIAS_GROUPS: list[set[str]] = [
    {"web_crawler", "webcrawler"},
    {"folder_crawler", "foldercrawler"},
    {"fileinfo_worker", "recalc_all_file_info"},
    {"thumbnails_worker", "regenerate_all_thumbs"},
    {"extract_worker", "extract_archives"},
    {"publish_worker", "publish_archives"},
    {"match_unmatched_worker", "generate_possible_matches_internally"},
    {"web_search_worker", "search_wanted_galleries_provider_titles"},
    {"wanted_local_search_worker", "wanted_galleries_possible_matches"},
    {"match_unmatched_gallery_groups_worker"},
    {"web_match_worker"},
]


def get_task_type_aliases(task_type: str) -> set[str]:
    """Return all task_type strings belonging to the same logical queue as task_type."""
    for group in TASK_TYPE_ALIAS_GROUPS:
        if task_type in group:
            return set(group)
    return {task_type}


def expand_task_type_aliases(task_types: Iterable[str]) -> set[str]:
    """Expand an iterable of task_types to include all aliases for each type."""
    expanded: set[str] = set()
    for t in task_types:
        expanded.update(get_task_type_aliases(t))
    return expanded


def get_pending_tasks(task_type: str | None = None):
    """Return pending tasks ordered by priority and creation time, optionally filtered by task_type."""
    qs = TaskQueue.objects.filter(status=TaskQueue.Status.PENDING)
    if task_type:
        qs = qs.filter(task_type__in=get_task_type_aliases(task_type))
    return qs.order_by("priority", "created_at", "id")


def get_processing_task(task_type: str | None = None) -> TaskQueue | None:
    """Return the currently processing task if any, optionally filtered by task_type."""
    qs = TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING)
    if task_type:
        qs = qs.filter(task_type__in=get_task_type_aliases(task_type))
    return qs.order_by("-started_at").first()


def queue_size(task_type: str | None = None) -> int:
    """Return the number of pending tasks, optionally filtered by task_type."""
    qs = TaskQueue.objects.filter(status=TaskQueue.Status.PENDING)
    if task_type:
        qs = qs.filter(task_type__in=get_task_type_aliases(task_type))
    return qs.count()


def remove_by_index(index: int, task_type: str | None = None) -> bool:
    """Remove/cancel a pending task by its index in the queue."""
    pending = list(get_pending_tasks(task_type=task_type).values_list("id", flat=True))
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


def is_archive_extracting(archive_pk: int) -> bool:
    """Check if an extraction task for the given archive PK is currently pending or processing."""
    active_tasks = TaskQueue.objects.filter(
        status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING],
        task_type__in=get_task_type_aliases("extract_worker"),
    ).values_list("args", flat=True)
    target_str = str(archive_pk)
    for task_args in active_tasks:
        if isinstance(task_args, list):
            for arg in task_args:
                if arg == archive_pk or str(arg) == target_str:
                    return True
    return False


def is_archive_publishing(archive_pk: int) -> bool:
    """Check if a publish task for the given archive PK is currently pending or processing."""
    active_tasks = TaskQueue.objects.filter(
        status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING],
        task_type__in=get_task_type_aliases("publish_worker"),
    ).values_list("args", flat=True)
    target_str = str(archive_pk)
    for task_args in active_tasks:
        if isinstance(task_args, list):
            for arg in task_args:
                if arg == archive_pk or str(arg) == target_str:
                    return True
    return False


