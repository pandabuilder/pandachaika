"""Admin activity report: what happened in a time window, and what needs an admin's attention right now.

``build_admin_report`` is the single source for the staff report page, its JSON output and (later) the scheduled
report emails. It only reads the database (and the disk usage of the media folders), and returns plain
JSON-serializable data (datetimes aside, which ``DjangoJSONEncoder`` handles).

Two kinds of data are mixed in a report:
- Activity in the window (``since`` to ``until``): archives and galleries added, downloads, failed tasks, new logs...
- Current state, independent of the window: queue backlogs, scheduler and worker health, disk space, pending
  submissions. These are what an admin has to act on, whatever window they look at.
"""

import dataclasses
import datetime
import logging
import os
import re
import shutil
import time
import typing
from collections import Counter
from typing import Any

from django.conf import settings
from django.db.models import Count, Max, Q, QuerySet, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

SECTION_CONTENT = "content"
SECTION_WANTED = "wanted"
SECTION_DOWNLOADS = "downloads"
SECTION_TASKS = "tasks"
SECTION_LOGS = "logs"
SECTION_HEALTH = "health"
SECTION_MODERATION = "moderation"
SECTION_INTEGRITY = "integrity"
SECTION_STORAGE = "storage"

SECTIONS: tuple[tuple[str, str], ...] = (
    (SECTION_CONTENT, "Content"),
    (SECTION_WANTED, "Wanted galleries"),
    (SECTION_DOWNLOADS, "Downloads"),
    (SECTION_TASKS, "Tasks and monitored links"),
    (SECTION_LOGS, "Server logs"),
    (SECTION_HEALTH, "Workers and schedulers"),
    (SECTION_MODERATION, "Submissions"),
    (SECTION_INTEGRITY, "Archive file checks"),
    (SECTION_STORAGE, "Storage"),
)
SECTION_KEYS: tuple[str, ...] = tuple(key for key, _ in SECTIONS)

LOG_LEVEL_CHOICES: tuple[tuple[int, str], ...] = (
    (logging.WARNING, "WARNING"),
    (logging.ERROR, "ERROR"),
    (logging.CRITICAL, "CRITICAL"),
)

DEFAULT_TOP_N = 10
MAX_TOP_N = 100
# Longest window a report can cover, to keep the log and activity aggregations bounded.
MAX_WINDOW = datetime.timedelta(days=366)

# Distinct (logger, level, message) groups read from the database before merging similar messages in Python.
LOG_GROUPS_SCAN_LIMIT = 5000
LOG_MESSAGE_MAX_LENGTH = 300

# A scheduler marked as running whose heartbeat is older than this is not really running.
SCHEDULER_HEARTBEAT_STALE = datetime.timedelta(minutes=5)
# A running scheduler is overdue when its last run is older than this many cycles (plus the grace period).
SCHEDULER_OVERDUE_CYCLES = 2
SCHEDULER_OVERDUE_GRACE = datetime.timedelta(minutes=10)
WORKER_HEARTBEAT_STALE = datetime.timedelta(seconds=60)
# Queued downloads waiting longer than this are reported as stuck.
DOWNLOAD_QUEUED_STUCK = datetime.timedelta(hours=6)
# Disk usage over this percentage is flagged.
DISK_USAGE_WARNING_PERCENT = 90.0

ATTENTION_ERROR = "error"
ATTENTION_WARNING = "warning"
ATTENTION_INFO = "info"

_NUMBER_RE = re.compile(r"\d+")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{8,}\b")


@dataclasses.dataclass(frozen=True)
class ReportOptions:
    """What a report covers. ``sections=None`` means every section."""

    since: datetime.datetime
    until: datetime.datetime
    sections: tuple[str, ...] | None = None
    min_log_level: int = logging.WARNING
    top_n: int = DEFAULT_TOP_N

    def __post_init__(self) -> None:
        if self.since >= self.until:
            raise ValueError("The report start must be before its end.")
        if self.until - self.since > MAX_WINDOW:
            raise ValueError(f"A report can cover at most {MAX_WINDOW.days} days.")
        if self.sections is not None:
            unknown = set(self.sections) - set(SECTION_KEYS)
            if unknown:
                raise ValueError(f"Unknown report sections: {', '.join(sorted(unknown))}")
        if not 1 <= self.top_n <= MAX_TOP_N:
            raise ValueError(f"top_n must be between 1 and {MAX_TOP_N}.")

    @property
    def enabled_sections(self) -> tuple[str, ...]:
        if self.sections is None:
            return SECTION_KEYS
        return tuple(key for key in SECTION_KEYS if key in self.sections)


def build_admin_report(options: ReportOptions, crawler_settings: Any = None) -> dict[str, Any]:
    """Build the report for ``options``. Each section is independent: one failing doesn't drop the others."""
    if crawler_settings is None:
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)

    builders: dict[str, typing.Callable[[ReportOptions, Any], dict[str, Any]]] = {
        SECTION_CONTENT: _content_section,
        SECTION_WANTED: _wanted_section,
        SECTION_DOWNLOADS: _downloads_section,
        SECTION_TASKS: _tasks_section,
        SECTION_LOGS: _logs_section,
        SECTION_HEALTH: _health_section,
        SECTION_MODERATION: _moderation_section,
        SECTION_INTEGRITY: _integrity_section,
        SECTION_STORAGE: _storage_section,
    }

    sections: dict[str, dict[str, Any]] = {}
    for key in options.enabled_sections:
        try:
            sections[key] = builders[key](options, crawler_settings)
        except Exception as e:
            logger.exception("Admin report: could not build section '%s'", key)
            sections[key] = {"error": f"{type(e).__name__}: {e}"}

    return {
        "generated_at": timezone.now(),
        "since": options.since,
        "until": options.until,
        "min_log_level": logging.getLevelName(options.min_log_level),
        "top_n": options.top_n,
        "section_titles": {key: title for key, title in SECTIONS if key in sections},
        "attention": _attention_items(sections),
        "sections": sections,
    }


def _in_window(field: str, options: ReportOptions) -> Q:
    return Q(**{f"{field}__gte": options.since, f"{field}__lt": options.until})


def _count_by(queryset: QuerySet, field: str, top_n: int | None = None) -> list[dict[str, Any]]:
    """``[{"key": value, "count": n}, ...]`` ordered by count, most frequent first."""
    rows = queryset.order_by().values(field).annotate(count=Count("pk")).order_by("-count", field)
    if top_n is not None:
        rows = rows[:top_n]
    return [{"key": row[field], "count": row["count"]} for row in rows]


def _first_line(text: str | None, max_length: int = LOG_MESSAGE_MAX_LENGTH) -> str:
    lines = (text or "").strip().splitlines()
    line = lines[0] if lines else ""
    if len(line) > max_length:
        line = line[: max_length - 1] + "…"
    return line


# ---------------------------------------------------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------------------------------------------------


def _content_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import Archive, ArchiveRecycleEntry, EventLog, Gallery

    archives = Archive.objects.filter(_in_window("create_date", options))
    archive_totals = archives.aggregate(
        count=Count("pk"),
        total_size=Sum("filesize"),
        total_files=Sum("filecount"),
        public=Count("pk", filter=Q(public=True)),
    )
    origin_labels = dict(Archive.ORIGIN_CHOICES)
    archives_by_origin = [
        {"key": origin_labels.get(row["key"], str(row["key"])), "count": row["count"]}
        for row in _count_by(archives, "origin")
    ]

    galleries = Gallery.objects.filter(_in_window("create_date", options))
    # "No metadata" galleries only mark links as already seen, they're not content.
    galleries_with_metadata = galleries.exclude(status=Gallery.StatusChoices.NO_METADATA)
    status_labels = dict(Gallery.StatusChoices.choices)

    activity = EventLog.objects.filter(_in_window("create_date", options))

    return {
        "archives_added": archive_totals["count"],
        "archives_added_public": archive_totals["public"],
        "archives_added_size": archive_totals["total_size"] or 0,
        "archives_added_files": archive_totals["total_files"] or 0,
        "archives_by_origin": archives_by_origin,
        "archives_recycled": ArchiveRecycleEntry.objects.filter(_in_window("date_deleted", options)).count(),
        "galleries_added": galleries_with_metadata.count(),
        "galleries_added_no_metadata": galleries.filter(status=Gallery.StatusChoices.NO_METADATA).count(),
        "galleries_by_provider": _count_by(galleries_with_metadata, "provider", options.top_n),
        "galleries_by_status": [
            {"key": status_labels.get(row["key"], str(row["key"])), "count": row["count"]}
            for row in _count_by(galleries, "status")
        ],
        "activity_total": activity.count(),
        "activity_by_action": _count_by(activity, "action", options.top_n),
    }


def _wanted_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import FoundGallery, WantedGallery

    found = FoundGallery.objects.filter(_in_window("create_date", options))
    recent_found = found.select_related("wanted_gallery", "gallery").order_by("-create_date")[: options.top_n]

    return {
        "found": found.count(),
        "wanted_galleries_found": found.values("wanted_gallery").distinct().count(),
        "recent_found": [
            {
                "wanted_gallery_id": entry.wanted_gallery_id,
                "wanted_gallery": entry.wanted_gallery.title or entry.wanted_gallery.search_title,
                "gallery_id": entry.gallery_id,
                "gallery": entry.gallery.title,
                "provider": entry.gallery.provider,
                "source": entry.source or "",
                "date": entry.create_date,
            }
            for entry in recent_found
        ],
        "wanted_created": WantedGallery.objects.filter(_in_window("create_date", options)).count(),
        "wanted_searching": WantedGallery.objects.filter(should_search=True, found=False).count(),
    }


def _downloads_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import DownloadEvent

    now = timezone.now()
    finished = DownloadEvent.objects.filter(_in_window("completed_date", options))
    failed = finished.filter(failed=True)
    completed = finished.filter(completed=True, failed=False)
    completed_totals = completed.aggregate(count=Count("pk"), total_size=Sum("total_size"))

    recent_failures = failed.order_by("-completed_date")[: options.top_n]

    queued = DownloadEvent.objects.queued()
    oldest_queued = queued.order_by("create_date").values_list("create_date", flat=True).first()

    return {
        "created": DownloadEvent.objects.filter(_in_window("create_date", options)).count(),
        "completed": completed_totals["count"],
        "completed_size": completed_totals["total_size"] or 0,
        "failed": failed.exclude(status=DownloadEvent.Status.CANCELLED).count(),
        "cancelled": failed.filter(status=DownloadEvent.Status.CANCELLED).count(),
        "failed_by_provider": _count_by(failed.exclude(status=DownloadEvent.Status.CANCELLED), "provider"),
        "failed_by_method": _count_by(failed.exclude(status=DownloadEvent.Status.CANCELLED), "method"),
        "recent_failures": [
            {
                "id": event.pk,
                "name": event.name,
                "provider": event.provider,
                "method": event.method,
                "status": event.status,
                "error": _first_line(event.error_message),
                "date": event.completed_date,
            }
            for event in recent_failures
        ],
        "queued_now": queued.count(),
        "downloading_now": DownloadEvent.objects.downloading().count(),
        "oldest_queued": oldest_queued,
        "stuck_queued": queued.filter(create_date__lt=now - DOWNLOAD_QUEUED_STUCK).count(),
        "stuck_queued_threshold_hours": DOWNLOAD_QUEUED_STUCK.total_seconds() / 3600,
    }


def _tasks_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import ProcessedLinks
    from workers.models import TaskQueue

    finished = TaskQueue.objects.filter(_in_window("finished_at", options))
    failed = finished.filter(status=TaskQueue.Status.FAILED)
    recent_failures = failed.order_by("-finished_at")[: options.top_n]

    pending_links = ProcessedLinks.objects.filter(status=ProcessedLinks.Status.PENDING)
    oldest_pending_link = pending_links.order_by("create_date").values_list("create_date", flat=True).first()

    return {
        "completed": finished.filter(status=TaskQueue.Status.COMPLETED).count(),
        "failed": failed.count(),
        "failed_by_type": _count_by(failed, "task_type"),
        "recent_failures": [
            {
                "id": task.pk,
                "task_type": task.task_type,
                "reason": task.reason,
                "error": _first_line(task.error_message),
                "attempts": task.attempts,
                "date": task.finished_at,
            }
            for task in recent_failures
        ],
        "pending_now": TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count(),
        "processing_now": TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).count(),
        "links_pending_now": pending_links.count(),
        "links_oldest_pending": oldest_pending_link,
        # Failed links don't record when they failed, so this is the current total, not the window's.
        "links_failed_total": ProcessedLinks.objects.filter(status=ProcessedLinks.Status.FAILED).count(),
    }


def normalize_log_message(message: str) -> str:
    """Collapse the variable parts of a log message (ids, hashes, numbers) so similar messages group together."""
    first_line = _first_line(message)
    return _NUMBER_RE.sub("#", _HEX_RE.sub("…", first_line))


def _logs_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from django_db_logger.models import StatusLog

    logs = StatusLog.objects.filter(_in_window("create_datetime", options), level__gte=options.min_log_level)

    by_level = {
        logging.getLevelName(row["key"]): row["count"]
        for row in _count_by(logs, "level")
    }

    # Group in the database first (exact message), then merge messages that only differ by numbers or ids.
    rows = (
        logs.order_by()
        .values("logger_name", "level", "msg")
        .annotate(count=Count("pk"), last_seen=Max("create_datetime"))
        .order_by("-count")[:LOG_GROUPS_SCAN_LIMIT]
    )
    groups: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["logger_name"], row["level"], normalize_log_message(row["msg"]))
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "logger_name": row["logger_name"],
                "level": logging.getLevelName(row["level"]),
                "level_number": row["level"],
                "message": _first_line(row["msg"]),
                "count": row["count"],
                "last_seen": row["last_seen"],
                "variants": 1,
            }
        else:
            group["count"] += row["count"]
            group["variants"] += 1
            if row["last_seen"] > group["last_seen"]:
                group["last_seen"] = row["last_seen"]

    top_groups = sorted(groups.values(), key=lambda g: (-g["level_number"], -g["count"], g["message"]))

    oldest_log = StatusLog.objects.order_by("create_datetime").values_list("create_datetime", flat=True).first()

    return {
        "enabled": not bool(getattr(crawler_settings, "disable_sql_log", False)),
        "min_level": logging.getLevelName(options.min_log_level),
        "total": sum(by_level.values()),
        "by_level": by_level,
        "groups": top_groups[: options.top_n],
        "groups_total": len(groups),
        "groups_truncated": len(rows) >= LOG_GROUPS_SCAN_LIMIT,
        "oldest_log": oldest_log,
        # The window starts before the oldest log kept: older logs were probably pruned, the counts may be low.
        "window_partially_pruned": oldest_log is not None and oldest_log > options.since,
    }


def _health_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from workers.models import ProviderRateLimit, SchedulerState, WorkerProcess

    now = timezone.now()

    workers = list(WorkerProcess.objects.filter(status="running"))
    alive_workers = [w for w in workers if w.last_heartbeat >= now - WORKER_HEARTBEAT_STALE]
    stale_workers = [w for w in workers if w.last_heartbeat < now - WORKER_HEARTBEAT_STALE]

    schedulers: list[dict[str, Any]] = []
    for state in SchedulerState.schedulers():
        problem = ""
        if state.is_running:
            if state.last_heartbeat is None or state.last_heartbeat < now - SCHEDULER_HEARTBEAT_STALE:
                problem = "unresponsive"
            elif state.cycle_timer > 0 and state.last_run is not None:
                overdue_cycles = datetime.timedelta(seconds=state.cycle_timer * SCHEDULER_OVERDUE_CYCLES)
                if state.last_run < now - overdue_cycles - SCHEDULER_OVERDUE_GRACE:
                    problem = "overdue"
        schedulers.append(
            {
                "name": state.name,
                "display_name": state.display_name or state.name,
                "is_running": state.is_running,
                "last_run": state.last_run,
                "next_run": state.next_run,
                "last_heartbeat": state.last_heartbeat,
                "cycle_seconds": state.cycle_timer,
                "problem": problem,
            }
        )

    now_ts = time.time()
    cooldowns = [
        {"provider": limit.name, "seconds_left": round(limit.cooldown_until - now_ts)}
        for limit in ProviderRateLimit.objects.filter(cooldown_until__gt=now_ts)
    ]

    return {
        "workers_alive": len(alive_workers),
        "workers": [
            {"pid": w.pid, "hostname": w.hostname, "type": w.worker_type, "last_heartbeat": w.last_heartbeat}
            for w in alive_workers
        ],
        "stale_workers": [
            {"pid": w.pid, "hostname": w.hostname, "type": w.worker_type, "last_heartbeat": w.last_heartbeat}
            for w in stale_workers
        ],
        "schedulers_running": sum(1 for s in schedulers if s["is_running"] and not s["problem"]),
        "schedulers_stopped": sum(1 for s in schedulers if not s["is_running"]),
        "scheduler_problems": [s for s in schedulers if s["problem"]],
        "schedulers": schedulers,
        "rate_limit_cooldowns": cooldowns,
    }


def _moderation_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import GallerySubmitEntry

    submissions = GallerySubmitEntry.objects
    pending = submissions.filter(resolved_status=GallerySubmitEntry.RESOLVED_SUBMITTED)
    oldest_pending = pending.order_by("submit_date").values_list("submit_date", flat=True).first()
    resolved = submissions.filter(_in_window("resolved_date", options))
    status_labels = dict(GallerySubmitEntry.RESOLVED_STATUS_CHOICES)

    return {
        "submitted": submissions.filter(_in_window("submit_date", options)).count(),
        "resolved_by_status": [
            {"key": status_labels.get(row["key"], str(row["key"])), "count": row["count"]}
            for row in _count_by(resolved, "resolved_status")
        ],
        "pending_now": pending.count(),
        "oldest_pending": oldest_pending,
    }


def _integrity_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import ArchiveFileCheck, ArchiveFileCheckIssue

    issues = ArchiveFileCheckIssue.objects.filter(_in_window("created_at", options))
    kind_labels = dict(ArchiveFileCheckIssue.Kind.choices)
    last_check = ArchiveFileCheck.objects.order_by("-pk").first()

    return {
        "issues": issues.count(),
        "archives_with_issues": issues.values("archive").distinct().count(),
        "issues_by_kind": [
            {"key": kind_labels.get(row["key"], row["key"]), "count": row["count"]} for row in _count_by(issues, "kind")
        ],
        "last_check": (
            {
                "id": last_check.pk,
                "status": last_check.get_status_display(),
                "created_at": getattr(last_check, "created_at", None),
                "finished_at": getattr(last_check, "finished_at", None),
            }
            if last_check
            else None
        ),
    }


def _storage_paths(crawler_settings: Any) -> list[tuple[str, str]]:
    media_root = str(getattr(crawler_settings, "MEDIA_ROOT", "") or getattr(settings, "MEDIA_ROOT", "") or "")
    if not media_root:
        return []
    paths = [("Media root", media_root)]
    for label, attribute in (("Archive downloads", "archive_dl_folder"), ("Torrent downloads", "torrent_dl_folder")):
        folder = getattr(crawler_settings, attribute, "")
        if folder:
            paths.append((label, os.path.join(media_root, folder)))
    return paths


def _storage_section(options: ReportOptions, crawler_settings: Any) -> dict[str, Any]:
    from viewer.models import Archive, Gallery
    from workers.models import SchedulerState

    # Only one entry per filesystem: download folders usually live on the same disk as the media root.
    disks: list[dict[str, Any]] = []
    seen_devices: set[int] = set()
    for index, (label, path) in enumerate(_storage_paths(crawler_settings)):
        # A download folder that doesn't exist yet isn't a disk problem, only the media root is required.
        if index > 0 and not os.path.exists(path):
            continue
        try:
            device = os.stat(path).st_dev
            if device in seen_devices:
                continue
            usage = shutil.disk_usage(path)
        except OSError as e:
            disks.append({"label": label, "path": path, "error": str(e)})
            continue
        seen_devices.add(device)
        used_percent = round(usage.used / usage.total * 100, 1) if usage.total else 0.0
        disks.append(
            {
                "label": label,
                "path": path,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "used_percent": used_percent,
                "warning": used_percent >= DISK_USAGE_WARNING_PERCENT,
            }
        )

    archive_totals = Archive.objects.filter(binned=False).aggregate(count=Count("pk"), total_size=Sum("filesize"))
    pruner = SchedulerState.objects.filter(name="timed_database_pruner").values("last_run", "is_running").first()

    return {
        "disks": disks,
        "disk_warning_percent": DISK_USAGE_WARNING_PERCENT,
        "archives_total": archive_totals["count"],
        "archives_total_size": archive_totals["total_size"] or 0,
        "galleries_total": Gallery.objects.count(),
        "database_pruner_last_run": pruner["last_run"] if pruner else None,
        "database_pruner_running": bool(pruner and pruner["is_running"]),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Attention summary
# ---------------------------------------------------------------------------------------------------------------------


def _count_text(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _attention_items(sections: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """Short list of what an admin should look at, most severe first. Built only from the computed sections."""
    items: list[dict[str, str]] = []

    def add(severity: str, section: str, message: str) -> None:
        items.append({"severity": severity, "section": section, "message": message})

    for key, data in sections.items():
        if "error" in data and len(data) == 1:
            add(ATTENTION_ERROR, key, f"The '{key}' section could not be built: {data['error']}")

    logs = sections.get(SECTION_LOGS, {})
    if logs and "error" not in logs:
        by_level: Counter[str] = Counter(logs.get("by_level", {}))
        serious = by_level["ERROR"] + by_level["CRITICAL"]
        if serious:
            add(ATTENTION_ERROR, SECTION_LOGS, _count_text(serious, "error log entry", "error log entries"))
        if by_level["WARNING"]:
            warnings = _count_text(by_level["WARNING"], "warning log entry", "warning log entries")
            add(ATTENTION_WARNING, SECTION_LOGS, warnings)
        if not logs.get("enabled", True):
            add(ATTENTION_INFO, SECTION_LOGS, "Database logging is disabled (disable_sql_log), no logs to report")

    health = sections.get(SECTION_HEALTH, {})
    if health and "error" not in health:
        if health.get("workers_alive") == 0:
            add(ATTENTION_ERROR, SECTION_HEALTH, "No worker process is running")
        for scheduler in health.get("scheduler_problems", []):
            add(
                ATTENTION_ERROR if scheduler["problem"] == "unresponsive" else ATTENTION_WARNING,
                SECTION_HEALTH,
                f"Scheduler {scheduler['display_name']} is {scheduler['problem']}",
            )
        if health.get("stale_workers"):
            stale = _count_text(len(health["stale_workers"]), "worker process", "worker processes")
            add(ATTENTION_WARNING, SECTION_HEALTH, f"{stale} stopped responding")

    storage = sections.get(SECTION_STORAGE, {})
    for disk in storage.get("disks", []) if "error" not in storage else []:
        if disk.get("error"):
            add(ATTENTION_WARNING, SECTION_STORAGE, f"Can't read disk usage of {disk['label']}: {disk['error']}")
        elif disk.get("warning"):
            add(ATTENTION_ERROR, SECTION_STORAGE, f"{disk['label']} disk is {disk['used_percent']}% full")

    downloads = sections.get(SECTION_DOWNLOADS, {})
    if downloads and "error" not in downloads:
        if downloads.get("failed"):
            failed = _count_text(downloads["failed"], "download", "downloads")
            add(ATTENTION_WARNING, SECTION_DOWNLOADS, f"{failed} failed")
        if downloads.get("stuck_queued"):
            stuck = _count_text(downloads["stuck_queued"], "download", "downloads")
            hours = downloads["stuck_queued_threshold_hours"]
            add(ATTENTION_WARNING, SECTION_DOWNLOADS, f"{stuck} queued for more than {hours:g} hours")

    tasks = sections.get(SECTION_TASKS, {})
    if tasks and "error" not in tasks and tasks.get("failed"):
        add(ATTENTION_WARNING, SECTION_TASKS, f"{_count_text(tasks['failed'], 'task', 'tasks')} failed")

    integrity = sections.get(SECTION_INTEGRITY, {})
    if integrity and "error" not in integrity and integrity.get("issues"):
        issues = _count_text(integrity["issues"], "new archive file issue", "new archive file issues")
        add(ATTENTION_WARNING, SECTION_INTEGRITY, issues)

    moderation = sections.get(SECTION_MODERATION, {})
    if moderation and "error" not in moderation and moderation.get("pending_now"):
        pending = _count_text(moderation["pending_now"], "submission", "submissions")
        add(ATTENTION_INFO, SECTION_MODERATION, f"{pending} waiting for review")

    severity_order = {ATTENTION_ERROR: 0, ATTENTION_WARNING: 1, ATTENTION_INFO: 2}
    items.sort(key=lambda item: severity_order[item["severity"]])
    return items
