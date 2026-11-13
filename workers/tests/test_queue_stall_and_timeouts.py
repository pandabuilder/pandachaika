# -*- coding: utf-8 -*-
from datetime import timedelta

import django.utils.timezone as django_tz
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.setup import Settings
from workers import client
from workers.models import TaskQueue, WorkerProcess
from workers.runner import WorkerRunner

User = get_user_model()




class QueueStallAndTimeoutFixesTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="stalltestuser", password="password123")
        self.settings = Settings(load_from_disk=False)

    def test_claim_next_events_ignores_non_queued_in_progress_and_avoids_head_of_line_blocking(self):
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        self.settings.parallel_archive_downloaders = 1
        panda_settings = type("MockProvSettings", (), {"max_parallel_archive_downloads": 1})()
        chaika_settings = type("MockProvSettings", (), {"max_parallel_archive_downloads": 1})()
        self.settings.providers = {"panda": panda_settings, "chaika": chaika_settings}
        adq = ArchiveDownloadQueue(self.settings)

        # 1. Create an active torrent/hath DownloadEvent (download_url="") for panda
        DownloadEvent.objects.create(
            name="https://e-hentai.org/g/1/abc/",
            method="torrent",
            provider="panda",
            download_id="torrent_hash_123",
            download_url="",
            status=DownloadEvent.Status.IN_PROGRESS,
            completed=False,
        )

        # Queued panda archive download should NOT be blocked by the torrent IN_PROGRESS row
        panda_q1 = DownloadEvent.objects.create(
            name="panda_q1",
            method="archive",
            provider="panda",
            download_url="http://hath.network/archive/1?start=1",
            status=DownloadEvent.Status.QUEUED,
            priority=10,
        )
        claimed_first = adq.claim_next_events(1)
        self.assertEqual(claimed_first, [panda_q1.id])

        # 2. Now panda_q1 is IN_PROGRESS (with download_url != "").
        # Enqueue 10 more panda archive downloads (created first) followed by 5 chaika downloads.
        for i in range(10):
            DownloadEvent.objects.create(
                name=f"panda_queued_{i}",
                method="archive",
                provider="panda",
                download_url=f"http://hath.network/archive/extra_{i}?start=1",
                status=DownloadEvent.Status.QUEUED,
                priority=10,
            )
        chaika_events = [
            DownloadEvent.objects.create(
                name=f"chaika_queued_{i}",
                method="chaika",
                provider="chaika",
                download_url=f"https://panda.chaika.moe/archive/{i}/download/",
                status=DownloadEvent.Status.QUEUED,
                priority=10,
            )
            for i in range(5)
        ]

        # Even though max_to_claim=1 and there are 10 panda items ahead of chaika,
        # claim_next_events(1) must skip blocked provider 'panda' and claim the first 'chaika' item!
        claimed_second = adq.claim_next_events(1)
        self.assertEqual(claimed_second, [chaika_events[0].id])

    def test_register_start_and_reconcile_recover_same_pid_and_stopped_worker_orphans(self):
        import os
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        runner = WorkerRunner(self.settings, enable_schedulers=False)

        # Simulate a Docker restart where a previous run with the SAME PID left an event IN_PROGRESS
        ev_same_pid = DownloadEvent.objects.create(
            name="same_pid_orphan",
            method="archive",
            provider="panda",
            download_url="http://example.com/same_pid.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=runner.pid,
            attempts=2,
            progress=55.0,
        )
        runner.register_start()
        ev_same_pid.refresh_from_db()
        self.assertEqual(ev_same_pid.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(ev_same_pid.worker_pid)
        self.assertEqual(ev_same_pid.attempts, 1)

        # Simulate a stopped WorkerProcess whose event was left IN_PROGRESS
        WorkerProcess.objects.create(
            pid=444444,
            hostname="stopped-host",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(minutes=5),
        )
        ev_stopped = DownloadEvent.objects.create(
            name="stopped_worker_orphan",
            method="chaika",
            provider="chaika",
            download_url="http://example.com/stopped.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=444444,
            attempts=1,
        )
        WorkerProcess.cleanup_dead_workers(threshold_seconds=60)
        ev_stopped.refresh_from_db()
        self.assertEqual(ev_stopped.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(ev_stopped.worker_pid)
        self.assertEqual(ev_stopped.attempts, 0)

        # Simulate an orphan with worker_pid=os.getpid() and no live thread in ArchiveDownloadQueue
        adq = ArchiveDownloadQueue(self.settings)
        ev_local_orphan = DownloadEvent.objects.create(
            name="local_orphan",
            method="chaika",
            provider="chaika",
            download_url="http://example.com/local_orphan.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=os.getpid(),
            attempts=1,
        )
        reconciled = adq.reconcile_orphaned_events()
        self.assertEqual(reconciled, 1)
        ev_local_orphan.refresh_from_db()
        self.assertEqual(ev_local_orphan.status, DownloadEvent.Status.QUEUED)
        self.assertIsNone(ev_local_orphan.worker_pid)

    def test_force_run_does_not_abort_active_downloads(self):
        import threading
        from unittest.mock import patch
        from core.downloaders.postdownload import CurrentDownload
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        adq = ArchiveDownloadQueue(self.settings)
        ev = DownloadEvent.objects.create(
            name="active_dl",
            method="chaika",
            provider="chaika",
            download_url="http://example.com/active.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            worker_pid=12345,
        )
        dummy_thread = threading.Thread(target=lambda: None)
        adq._active_downloads[ev.id] = CurrentDownload(filename="active.zip", speed=1000.0, index=1, total=1)
        adq._active_threads[ev.id] = dummy_thread

        with (
            patch.object(adq, "is_running_locally", return_value=True),
            patch.object(adq, "process_queue") as mock_pq,
        ):
            adq.force_run(force_local=True)
            mock_pq.assert_called_once()

        ev.refresh_from_db()
        self.assertEqual(ev.status, DownloadEvent.Status.IN_PROGRESS)
        self.assertIn(ev.id, adq._active_downloads)

    def test_archive_download_stall_watchdog_aborts_stuck_download_and_allows_slow_download(self):
        import threading
        import time
        from unittest.mock import MagicMock
        from core.workers.archive_download_queue import ArchiveDownloadQueue
        from viewer.models import DownloadEvent

        self.settings.archive_download_stall_timeout = 10.0
        adq = ArchiveDownloadQueue(self.settings)

        ev_stuck = DownloadEvent.objects.create(
            name="stuck_dl",
            method="archive",
            provider="panda",
            download_id="/tmp/stuck_test.zip",
            download_url="http://example.com/stuck.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=1,
            max_attempts=3,
        )
        ev_slow = DownloadEvent.objects.create(
            name="slow_dl",
            method="chaika",
            provider="chaika",
            download_id="/tmp/slow_test.zip",
            download_url="http://example.com/slow.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            attempts=1,
            max_attempts=3,
        )

        stop_threads = threading.Event()
        t_stuck = threading.Thread(target=lambda: stop_threads.wait(5.0), daemon=True)
        t_slow = threading.Thread(target=lambda: stop_threads.wait(5.0), daemon=True)
        t_stuck.start()
        t_slow.start()

        try:
            cancel_stuck = threading.Event()
            cancel_slow = threading.Event()
            resp_stuck = MagicMock()
            resp_slow = MagicMock()

            now_mono = time.monotonic()
            with adq._lock:
                adq._active_threads[ev_stuck.id] = t_stuck
                adq._download_activity[ev_stuck.id] = now_mono - 25.0  # Stalled for 25s (> 10s)
                adq._download_cancel_events[ev_stuck.id] = cancel_stuck
                adq._download_responses[ev_stuck.id] = resp_stuck

                adq._active_threads[ev_slow.id] = t_slow
                adq._download_activity[ev_slow.id] = now_mono - 2.0  # Active 2s ago (< 10s)
                adq._download_cancel_events[ev_slow.id] = cancel_slow
                adq._download_responses[ev_slow.id] = resp_slow

            stalled = adq.check_stalled_downloads()
            self.assertEqual(stalled, [ev_stuck.id])
            self.assertTrue(cancel_stuck.is_set())
            resp_stuck.close.assert_called_once()
            self.assertNotIn(ev_stuck.id, adq._active_threads)

            # Slow download that is actively receiving chunks must remain untouched
            self.assertFalse(cancel_slow.is_set())
            resp_slow.close.assert_not_called()
            self.assertIn(ev_slow.id, adq._active_threads)

            ev_stuck.refresh_from_db()
            self.assertEqual(ev_stuck.status, DownloadEvent.Status.QUEUED)
            self.assertIn("timed out", ev_stuck.error_message)
            self.assertIsNone(ev_stuck.worker_pid)

            ev_slow.refresh_from_db()
            self.assertEqual(ev_slow.status, DownloadEvent.Status.IN_PROGRESS)
        finally:
            stop_threads.set()
            t_stuck.join(timeout=1.0)
            t_slow.join(timeout=1.0)

    def test_worker_runner_task_stall_timeout_marks_failed_and_unblocks_queue(self):
        import os
        import tempfile
        import time
        from viewer.models import DownloadEvent

        self.settings.worker_task_stall_timeout = 10.0
        runner = WorkerRunner(self.settings, enable_schedulers=False)

        task_stuck = client.enqueue(args=["https://example.com/stuck"], task_type="web_crawler", priority=1)
        task_next = client.enqueue(args=["https://example.com/next"], task_type="web_crawler", priority=5)

        claimed = runner.claim_next_job()
        self.assertEqual(claimed.pk, task_stuck.pk)
        with runner._jobs_lock:
            runner.active_jobs[claimed.pk] = claimed
            runner.active_job_activity[claimed.pk] = time.monotonic() - 30.0  # Inactive for 30s (> 10s)

        # Case 1: If an inline download is actively writing to disk, check_stuck_jobs refreshes activity
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"active bytes")
            tmp_path = tmp.name

        try:
            inline_ev = DownloadEvent.objects.create(
                name="inline_active",
                method="archive",
                provider="panda",
                download_id=tmp_path,
                download_url="",
                status=DownloadEvent.Status.IN_PROGRESS,
                completed=False,
            )
            timed_out_while_downloading = runner.check_stuck_jobs()
            self.assertEqual(timed_out_while_downloading, [])
            claimed.refresh_from_db()
            self.assertEqual(claimed.status, TaskQueue.Status.PROCESSING)

            # Case 2: Once no inline download is active and task is stalled > 10s, it times out
            inline_ev.finish_download()
            inline_ev.save()
            with runner._jobs_lock:
                runner.active_job_activity[claimed.pk] = time.monotonic() - 30.0

            timed_out = runner.check_stuck_jobs()
            self.assertEqual(timed_out, [claimed.pk])
            claimed.refresh_from_db()
            self.assertEqual(claimed.status, TaskQueue.Status.FAILED)
            self.assertIn("FAILED (timeout)", claimed.error_message)

            # Next web_crawler task is now unblocked and can be claimed
            next_claimed = runner.claim_next_job()
            self.assertIsNotNone(next_claimed)
            self.assertEqual(next_claimed.pk, task_next.pk)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)









