from datetime import timedelta
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback

import django.utils.timezone as django_tz
from django import db
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest

from core.base.cancellation import TaskCancelledError, clear_task_hooks, set_task_hooks
from core.base.errors import ProviderBannedError
from core.base.setup import Settings
from core.base.utilities import get_part_filepath
from core.web.crawler import WebCrawler
from workers.client import expand_task_type_aliases
from workers.models import TaskQueue, WorkerProcess, SchedulerState
from workers.serialization import deserialize_settings_options

logger = logging.getLogger(__name__)


__all__ = ["TaskCancelledError", "WorkerRunner"]


def _safe_close_old_connections() -> None:
    try:
        if not db.connection.in_atomic_block:
            db.close_old_connections()
    except Exception:
        pass


class WorkerRunner:

    def __init__(
        self,
        settings: Settings,
        poll_interval: float = 1.0,
        heartbeat_interval: float = 10.0,
        enable_schedulers: bool = True,
        force_start_schedulers: bool = False,
        schedulers_to_start: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.enable_schedulers = enable_schedulers
        self.force_start_schedulers = force_start_schedulers
        self.schedulers_to_start = schedulers_to_start or []
        self.running = False
        self.pid = os.getpid()
        self.hostname = socket.gethostname()
        self.last_heartbeat_time = 0.0
        self.current_job: TaskQueue | None = None
        self.active_jobs: dict[int, TaskQueue] = {}
        self.active_threads: dict[int, threading.Thread] = {}
        self.active_job_activity: dict[int, float] = {}
        self.active_cancel_events: dict[int, threading.Event] = {}
        self._jobs_lock = threading.Lock()
        self._job_finished_event = threading.Event()
        self.settings.is_worker_process = True
        try:
            from pandabackup.settings import configure_database

            configure_database(self.settings)
        except Exception:
            pass
        if self.settings.workers.web_queue is None:
            from core.workers.webqueue import WebQueue
            self.settings.workers.web_queue = WebQueue(self.settings)
        self.heartbeat_thread: threading.Thread | None = None
        self.heartbeat_stop_event = threading.Event()
        self.should_restart = False

    def get_task_stall_timeout(self) -> float:
        """Return stall/inactivity timeout in seconds for TaskQueue jobs (0 disables)."""
        val = getattr(self.settings, "worker_task_stall_timeout", 1800.0)
        try:
            return max(0.0, float(val if val is not None else 1800.0))
        except (TypeError, ValueError):
            return 1800.0

    def prune_inactive_jobs(self) -> list[int]:
        """Prune jobs from active_jobs that are no longer PROCESSING in the database."""
        with self._jobs_lock:
            active_pks = list(self.active_jobs.keys())
        if not active_pks:
            return []

        try:
            still_processing_pks = set(
                TaskQueue.objects.filter(
                    pk__in=active_pks, status=TaskQueue.Status.PROCESSING
                ).values_list("pk", flat=True)
            )
        except Exception:
            return []

        cancelled_pks = [pk for pk in active_pks if pk not in still_processing_pks]
        if cancelled_pks:
            with self._jobs_lock:
                for pk in cancelled_pks:
                    logger.info("Active task #%d is no longer PROCESSING in DB; releasing from active_jobs", pk)
                    self.active_jobs.pop(pk, None)
                    self.active_threads.pop(pk, None)
                    self.active_job_activity.pop(pk, None)
                    cancel_ev = self.active_cancel_events.get(pk)
                    if cancel_ev:
                        cancel_ev.set()
                self.current_job = next(iter(self.active_jobs.values()), None)
            self._job_finished_event.set()
        return cancelled_pks

    def touch_job_activity(self, job_pk: int | None = None) -> None:
        """Update activity timestamp for a running TaskQueue job (or all active jobs if job_pk is None)."""
        now_mono = time.monotonic()
        with self._jobs_lock:
            if job_pk is not None:
                cancel_ev = self.active_cancel_events.get(job_pk)
                if (cancel_ev and cancel_ev.is_set()) or (job_pk not in self.active_jobs):
                    raise TaskCancelledError(f"Task #{job_pk} was cancelled or is no longer active")
                self.active_job_activity[job_pk] = now_mono
            else:
                for pk in self.active_jobs:
                    self.active_job_activity[pk] = now_mono

    def check_stuck_jobs(self) -> list[int]:
        """Check active TaskQueue jobs for inactivity timeouts while respecting active slow downloads."""
        self.prune_inactive_jobs()
        stall_timeout = self.get_task_stall_timeout()
        if stall_timeout <= 0:
            return []

        now_mono = time.monotonic()
        now_wall = time.time()

        # If any inline/synchronous download file on disk was modified recently, refresh web_crawler activity
        try:
            from viewer.models import DownloadEvent

            inline_dls = list(
                DownloadEvent.objects.filter(
                    status=DownloadEvent.Status.IN_PROGRESS,
                    completed=False,
                    download_url="",
                ).exclude(download_id="")[:20]
            )
            for ev in inline_dls:
                for candidate_path in (ev.download_id, get_part_filepath(ev.download_id)):
                    if candidate_path and os.path.exists(candidate_path):
                        try:
                            mtime = os.path.getmtime(candidate_path)
                            if (now_wall - mtime) < min(stall_timeout, 60.0):
                                with self._jobs_lock:
                                    for pk, j in self.active_jobs.items():
                                        if j.task_type in (
                                            "web_crawler",
                                            "webcrawler",
                                            "gallery_creator",
                                            "gallery_worker",
                                            "create_galleries",
                                        ):
                                            self.active_job_activity[pk] = now_mono
                                break
                        except OSError:
                            pass
        except Exception:
            pass

        timed_out_jobs: list[TaskQueue] = []
        with self._jobs_lock:
            for pk, job in list(self.active_jobs.items()):
                last_act = self.active_job_activity.get(pk, now_mono)
                if (now_mono - last_act) > stall_timeout:
                    self.active_jobs.pop(pk, None)
                    self.active_threads.pop(pk, None)
                    self.active_job_activity.pop(pk, None)
                    # The thread can't be killed. Make it stop at its next cancellation check.
                    cancel_ev = self.active_cancel_events.get(pk)
                    if cancel_ev:
                        cancel_ev.set()
                    timed_out_jobs.append(job)
            if timed_out_jobs:
                self.current_job = next(iter(self.active_jobs.values()), None)

        timed_out_pks: list[int] = []
        for job in timed_out_jobs:
            timed_out_pks.append(job.pk)
            err_msg = f"FAILED (timeout): Task timed out after {stall_timeout:.0f}s of inactivity"
            logger.error("Task #%d (%s) stalled for >%.1fs; marking as FAILED (timeout)", job.pk, job.task_type, stall_timeout)
            try:
                job.refresh_from_db()
                if job.status == TaskQueue.Status.PROCESSING:
                    job.status = TaskQueue.Status.FAILED
                    job.finished_at = django_tz.now()
                    job.error_message = err_msg
                    job.save(update_fields=["status", "finished_at", "error_message"])
            except Exception as e:
                logger.error("Failed to mark timed-out Task #%d as FAILED: %s", job.pk, e)

        if timed_out_pks:
            self._job_finished_event.set()

        return timed_out_pks

    def setup_signals(self) -> None:
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._handle_restart)

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        logger.info("Received shutdown signal (%d). Shutting down worker...", signum)
        self.running = False
        self._job_finished_event.set()
        try:
            from core.base.process_pool import shutdown_process_pool

            shutdown_process_pool(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _handle_restart(self, signum: int, frame: object) -> None:
        logger.info("Received restart signal (%d). Shutting down worker for restart...", signum)
        self.should_restart = True
        self.running = False
        self._job_finished_event.set()
        try:
            from core.base.process_pool import shutdown_process_pool

            shutdown_process_pool(wait=False, cancel_futures=True)
        except Exception:
            pass

    def sync_schedulers_state(self) -> None:
        """Sync live running status and heartbeats of local schedulers to SchedulerState."""
        if not self.enable_schedulers:
            return
        try:
            now = django_tz.now()
            for s in self.settings.workers.get_active_initialized_workers():
                is_running = s.is_running_locally()
                last_run = getattr(s, "current_last_run", s.last_run)
                next_run = (last_run + timedelta(seconds=s.timer)) if (last_run and is_running) else None
                extra_data = {}
                if (
                    s.thread_name in ("download_progress_checker", "archive_download_queue")
                    and hasattr(s, "current_download")
                ):
                    try:
                        downloads = s.current_download()
                        extra_data["current_download"] = [
                            {
                                "filename": getattr(d, "filename", ""),
                                "speed": getattr(d, "speed", 0.0),
                                "index": getattr(d, "index", 0),
                                "total": getattr(d, "total", 0),
                                "downloaded": getattr(d, "downloaded", 0),
                                "filesize": getattr(d, "filesize", 0),
                            }
                            for d in (downloads or [])
                            if getattr(d, "filename", "")
                        ]
                    except Exception:
                        pass

                SchedulerState.objects.update_or_create(
                    name=s.thread_name,
                    defaults={
                        "display_name": getattr(s, "thread_name", ""),
                        "is_running": is_running,
                        "last_run": last_run,
                        "next_run": next_run,
                        "cycle_timer": s.timer,
                        "worker_pid": self.pid,
                        "last_heartbeat": now,
                        "extra_data": extra_data,
                    },
                )
        except Exception as e:
            logger.warning("Failed to sync schedulers state: %s", e)

    def sync_monitored_links(self) -> None:
        """Keep timed_link_monitors in sync with the database, stopping and pruning deleted links."""
        if not self.enable_schedulers:
            return
        try:
            from viewer.models import MonitoredLink
            existing_pks = set(MonitoredLink.objects.values_list("pk", flat=True))
            for lm in list(self.settings.workers.timed_link_monitors):
                if lm.monitored_link.pk not in existing_pks:
                    lm.stop_running(force_local=True)
                    self.settings.workers.timed_link_monitors.remove(lm)
                    SchedulerState.objects.filter(name=lm.thread_name).delete()
        except Exception as e:
            logger.warning("Error syncing monitored links: %s", e)

    def check_scheduler_commands(self) -> None:
        """Check and process pending start/stop/force_run/restart commands from SchedulerState."""
        try:
            pending_commands = list(SchedulerState.objects.exclude(command_requested=""))
            if not pending_commands:
                return

            # Check for worker-runner level commands (e.g. restart) regardless of enable_schedulers
            for state in pending_commands:
                cmd = state.command_requested
                name = state.name
                if name in ("worker_runner", "worker_process") and cmd == "restart":
                    logger.info("Executing requested 'restart' for %s via IPC", name)
                    state.command_requested = ""
                    state.command_arg = ""
                    state.last_heartbeat = django_tz.now()
                    state.save(update_fields=["command_requested", "command_arg", "last_heartbeat"])
                    self.should_restart = True
                    self.running = False
                    self._job_finished_event.set()
                    try:
                        from core.base.process_pool import shutdown_process_pool

                        shutdown_process_pool(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    return

            if not self.enable_schedulers:
                return

            schedulers_by_name = {
                s.thread_name: s for s in self.settings.workers.get_active_initialized_workers()
            }

            for state in pending_commands:
                cmd = state.command_requested
                name = state.name
                scheduler = schedulers_by_name.get(name)

                # Special handling for MonitoredLink dynamic schedulers
                if name.startswith("link_monitor_"):
                    try:
                        link_pk = int(name.replace("link_monitor_", ""))
                    except ValueError:
                        link_pk = None

                    if cmd == "delete":
                        logger.info("Executing requested 'delete' for link monitor '%s'", name)
                        if scheduler:
                            scheduler.stop_running(force_local=True)
                            if link_pk is not None:
                                self.settings.workers.remove_link_monitor(link_pk)
                        state.delete()
                        continue

                    if link_pk is not None:
                        from viewer.models import MonitoredLink
                        link = MonitoredLink.objects.filter(pk=link_pk).first()
                        if not link:
                            # Link was removed from database
                            if scheduler:
                                scheduler.stop_running(force_local=True)
                                self.settings.workers.remove_link_monitor(link_pk)
                            state.delete()
                            continue

                        if not scheduler:
                            scheduler = self.settings.workers.add_new_link_monitor(self.settings, link)
                            schedulers_by_name[name] = scheduler
                        else:
                            scheduler.monitored_link = link
                            scheduler.link_name = link.name
                            scheduler.timer = link.frequency.total_seconds()
                            scheduler.original_timer = link.frequency

                        if cmd == "reload":
                            logger.info("Executing requested 'reload' for link monitor '%s'", name)
                            if not link.enabled:
                                if scheduler.is_running_locally():
                                    scheduler.stop_running(force_local=True)
                            elif scheduler.is_running_locally():
                                scheduler.stop_running(force_local=True)
                                timer = float(state.command_arg) if state.command_arg else link.frequency.total_seconds()
                                scheduler.start_running(timer=timer, force_local=True)
                            elif link.auto_start:
                                timer = float(state.command_arg) if state.command_arg else link.frequency.total_seconds()
                                scheduler.start_running(timer=timer, force_local=True)

                if scheduler:
                    if cmd == "start":
                        logger.info("Executing requested 'start' for scheduler '%s'", name)
                        start_timer: float | None = float(state.command_arg) if state.command_arg else None
                        scheduler.start_running(timer=start_timer, force_local=True)
                    elif cmd == "stop":
                        logger.info("Executing requested 'stop' for scheduler '%s'", name)
                        scheduler.stop_running(force_local=True)
                    elif cmd == "force_run":
                        logger.info("Executing requested 'force_run' for scheduler '%s'", name)
                        scheduler.force_run(force_local=True)

                state.command_requested = ""
                state.command_arg = ""
                state.is_running = scheduler.is_running_locally() if scheduler else False
                state.last_heartbeat = django_tz.now()
                state.save(update_fields=["command_requested", "command_arg", "is_running", "last_heartbeat"])

        except Exception as e:
            logger.warning("Error processing scheduler commands: %s", e)

    def update_heartbeat(self, force: bool = False) -> None:
        now = time.time()
        if force or (now - self.last_heartbeat_time >= self.heartbeat_interval):
            try:
                _safe_close_old_connections()
                WorkerProcess.objects.update_or_create(
                    pid=self.pid,
                    defaults={
                        "hostname": self.hostname,
                        "worker_type": "general",
                        "status": "running",
                        "last_heartbeat": django_tz.now(),
                    },
                )
                WorkerProcess.cleanup_dead_workers()
                self.check_stuck_jobs()
                self.sync_monitored_links()
                self.sync_schedulers_state()
                self.last_heartbeat_time = now
            except Exception as e:
                logger.warning("Failed to update worker heartbeat: %s", e)

    def start_heartbeat_thread(self) -> None:
        if self.heartbeat_thread is not None and self.heartbeat_thread.is_alive():
            return
        self.heartbeat_stop_event.clear()
        self.heartbeat_thread = threading.Thread(
            name="worker_heartbeat",
            target=self._heartbeat_loop,
            daemon=True,
        )
        self.heartbeat_thread.start()

    def stop_heartbeat_thread(self) -> None:
        if self.heartbeat_thread is not None:
            self.heartbeat_stop_event.set()
            self.heartbeat_thread.join(timeout=3.0)
            self.heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        check_interval = min(self.heartbeat_interval / 2, 2.0)
        try:
            while not self.heartbeat_stop_event.is_set():
                try:
                    _safe_close_old_connections()
                    self.check_scheduler_commands()
                    self.update_heartbeat()
                except Exception as e:
                    logger.warning("Error in heartbeat loop: %s", e)
                finally:
                    _safe_close_old_connections()
                self.heartbeat_stop_event.wait(timeout=check_interval)
        finally:
            try:
                if not db.connection.in_atomic_block:
                    db.connections.close_all()
            except Exception:
                pass

    def warn_on_pid_collision(self) -> None:
        """Workers are identified by PID only, so two hosts/containers sharing a PID would overwrite each other."""
        other = WorkerProcess.objects.filter(
            pid=self.pid,
            status="running",
            last_heartbeat__gte=django_tz.now() - timedelta(seconds=60),
        ).exclude(hostname=self.hostname)
        for row in other:
            logger.critical(
                "Another live worker on host %r uses the same PID (%d). Workers are tracked by PID, so their tasks "
                "and heartbeats will get mixed up. Run workers with distinct PIDs (e.g. `pid: host`/different "
                "PID namespaces).",
                row.hostname,
                self.pid,
            )

    def register_start(self) -> None:
        _safe_close_old_connections()
        self.warn_on_pid_collision()
        WorkerProcess.objects.update_or_create(
            pid=self.pid,
            defaults={
                "hostname": self.hostname,
                "worker_type": "general",
                "status": "running",
                "started_at": django_tz.now(),
                "last_heartbeat": django_tz.now(),
            },
        )
        WorkerProcess.cleanup_dead_workers()
        running_pids = list(WorkerProcess.objects.filter(status="running").values_list("pid", flat=True))
        other_running_pids = [p for p in running_pids if p != self.pid]
        stale_qs = TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).exclude(
            worker_pid__in=other_running_pids
        )
        requeued, failed = TaskQueue.release_interrupted(
            stale_qs, reason="Worker stopped unexpectedly while running this task too many times."
        )
        if requeued or failed:
            logger.warning(
                "Found %d task(s) interrupted by a worker crash: %d re-queued, %d failed (too many attempts).",
                requeued + failed,
                requeued,
                failed,
            )
        try:
            from viewer.models import DownloadEvent

            stale_dl_qs = (
                DownloadEvent.objects.filter(
                    status=DownloadEvent.Status.IN_PROGRESS,
                    completed=False,
                )
                .exclude(download_url="")
                .exclude(worker_pid__in=other_running_pids)
            )
            stale_dl_qs.update(
                status=DownloadEvent.Status.QUEUED,
                worker_pid=None,
                progress=0.0,
                attempts=Greatest(F("attempts") - 1, 0),
            )
        except Exception as e:
            logger.warning("Failed to reset stale in-progress download events on start: %s", e)
        self.last_heartbeat_time = time.time()
        logger.info("Worker process registered with PID %d on %s", self.pid, self.hostname)

    def register_stop(self) -> None:
        try:
            from core.base.process_pool import shutdown_process_pool

            shutdown_process_pool(wait=True)
        except Exception:
            pass
        try:
            SchedulerState.objects.filter(worker_pid=self.pid).update(is_running=False)
        except Exception:
            pass
        try:
            _safe_close_old_connections()
            WorkerProcess.objects.filter(pid=self.pid).update(status="stopped", last_heartbeat=django_tz.now())
            logger.info("Worker process unregistered with PID %d", self.pid)
        except Exception as e:
            logger.warning("Failed to mark worker process as stopped: %s", e)

    TASK_CLAIM_LOCK_ID = 7_283_104_551

    def _lock_task_claims(self) -> None:
        """Take a transaction-scoped lock that serializes task claims across all workers."""
        if db.connection.vendor != "postgresql":
            return
        with db.connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [self.TASK_CLAIM_LOCK_ID])

    def claim_next_job(self) -> TaskQueue | None:
        try:
            self.prune_inactive_jobs()
            with transaction.atomic():
                # Only one worker at a time may decide which task types are busy and claim a task, otherwise two
                # workers can both see "not busy" and start two tasks of the same type.
                self._lock_task_claims()
                processing_types = set(
                    TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).values_list("task_type", flat=True)
                )
                with self._jobs_lock:
                    processing_types.update(j.task_type for j in self.active_jobs.values())
                busy_types = expand_task_type_aliases(processing_types)

                qs = TaskQueue.startable().select_for_update(skip_locked=True)
                if busy_types:
                    qs = qs.exclude(task_type__in=busy_types)

                job = qs.order_by("priority", "created_at", "id").first()
                if job:
                    job.status = TaskQueue.Status.PROCESSING
                    job.started_at = django_tz.now()
                    job.worker_pid = self.pid
                    job.attempts += 1
                    job.save(update_fields=["status", "started_at", "worker_pid", "attempts"])
                    return job
        except Exception:
            logger.critical("Error while claiming next job:\n%s", traceback.format_exc())
        return None

    def start_job_thread(self, job: TaskQueue) -> threading.Thread:
        cancel_event = threading.Event()
        with self._jobs_lock:
            self.active_jobs[job.pk] = job
            self.active_job_activity[job.pk] = time.monotonic()
            self.active_cancel_events[job.pk] = cancel_event
            self.current_job = job
        thread = threading.Thread(
            name=f"worker_task_{job.task_type}_{job.pk}",
            target=self._run_job_thread,
            args=(job,),
            daemon=True,
        )
        with self._jobs_lock:
            self.active_threads[job.pk] = thread
        thread.start()
        return thread

    def _run_job_thread(self, job: TaskQueue) -> None:
        try:
            _safe_close_old_connections()
            self.execute_job(job)
        finally:
            with self._jobs_lock:
                self.active_threads.pop(job.pk, None)
                self.active_job_activity.pop(job.pk, None)
                self.active_cancel_events.pop(job.pk, None)
            self._job_finished_event.set()
            if not db.connection.in_atomic_block:
                try:
                    db.connections.close_all()
                except Exception:
                    pass

    def execute_job(self, job: TaskQueue) -> None:
        logger.info("Starting execution of Task #%d (%s), args: %s", job.pk, job.task_type, job.args)
        job.refresh_from_db()
        if job.status == TaskQueue.Status.CANCELLED:
            logger.info("Task #%d was cancelled before execution started; skipping.", job.pk)
            with self._jobs_lock:
                self.active_jobs.pop(job.pk, None)
                self.active_job_activity.pop(job.pk, None)
                self.active_threads.pop(job.pk, None)
                self.active_cancel_events.pop(job.pk, None)
                self.current_job = next(iter(self.active_jobs.values()), None)
            return

        cancel_ev = None
        with self._jobs_lock:
            self.active_jobs[job.pk] = job
            self.active_job_activity[job.pk] = time.monotonic()
            if job.pk not in self.active_cancel_events:
                self.active_cancel_events[job.pk] = threading.Event()
            cancel_ev = self.active_cancel_events[job.pk]
            self.current_job = job
        start_time = time.time()
        set_task_hooks(lambda: self.touch_job_activity(job.pk), lambda: cancel_ev is not None and cancel_ev.is_set())

        try:
            current_settings = deserialize_settings_options(self.settings, job.options)
            current_settings.activity_callback = lambda: self.touch_job_activity(job.pk)
            current_settings.cancel_event = cancel_ev
            current_settings.is_cancelled = lambda: (cancel_ev is not None and cancel_ev.is_set())
            current_settings.is_worker_process = True
            current_settings.current_task_type = job.task_type
            current_settings.current_task_id = job.pk
            current_settings.current_task_priority = job.priority
            if job.user:
                current_settings.archive_user = job.user
            if job.reason:
                current_settings.archive_reason = job.reason
            if job.event_action:
                current_settings.event_action = job.event_action

            if job.task_type in ("web_crawler", "webcrawler"):
                crawler = WebCrawler(current_settings)
                crawler.start_crawling(job.args)
            elif job.task_type in ("gallery_creator", "gallery_worker", "create_galleries"):
                self.execute_gallery_creator(job, current_settings)
            elif job.task_type in ("gallery_updater", "update_galleries"):
                from core.web.gallery_updater import GalleryMetadataUpdater

                GalleryMetadataUpdater(current_settings).update(job.args)
            elif job.task_type in ("folder_crawler", "foldercrawler"):
                from core.local.foldercrawler import FolderCrawler
                folder_crawler = FolderCrawler(current_settings)
                folder_crawler.start_crawling(job.args)
            elif job.task_type in ("fileinfo_worker", "recalc_all_file_info", "calculate_missing_info", "recalc_info"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                if job.args:
                    archives = Archive.objects.filter(pk__in=job.args)
                elif job.task_type == "calculate_missing_info":
                    archives = Archive.objects.filter_by_missing_file_info()
                elif self.runs_on_all_archives(job, "recalc_all_file_info"):
                    archives = Archive.objects.all()
                else:
                    logger.warning("Task #%d (%s) has no archives and is not an 'all' task, nothing to do.", job.pk, job.task_type)
                    archives = Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                full = bool(job.options.get("full", False)) if isinstance(job.options, dict) else False
                archive_worker.recalc_all_file_info(archives, force_local=True, full=full)
            elif job.task_type in ("thumbnails_worker", "regenerate_all_thumbs", "generate_missing_thumbs"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                if job.args:
                    archives = Archive.objects.filter(pk__in=job.args)
                elif job.task_type == "generate_missing_thumbs":
                    archives = Archive.objects.filter(thumbnail="")
                elif self.runs_on_all_archives(job, "regenerate_all_thumbs"):
                    archives = Archive.objects.all()
                else:
                    logger.warning("Task #%d (%s) has no archives and is not an 'all' task, nothing to do.", job.pk, job.task_type)
                    archives = Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.regenerate_all_thumbs(archives, force_local=True)
            elif job.task_type in ("extract_worker", "extract_archives"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                extract_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                resized = bool(job.options.get("resized", False)) if isinstance(job.options, dict) else False
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.extract_archives(extract_archives_qs, resized=resized, user=job.user, force_local=True)
            elif job.task_type in ("reduce_worker", "reduce_archives", "reduce_archive"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                reduce_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.reduce_archives(reduce_archives_qs, user=job.user, force_local=True)
            elif job.task_type in ("calculate_sha1_worker", "calculate_images_sha1"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                sha1_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.calculate_images_sha1(sha1_archives_qs, user=job.user, force_local=True)
            elif job.task_type in ("convert_archive_worker", "check_and_convert_filetype", "convert_archive"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                convert_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.check_and_convert_filetype(convert_archives_qs, user=job.user, force_local=True)
            elif job.task_type in ("clone_archive_worker", "clone_plus", "clone_archive"):
                from core.workers.archive_work import ArchiveWorker
                opts = job.options if isinstance(job.options, dict) else {}
                archive_id = self.required_archive_id(job)
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.clone_archive(
                    archive_id=archive_id,
                    sha1_list=opts.get("sha1_list"),
                    run_image_tool=bool(opts.get("run_image_tool", False)),
                    bin_original=bool(opts.get("bin_original", False)),
                    user_reason=opts.get("user_reason", "") or job.reason or "",
                    user=job.user,
                    force_local=True,
                )
            elif job.task_type in ("split_archive_worker", "split_archive", "split_archives"):
                from core.workers.archive_work import ArchiveWorker
                opts = job.options if isinstance(job.options, dict) else {}
                archive_id = self.required_archive_id(job)
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.split_archive(
                    archive_id=archive_id,
                    cleaned_data=opts.get("cleaned_data"),
                    split_nested=bool(opts.get("split_nested", False)),
                    bin_original=bool(opts.get("bin_original", False)),
                    user_reason=opts.get("user_reason", "") or job.reason or "",
                    user=job.user,
                    force_local=True,
                )
            elif job.task_type in ("mark_similar_worker", "mark_similar_archives", "mark_similar"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                opts = job.options if isinstance(job.options, dict) else {}
                use_recycled = bool(opts.get("use_recycled", False))
                archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.mark_similar_archives(archives_qs, use_recycled=use_recycled, user=job.user, force_local=True)
            elif job.task_type in ("delete_archive_worker", "delete_archive", "delete_archives"):
                from core.workers.archive_work import ArchiveWorker
                opts = job.options if isinstance(job.options, dict) else {}
                archive_id = self.required_archive_id(job)
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.delete_archive(
                    archive_id=archive_id,
                    delete_zipped=bool(opts.get("delete_zipped", False)),
                    delete_archive=bool(opts.get("delete_archive", False)),
                    delete_file=bool(opts.get("delete_file", False)),
                    delete_gallery=bool(opts.get("delete_gallery", False)),
                    mark_gallery_deleted=bool(opts.get("mark_gallery_deleted", False)),
                    mark_gallery_denied=bool(opts.get("mark_gallery_denied", False)),
                    user_reason=opts.get("user_reason", "") or job.reason or "",
                    user=job.user,
                    force_local=True,
                )
            elif job.task_type in ("publish_worker", "publish_archives", "set_all_hidden_as_public"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                if job.args:
                    publish_archives_qs = Archive.objects.filter(pk__in=job.args)
                elif job.task_type == "set_all_hidden_as_public":
                    publish_archives_qs = Archive.objects.filter(gallery__hidden=True)
                else:
                    publish_archives_qs = Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.publish_archives(
                    publish_archives_qs,
                    reason=job.reason or "",
                    user=job.user,
                    force_local=True,
                )
            elif job.task_type in ("contextual_extractor_worker", "extract_contextual_info"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                ctx_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.extract_contextual_info(
                    ctx_archives_qs,
                    reason=job.reason or "",
                    user=job.user,
                    force_local=True,
                )
                if current_settings.is_cancelled():
                    raise TaskCancelledError("Contextual extraction task cancelled")
            elif job.task_type in ("match_unmatched_worker", "web_match_worker", "generate_possible_matches_internally", "generate_matches"):
                opts = job.options if isinstance(job.options, dict) else {}
                if "cutoff" in opts or "max_matches" in opts or "clear_title" in opts or "provider_filter" in opts:
                    from core.workers.archive_work import ArchiveWorker
                    from viewer.models import Archive
                    match_archives = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                    archive_worker = ArchiveWorker(current_settings, 4)
                    archive_worker.generate_matches(
                        archives=match_archives,
                        clear_title=bool(opts.get("clear_title", False)),
                        provider_filter=opts.get("provider_filter", ""),
                        cutoff=float(opts.get("cutoff", 0.4)),
                        max_matches=int(opts.get("max_matches", 10)),
                        user=job.user,
                        force_local=True,
                        auto_link=bool(opts.get("auto_link", False)),
                        auto_link_cutoff=(
                            float(opts["auto_link_cutoff"]) if opts.get("auto_link_cutoff") is not None else None
                        ),
                    )
                else:
                    from viewer.utils.matching import generate_possible_matches_for_archives
                    from viewer.models import Archive
                    legacy_archives = Archive.objects.filter(pk__in=job.args) if job.args else None
                    kwargs = opts.get("matching_kwargs", {})
                    generate_possible_matches_for_archives(legacy_archives, **kwargs)
            elif job.task_type in ("web_search_worker", "search_wanted_galleries_provider_titles"):
                from viewer.utils.matching import create_matches_wanted_galleries_from_providers
                from viewer.models import WantedGallery
                results = WantedGallery.objects.filter(pk__in=job.args) if job.args else WantedGallery.objects.eligible_to_search()
                provider = job.options.get("provider", "")
                kwargs = job.options.get("matching_kwargs", {})
                create_matches_wanted_galleries_from_providers(results, provider, **kwargs)
            elif job.task_type in ("wanted_local_search_worker", "wanted_galleries_possible_matches"):
                from viewer.utils.matching import create_matches_wanted_galleries_from_providers_internal
                from viewer.models import WantedGallery
                results = WantedGallery.objects.filter(pk__in=job.args) if job.args else WantedGallery.objects.eligible_to_search()
                kwargs = job.options.get("matching_kwargs", {})
                create_matches_wanted_galleries_from_providers_internal(results, **kwargs)
            elif job.task_type in ("match_unmatched_gallery_groups_worker",):
                from viewer.utils.matching import generate_possible_matches_for_gallery_match_groups
                from viewer.models import GalleryMatchGroup
                groups = GalleryMatchGroup.objects.filter(pk__in=job.args) if job.args else GalleryMatchGroup.objects.all()
                kwargs = job.options.get("matching_kwargs", {})
                generate_possible_matches_for_gallery_match_groups(groups, **kwargs)
            elif job.task_type in ("database_prune", "prune_database"):
                from viewer.services.retention import prune_database
                options = job.options if isinstance(job.options, dict) else {}
                entity = options.get("entity", "all")
                days_override = options.get("days")
                dry_run = bool(options.get("dry_run", False))
                chunk_size = options.get("chunk_size")
                res = prune_database(
                    settings_obj=current_settings,
                    entity=entity,
                    days_override=days_override,
                    dry_run=dry_run,
                    chunk_size=chunk_size,
                    user=job.user,
                )
                logger.info("Task #%d pruned database: %s", job.pk, res)
            elif job.task_type in ("archive_file_check",):
                from viewer.services.archive_file_check import run_file_check
                options = job.options if isinstance(job.options, dict) else {}
                check_id = options.get("check_id") or (job.args[0] if job.args else None)
                if not check_id:
                    raise ValueError("archive_file_check task has no check id")
                run_file_check(
                    int(check_id),
                    activity_cb=current_settings.activity_callback,
                    is_cancelled=current_settings.is_cancelled,
                )
            else:
                raise ValueError(f"Unknown task_type: {job.task_type!r}")

            job.refresh_from_db()
            if job.status == TaskQueue.Status.PROCESSING:
                job.status = TaskQueue.Status.COMPLETED
                job.finished_at = django_tz.now()
                job.save(update_fields=["status", "finished_at"])
                duration = time.time() - start_time
                logger.info("Finished Task #%d successfully in %.2fs", job.pk, duration)

        except TaskCancelledError as e:
            duration = time.time() - start_time
            logger.info("Task #%d stopped promptly due to cancellation (%.2fs): %s", job.pk, duration, e)
        except ProviderBannedError as e:
            self.delay_banned_job(job, e)
        except Exception as e:
            duration = time.time() - start_time
            logger.critical("Error executing Task #%d:\n%s", job.pk, traceback.format_exc())
            try:
                job.refresh_from_db()
                if job.status == TaskQueue.Status.PROCESSING:
                    job.status = TaskQueue.Status.FAILED
                    job.finished_at = django_tz.now()
                    job.error_message = str(e)
                    job.traceback = traceback.format_exc()
                    job.save(update_fields=["status", "finished_at", "error_message", "traceback"])
            except Exception:
                logger.critical("Failed to save error state for Task #%d", job.pk)
        finally:
            clear_task_hooks()
            with self._jobs_lock:
                self.active_jobs.pop(job.pk, None)
                self.active_job_activity.pop(job.pk, None)
                self.active_cancel_events.pop(job.pk, None)
                self.current_job = next(iter(self.active_jobs.values()), None)

    def delay_banned_job(self, job: TaskQueue, error: ProviderBannedError) -> None:
        """Put a task that hit a long provider ban back in the queue, to start after the ban.

        Waiting for the ban isn't the task's fault, so it doesn't use up one of its attempts.
        """
        not_before = django_tz.now() + timedelta(seconds=error.retry_after)
        logger.warning("Task #%d (%s) delayed until %s: %s", job.pk, job.task_type, not_before.isoformat(), error)
        try:
            TaskQueue.objects.filter(pk=job.pk, status=TaskQueue.Status.PROCESSING).update(
                status=TaskQueue.Status.PENDING,
                worker_pid=None,
                not_before=not_before,
                error_message=str(error),
                attempts=Greatest(F("attempts") - 1, 0),
            )
        except Exception:
            logger.critical("Failed to delay banned Task #%d", job.pk, exc_info=True)

    @staticmethod
    def runs_on_all_archives(job: TaskQueue, all_task_type: str) -> bool:
        """An archive task without archive ids only means "every archive" if it says so explicitly."""
        opts = job.options if isinstance(job.options, dict) else {}
        return bool(opts.get("all")) or job.task_type == all_task_type

    @staticmethod
    def required_archive_id(job: TaskQueue) -> int:
        if not job.args:
            raise ValueError(f"Task {job.task_type!r} requires an archive id in its arguments")
        return int(job.args[0])

    def execute_gallery_creator(self, job: TaskQueue, current_settings: Settings) -> None:
        from collections import defaultdict
        from core.base.types import GalleryData
        from viewer.models import WantedGallery

        current_settings.is_worker_process = True
        current_settings.current_task_type = "gallery_creator"
        current_settings.current_task_id = job.pk
        current_settings.dispatch_gallery_creation = False

        provider_name = job.options.get("provider", "panda") if isinstance(job.options, dict) else "panda"
        force_provider = bool(job.options.get("force_provider", False)) if isinstance(job.options, dict) else False
        raw_galleries = job.options.get("galleries_data", []) if isinstance(job.options, dict) else []
        raw_wanted = job.options.get("gallery_wanted_lists", {}) if isinstance(job.options, dict) else {}

        gallery_data_list = [GalleryData.from_dict(d) for d in raw_galleries]

        all_wanted_ids = {wg_id for id_list in raw_wanted.values() for wg_id in id_list if isinstance(wg_id, int)}
        wanted_map = {}
        if all_wanted_ids:
            wanted_map = {wg.pk: wg for wg in WantedGallery.objects.filter(pk__in=all_wanted_ids)}

        gallery_wanted_lists: dict[str, list[WantedGallery]] = defaultdict(list)
        for gid, id_list in raw_wanted.items():
            gallery_wanted_lists[gid] = [wanted_map[wg_id] for wg_id in id_list if wg_id in wanted_map]

        parsers = current_settings.provider_context.get_parsers(current_settings, filter_names=[provider_name])
        if not parsers:
            all_parsers = current_settings.provider_context.get_parsers(current_settings)
            parsers = [p for p in all_parsers if p.name == provider_name]

        if not parsers:
            raise ValueError(f"No parser found for provider '{provider_name}' in gallery_creator task #{job.pk}")

        parser = parsers[0]

        if gallery_data_list:
            valid_galleries = []
            for g in gallery_data_list:
                discarded = False
                if hasattr(parser, "discard_gallery_by_internal_checks"):
                    try:
                        discard_approved, discard_message = parser.discard_gallery_by_internal_checks(
                            gallery_id=g.gid, link=g.link or ""
                        )
                        if isinstance(discard_approved, bool) and discard_approved:
                            logger.info("gallery_creator Task #%d: %s", job.pk, discard_message)
                            discarded = True
                    except Exception as e:
                        logger.warning("Error checking discard status for gallery %s (GID: %s): %s", g.title, g.gid, e)
                if not discarded:
                    valid_galleries.append(g)

            if not valid_galleries:
                logger.info(
                    "gallery_creator Task #%d: All %d galleries already exist or were discarded, skipping execution.",
                    job.pk,
                    len(gallery_data_list),
                )
                return
            gallery_data_list = valid_galleries

        logger.info(
            "Executing gallery_creator for %d galleries (provider: %s) in Task #%d",
            len(gallery_data_list),
            provider_name,
            job.pk,
        )
        parser.pass_gallery_data_to_downloaders(
            gallery_data_list,
            gallery_wanted_lists,
            force_provider=force_provider,
        )

    def start_schedulers_if_enabled(self) -> None:
        if not self.enable_schedulers:
            return
        logger.info("Initializing schedulers in worker process...")
        self.settings.is_worker_process = True
        self.settings.workers.start_workers(self.settings, start_threads=True)

        # Clear any lingering / stale commands from previous dead worker sessions
        try:
            SchedulerState.objects.exclude(command_requested="").update(command_requested="", command_arg="")
        except Exception as e:
            logger.warning("Failed to clear stale scheduler commands: %s", e)

        if self.force_start_schedulers:
            logger.info("Force-starting all schedulers on worker startup (--all-schedulers)...")
            for s in self.settings.workers.get_active_initialized_workers():
                if not s.is_running_locally():
                    s.start_running(force_local=True)
                    logger.info("Started scheduler '%s' (timer: %.1fs)", s.thread_name, s.timer)
        elif self.schedulers_to_start:
            for s in self.settings.workers.get_active_initialized_workers():
                if s.thread_name in self.schedulers_to_start and not s.is_running_locally():
                    s.start_running(force_local=True)
                    logger.info("Started scheduler '%s' (timer: %.1fs)", s.thread_name, s.timer)

        self.sync_schedulers_state()

    def stop_schedulers_if_enabled(self) -> None:
        if not self.enable_schedulers:
            return
        logger.info("Stopping schedulers in worker process...")
        self.settings.workers.command_workers_to_stop()
        try:
            SchedulerState.objects.filter(worker_pid=self.pid).update(is_running=False)
        except Exception:
            pass

    def revert_interrupted_jobs(self, jobs: list[TaskQueue]) -> None:
        """Hand back tasks that were still running at a clean shutdown. This doesn't count as an attempt."""
        for active_job in jobs:
            try:
                _safe_close_old_connections()
                reverted = TaskQueue.objects.filter(pk=active_job.pk, status=TaskQueue.Status.PROCESSING).update(
                    status=TaskQueue.Status.PENDING,
                    worker_pid=None,
                    attempts=Greatest(F("attempts") - 1, 0),
                )
                if reverted:
                    logger.info("Reverted interrupted Task #%d back to PENDING", active_job.pk)
            except Exception:
                logger.warning("Could not revert interrupted Task #%s", active_job.pk, exc_info=True)

    def run(self) -> None:
        self.setup_signals()
        self.register_start()
        self.running = True

        if self.enable_schedulers:
            self.start_schedulers_if_enabled()

        self.start_heartbeat_thread()
        logger.info("Worker loop started. Waiting for tasks...")

        try:
            while self.running:
                self.update_heartbeat()
                self.check_scheduler_commands()
                while self.running:
                    job = self.claim_next_job()
                    if job:
                        self.start_job_thread(job)
                    else:
                        break
                if not self.running:
                    break
                _safe_close_old_connections()
                self._job_finished_event.wait(timeout=self.poll_interval)
                self._job_finished_event.clear()
        finally:
            self.stop_heartbeat_thread()
            with self._jobs_lock:
                interrupted_jobs = list(self.active_jobs.values())
            if self.current_job and all(j.pk != self.current_job.pk for j in interrupted_jobs):
                interrupted_jobs.append(self.current_job)
            self.revert_interrupted_jobs(interrupted_jobs)

            if self.enable_schedulers:
                self.stop_schedulers_if_enabled()

            self.register_stop()
            logger.info("Worker loop ended.")
            try:
                if not db.connection.in_atomic_block:
                    for conn in db.connections.all(initialized_only=False):
                        if hasattr(conn, "close_pool"):
                            try:
                                conn.close_pool()
                            except Exception:
                                pass
                    db.connections.close_all()
            except Exception:
                pass

        if getattr(self, "should_restart", False):
            self._restart_process()

    def _restart_process(self) -> None:
        logger.info("Restarting worker runner process (PID %d) via execv...", os.getpid())
        try:
            for conn in db.connections.all(initialized_only=False):
                if hasattr(conn, "close_pool"):
                    try:
                        conn.close_pool()
                    except Exception:
                        pass
            db.connections.close_all()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error("Failed to execv on restart: %s. Exiting cleanly.", e)
            sys.exit(0)
