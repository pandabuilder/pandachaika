"""Background task actions that the page follows by polling, without a full reload.

An action view enqueues a worker task and answers an HTMX request with the task status fragment
(``viewer/include/task_status.html``) instead of a redirect. While the task is pending or processing, the fragment
asks the task status endpoint for a new copy of itself after a short delay, so the button shows the progress and
``js/task_actions.js`` reloads the page once the task is done. Requests without HTMX keep the old behavior (a flash
message and a redirect back).
"""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
import django.utils.timezone as django_tz

from workers import client
from workers.models import TaskQueue

TASK_STATUS_TEMPLATE = "viewer/include/task_status.html"
VALID_VARIANTS = ("button", "block")

# Labels shown while a task runs and once it finishes, keyed by task_type (any alias of the queue works).
TASK_LABELS: dict[str, tuple[str, str]] = {
    "extract_worker": ("Extracting", "Extracted"),
    "reduce_worker": ("Reducing", "Reduced"),
    "publish_worker": ("Publishing", "Published"),
    "contextual_extractor_worker": ("Extracting context", "Context extracted"),
    "fileinfo_worker": ("Recalculating", "Recalculated"),
    "thumbnails_worker": ("Generating thumbnails", "Thumbnails generated"),
    "gallery_thumbnail_worker": ("Refetching thumbnail", "Thumbnail refetched"),
    "calculate_sha1_worker": ("Calculating SHA1", "SHA1 calculated"),
    "mark_similar_worker": ("Marking similar", "Similar marked"),
    "convert_archive_worker": ("Converting", "Converted"),
    "clone_archive_worker": ("Cloning", "Cloned"),
    "split_archive_worker": ("Splitting", "Split"),
    "match_unmatched_worker": ("Matching", "Matched"),
    "web_search_worker": ("Searching wanted", "Wanted search done"),
    "wanted_local_search_worker": ("Matching wanted", "Wanted matched"),
    "auto_wanted_worker": ("Running auto wanted", "Auto wanted done"),
    "gallery_updater": ("Updating metadata", "Metadata updated"),
    "folder_crawler": ("Rematching", "Rematched"),
    "delete_archive_worker": ("Deleting", "Deleted"),
    "web_crawler": ("Downloading", "Downloaded"),
    "cold_storage_offload_worker": ("Offloading", "Offloaded"),
    "cold_storage_restore_worker": ("Restoring", "Restored"),
    "archive_batch_worker": ("Updating archives", "Archives updated"),
    "gallery_batch_worker": ("Updating galleries", "Galleries updated"),
    "reconcile_downloads_worker": ("Reconciling downloads", "Downloads reconciled"),
    "update_provider_wanted_worker": ("Creating wanted galleries", "Wanted galleries created"),
    "send_email_worker": ("Sending emails", "Emails sent"),
    "database_prune": ("Pruning database", "Database pruned"),
    "web_match_worker": ("Web matching", "Web match done"),
}


def task_labels(task_type: str, task: TaskQueue | None = None) -> tuple[str, str]:
    aliases = client.get_task_type_aliases(task_type)
    if "web_crawler" in aliases and task is not None and isinstance(task.args, list):
        if "--retry-failed" in task.args:
            return ("Retrying failed", "Failed retried")
        if "-unt" in task.args:
            return ("Updating galleries", "Galleries updated")
        if "-umt" in task.args:
            return ("Updating thumbnails", "Thumbnails updated")
    for alias in aliases:
        if alias in TASK_LABELS:
            return TASK_LABELS[alias]
    readable = task_type.replace("_", " ").capitalize()
    return readable, f"{readable} done"


def normalize_variant(variant: str | None) -> str:
    if variant in VALID_VARIANTS:
        return variant
    return "button"


def request_variant(request: HttpRequest, variant: str | None = None) -> str:
    if variant is not None:
        return normalize_variant(variant)
    raw = (
        request.GET.get("variant")
        or request.POST.get("_task_variant")
        or request.headers.get("X-Task-Status-Variant")
    )
    return normalize_variant(raw)


def is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request") == "true"


def htmx_redirect(url: str) -> HttpResponse:
    """Make HTMX navigate the whole page to ``url``, so flash messages show on that page.

    A plain redirect would be followed by the XMLHttpRequest itself, which renders (and consumes) the messages in a
    page that is never shown.
    """
    response = HttpResponse(status=200)
    response["HX-Redirect"] = url
    return response


def poll_delay_ms(task: TaskQueue) -> int:
    """Milliseconds until the next status poll: quick at first, slower for tasks that wait or run for long."""
    now = django_tz.now()
    if task.status == TaskQueue.Status.PENDING and task.not_before and task.not_before > now:
        return 15000
    since = task.started_at or task.created_at
    elapsed = (now - since).total_seconds() if since else 0.0
    if elapsed < 30:
        return 1500
    if elapsed < 300:
        return 3000
    return 10000


def task_status_context(task: TaskQueue | None, variant: str = "button") -> dict[str, Any]:
    resolved_variant = normalize_variant(variant)
    if task is None:
        return {"task": None, "state": "missing", "active": False, "variant": resolved_variant}
    running_label, done_label = task_labels(task.task_type, task=task)
    active = task.status in (TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING)
    status_url = reverse("viewer:task-status", args=[task.pk])
    if resolved_variant != "button":
        status_url = f"{status_url}?variant={resolved_variant}"
    context: dict[str, Any] = {
        "task": task,
        "state": task.status,
        "active": active,
        "label": running_label,
        "done_label": done_label,
        "status_url": status_url,
        "variant": resolved_variant,
    }
    if active:
        context["poll_delay_ms"] = poll_delay_ms(task)
        context["worker_running"] = client.is_worker_running()
        if task.status == TaskQueue.Status.PENDING:
            context["ahead"] = client.tasks_ahead(task)
            if task.not_before and task.not_before > django_tz.now():
                context["not_before"] = task.not_before
    return context


def render_task_status(request: HttpRequest, task: TaskQueue | None, variant: str | None = None) -> HttpResponse:
    return render(request, TASK_STATUS_TEMPLATE, task_status_context(task, variant=request_variant(request, variant)))


def task_action_response(
    request: HttpRequest,
    task: TaskQueue | None,
    target_url: str,
    message: str,
    level: int = messages.SUCCESS,
    variant: str | None = None,
) -> HttpResponse:
    """Answer an action that queued (or found already queued) a task.

    HTMX requests get the polling status fragment, others get ``message`` and a redirect to ``target_url``.
    """
    if task is not None and is_htmx(request):
        return render_task_status(request, task, variant=variant)
    extra_tags = "danger" if level == messages.ERROR else ""
    messages.add_message(request, level, message, extra_tags=extra_tags)
    if is_htmx(request):
        return htmx_redirect(target_url)
    return HttpResponseRedirect(target_url)


def can_view_task_user(user: Any, task: TaskQueue) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (user.is_staff or (task.user_id is not None and task.user_id == user.pk))
    )


def can_view_task(request: HttpRequest, task: TaskQueue) -> bool:
    return can_view_task_user(request.user, task)


def task_batch_status_summary(task_ids: list[int], user: Any) -> dict[str, Any]:
    """Summarize the live status of a batch of queued tasks (for Archive Manager and bulk actions)."""
    unique_ids = list(dict.fromkeys(task_ids))
    tasks_by_id = {t.pk: t for t in TaskQueue.objects.filter(pk__in=unique_ids)}

    counts = {
        "pending": 0,
        "processing": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "missing": 0,
    }
    items: list[dict[str, Any]] = []
    active_delays: list[int] = []

    for tid in unique_ids:
        task = tasks_by_id.get(tid)
        if task is None or not can_view_task_user(user, task):
            counts["missing"] += 1
            items.append(
                {
                    "id": tid,
                    "task_type": "",
                    "status": "missing",
                    "label": "Task",
                    "done_label": "Done",
                    "error_message": "",
                }
            )
            continue

        running_label, done_label = task_labels(task.task_type, task=task)
        state = task.status
        if state in counts:
            counts[state] += 1
        if state in (TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING):
            active_delays.append(poll_delay_ms(task))
        items.append(
            {
                "id": task.pk,
                "task_type": task.task_type,
                "status": state,
                "label": running_label,
                "done_label": done_label,
                "error_message": task.error_message or "",
            }
        )

    active = (counts["pending"] + counts["processing"]) > 0
    done = counts["completed"] + counts["failed"] + counts["cancelled"] + counts["missing"]
    return {
        "total": len(unique_ids),
        "pending": counts["pending"],
        "processing": counts["processing"],
        "completed": counts["completed"],
        "failed": counts["failed"],
        "cancelled": counts["cancelled"],
        "missing": counts["missing"],
        "done": done,
        "active": active,
        "worker_running": client.is_worker_running() if active else True,
        "poll_delay_ms": min(active_delays) if active_delays else 0,
        "tasks": items,
    }

