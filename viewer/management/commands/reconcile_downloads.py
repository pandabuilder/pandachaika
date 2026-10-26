from django.core.management.base import BaseCommand
from django.conf import settings

from viewer.utils.downloads import reconcile_downloads

crawler_settings = settings.CRAWLER_SETTINGS


class Command(BaseCommand):
    help = "Reconcile out-of-sync Archives and DownloadEvents, rescuing files and recycling dead downloads."

    def add_arguments(self, parser):
        parser.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="Show what would be reconciled without modifying the database.",
        )
        parser.add_argument(
            "--no-requeue",
            action="store_true",
            help="Do not requeue failed downloads via fallback providers.",
        )
        parser.add_argument(
            "--no-disk-check",
            action="store_true",
            help="Skip checking local disk and torrent download staging folders.",
        )
        parser.add_argument(
            "--no-recycle-orphaned",
            action="store_true",
            help="Do not recycle orphaned archives that lack download events.",
        )
        parser.add_argument(
            "--no-recycle-failed",
            action="store_true",
            help="Do not recycle archives whose download events are marked failed/cancelled.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        if dry_run:
            self.stdout.write(self.style.WARNING("Running in DRY-RUN mode. No changes will be applied."))

        stats = reconcile_downloads(
            dry_run=dry_run,
            requeue_failed=not options["no_requeue"],
            recycle_orphaned=not options["no_recycle_orphaned"],
            recycle_failed=not options["no_recycle_failed"],
            check_disk=not options["no_disk_check"],
            settings=crawler_settings,
        )

        self.stdout.write("\n" + "=" * 45)
        self.stdout.write("      Reconciliation Results")
        self.stdout.write("=" * 45)
        self.stdout.write(f"Total checked:            {stats['total_checked']}")
        self.stdout.write(f"Rescued from disk:        {stats['rescued_from_disk']}")
        self.stdout.write(f"Rescued from staging:     {stats['rescued_from_staging']}")
        self.stdout.write(f"Recycled (failed):        {stats['recycled_failed']}")
        self.stdout.write(f"Recycled (false-complete): {stats['recycled_false_completed']}")
        self.stdout.write(f"Recycled (orphaned):      {stats['recycled_orphaned']}")
        self.stdout.write(f"Requeued via fallback:    {stats['requeued']}")
        self.stdout.write(f"Kept in-progress:         {stats['kept_in_progress']}")
        self.stdout.write("=" * 45)

        total_rescued = stats["rescued_from_disk"] + stats["rescued_from_staging"]
        total_recycled = stats["recycled_failed"] + stats["recycled_false_completed"] + stats["recycled_orphaned"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Reconciliation completed successfully: {total_rescued} rescued, {total_recycled} recycled."
            )
        )
