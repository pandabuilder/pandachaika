import datetime
import logging
import time
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.models import Max, Q, QuerySet
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


def _prunable_history(model: Any, cutoff: datetime.datetime) -> QuerySet:
    """Old history rows, except the newest revision of every object (so unchanged objects keep their snapshot)."""
    newest_per_object = model.objects.values("id").annotate(newest=Max("history_id")).values("newest")
    return model.objects.filter(history_date__lt=cutoff).exclude(history_id__in=newest_per_object)


def get_entity_definitions() -> list[dict[str, Any]]:
    """Return entity definitions specifying model, date fields, and query builders."""
    from django_db_logger.models import StatusLog
    from viewer.models import DownloadEvent, EventLog, ProcessedLinks
    from viewer.models.archive import Archive
    from viewer.models.gallery import Gallery
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
    ]

    # Historical models from django-simple-history
    hist_models = [
        ("history_archive", "Historical Archive Snapshots", Archive.history.model, "Archive revision history"),
        ("history_gallery", "Historical Gallery Snapshots", Gallery.history.model, "Gallery revision history"),
    ]

    for key, label, model, desc in hist_models:
        defs.append(
            {
                "key": key,
                "label": label,
                "model_name": model.__name__,
                "app_label": model._meta.app_label,
                "model": model,
                "description": desc,
                "date_field": "history_date",
                "fallback_date_field": None,
                "config_field": "history_days",
                "policy": "Revision records older than retention period (the newest revision of each object is kept)",
                "build_prunable_qs": (lambda m=model: lambda cutoff: _prunable_history(m, cutoff))(),
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


def prune_database(
    settings_obj: Any = None,
    entity: str = "all",
    days_override: int | dict[str, int] | None = None,
    dry_run: bool = False,
    chunk_size: int | None = None,
    user: Any = None,
) -> dict[str, Any]:
    """Execute pruning on specified entity or all entities according to retention settings."""
    start_time = time.time()
    current_settings = get_active_settings(settings_obj)
    retention_cfg = getattr(current_settings, "retention", None)

    if chunk_size is None or chunk_size <= 0:
        chunk_size = getattr(retention_cfg, "chunk_size", 5000) if retention_cfg else 5000

    entity_defs = get_entity_definitions()
    now = timezone.now()

    # Determine which entities to target
    target_defs = []
    if entity == "all":
        target_defs = entity_defs
    elif entity in ("history", "historical"):
        target_defs = [ed for ed in entity_defs if ed["key"].startswith("history_")]
    else:
        target_defs = [ed for ed in entity_defs if ed["key"] == entity]
        if not target_defs:
            # Match by model_name or key prefix
            target_defs = [ed for ed in entity_defs if ed["model_name"].lower() == entity.lower()]

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "entity": entity,
        "total_deleted": 0,
        "entities": {},
    }

    for ed in target_defs:
        key = ed["key"]
        config_field = ed["config_field"]

        # Determine retention days
        days: int = 0
        if isinstance(days_override, dict):
            days = days_override.get(key, days_override.get(config_field, 0))
        elif isinstance(days_override, int) and days_override > 0:
            days = days_override
        elif retention_cfg:
            days = getattr(retention_cfg, config_field, 0)

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
        prunable_count = qs.count()

        deleted_count = 0
        if dry_run:
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
        entity,
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
                data=f"entity={entity}, deleted={summary['total_deleted']}, duration={duration}s",
                result="success",
            )
        except Exception:
            pass

    return summary
