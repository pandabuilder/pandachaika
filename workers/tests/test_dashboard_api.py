# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

import django.utils.timezone as django_tz
from django.contrib.auth import get_user_model
from django.test import TestCase


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

        # Unknown scheduler name is rejected and does not create a row
        res_unknown = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_start", "target": "post_downloader"}),
            content_type="application/json",
        )
        self.assertEqual(res_unknown.status_code, 404)
        self.assertFalse(SchedulerState.objects.filter(name="post_downloader").exists())

        # Control rows cannot be targeted by scheduler actions
        SchedulerState.request_command("worker_runner", "restart")
        res_control = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "scheduler_start", "target": "worker_runner"}),
            content_type="application/json",
        )
        self.assertEqual(res_control.status_code, 404)
        self.assertEqual(SchedulerState.objects.get(name="worker_runner").command_requested, "restart")

    def test_workers_status_api_excludes_control_rows(self):
        from workers.models import SchedulerState

        now = django_tz.now()
        SchedulerState.objects.update_or_create(
            name="auto_wanted",
            defaults={"is_running": True, "last_heartbeat": now},
        )
        SchedulerState.request_command("worker_runner", "restart")

        res = self.staff_client.get("/api/workers/status/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        names = [s["name"] for s in data["schedulers"]]
        self.assertIn("auto_wanted", names)
        self.assertNotIn("worker_runner", names)
        self.assertEqual(data["summary"]["total_schedulers_count"], len(names))

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

        # The view takes the time again: pin it to the same instant, or a slow runner sees 499.x seconds left and
        # formats them as "8m 19s".
        with patch("viewer.views.workers_dashboard.django_tz.now", return_value=now):
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
        self.assertEqual(s_future["next_run_in_seconds"], 500.0)
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

    def _create_batch_block(
        self,
        batch_id: str,
        index: int,
        total: int,
        *,
        status: str = "pending",
        priority: int = 10,
        total_links: int = 10,
        args: list[str] | None = None,
        providers_stats: dict | None = None,
        reason: str = "",
        error_message: str = "",
        traceback_text: str = "",
        finished_at=None,
        started_at=None,
        user=None,
    ):
        from workers.models import TaskQueue

        now = django_tz.now()
        options: dict = {
            "crawl_batch": {
                "id": batch_id,
                "index": index,
                "total": total,
                "total_links": total_links,
                "created_at": now.isoformat(),
            }
        }
        if providers_stats is not None:
            options["crawl_batch_stats"] = {"providers": providers_stats}

        return TaskQueue.objects.create(
            task_type="web_crawler",
            args=args or [f"https://e-hentai.org/g/{index}01/aaa/", f"https://e-hentai.org/g/{index}02/bbb/"],
            options=options,
            user=user or self.staff_user,
            reason=reason,
            priority=priority,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            error_message=error_message,
            traceback=traceback_text,
        )

    def test_workers_status_groups_split_crawl_batches_and_aggregates_provider_stats(self):
        from workers.models import TaskQueue

        now = django_tz.now()
        active_bid = "batchlive001"
        done_bid = "batchdone002"

        # Active batch: 4 blocks (1 completed, 1 processing with live stats, 2 pending)
        self._create_batch_block(
            active_bid,
            1,
            4,
            status=TaskQueue.Status.COMPLETED,
            priority=8,
            total_links=8,
            reason="Nightly import",
            started_at=now - timedelta(seconds=40),
            finished_at=now - timedelta(seconds=20),
            providers_stats={
                "panda": {
                    "db_total": 2,
                    "db_existing": 1,
                    "db_discarded": 1,
                    "db_to_update": 0,
                    "meta_evaluated": 1,
                    "meta_accepted": 1,
                    "meta_matched_wanted": 1,
                    "to_download": 1,
                    "dispatched_galleries": 1,
                    "dispatched_tasks": 1,
                }
            },
        )
        t_proc = self._create_batch_block(
            active_bid,
            2,
            4,
            status=TaskQueue.Status.PROCESSING,
            priority=8,
            total_links=8,
            reason="Nightly import",
            started_at=now - timedelta(seconds=15),
            providers_stats={
                "panda": {
                    "db_total": 2,
                    "db_existing": 0,
                    "meta_evaluated": 2,
                    "meta_accepted": 1,
                    "meta_banned": 1,
                    "to_download": 1,
                },
                "nhentai": {
                    "db_total": 3,
                    "db_existing": 1,
                    "db_to_update": 1,
                    "meta_evaluated": 2,
                    "meta_accepted": 2,
                    "to_download": 2,
                    "dispatched_galleries": 2,
                    "dispatched_tasks": 1,
                },
            },
        )
        t_pend1 = self._create_batch_block(
            active_bid, 3, 4, status=TaskQueue.Status.PENDING, priority=8, total_links=8, reason="Nightly import"
        )
        self._create_batch_block(
            active_bid, 4, 4, status=TaskQueue.Status.PENDING, priority=8, total_links=8, reason="Nightly import"
        )

        # Recently finished batch (2 completed blocks)
        self._create_batch_block(
            done_bid,
            1,
            2,
            status=TaskQueue.Status.COMPLETED,
            total_links=4,
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=4),
            providers_stats={"panda": {"db_total": 2, "db_existing": 2, "db_discarded": 2}},
        )
        self._create_batch_block(
            done_bid,
            2,
            2,
            status=TaskQueue.Status.COMPLETED,
            total_links=4,
            started_at=now - timedelta(minutes=4),
            finished_at=now - timedelta(minutes=3),
            providers_stats={"panda": {"db_total": 2, "db_existing": 0, "to_download": 2}},
        )

        # Even with pending_limit=1, the entire active batch (all 4 blocks) is aggregated accurately.
        res = self.staff_client.get("/api/workers/status/?pending_limit=1")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        self.assertEqual(data["summary"]["active_crawl_batches_count"], 1)
        self.assertEqual(data["summary"]["total_crawl_batches_count"], 2)

        batches = data["crawl_batches"]
        self.assertEqual([b["batch_id"] for b in batches], [active_bid, done_bid])

        active = batches[0]
        self.assertTrue(active["is_active"])
        self.assertEqual(active["status"], "processing")
        self.assertEqual(active["completed_blocks"], 1)
        self.assertEqual(active["processing_blocks"], 1)
        self.assertEqual(active["pending_blocks"], 2)
        self.assertEqual(active["remaining_blocks"], 3)
        self.assertEqual(active["total_blocks"], 4)
        self.assertEqual(active["total_links"], 8)
        self.assertEqual(active["progress_percent"], 25.0)
        self.assertEqual(active["priority"], 8)
        self.assertEqual(active["reason"], "Nightly import")
        self.assertEqual([blk["index"] for blk in active["blocks"]], [1, 2, 3, 4])

        # Verify aggregated provider stats across blocks
        panda = active["providers"]["panda"]
        self.assertEqual(panda["db_total"], 4)
        self.assertEqual(panda["db_existing"], 1)
        self.assertEqual(panda["db_new"], 3)
        self.assertEqual(panda["meta_evaluated"], 3)
        self.assertEqual(panda["meta_accepted"], 2)
        self.assertEqual(panda["meta_banned"], 1)
        self.assertEqual(panda["to_download"], 2)
        self.assertEqual(panda["dispatched_galleries"], 1)
        self.assertEqual(panda["dispatched_tasks"], 1)

        nhentai = active["providers"]["nhentai"]
        self.assertEqual(nhentai["db_total"], 3)
        self.assertEqual(nhentai["db_new"], 2)
        self.assertEqual(nhentai["db_to_update"], 1)
        self.assertEqual(nhentai["dispatched_galleries"], 2)

        # Verify overall totals across both providers
        self.assertEqual(active["totals"]["db_total"], 7)
        self.assertEqual(active["totals"]["db_new"], 5)
        self.assertEqual(active["totals"]["to_download"], 4)

        # Verify finished batch
        done = batches[1]
        self.assertFalse(done["is_active"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["completed_blocks"], 2)
        self.assertEqual(done["total_blocks"], 2)
        self.assertEqual(done["progress_percent"], 100.0)
        self.assertEqual(done["providers"]["panda"]["db_total"], 4)
        self.assertEqual(done["providers"]["panda"]["db_new"], 2)

        # Verify flat task lists carry batch reference metadata
        proc_item = next(t for t in data["active_tasks"] if t["id"] == t_proc.pk)
        self.assertEqual(proc_item["batch_id"], active_bid)
        self.assertEqual(proc_item["batch_index"], 2)
        self.assertEqual(proc_item["batch_total"], 4)

        pend_item = next(t for t in data["pending_tasks"] if t["id"] == t_pend1.pk)
        self.assertEqual(pend_item["batch_id"], active_bid)
        self.assertEqual(pend_item["batch_index"], 3)
        self.assertEqual(pend_item["batch_total"], 4)

        # Verify SSR HTML renders collapsible batch card and controls
        res_html = self.staff_client.get("/stats/workers/")
        self.assertEqual(res_html.status_code, 200)
        self.assertContains(res_html, active_bid)
        self.assertContains(res_html, "1 / 4")
        self.assertContains(res_html, "Cancel remaining blocks")
        self.assertContains(res_html, "Reprioritize batch")

    def test_crawl_batch_collector_persists_live_stats_while_processing(self):
        from workers.crawl_batch import CrawlBatchCollector
        from workers.models import TaskQueue

        batch_id = "batchliveflush"
        task = self._create_batch_block(
            batch_id,
            1,
            2,
            status=TaskQueue.Status.PROCESSING,
            started_at=django_tz.now(),
        )
        self._create_batch_block(batch_id, 2, 2, status=TaskQueue.Status.PENDING)

        collector = CrawlBatchCollector(batch_id, task_pk=task.pk)
        collector.record_db_check("panda", total=5, existing=2, discarded=1, to_update=1)
        collector.record_metadata_check("panda", total_evaluated=4, accepted=3, banned=1)
        collector.record_download_dispatch("panda", gallery_count=3, dispatched=True)

        data = self.staff_client.get("/api/workers/status/").json()
        batch = next(b for b in data["crawl_batches"] if b["batch_id"] == batch_id)
        panda = batch["providers"]["panda"]
        self.assertEqual(panda["db_total"], 5)
        self.assertEqual(panda["db_new"], 3)
        self.assertEqual(panda["meta_evaluated"], 4)
        self.assertEqual(panda["meta_accepted"], 3)
        self.assertEqual(panda["meta_banned"], 1)
        self.assertEqual(panda["to_download"], 3)
        self.assertEqual(panda["dispatched_galleries"], 3)
        self.assertEqual(panda["dispatched_tasks"], 1)

    def test_workers_action_api_batch_cancel(self):
        from workers.models import TaskQueue

        batch_id = "batchcancel99"
        now = django_tz.now()

        t_done = self._create_batch_block(
            batch_id,
            1,
            4,
            status=TaskQueue.Status.COMPLETED,
            finished_at=now - timedelta(seconds=10),
            providers_stats={"panda": {"db_total": 2, "db_existing": 0}},
        )
        t_proc = self._create_batch_block(batch_id, 2, 4, status=TaskQueue.Status.PROCESSING, started_at=now)
        t_pend1 = self._create_batch_block(batch_id, 3, 4, status=TaskQueue.Status.PENDING)
        t_pend2 = self._create_batch_block(batch_id, 4, 4, status=TaskQueue.Status.PENDING)
        unrelated = TaskQueue.objects.create(
            task_type="web_crawler", args=["https://example.com/solo/"], status=TaskQueue.Status.PENDING
        )

        # Non-staff user rejected
        res_forbidden = self.user_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_cancel", "batch_id": batch_id}),
            content_type="application/json",
        )
        self.assertEqual(res_forbidden.status_code, 403)

        # Unknown batch -> 404
        res_404 = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_cancel", "batch_id": "nonexistent123"}),
            content_type="application/json",
        )
        self.assertEqual(res_404.status_code, 404)

        # Cancel remaining blocks (pending + processing) and verify grouped summary log emitted
        with patch("workers.crawl_batch.logger") as mock_batch_logger:
            res = self.staff_client.post(
                "/api/workers/action/",
                data=json.dumps({"action": "batch_cancel", "batch_id": batch_id}),
                content_type="application/json",
            )
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertTrue(body["success"])
            self.assertEqual(body["cancelled_count"], 3)
            self.assertEqual(body["cancelled_pending"], 2)
            self.assertEqual(body["cancelled_processing"], 1)
            self.assertGreater(mock_batch_logger.info.call_count, 0)

        t_done.refresh_from_db()
        t_proc.refresh_from_db()
        t_pend1.refresh_from_db()
        t_pend2.refresh_from_db()
        unrelated.refresh_from_db()

        self.assertEqual(t_done.status, TaskQueue.Status.COMPLETED)
        self.assertEqual(t_proc.status, TaskQueue.Status.CANCELLED)
        self.assertEqual(t_pend1.status, TaskQueue.Status.CANCELLED)
        self.assertEqual(t_pend2.status, TaskQueue.Status.CANCELLED)
        self.assertIsNotNone(t_pend1.finished_at)
        self.assertEqual(unrelated.status, TaskQueue.Status.PENDING)

        # Calling again when no remaining blocks exist returns 400
        res_again = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_cancel", "batch_id": batch_id}),
            content_type="application/json",
        )
        self.assertEqual(res_again.status_code, 400)

    def test_workers_action_api_batch_cancel_pending_only(self):
        from workers.models import TaskQueue

        batch_id = "batchpendonly"
        t_proc = self._create_batch_block(
            batch_id, 1, 2, status=TaskQueue.Status.PROCESSING, started_at=django_tz.now()
        )
        t_pend = self._create_batch_block(batch_id, 2, 2, status=TaskQueue.Status.PENDING)

        res = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_cancel", "batch_id": batch_id, "include_processing": False}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["cancelled_count"], 1)
        self.assertEqual(body["cancelled_pending"], 1)
        self.assertEqual(body["cancelled_processing"], 0)

        t_proc.refresh_from_db()
        t_pend.refresh_from_db()
        self.assertEqual(t_proc.status, TaskQueue.Status.PROCESSING)
        self.assertEqual(t_pend.status, TaskQueue.Status.CANCELLED)

    def test_workers_action_api_batch_reprioritize(self):
        from workers.models import TaskQueue

        batch_id = "batchprio001"
        t_done = self._create_batch_block(
            batch_id, 1, 4, status=TaskQueue.Status.COMPLETED, priority=10, finished_at=django_tz.now()
        )
        t_proc = self._create_batch_block(
            batch_id, 2, 4, status=TaskQueue.Status.PROCESSING, priority=10, started_at=django_tz.now()
        )
        t_pend1 = self._create_batch_block(batch_id, 3, 4, status=TaskQueue.Status.PENDING, priority=10)
        t_pend2 = self._create_batch_block(batch_id, 4, 4, status=TaskQueue.Status.PENDING, priority=9)
        other_batch = self._create_batch_block("otherbatch99", 1, 2, status=TaskQueue.Status.PENDING, priority=10)

        # Validation errors
        for bad_priority in (0, 11, -1, "abc", 2.5, True, None):
            res_bad = self.staff_client.post(
                "/api/workers/action/",
                data=json.dumps({"action": "batch_reprioritize", "batch_id": batch_id, "priority": bad_priority}),
                content_type="application/json",
            )
            self.assertEqual(res_bad.status_code, 400, f"Expected 400 for priority={bad_priority!r}")

        # Unknown batch -> 404
        res_404 = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_reprioritize", "batch_id": "missingbatch", "priority": 2}),
            content_type="application/json",
        )
        self.assertEqual(res_404.status_code, 404)

        # Valid reprioritize updates only PENDING blocks in the target batch
        res_ok = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_reprioritize", "batch_id": batch_id, "priority": 2}),
            content_type="application/json",
        )
        self.assertEqual(res_ok.status_code, 200)
        body = res_ok.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["priority"], 2)
        self.assertEqual(body["updated_count"], 2)

        t_done.refresh_from_db()
        t_proc.refresh_from_db()
        t_pend1.refresh_from_db()
        t_pend2.refresh_from_db()
        other_batch.refresh_from_db()

        self.assertEqual(t_done.priority, 10)
        self.assertEqual(t_proc.priority, 10)
        self.assertEqual(t_pend1.priority, 2)
        self.assertEqual(t_pend2.priority, 2)
        self.assertEqual(other_batch.priority, 10)

        # Batch with no pending blocks returns 400
        t_pend1.status = TaskQueue.Status.COMPLETED
        t_pend2.status = TaskQueue.Status.COMPLETED
        t_pend1.save(update_fields=["status"])
        t_pend2.save(update_fields=["status"])
        res_none = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "batch_reprioritize", "batch_id": batch_id, "priority": 1}),
            content_type="application/json",
        )
        self.assertEqual(res_none.status_code, 400)

    def test_task_retry_clears_transient_crawl_batch_stats(self):
        from workers.models import TaskQueue

        failed_block = self._create_batch_block(
            "batchretry01",
            1,
            2,
            status=TaskQueue.Status.FAILED,
            finished_at=django_tz.now(),
            providers_stats={"panda": {"db_total": 2, "db_existing": 0}},
            error_message="Connection reset",
        )
        failed_block.options["crawl_batch_summary_logged"] = True
        failed_block.save(update_fields=["options"])

        res = self.staff_client.post(
            "/api/workers/action/",
            data=json.dumps({"action": "task_retry", "target": failed_block.pk}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        retried = TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).order_by("-id").first()
        self.assertIsNotNone(retried)
        self.assertIn("crawl_batch", retried.options)
        self.assertNotIn("crawl_batch_stats", retried.options)
        self.assertNotIn("crawl_batch_summary_logged", retried.options)
