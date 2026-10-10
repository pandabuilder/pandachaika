# -*- coding: utf-8 -*-
from django.conf import settings
from django.core.management.base import BaseCommand

from workers.runner import WorkerRunner


class Command(BaseCommand):
    help = "Run the PandaGallery background worker process to consume tasks and execute schedulers."
    def add_arguments(self, parser):
        parser.add_argument(
            "-c",
            "--config-dir",
            type=str,
            default=None,
            help="Directory where settings.yaml is located.",
        )
        parser.add_argument(
            "--all-schedulers",
            "--start-schedulers",
            action="store_true",
            default=False,
            dest="all_schedulers",
            help="Start all periodic schedulers (timed downloader, auto updater, auto wanted, monitored links, download progress checker) on worker startup.",
        )
        parser.add_argument(
            "--schedulers",
            type=str,
            default=None,
            help="Comma-separated list of specific schedulers to start on startup (e.g. 'timed_downloader,auto_wanted').",
        )
        parser.add_argument(
            "--no-scheduler",
            action="store_true",
            default=False,
            help="Do not run periodic schedulers (timed downloader, auto updater, etc.) in this worker process.",
        )
        parser.add_argument(
            "--poll-interval",
            type=float,
            default=1.0,
            help="Interval in seconds to check for new tasks when idle.",
        )
        parser.add_argument(
            "--heartbeat-interval",
            type=float,
            default=10.0,
            help="Interval in seconds between worker heartbeats.",
        )

    def handle(self, *args, **options):
        config_dir = options.get("config_dir")
        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
        if not crawler_settings or config_dir:
            from core.base.setup import Settings
            crawler_settings = Settings(load_from_disk=True, default_dir=config_dir)

        enable_schedulers = not options["no_scheduler"]
        all_schedulers = options.get("all_schedulers", False)
        schedulers_str = options.get("schedulers")
        schedulers_to_start = [s.strip() for s in schedulers_str.split(",") if s.strip()] if schedulers_str else None
        poll_interval = options["poll_interval"]
        heartbeat_interval = options["heartbeat_interval"]

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting PandaGallery worker (schedulers: {'enabled' if enable_schedulers else 'disabled'}, "
                f"force_start: {'all' if all_schedulers else (schedulers_to_start or 'config_defaults')}, "
                f"poll: {poll_interval}s, heartbeat: {heartbeat_interval}s)..."
            )
        )

        runner = WorkerRunner(
            settings=crawler_settings,
            poll_interval=poll_interval,
            heartbeat_interval=heartbeat_interval,
            enable_schedulers=enable_schedulers,
            force_start_schedulers=all_schedulers,
            schedulers_to_start=schedulers_to_start,
        )
        runner.run()
