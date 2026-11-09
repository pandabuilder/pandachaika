from django.conf import settings
from django.core.management.base import BaseCommand

from viewer.services.retention import prune_database

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Prune old TaskQueue, DownloadEvent, EventLog, ProcessedLinks, StatusLog, and historical records according to retention policy."

    def add_arguments(self, parser):
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="Simulate pruning and report how many rows would be deleted without modifying the database.",
        )
        parser.add_argument(
            "--models",
            "--entities",
            type=str,
            default="all",
            help="Comma-separated list of entities to prune (task_queue, download_event, event_log, processed_links, status_log, history, or all).",
        )
        parser.add_argument(
            "--task-queue-days",
            type=int,
            default=None,
            help="Override retention period in days for TaskQueue (0 to skip).",
        )
        parser.add_argument(
            "--download-event-days",
            type=int,
            default=None,
            help="Override retention period in days for DownloadEvent (0 to skip).",
        )
        parser.add_argument(
            "--event-log-days",
            type=int,
            default=None,
            help="Override retention period in days for EventLog (0 to skip).",
        )
        parser.add_argument(
            "--status-log-days",
            type=int,
            default=None,
            help="Override retention period in days for StatusLog (0 to skip).",
        )
        parser.add_argument(
            "--processed-links-days",
            type=int,
            default=None,
            help="Override retention period in days for ProcessedLinks (0 to skip).",
        )
        parser.add_argument(
            "--history-days",
            type=int,
            default=None,
            help="Override retention period in days for django-simple-history tables (0 to skip).",
        )
        parser.add_argument(
            "--chunk-size",
            type=int,
            default=None,
            help="Batch size for deletions to avoid lock contention.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        entities_arg = options["models"]
        chunk_size = options["chunk_size"]

        if dry_run:
            self.stdout.write(self.style.WARNING("Running in DRY-RUN mode. No rows will be deleted."))

        days_override: dict[str, int] = {}
        if options["task_queue_days"] is not None:
            days_override["task_queue"] = options["task_queue_days"]
            days_override["task_queue_days"] = options["task_queue_days"]
        if options["download_event_days"] is not None:
            days_override["download_event"] = options["download_event_days"]
            days_override["download_event_days"] = options["download_event_days"]
        if options["event_log_days"] is not None:
            days_override["event_log"] = options["event_log_days"]
            days_override["event_log_days"] = options["event_log_days"]
        if options["status_log_days"] is not None:
            days_override["status_log"] = options["status_log_days"]
            days_override["status_log_days"] = options["status_log_days"]
        if options["processed_links_days"] is not None:
            days_override["processed_links"] = options["processed_links_days"]
            days_override["processed_links_days"] = options["processed_links_days"]
        if options["history_days"] is not None:
            days_override["history_days"] = options["history_days"]
            days_override["history_archive"] = options["history_days"]
            days_override["history_gallery"] = options["history_days"]

        # Parse entities
        target_entities = [e.strip() for e in entities_arg.split(",") if e.strip()]
        if not target_entities or "all" in target_entities:
            target_entity = "all"
        elif len(target_entities) == 1:
            target_entity = target_entities[0]
        else:
            target_entity = "all"

        self.stdout.write(f"Initiating pruning (target: {target_entity}, dry_run: {dry_run})...")

        results = prune_database(
            settings_obj=crawler_settings,
            entity=target_entity,
            days_override=days_override or None,
            dry_run=dry_run,
            chunk_size=chunk_size,
        )

        self.stdout.write("\n" + "=" * 65)
        self.stdout.write("              Database Pruning Results")
        self.stdout.write("=" * 65)
        for key, entity_data in results.get("entities", {}).items():
            label = entity_data.get("label", key)
            pruned = entity_data.get("pruned", 0)
            status = entity_data.get("status", "")
            days = entity_data.get("days", 0)
            action_label = "Would delete" if dry_run else "Deleted"
            self.stdout.write(f"  * {label:32} (days: {days:3d}): {action_label} {pruned:6d} rows [{status}]")

        self.stdout.write("-" * 65)
        total_action = "Total rows would be deleted" if dry_run else "Total rows deleted"
        self.stdout.write(f"{total_action}: {results['total_deleted']}")
        self.stdout.write(f"Execution time: {results['duration_seconds']}s")
        self.stdout.write("=" * 65 + "\n")
