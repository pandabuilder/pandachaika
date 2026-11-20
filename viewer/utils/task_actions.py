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

# Labels shown while a task runs and once it finishes, keyed by task_type (any alias of the queue works).
TASK_LABELS: dict[str, tuple[str, str]] = {
    "extract_worker": ("Extracting", "Extracted"),
    "reduce_worker": ("Reducing", "Reduced"),
    "publish_worker": ("Publishing", "Published"),
    "contextual_extractor_worker": ("Extracting context", "Context extracted"),
    "fileinfo_worker": ("Recalculating", "Recalculated"),
    "calculate_sha1_worker": ("Calculating SHA1", "SHA1 calculated"),
    "mark_similar_worker": ("Marking similar", "Similar marked"),
    "convert_archive_worker": ("Converting", "Converted"),
    "clone_archive_worker": ("Cloning", "Cloned"),
    "split_archive_worker": ("Splitting", "Split"),
    "match_unmatched_worker": ("Matching", "Matched"),
    "gallery_updater": ("Updating metadata", "Metadata updated"),
    "folder_crawler": ("Rematching", "Rematched"),
    "delete_archive_worker": ("Deleting", "Deleted"),
}


def task_labels(task_type: str) -> tuple[str, str]:
    for alias in client.get_task_type_aliases(task_type):
        if alias in TASK_LABELS:
            return TASK_LABELS[alias]
    readable = task_type.replace("_", " ").capitalize()
    return readable, f"{readable} done"


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


def task_status_context(task: TaskQueue | None) -> dict[str, Any]:
    if task is None:
        return {"task": None, "state": "missing", "active": False}
    running_label, done_label = task_labels(task.task_type)
    active = task.status in (TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING)
    context: dict[str, Any] = {
        "task": task,
        "state": task.status,
        "active": active,
        "label": running_label,
        "done_label": done_label,
        "status_url": reverse("viewer:task-status", args=[task.pk]),
    }
    if active:
        context["poll_delay_ms"] = poll_delay_ms(task)
        context["worker_running"] = client.is_worker_running()
        if task.status == TaskQueue.Status.PENDING:
            context["ahead"] = client.tasks_ahead(task)
            if task.not_before and task.not_before > django_tz.now():
                context["not_before"] = task.not_before
    return context


def render_task_status(request: HttpRequest, task: TaskQueue | None) -> HttpResponse:
    return render(request, TASK_STATUS_TEMPLATE, task_status_context(task))


def task_action_response(
    request: HttpRequest,
    task: TaskQueue | None,
    target_url: str,
    message: str,
    level: int = messages.SUCCESS,
) -> HttpResponse:
    """Answer an action that queued (or found already queued) a task.

    HTMX requests get the polling status fragment, others get ``message`` and a redirect to ``target_url``.
    """
    if task is not None and is_htmx(request):
        return render_task_status(request, task)
    messages.add_message(request, level, message)
    if is_htmx(request):
        return htmx_redirect(target_url)
    return HttpResponseRedirect(target_url)


def can_view_task(request: HttpRequest, task: TaskQueue) -> bool:
    user = request.user
    return bool(user.is_authenticated and (user.is_staff or (task.user_id is not None and task.user_id == user.pk)))

