"""Aggregation and grouped summary logging for split web-crawler task batches."""

from __future__ import annotations

from datetime import timedelta
import logging
import threading
from typing import Any

from django.db import transaction
from django.utils.dateparse import parse_datetime

logger = logging.getLogger(__name__)


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

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        self._lock = threading.Lock()
        self._providers: dict[str, dict[str, Any]] = {}

    def _get_provider(self, provider: str) -> dict[str, Any]:
        key = provider or "unknown"
        if key not in self._providers:
            self._providers[key] = _empty_provider_stats()
        return self._providers[key]

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
        aggregated_providers: dict[str, dict[str, Any]] = {}

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

            task_stats = opts.get("crawl_batch_stats")
            if isinstance(task_stats, dict):
                providers_map = task_stats.get("providers")
                if isinstance(providers_map, dict):
                    for prov_name, prov_values in providers_map.items():
                        if isinstance(prov_values, dict):
                            if prov_name not in aggregated_providers:
                                aggregated_providers[prov_name] = _empty_provider_stats()
                            _merge_provider_stats(aggregated_providers[prov_name], prov_values)

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
