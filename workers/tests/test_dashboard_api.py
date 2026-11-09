# -*- coding: utf-8 -*-
import json
import uuid
from datetime import timedelta

import django.utils.timezone as django_tz
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.parsers import BaseParser
from core.base.setup import Settings
from core.workers.webqueue import WebQueue
from viewer.models import Archive, EventLog, Gallery, GallerySubmitEntry, UserArchivePrefs
from workers import client
from workers.models import TaskQueue, WorkerProcess
from workers.runner import WorkerRunner
from workers.serialization import deserialize_settings_options, serialize_settings_options

User = get_user_model()


class WorkersDashboardAPITests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_superuser(
            username="dash_admin", email="dash_admin@example.com", password="password123"
        )
        self.regular_user = User.objects.create_user(
            username="dash_user", email="dash_user@example.com", password="password123"
        )
        from django.test import Client

        self.staff_client = Client()
        self.staff_client.force_login(self.staff_user)

        self.user_client = Client()
        self.user_client.force_login(self.regular_user)

        self.anon_client = Client()

    def test_workers_status_api_permissions(self):
        # Anonymous user should be redirected to login
        res_anon = self.anon_client.get("/api/workers/status/")
        self.assertEqual(res_anon.status_code, 302)

        # Non-staff user should get 403 Forbidden
        res_user = self.user_client.get("/api/workers/status/")
        self.assertEqual(res_user.status_code, 403)

        # Staff user should get 200 OK
        res_staff = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res_staff.status_code, 200)
        data = res_staff.json()
        self.assertIn("summary", data)
        self.assertIn("workers", data)
        self.assertIn("queues", data)
        self.assertIn("schedulers", data)
        self.assertIn("diagram", data)

    def test_workers_status_api_live_data(self):
        from workers.models import WorkerProcess, SchedulerState, TaskQueue

        # Create active worker
        worker = WorkerProcess.objects.create(
            pid=55555,
            hostname="test-node-1",
            worker_type="general",
            status="running",
            last_heartbeat=django_tz.now(),
        )

        # Create processing task assigned to this worker
        task_proc = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/live/1/"],
            status=TaskQueue.Status.PROCESSING,
            worker_pid=worker.pid,
            started_at=django_tz.now(),
        )

        # Create pending task
        TaskQueue.objects.create(
            task_type="fileinfo_worker",
            args=[123],
            status=TaskQueue.Status.PENDING,
        )

        # Create scheduler
        SchedulerState.objects.update_or_create(
            name="timed_downloader",
            defaults={
                "cycle_timer": 15.0,
                "is_running": True,
                "worker_pid": worker.pid,
                "last_heartbeat": django_tz.now(),
            },
        )

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Summary check
        self.assertEqual(data["summary"]["active_workers_count"], 1)
        self.assertEqual(data["summary"]["processing_tasks_count"], 1)
        self.assertEqual(data["summary"]["pending_tasks_count"], 1)

        # Worker check
        matched_worker = next((w for w in data["workers"] if w["pid"] == 55555), None)
        self.assertIsNotNone(matched_worker)
        self.assertEqual(matched_worker["status"], "healthy")
        self.assertEqual(len(matched_worker["current_tasks"]), 1)
        self.assertEqual(matched_worker["current_tasks"][0]["id"], task_proc.pk)

        # Diagram check
        self.assertIn("nodes", data["diagram"])
        self.assertIn("edges", data["diagram"])

    def test_workers_action_api_scheduler_commands(self):
        from workers.models import SchedulerState

        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={"is_running": False},
        )

        # Start scheduler command
        res_start = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_start", "target": "auto_wanted", "timer": "1800"}),
            content_type="application/json",
        )
        self.assertEqual(res_start.status_code, 200)
        self.assertTrue(res_start.json()["success"])
        state = SchedulerState.objects.get(name="auto_wanted")
        self.assertEqual(state.command_requested, "start")
        self.assertEqual(state.command_arg, "1800")

        # Stop scheduler command
        res_stop = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_stop", "target": "auto_wanted"}),
            content_type="application/json",
        )
        self.assertEqual(res_stop.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "stop")

        # Force run command
        res_force = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_force_run", "target": "auto_wanted"}),
            content_type="application/json",
        )
        self.assertEqual(res_force.status_code, 200)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "force_run")

    def test_workers_action_api_task_cancel_and_retry(self):
        from workers.models import TaskQueue

        # Cancel task
        pending_task = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/cancel-me/"],
            status=TaskQueue.Status.PENDING,
        )
        res_cancel = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "task_cancel", "target": pending_task.pk}),
            content_type="application/json",
        )
        self.assertEqual(res_cancel.status_code, 200)
        pending_task.refresh_from_db()
        self.assertEqual(pending_task.status, TaskQueue.Status.CANCELLED)
        self.assertIsNotNone(pending_task.finished_at)

        # Cancel active (PROCESSING) task while another task is queued
        active_task = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/active/"],
            status=TaskQueue.Status.PROCESSING,
        )
        queued_task = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/queued/"],
            status=TaskQueue.Status.PENDING,
        )
        res_cancel_active = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "task_cancel", "target": active_task.pk}),
            content_type="application/json",
        )
        self.assertEqual(res_cancel_active.status_code, 200)
        active_task.refresh_from_db()
        self.assertEqual(active_task.status, TaskQueue.Status.CANCELLED)
        self.assertIsNotNone(active_task.finished_at)
        queued_task.refresh_from_db()
        self.assertEqual(queued_task.status, TaskQueue.Status.PENDING)

        # Retry failed task
        failed_task = TaskQueue.objects.create(
            task_type="thumbnails_worker",
            args=[999],
            status=TaskQueue.Status.FAILED,
            error_message="Thumbnail generation failed",
        )
        res_retry = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "task_retry", "target": failed_task.pk}),
            content_type="application/json",
        )
        self.assertEqual(res_retry.status_code, 200)
        self.assertTrue(res_retry.json()["success"])
        retried_task = TaskQueue.objects.filter(task_type="thumbnails_worker", status=TaskQueue.Status.PENDING).last()
        self.assertIsNotNone(retried_task)
        self.assertIn("Retry of #", retried_task.reason)

    def test_workers_action_api_clear_pending_tasks(self):
        from workers.models import TaskQueue
        from workers import client as worker_client

        # 1. Clear all pending tasks via API
        TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/1/"], status=TaskQueue.Status.PENDING
        )
        TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/2/"], status=TaskQueue.Status.PENDING
        )
        TaskQueue.objects.create(task_type="fileinfo_worker", args=[101], status=TaskQueue.Status.PENDING)
        active_task = TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/active/"], status=TaskQueue.Status.PROCESSING
        )
        completed_task = TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/done/"], status=TaskQueue.Status.COMPLETED
        )

        res_clear_all = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "clear_pending_tasks"}),
            content_type="application/json",
        )
        self.assertEqual(res_clear_all.status_code, 200)
        self.assertTrue(res_clear_all.json()["success"])
        self.assertIn("Cleared 3 pending task(s)", res_clear_all.json()["message"])

        # Pending tasks are deleted, while active and completed are untouched
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count(), 0)
        self.assertTrue(TaskQueue.objects.filter(pk=active_task.pk).exists())
        self.assertTrue(TaskQueue.objects.filter(pk=completed_task.pk).exists())

        # 2. Clear pending tasks by specific task_type
        TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/w1/"], status=TaskQueue.Status.PENDING
        )
        TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/w2/"], status=TaskQueue.Status.PENDING
        )
        thumb_task = TaskQueue.objects.create(
            task_type="thumbnails_worker", args=[202], status=TaskQueue.Status.PENDING
        )

        res_clear_type = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "clear_pending_tasks", "task_type": "web_crawler"}),
            content_type="application/json",
        )
        self.assertEqual(res_clear_type.status_code, 200)
        self.assertTrue(res_clear_type.json()["success"])
        self.assertIn("Cleared 2 pending task(s) for 'web_crawler'", res_clear_type.json()["message"])

        # web_crawler pending tasks gone, thumbnails_worker still pending
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PENDING, task_type="web_crawler").count(), 0)
        self.assertTrue(TaskQueue.objects.filter(pk=thumb_task.pk).exists())

        # 3. Test clear_pending_tasks with mode="cancel"
        TaskQueue.objects.create(task_type="fileinfo_worker", args=[301], status=TaskQueue.Status.PENDING)
        res_cancel_mode = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "clear_pending_tasks", "mode": "cancel"}),
            content_type="application/json",
        )
        self.assertEqual(res_cancel_mode.status_code, 200)
        self.assertTrue(res_cancel_mode.json()["success"])
        self.assertIn("Cancelled", res_cancel_mode.json()["message"])
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count(), 0)
        self.assertGreaterEqual(TaskQueue.objects.filter(status=TaskQueue.Status.CANCELLED).count(), 1)

        # 4. Test client helper directly
        TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/c1/"], status=TaskQueue.Status.PENDING
        )
        deleted_count = worker_client.clear_pending_tasks()
        self.assertEqual(deleted_count, 1)
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count(), 0)

    def test_workers_action_api_cleanup_workers(self):
        from workers.models import WorkerProcess

        WorkerProcess.objects.create(
            pid=77777,
            hostname="dead-node",
            status="running",
            last_heartbeat=django_tz.now() - timedelta(seconds=120),
        )
        res_clean = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "cleanup_workers"}),
            content_type="application/json",
        )
        self.assertEqual(res_clean.status_code, 200)
        self.assertTrue(res_clean.json()["success"])
        w = WorkerProcess.objects.get(pid=77777)
        self.assertEqual(w.status, "stopped")

    def test_workers_action_api_worker_delete(self):
        from workers.models import WorkerProcess, SchedulerState

        # Stopped worker can be deleted
        WorkerProcess.objects.create(
            pid=88881,
            hostname="old-node",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(hours=2),
        )
        SchedulerState.objects.update_or_create(
            name="test_worker_sched",
            defaults={"worker_pid": 88881, "is_running": False},
        )
        res = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "worker_delete", "target": 88881}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])
        self.assertFalse(WorkerProcess.objects.filter(pid=88881).exists())
        sched = SchedulerState.objects.get(name="test_worker_sched")
        self.assertIsNone(sched.worker_pid)

        # Actively running healthy worker cannot be deleted directly
        WorkerProcess.objects.create(
            pid=88882,
            hostname="live-node",
            status="running",
            last_heartbeat=django_tz.now(),
        )
        res_live = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "worker_delete", "target": 88882}),
            content_type="application/json",
        )
        self.assertEqual(res_live.status_code, 400)
        self.assertTrue(WorkerProcess.objects.filter(pid=88882).exists())

    def test_workers_action_api_purge_workers(self):
        from workers.models import WorkerProcess

        # Create stopped workers and 1 healthy worker
        WorkerProcess.objects.create(
            pid=99901,
            hostname="stopped-1",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(hours=1),
        )
        WorkerProcess.objects.create(
            pid=99902,
            hostname="stopped-2",
            status="stopped",
            last_heartbeat=django_tz.now() - timedelta(days=10),
        )
        WorkerProcess.objects.create(
            pid=99903,
            hostname="live-runner",
            status="running",
            last_heartbeat=django_tz.now(),
        )

        res = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "purge_workers"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["success"])

        # Stopped workers deleted, healthy runner preserved
        self.assertFalse(WorkerProcess.objects.filter(pid=99901).exists())
        self.assertFalse(WorkerProcess.objects.filter(pid=99902).exists())
        self.assertTrue(WorkerProcess.objects.filter(pid=99903).exists())

    def test_stats_workers_json_content_negotiation(self):
        # GET with ?json=1
        res_query = self.staff_client.get("/stats/workers/?json=1")
        self.assertEqual(res_query.status_code, 200)
        self.assertEqual(res_query["Content-Type"], "application/json")
        self.assertIn("summary", res_query.json())

        # GET with Accept: application/json
        res_header = self.staff_client.get("/stats/workers/", HTTP_ACCEPT="application/json")
        self.assertEqual(res_header.status_code, 200)
        self.assertEqual(res_header["Content-Type"], "application/json")
        self.assertIn("summary", res_header.json())

    def test_workers_status_scheduler_next_run_and_stopped(self):
        from workers.models import SchedulerState

        now = django_tz.now()

        # 1. Stopped scheduler with past next_run
        SchedulerState.objects.update_or_create(
            name="link_monitor_7",
            defaults={
                "display_name": "link_monitor_7",
                "is_running": False,
                "cycle_timer": 900.0,
                "last_run": now - timedelta(hours=2),
                "next_run": now - timedelta(hours=1, minutes=45),
                "last_heartbeat": now - timedelta(hours=2),
            },
        )

        # 2. Active scheduler running a cycle (next_run in past / due now)
        SchedulerState.objects.update_or_create(
            name="auto_updater_panda",
            defaults={
                "display_name": "auto_updater_panda",
                "is_running": True,
                "cycle_timer": 86400.0,
                "last_run": now - timedelta(days=1),
                "next_run": now - timedelta(seconds=5),
                "last_heartbeat": now,
            },
        )

        # 3. Active scheduler with future next_run
        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={
                "display_name": "auto_wanted",
                "is_running": True,
                "cycle_timer": 3600.0,
                "last_run": now - timedelta(minutes=10),
                "next_run": now + timedelta(seconds=500),
                "last_heartbeat": now,
            },
        )

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        sched_map = {s["name"]: s for s in data["schedulers"]}

        # Check stopped scheduler
        s_stopped = sched_map["link_monitor_7"]
        self.assertFalse(s_stopped["is_active"])
        self.assertFalse(s_stopped["is_running"])
        self.assertIsNone(s_stopped["next_run_in_seconds"])
        self.assertEqual(s_stopped["next_run_in_formatted"], "-")
        self.assertEqual(s_stopped["next_run_formatted"], "Stopped")

        # Check active cycle running scheduler
        s_running = sched_map["auto_updater_panda"]
        self.assertTrue(s_running["is_active"])
        self.assertEqual(s_running["next_run_in_seconds"], 0.0)
        self.assertEqual(s_running["next_run_in_formatted"], "Running...")
        self.assertEqual(s_running["next_run_formatted"], "Running cycle")

        # Check future next run scheduler
        s_future = sched_map["auto_wanted"]
        self.assertTrue(s_future["is_active"])
        self.assertIsNotNone(s_future["next_run_in_seconds"])
        self.assertGreater(s_future["next_run_in_seconds"], 490)
        self.assertEqual(s_future["next_run_in_formatted"], "8m 20s")

    def test_workers_status_download_speed_aggregation(self):
        from viewer.models import DownloadEvent
        from workers.models import SchedulerState

        now = django_tz.now()

        # Scheduler with active download speed
        SchedulerState.objects.update_or_create(
            name="archive_download_queue",
            defaults={
                "display_name": "archive_download_queue",
                "is_running": True,
                "cycle_timer": 2.0,
                "last_heartbeat": now,
                "extra_data": {
                    "current_download": [
                        {
                            "filename": "manga_chapter_1.zip",
                            "speed": 5242880.0,  # 5.0 MB/s
                            "downloaded": 10485760,
                            "filesize": 20971520,
                            "index": 1,
                            "total": 1,
                        }
                    ]
                },
            },
        )

        # DownloadEvent in progress for that file
        DownloadEvent.objects.create(
            name="manga_chapter_1",
            download_id="/media/galleries/dls/manga_chapter_1.zip",
            download_url="https://example.com/download/1.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            total_size=20971520,
            progress=50.0,
        )

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # Summary download speed check
        self.assertEqual(data["summary"]["active_downloads_count"], 1)
        self.assertEqual(data["summary"]["total_download_speed"], 5242880.0)
        self.assertEqual(data["summary"]["total_download_speed_formatted"], "5.0 MB/s")

        # Downloads list speed check
        self.assertEqual(len(data["downloads"]), 1)
        dl = data["downloads"][0]
        self.assertEqual(dl["speed"], 5242880.0)
        self.assertEqual(dl["speed_formatted"], "5.0 MB/s")

    def test_workers_status_list_limit_param(self):
        from workers.models import TaskQueue

        for i in range(30):
            TaskQueue.objects.create(task_type="fileinfo_worker", args=[i], status=TaskQueue.Status.PENDING)
            TaskQueue.objects.create(
                task_type="fileinfo_worker",
                args=[i],
                status=TaskQueue.Status.COMPLETED,
                finished_at=django_tz.now(),
            )

        # Default limit is 25
        data = self.staff_client.get("/api/workers/status/").json()
        self.assertEqual(len(data["pending_tasks"]), 25)
        self.assertEqual(len(data["recent_tasks"]), 25)
        self.assertEqual(data["limits"]["limit"], 25)
        self.assertEqual(data["summary"]["pending_tasks_count"], 30)

        # Explicit limit
        data = self.staff_client.get("/api/workers/status/?limit=5").json()
        self.assertEqual(len(data["pending_tasks"]), 5)
        self.assertEqual(len(data["recent_tasks"]), 5)
        self.assertEqual(data["limits"]["limit"], 5)

        # Per-section override
        data = self.staff_client.get("/api/workers/status/?limit=5&recent_limit=10").json()
        self.assertEqual(len(data["pending_tasks"]), 5)
        self.assertEqual(len(data["recent_tasks"]), 10)

        # Invalid / out of range values are sanitized
        data = self.staff_client.get("/api/workers/status/?limit=abc").json()
        self.assertEqual(data["limits"]["limit"], 25)
        data = self.staff_client.get("/api/workers/status/?limit=0").json()
        self.assertEqual(data["limits"]["limit"], 1)
        data = self.staff_client.get("/api/workers/status/?limit=100000").json()
        self.assertEqual(data["limits"]["limit"], data["limits"]["max_limit"])

        # The HTML page honors the limit too
        res_page = self.staff_client.get("/stats/workers/?limit=5")
        self.assertEqual(res_page.status_code, 200)
        self.assertEqual(len(res_page.context["dashboard"]["pending_tasks"]), 5)

    def test_workers_status_downloads_broken_down_by_type(self):
        from viewer.models import DownloadEvent

        # Many stuck hath downloads, newer than the single archive download.
        archive_dl = DownloadEvent.objects.create(
            name="active_archive",
            method="archive",
            download_id="/tmp/active_archive.zip",
            status=DownloadEvent.Status.IN_PROGRESS,
            total_size=1000,
            progress=40.0,
        )
        for i in range(8):
            DownloadEvent.objects.create(
                name=f"stuck_hath_{i}",
                method="hath",
                download_id=f"hath_{i}",
                status=DownloadEvent.Status.IN_PROGRESS,
            )
        for i in range(2):
            DownloadEvent.objects.create(
                name=f"queued_torrent_{i}",
                method="torrent",
                download_id=f"torrent_{i}",
                status=DownloadEvent.Status.QUEUED,
            )
        # Finished downloads are not counted
        DownloadEvent.objects.create(
            name="done", method="archive", download_id="done", status=DownloadEvent.Status.COMPLETED
        )

        data = self.staff_client.get("/api/workers/status/?downloads_limit=3").json()

        self.assertEqual(data["summary"]["active_downloads_count"], 11)
        types = {t["type"]: t for t in data["download_types"]}
        self.assertEqual([t["type"] for t in data["download_types"]], ["archive", "hath", "torrent"])

        self.assertEqual(types["archive"]["total_count"], 1)
        self.assertEqual(types["archive"]["in_progress_count"], 1)
        self.assertEqual(types["archive"]["shown_count"], 1)
        self.assertFalse(types["archive"]["is_truncated"])

        self.assertEqual(types["hath"]["total_count"], 8)
        self.assertEqual(types["hath"]["shown_count"], 3)
        self.assertTrue(types["hath"]["is_truncated"])

        self.assertEqual(types["torrent"]["queued_count"], 2)
        self.assertEqual(types["torrent"]["shown_count"], 2)

        # The archive download is still listed despite the hath ones being newer.
        self.assertEqual(len(data["downloads"]), 6)
        self.assertEqual(data["downloads"][0]["id"], archive_dl.pk)
        self.assertEqual(data["downloads"][0]["type"], "archive")
        self.assertEqual(data["downloads"][0]["type_label"], "Archive")
        self.assertEqual(data["summary"]["shown_downloads_count"], 6)
