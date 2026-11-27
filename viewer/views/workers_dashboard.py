# -*- coding: utf-8 -*-
from datetime import timedelta
import json
import logging
import os
from collections import defaultdict
from typing import Any

import django.utils.timezone as django_tz
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from viewer.models import DownloadEvent
from viewer.utils.requests import double_check_auth
from viewer.utils.task_actions import (
    can_view_task,
    render_task_status,
    task_batch_status_summary,
)
from workers import client as worker_client
from workers.crawl_batch import (
    MAX_BATCH_PRIORITY,
    MIN_BATCH_PRIORITY,
    aggregate_tasks_provider_stats,
    enrich_provider_stats,
    extract_crawl_batch_meta,
    summarize_provider_stats,
)
from workers.models import SchedulerState, TaskQueue, WorkerProcess

logger = logging.getLogger(__name__)

KNOWN_TASK_TYPES: list[tuple[str, str, str]] = [
    ("web_crawler", "Web Crawler", "Crawls URLs and downloads galleries/archives"),
    ("gallery_creator", "Gallery Creator", "Creates galleries in database and handles downloads"),
    (
        "gallery_updater",
        "Gallery Updater",
        "Refreshes the metadata of known galleries and their related archives",
    ),
    ("folder_crawler", "Folder Crawler", "Scans local filesystem folders for new archives"),
    ("fileinfo_worker", "File Info", "Recalculates archive CRC32, file list, and sizes"),
    ("thumbnails_worker", "Thumbnails", "Regenerates cover and page thumbnails"),
    ("extract_worker", "Archive Extraction", "Extracts archive images and generates page thumbnails"),
    (
        "publish_worker",
        "Archive Publishing",
        "Verifies zip integrity, generates thumbnails/SHA1s, and publishes archives",
    ),
    (
        "contextual_extractor_worker",
        "Contextual Extractor",
        "Extracts contextual page and archive metrics via manga-extractor",
    ),
    ("match_unmatched_worker", "Local Matcher", "Matches unmatched archives to galleries locally"),
    ("cold_storage_offload_worker", "Cold Storage Offload", "Moves archive files to the cold storage bucket"),
    ("cold_storage_restore_worker", "Cold Storage Restore", "Brings offloaded archive files back to the local disk"),
    ("auto_wanted_worker", "Auto Wanted", "Runs auto wanted sources, creating wanted galleries for new releases"),
    ("web_search_worker", "Wanted Provider Search", "Searches wanted galleries against external providers"),
    ("wanted_local_search_worker", "Wanted Local Search", "Searches wanted galleries against local database"),
    ("match_unmatched_gallery_groups_worker", "Match Groups", "Matches archives within gallery match groups"),
    ("web_match_worker", "Web Matcher", "Web matching for archive groups"),
    ("archive_batch_worker", "Archive Batch", "Applies bulk archive state, metadata, group, and match changes"),
    ("gallery_batch_worker", "Gallery Batch", "Applies bulk gallery status, deletion, and match-group changes"),
    ("reconcile_downloads_worker", "Reconcile Downloads", "Reconciles archives and downloads against disk and staging"),
    ("update_provider_wanted_worker", "Provider Wanted", "Creates wanted galleries from provider artist/group lists"),
    ("send_email_worker", "Email Delivery", "Sends notification emails asynchronously"),
]

# Default/maximum number of rows returned for list sections (pending tasks, recent tasks,
# downloads per type). The page sends the desired value via the ``limit`` query parameter.
DEFAULT_LIST_LIMIT = 25
MAX_LIST_LIMIT = 500

# Download types correspond to DownloadEvent.method (the downloader ``type``).
DOWNLOAD_TYPE_LABELS: dict[str, str] = {
    "archive": "Archive",
    "hath": "H@H",
    "torrent": "Torrent",
    "torrent_api": "Torrent (API)",
    "gallerydl": "gallery-dl",
    "chaika": "Chaika",
    "submit": "Submit",
    "info": "Info",
    "fake": "Fake",
    "scheduler": "Scheduler Transfer",
    "other": "Other",
}
# Preferred display order, unknown types are appended alphabetically.
DOWNLOAD_TYPE_ORDER: list[str] = ["archive", "hath", "torrent", "torrent_api", "gallerydl"]
# Live scheduler downloads not matched to a DownloadEvent are attributed to these types.
SCHEDULER_DOWNLOAD_TYPES: dict[str, str] = {
    "archive_download_queue": "archive",
}


def parse_list_limit(value: Any, default: int = DEFAULT_LIST_LIMIT) -> int:
    """Parse a list limit from user input, clamped to [1, MAX_LIST_LIMIT]."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, MAX_LIST_LIMIT))


def get_list_limits_from_request(request: HttpRequest) -> dict[str, int]:
    """Read list limits from the query string. ``limit`` sets all sections, while
    ``pending_limit``, ``recent_limit`` and ``downloads_limit`` override individual ones."""
    base = parse_list_limit(request.GET.get("limit"))
    return {
        "limit": base,
        "pending_limit": parse_list_limit(request.GET.get("pending_limit"), base),
        "recent_limit": parse_list_limit(request.GET.get("recent_limit"), base),
        "downloads_limit": parse_list_limit(request.GET.get("downloads_limit"), base),
    }


def normalize_download_type(method: str | None) -> str:
    return (method or "").strip().lower() or "other"


def download_type_label(dl_type: str) -> str:
    return DOWNLOAD_TYPE_LABELS.get(dl_type) or dl_type.replace("_", " ").title()


def download_type_sort_key(dl_type: str) -> tuple[int, str]:
    if dl_type in DOWNLOAD_TYPE_ORDER:
        return DOWNLOAD_TYPE_ORDER.index(dl_type), ""
    if dl_type in ("scheduler", "other"):
        return len(DOWNLOAD_TYPE_ORDER) + 2, dl_type
    return len(DOWNLOAD_TYPE_ORDER) + 1, download_type_label(dl_type).lower()


def format_timespan(seconds: float | int | None) -> str:
    """Format seconds into human-readable compact timespan string."""
    if seconds is None:
        return "-"
    sec = max(0, int(seconds))
    if sec < 60:
        return f"{sec}s"
    minutes = sec // 60
    rem_sec = sec % 60
    if minutes < 60:
        return f"{minutes}m {rem_sec}s" if rem_sec else f"{minutes}m"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours < 24:
        return f"{hours}h {rem_min}m"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d {rem_hours}h"


def format_bytes(num_bytes: int | float | None) -> str:
    """Format bytes into human-readable string with units."""
    if not num_bytes or num_bytes <= 0:
        return "0 B"
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024.0
    return f"{num:.1f} PB"


def _task_batch_fields(task: TaskQueue) -> dict[str, Any]:
    """Extract compact crawl-batch reference fields for a serialized task row."""
    meta = extract_crawl_batch_meta(task.options)
    if not meta:
        return {"batch_id": None, "batch_index": None, "batch_total": None}
    return {
        "batch_id": str(meta["id"]),
        "batch_index": int(meta["index"]) if meta.get("index") is not None else None,
        "batch_total": int(meta["total"]) if meta.get("total") is not None else None,
    }


def _build_crawl_batches_snapshot(
    now: Any,
    recent_tasks: list[TaskQueue],
    max_recent_batches: int = 20,
) -> list[dict[str, Any]]:
    """Build grouped progress & live provider-stat cards for split web-crawler batches.

    Includes every active batch (any ``PENDING`` or ``PROCESSING`` block) plus recently finished
    batches represented in ``recent_tasks``.
    """
    web_crawler_aliases = worker_client.get_task_type_aliases("web_crawler")

    active_batch_ids: set[str] = set()
    active_opts = TaskQueue.objects.filter(
        status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING],
        task_type__in=web_crawler_aliases,
        options__has_key="crawl_batch",
    ).values_list("options", flat=True)
    for opts in active_opts:
        meta = extract_crawl_batch_meta(opts)
        if meta:
            active_batch_ids.add(str(meta["id"]))

    recent_batch_ids: list[str] = []
    seen_recent: set[str] = set()
    for task in recent_tasks:
        if task.task_type not in web_crawler_aliases:
            continue
        meta = extract_crawl_batch_meta(task.options)
        if not meta:
            continue
        bid = str(meta["id"])
        if bid not in active_batch_ids and bid not in seen_recent:
            seen_recent.add(bid)
            recent_batch_ids.append(bid)
            if len(recent_batch_ids) >= max_recent_batches:
                break

    all_batch_ids = active_batch_ids | set(recent_batch_ids)
    if not all_batch_ids:
        return []

    batch_tasks_qs = (
        TaskQueue.objects.filter(
            task_type__in=web_crawler_aliases,
            options__crawl_batch__id__in=list(all_batch_ids),
        )
        .select_related("user")
        .order_by("created_at", "id")
    )

    grouped: dict[str, list[TaskQueue]] = defaultdict(list)
    for task in batch_tasks_qs:
        meta = extract_crawl_batch_meta(task.options)
        if meta:
            grouped[str(meta["id"])].append(task)

    batches: list[dict[str, Any]] = []
    for batch_id, tasks in grouped.items():
        if not tasks:
            continue

        def _block_sort_key(t: TaskQueue) -> tuple[int, int]:
            m = extract_crawl_batch_meta(t.options)
            raw_idx = m.get("index") if m else None
            try:
                idx_val = int(raw_idx) if raw_idx is not None else t.pk
            except (TypeError, ValueError):
                idx_val = t.pk
            return (idx_val, t.pk)

        tasks.sort(key=_block_sort_key)

        completed_blocks = 0
        processing_blocks = 0
        pending_blocks = 0
        failed_blocks = 0
        cancelled_blocks = 0
        total_blocks = len(tasks)
        total_links = 0
        counted_links = 0
        pending_priorities: set[int] = set()
        all_priorities: set[int] = set()
        username = "System"
        reason = ""
        earliest_created = None
        earliest_started = None
        latest_finished = None

        blocks_list: list[dict[str, Any]] = []
        for pos, task in enumerate(tasks, start=1):
            meta = extract_crawl_batch_meta(task.options) or {}
            meta_total = int(meta.get("total") or 0)
            if meta_total > total_blocks:
                total_blocks = meta_total
            meta_links = int(meta.get("total_links") or 0)
            if meta_links > total_links:
                total_links = meta_links

            raw_index = meta.get("index")
            try:
                block_index = int(raw_index) if raw_index is not None else pos
            except (TypeError, ValueError):
                block_index = pos

            if task.user and username == "System":
                username = task.user.username
            if task.reason and not reason:
                reason = task.reason

            all_priorities.add(task.priority)
            if task.status == TaskQueue.Status.COMPLETED:
                completed_blocks += 1
            elif task.status == TaskQueue.Status.PROCESSING:
                processing_blocks += 1
            elif task.status == TaskQueue.Status.PENDING:
                pending_blocks += 1
                pending_priorities.add(task.priority)
            elif task.status == TaskQueue.Status.FAILED:
                failed_blocks += 1
            elif task.status == TaskQueue.Status.CANCELLED:
                cancelled_blocks += 1

            if task.created_at and (earliest_created is None or task.created_at < earliest_created):
                earliest_created = task.created_at
            if task.started_at and (earliest_started is None or task.started_at < earliest_started):
                earliest_started = task.started_at
            if task.finished_at and (latest_finished is None or task.finished_at > latest_finished):
                latest_finished = task.finished_at

            task_args = task.args if isinstance(task.args, list) else []
            links_count = sum(1 for a in task_args if not (isinstance(a, str) and a.startswith("-")))
            counted_links += links_count

            elapsed_sec: float | None = None
            if task.status == TaskQueue.Status.PROCESSING and task.started_at:
                elapsed_sec = round((now - task.started_at).total_seconds(), 1)
            elif task.finished_at and task.started_at:
                elapsed_sec = round((task.finished_at - task.started_at).total_seconds(), 2)
            elif task.status == TaskQueue.Status.PENDING and task.created_at:
                elapsed_sec = round((now - task.created_at).total_seconds(), 0)

            is_delayed = bool(
                task.status == TaskQueue.Status.PENDING and task.not_before and task.not_before > now
            )

            task_providers_raw = aggregate_tasks_provider_stats([task])
            task_providers = {
                prov_name: enrich_provider_stats(prov_vals)
                for prov_name, prov_vals in task_providers_raw.items()
            }

            blocks_list.append(
                {
                    "id": task.pk,
                    "index": block_index,
                    "total": total_blocks,
                    "status": task.status,
                    "priority": task.priority,
                    "args": task_args[:5],
                    "args_count": len(task_args),
                    "links_count": links_count,
                    "worker_pid": task.worker_pid,
                    "created_at": task.created_at.isoformat() if task.created_at else None,
                    "started_at": task.started_at.isoformat() if task.started_at else None,
                    "finished_at": task.finished_at.isoformat() if task.finished_at else None,
                    "not_before": task.not_before.isoformat() if task.not_before else None,
                    "is_delayed": is_delayed,
                    "elapsed_seconds": elapsed_sec,
                    "elapsed_formatted": format_timespan(elapsed_sec) if elapsed_sec is not None else "-",
                    "error_message": task.error_message,
                    "has_traceback": bool(task.traceback),
                    "traceback": task.traceback,
                    "providers": task_providers,
                }
            )

        # Normalize per-block total to the batch's resolved total_blocks
        for blk in blocks_list:
            blk["total"] = total_blocks

        if total_links == 0:
            total_links = counted_links

        remaining_blocks = pending_blocks + processing_blocks
        done_blocks = completed_blocks + failed_blocks + cancelled_blocks
        is_active = remaining_blocks > 0

        if processing_blocks > 0:
            batch_status = "processing"
        elif pending_blocks > 0:
            batch_status = "pending"
        elif completed_blocks > 0 and failed_blocks == 0 and cancelled_blocks == 0:
            batch_status = "completed"
        elif failed_blocks > 0 and completed_blocks == 0 and cancelled_blocks == 0:
            batch_status = "failed"
        elif cancelled_blocks > 0 and completed_blocks == 0 and failed_blocks == 0:
            batch_status = "cancelled"
        else:
            batch_status = "partial"

        progress_percent = round((completed_blocks / total_blocks) * 100.0, 1) if total_blocks > 0 else 0.0
        done_percent = round((done_blocks / total_blocks) * 100.0, 1) if total_blocks > 0 else 0.0

        providers_raw = aggregate_tasks_provider_stats(tasks)
        providers_enriched = {
            prov_name: enrich_provider_stats(prov_vals)
            for prov_name, prov_vals in providers_raw.items()
        }
        totals_enriched = summarize_provider_stats(providers_raw)

        sorted_pending_priorities = sorted(pending_priorities)
        sorted_all_priorities = sorted(all_priorities)
        effective_priorities = sorted_pending_priorities or sorted_all_priorities
        effective_priority = effective_priorities[0] if effective_priorities else 10

        batch_elapsed: float | None = None
        if earliest_started is not None:
            end_dt = latest_finished if (not is_active and latest_finished is not None) else now
            batch_elapsed = max(0.0, round((end_dt - earliest_started).total_seconds(), 1))
        elif earliest_created is not None:
            batch_elapsed = max(0.0, round((now - earliest_created).total_seconds(), 0))

        batches.append(
            {
                "batch_id": batch_id,
                "is_active": is_active,
                "status": batch_status,
                "total_blocks": total_blocks,
                "known_blocks": len(tasks),
                "completed_blocks": completed_blocks,
                "processing_blocks": processing_blocks,
                "pending_blocks": pending_blocks,
                "failed_blocks": failed_blocks,
                "cancelled_blocks": cancelled_blocks,
                "remaining_blocks": remaining_blocks,
                "done_blocks": done_blocks,
                "progress_percent": progress_percent,
                "done_percent": done_percent,
                "total_links": total_links,
                "user": username,
                "reason": reason,
                "priority": effective_priority,
                "priorities": effective_priorities,
                "created_at": earliest_created.isoformat() if earliest_created else None,
                "created_at_formatted": earliest_created.strftime("%Y-%m-%d %H:%M:%S") if earliest_created else "-",
                "started_at": earliest_started.isoformat() if earliest_started else None,
                "finished_at": (latest_finished.isoformat() if (not is_active and latest_finished) else None),
                "elapsed_seconds": batch_elapsed,
                "elapsed_formatted": format_timespan(batch_elapsed) if batch_elapsed is not None else "-",
                "providers": providers_enriched,
                "totals": totals_enriched,
                "blocks": blocks_list,
            }
        )

    def _batch_sort_key(b: dict[str, Any]) -> tuple[int, int, int, float]:
        if b["is_active"]:
            state_rank = 0 if b["status"] == "processing" else 1
            ts_str = b["created_at"] or ""
            return (0, state_rank, int(b["priority"]), -(_iso_sort_ts(ts_str)))
        ts_str = b["finished_at"] or b["created_at"] or ""
        return (1, 0, 0, -(_iso_sort_ts(ts_str)))

    batches.sort(key=_batch_sort_key)
    return batches


def _iso_sort_ts(iso_str: str) -> float:
    if not iso_str:
        return 0.0
    try:
        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(iso_str)
        return parsed.timestamp() if parsed is not None else 0.0
    except Exception:
        return 0.0


def get_workers_dashboard_snapshot(
    limit: int = DEFAULT_LIST_LIMIT,
    pending_limit: int | None = None,
    recent_limit: int | None = None,
    downloads_limit: int | None = None,
) -> dict[str, Any]:
    """Compile a comprehensive operational snapshot of all background processes,
    worker runners, queues, schedulers, and active downloads.

    ``limit`` caps the number of rows of list sections, the specific ``*_limit``
    arguments override it per section. ``downloads_limit`` is applied per download type
    so a type with many idle downloads can't hide another type's active ones.
    """
    limit = parse_list_limit(limit)
    pending_limit = parse_list_limit(pending_limit, limit)
    recent_limit = parse_list_limit(recent_limit, limit)
    downloads_limit = parse_list_limit(downloads_limit, limit)

    now = django_tz.now()
    heartbeat_threshold_seconds = 30
    cutoff = now - timedelta(seconds=heartbeat_threshold_seconds)
    last_24h = now - timedelta(hours=24)

    # 1. Processing tasks & mapping to Worker PIDs
    processing_tasks_qs = (
        TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING)
        .select_related("user")
        .order_by("priority", "started_at")
    )
    processing_tasks: list[dict[str, Any]] = []
    worker_tasks: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for task in processing_tasks_qs:
        elapsed = round((now - task.started_at).total_seconds(), 1) if task.started_at else 0.0
        serialized_task = {
            "id": task.pk,
            "task_type": task.task_type,
            "args": (task.args or [])[:10],
            "args_count": len(task.args or []),
            "user": task.user.username if task.user else "System",
            "reason": task.reason,
            "event_action": task.event_action,
            "priority": task.priority,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "elapsed_seconds": elapsed,
            "elapsed_formatted": format_timespan(elapsed),
            "worker_pid": task.worker_pid,
            **_task_batch_fields(task),
        }
        processing_tasks.append(serialized_task)
        if task.worker_pid:
            worker_tasks[task.worker_pid].append(serialized_task)

    # 2. Schedulers & mapping to Worker PIDs
    schedulers_qs = SchedulerState.schedulers().order_by("name")
    schedulers_by_pid: dict[int, list[str]] = defaultdict(list)
    schedulers_list: list[dict[str, Any]] = []
    active_schedulers_count = 0

    for state in schedulers_qs:
        is_active = bool(state.is_running and state.last_heartbeat and state.last_heartbeat >= cutoff)
        if is_active:
            active_schedulers_count += 1
            if state.worker_pid:
                schedulers_by_pid[state.worker_pid].append(state.display_name or state.name)

        next_run_in = None
        next_run_in_formatted = "-"
        if not is_active or not state.is_running:
            next_run_formatted = "Stopped"
        elif state.next_run:
            diff = (state.next_run - now).total_seconds()
            if diff > 1.0:
                next_run_in = round(diff, 1)
                next_run_in_formatted = format_timespan(next_run_in)
                next_run_formatted = state.next_run.strftime("%Y-%m-%d %H:%M:%S")
            else:
                next_run_in = 0.0
                next_run_in_formatted = "Running..."
                next_run_formatted = "Running cycle"
        else:
            next_run_formatted = "Idle / None"

        current_downloads = []
        if isinstance(state.extra_data, dict) and "current_download" in state.extra_data:
            for d in state.extra_data["current_download"]:
                if isinstance(d, dict) and d.get("filename"):
                    spd = float(d.get("speed", 0.0) or 0.0)
                    filesize = int(d.get("filesize", 0) or 0)
                    downloaded = int(d.get("downloaded", 0) or 0)
                    current_downloads.append(
                        {
                            "filename": d.get("filename", ""),
                            "filesize": filesize,
                            "filesize_formatted": format_bytes(filesize),
                            "downloaded": downloaded,
                            "downloaded_formatted": format_bytes(downloaded),
                            "speed": spd,
                            "speed_formatted": f"{format_bytes(spd)}/s",
                            "index": d.get("index", 0),
                            "total": d.get("total", 0),
                        }
                    )

        schedulers_list.append(
            {
                "name": state.name,
                "display_name": state.display_name or state.name,
                "schedule_type": state.schedule_type,
                "is_running": state.is_running,
                "is_active": is_active,
                "worker_pid": state.worker_pid,
                "cycle_timer": state.cycle_timer,
                "cycle_timer_formatted": format_timespan(state.cycle_timer),
                "last_run": state.last_run.isoformat() if state.last_run else None,
                "last_run_formatted": state.last_run.strftime("%Y-%m-%d %H:%M:%S") if state.last_run else "Never",
                "next_run": state.next_run.isoformat() if (state.next_run and is_active and state.is_running) else None,
                "next_run_formatted": next_run_formatted,
                "next_run_in_seconds": next_run_in,
                "next_run_in_formatted": next_run_in_formatted,
                "command_requested": state.command_requested,
                "current_downloads": current_downloads,
            }
        )

    # 3. Worker Processes
    workers_qs = WorkerProcess.objects.all().order_by("-last_heartbeat")
    workers_list: list[dict[str, Any]] = []
    active_workers_count = 0
    stale_workers_count = 0
    stopped_workers_count = 0

    for w in workers_qs:
        heartbeat_age = round((now - w.last_heartbeat).total_seconds(), 1) if w.last_heartbeat else None
        is_running = w.status == "running"
        is_healthy = is_running and (heartbeat_age is not None and heartbeat_age < heartbeat_threshold_seconds)
        is_stale = is_running and not is_healthy

        if is_healthy:
            status_label = "healthy"
            active_workers_count += 1
        elif is_stale:
            status_label = "stale"
            stale_workers_count += 1
        else:
            status_label = "stopped"
            stopped_workers_count += 1

        uptime_sec = round((now - w.started_at).total_seconds(), 0) if w.started_at else None

        workers_list.append(
            {
                "pid": w.pid,
                "hostname": w.hostname or "local",
                "worker_type": w.worker_type,
                "started_at": w.started_at.isoformat() if w.started_at else None,
                "last_heartbeat": w.last_heartbeat.isoformat() if w.last_heartbeat else None,
                "heartbeat_age_seconds": heartbeat_age,
                "heartbeat_age_formatted": f"{int(heartbeat_age)}s ago" if heartbeat_age is not None else "-",
                "status": status_label,
                "raw_status": w.status,
                "uptime_seconds": uptime_sec,
                "uptime_formatted": format_timespan(uptime_sec) if uptime_sec else "-",
                "current_tasks": worker_tasks.get(w.pid, []),
                "hosted_schedulers": schedulers_by_pid.get(w.pid, []),
            }
        )

    # 4. Queues breakdown & Pending Tasks
    queue_counts_raw = (
        TaskQueue.objects.filter(status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING])
        .values("task_type", "status")
        .annotate(count=Count("id"))
    )
    counts_map: dict[str, dict[str, int]] = defaultdict(lambda: {"pending": 0, "processing": 0})
    for queue_entry in queue_counts_raw:
        t_type = queue_entry["task_type"]
        status = queue_entry["status"]
        if status in ("pending", "processing"):
            counts_map[t_type][status] += queue_entry["count"]

    known_types_set = {t[0] for t in KNOWN_TASK_TYPES}
    all_task_types = list(KNOWN_TASK_TYPES)
    # Add any custom/unregistered task types dynamically
    for t_type in counts_map:
        if t_type not in known_types_set:
            label = t_type.replace("_", " ").title()
            all_task_types.append((t_type, label, "Dynamic background task queue"))

    queues_summary: list[dict[str, Any]] = []
    total_pending_tasks = 0
    total_processing_tasks = 0
    for t_type, label, desc in all_task_types:
        p_count = counts_map[t_type]["pending"]
        proc_count = counts_map[t_type]["processing"]
        total_pending_tasks += p_count
        total_processing_tasks += proc_count
        queues_summary.append(
            {
                "task_type": t_type,
                "label": label,
                "description": desc,
                "pending_count": p_count,
                "processing_count": proc_count,
                "total_active": p_count + proc_count,
            }
        )

    # 5. Pending tasks preview (first N)
    pending_tasks_qs = (
        TaskQueue.objects.filter(status=TaskQueue.Status.PENDING)
        .select_related("user")
        .order_by("priority", "created_at", "id")[:pending_limit]
    )
    pending_tasks = []
    for task in pending_tasks_qs:
        wait_time = round((now - task.created_at).total_seconds(), 0) if task.created_at else 0
        pending_tasks.append(
            {
                "id": task.pk,
                "task_type": task.task_type,
                "args": (task.args or [])[:5],
                "args_count": len(task.args or []),
                "user": task.user.username if task.user else "System",
                "reason": task.reason,
                "priority": task.priority,
                "created_at": task.created_at.isoformat(),
                "wait_seconds": wait_time,
                "wait_formatted": format_timespan(wait_time),
                **_task_batch_fields(task),
            }
        )

    # 6. Recent tasks (completed / failed / cancelled)
    recent_tasks_objs = list(
        TaskQueue.objects.filter(
            status__in=[TaskQueue.Status.COMPLETED, TaskQueue.Status.FAILED, TaskQueue.Status.CANCELLED]
        )
        .select_related("user")
        .order_by("-finished_at", "-id")[:recent_limit]
    )
    recent_tasks = []
    for task in recent_tasks_objs:
        dur = None
        if task.finished_at and task.started_at:
            dur = round((task.finished_at - task.started_at).total_seconds(), 2)
        recent_tasks.append(
            {
                "id": task.pk,
                "task_type": task.task_type,
                "status": task.status,
                "user": task.user.username if task.user else "System",
                "args": (task.args or [])[:5],
                "args_count": len(task.args or []),
                "reason": task.reason,
                "priority": task.priority,
                "created_at": task.created_at.isoformat(),
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "finished_at": task.finished_at.isoformat() if task.finished_at else None,
                "duration_seconds": dur,
                "duration_formatted": format_timespan(dur) if dur is not None else "-",
                "error_message": task.error_message,
                "has_traceback": bool(task.traceback),
                "traceback": task.traceback,
                **_task_batch_fields(task),
            }
        )

    # 6b. Split Web-Crawler Batches (grouped by crawl_batch.id)
    crawl_batches = _build_crawl_batches_snapshot(now, recent_tasks_objs)
    active_crawl_batches_count = sum(1 for b in crawl_batches if b["is_active"])

    # 7. Download Events & Live Scheduler Downloads
    active_downloads_qs = DownloadEvent.objects.filter(completed=False).exclude(
        status__in=[
            DownloadEvent.Status.COMPLETED,
            DownloadEvent.Status.FAILED,
            DownloadEvent.Status.CANCELLED,
        ]
    )

    # Per-type totals over *all* active downloads (not limited), so the summary stays accurate.
    download_type_stats: dict[str, dict[str, Any]] = {}
    raw_methods_by_type: dict[str, set[str]] = defaultdict(set)

    def _type_entry(dl_type: str) -> dict[str, Any]:
        if dl_type not in download_type_stats:
            download_type_stats[dl_type] = {
                "type": dl_type,
                "label": download_type_label(dl_type),
                "total_count": 0,
                "in_progress_count": 0,
                "queued_count": 0,
                "transferring_count": 0,
                "shown_count": 0,
                "speed": 0.0,
            }
        return download_type_stats[dl_type]

    for row in active_downloads_qs.values("method", "status").annotate(count=Count("id")):
        dl_type = normalize_download_type(row["method"])
        raw_methods_by_type[dl_type].add(row["method"] or "")
        stats_item = _type_entry(dl_type)
        stats_item["total_count"] += row["count"]
        if row["status"] == DownloadEvent.Status.IN_PROGRESS:
            stats_item["in_progress_count"] += row["count"]
        elif row["status"] == DownloadEvent.Status.QUEUED:
            stats_item["queued_count"] += row["count"]

    # Fetch up to ``downloads_limit`` rows per type, in-progress before queued.
    status_rank = Case(
        When(status=DownloadEvent.Status.IN_PROGRESS, then=Value(0)),
        When(status=DownloadEvent.Status.QUEUED, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    downloads_events: list[tuple[str, DownloadEvent]] = []
    for dl_type, raw_methods in raw_methods_by_type.items():
        type_qs = (
            active_downloads_qs.filter(method__in=raw_methods)
            .select_related("archive", "gallery")
            .annotate(status_rank=status_rank)
            .order_by("status_rank", "-create_date", "-id")[:downloads_limit]
        )
        downloads_events.extend((dl_type, dl) for dl in type_qs)

    # Gather active downloads from schedulers (e.g. archive_download_queue, download_progress_checker)
    scheduler_downloads_map: dict[str, dict[str, Any]] = {}
    for s in schedulers_list:
        for cd in s.get("current_downloads", []):
            fn = cd.get("filename", "")
            if fn:
                scheduler_downloads_map[fn] = {
                    "scheduler_name": s.get("display_name") or s.get("name"),
                    "scheduler_key": s.get("name"),
                    "worker_pid": s.get("worker_pid"),
                    **cd,
                }

    downloads_list: list[dict[str, Any]] = []
    matched_filenames: set[str] = set()

    for dl_type, dl in downloads_events:
        spd = getattr(dl, "speed", 0.0) or 0.0
        dl_filesize = dl.total_size
        dl_progress = round(dl.progress, 1)

        matched_cd = None
        if dl.download_id:
            base_id = os.path.basename(dl.download_id)
            if base_id in scheduler_downloads_map:
                matched_cd = scheduler_downloads_map[base_id]
                matched_filenames.add(base_id)

        if not matched_cd and dl.name and dl.name in scheduler_downloads_map:
            matched_cd = scheduler_downloads_map[dl.name]
            matched_filenames.add(dl.name)

        if not matched_cd:
            for s_fn, cd_info in scheduler_downloads_map.items():
                if (dl.name and (dl.name in s_fn or s_fn in dl.name)) or (
                    dl.archive and dl.archive.title and dl.archive.title in s_fn
                ):
                    matched_cd = cd_info
                    matched_filenames.add(s_fn)
                    break

        if matched_cd:
            spd = float(matched_cd.get("speed", 0.0) or 0.0)
            if matched_cd.get("filesize", 0) > 0 and dl_filesize <= 0:
                dl_filesize = matched_cd["filesize"]
            if matched_cd.get("downloaded", 0) > 0 and dl_filesize > 0:
                dl_progress = round((matched_cd["downloaded"] / dl_filesize) * 100.0, 1)

        downloads_list.append(
            {
                "id": dl.pk,
                "name": dl.name or (dl.archive.title if dl.archive else "Download"),
                "type": dl_type,
                "type_label": download_type_label(dl_type),
                "method": dl.method,
                "provider": dl.provider,
                "archive_id": dl.archive_id,
                "archive_url": dl.archive.get_absolute_url() if dl.archive else None,
                "gallery_id": dl.gallery_id,
                "gallery_url": dl.gallery.get_absolute_url() if dl.gallery else None,
                "total_size": dl_filesize,
                "total_size_formatted": format_bytes(dl_filesize),
                "progress": dl_progress,
                "speed": spd,
                "speed_formatted": f"{format_bytes(spd)}/s",
                "is_transferring": spd > 0 or (dl.status == DownloadEvent.Status.IN_PROGRESS and dl_progress >= 100),
                "status": "in_progress (transferring)"
                if (dl.status == DownloadEvent.Status.IN_PROGRESS and dl_progress >= 100)
                else dl.status,
                "worker_pid": dl.worker_pid or (matched_cd.get("worker_pid") if matched_cd else None),
                "created_at": dl.create_date.isoformat() if dl.create_date else None,
            }
        )

    # Also include any active scheduler downloads not represented in DownloadEvent
    for s_fn, cd_info in scheduler_downloads_map.items():
        if s_fn not in matched_filenames:
            fs = int(cd_info.get("filesize", 0) or 0)
            dled = int(cd_info.get("downloaded", 0) or 0)
            prog = round((dled / fs) * 100.0, 1) if fs > 0 else 0.0
            spd = float(cd_info.get("speed", 0.0) or 0.0)
            dl_type = SCHEDULER_DOWNLOAD_TYPES.get(str(cd_info.get("scheduler_key") or ""), "scheduler")
            stats_item = _type_entry(dl_type)
            stats_item["total_count"] += 1
            stats_item["in_progress_count"] += 1
            downloads_list.append(
                {
                    "id": f"sched_{cd_info.get('scheduler_name')}_{s_fn[:10]}",
                    "name": s_fn,
                    "type": dl_type,
                    "type_label": download_type_label(dl_type),
                    "method": "",
                    "provider": "",
                    "archive_id": None,
                    "archive_url": None,
                    "gallery_id": None,
                    "gallery_url": None,
                    "total_size": fs,
                    "total_size_formatted": format_bytes(fs),
                    "progress": prog,
                    "speed": spd,
                    "speed_formatted": f"{format_bytes(spd)}/s",
                    "is_transferring": spd > 0,
                    "status": f"in_progress ({cd_info.get('scheduler_name')})",
                    "worker_pid": cd_info.get("worker_pid"),
                    "created_at": None,
                },
            )

    # Group by type; within a type, show downloads actually transferring data first,
    # then in-progress, then queued (stable sort keeps newest-first order otherwise).
    def _status_rank(status: str) -> int:
        if status.startswith(DownloadEvent.Status.IN_PROGRESS):
            return 0
        if status == DownloadEvent.Status.QUEUED:
            return 1
        return 2

    downloads_list.sort(
        key=lambda d: (
            download_type_sort_key(d["type"]),
            0 if d["is_transferring"] else 1,
            _status_rank(str(d["status"])),
        )
    )

    for d in downloads_list:
        stats_item = _type_entry(d["type"])
        stats_item["shown_count"] += 1
        stats_item["speed"] += float(d["speed"] or 0.0)
        if d["is_transferring"]:
            stats_item["transferring_count"] += 1

    download_types: list[dict[str, Any]] = []
    for dl_type in sorted(download_type_stats, key=download_type_sort_key):
        type_info = download_type_stats[dl_type]
        type_info["speed_formatted"] = f"{format_bytes(type_info['speed'])}/s"
        type_info["is_truncated"] = type_info["shown_count"] < type_info["total_count"]
        download_types.append(type_info)

    total_download_speed = sum(float(d.get("speed", 0.0) or 0.0) for d in downloads_list)
    total_active_downloads = sum(t["total_count"] for t in download_types)
    transferring_downloads = sum(t["transferring_count"] for t in download_types)

    # 8. 24h task stats (single aggregate query hitting taskqueue_finished_order_idx)
    recent_24h_counts = TaskQueue.objects.filter(
        status__in=[TaskQueue.Status.COMPLETED, TaskQueue.Status.FAILED, TaskQueue.Status.CANCELLED],
        finished_at__gte=last_24h,
    ).aggregate(
        failed_24h=Count("id", filter=Q(status=TaskQueue.Status.FAILED)),
        completed_24h=Count("id", filter=Q(status=TaskQueue.Status.COMPLETED)),
    )
    failed_24h = recent_24h_counts["failed_24h"]
    completed_24h = recent_24h_counts["completed_24h"]

    # 9. Diagram Topology Information for Client Rendering
    diagram_nodes = {
        "queues": [
            {
                "id": f"q_{q['task_type']}",
                "label": q["label"],
                "task_type": q["task_type"],
                "pending": q["pending_count"],
                "processing": q["processing_count"],
                "is_active": int(q["total_active"]) > 0,
            }
            for q in queues_summary
            if int(q["total_active"]) > 0 or q["task_type"] in ("web_crawler", "gallery_creator", "fileinfo_worker", "thumbnails_worker")
        ],
        "workers": [
            {
                "id": f"w_{w_info['pid']}",
                "pid": w_info["pid"],
                "hostname": w_info["hostname"],
                "status": w_info["status"],
                "is_healthy": w_info["status"] == "healthy",
                "current_job_count": len(w_info["current_tasks"]),
                "hosted_scheduler_count": len(w_info["hosted_schedulers"]),
            }
            for w_info in workers_list
            if w_info["status"] in ("healthy", "stale")
        ],
        "schedulers": [
            {
                "id": f"s_{s['name']}",
                "name": s["name"],
                "label": s["display_name"],
                "is_running": s["is_running"],
                "is_active": s["is_active"],
                "worker_pid": s["worker_pid"],
            }
            for s in schedulers_list
        ],
    }

    # Topology active connections (Edges)
    diagram_edges = []
    for worker_dict in workers_list:
        if worker_dict["status"] in ("healthy", "stale"):
            for t in worker_dict["current_tasks"]:
                diagram_edges.append(
                    {
                        "source": f"q_{t['task_type']}",
                        "target": f"w_{worker_dict['pid']}",
                        "type": "processing",
                        "label": f"Task #{t['id']}",
                    }
                )
            for s_name in worker_dict["hosted_schedulers"]:
                diagram_edges.append(
                    {
                        "source": f"w_{worker_dict['pid']}",
                        "target": f"s_{s_name}",
                        "type": "hosting",
                        "label": "Hosts",
                    }
                )

    # Default pool workers info from settings
    process_pool_workers = getattr(settings.CRAWLER_SETTINGS, "process_pool_workers", 0) or 4

    return {
        "summary": {
            "active_workers_count": active_workers_count,
            "stale_workers_count": stale_workers_count,
            "stopped_workers_count": stopped_workers_count,
            "total_workers_count": len(workers_list),
            "pending_tasks_count": total_pending_tasks,
            "processing_tasks_count": total_processing_tasks,
            "active_crawl_batches_count": active_crawl_batches_count,
            "total_crawl_batches_count": len(crawl_batches),
            "failed_tasks_24h_count": failed_24h,
            "completed_tasks_24h_count": completed_24h,
            "active_schedulers_count": active_schedulers_count,
            "total_schedulers_count": len(schedulers_list),
            "active_downloads_count": total_active_downloads,
            "shown_downloads_count": len(downloads_list),
            "transferring_downloads_count": transferring_downloads,
            "total_download_speed": total_download_speed,
            "total_download_speed_formatted": f"{format_bytes(total_download_speed)}/s",
            "process_pool_workers": process_pool_workers,
            "server_time": now.isoformat(),
            "server_time_formatted": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
        "limits": {
            "limit": limit,
            "pending_limit": pending_limit,
            "recent_limit": recent_limit,
            "downloads_limit": downloads_limit,
            "max_limit": MAX_LIST_LIMIT,
        },
        "workers": workers_list,
        "queues": queues_summary,
        "crawl_batches": crawl_batches,
        "active_tasks": processing_tasks,
        "pending_tasks": pending_tasks,
        "recent_tasks": recent_tasks,
        "schedulers": schedulers_list,
        "downloads": downloads_list,
        "download_types": download_types,
        "diagram": {
            "nodes": diagram_nodes,
            "edges": diagram_edges,
        },
    }


@login_required
def task_status(request: HttpRequest, pk: int) -> HttpResponse:
    """Status fragment of one queued task, polled by the page that started it (see viewer.utils.task_actions).

    Only staff and the user who queued the task can see it; for anyone else it is reported as missing.
    """
    task = TaskQueue.objects.filter(pk=pk).first()
    if task is not None and not can_view_task(request, task):
        task = None
    return render_task_status(request, task)


def task_batch_status(request: HttpRequest) -> HttpResponse:
    """JSON status summary for a batch of queued tasks (``?ids=1,2,3`` or repeated ``?id=1&id=2``).

    Accepts session or read-scoped bearer token auth (same as Archive Manager). Only staff and the user who queued
    each task can see its details; inaccessible or unknown IDs are reported as ``missing``.
    """
    authenticated, actual_user = double_check_auth(request)
    if not authenticated or actual_user is None:
        return JsonResponse({"error": "Authentication required."}, status=403)

    raw_ids: list[str] = []
    for part in request.GET.getlist("ids"):
        raw_ids.extend(p.strip() for p in part.split(",") if p.strip())
    for part in request.GET.getlist("id"):
        if part.strip():
            raw_ids.append(part.strip())

    task_ids: list[int] = []
    for raw in raw_ids:
        try:
            tid = int(raw)
        except ValueError:
            continue
        if tid > 0:
            task_ids.append(tid)
        if len(task_ids) >= 500:
            break

    return JsonResponse(task_batch_status_summary(task_ids, actual_user))


@login_required
def workers_status_api(request: HttpRequest) -> HttpResponse:
    """REST API returning live JSON status of all background worker processes,
    queues, schedulers, and downloads.

    Query parameters: ``limit`` (rows per list section, default 25, max 500) and the
    optional per-section overrides ``pending_limit``, ``recent_limit``, ``downloads_limit``
    (the latter is applied per download type).
    """
    if not request.user.is_staff:
        return JsonResponse({"error": "Admin permission required."}, status=403)

    data = get_workers_dashboard_snapshot(**get_list_limits_from_request(request))
    return JsonResponse(data, safe=False)


@login_required
@require_http_methods(["POST"])
def workers_action_api(request: HttpRequest) -> HttpResponse:
    """REST API handling administrative operations on background workers,
    schedulers, and task queues.
    """
    if not request.user.is_staff:
        return JsonResponse({"error": "Admin permission required."}, status=403)

    # Parse request data from JSON or form post
    payload: dict[str, Any] = {}
    if request.content_type == "application/json":
        try:
            payload = json.loads(request.body)
        except Exception:
            return JsonResponse({"error": "Invalid JSON body."}, status=400)
    else:
        payload = request.POST.dict()

    action = payload.get("action", "")
    target = payload.get("target", "")

    if not action:
        return JsonResponse({"error": "Action parameter is required."}, status=400)

    try:
        # --- Scheduler actions ---
        if action in ("scheduler_start", "scheduler_stop", "scheduler_force_run"):
            name = target or payload.get("name", "")
            if not name:
                return JsonResponse({"error": "Scheduler name is required."}, status=400)
            if not SchedulerState.schedulers().filter(name=name).exists():
                return JsonResponse({"error": f"Unknown scheduler '{name}'."}, status=404)

            if action == "scheduler_start":
                timer_arg = str(payload.get("timer", ""))
                SchedulerState.request_command(name, "start", timer_arg)
                logger.info("Admin %s requested 'start' for scheduler '%s'", request.user, name)
                return JsonResponse({"success": True, "message": f"Requested start command for scheduler '{name}'."})
            elif action == "scheduler_stop":
                SchedulerState.request_command(name, "stop")
                logger.info("Admin %s requested 'stop' for scheduler '%s'", request.user, name)
                return JsonResponse({"success": True, "message": f"Requested stop command for scheduler '{name}'."})
            else:
                SchedulerState.request_command(name, "force_run")
                logger.info("Admin %s requested 'force_run' for scheduler '%s'", request.user, name)
                return JsonResponse({"success": True, "message": f"Requested immediate run for scheduler '{name}'."})

        # --- Task Queue actions ---
        elif action == "task_cancel":
            task_id = int(target or payload.get("task_id", 0))
            if not task_id:
                return JsonResponse({"error": "Valid task_id is required."}, status=400)
            cancelled = worker_client.cancel_by_id(task_id)
            if cancelled:
                logger.info("Admin %s cancelled Task #%d", request.user, task_id)
                return JsonResponse({"success": True, "message": f"Task #{task_id} marked as cancelled."})
            return JsonResponse({"success": False, "error": f"Task #{task_id} could not be cancelled."}, status=400)

        elif action == "task_retry":
            task_id = int(target or payload.get("task_id", 0))
            if not task_id:
                return JsonResponse({"error": "Valid task_id is required."}, status=400)
            original = TaskQueue.objects.filter(pk=task_id).first()
            if not original:
                return JsonResponse({"error": f"Task #{task_id} not found."}, status=404)

            retry_options = dict(original.options) if isinstance(original.options, dict) else {}
            retry_options.pop("crawl_batch_stats", None)
            retry_options.pop("crawl_batch_summary_logged", None)

            new_task = TaskQueue.objects.create(
                task_type=original.task_type,
                args=original.args,
                options=retry_options,
                user=request.user,
                reason=f"Retry of #{original.pk}: {original.reason}".strip(": "),
                event_action=original.event_action,
                priority=original.priority,
                status=TaskQueue.Status.PENDING,
            )
            logger.info("Admin %s retried Task #%d as new Task #%d", request.user, task_id, new_task.pk)
            return JsonResponse({"success": True, "message": f"Task #{task_id} re-queued as Task #{new_task.pk}."})

        elif action == "batch_cancel":
            batch_id = str(target or payload.get("batch_id", "")).strip()
            if not batch_id:
                return JsonResponse({"error": "Valid batch_id is required."}, status=400)
            raw_include_proc = payload.get("include_processing", True)
            if isinstance(raw_include_proc, str):
                include_processing = raw_include_proc.strip().lower() not in ("0", "false", "no")
            else:
                include_processing = bool(raw_include_proc)
            result = worker_client.cancel_crawl_batch(batch_id, include_processing=include_processing)
            if not result["exists"]:
                return JsonResponse({"error": f"Crawl batch '{batch_id}' not found."}, status=404)
            cancelled_count = int(result["cancelled"])
            if cancelled_count == 0:
                return JsonResponse(
                    {"success": False, "error": f"No remaining blocks to cancel in batch '{batch_id}'."},
                    status=400,
                )
            parts: list[str] = []
            if result["cancelled_pending"]:
                parts.append(f"{result['cancelled_pending']} pending")
            if result["cancelled_processing"]:
                parts.append(f"{result['cancelled_processing']} running")
            detail = f" ({', '.join(parts)})" if parts else ""
            logger.info(
                "Admin %s cancelled %d remaining block(s) in crawl batch %s%s",
                request.user,
                cancelled_count,
                batch_id,
                detail,
            )
            return JsonResponse(
                {
                    "success": True,
                    "batch_id": batch_id,
                    "cancelled_count": cancelled_count,
                    "cancelled_pending": result["cancelled_pending"],
                    "cancelled_processing": result["cancelled_processing"],
                    "message": f"Cancelled {cancelled_count} remaining block(s) in batch {batch_id}{detail}.",
                }
            )

        elif action == "batch_reprioritize":
            batch_id = str(target or payload.get("batch_id", "")).strip()
            if not batch_id:
                return JsonResponse({"error": "Valid batch_id is required."}, status=400)
            raw_priority = payload.get("priority")
            if isinstance(raw_priority, (bool, float)) or raw_priority is None or str(raw_priority).strip() == "":
                return JsonResponse(
                    {"error": f"Priority must be an integer between {MIN_BATCH_PRIORITY} and {MAX_BATCH_PRIORITY}."},
                    status=400,
                )
            try:
                priority = int(raw_priority)
            except (TypeError, ValueError):
                return JsonResponse(
                    {"error": f"Priority must be an integer between {MIN_BATCH_PRIORITY} and {MAX_BATCH_PRIORITY}."},
                    status=400,
                )
            if not (MIN_BATCH_PRIORITY <= priority <= MAX_BATCH_PRIORITY):
                return JsonResponse(
                    {"error": f"Priority must be an integer between {MIN_BATCH_PRIORITY} and {MAX_BATCH_PRIORITY}."},
                    status=400,
                )
            result = worker_client.reprioritize_crawl_batch(batch_id, priority)
            if not result["exists"]:
                return JsonResponse({"error": f"Crawl batch '{batch_id}' not found."}, status=404)
            updated_count = int(result["updated"])
            if updated_count == 0:
                return JsonResponse(
                    {"success": False, "error": f"No pending blocks to reprioritize in batch '{batch_id}'."},
                    status=400,
                )
            logger.info(
                "Admin %s reprioritized %d pending block(s) in crawl batch %s to priority %d",
                request.user,
                updated_count,
                batch_id,
                priority,
            )
            return JsonResponse(
                {
                    "success": True,
                    "batch_id": batch_id,
                    "priority": priority,
                    "updated_count": updated_count,
                    "message": f"Updated priority to {priority} for {updated_count} pending block(s) in batch {batch_id}.",
                }
            )

        elif action in ("clear_pending_tasks", "purge_pending_tasks"):
            task_type = str(target or payload.get("task_type", "")).strip()
            mode = str(payload.get("mode", "delete")).strip().lower()
            deleted = worker_client.clear_pending_tasks(
                task_type=task_type if task_type and task_type != "all" else None,
                mode=mode,
            )
            type_str = f" for '{task_type}'" if (task_type and task_type != "all") else ""
            verb = "Cancelled" if mode == "cancel" else "Cleared"
            logger.info("Admin %s %s %d pending task(s)%s", request.user, verb.lower(), deleted, type_str)
            return JsonResponse({"success": True, "message": f"{verb} {deleted} pending task(s){type_str}."})

        # --- Maintenance actions ---
        elif action == "worker_delete":
            pid = int(target or payload.get("pid", 0))
            if not pid:
                return JsonResponse({"error": "Valid worker PID is required."}, status=400)
            worker = WorkerProcess.objects.filter(pid=pid).first()
            if not worker:
                return JsonResponse({"error": f"Worker PID {pid} not found."}, status=404)
            # Check if actively running with recent heartbeat
            heartbeat_cutoff = django_tz.now() - timedelta(seconds=30)
            if worker.status == "running" and worker.last_heartbeat and worker.last_heartbeat >= heartbeat_cutoff:
                return JsonResponse(
                    {"error": f"Worker PID {pid} is actively running with live heartbeat. Stop the process first."},
                    status=400,
                )
            SchedulerState.objects.filter(worker_pid=pid).update(is_running=False, worker_pid=None)
            TaskQueue.objects.filter(worker_pid=pid, status=TaskQueue.Status.PROCESSING).update(
                status=TaskQueue.Status.PENDING, worker_pid=None
            )
            from django.db.models import F
            from django.db.models.functions import Greatest

            DownloadEvent.objects.filter(
                worker_pid=pid,
                completed=False,
                status=DownloadEvent.Status.IN_PROGRESS,
            ).exclude(download_url="").update(
                status=DownloadEvent.Status.QUEUED,
                worker_pid=None,
                progress=0.0,
                attempts=Greatest(F("attempts") - 1, 0),
            )
            worker.delete()
            logger.info("Admin %s deleted WorkerProcess PID %d", request.user, pid)
            return JsonResponse({"success": True, "message": f"Worker process PID {pid} removed."})

        elif action == "purge_workers":
            days = payload.get("days")
            WorkerProcess.cleanup_dead_workers(threshold_seconds=60)
            cutoff = django_tz.now() - timedelta(seconds=30)
            from django.db.models import Q

            qs = WorkerProcess.objects.filter(Q(status="stopped") | Q(last_heartbeat__lt=cutoff))
            if days is not None and str(days).isdigit():
                days_cutoff = django_tz.now() - timedelta(days=int(days))
                qs = qs.filter(last_heartbeat__lt=days_cutoff)

            dead_pids = list(qs.values_list("pid", flat=True))
            if dead_pids:
                SchedulerState.objects.filter(worker_pid__in=dead_pids).update(is_running=False, worker_pid=None)
                TaskQueue.objects.filter(worker_pid__in=dead_pids, status=TaskQueue.Status.PROCESSING).update(
                    status=TaskQueue.Status.PENDING, worker_pid=None
                )
                from django.db.models import F
                from django.db.models.functions import Greatest

                DownloadEvent.objects.filter(
                    worker_pid__in=dead_pids,
                    completed=False,
                    status=DownloadEvent.Status.IN_PROGRESS,
                ).exclude(download_url="").update(
                    status=DownloadEvent.Status.QUEUED,
                    worker_pid=None,
                    progress=0.0,
                    attempts=Greatest(F("attempts") - 1, 0),
                )
            deleted, _ = qs.delete()
            logger.info("Admin %s purged %d inactive worker process records", request.user, deleted)
            time_str = f" older than {days} days" if (days is not None and str(days).isdigit()) else ""
            return JsonResponse(
                {"success": True, "message": f"Purged {deleted} inactive worker process record(s){time_str}."}
            )

        elif action == "cleanup_workers":
            cleaned = WorkerProcess.cleanup_dead_workers(threshold_seconds=60)
            logger.info("Admin %s triggered cleanup_dead_workers (cleaned %d)", request.user, cleaned)
            return JsonResponse({"success": True, "message": f"Cleaned up {cleaned} dead worker process(es)."})

        elif action == "purge_completed_tasks":
            days = int(payload.get("days", 7))
            purge_cutoff = django_tz.now() - timedelta(days=days)
            deleted, _ = TaskQueue.objects.filter(
                status__in=[TaskQueue.Status.COMPLETED, TaskQueue.Status.CANCELLED],
                finished_at__lt=purge_cutoff,
            ).delete()
            logger.info("Admin %s purged %d old tasks older than %d days", request.user, deleted, days)
            return JsonResponse(
                {"success": True, "message": f"Purged {deleted} completed/cancelled task(s) older than {days} days."}
            )

        else:
            return JsonResponse({"error": f"Unknown action: '{action}'"}, status=400)

    except Exception as e:
        logger.exception("Error executing worker action '%s': %s", action, e)
        return JsonResponse({"error": str(e)}, status=500)
