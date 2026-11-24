"""Background task status for API clients (``GET ?task=<id>`` and ``GET ?tasks=<id1,id2>``).

Write commands that enqueue worker tasks answer with ``task_id`` (or ``task_ids``) and a ``status_url`` pointing here.
Only staff and the user who queued a task can see it; anyone else gets it reported as missing.
"""

from datetime import datetime
from typing import Any

from django.http import HttpRequest, HttpResponse
from django.http.request import QueryDict
from django.urls import reverse

from core.base.utilities import timestamp_or_zero
from viewer.utils.task_actions import can_view_task_user, poll_delay_ms, task_labels
from viewer.views.api.auth import require_user
from viewer.views.api.errors import ApiError, ApiSurface, json_response
from workers import client
from workers.models import TaskQueue

# Same limit as /api/tasks/status/ (Archive Manager bulk actions can queue one task per archive).
MAX_TASKS_PER_REQUEST = 500
ACTIVE_STATUSES = (TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING)
# Task options worth echoing back as the task's result summary (never the full serialized settings).
SUMMARY_OPTION_KEYS = (
    "delete_zipped",
    "delete_archive",
    "delete_file",
    "delete_gallery",
    "mark_gallery_deleted",
    "mark_gallery_denied",
    "gallery_reason",
)


def task_status_url(request: HttpRequest | None, task_ids: list[int], batch: bool = False) -> str | None:
    """URL an API client polls for these tasks, on the API surface the request came in on.

    A single task gets ``?task=`` (one task object) unless ``batch`` is set: responses that carry ``task_ids``
    always point at ``?tasks=`` (the batch summary), so their shape doesn't change with how many tasks were queued.
    """
    if not task_ids:
        return None
    surface = getattr(getattr(request, "api", None), "surface", ApiSurface.V1)
    base = reverse("viewer:api-v1") if surface == ApiSurface.V1 else reverse("viewer:api")
    if len(task_ids) == 1 and not batch:
        return f"{base}?task={task_ids[0]}"
    return f"{base}?tasks={','.join(str(task_id) for task_id in task_ids)}"


def task_reference(request: HttpRequest | None, task: TaskQueue) -> dict[str, Any]:
    """The ``task_id``/``status_url`` pair that every response of a command that queued one task includes."""
    return {"task_id": task.pk, "status_url": task_status_url(request, [task.pk])}


def tasks_reference(request: HttpRequest | None, task_ids: list[int]) -> dict[str, Any]:
    """The ``task_ids``/``status_url`` pair for commands that queue several tasks (``status_url`` is null if none)."""
    return {"task_ids": task_ids, "status_url": task_status_url(request, task_ids, batch=True)}


def _timestamp(value: datetime | None) -> int | None:
    return int(timestamp_or_zero(value)) if value else None


def _result_summary(task: TaskQueue) -> dict[str, Any] | None:
    """What the task worked on and what the worker recorded about it. None until the task finishes."""
    if task.status in ACTIVE_STATUSES:
        return None
    args = task.args if isinstance(task.args, list) else []
    options = task.options if isinstance(task.options, dict) else {}
    summary: dict[str, Any] = {
        "outcome": task.status,
        "targets": [arg for arg in args if not (isinstance(arg, str) and arg.startswith("-"))][:50],
    }
    for key in SUMMARY_OPTION_KEYS:
        if key in options:
            summary[key] = options[key]
    crawl_stats = options.get("crawl_batch_stats")
    if isinstance(crawl_stats, dict):
        summary["crawl_stats"] = crawl_stats
    if task.result is not None:
        summary["details"] = task.result
    return summary


def task_to_json(request: HttpRequest | None, task: TaskQueue) -> dict[str, Any]:
    running_label, done_label = task_labels(task.task_type, task=task)
    active = task.status in ACTIVE_STATUSES
    return {
        "id": task.pk,
        "task_type": task.task_type,
        "status": task.status,
        "active": active,
        "label": running_label,
        "done_label": done_label,
        "reason": task.reason,
        "created_at": _timestamp(task.created_at),
        "started_at": _timestamp(task.started_at),
        "finished_at": _timestamp(task.finished_at),
        "not_before": _timestamp(task.not_before),
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "error_message": task.error_message or "",
        "result": _result_summary(task),
        "poll_delay_ms": poll_delay_ms(task) if active else 0,
        "status_url": task_status_url(request, [task.pk]),
    }


def _missing_task_json(task_id: int) -> dict[str, Any]:
    # Same fields as a missing task in /api/tasks/status/, so clients can read either.
    return {
        "id": task_id,
        "task_type": "",
        "status": "missing",
        "active": False,
        "label": "Task",
        "done_label": "Done",
        "error_message": "",
    }


def _parse_task_ids(values: list[str]) -> list[int]:
    task_ids: list[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                task_id = int(part)
            except ValueError:
                raise ApiError.bad_request(f"Invalid task ID: {part!r}.") from None
            if task_id <= 0:
                raise ApiError.bad_request(f"Invalid task ID: {part!r}.")
            task_ids.append(task_id)
    return list(dict.fromkeys(task_ids))


def handle_get_task(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    user = require_user(request)
    task_ids = _parse_task_ids(data.getlist("task"))
    if len(task_ids) != 1:
        raise ApiError.bad_request("Pass exactly one task ID in 'task' (use 'tasks' for several).")
    task = TaskQueue.objects.filter(pk=task_ids[0]).first()
    if task is None or not can_view_task_user(user, task):
        raise ApiError.not_found("Task does not exist.")
    task_data = task_to_json(request, task)
    task_data["worker_running"] = client.is_worker_running() if task_data["active"] else True
    return json_response(task_data, sort_keys=True)


def handle_get_tasks(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    user = require_user(request)
    task_ids = _parse_task_ids(data.getlist("tasks"))
    if not task_ids:
        raise ApiError.bad_request("Pass one or more comma-separated task IDs in 'tasks'.")
    if len(task_ids) > MAX_TASKS_PER_REQUEST:
        raise ApiError.bad_request(f"At most {MAX_TASKS_PER_REQUEST} task IDs per request.")

    tasks_by_id = {task.pk: task for task in TaskQueue.objects.filter(pk__in=task_ids)}
    items: list[dict[str, Any]] = []
    counts = {status: 0 for status in (*TaskQueue.Status.values, "missing")}
    for task_id in task_ids:
        task = tasks_by_id.get(task_id)
        if task is None or not can_view_task_user(user, task):
            items.append(_missing_task_json(task_id))
            counts["missing"] += 1
            continue
        items.append(task_to_json(request, task))
        counts[task.status] = counts.get(task.status, 0) + 1

    active_items = [item for item in items if item["active"]]
    active = bool(active_items)
    # The per-status counts are top-level keys, as in /api/tasks/status/ (pending, processing, ..., missing).
    return json_response(
        {
            **counts,
            "total": len(task_ids),
            "active": active,
            "done": len(task_ids) - len(active_items),
            "worker_running": client.is_worker_running() if active else True,
            "poll_delay_ms": min(item["poll_delay_ms"] for item in active_items) if active_items else 0,
            "tasks": items,
            "status_url": task_status_url(request, task_ids, batch=True),
        },
        sort_keys=True,
    )
