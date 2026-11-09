import datetime
import json
import logging
import os.path
import signal
import typing

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.urls import reverse
from django.db.models import Avg, Max, Min, Sum, Count, Q
from django.http import HttpResponseRedirect, HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django_db_logger.models import StatusLog

from viewer.utils.actions import event_log

from core.base.setup import Settings
from core.base.setup_utilities import get_thread_status, get_thread_status_bool
from core.base.utilities import thread_exists_in_db, get_schedulers_status, timestamp_or_zero
from core.workers.archive_work import ArchiveWorker

from viewer.models import Archive, Tag, Gallery, ArchiveMatches, WantedGallery, FoundGallery, DownloadEvent
from viewer.utils.general import clean_up_referer
from django.conf import settings
from viewer.views.head import render_error
from workers import client

MAIN_LOGGER = settings.MAIN_LOGGER
crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


@login_required
def stats_settings(request: HttpRequest) -> HttpResponse:
    """Display settings objects."""

    # Settings objects include credentials (database, providers, torrent clients, secret key),
    # so they are restricted to superusers rather than all staff.
    if not request.user.is_superuser:
        return render_error(request, "You need to be a superuser to view setting objects.")

    stats_dict = {
        "current_settings": crawler_settings,
    }

    d = {"stats": stats_dict}

    return render(request, "viewer/stats_settings.html", d)


@login_required
def stats_workers(request: HttpRequest) -> HttpResponse:
    """Display workers stats."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to view workers' status.")

    if (
        request.GET.get("json")
        or request.headers.get("x-requested-with") == "XMLHttpRequest"
        or request.headers.get("accept", "").startswith("application/json")
    ):
        from viewer.views.workers_dashboard import workers_status_api

        return workers_status_api(request)

    if crawler_settings.workers.web_queue is None:
        from core.workers.webqueue import WebQueue

        crawler_settings.workers.web_queue = WebQueue(crawler_settings)

    if crawler_settings.workers.archive_download_queue is None:
        from core.workers.archive_download_queue import ArchiveDownloadQueue

        crawler_settings.workers.archive_download_queue = ArchiveDownloadQueue(crawler_settings)

    from viewer.views.workers_dashboard import get_workers_dashboard_snapshot, get_list_limits_from_request

    dashboard_snapshot = get_workers_dashboard_snapshot(**get_list_limits_from_request(request))

    stats_dict = {
        "current_settings": crawler_settings,
        "thread_status": get_thread_status(),
        "web_queue": crawler_settings.workers.web_queue,
        "post_downloader": crawler_settings.workers.timed_downloader,
        "archive_download_queue": crawler_settings.workers.archive_download_queue,
        "download_progress_checker": DownloadEvent.objects.in_progress().select_related("archive", "gallery"),
        "schedulers": get_schedulers_status(crawler_settings.workers.get_active_initialized_workers_from_db()),
    }

    d = {
        "stats": stats_dict,
        "dashboard": dashboard_snapshot,
    }

    return render(request, "viewer/stats_workers.html", d)


@login_required
def stats_collection(request: HttpRequest) -> HttpResponse:
    """Display galleries and archives stats."""

    if not request.user.has_perm("viewer.read_private_stats"):
        return render_error(request, "You don't have the permissions to read this page.")

    # General
    stats_dict = {
        "n_archives": Archive.objects.count(),
        "n_expanded_archives": Archive.objects.filter(extracted=True).count(),
        "n_to_download_archives": Archive.objects.filter_by_dl_remote().count(),
        "n_galleries": Gallery.objects.count(),
        "archive": Archive.objects.filter(filesize__gt=0).aggregate(
            Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
        ),
        "gallery": Gallery.objects.filter(filesize__gt=0).aggregate(
            Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
        ),
        "hidden_galleries": Gallery.objects.filter(hidden=True).count(),
        "hidden_galleries_size": Gallery.objects.filter(filesize__gt=0, hidden=True).aggregate(Sum("filesize")),
        "fjord_galleries": Gallery.objects.filter(fjord=True).count(),
        "expunged_galleries": Gallery.objects.filter(expunged=True).count(),
        "disowned_galleries": Gallery.objects.filter(disowned=True).count(),
        "n_tags": Tag.objects.count(),
        "n_tag_scopes": Tag.objects.values("scope").distinct().count(),
        "n_custom_tags": Tag.objects.are_custom().count(),
        "top_10_tags": Tag.objects.annotate(num_archive=Count("archive_tags")).order_by("-num_archive")[:10],
        "top_10_parody_tags": Tag.objects.filter(scope="parody")
        .annotate(num_archive=Count("archive_tags"))
        .order_by("-num_archive")[:10],
        "top_10_artist_tags": Tag.objects.filter(scope="artist")
        .annotate(num_archive=Count("archive_tags"))
        .order_by("-num_archive")[:10],
        "wanted_galleries": {
            "total": WantedGallery.objects.all().count(),
            "found": WantedGallery.objects.filter(found=True).count(),
            "total_galleries_found": FoundGallery.objects.all().count(),
            "user_created": WantedGallery.objects.filter(book_type="user").count(),
        },
    }

    # Per provider
    gallery_counts = {
        row["provider"]: row["count"]
        for row in Gallery.objects.order_by("provider").values("provider").annotate(count=Count("id"))
    }
    archive_counts = {
        row["gallery__provider"]: row["count"]
        for row in Archive.objects.filter(gallery__provider__isnull=False)
        .order_by()
        .values("gallery__provider")
        .annotate(count=Count("id"))
    }
    wanted_counts = {
        row["wanted_providers__slug"]: row["count"]
        for row in WantedGallery.objects.filter(wanted_providers__slug__isnull=False)
        .order_by()
        .values("wanted_providers__slug")
        .annotate(count=Count("id", distinct=True))
    }

    providers_dict = {
        provider: {
            "galleries": count,
            "archives": archive_counts.get(provider, 0),
            "wanted_galleries": wanted_counts.get(provider, 0),
        }
        for provider, count in gallery_counts.items()
    }

    # Per category
    category_stats = (
        Gallery.objects.order_by("category")
        .values("category")
        .annotate(
            n_galleries=Count("id"),
            filesize__avg=Avg("filesize", filter=Q(filesize__gt=0)),
            filesize__max=Max("filesize", filter=Q(filesize__gt=0)),
            filesize__min=Min("filesize", filter=Q(filesize__gt=0)),
            filesize__sum=Sum("filesize", filter=Q(filesize__gt=0)),
            filecount__avg=Avg("filecount", filter=Q(filesize__gt=0)),
            filecount__sum=Sum("filecount", filter=Q(filesize__gt=0)),
        )
    )

    categories_dict = {
        row["category"]: {
            "n_galleries": row["n_galleries"],
            "gallery": {
                "filesize__avg": row["filesize__avg"],
                "filesize__max": row["filesize__max"],
                "filesize__min": row["filesize__min"],
                "filesize__sum": row["filesize__sum"],
                "filecount__avg": row["filecount__avg"],
                "filecount__sum": row["filecount__sum"],
            },
        }
        for row in category_stats
    }

    # Per reason
    reason_stats = (
        Archive.objects.order_by("reason")
        .values("reason")
        .annotate(
            n_archives=Count("id"),
            filesize__avg=Avg("filesize", filter=Q(filesize__gt=0)),
            filesize__max=Max("filesize", filter=Q(filesize__gt=0)),
            filesize__min=Min("filesize", filter=Q(filesize__gt=0)),
            filesize__sum=Sum("filesize", filter=Q(filesize__gt=0)),
            filecount__avg=Avg("filecount", filter=Q(filesize__gt=0)),
            filecount__sum=Sum("filecount", filter=Q(filesize__gt=0)),
        )
    )

    reasons_dict = {
        row["reason"]: {
            "n_archives": row["n_archives"],
            "archive": {
                "filesize__avg": row["filesize__avg"],
                "filesize__max": row["filesize__max"],
                "filesize__min": row["filesize__min"],
                "filesize__sum": row["filesize__sum"],
                "filecount__avg": row["filecount__avg"],
                "filecount__sum": row["filecount__sum"],
            },
        }
        for row in reason_stats
    }

    # Per language tag
    untranslated_stats = Gallery.objects.exclude(tags__scope="language").aggregate(
        n_galleries=Count("id", distinct=True),
        filesize__avg=Avg("filesize", filter=Q(filesize__gt=0)),
        filesize__max=Max("filesize", filter=Q(filesize__gt=0)),
        filesize__min=Min("filesize", filter=Q(filesize__gt=0)),
        filesize__sum=Sum("filesize", filter=Q(filesize__gt=0)),
        filecount__avg=Avg("filecount", filter=Q(filesize__gt=0)),
        filecount__sum=Sum("filecount", filter=Q(filesize__gt=0)),
    )

    languages_dict = {
        "untranslated": {
            "n_galleries": untranslated_stats["n_galleries"],
            "gallery": {
                "filesize__avg": untranslated_stats["filesize__avg"],
                "filesize__max": untranslated_stats["filesize__max"],
                "filesize__min": untranslated_stats["filesize__min"],
                "filesize__sum": untranslated_stats["filesize__sum"],
                "filecount__avg": untranslated_stats["filecount__avg"],
                "filecount__sum": untranslated_stats["filecount__sum"],
            },
        }
    }

    language_stats = (
        Tag.objects.filter(scope="language")
        .exclude(name="translated")
        .order_by()
        .values("name")
        .annotate(
            n_galleries=Count("gallery", distinct=True),
            filesize__avg=Avg("gallery__filesize", filter=Q(gallery__filesize__gt=0)),
            filesize__max=Max("gallery__filesize", filter=Q(gallery__filesize__gt=0)),
            filesize__min=Min("gallery__filesize", filter=Q(gallery__filesize__gt=0)),
            filesize__sum=Sum("gallery__filesize", filter=Q(gallery__filesize__gt=0)),
            filecount__avg=Avg("gallery__filecount", filter=Q(gallery__filesize__gt=0)),
            filecount__sum=Sum("gallery__filecount", filter=Q(gallery__filesize__gt=0)),
        )
        .order_by("-n_galleries", "name")
    )

    for row in language_stats:
        languages_dict[str(row["name"])] = {
            "n_galleries": row["n_galleries"],
            "gallery": {
                "filesize__avg": row["filesize__avg"],
                "filesize__max": row["filesize__max"],
                "filesize__min": row["filesize__min"],
                "filesize__sum": row["filesize__sum"],
                "filecount__avg": row["filecount__avg"],
                "filecount__sum": row["filecount__sum"],
            },
        }

    d = {
        "stats": stats_dict,
        "providers": providers_dict,
        "gallery_categories": categories_dict,
        "gallery_languages": languages_dict,
        "archive_reasons": reasons_dict,
    }

    return render(request, "viewer/stats_collection.html", d)


@login_required
def queue_operations(request: HttpRequest, operation: str, arguments: str = "") -> HttpResponseRedirect:

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to operate the queue.")
    if operation == "remove_by_index":
        if arguments:
            try:
                client.remove_by_index(int(arguments), task_type="web_crawler")
            except ValueError:
                return render_error(request, "Invalid index.")
        else:
            return render_error(request, "Unknown argument.")
    elif operation in ("clear", "clear_all"):
        task_type = arguments or "web_crawler"
        client.clear_pending_tasks(task_type=task_type if task_type != "all" else None)
    else:
        return render_error(request, "Unknown queue operation.")

    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))


@login_required
def tools(request: HttpRequest, tool: str = "main", tool_arg: str = "") -> HttpResponse:
    """Tools listing."""
    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to use the tools.")
    if tool not in ("main", "modify_settings", "modify_settings_editor"):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
    if tool == "transfer_missing_downloads":
        client.enqueue(args=["-tmd"], task_type="web_crawler", override_options=crawler_settings, user=request.user)
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "reconcile_downloads":
        from viewer.utils.downloads import reconcile_downloads

        stats = reconcile_downloads(settings=crawler_settings)
        rescued = stats["rescued_from_disk"] + stats["rescued_from_staging"]
        recycled = stats["recycled_failed"] + stats["recycled_false_completed"] + stats["recycled_orphaned"]
        messages.success(
            request,
            f"Reconciliation completed: {stats['total_checked']} checked, {rescued} rescued, "
            f"{recycled} recycled, {stats['requeued']} requeued, {stats['kept_in_progress']} in progress.",
        )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "retry_failed":
        client.enqueue(
            args=["--retry-failed"], task_type="web_crawler", override_options=crawler_settings, user=request.user
        )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "update_newer_than":
        p = request.POST if request.POST else request.GET
        if p and "newer_than" in p:
            newer_than_date = p["newer_than"]
            try:
                if parse_date(newer_than_date) is not None:
                    client.enqueue(
                        args=["-unt", newer_than_date],
                        task_type="web_crawler",
                        override_options=crawler_settings,
                        user=request.user,
                    )
                    messages.success(request, "Updating galleries posted after " + newer_than_date)
                else:
                    messages.error(request, "Invalid date format.", extra_tags="danger")
            except ValueError:
                messages.error(request, "Invalid date.", extra_tags="danger")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "update_missing_thumbnails":
        p = request.POST if request.POST else request.GET
        if p and "limit_number" in p:
            try:
                limit_number = int(p["limit_number"])

                provider = p.get("provider", "")
                if provider:
                    client.enqueue(
                        args=["-umt", str(limit_number), "-ip", provider],
                        task_type="web_crawler",
                        override_options=crawler_settings,
                        user=request.user,
                    )
                else:
                    client.enqueue(
                        args=["-umt", str(limit_number)],
                        task_type="web_crawler",
                        override_options=crawler_settings,
                        user=request.user,
                    )
                messages.success(
                    request, "Updating galleries missing thumbnails, limiting to older {}".format(limit_number)
                )

            except ValueError:
                messages.error(request, "Invalid limit.", extra_tags="danger")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "generate_missing_thumbs":
        if thread_exists_in_db("thumbnails_worker"):
            return render_error(request, "Thumbnails worker is already running.")

        archives_no_thumbnail = Archive.objects.filter(thumbnail="")
        archive_pks = list(archives_no_thumbnail.values_list("pk", flat=True))
        if not archive_pks:
            messages.info(request, "No archives missing thumbnails found.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        logger.info("Generating thumbs for %d archives missing thumbnails", len(archive_pks))
        if crawler_settings.workers.archive_worker:
            crawler_settings.workers.archive_worker.regenerate_all_thumbs(archives=archive_pks, user=request.user)
        else:
            archive_worker_thread = ArchiveWorker(crawler_settings, 4)
            archive_worker_thread.regenerate_all_thumbs(archives=archive_pks, user=request.user)
        messages.success(request, f"Thumbnails worker started in background for {len(archive_pks)} archives.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "calculate_missing_info":
        if thread_exists_in_db("fileinfo_worker"):
            return render_error(request, "File info worker is already running.")

        archives_missing_info = Archive.objects.filter_by_missing_file_info()
        archive_pks = list(archives_missing_info.values_list("pk", flat=True))
        if not archive_pks:
            messages.info(request, "No archives with missing file info found.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        logger.info("Calculating file info for %d archives missing info", len(archive_pks))
        if crawler_settings.workers.archive_worker:
            crawler_settings.workers.archive_worker.recalc_all_file_info(archives=archive_pks, user=request.user)
        else:
            archive_worker_thread = ArchiveWorker(crawler_settings, 4)
            archive_worker_thread.recalc_all_file_info(archives=archive_pks, user=request.user)
        messages.success(request, f"File info worker started in background for {len(archive_pks)} archives.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "recalc_all_file_info":
        if thread_exists_in_db("fileinfo_worker"):
            return render_error(request, "File info worker is already running.")

        logger.info("Recalculating file info for all archives")
        if crawler_settings.workers.archive_worker:
            crawler_settings.workers.archive_worker.recalc_all_file_info(user=request.user)
        else:
            archive_worker_thread = ArchiveWorker(crawler_settings, 4)
            archive_worker_thread.recalc_all_file_info(user=request.user)
        messages.success(request, "File info worker started in background.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "set_all_hidden_as_public":
        if thread_exists_in_db("publish_worker"):
            return render_error(request, "Publish worker is already running.")

        archives = Archive.objects.filter(gallery__hidden=True)
        archive_pks = list(archives.values_list("pk", flat=True))
        if not archive_pks:
            messages.info(request, "No archives with hidden galleries found.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        logger.info("Publishing %d archives with hidden galleries", len(archive_pks))
        if crawler_settings.workers.archive_worker:
            crawler_settings.workers.archive_worker.publish_archives(archives=archive_pks, user=request.user)
        else:
            archive_worker_thread = ArchiveWorker(crawler_settings, 4)
            archive_worker_thread.publish_archives(archives=archive_pks, user=request.user)
        messages.success(request, f"Publish worker started in background for {len(archive_pks)} archives.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "regenerate_all_thumbs":
        if thread_exists_in_db("thumbnails_worker"):
            return render_error(request, "Thumbnails worker is already running.")

        logger.info("Generating thumbs for all archives")
        if crawler_settings.workers.archive_worker:
            crawler_settings.workers.archive_worker.regenerate_all_thumbs(user=request.user)
        else:
            archive_worker_thread = ArchiveWorker(crawler_settings, 4)
            archive_worker_thread.regenerate_all_thumbs(user=request.user)
        messages.success(request, "Thumbnails worker started in background.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "generate_possible_matches_internally":
        if thread_exists_in_db("match_unmatched_worker"):
            return render_error(request, "Matching worker is already running.")
        p = request.POST if request.POST else request.GET
        provider = p.get("provider", "")
        try:
            cutoff = float(p.get("cutoff", "0.4"))
        except ValueError:
            cutoff = 0.4
        try:
            max_matches = int(p.get("max-matches", p.get("max_matches", "10")))
        except ValueError:
            max_matches = 10
        logger.info(
            "Looking for possible matches in gallery database "
            "for non-matched archives (cutoff: {}, max matches: {}) "
            'using provider filter "{}"'.format(cutoff, max_matches, provider)
        )
        client.enqueue_matching_task(
            task_type="match_unmatched_worker",
            args=None,
            matching_kwargs={
                "cutoff": cutoff,
                "max_matches": max_matches,
                "filters": (provider,),
                "match_local": True,
                "match_web": False,
            },
            user=request.user,
        )
        messages.success(
            request, "Looking for possible matches, filtering providers: {}, cutoff: {}.".format(provider, cutoff)
        )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "clear_all_archive_possible_matches":
        ArchiveMatches.objects.all().delete()
        logger.info("Clearing all possible matches for archives.")
        messages.success(request, "Clearing all possible matches for archives.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "search_wanted_galleries_provider_titles":
        if thread_exists_in_db("web_search_worker"):
            messages.error(request, "Web search worker is already running.", extra_tags="danger")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        results = WantedGallery.objects.eligible_to_search()

        if not results:
            logger.info("No wanted galleries eligible to search.")
            messages.success(request, "No wanted galleries eligible to search.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        p = request.POST if request.POST else request.GET
        provider = p.get("provider", "")

        logger.info("Searching for gallery matches in panda for wanted galleries, starting thread.")
        messages.success(request, "Searching for gallery matches in panda for wanted galleries, starting thread.")

        client.enqueue_matching_task(
            task_type="web_search_worker",
            args=results,
            provider=provider,
            user=request.user,
        )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "wanted_galleries_possible_matches":
        if thread_exists_in_db("wanted_local_search_worker"):
            return render_error(request, "Wanted local matching worker is already running.")

        non_match_wanted = WantedGallery.objects.eligible_to_search()

        if not non_match_wanted:
            logger.info("No wanted galleries eligible to search.")
            messages.success(request, "No wanted galleries eligible to search.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        logger.info("Looking for possible matches in gallery database for wanted galleries (fixed 0.4 cutoff)")

        p = request.POST if request.POST else request.GET
        provider = p.get("provider", "")

        try:
            cutoff = float(p.get("cutoff", "0.4"))
        except ValueError:
            cutoff = 0.4
        try:
            max_matches = int(p.get("max-matches", p.get("max_matches", "10")))
        except ValueError:
            max_matches = 10

        client.enqueue_matching_task(
            task_type="wanted_local_search_worker",
            args=non_match_wanted,
            matching_kwargs={"provider_filter": provider, "cutoff": cutoff, "max_matches": max_matches},
            user=request.user,
        )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "restart_viewer":
        if hasattr(signal, "SIGUSR2"):
            os.kill(os.getpid(), signal.SIGUSR2)
        else:
            return render_error(request, "This OS does not support signal SIGUSR2.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "restart_workers":
        import socket
        from datetime import timedelta
        import django.utils.timezone as django_tz
        from workers.models import WorkerProcess, SchedulerState

        heartbeat_cutoff = django_tz.now() - timedelta(seconds=60)
        active_workers = list(WorkerProcess.objects.filter(status="running", last_heartbeat__gte=heartbeat_cutoff))
        current_host = socket.gethostname()
        for wp in active_workers:
            if hasattr(signal, "SIGHUP") and (not wp.hostname or wp.hostname == current_host):
                try:
                    os.kill(wp.pid, signal.SIGHUP)
                except (OSError, ProcessLookupError, PermissionError) as e:
                    logger.warning("Could not send SIGHUP to worker PID %d: %s", wp.pid, e)

        SchedulerState.request_command("worker_runner", "restart")
        if active_workers:
            messages.success(request, f"Restart requested for {len(active_workers)} running worker process(es).")
        else:
            messages.warning(request, "Restart command queued, but no running worker processes were found.")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "modify_settings":
        return HttpResponseRedirect(reverse("viewer:tools-id", args=["modify_settings_editor"]))
    elif tool == "modify_settings_editor":
        if not request.user.is_superuser:
            return render_error(request, "You need to be a superuser to modify the settings file.")
        return render(request, "viewer/settings.html")
    elif tool == "reload_settings":
        if not request.user.is_superuser:
            return render_error(request, "You need to be a superuser to reload the config.")
        crawler_settings.load_config_from_file()
        logger.info("Reloaded settings file for Panda Backup")
        messages.success(request, "Reloaded settings file for Panda Backup")
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "start_timed_dl":
        if crawler_settings.workers.timed_downloader:
            crawler_settings.workers.timed_downloader.start_running(timer=crawler_settings.timed_downloader_cycle_timer)
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "stop_timed_dl":
        if crawler_settings.workers.timed_downloader:
            crawler_settings.workers.timed_downloader.stop_running()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "force_run_timed_dl":
        if crawler_settings.workers.timed_downloader:
            crawler_settings.workers.timed_downloader.force_run()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "start_archive_download_queue":
        if crawler_settings.workers.archive_download_queue:
            crawler_settings.workers.archive_download_queue.start_running(
                timer=crawler_settings.archive_download_queue_cycle_timer
            )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "stop_archive_download_queue":
        if crawler_settings.workers.archive_download_queue:
            crawler_settings.workers.archive_download_queue.stop_running()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "force_archive_download_queue":
        if crawler_settings.workers.archive_download_queue:
            crawler_settings.workers.archive_download_queue.force_run()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "start_download_progress_checker":
        if crawler_settings.workers.download_progress_checker:
            crawler_settings.workers.download_progress_checker.start_running(
                timer=crawler_settings.download_progress_checker_cycle_timer
            )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "stop_download_progress_checker":
        if crawler_settings.workers.download_progress_checker:
            crawler_settings.workers.download_progress_checker.stop_running()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "force_download_progress_checker":
        if crawler_settings.workers.download_progress_checker:
            crawler_settings.workers.download_progress_checker.force_run()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "start_timed_updater":
        if tool_arg:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                if provider_auto_updater.provider_name == tool_arg:
                    provider_auto_updater.start_running(
                        timer=crawler_settings.providers[provider_auto_updater.provider_name].autoupdater_timer
                    )
                    break
        else:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                provider_auto_updater.start_running(
                    timer=crawler_settings.providers[provider_auto_updater.provider_name].autoupdater_timer
                )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "stop_timed_updater":
        if tool_arg:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                if provider_auto_updater.provider_name == tool_arg:
                    provider_auto_updater.stop_running()
                    break
        else:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                provider_auto_updater.stop_running()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "force_run_timed_updater":
        if tool_arg:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                if provider_auto_updater.provider_name == tool_arg:
                    provider_auto_updater.force_run()
                    break
        else:
            for provider_auto_updater in crawler_settings.workers.timed_auto_updaters:
                provider_auto_updater.force_run()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "start_timed_auto_wanted":
        if crawler_settings.workers.timed_auto_wanted:
            crawler_settings.workers.timed_auto_wanted.start_running(timer=crawler_settings.auto_wanted.cycle_timer)
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "stop_timed_auto_wanted":
        if crawler_settings.workers.timed_auto_wanted:
            crawler_settings.workers.timed_auto_wanted.stop_running()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "force_run_timed_auto_wanted":
        if crawler_settings.workers.timed_auto_wanted:
            crawler_settings.workers.timed_auto_wanted.force_run()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "start_web_queue":
        if crawler_settings.workers.web_queue:
            crawler_settings.workers.web_queue.start_running()
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    threads_status = get_thread_status_bool()

    autoupdater_providers = (
        (provider_name, threads_status["auto_updater_" + provider_name])
        for provider_name in crawler_settings.autoupdater.providers
        if "auto_updater_" + provider_name in threads_status
    )

    d = {
        "tool": tool,
        "threads_status": threads_status,
        "autoupdater_providers": autoupdater_providers,
    }

    return render(request, "viewer/tools.html", d)


@login_required
def logs(request: HttpRequest, tool: str = "all") -> HttpResponse:
    """Logs listing."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to see logs.")

    if request.method == "POST":
        action = request.POST.get("action", "")
        delete_type = request.POST.get("delete_type", "") or request.POST.get("range", "")
        custom_date_str = request.POST.get("custom_date", "").strip()

        if action in ("delete_logs", "delete") or delete_type or custom_date_str:
            deleted_count = 0
            success = False
            msg = ""

            if delete_type == "all":
                deleted_count, _ = StatusLog.objects.all().delete()
                msg = f"Successfully deleted all server logs ({deleted_count} removed)."
                success = True
            elif delete_type in ("1_day", "1"):
                cutoff = timezone.now() - datetime.timedelta(days=1)
                deleted_count, _ = StatusLog.objects.filter(create_datetime__lt=cutoff).delete()
                msg = f"Successfully deleted {deleted_count} logs older than 1 day."
                success = True
            elif delete_type in ("7_days", "1_week", "7"):
                cutoff = timezone.now() - datetime.timedelta(days=7)
                deleted_count, _ = StatusLog.objects.filter(create_datetime__lt=cutoff).delete()
                msg = f"Successfully deleted {deleted_count} logs older than 7 days (1 week)."
                success = True
            elif delete_type in ("14_days", "2_weeks", "14"):
                cutoff = timezone.now() - datetime.timedelta(days=14)
                deleted_count, _ = StatusLog.objects.filter(create_datetime__lt=cutoff).delete()
                msg = f"Successfully deleted {deleted_count} logs older than 14 days."
                success = True
            elif delete_type in ("30_days", "1_month", "30"):
                cutoff = timezone.now() - datetime.timedelta(days=30)
                deleted_count, _ = StatusLog.objects.filter(create_datetime__lt=cutoff).delete()
                msg = f"Successfully deleted {deleted_count} logs older than 30 days."
                success = True
            elif delete_type.isdigit():
                days = int(delete_type)
                cutoff = timezone.now() - datetime.timedelta(days=days)
                deleted_count, _ = StatusLog.objects.filter(create_datetime__lt=cutoff).delete()
                msg = f"Successfully deleted {deleted_count} logs older than {days} days."
                success = True
            elif delete_type == "custom" or custom_date_str:
                dt = None
                if custom_date_str:
                    dt = parse_datetime(custom_date_str)
                    if dt is None:
                        parsed_date = parse_date(custom_date_str)
                        if parsed_date is not None:
                            dt = datetime.datetime.combine(parsed_date, datetime.time.min)
                    if dt is None:
                        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
                            try:
                                dt = datetime.datetime.strptime(custom_date_str, fmt)
                                break
                            except ValueError:
                                pass

                if dt is not None:
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt, timezone.get_current_timezone())
                    deleted_count, _ = StatusLog.objects.filter(create_datetime__lt=dt).delete()
                    date_display = (
                        dt.strftime("%Y-%m-%d %H:%M:%S") if dt.time() != datetime.time.min else dt.strftime("%Y-%m-%d")
                    )
                    msg = f"Successfully deleted {deleted_count} logs older than {date_display}."
                    success = True
                else:
                    msg = f"Invalid date specified: '{custom_date_str}'"
                    messages.error(request, msg, extra_tags="danger")
            else:
                msg = "No valid delete option specified."
                messages.error(request, msg, extra_tags="danger")

            if success:
                messages.success(request, msg)
                try:
                    event_log(
                        request.user,
                        "DELETE_STATUS_LOGS",
                        data=f"delete_type={delete_type}, count={deleted_count}",
                        result="success",
                    )
                except Exception:
                    pass

            if request.POST.get("json") or request.headers.get("x-requested-with") == "XMLHttpRequest":
                return HttpResponse(
                    json.dumps({"success": success, "deleted": deleted_count, "message": msg}),
                    content_type="application/json; charset=utf-8",
                )

            referer = clean_up_referer(request.META.get("HTTP_REFERER", ""))
            if not referer:
                referer = reverse("viewer:logs")
            return HttpResponseRedirect(referer)

    status_logs = StatusLog.objects.all()

    json_request = request.GET.get("json", "")

    log_filter_text = request.GET.get("filter", "")
    log_filter_level = request.GET.get("log-level", "")
    log_filter_logger = request.GET.get("logger-name", "")

    if log_filter_text:
        status_logs = status_logs.filter(msg__icontains=log_filter_text)

    if log_filter_logger:
        status_logs = status_logs.filter(logger_name__icontains=log_filter_logger)

    if log_filter_level:
        int_level = logging.getLevelName(log_filter_level)
        if isinstance(int_level, int):
            status_logs = status_logs.filter(level__gte=int_level)

    log_filter_context_str = request.GET.get("context", "0")
    try:
        log_filter_context = max(0, min(100, int(log_filter_context_str)))
    except ValueError:
        log_filter_context = 0

    has_filters = bool(log_filter_text or log_filter_logger or log_filter_level)

    matched_ids: set[int] = set()
    is_context_view = has_filters and log_filter_context > 0

    if is_context_view:
        # Paginate the matching entries (fewer per page because each match expands by up to 2*context)
        paginator = Paginator(status_logs, 25)
        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1

        try:
            matching_page = paginator.page(page)
        except (InvalidPage, EmptyPage):
            matching_page = paginator.page(paginator.num_pages)

        matched_list_ids = [m.id for m in matching_page]
        matched_ids = set(matched_list_ids)

        all_ids: set[int] = set(matched_ids)
        for match_id in matched_list_ids:
            before_ids = list(
                StatusLog.objects.filter(id__lt=match_id)
                .order_by("-id")
                .values_list("id", flat=True)[:log_filter_context]
            )
            after_ids = list(
                StatusLog.objects.filter(id__gt=match_id)
                .order_by("id")
                .values_list("id", flat=True)[:log_filter_context]
            )
            all_ids.update(before_ids)
            all_ids.update(after_ids)

        if all_ids:
            display_logs = list(StatusLog.objects.filter(id__in=all_ids).order_by("-id"))
        else:
            display_logs = []

        log_lines_paginated = matching_page
    else:
        paginator = Paginator(status_logs, 100)
        try:
            page = int(request.GET.get("page", "1"))
        except ValueError:
            page = 1

        try:
            log_lines_paginated = paginator.page(page)
        except (InvalidPage, EmptyPage):
            log_lines_paginated = paginator.page(paginator.num_pages)

        display_logs = list(log_lines_paginated)

    # Attach is_match flag to each log item
    for log_item in display_logs:
        setattr(log_item, "is_match", (not is_context_view) or (log_item.id in matched_ids))

    if json_request:
        response = json.dumps(
            {
                "results": [
                    {
                        "id": x.id,
                        "logger_name": x.logger_name,
                        "level": x.get_level_display(),
                        "msg": x.msg,
                        "trace": x.trace,
                        "create_datetime_timestamp": int(timestamp_or_zero(x.create_datetime)),
                        "create_datetime": x.create_datetime.strftime("%Y-%m-%d %H:%M:%S.%f"),
                        "is_match": getattr(x, "is_match", True),
                    }
                    for x in display_logs
                ],
                "has_previous": log_lines_paginated.has_previous(),
                "has_next": log_lines_paginated.has_next(),
                "num_pages": paginator.num_pages,
                "count": paginator.count,
                "number": log_lines_paginated.number,
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")

    d = {
        "log_lines": log_lines_paginated,
        "display_logs": display_logs,
        "is_context_view": is_context_view,
        "log_context": log_filter_context,
    }
    return render(request, "viewer/logs.html", d)


@login_required
def crawler(request: HttpRequest) -> HttpResponse:
    """Crawl given URLs."""

    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to crawl a link.")

    d = {}

    p = request.POST

    if p:
        if "keep_this_settings" in p:
            current_settings = crawler_settings
        else:
            current_settings = Settings(load_from_config=crawler_settings.config)
        url_set = set()

        # Update transferable options (allowed section)
        if "allowed" not in current_settings.config or not isinstance(current_settings.config["allowed"], dict):
            current_settings.config["allowed"] = {}
        if "downloaders" not in current_settings.config or not isinstance(current_settings.config["downloaders"], dict):
            current_settings.config["downloaders"] = {}

        current_settings.replace_metadata = "replace_metadata" in p
        current_settings.config["allowed"]["replace_metadata"] = current_settings.replace_metadata

        current_settings.non_current_links_as_deleted = "skip-non-current" in p
        current_settings.config["allowed"]["non_current_links_as_deleted"] = (
            current_settings.non_current_links_as_deleted
        )
        if "general" in current_settings.config and isinstance(current_settings.config["general"], dict):
            if "non_current_links_as_deleted" in current_settings.config["general"]:
                current_settings.config["general"]["non_current_links_as_deleted"] = (
                    current_settings.non_current_links_as_deleted
                )

        for k, v in p.items():
            if isinstance(v, str):
                if k.startswith("downloaders-"):
                    dl = k.split("-", 1)[1]
                    try:
                        dl_priority = int(v)
                        current_settings.config["downloaders"][dl] = dl_priority
                        current_settings.downloaders[dl] = dl_priority
                    except (ValueError, TypeError):
                        pass
                elif k == "urls":
                    url_list = v.split("\n")
                    for item in url_list:
                        cleaned = item.rstrip("\r").strip()
                        if cleaned:
                            url_set.add(cleaned)
        urls = list(url_set)

        if "keep_this_settings" in p:
            current_settings.write()
            current_settings.load_config_from_file()
            messages.info(
                request,
                "Settings saved to settings.yaml (note: these settings will only be applied to background workers when restarted).",
            )
            job_settings = Settings(load_from_config=crawler_settings.config)
        else:
            job_settings = current_settings

        if "reason" in p and p["reason"] != "":
            reason = p["reason"]
            # Force limit string length (reason field max_length)
            job_settings.archive_reason = reason[:200]
            job_settings.gallery_reason = reason[:200]

        if "gallery_only" in p:
            job_settings.allow_type_downloaders_only("info")

        job_settings.non_current_links_as_deleted = "skip-non-current" in p
        job_settings.replace_metadata = "replace_metadata" in p

        if "ignore_global_filters" in p or "ignore-global-filters" in p:
            job_settings.ignore_global_filters = True
            urls.append("--ignore-global-filters")

        if "wanted_only" in p:
            urls.insert(0, "--wanted-only")

        # Parse task queue priority (1 to 10, default: 10) with backward compatibility for run_separate
        priority = 10
        if "priority" in p:
            try:
                priority = int(p["priority"])
                if not 1 <= priority <= 10:
                    priority = 10
            except (ValueError, TypeError):
                priority = 10
        elif "run_separate" in p:
            priority = 5

        client.enqueue(
            args=urls,
            task_type="web_crawler",
            override_options=job_settings,
            user=request.user,
            priority=priority,
        )
        messages.success(request, "Starting Crawler, check the logs for a report.")
        # Not really optimal when there's many commands being queued
        # for command in url_list:
        #     messages.success(request, command)
        return HttpResponseRedirect(reverse("viewer:main-page"))

    d.update(
        {
            "settings": crawler_settings,
            "downloaders": crawler_settings.provider_context.get_downloaders_name_priority(crawler_settings),
        }
    )

    return render(request, "viewer/crawler.html", d)


@login_required
def foldercrawler(request: HttpRequest) -> HttpResponse:
    """Folder crawler."""
    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to use the tools.")

    d: dict[str, typing.Any] = {"media_root": os.path.realpath(crawler_settings.MEDIA_ROOT)}

    p = request.POST

    if p:
        if "keep_this_settings" in p:
            current_settings = crawler_settings
        else:
            current_settings = Settings(load_from_config=crawler_settings.config)
        commands = set()
        # create dictionary of properties for each command
        for k, v in p.items():
            if isinstance(v, str):
                if k.startswith("matchers"):
                    k, matcher = k.split("-")
                    current_settings.config["matchers"][matcher] = v
                    current_settings.matchers[matcher] = int(v)
                elif k == "commands":
                    command_list = v.split("\n")
                    for item in command_list:
                        commands.add(item.rstrip("\r"))
                elif k == "internal_matches":
                    current_settings.internal_matches_for_non_matches = True

        if "reason" in p and p["reason"] != "":
            reason = p["reason"]
            # Force limit string length (reason field max_length)
            current_settings.archive_reason = reason[:200]
            current_settings.gallery_reason = reason[:200]

        if "source" in p and p["source"] != "":
            source = p["source"]
            # Force limit string length (reason field max_length)
            current_settings.archive_source = source[:50]

        if "keep_this_settings" in p:
            current_settings.write()
            current_settings.load_config_from_file()
        client.enqueue(
            args=list(commands),
            task_type="folder_crawler",
            override_options=current_settings,
            user=request.user,
        )
        messages.success(request, "Starting Folder Crawler, check the logs for a report.")
        # Not really optimal when there's many commands being queued
        # for command in commands:
        #     messages.success(request, command)
        return HttpResponseRedirect(reverse("viewer:main-page"))

    d.update(
        {
            "settings": crawler_settings,
            "matchers": crawler_settings.provider_context.get_matchers_name_priority(crawler_settings),
        }
    )

    return render(request, "viewer/foldercrawler.html", d)


@login_required
def database_maintenance(request: HttpRequest) -> HttpResponse:
    """Database metrics, retention settings, and maintenance trigger dashboard."""
    if not request.user.is_staff:
        return render_error(request, "You need to be an admin to view database maintenance.")

    from viewer.services.retention import (
        get_database_info,
        get_retention_counts,
        prune_database,
    )
    from workers.models import SchedulerState, TaskQueue

    if request.method == "POST":
        action = request.POST.get("action", "prune_background")
        entity = request.POST.get("entity", "all")
        days_str = request.POST.get("days", "").strip()
        dry_run = request.POST.get("dry_run") in ("1", "true", "True", True)
        days = int(days_str) if days_str.isdigit() else None

        if action == "scheduler_control":
            cmd = request.POST.get("command", "")
            timer_str = request.POST.get("timer", "")
            if cmd in ("start", "stop", "force_run", "reload"):
                SchedulerState.request_command("timed_database_pruner", cmd, timer_str)
                messages.success(request, f"Sent '{cmd}' command to Database Pruner scheduler.")
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("json"):
                    return JsonResponse({"success": True, "message": f"Command '{cmd}' dispatched."})
            else:
                messages.error(request, f"Unknown scheduler command '{cmd}'.", extra_tags="danger")

        elif action == "prune_immediate":
            try:
                res = prune_database(
                    settings_obj=crawler_settings,
                    entity=entity,
                    days_override=days,
                    dry_run=dry_run,
                    user=request.user,
                )
                action_text = "Dry run: would delete" if dry_run else "Successfully deleted"
                msg = f"{action_text} {res['total_deleted']} rows across targeted tables in {res['duration_seconds']}s."
                messages.success(request, msg)
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("json"):
                    return JsonResponse({"success": True, "message": msg, "results": res})
            except Exception as e:
                err_msg = f"Failed to execute database pruning: {e}"
                logger.error(err_msg, exc_info=True)
                messages.error(request, err_msg, extra_tags="danger")
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("json"):
                    return JsonResponse({"success": False, "error": err_msg}, status=500)

        else:
            # Default: prune_background (enqueue to background worker)
            try:
                task = client.enqueue(
                    args=[],
                    task_type="database_prune",
                    override_options={
                        "entity": entity,
                        "days": days,
                        "dry_run": dry_run,
                    },
                    user=request.user,
                    reason=f"Database maintenance prune ({entity})",
                    event_action="DATABASE_PRUNE",
                )
                mode_str = " (dry-run)" if dry_run else ""
                msg = (
                    f"Enqueued background task #{task.pk} to prune {entity}{mode_str}. Worker will process it shortly."
                )
                messages.success(request, msg)
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("json"):
                    return JsonResponse({"success": True, "task_id": task.pk, "message": msg})
            except Exception as e:
                err_msg = f"Failed to enqueue database pruning task: {e}"
                logger.error(err_msg, exc_info=True)
                messages.error(request, err_msg, extra_tags="danger")
                if request.headers.get("x-requested-with") == "XMLHttpRequest" or request.POST.get("json"):
                    return JsonResponse({"success": False, "error": err_msg}, status=500)

        return HttpResponseRedirect(reverse("viewer:database-maintenance"))

    db_info = get_database_info()
    entity_metrics = get_retention_counts(crawler_settings)
    retention_cfg = getattr(crawler_settings, "retention", None)
    scheduler_state = SchedulerState.objects.filter(name="timed_database_pruner").first()
    recent_tasks = TaskQueue.objects.filter(task_type__in=["database_prune", "prune_database"]).order_by("-created_at")[
        :10
    ]

    total_prunable = sum(m["prunable_count"] for m in entity_metrics)
    total_rows = sum(m["total_count"] for m in entity_metrics)

    d = {
        "db_info": db_info,
        "entities": entity_metrics,
        "retention": retention_cfg,
        "scheduler_state": scheduler_state,
        "recent_tasks": recent_tasks,
        "total_prunable": total_prunable,
        "total_rows": total_rows,
    }

    return render(request, "viewer/database_maintenance.html", d)
