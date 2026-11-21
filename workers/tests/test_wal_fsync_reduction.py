from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import django.utils.timezone as django_tz
import yaml
from django.conf import settings as django_settings
from django.test import SimpleTestCase

from core.base.config_models import GeneralSettings, PandaGalleryConfig
from core.base.setup import Settings
from core.workers.archive_download_queue import ArchiveDownloadQueue
from core.workers.download_progress import DownloadProgressChecker
from core.workers.schedulers import BaseScheduler
from workers.models import SchedulerState
from workers.runner import WorkerRunner
from workers.serialization import EXCLUDED_SETTINGS_ATTRIBUTES


class _RaisingCurrentLastRunScheduler:
    """Scheduler stub whose current_last_run property fails if accessed (would be a DB SELECT)."""

    def __init__(
        self,
        thread_name: str,
        *,
        running: bool = True,
        last_run=None,
        timer: float = 10.0,
        downloads=None,
    ) -> None:
        self.thread_name = thread_name
        self._running = running
        self.last_run = last_run
        self.timer = timer
        self._downloads = downloads

    @property
    def current_last_run(self):
        raise AssertionError("sync_schedulers_state should read s.last_run locally, not s.current_last_run")

    def is_running_locally(self) -> bool:
        return self._running

    def current_download(self):
        return self._downloads or []


class WorkerWalFsyncReductionTests(SimpleTestCase):
    def _make_runner(self, *, enable_schedulers: bool = True) -> WorkerRunner:
        cfg = Settings(load_from_disk=False)
        with patch("pandabackup.settings.configure_database"):
            return WorkerRunner(cfg, enable_schedulers=enable_schedulers)

    def test_sync_schedulers_state_batches_updates_skips_unchanged_stopped_and_avoids_current_last_run(self) -> None:
        runner = self._make_runner(enable_schedulers=True)
        now = django_tz.now()
        old_run = now - timedelta(minutes=5)

        running_sched = _RaisingCurrentLastRunScheduler(
            "download_progress_checker",
            running=True,
            last_run=now - timedelta(seconds=10),
            timer=10.0,
            downloads=[SimpleNamespace(filename="a.zip", speed=500.0, index=1, total=1, downloaded=50, filesize=100)],
        )
        stopped_unchanged = _RaisingCurrentLastRunScheduler(
            "auto_wanted",
            running=False,
            last_run=old_run,
            timer=3600.0,
        )
        new_sched = _RaisingCurrentLastRunScheduler(
            "archive_download_queue",
            running=True,
            last_run=None,
            timer=2.0,
        )

        existing_running = SchedulerState(
            name="download_progress_checker",
            display_name="download_progress_checker",
            is_running=True,
            last_run=old_run,
            next_run=old_run + timedelta(seconds=10),
            cycle_timer=10.0,
            worker_pid=runner.pid,
            last_heartbeat=old_run,
            extra_data={},
        )
        existing_stopped = SchedulerState(
            name="auto_wanted",
            display_name="auto_wanted",
            is_running=False,
            last_run=old_run,
            next_run=None,
            cycle_timer=3600.0,
            worker_pid=runner.pid,
            last_heartbeat=old_run,
            extra_data={},
        )

        atomic_calls = 0

        def fake_atomic(*args, **kwargs):
            nonlocal atomic_calls
            atomic_calls += 1
            return nullcontext()

        with (
            patch.object(
                runner.settings.workers,
                "get_active_initialized_workers",
                return_value=[running_sched, stopped_unchanged, new_sched],
            ),
            patch("workers.runner.transaction.atomic", side_effect=fake_atomic),
            patch.object(
                SchedulerState.objects,
                "in_bulk",
                return_value={
                    "download_progress_checker": existing_running,
                    "auto_wanted": existing_stopped,
                },
            ) as mock_in_bulk,
            patch.object(SchedulerState.objects, "bulk_create") as mock_bulk_create,
            patch.object(SchedulerState.objects, "bulk_update") as mock_bulk_update,
            patch.object(SchedulerState.objects, "update_or_create") as mock_uoc,
        ):
            runner.sync_schedulers_state()

        self.assertEqual(atomic_calls, 1)
        mock_in_bulk.assert_called_once_with(
            ["download_progress_checker", "auto_wanted", "archive_download_queue"]
        )
        mock_uoc.assert_not_called()

        mock_bulk_create.assert_called_once()
        created_rows = mock_bulk_create.call_args.args[0]
        self.assertEqual([r.name for r in created_rows], ["archive_download_queue"])

        mock_bulk_update.assert_called_once()
        updated_rows = mock_bulk_update.call_args.args[0]
        # Stopped unchanged scheduler must be skipped; running scheduler must be updated
        self.assertEqual([r.name for r in updated_rows], ["download_progress_checker"])
        self.assertEqual(updated_rows[0].extra_data["current_download"][0]["filename"], "a.zip")

    def test_update_heartbeat_batches_worker_and_schedulers_in_one_atomic_block(self) -> None:
        runner = self._make_runner(enable_schedulers=True)
        atomic_calls = 0

        def fake_atomic(*args, **kwargs):
            nonlocal atomic_calls
            atomic_calls += 1
            return nullcontext()

        with (
            patch("workers.runner._safe_close_old_connections"),
            patch("workers.runner.transaction.atomic", side_effect=fake_atomic),
            patch("workers.runner.WorkerProcess.objects.filter") as mock_wp_filter,
            patch("workers.runner.WorkerProcess.objects.update_or_create") as mock_wp_uoc,
            patch("workers.runner.WorkerProcess.cleanup_dead_workers") as mock_cleanup,
            patch.object(runner, "sync_schedulers_state") as mock_sync_sched,
            patch.object(runner, "check_stuck_jobs") as mock_stuck,
            patch.object(runner, "sync_monitored_links") as mock_links,
        ):
            mock_wp_filter.return_value.update.return_value = 1
            runner.update_heartbeat(force=True)

        self.assertEqual(atomic_calls, 1)
        mock_wp_filter.return_value.update.assert_called_once()
        mock_wp_uoc.assert_not_called()
        mock_sync_sched.assert_called_once()
        mock_cleanup.assert_called_once()
        mock_stuck.assert_called_once()
        mock_links.assert_called_once()

    def test_check_stuck_jobs_and_sync_monitored_links_short_circuit_when_empty(self) -> None:
        runner = self._make_runner(enable_schedulers=True)
        runner.settings.workers.timed_link_monitors = []

        with (
            patch.object(runner, "prune_inactive_jobs", return_value=[]),
            patch("viewer.models.DownloadEvent.objects.filter") as mock_dl_filter,
            patch("viewer.models.MonitoredLink.objects.values_list") as mock_ml_values,
        ):
            self.assertEqual(runner.check_stuck_jobs(), [])
            runner.sync_monitored_links()

        mock_dl_filter.assert_not_called()
        mock_ml_values.assert_not_called()


class ArchiveDownloadQueueChurnTests(SimpleTestCase):
    def _make_settings(self, *, queue_enabled: bool = True, parallel: int = 1) -> Settings:
        cfg = Settings(load_from_disk=False)
        cfg.queue_archive_downloads = queue_enabled
        cfg.parallel_archive_downloaders = parallel
        return cfg

    def test_base_scheduler_update_last_run_persist_false_skips_db_write(self) -> None:
        class _DummySched(BaseScheduler):
            thread_name = "dummy_sched"

            def job(self) -> None:
                pass

        sched = _DummySched(Settings(load_from_disk=False), timer=1)
        now = django_tz.now()
        with patch.object(SchedulerState.objects, "filter") as mock_filter:
            sched.update_last_run(now, persist=False)
        self.assertEqual(sched.last_run, now)
        mock_filter.assert_not_called()

    def test_claim_next_events_short_circuits_before_transaction_when_queue_empty(self) -> None:
        adq = ArchiveDownloadQueue(self._make_settings())
        with (
            patch("core.workers.archive_download_queue.DownloadEvent.objects.filter") as mock_filter,
            patch("core.workers.archive_download_queue.transaction.atomic") as mock_atomic,
        ):
            mock_filter.return_value.exclude.return_value.exists.return_value = False
            claimed = adq.claim_next_events(2)

        self.assertEqual(claimed, [])
        mock_atomic.assert_not_called()

    def test_process_queue_throttles_reconcile_and_skips_stall_check_when_idle(self) -> None:
        adq = ArchiveDownloadQueue(self._make_settings())
        with (
            patch.object(adq, "check_stalled_downloads", return_value=[]) as mock_stall,
            patch.object(adq, "reconcile_orphaned_events", return_value=0) as mock_reconcile,
            patch.object(adq, "claim_next_events", return_value=[]) as mock_claim,
        ):
            # First tick reconciles once
            self.assertFalse(adq.process_queue())
            # Second immediate idle tick skips reconcile
            self.assertFalse(adq.process_queue())

        mock_stall.assert_not_called()
        mock_reconcile.assert_called_once()
        self.assertEqual(mock_claim.call_count, 2)

    def test_idle_backoff_and_wake_reset(self) -> None:
        adq = ArchiveDownloadQueue(self._make_settings(), timer=2.0)
        adq.last_run = django_tz.now()

        adq._idle_cycles = 1
        self.assertEqual(adq._idle_wait_seconds(), 4.0)
        adq._idle_cycles = 2
        self.assertEqual(adq._idle_wait_seconds(), 8.0)
        adq._idle_cycles = 5
        self.assertEqual(adq._idle_wait_seconds(), 30.0)

        # wake() resets backoff and wakes _wait_for_next_tick immediately
        adq.wake()
        self.assertEqual(adq._idle_cycles, 0)
        self.assertFalse(adq._wait_for_next_tick(30.0))

    def test_archive_download_queue_job_does_not_persist_scheduler_state_directly(self) -> None:
        adq = ArchiveDownloadQueue(self._make_settings(), timer=2.0)

        def stop_after_one_tick() -> bool:
            adq.stop.set()
            return False

        with (
            patch("core.workers.archive_download_queue._safe_close_old_connections"),
            patch.object(adq, "process_queue", side_effect=stop_after_one_tick),
            patch.object(adq, "update_last_run", wraps=adq.update_last_run) as mock_update,
            patch.object(SchedulerState.objects, "filter") as mock_state_filter,
        ):
            adq.job()

        mock_update.assert_called_once()
        self.assertIs(mock_update.call_args.kwargs.get("persist"), False)
        mock_state_filter.assert_not_called()

    def test_download_progress_checker_idle_tick_skips_direct_db_write(self) -> None:
        checker = DownloadProgressChecker(self._make_settings(), timer=10.0)

        def idle_then_stop() -> bool:
            checker.stop.set()
            return False

        with (
            patch("core.workers.download_progress.close_old_connections"),
            patch.object(checker, "check_downloads", side_effect=idle_then_stop),
            patch.object(checker, "update_last_run", wraps=checker.update_last_run) as mock_update,
            patch.object(SchedulerState.objects, "filter") as mock_state_filter,
        ):
            checker.job()

        mock_update.assert_called_once()
        self.assertIs(mock_update.call_args.kwargs.get("persist"), False)
        mock_state_filter.assert_not_called()

    def test_holder_skips_archive_download_queue_autostart_when_disabled(self) -> None:
        from core.workers.holder import WorkerContext

        cfg = self._make_settings(queue_enabled=False, parallel=0)
        cfg.archive_download_queue_startup = True
        cfg.download_progress_checker_startup = False
        cfg.auto_wanted.startup = False
        cfg.autoupdater.startup = False
        cfg.monitored_links.enable = False
        cfg.retention.enabled = False
        cfg.admin_report.enable = False

        ctx = WorkerContext()
        fake_state = SimpleNamespace(last_run=None, name="sched")
        with (
            patch("workers.models.SchedulerState.objects.get_or_create", return_value=(fake_state, True)),
            patch("core.workers.archive_download_queue.ArchiveDownloadQueue.start_running") as mock_start,
        ):
            ctx.start_workers(cfg, start_threads=True)

        mock_start.assert_not_called()


class DbLogLevelConfigTests(SimpleTestCase):
    def test_sql_log_level_defaults_to_info_and_is_excluded_from_task_options(self) -> None:
        self.assertEqual(GeneralSettings().sql_log_level, "INFO")
        self.assertEqual(Settings(load_from_disk=False).sql_log_level, "INFO")

        cfg = PandaGalleryConfig()
        cfg.update_from_dict({"general": {"sql_log_level": "WARNING"}})
        self.assertEqual(cfg.general.sql_log_level, "WARNING")

        default_yaml_path = Path(__file__).resolve().parents[2] / "default.yaml"
        parsed = yaml.safe_load(default_yaml_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["general"]["sql_log_level"], "INFO")

        self.assertIn("sql_log_level", EXCLUDED_SETTINGS_ATTRIBUTES)

        db_handler = django_settings.LOGGING["handlers"].get("db_log")
        if db_handler is not None:
            self.assertEqual(db_handler["level"], "INFO")
