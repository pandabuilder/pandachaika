# -*- coding: utf-8 -*-
import logging
from typing import Any
from collections.abc import Iterable

from django.contrib.auth import get_user_model
from django.db import transaction
import django.utils.timezone as django_tz

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
    deduplicate: bool = True,
    extra_options: dict[str, Any] | None = None,
) -> TaskQueue:
    """Enqueue a new task into PostgreSQL TaskQueue.

    ``extra_options`` are task parameters (e.g. ``{"all": True}``) merged into the serialized options.
    """
    args_list = list(args)

    if deduplicate:
        aliases = get_task_type_aliases(task_type)
        existing = (
            TaskQueue.objects.filter(
                task_type__in=aliases,
                status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING],
                args=args_list,
            )
            .order_by("created_at", "id")
            .first()
        )
        if existing:
            if existing.status == TaskQueue.Status.PENDING and priority < existing.priority:
                existing.priority = priority
                existing.save(update_fields=["priority"])
            logger.info(
                "Task #%d (%s) with identical args is already %s; skipping duplicate enqueue.",
                existing.pk,
                existing.task_type,
                existing.status,
            )
            return existing

    serialized_options = serialize_settings_options(override_options)
    if extra_options:
        serialized_options.update(extra_options)

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


def enqueue_gallery_update(
    links: Iterable[str],
    user: Any = None,
    reason: str | None = None,
    override_options: Any = None,
    event_action: str = "UPDATE_METADATA",
    priority: int = 10,
    deduplicate: bool = True,
) -> TaskQueue:
    """Enqueue a metadata refresh for already known galleries (and their related archives) by gallery link.

    These tasks run in their own queue (``gallery_updater``), so they don't hold back ``web_crawler`` tasks.
    The provider request limits are still shared, since they are enforced per request.
    """
    link_list = list(dict.fromkeys(links))
    if deduplicate and link_list:
        queued_links: set[str] = set()
        first_task_for_link: dict[str, TaskQueue] = {}
        pending = TaskQueue.objects.filter(
            task_type__in=get_task_type_aliases("gallery_updater"), status=TaskQueue.Status.PENDING
        ).order_by("created_at", "id")
        for pending_task in pending:
            for queued_link in pending_task.args if isinstance(pending_task.args, list) else []:
                if isinstance(queued_link, str):
                    queued_links.add(queued_link)
                    first_task_for_link.setdefault(queued_link, pending_task)
        remaining_links = [link for link in link_list if link not in queued_links]
        if not remaining_links:
            logger.info("All %d links to update are already queued; skipping duplicate enqueue.", len(link_list))
            return first_task_for_link[link_list[0]]
        if len(remaining_links) != len(link_list):
            logger.info(
                "Skipping %d links that are already queued for update.", len(link_list) - len(remaining_links)
            )
        link_list = remaining_links

    return enqueue(
        args=link_list,
        task_type="gallery_updater",
        override_options=override_options,
        user=user,
        reason=reason,
        event_action=event_action,
        priority=priority,
        deduplicate=deduplicate,
    )


def enqueue_matching_task(
    task_type: str,
    args: Any = None,
    matching_kwargs: dict[str, Any] | None = None,
    provider: str | None = None,
    user: Any = None,
    priority: int = 10,
    deduplicate: bool = True,
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
        deduplicate=deduplicate,
    )


TASK_TYPE_ALIAS_GROUPS: list[set[str]] = [
    {"web_crawler", "webcrawler"},
    {"gallery_creator", "gallery_worker", "create_galleries"},
    {"gallery_updater", "update_galleries"},
    {"folder_crawler", "foldercrawler"},
    {"fileinfo_worker", "recalc_all_file_info", "calculate_missing_info", "recalc_info"},
    {"thumbnails_worker", "regenerate_all_thumbs", "generate_missing_thumbs"},
    {"extract_worker", "extract_archives"},
    {"reduce_worker", "reduce_archives", "reduce_archive"},
    {"calculate_sha1_worker", "calculate_images_sha1"},
    {"convert_archive_worker", "check_and_convert_filetype", "convert_archive"},
    {"clone_archive_worker", "clone_plus", "clone_archive"},
    {"split_archive_worker", "split_archive", "split_archives"},
    {"mark_similar_worker", "mark_similar_archives", "mark_similar"},
    {"delete_archive_worker", "delete_archive", "delete_archives"},
    {"publish_worker", "publish_archives", "set_all_hidden_as_public"},
    {"contextual_extractor_worker", "extract_contextual_info"},
    {"match_unmatched_worker", "generate_possible_matches_internally", "generate_matches"},
    {"web_search_worker", "search_wanted_galleries_provider_titles"},
    {"wanted_local_search_worker", "wanted_galleries_possible_matches"},
    {"match_unmatched_gallery_groups_worker"},
    {"web_match_worker"},
    {"database_prune", "prune_database"},
    {"archive_file_check"},
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
        ).update(status=TaskQueue.Status.CANCELLED, finished_at=django_tz.now())
        return updated > 0


def clear_pending_tasks(task_type: str | None = None, mode: str = "delete") -> int:
    """Clear/delete or cancel pending tasks from the queue, optionally filtered by task_type.

    Returns the number of affected tasks.
    """
    qs = TaskQueue.objects.filter(status=TaskQueue.Status.PENDING)
    if task_type and task_type != "all":
        qs = qs.filter(task_type__in=get_task_type_aliases(task_type))
    if mode == "cancel":
        return qs.update(status=TaskQueue.Status.CANCELLED, finished_at=django_tz.now())
    deleted, _ = qs.delete()
    return deleted


def is_worker_running(threshold_seconds: int = 30) -> bool:
    """Check if any worker process is currently reporting a heartbeat."""
    return WorkerProcess.is_any_worker_running(threshold_seconds=threshold_seconds)


def is_archive_task_active(archive_pk: int, task_type: str) -> bool:
    """Check if a task of given type for the archive PK is currently pending or processing."""
    active_tasks = TaskQueue.objects.filter(
        status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING],
        task_type__in=get_task_type_aliases(task_type),
    ).values_list("args", flat=True)
    target_str = str(archive_pk)
    for task_args in active_tasks:
        if isinstance(task_args, list):
            for arg in task_args:
                if arg == archive_pk or str(arg) == target_str:
                    return True
    return False


def is_archive_extracting(archive_pk: int) -> bool:
    """Check if an extraction task for the given archive PK is currently pending or processing."""
    return is_archive_task_active(archive_pk, "extract_worker")


def is_archive_reducing(archive_pk: int) -> bool:
    """Check if a reduction task for the given archive PK is currently pending or processing."""
    return is_archive_task_active(archive_pk, "reduce_worker")


def is_archive_publishing(archive_pk: int) -> bool:
    """Check if a publish task for the given archive PK is currently pending or processing."""
    return is_archive_task_active(archive_pk, "publish_worker")


def is_archive_extracting_context(archive_pk: int) -> bool:
    """Check if a contextual extraction task for the given archive PK is currently pending or processing."""
    return is_archive_task_active(archive_pk, "contextual_extractor_worker")


def enqueue_contextual_extraction(
    archives: Any,
    override_options: Any = None,
    user: Any = None,
    reason: str | None = None,
    event_action: str = "EXTRACT_CONTEXTUAL_INFO",
    priority: int = 10,
    profile: str | None = None,
) -> list[TaskQueue]:
    """Enqueue one contextual_extractor_worker task per archive so each task is individually controlled."""
    pks: list[int] = []
    if archives is not None:
        if hasattr(archives, "values_list"):
            pks = [int(pk) for pk in archives.values_list("pk", flat=True)]
        elif isinstance(archives, Iterable) and not isinstance(archives, (str, bytes)):
            pks = [int(x.pk if hasattr(x, "pk") else x) for x in archives]
        else:
            val: Any = archives.pk if hasattr(archives, "pk") else archives
            pks = [int(val)]

    task_options = override_options
    if profile is not None:
        if task_options is None:
            task_options = {"manga_extractor_profile": profile}
        elif isinstance(task_options, dict):
            task_options = dict(task_options)
            task_options["manga_extractor_profile"] = profile
        else:
            try:
                setattr(task_options, "manga_extractor_profile", profile)
            except Exception:
                pass

    created_tasks: list[TaskQueue] = []
    seen_pks: set[int] = set()
    for pk in pks:
        if pk in seen_pks:
            continue
        seen_pks.add(pk)
        if is_archive_extracting_context(pk):
            continue
        task = enqueue(
            args=[pk],
            task_type="contextual_extractor_worker",
            override_options=task_options,
            user=user,
            reason=reason,
            event_action=event_action,
            priority=priority,
        )
        created_tasks.append(task)
    return created_tasks

