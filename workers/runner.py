import datetime
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

from core.base.setup import Settings
from core.web.crawler import WebCrawler
from workers.client import expand_task_type_aliases
from workers.models import TaskQueue, WorkerProcess, SchedulerState
from workers.serialization import deserialize_settings_options

logger = logging.getLogger(__name__)


def _safe_close_old_connections() -> None:
    if not db.connection.in_atomic_block:
        db.close_old_connections()


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
        self._jobs_lock = threading.Lock()
        self._job_finished_event = threading.Event()
        self.settings.is_worker_process = True
        if self.settings.workers.web_queue is None:
            from core.workers.webqueue import WebQueue
            self.settings.workers.web_queue = WebQueue(self.settings)
        self.heartbeat_thread: threading.Thread | None = None
        self.heartbeat_stop_event = threading.Event()

    def get_task_stall_timeout(self) -> float:
        """Return stall/inactivity timeout in seconds for TaskQueue jobs (0 disables)."""
        val = getattr(self.settings, "worker_task_stall_timeout", 1800.0)
        try:
            return max(0.0, float(val if val is not None else 1800.0))
        except (TypeError, ValueError):
            return 1800.0

    def touch_job_activity(self, job_pk: int | None = None) -> None:
        """Update activity timestamp for a running TaskQueue job (or all active jobs if job_pk is None)."""
        now_mono = time.monotonic()
        with self._jobs_lock:
            if job_pk is not None:
                if job_pk in self.active_jobs:
                    self.active_job_activity[job_pk] = now_mono
            else:
                for pk in self.active_jobs:
                    self.active_job_activity[pk] = now_mono

    def check_stuck_jobs(self) -> list[int]:
        """Check active TaskQueue jobs for inactivity timeouts while respecting active slow downloads."""
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
                for candidate_path in (ev.download_id, f"{ev.download_id}.part"):
                    if candidate_path and os.path.exists(candidate_path):
                        try:
                            mtime = os.path.getmtime(candidate_path)
                            if (now_wall - mtime) < min(stall_timeout, 60.0):
                                with self._jobs_lock:
                                    for pk, j in self.active_jobs.items():
                                        if j.task_type in ("web_crawler", "webcrawler"):
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

    def _handle_shutdown(self, signum: int, frame: object) -> None:
        logger.info("Received shutdown signal (%d). Shutting down worker...", signum)
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
                    s.thread_name in ("timed_downloader", "post_downloader", "archive_download_queue")
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
        """Check and process pending start/stop/force_run commands from SchedulerState."""
        if not self.enable_schedulers:
            return
        try:
            pending_commands = list(SchedulerState.objects.exclude(command_requested=""))
            if not pending_commands:
                return

            schedulers_by_name = {
                s.thread_name: s for s in self.settings.workers.get_active_initialized_workers()
            }
            if "post_downloader" in schedulers_by_name:
                schedulers_by_name["timed_downloader"] = schedulers_by_name["post_downloader"]

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

    def register_start(self) -> None:
        _safe_close_old_connections()
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
        stale_qs.update(status=TaskQueue.Status.PENDING, worker_pid=None)
        try:
            from django.db.models import F
            from django.db.models.functions import Greatest
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

    def claim_next_job(self) -> TaskQueue | None:
        try:
            with transaction.atomic():
                processing_types = set(
                    TaskQueue.objects.filter(status=TaskQueue.Status.PROCESSING).values_list("task_type", flat=True)
                )
                with self._jobs_lock:
                    processing_types.update(j.task_type for j in self.active_jobs.values())
                busy_types = expand_task_type_aliases(processing_types)

                qs = TaskQueue.objects.select_for_update(skip_locked=True).filter(status=TaskQueue.Status.PENDING)
                if busy_types:
                    qs = qs.exclude(task_type__in=busy_types)

                job = qs.order_by("priority", "created_at", "id").first()
                if job:
                    job.status = TaskQueue.Status.PROCESSING
                    job.started_at = django_tz.now()
                    job.worker_pid = self.pid
                    job.save(update_fields=["status", "started_at", "worker_pid"])
                    return job
        except Exception:
            logger.critical("Error while claiming next job:\n%s", traceback.format_exc())
        return None

    def start_job_thread(self, job: TaskQueue) -> threading.Thread:
        with self._jobs_lock:
            self.active_jobs[job.pk] = job
            self.active_job_activity[job.pk] = time.monotonic()
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
            self._job_finished_event.set()
            db.connections.close_all()

    def execute_job(self, job: TaskQueue) -> None:
        logger.info("Starting execution of Task #%d (%s), args: %s", job.pk, job.task_type, job.args)
        with self._jobs_lock:
            self.active_jobs[job.pk] = job
            self.active_job_activity[job.pk] = time.monotonic()
            self.current_job = job
        start_time = time.time()

        try:
            current_settings = deserialize_settings_options(self.settings, job.options)
            current_settings.activity_callback = lambda: self.touch_job_activity(job.pk)
            if job.user:
                current_settings.archive_user = job.user
            if job.reason:
                current_settings.archive_reason = job.reason
            if job.event_action:
                current_settings.event_action = job.event_action

            if job.task_type in ("web_crawler", "webcrawler"):
                crawler = WebCrawler(current_settings)
                crawler.start_crawling(job.args)
            elif job.task_type in ("folder_crawler", "foldercrawler"):
                from core.local.foldercrawler import FolderCrawler
                folder_crawler = FolderCrawler(current_settings)
                folder_crawler.start_crawling(job.args)
            elif job.task_type in ("fileinfo_worker", "recalc_all_file_info"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                archives = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.all()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.recalc_all_file_info(archives, force_local=True)
            elif job.task_type in ("thumbnails_worker", "regenerate_all_thumbs"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                archives = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.all()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.regenerate_all_thumbs(archives, force_local=True)
            elif job.task_type in ("extract_worker", "extract_archives"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                extract_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                resized = bool(job.options.get("resized", False)) if isinstance(job.options, dict) else False
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.extract_archives(extract_archives_qs, resized=resized, user=job.user, force_local=True)
            elif job.task_type in ("publish_worker", "publish_archives"):
                from core.workers.archive_work import ArchiveWorker
                from viewer.models import Archive
                publish_archives_qs = Archive.objects.filter(pk__in=job.args) if job.args else Archive.objects.none()
                archive_worker = ArchiveWorker(current_settings, 4)
                archive_worker.publish_archives(
                    publish_archives_qs,
                    reason=job.reason or "",
                    user=job.user,
                    force_local=True,
                )
            elif job.task_type in ("match_unmatched_worker", "web_match_worker", "generate_possible_matches_internally"):
                from viewer.utils.matching import generate_possible_matches_for_archives
                from viewer.models import Archive
                match_archives = Archive.objects.filter(pk__in=job.args) if job.args else None
                kwargs = job.options.get("matching_kwargs", {})
                generate_possible_matches_for_archives(match_archives, **kwargs)
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
            else:
                logger.warning("Unknown task_type: %s. Falling back to WebCrawler.", job.task_type)
                crawler = WebCrawler(current_settings)
                crawler.start_crawling(job.args)

            job.refresh_from_db()
            if job.status == TaskQueue.Status.PROCESSING:
                job.status = TaskQueue.Status.COMPLETED
                job.finished_at = django_tz.now()
                job.save(update_fields=["status", "finished_at"])
                duration = time.time() - start_time
                logger.info("Finished Task #%d successfully in %.2fs", job.pk, duration)

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
            with self._jobs_lock:
                self.active_jobs.pop(job.pk, None)
                self.active_job_activity.pop(job.pk, None)
                self.current_job = next(iter(self.active_jobs.values()), None)

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
            for active_job in interrupted_jobs:
                try:
                    _safe_close_old_connections()
                    active_job.refresh_from_db()
                    if active_job.status == TaskQueue.Status.PROCESSING:
                        active_job.status = TaskQueue.Status.PENDING
                        active_job.save(update_fields=["status"])
                        logger.info("Reverted interrupted Task #%d back to PENDING", active_job.pk)
                except Exception:
                    pass

            if self.enable_schedulers:
                self.stop_schedulers_if_enabled()

            self.register_stop()
            logger.info("Worker loop ended.")
