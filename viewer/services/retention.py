import datetime
import logging
import time
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Q, QuerySet
from django.utils import timezone

logger = logging.getLogger(__name__)


def get_active_settings(settings_obj: Any = None) -> Any:
    """Retrieve active crawler settings instance."""
    if settings_obj is not None:
        return settings_obj
    return getattr(settings, "CRAWLER_SETTINGS", None)


def get_database_info() -> dict[str, Any]:
    """Retrieve metadata and storage statistics about the current database."""
    db_dict = connection.settings_dict or {}
    vendor = connection.vendor
    name = db_dict.get("NAME", "")
    host = db_dict.get("HOST", "")
    port = db_dict.get("PORT", "")
    engine = db_dict.get("ENGINE", "")

    db_version = "Unknown"
    db_size = "N/A"
    table_sizes: dict[str, str] = {}

    try:
        with connection.cursor() as cursor:
            if vendor == "postgresql":
                try:
                    cursor.execute("SELECT version();")
                    row = cursor.fetchone()
                    if row:
                        db_version = str(row[0]).split(",")[0]
                except Exception:
                    pass

                try:
                    cursor.execute("SELECT pg_size_pretty(pg_database_size(current_database()));")
                    row = cursor.fetchone()
                    if row:
                        db_size = str(row[0])
                except Exception:
                    pass

                try:
                    cursor.execute(
                        """
                        SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid))
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public' AND c.relkind = 'r'
                        ORDER BY pg_total_relation_size(c.oid) DESC;
                        """
                    )
                    table_sizes = {r[0]: r[1] for r in cursor.fetchall()}
                except Exception:
                    pass
            elif vendor == "sqlite":
                try:
                    cursor.execute("SELECT sqlite_version();")
                    row = cursor.fetchone()
                    if row:
                        db_version = f"SQLite {row[0]}"
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Could not query database metadata: %s", e)

    pooling = bool(db_dict.get("OPTIONS", {}).get("pool") if isinstance(db_dict.get("OPTIONS"), dict) else False)
    conn_max_age = db_dict.get("CONN_MAX_AGE")

    return {
        "vendor": vendor,
        "name": name,
        "host": host or "localhost",
        "port": port or ("5432" if vendor == "postgresql" else ""),
        "engine": engine,
        "version": db_version,
        "db_size": db_size,
        "table_sizes": table_sizes,
        "pooling": pooling,
        "conn_max_age": conn_max_age,
    }


def get_entity_definitions() -> list[dict[str, Any]]:
    """Return entity definitions specifying model, date fields, and query builders."""
    from django.contrib.sessions.models import Session
    from django_db_logger.models import StatusLog
    from viewer.models import DownloadEvent, EventLog, MonitoredLinkRun, ProcessedLinks
    from viewer.models.change_log import ArchiveChange, GalleryChange
    from workers.models import TaskQueue

    defs: list[dict[str, Any]] = [
        {
            "key": "task_queue",
            "label": "Task Queue",
            "model_name": "TaskQueue",
            "app_label": "workers",
            "model": TaskQueue,
            "description": "Background worker task history (crawlers, hashing, maintenance)",
            "date_field": "finished_at",
            "fallback_date_field": "created_at",
            "config_field": "task_queue_days",
            "policy": "Terminal tasks (completed, failed, cancelled)",
            "build_prunable_qs": lambda cutoff: TaskQueue.objects.filter(
                status__in=[
                    TaskQueue.Status.COMPLETED,
                    TaskQueue.Status.FAILED,
                    TaskQueue.Status.CANCELLED,
                ]
            ).filter(
                Q(finished_at__lt=cutoff)
                | (Q(finished_at__isnull=True) & Q(created_at__lt=cutoff))
            ),
            "get_active_count": lambda: TaskQueue.objects.filter(
                status__in=[TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING]
            ).count(),
        },
        {
            "key": "download_event",
            "label": "Download Events",
            "model_name": "DownloadEvent",
            "app_label": "viewer",
            "model": DownloadEvent,
            "description": "Archive and torrent download progress and status logs",
            "date_field": "completed_date",
            "fallback_date_field": "create_date",
            "config_field": "download_event_days",
            "policy": "Finished or failed downloads",
            "build_prunable_qs": lambda cutoff: DownloadEvent.objects.filter(
                Q(completed=True)
                | Q(failed=True)
                | Q(
                    status__in=[
                        DownloadEvent.Status.COMPLETED,
                        DownloadEvent.Status.FAILED,
                        DownloadEvent.Status.CANCELLED,
                    ]
                )
            ).filter(
                Q(completed_date__lt=cutoff)
                | (Q(completed_date__isnull=True) & Q(create_date__lt=cutoff))
            ),
            "get_active_count": lambda: DownloadEvent.objects.in_progress().count(),
        },
        {
            "key": "event_log",
            "label": "Event Logs",
            "model_name": "EventLog",
            "app_label": "viewer",
            "model": EventLog,
            "description": "User and system operational activity logs",
            "date_field": "create_date",
            "fallback_date_field": None,
            "config_field": "event_log_days",
            "policy": "All event logs older than retention period",
            "build_prunable_qs": lambda cutoff: EventLog.objects.filter(create_date__lt=cutoff),
            "get_active_count": None,
        },
        {
            "key": "processed_links",
            "label": "Processed Links",
            "model_name": "ProcessedLinks",
            "app_label": "viewer",
            "model": ProcessedLinks,
            "description": "Visited crawler URL history preventing duplicate downloads",
            "date_field": "create_date",
            "fallback_date_field": "link_date",
            "config_field": "processed_links_days",
            "policy": "Processed link entries older than retention period",
            "build_prunable_qs": lambda cutoff: ProcessedLinks.objects.filter(create_date__lt=cutoff).exclude(
                status=ProcessedLinks.Status.PENDING
            ),
            "get_active_count": None,
        },
        {
            "key": "monitored_link_runs",
            "label": "Monitored Link Runs",
            "model_name": "MonitoredLinkRun",
            "app_label": "viewer",
            "model": MonitoredLinkRun,
            "description": "Per run stats of the monitored links (links found, overlap, processing results)",
            "date_field": "started",
            "fallback_date_field": None,
            # Same window as the processed links: the overlap between runs is measured against them.
            "config_field": "processed_links_days",
            "policy": "Runs older than the processed links retention period",
            "build_prunable_qs": lambda cutoff: MonitoredLinkRun.objects.filter(started__lt=cutoff),
            "get_active_count": None,
        },
        {
            "key": "status_log",
            "label": "Server Status Logs",
            "model_name": "StatusLog",
            "app_label": "django_db_logger",
            "model": StatusLog,
            "description": "Application logging records stored in the database",
            "date_field": "create_datetime",
            "fallback_date_field": None,
            "config_field": "status_log_days",
            "policy": "All server status logs older than retention period",
            "build_prunable_qs": lambda cutoff: StatusLog.objects.filter(create_datetime__lt=cutoff),
            "get_active_count": None,
        },
        {
            "key": "sessions",
            "label": "Expired Sessions",
            "model_name": "Session",
            "app_label": "sessions",
            "model": Session,
            "description": "Expired Django HTTP sessions stored in django_session",
            "date_field": "expire_date",
            "fallback_date_field": None,
            "config_field": "session_days",
            "policy": "Expired sessions past expiry grace period",
            "build_prunable_qs": lambda cutoff: Session.objects.filter(expire_date__lt=cutoff),
            "get_active_count": lambda: Session.objects.filter(expire_date__gte=timezone.now()).count(),
        },
    ]

    # Change log rows (GalleryChange / ArchiveChange). Each row is a diff and the current state lives in the
    # object itself, so old rows can be pruned without losing anything but the oldest changes.
    change_log_models = [
        ("history_archive", "Archive Change Log", ArchiveChange, "Archive field changes"),
        ("history_gallery", "Gallery Change Log", GalleryChange, "Gallery field and tag changes"),
    ]

    for key, label, model, desc in change_log_models:
        defs.append(
            {
                "key": key,
                "label": label,
                "model_name": model.__name__,
                "app_label": model._meta.app_label,
                "model": model,
                "description": desc,
                "date_field": "changed_at",
                "fallback_date_field": None,
                "config_field": "history_days",
                "policy": "Recorded changes older than retention period",
                "build_prunable_qs": (lambda m=model: lambda cutoff: m.objects.filter(changed_at__lt=cutoff))(),
                "get_active_count": None,
            }
        )

    return defs


def get_retention_counts(settings_obj: Any = None) -> list[dict[str, Any]]:
    """Calculate total row count, prunable count, and timestamps for each entity."""
    current_settings = get_active_settings(settings_obj)
    retention_cfg = getattr(current_settings, "retention", None)
    db_info = get_database_info()
    table_sizes = db_info.get("table_sizes", {})

    now = timezone.now()
    results: list[dict[str, Any]] = []

    for entity_def in get_entity_definitions():
        model = entity_def["model"]
        config_field = entity_def["config_field"]
        days = getattr(retention_cfg, config_field, 0) if retention_cfg else 0

        table_name = model._meta.db_table
        table_size = table_sizes.get(table_name, "N/A")

        total_count = model.objects.count()

        prunable_count = 0
        cutoff = None
        if days > 0:
            cutoff = now - datetime.timedelta(days=days)
            prunable_count = entity_def["build_prunable_qs"](cutoff).count()

        # Oldest / newest timestamps
        date_field = entity_def["date_field"]
        fallback_field = entity_def["fallback_date_field"]

        oldest_ts = None
        newest_ts = None
        try:
            oldest_obj = model.objects.order_by(date_field).values_list(date_field, flat=True).first()
            if oldest_obj is None and fallback_field:
                oldest_obj = model.objects.order_by(fallback_field).values_list(fallback_field, flat=True).first()
            oldest_ts = oldest_obj

            newest_obj = model.objects.order_by(f"-{date_field}").values_list(date_field, flat=True).first()
            if newest_obj is None and fallback_field:
                newest_obj = model.objects.order_by(f"-{fallback_field}").values_list(fallback_field, flat=True).first()
            newest_ts = newest_obj
        except Exception:
            pass

        active_count = None
        if entity_def["get_active_count"]:
            try:
                active_count = entity_def["get_active_count"]()
            except Exception:
                active_count = None

        results.append(
            {
                "key": entity_def["key"],
                "label": entity_def["label"],
                "model_name": entity_def["model_name"],
                "app_label": entity_def["app_label"],
                "description": entity_def["description"],
                "table_name": table_name,
                "table_size": table_size,
                "retention_days": days,
                "policy": entity_def["policy"],
                "cutoff": cutoff,
                "total_count": total_count,
                "prunable_count": prunable_count,
                "active_count": active_count,
                "oldest_timestamp": oldest_ts,
                "newest_timestamp": newest_ts,
            }
        )

    return results


ALL_ENTITIES = "all"
HISTORY_ENTITIES = ("history", "historical")


def _entity_names(entity: "str | list[str] | tuple[str, ...]") -> list[str]:
    names = [entity] if isinstance(entity, str) else list(entity)
    return [part.strip() for name in names for part in str(name).split(",") if part.strip()]


def resolve_prune_targets(entity: "str | list[str] | tuple[str, ...]") -> list[dict[str, Any]]:
    """Entity definitions for ``entity``: "all", "history" (both change logs), an entity key or model name, or a
    list (or comma-separated string) of those. Raises ValueError for an unknown name, so a typo never widens or
    silently skips a prune."""
    entity_defs = get_entity_definitions()
    names = _entity_names(entity)
    if not names:
        raise ValueError("No entity to prune given.")
    if ALL_ENTITIES in names:
        return entity_defs

    targets: list[dict[str, Any]] = []
    for name in names:
        if name in HISTORY_ENTITIES:
            matched = [ed for ed in entity_defs if ed["key"].startswith("history_")]
        else:
            matched = [ed for ed in entity_defs if ed["key"] == name or ed["model_name"].lower() == name.lower()]
        if not matched:
            valid = ", ".join([ALL_ENTITIES, HISTORY_ENTITIES[0], *(ed["key"] for ed in entity_defs)])
            raise ValueError(f"Unknown entity to prune: '{name}'. Valid entities: {valid}.")
        for ed in matched:
            if ed not in targets:
                targets.append(ed)
    return targets


def validate_prune_request(
    entity: "str | list[str] | tuple[str, ...]", days_override: int | None
) -> list[dict[str, Any]]:
    """Entity definitions targeted by a prune request, or ValueError when it would delete more than intended:
    unknown entities, or a single days value for every entity at once (each entity keeps its own retention when
    pruning all)."""
    targets = resolve_prune_targets(entity)
    if isinstance(days_override, int) and days_override > 0 and ALL_ENTITIES in _entity_names(entity):
        raise ValueError(
            "A days override can't be applied to every entity at once. Pick the entity to prune, "
            "or prune all of them with their configured retention."
        )
    return targets


def prune_database(
    settings_obj: Any = None,
    entity: "str | list[str] | tuple[str, ...]" = ALL_ENTITIES,
    days_override: int | dict[str, int] | None = None,
    dry_run: bool = False,
    chunk_size: int | None = None,
    user: Any = None,
) -> dict[str, Any]:
    """Execute pruning on the given entities according to retention settings.

    ``days_override`` is either a single number of days for the targeted entities (refused when targeting all of
    them), or a dict of days by entity key or config field; entities missing from the dict use their configured
    retention.
    """
    start_time = time.time()
    current_settings = get_active_settings(settings_obj)
    retention_cfg = getattr(current_settings, "retention", None)

    if chunk_size is None or chunk_size <= 0:
        chunk_size = getattr(retention_cfg, "chunk_size", 5000) if retention_cfg else 5000

    target_defs = validate_prune_request(entity, days_override if isinstance(days_override, int) else None)
    now = timezone.now()

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "entity": entity if isinstance(entity, str) else ",".join(entity),
        "total_deleted": 0,
        "entities": {},
    }

    for ed in target_defs:
        key = ed["key"]
        config_field = ed["config_field"]

        # Determine retention days: an override for this entity, else its configured retention.
        configured_days: int = getattr(retention_cfg, config_field, 0) if retention_cfg else 0
        days: int = configured_days
        if isinstance(days_override, dict):
            override = days_override.get(key, days_override.get(config_field))
            if override is not None:
                days = override
        elif isinstance(days_override, int) and days_override > 0:
            days = days_override

        if days <= 0:
            summary["entities"][key] = {
                "label": ed["label"],
                "days": 0,
                "pruned": 0,
                "status": "skipped (retention disabled or 0 days)",
            }
            continue

        cutoff = now - datetime.timedelta(days=days)
        model: Any = ed["model"]
        qs: QuerySet = ed["build_prunable_qs"](cutoff)

        deleted_count = 0
        if dry_run:
            prunable_count = qs.count()
            deleted_count = prunable_count
            status_text = f"dry-run (would delete {prunable_count})"
        else:
            # Batch chunked deletion
            while True:
                pks = list(qs.values_list("pk", flat=True)[:chunk_size])
                if not pks:
                    break
                deleted, _ = model.objects.filter(pk__in=pks).delete()
                deleted_count += deleted

            status_text = f"deleted {deleted_count} rows"

        summary["total_deleted"] += deleted_count
        summary["entities"][key] = {
            "label": ed["label"],
            "days": days,
            "cutoff": cutoff.isoformat(),
            "pruned": deleted_count,
            "status": status_text,
        }

    duration = round(time.time() - start_time, 2)
    summary["duration_seconds"] = duration

    logger.info(
        "Database pruning (%s, dry_run=%s) finished in %.2fs: %d rows affected across %d entities",
        summary["entity"],
        dry_run,
        duration,
        summary["total_deleted"],
        len(summary["entities"]),
    )

    if not dry_run and summary["total_deleted"] > 0:
        try:
            from viewer.utils.actions import event_log

            event_log(
                user,
                "DATABASE_PRUNE",
                data=f"entity={summary['entity']}, deleted={summary['total_deleted']}, duration={duration}s",
                result="success",
            )
        except Exception:
            pass

    return summary
