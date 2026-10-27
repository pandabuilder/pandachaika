# -*- coding: utf-8 -*-
import datetime
from datetime import timedelta
import json
import logging
import os
from collections import defaultdict
from typing import Any

import django.utils.timezone as django_tz
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from viewer.models import DownloadEvent
from workers import client as worker_client
from workers.models import SchedulerState, TaskQueue, WorkerProcess

logger = logging.getLogger(__name__)

KNOWN_TASK_TYPES: list[tuple[str, str, str]] = [
    ("web_crawler", "Web Crawler", "Crawls URLs and downloads galleries/archives"),
    ("folder_crawler", "Folder Crawler", "Scans local filesystem folders for new archives"),
    ("fileinfo_worker", "File Info", "Recalculates archive CRC32, file list, and sizes"),
    ("thumbnails_worker", "Thumbnails", "Regenerates cover and page thumbnails"),
    ("match_unmatched_worker", "Local Matcher", "Matches unmatched archives to galleries locally"),
    ("web_search_worker", "Wanted Provider Search", "Searches wanted galleries against external providers"),
    ("wanted_local_search_worker", "Wanted Local Search", "Searches wanted galleries against local database"),
    ("match_unmatched_gallery_groups_worker", "Match Groups", "Matches archives within gallery match groups"),
    ("web_match_worker", "Web Matcher", "Web matching for archive groups"),
]


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


def get_workers_dashboard_snapshot() -> dict[str, Any]:
    """Compile a comprehensive operational snapshot of all background processes,
    worker runners, queues, schedulers, and active downloads.
    """
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
        }
        processing_tasks.append(serialized_task)
        if task.worker_pid:
            worker_tasks[task.worker_pid].append(serialized_task)

    # 2. Schedulers & mapping to Worker PIDs
    schedulers_qs = SchedulerState.objects.all().order_by("name")
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
    for entry in queue_counts_raw:
        t_type = entry["task_type"]
        status = entry["status"]
        if status in ("pending", "processing"):
            counts_map[t_type][status] += entry["count"]

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

    # 5. Pending tasks preview (first 25)
    pending_tasks_qs = (
        TaskQueue.objects.filter(status=TaskQueue.Status.PENDING)
        .select_related("user")
        .order_by("priority", "created_at", "id")[:25]
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
            }
        )

    # 6. Recent tasks (completed / failed / cancelled)
    recent_tasks_qs = (
        TaskQueue.objects.filter(
            status__in=[TaskQueue.Status.COMPLETED, TaskQueue.Status.FAILED, TaskQueue.Status.CANCELLED]
        )
        .select_related("user")
        .order_by("-finished_at", "-id")[:25]
    )
    recent_tasks = []
    for task in recent_tasks_qs:
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
            }
        )

    # 7. Download Events & Live Scheduler Downloads
    downloads_qs = (
        DownloadEvent.objects.filter(completed=False)
        .exclude(
            status__in=[
                DownloadEvent.Status.COMPLETED,
                DownloadEvent.Status.FAILED,
                DownloadEvent.Status.CANCELLED,
            ]
        )
        .select_related("archive", "gallery")
        .order_by("-create_date")[:25]
    )

    # Gather active downloads from schedulers (e.g. archive_download_queue, post_downloader)
    scheduler_downloads_map: dict[str, dict[str, Any]] = {}
    for s in schedulers_list:
        for cd in s.get("current_downloads", []):
            fn = cd.get("filename", "")
            if fn:
                scheduler_downloads_map[fn] = {
                    "scheduler_name": s.get("display_name") or s.get("name"),
                    "worker_pid": s.get("worker_pid"),
                    **cd,
                }

    downloads_list: list[dict[str, Any]] = []
    matched_filenames: set[str] = set()

    for dl in downloads_qs:
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
                "archive_id": dl.archive_id,
                "archive_url": dl.archive.get_absolute_url() if dl.archive else None,
                "gallery_id": dl.gallery_id,
                "gallery_url": dl.gallery.get_absolute_url() if dl.gallery else None,
                "total_size": dl_filesize,
                "total_size_formatted": format_bytes(dl_filesize),
                "progress": dl_progress,
                "speed": spd,
                "speed_formatted": f"{format_bytes(spd)}/s",
                "status": dl.status,
                "worker_pid": dl.worker_pid or (matched_cd.get("worker_pid") if matched_cd else None),
            }
        )

    # Also include any active scheduler downloads not represented in DownloadEvent
    for s_fn, cd_info in scheduler_downloads_map.items():
        if s_fn not in matched_filenames:
            fs = int(cd_info.get("filesize", 0) or 0)
            dled = int(cd_info.get("downloaded", 0) or 0)
            prog = round((dled / fs) * 100.0, 1) if fs > 0 else 0.0
            spd = float(cd_info.get("speed", 0.0) or 0.0)
            downloads_list.insert(
                0,
                {
                    "id": f"sched_{cd_info.get('scheduler_name')}_{s_fn[:10]}",
                    "name": s_fn,
                    "archive_id": None,
                    "archive_url": None,
                    "gallery_id": None,
                    "gallery_url": None,
                    "total_size": fs,
                    "total_size_formatted": format_bytes(fs),
                    "progress": prog,
                    "speed": spd,
                    "speed_formatted": f"{format_bytes(spd)}/s",
                    "status": f"in_progress ({cd_info.get('scheduler_name')})",
                    "worker_pid": cd_info.get("worker_pid"),
                },
            )

    total_download_speed = sum(float(d.get("speed", 0.0) or 0.0) for d in downloads_list)

    # 8. 24h task stats
    failed_24h = TaskQueue.objects.filter(status=TaskQueue.Status.FAILED, finished_at__gte=last_24h).count()
    completed_24h = TaskQueue.objects.filter(status=TaskQueue.Status.COMPLETED, finished_at__gte=last_24h).count()

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
            if int(q["total_active"]) > 0 or q["task_type"] in ("web_crawler", "fileinfo_worker", "thumbnails_worker")
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
            "failed_tasks_24h_count": failed_24h,
            "completed_tasks_24h_count": completed_24h,
            "active_schedulers_count": active_schedulers_count,
            "total_schedulers_count": len(schedulers_list),
            "active_downloads_count": len(downloads_list),
            "total_download_speed": total_download_speed,
            "total_download_speed_formatted": f"{format_bytes(total_download_speed)}/s",
            "process_pool_workers": process_pool_workers,
            "server_time": now.isoformat(),
            "server_time_formatted": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        },
        "workers": workers_list,
        "queues": queues_summary,
        "active_tasks": processing_tasks,
        "pending_tasks": pending_tasks,
        "recent_tasks": recent_tasks,
        "schedulers": schedulers_list,
        "downloads": downloads_list,
        "diagram": {
            "nodes": diagram_nodes,
            "edges": diagram_edges,
        },
    }


@login_required
def workers_status_api(request: HttpRequest) -> HttpResponse:
    """REST API returning live JSON status of all background worker processes,
    queues, schedulers, and downloads.
    """
    if not request.user.is_staff:
        return JsonResponse({"error": "Admin permission required."}, status=403)

    data = get_workers_dashboard_snapshot()
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
        if action == "scheduler_start":
            name = target or payload.get("name", "")
            if not name:
                return JsonResponse({"error": "Scheduler name is required."}, status=400)
            timer_arg = str(payload.get("timer", ""))
            SchedulerState.request_command(name, "start", timer_arg)
            logger.info("Admin %s requested 'start' for scheduler '%s'", request.user, name)
            return JsonResponse(
                {"success": True, "message": f"Requested start command for scheduler '{name}'."}
            )

        elif action == "scheduler_stop":
            name = target or payload.get("name", "")
            if not name:
                return JsonResponse({"error": "Scheduler name is required."}, status=400)
            SchedulerState.request_command(name, "stop")
            logger.info("Admin %s requested 'stop' for scheduler '%s'", request.user, name)
            return JsonResponse(
                {"success": True, "message": f"Requested stop command for scheduler '{name}'."}
            )

        elif action == "scheduler_force_run":
            name = target or payload.get("name", "")
            if not name:
                return JsonResponse({"error": "Scheduler name is required."}, status=400)
            SchedulerState.request_command(name, "force_run")
            logger.info("Admin %s requested 'force_run' for scheduler '%s'", request.user, name)
            return JsonResponse(
                {"success": True, "message": f"Requested immediate run for scheduler '{name}'."}
            )

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

            new_task = TaskQueue.objects.create(
                task_type=original.task_type,
                args=original.args,
                options=original.options,
                user=request.user,
                reason=f"Retry of #{original.pk}: {original.reason}".strip(": "),
                event_action=original.event_action,
                priority=original.priority,
                status=TaskQueue.Status.PENDING,
            )
            logger.info("Admin %s retried Task #%d as new Task #%d", request.user, task_id, new_task.pk)
            return JsonResponse(
                {"success": True, "message": f"Task #{task_id} re-queued as Task #{new_task.pk}."}
            )

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
            worker.delete()
            logger.info("Admin %s deleted WorkerProcess PID %d", request.user, pid)
            return JsonResponse({"success": True, "message": f"Worker process PID {pid} removed."})

        elif action == "purge_workers":
            days = payload.get("days")
            WorkerProcess.cleanup_dead_workers(threshold_seconds=60)
            cutoff = django_tz.now() - timedelta(seconds=30)
            from django.db.models import Q
            qs = WorkerProcess.objects.filter(
                Q(status="stopped") | Q(last_heartbeat__lt=cutoff)
            )
            if days is not None and str(days).isdigit():
                days_cutoff = django_tz.now() - timedelta(days=int(days))
                qs = qs.filter(last_heartbeat__lt=days_cutoff)

            dead_pids = list(qs.values_list("pid", flat=True))
            if dead_pids:
                SchedulerState.objects.filter(worker_pid__in=dead_pids).update(is_running=False, worker_pid=None)
                TaskQueue.objects.filter(worker_pid__in=dead_pids, status=TaskQueue.Status.PROCESSING).update(
                    status=TaskQueue.Status.PENDING, worker_pid=None
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
            return JsonResponse(
                {"success": True, "message": f"Cleaned up {cleaned} dead worker process(es)."}
            )

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
