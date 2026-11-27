"""Aggregation and grouped summary logging for split web-crawler task batches."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
import logging
import threading
from typing import Any

from django.db import transaction
import django.utils.timezone as django_tz
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)

MIN_BATCH_PRIORITY = 1
MAX_BATCH_PRIORITY = 10


def _empty_provider_stats() -> dict[str, Any]:
    return {
        "db_total": 0,
        "db_existing": 0,
        "db_discarded": 0,
        "db_to_update": 0,
        "meta_evaluated": 0,
        "meta_accepted": 0,
        "meta_matched_wanted": 0,
        "meta_unmatched_wanted": 0,
        "meta_banned": 0,
        "meta_errors": 0,
        "wanted_filters": False,
        "wanted_only": False,
        "wanted_galleries_matched": 0,
        "wanted_filter_matches": 0,
        "to_download": 0,
        "dispatched_galleries": 0,
        "dispatched_tasks": 0,
    }


class CrawlBatchCollector:
    """Thread-safe per-task collector for crawler summary counters within a split batch."""

    def __init__(self, batch_id: str, task_pk: int | None = None) -> None:
        self.batch_id = batch_id
        self._task_pk = task_pk
        self._lock = threading.Lock()
        self._providers: dict[str, dict[str, Any]] = {}

    def _get_provider(self, provider: str) -> dict[str, Any]:
        key = provider or "unknown"
        if key not in self._providers:
            self._providers[key] = _empty_provider_stats()
        return self._providers[key]

    def _flush_live_stats(self) -> None:
        if self._task_pk is None:
            return
        try:
            record_task_batch_stats(self._task_pk, self)
        except Exception:
            logger.debug(
                "Could not persist live crawl batch stats for task #%s (batch %s)",
                self._task_pk,
                self.batch_id,
                exc_info=True,
            )

    def record_db_check(
        self,
        provider: str,
        total: int,
        existing: int,
        discarded: int,
        to_update: int = 0,
    ) -> None:
        if total <= 0:
            return
        with self._lock:
            stats = self._get_provider(provider)
            stats["db_total"] += total
            stats["db_existing"] += existing
            stats["db_discarded"] += discarded
            stats["db_to_update"] += to_update
        self._flush_live_stats()

    def record_metadata_check(
        self,
        provider: str,
        total_evaluated: int,
        accepted: int,
        matched_wanted: int = 0,
        unmatched_wanted: int = 0,
        banned: int = 0,
        errors: int = 0,
        wanted_filters: bool = False,
        wanted_only: bool = False,
    ) -> None:
        if total_evaluated <= 0:
            return
        with self._lock:
            stats = self._get_provider(provider)
            stats["meta_evaluated"] += total_evaluated
            stats["meta_accepted"] += accepted
            stats["meta_matched_wanted"] += matched_wanted
            stats["meta_unmatched_wanted"] += unmatched_wanted
            stats["meta_banned"] += banned
            stats["meta_errors"] += errors
            if wanted_filters:
                stats["wanted_filters"] = True
            if wanted_only:
                stats["wanted_only"] = True
        self._flush_live_stats()

    def record_download_dispatch(
        self,
        provider: str,
        gallery_count: int,
        wanted_galleries_matched: int = 0,
        wanted_filter_matches: int = 0,
        dispatched: bool = False,
    ) -> None:
        with self._lock:
            stats = self._get_provider(provider)
            stats["wanted_galleries_matched"] += wanted_galleries_matched
            stats["wanted_filter_matches"] += wanted_filter_matches
            stats["to_download"] += gallery_count
            if dispatched and gallery_count > 0:
                stats["dispatched_galleries"] += gallery_count
                stats["dispatched_tasks"] += 1
        self._flush_live_stats()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "providers": {
                    name: dict(values) for name, values in self._providers.items()
                }
            }


def _merge_provider_stats(target: dict[str, Any], source: dict[str, Any]) -> None:
    for int_key in (
        "db_total",
        "db_existing",
        "db_discarded",
        "db_to_update",
        "meta_evaluated",
        "meta_accepted",
        "meta_matched_wanted",
        "meta_unmatched_wanted",
        "meta_banned",
        "meta_errors",
        "wanted_galleries_matched",
        "wanted_filter_matches",
        "to_download",
        "dispatched_galleries",
        "dispatched_tasks",
    ):
        target[int_key] += int(source.get(int_key, 0) or 0)
    if source.get("wanted_filters"):
        target["wanted_filters"] = True
    if source.get("wanted_only"):
        target["wanted_only"] = True


def extract_crawl_batch_meta(options: Any) -> dict[str, Any] | None:
    """Return the ``crawl_batch`` metadata dict from a TaskQueue ``options`` payload, or None."""
    if not isinstance(options, dict):
        return None
    batch_meta = options.get("crawl_batch")
    if isinstance(batch_meta, dict) and batch_meta.get("id"):
        return batch_meta
    return None


def enrich_provider_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of provider stats enriched with derived counters (e.g. ``db_new``)."""
    enriched = _empty_provider_stats()
    _merge_provider_stats(enriched, stats)
    enriched["db_new"] = max(0, int(enriched["db_total"]) - int(enriched["db_existing"]))
    return enriched


def aggregate_tasks_provider_stats(tasks: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Aggregate ``crawl_batch_stats`` provider counters across an iterable of TaskQueue rows."""
    aggregated: dict[str, dict[str, Any]] = {}
    for task in tasks:
        opts = task.options if isinstance(getattr(task, "options", None), dict) else {}
        task_stats = opts.get("crawl_batch_stats")
        if not isinstance(task_stats, dict):
            continue
        providers_map = task_stats.get("providers")
        if not isinstance(providers_map, dict):
            continue
        for prov_name, prov_values in providers_map.items():
            if isinstance(prov_values, dict):
                if prov_name not in aggregated:
                    aggregated[prov_name] = _empty_provider_stats()
                _merge_provider_stats(aggregated[prov_name], prov_values)
    return {name: aggregated[name] for name in sorted(aggregated.keys())}


def summarize_provider_stats(providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute overall totals across all providers in a batch."""
    totals = _empty_provider_stats()
    for prov_values in providers.values():
        if isinstance(prov_values, dict):
            _merge_provider_stats(totals, prov_values)
    totals["db_new"] = max(0, int(totals["db_total"]) - int(totals["db_existing"]))
    return totals


def _emit_batch_summary_logs(
    batch_id: str,
    total_blocks: int,
    total_links: int,
    completed_blocks: int,
    failed_blocks: int,
    cancelled_blocks: int,
    aggregated_providers: dict[str, dict[str, Any]],
) -> None:
    status_parts = [f"{completed_blocks}/{total_blocks} blocks completed"]
    if failed_blocks:
        status_parts.append(f"{failed_blocks} failed")
    if cancelled_blocks:
        status_parts.append(f"{cancelled_blocks} cancelled")
    missing_blocks = max(0, total_blocks - completed_blocks - failed_blocks - cancelled_blocks)
    if missing_blocks:
        status_parts.append(f"{missing_blocks} removed")

    links_note = f", {total_links} links" if total_links else ""
    logger.info(
        "Crawl batch %s summary (%s%s):",
        batch_id,
        ", ".join(status_parts),
        links_note,
    )

    if not aggregated_providers:
        logger.info("Crawl batch %s: no galleries were processed.", batch_id)
        return

    for provider_name in sorted(aggregated_providers.keys()):
        s = aggregated_providers[provider_name]
        prefix = f"Crawl batch {batch_id} [{provider_name}]"

        db_total = s["db_total"]
        if db_total > 0:
            db_existing = s["db_existing"]
            db_new = db_total - db_existing
            if db_existing > 0:
                logger.info(
                    "%s database check for %d galleries: %d existing (%d discarded, %d to update), %d new.",
                    prefix,
                    db_total,
                    db_existing,
                    s["db_discarded"],
                    s["db_to_update"],
                    db_new,
                )
            else:
                logger.info(
                    "%s database check for %d galleries: 0 existing in database (all %d new).",
                    prefix,
                    db_total,
                    db_total,
                )

        meta_evaluated = s["meta_evaluated"]
        if meta_evaluated > 0:
            extra_banned = f", {s['meta_banned']} discarded (banned data)" if s["meta_banned"] else ""
            extra_errors = f", {s['meta_errors']} API/network errors" if s["meta_errors"] else ""
            if s["wanted_filters"]:
                if s["wanted_only"]:
                    logger.info(
                        "%s metadata check for %d galleries: %d matched wanted filters, %d skipped as non-matching (wanted-only mode)%s%s.",
                        prefix,
                        meta_evaluated,
                        s["meta_matched_wanted"],
                        s["meta_unmatched_wanted"],
                        extra_banned,
                        extra_errors,
                    )
                else:
                    logger.info(
                        "%s metadata check for %d galleries: %d to process (%d matched wanted filters)%s%s.",
                        prefix,
                        meta_evaluated,
                        s["meta_accepted"],
                        s["meta_matched_wanted"],
                        extra_banned,
                        extra_errors,
                    )
            else:
                logger.info(
                    "%s metadata check for %d galleries: %d to process%s%s.",
                    prefix,
                    meta_evaluated,
                    s["meta_accepted"],
                    extra_banned,
                    extra_errors,
                )

        if s["wanted_galleries_matched"] > 0:
            logger.info(
                "%s WantedGallery matching complete: %d galleries matched (%d total filter matches).",
                prefix,
                s["wanted_galleries_matched"],
                s["wanted_filter_matches"],
            )

        if s["to_download"] == 0:
            logger.info("%s no galleries need downloading.", prefix)
        elif s["dispatched_galleries"] > 0:
            logger.info(
                "%s %d galleries for downloaders (%d dispatched across %d gallery_creator task(s)).",
                prefix,
                s["to_download"],
                s["dispatched_galleries"],
                s["dispatched_tasks"],
            )
        else:
            logger.info("%s %d galleries for downloaders.", prefix, s["to_download"])


def record_task_batch_stats(task_pk: int, collector: CrawlBatchCollector) -> None:
    """Persist collected per-task crawl stats onto the TaskQueue row."""
    from workers.models import TaskQueue

    stats_dict = collector.to_dict()
    with transaction.atomic():
        task = TaskQueue.objects.select_for_update().filter(pk=task_pk).first()
        if not task:
            return
        options = dict(task.options) if isinstance(task.options, dict) else {}
        options["crawl_batch_stats"] = stats_dict
        task.options = options
        task.save(update_fields=["options"])


def finalize_crawl_batch_if_done(batch_id: str, created_at_hint: str | None = None) -> bool:
    """Emit a grouped summary log once all tasks in a split crawl batch have finished.

    Returns True if the grouped summary was emitted by this call, False otherwise.
    """
    from workers.client import get_task_type_aliases
    from workers.models import TaskQueue

    if not batch_id:
        return False

    summary_payload: tuple[str, int, int, int, int, int, dict[str, dict[str, Any]]] | None = None

    with transaction.atomic():
        qs = TaskQueue.objects.select_for_update().filter(
            task_type__in=get_task_type_aliases("web_crawler"),
            options__crawl_batch__id=batch_id,
        )
        if created_at_hint:
            parsed_dt = parse_datetime(created_at_hint)
            if parsed_dt is not None:
                qs = qs.filter(created_at__gte=parsed_dt - timedelta(minutes=1))

        tasks = list(qs.order_by("id"))
        if not tasks:
            return False

        active_statuses = {TaskQueue.Status.PENDING, TaskQueue.Status.PROCESSING}
        if any(t.status in active_statuses for t in tasks):
            return False

        if any(isinstance(t.options, dict) and t.options.get("crawl_batch_summary_logged") for t in tasks):
            return False

        completed_blocks = 0
        failed_blocks = 0
        cancelled_blocks = 0
        total_blocks = len(tasks)
        total_links = 0

        for task in tasks:
            opts = task.options if isinstance(task.options, dict) else {}
            batch_meta = opts.get("crawl_batch")
            if isinstance(batch_meta, dict):
                meta_total = int(batch_meta.get("total") or 0)
                if meta_total > total_blocks:
                    total_blocks = meta_total
                meta_links = int(batch_meta.get("total_links") or 0)
                if meta_links > total_links:
                    total_links = meta_links

            if task.status == TaskQueue.Status.COMPLETED:
                completed_blocks += 1
            elif task.status == TaskQueue.Status.FAILED:
                failed_blocks += 1
            elif task.status == TaskQueue.Status.CANCELLED:
                cancelled_blocks += 1

        aggregated_providers = aggregate_tasks_provider_stats(tasks)

        # Mark the last task in the batch so summary is emitted at most once.
        marker_task = tasks[-1]
        marker_options = dict(marker_task.options) if isinstance(marker_task.options, dict) else {}
        marker_options["crawl_batch_summary_logged"] = True
        marker_task.options = marker_options
        marker_task.save(update_fields=["options"])

        # Only emit a summary if at least one block ran (completed or failed) or produced stats.
        if completed_blocks > 0 or failed_blocks > 0 or aggregated_providers:
            summary_payload = (
                batch_id,
                total_blocks,
                total_links,
                completed_blocks,
                failed_blocks,
                cancelled_blocks,
                aggregated_providers,
            )

    if summary_payload is not None:
        _emit_batch_summary_logs(*summary_payload)
        return True
    return False


def cancel_crawl_batch(batch_id: str, include_processing: bool = True) -> dict[str, Any]:
    """Cancel remaining unfinished blocks in a split web-crawler batch.

    When ``include_processing`` is True (default), both ``PENDING`` and ``PROCESSING`` blocks are
    marked ``CANCELLED`` (running workers observe the status change and abort promptly).
    Triggers ``finalize_crawl_batch_if_done`` once no active blocks remain.
    """
    from workers.client import get_task_type_aliases
    from workers.models import TaskQueue

    cleaned_id = str(batch_id or "").strip()
    if not cleaned_id:
        return {"exists": False, "cancelled": 0, "cancelled_pending": 0, "cancelled_processing": 0}

    created_at_hint: str | None = None
    cancelled_pending = 0
    cancelled_processing = 0

    with transaction.atomic():
        qs = TaskQueue.objects.select_for_update().filter(
            task_type__in=get_task_type_aliases("web_crawler"),
            options__crawl_batch__id=cleaned_id,
        )
        tasks = list(qs.order_by("id"))
        if not tasks:
            return {"exists": False, "cancelled": 0, "cancelled_pending": 0, "cancelled_processing": 0}

        for task in tasks:
            meta = extract_crawl_batch_meta(task.options)
            if meta and meta.get("created_at"):
                created_at_hint = str(meta["created_at"])
                break

        target_statuses = [TaskQueue.Status.PENDING]
        if include_processing:
            target_statuses.append(TaskQueue.Status.PROCESSING)

        to_cancel_ids: list[int] = []
        for task in tasks:
            if task.status in target_statuses:
                to_cancel_ids.append(task.pk)
                if task.status == TaskQueue.Status.PENDING:
                    cancelled_pending += 1
                elif task.status == TaskQueue.Status.PROCESSING:
                    cancelled_processing += 1

        if to_cancel_ids:
            TaskQueue.objects.filter(pk__in=to_cancel_ids, status__in=target_statuses).update(
                status=TaskQueue.Status.CANCELLED,
                finished_at=django_tz.now(),
            )

    cancelled_total = cancelled_pending + cancelled_processing
    if cancelled_total > 0:
        finalize_crawl_batch_if_done(cleaned_id, created_at_hint)

    return {
        "exists": True,
        "cancelled": cancelled_total,
        "cancelled_pending": cancelled_pending,
        "cancelled_processing": cancelled_processing,
    }


def reprioritize_crawl_batch(batch_id: str, priority: int) -> dict[str, Any]:
    """Set queue ``priority`` (1 = highest .. 10 = lowest) on all pending blocks of a split batch."""
    from workers.client import get_task_type_aliases
    from workers.models import TaskQueue

    if isinstance(priority, bool) or not isinstance(priority, int) or not (MIN_BATCH_PRIORITY <= priority <= MAX_BATCH_PRIORITY):
        raise ValueError(f"Priority must be an integer between {MIN_BATCH_PRIORITY} and {MAX_BATCH_PRIORITY}.")

    cleaned_id = str(batch_id or "").strip()
    if not cleaned_id:
        return {"exists": False, "updated": 0, "priority": priority}

    with transaction.atomic():
        qs = TaskQueue.objects.filter(
            task_type__in=get_task_type_aliases("web_crawler"),
            options__crawl_batch__id=cleaned_id,
        )
        if not qs.exists():
            return {"exists": False, "updated": 0, "priority": priority}

        updated = qs.filter(status=TaskQueue.Status.PENDING).update(priority=priority)

    return {
        "exists": True,
        "updated": updated,
        "priority": priority,
    }
