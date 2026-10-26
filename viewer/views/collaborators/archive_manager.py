import json
import logging
import re
from collections import defaultdict
from itertools import groupby
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.contrib.contenttypes.models import ContentType
from django.core import management
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Case, When, QuerySet, Q, F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import get_random_string

from core.base.setup import Settings
from core.base.utilities import thread_exists_in_db, clamp, get_schedulers_status
from viewer.management.commands import update_provider_wanted
from viewer.utils.functions import (
    archive_manage_results_to_json,
    galleries_update_metadata,
    gallery_search_results_to_json,
)
from viewer.utils.general import clean_up_referer
from viewer.utils.actions import event_log
from workers import client
from viewer.forms import (
    GallerySearchForm,
    ArchiveSearchForm,
    WantedGalleryCreateOrEditForm,
    ArchiveCreateForm,
    ArchiveGroupSelectForm,
    GalleryCreateForm,
    ArchiveManageEntrySimpleForm,
    WantedGalleryColSearchForm,
    ArchiveManageSearchSimpleForm,
    AllGalleriesSearchForm,
    EventSearchForm,
    GallerySearchSimpleForm,
    ArchiveExtraFileSimpleForm,
    WantedGalleryCommand,
    GallerySearchFields,
    DownloadHistoryForm,
    MonitoredLinkCreateOrEditForm,
    MonitoredLinkSearchForm,
)
from viewer.models import (
    Archive,
    Gallery,
    EventLog,
    ArchiveMatches,
    Tag,
    WantedGallery,
    ArchiveGroup,
    ArchiveGroupEntry,
    GallerySubmitEntry,
    MonitoredLink,
    ArchiveRecycleEntry,
    ArchiveTag,
    UserLongLivedToken,
    DownloadEvent,
    Category,
    Provider,
    GalleryMatchGroup,
    GalleryMatchGroupEntry,
    GalleryGroupPossibleMatches, ArchiveManageEntry, FoundGallery
)
from viewer.utils.requests import double_check_auth
from viewer.utils.types import AuthenticatedHttpRequest
from viewer.views.head import (
    gallery_filter_keys,
    filter_galleries_simple,
    archive_filter_keys,
    filter_archives_simple,
    render_error,
    wanted_gallery_filter_keys,
    filter_wanted_galleries_simple,
    gallery_list_filters_keys,
)

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)



def filter_by_marks(archives: QuerySet[Archive], params: QueryDict) -> tuple[QuerySet[Archive], bool]:
    mark_filters = False

    if "and-mark-conditions" in params and params["and-mark-conditions"]:

        q_objects = Q()

        if "marked" in params and params["marked"]:
            q_objects.add(Q(manage_entries__mark_check=True, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "mark_reason" in params and params["mark_reason"]:
            q_objects.add(Q(manage_entries__mark_reason__contains=str(params["mark_reason"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "mark_comment" in params and params["mark_comment"]:
            q_objects.add(Q(manage_entries__mark_comment__contains=str(params["mark_comment"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "mark_extra" in params and params["mark_extra"]:
            q_objects.add(Q(manage_entries__mark_extra__contains=str(params["mark_extra"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "origin" in params and params["origin"]:
            q_objects.add(Q(manage_entries__origin=str(params["origin"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        priority_to = params.get("priority_to", None)
        if priority_to is not None and priority_to != "":
            q_objects.add(Q(manage_entries__mark_priority__lte=float(priority_to), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        priority_from = params.get("priority_from", None)
        if priority_from is not None and priority_from != "":
            q_objects.add(Q(manage_entries__mark_priority__gte=float(priority_from), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        elif mark_filters:
            # By default, don't filter marks with less than 1 priority (low level priorities)
            archives = archives.annotate(
                num_manage_total=Count("manage_entries", filter=Q(manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
                num_manage_below_1=Count("manage_entries", filter=Q(manage_entries__mark_priority__lt=1, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
            )
            q_objects.add(~Q(num_manage_below_1=F("num_manage_total")), Q.AND)
        archives = archives.filter(q_objects)
    else:
        if "marked" in params and params["marked"]:
            archives = archives.filter(manage_entries__mark_check=True, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "mark_reason" in params and params["mark_reason"]:
            archives = archives.filter(manage_entries__mark_reason__contains=str(params["mark_reason"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "mark_comment" in params and params["mark_comment"]:
            archives = archives.filter(manage_entries__mark_comment__contains=str(params["mark_comment"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "mark_extra" in params and params["mark_extra"]:
            archives = archives.filter(manage_entries__mark_extra__contains=str(params["mark_extra"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "origin" in params and params["origin"]:
            archives = archives.filter(manage_entries__origin=str(params["origin"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        priority_to = params.get("priority_to", None)
        if priority_to is not None and priority_to != "":
            archives = archives.filter(manage_entries__mark_priority__lte=float(priority_to), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        priority_from = params.get("priority_from", None)
        if priority_from is not None and priority_from != "":
            archives = archives.filter(manage_entries__mark_priority__gte=float(priority_from), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        elif mark_filters:
            # By default, don't filter marks with less than 1 priority (low level priorities)
            archives = archives.annotate(
                num_manage_total=Count("manage_entries", filter=Q(manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
                num_manage_below_1=Count("manage_entries", filter=Q(manage_entries__mark_priority__lt=1, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
            ).exclude(num_manage_below_1=F("num_manage_total"))

    archives = archives.distinct()

    return archives, mark_filters


def filter_by_extra_files(archives: QuerySet[Archive], params: QueryDict) -> tuple[QuerySet[Archive], bool]:
    extra_file_filters = False

    if "extra_file_name" in params and params["extra_file_name"]:
        archives = archives.filter(archivefileentry__file_name__icontains=str(params["extra_file_name"]))
        extra_file_filters = True
    if "extra_file_type" in params and params["extra_file_type"]:
        archives = archives.filter(archivefileentry__file_type__icontains=str(params["extra_file_type"]))
        extra_file_filters = True
    extra_file_size_to = params.get("extra_file_size_to", None)
    if extra_file_size_to is not None and extra_file_size_to != "":
        archives = archives.filter(archivefileentry__file_size__lte=float(extra_file_size_to))
        extra_file_filters = True
    extra_file_size_from = params.get("extra_file_size_from", None)
    if extra_file_size_from is not None and extra_file_size_from != "":
        archives = archives.filter(archivefileentry__file_size__gte=float(extra_file_size_from))
        extra_file_filters = True

    archives = archives.distinct()

    return archives, extra_file_filters


def manage_archives(request: HttpRequest) -> HttpResponse:
    authenticated, actual_user = double_check_auth(request)

    if not actual_user or not actual_user.has_perm("viewer.manage_archive"):
        return HttpResponse(status=403)

    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    user_reason = p.get("reason", "")
    json_request = get.get("json", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    try:
        size = int(get.get("size", "100"))
        if size not in (10, 20, 50, 100):
            size = 100
    except ValueError:
        size = 100

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-") and isinstance(v, str):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        if "run_for_all" in p and request.user.is_staff:
            params = {
                "sort": "create_date",
                "asc_desc": "desc",
            }

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in archive_filter_keys:
                if k not in params:
                    params[k] = ""

            if "sort_by" in get:
                params["sort_by"] = get.get("sort_by", "")

            archives: QuerySet[Archive] = filter_archives_simple(params, actual_user.is_authenticated, show_binned=True)

            archives, _ = filter_by_marks(archives, request.GET)

            if "recycled" in get and actual_user.has_perm("viewer.recycle_archive"):
                archives = archives.filter(binned=True)
            else:
                archives = archives.filter(binned=False)

            if "file_deleted" in get and actual_user.has_perm("viewer.recycle_archive"):
                archives = archives.filter(file_deleted=True)
            else:
                archives = archives.filter(file_deleted=False)

            if "downloading" in get:
                archives = archives.filter(crc32="")
        else:
            preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])
            archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        if "publish_archives" in p and actual_user.has_perm("viewer.publish_archive"):
            for archive in archives:
                message = "Publishing archive: {}, link: {}".format(archive.title, archive.get_absolute_url())
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(actual_user.username, message))
                if not json_request:
                    messages.success(request, message)
                archive.set_public(reason=user_reason)
                event_log(
                    actual_user, "PUBLISH_ARCHIVE", reason=user_reason, content_object=archive, result="published"
                )
        elif "unpublish_archives" in p and actual_user.has_perm("viewer.publish_archive"):
            for archive in archives:
                message = "Unpublishing archive: {}, link: {}".format(archive.title, archive.get_absolute_url())
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(actual_user.username, message))
                if not json_request:
                    messages.success(request, message)
                archive.set_private(reason=user_reason)
                event_log(
                    actual_user, "UNPUBLISH_ARCHIVE", reason=user_reason, content_object=archive, result="unpublished"
                )
        elif "change_archive_reason" in p and actual_user.has_perm("viewer.change_archive"):
            for archive in archives:
                archive_reason = p.get("archive_reason", "")
                message = "Changing archive reason: {}, link: {}, new reason: {}".format(archive.title, archive.get_absolute_url(), archive_reason)
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(actual_user.username, message))
                if not json_request:
                    messages.success(request, message)
                archive.set_reason(archive_reason)
                event_log(
                    actual_user, "CHANGE_ARCHIVE_REASON", reason=user_reason, content_object=archive, result="changed"
                )
        elif "release_gallery" in p:
            for archive in archives:
                if archive.gallery:
                    message = "Releasing associated gallery: {} for archive: {}, link: {}".format(
                        archive.gallery.get_absolute_url(), archive.title, archive.get_absolute_url()
                    )
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(actual_user.username, message))
                    if not json_request:
                        messages.success(request, message)
                    archive.release_gallery()
                    event_log(
                        actual_user,
                        "RELEASE_GALLERY_FROM_ARCHIVE",
                        reason=user_reason,
                        content_object=archive,
                        result="success",
                    )
        elif "gallery_to_alternative" in p:
            for archive in archives:
                if archive.gallery:

                    message = "Moving associated gallery to alternative source: {} for archive: {}, link: {}".format(
                        archive.gallery.get_absolute_url(), archive.title, archive.get_absolute_url()
                    )
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(actual_user.username, message))
                    if not json_request:
                        messages.success(request, message)

                    archive.move_gallery_to_alternative()
                    event_log(
                        actual_user,
                        "GALLERY_TO_ALTERNATIVE",
                        reason=user_reason,
                        content_object=archive,
                        result="success",
                    )
        elif "delete_archives" in p and actual_user.has_perm("viewer.delete_archive"):
            for archive in archives:
                message = "Deleting archive: {}, link: {}, with its file: {}".format(
                    archive.title, archive.get_absolute_url(), archive.zipped.path
                )
                gallery = archive.gallery
                archive_report = archive.delete_text_report()
                if archive.gallery:
                    if (
                        "mark_delete_galleries" in p
                        and p["mark_delete_galleries"]
                        and actual_user.has_perm("viewer.mark_delete_gallery")
                    ):
                        archive.gallery.mark_as_deleted()
                        message += ", gallery: {} will be marked as deleted".format(archive.gallery.get_absolute_url())
                        archive.gallery = None
                        event_log(
                            actual_user,
                            "MARK_DELETE_GALLERY",
                            data=archive_report,
                            reason=user_reason,
                            content_object=gallery,
                            result="success",
                        )
                    elif (
                        "deny_galleries" in p
                        and p["deny_galleries"]
                        and actual_user.has_perm("viewer.approve_gallery")
                    ):
                        archive.gallery.mark_as_denied()
                        message += ", gallery: {} will be marked as denied".format(archive.gallery.get_absolute_url())
                        archive.gallery = None
                        event_log(
                            actual_user,
                            "DENY_GALLERY",
                            data=archive_report,
                            reason=user_reason,
                            content_object=gallery,
                            result="success",
                        )
                    elif (
                        "delete_galleries" in p
                        and p["delete_galleries"]
                        and actual_user.has_perm("viewer.delete_gallery")
                    ):
                        old_gallery_link = archive.gallery.get_link()
                        archive.gallery.delete()
                        archive.gallery = None
                        event_log(
                            actual_user,
                            "DELETE_GALLERY",
                            reason=user_reason,
                            data=old_gallery_link,
                            result="success",
                        )
                        message += ", gallery: {} will be deleted".format(old_gallery_link)
                    else:
                        message += ", gallery: {} will be kept".format(archive.gallery.get_absolute_url())
                archive.delete_all_files()
                archive.delete()
                event_log(
                    actual_user,
                    "DELETE_ARCHIVE",
                    content_object=gallery,
                    reason=user_reason,
                    result="deleted",
                    data=archive_report,
                )
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(actual_user.username, message))
                if not json_request:
                    messages.success(request, message)
        elif "update_metadata" in p and actual_user.has_perm("viewer.update_metadata"):

            galleries_from_archives = Gallery.objects.filter(archive__in=archives).distinct()

            gallery_providers = list(galleries_from_archives.values_list("provider", flat=True).distinct())

            providers_filtered = [x for x in gallery_providers if x is not None]

            message = "Updating gallery API data for {} galleries and related archives".format(
                galleries_from_archives.count()
            )
            if "reason" in p and p["reason"] != "":
                message += ", reason: {}".format(p["reason"])
            logger.info("User {}: {}".format(actual_user.username, message))
            if not json_request:
                messages.success(request, message)

            current_settings = Settings(load_from_config=crawler_settings.config)

            current_settings.set_update_metadata_options(providers=providers_filtered)
            current_settings.archive_user = actual_user
            current_settings.event_action = "UPDATE_METADATA"

            gallery_links = [x.get_link() for x in galleries_from_archives]

            client.enqueue(
                args=gallery_links,
                task_type="web_crawler",
                override_options=current_settings,
                user=actual_user,
                reason=user_reason,
                event_action="UPDATE_METADATA",
            )

        elif "recalc_fileinfo" in p and actual_user.has_perm("viewer.recalc_fileinfo"):
            for archive in archives:
                message = "Recalculating file information for archive: {}, link: {}".format(
                    archive.title, archive.get_absolute_url()
                )
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(actual_user.username, message))
                if not json_request:
                    messages.success(request, message)
                archive.recalc_fileinfo()
                event_log(actual_user, "RECALC_ARCHIVE", reason=user_reason, content_object=archive, result="success")
        elif "add_to_group" in p and actual_user.has_perm("viewer.change_archivegroup"):

            if "archive_group" in p:
                archive_group_ids = p.getlist("archive_group")

                preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(archive_group_ids)])

                archive_groups = ArchiveGroup.objects.filter(pk__in=archive_group_ids).order_by(preserved)

                for archive in archives:
                    for archive_group in archive_groups:
                        if not ArchiveGroupEntry.objects.filter(archive=archive, archive_group=archive_group).exists():

                            archive_group_entry = ArchiveGroupEntry(archive=archive, archive_group=archive_group)
                            archive_group_entry.save()

                            message = "Adding archive: {}, link: {}, to group: {}, link {}".format(
                                archive.title,
                                archive.get_absolute_url(),
                                archive_group.title,
                                archive_group.get_absolute_url(),
                            )
                            if "reason" in p and p["reason"] != "":
                                message += ", reason: {}".format(p["reason"])
                            logger.info("User {}: {}".format(actual_user.username, message))
                            if not json_request:
                                messages.success(request, message)
                            event_log(
                                actual_user,
                                "ADD_ARCHIVE_TO_GROUP",
                                content_object=archive,
                                reason=user_reason,
                                result="added",
                            )
        elif (
            "recycle_archives" in p
            and actual_user.is_authenticated
            and actual_user.has_perm("viewer.recycle_archive")
        ):
            for archive in archives:
                if not archive.is_recycled():
                    message = "Moving to Recycle Bin archive: {}, link: {}".format(
                        archive.title, archive.get_absolute_url()
                    )
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(actual_user.username, message))
                    if not json_request:
                        messages.success(request, message)
                    r = ArchiveRecycleEntry(
                        archive=archive,
                        reason=user_reason,
                        user=actual_user,
                        origin=ArchiveRecycleEntry.ORIGIN_USER,
                    )

                    r.save()
                    archive.binned = True
                    archive.simple_save()
                    event_log(
                        actual_user,
                        "MOVE_TO_RECYCLE_BIN",
                        reason=user_reason,
                        content_object=archive,
                        result="recycled",
                    )
        elif (
            "reduce_archives" in p and actual_user.is_authenticated and actual_user.has_perm("viewer.expand_archive")
        ):
            for archive in archives:
                if archive.extracted:
                    message = "Reducing Archive: {}, link: {}".format(archive.title, archive.get_absolute_url())
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(actual_user.username, message))
                    if not json_request:
                        messages.success(request, message)

                    archive.reduce()
                    event_log(
                        actual_user, "REDUCE_ARCHIVE", reason=user_reason, content_object=archive, result="success"
                    )
        elif (
            "mark_similar" in p
            and actual_user.is_authenticated
            and actual_user.has_perm("viewer.mark_similar_archive")
        ):
            for archive in archives:
                message = "Creating similar info as marks for Archive: {}".format(archive.get_absolute_url())
                if "reason" in p and p["reason"] != "":
                    message += ", reason: {}".format(p["reason"])
                logger.info("User {}: {}".format(actual_user.username, message))
                if not json_request:
                    messages.success(request, message)

                archive.create_marks_for_similar_archives()

        if json_request:
            return HttpResponse("", content_type="application/json; charset=utf-8")

    params = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ""

    if "sort_by" in get:
        params["sort_by"] = get.get("sort_by", "")

    results: QuerySet[Archive] = filter_archives_simple(params, authenticated, show_binned=True)

    results, mark_filters = filter_by_marks(results, request.GET)

    results, extra_file_filters = filter_by_extra_files(results, request.GET)

    if "diff-filesize" in get:
        results = (
            results.exclude(gallery__filesize__isnull=True)
            .exclude(filesize=0)
            .filter(gallery__filesize__gt=0)
            .exclude(filesize=F("gallery__filesize"))
        )

    if "recycled" in get and actual_user is not None and actual_user.has_perm("viewer.recycle_archive"):
        results = results.filter(binned=True)
    else:
        results = results.filter(binned=False)

    if "file_deleted" in get and actual_user is not None and actual_user.has_perm("viewer.recycle_archive"):
        results = results.filter(file_deleted=True)
    else:
        results = results.filter(file_deleted=False)

    if "downloading" in get:
        results = results.filter(crc32="")

    if "no-custom-tags" in get:
        results = results.annotate(
            num_custom_tags=Count("tags", filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))
        ).filter(num_custom_tags=0)

    if "with-custom-tags" in get:
        results = results.annotate(
            num_custom_tags=Count("tags", filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))
        ).filter(num_custom_tags__gt=0)

    if "with-scopeless-tags" in get:
        results = results.annotate(
            num_scopeless_tags=Count(
                "tags", filter=Q(~Q(archivetag__origin=ArchiveTag.ORIGIN_USER) & Q(archivetag__tag__scope=""))
            )
        ).filter(num_scopeless_tags__gt=0)

    results = results.select_related("gallery", "user")

    if "gallery-in-queue" in get:
        results = results.annotate(
            num_galleries_in_submit=Count(
                "gallery__gallerysubmitentry"
            )
        ).filter(num_galleries_in_submit__gt=0)

    if json_request:
        results = results.prefetch_related("tags")

    if actual_user.has_perm("viewer.view_marks"):
        results = results.prefetch_related(
            Prefetch(
                "manage_entries",
                queryset=ArchiveManageEntry.objects.filter(status=ArchiveManageEntry.StatusChoices.NORMAL),
                to_attr="indexed_entries",
            )
        )

    paginator = Paginator(results, size)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    if json_request:
        response = json.dumps(
            {
                "results": archive_manage_results_to_json(request, results_page, authenticated),
                "has_previous": results_page.has_previous(),
                "has_next": results_page.has_next(),
                "num_pages": paginator.num_pages,
                "count": paginator.count,
                "number": results_page.number,
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")

    if "clear" in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={"title": title, "tags": tags})

    mark_form_simple = ArchiveManageEntrySimpleForm(request.GET)
    extra_file_form_simple = ArchiveExtraFileSimpleForm(request.GET)
    search_form = ArchiveManageSearchSimpleForm(request.GET)

    d: dict[str, Any] = {
        "results": results_page,
        "form": form,
        "mark_filters": mark_filters,
        "extra_file_filters": extra_file_filters,
        "mark_form_simple": mark_form_simple,
        "extra_file_form_simple": extra_file_form_simple,
        "search_form": search_form,
    }

    if actual_user.has_perm("viewer.change_archivegroup"):
        group_form = ArchiveGroupSelectForm()
        d.update(group_form=group_form)

    return render(request, "viewer/collaborators/manage_archives.html", d)


@permission_required("viewer.manage_archive")
def archive_manager(request: HttpRequest) -> HttpResponse:
    authenticated, actual_user = double_check_auth(request)

    if not actual_user or not actual_user.has_perm("viewer.manage_archive"):
        return HttpResponse(status=403)

    json_request = request.GET.get("json", "")

    if json_request:
        response = manage_archives(request)
        if response.status_code == 200 and response.headers.get("Content-Type", "").startswith("application/json"):
            try:
                # Decodes legacy json and injects groups list & permissions mapping
                body = response.content.decode("utf-8")
                if not body:
                    return response
                data = json.loads(body)
                if isinstance(data, dict) and "results" in data:
                    groups_list = []
                    if actual_user.has_perm("viewer.change_archivegroup"):
                        groups_list = list(ArchiveGroup.objects.all().order_by("title").values("id", "title"))

                    permissions = {
                        "publish_archive": actual_user.has_perm("viewer.publish_archive"),
                        "delete_archive": actual_user.has_perm("viewer.delete_archive"),
                        "change_archive": actual_user.has_perm("viewer.change_archive"),
                        "recycle_archive": actual_user.has_perm("viewer.recycle_archive"),
                        "expand_archive": actual_user.has_perm("viewer.expand_archive"),
                        "update_metadata": actual_user.has_perm("viewer.update_metadata"),
                        "recalc_fileinfo": actual_user.has_perm("viewer.recalc_fileinfo"),
                        "mark_similar_archive": actual_user.has_perm("viewer.mark_similar_archive"),
                        "change_archivegroup": actual_user.has_perm("viewer.change_archivegroup"),
                        "match_archive": actual_user.has_perm("viewer.match_archive"),
                        "view_marks": actual_user.has_perm("viewer.view_marks"),
                        "is_staff": actual_user.is_staff,
                    }
                    data["groups"] = groups_list
                    data["permissions"] = permissions

                new_content = json.dumps(data, ensure_ascii=False)
                return HttpResponse(new_content, content_type="application/json; charset=utf-8")
            except Exception as e:
                logger.error(f"Error parsing legacy json response in archive_manager: {e}")
                return response
        return response

    return render(request, "viewer/collaborators/archive_manager.html")



def archives_not_matched_with_gallery(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    try:
        limit = max(1, int(get.get("limit", "100")))
    except ValueError:
        limit = 100

    try:
        inline_thumbnails = bool(get.get("inline-thumbnails", ""))
    except ValueError:
        inline_thumbnails = False

    if "clear" in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-") and isinstance(v, str):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        if "create_possible_matches" in p:
            if thread_exists_in_db("match_unmatched_worker"):
                return render_error(request, "Local matching worker is already running.")
            provider = p["create_possible_matches"]
            method = p.get("matcher_type", "title")
            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            logger.info(
                "User {}: Looking for possible matches in gallery database "
                "for non-matched archives (cutoff: {}, max matches: {}) "
                'using provider filter "{}" and method filter: {}'.format(
                    request.user.username, cutoff, max_matches, provider, method
                )
            )
            client.enqueue_matching_task(
                task_type="match_unmatched_worker",
                args=archives,
                matching_kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "filters": (provider,),
                    "match_local": True,
                    "match_web": False,
                    "method_filter": method,
                },
                user=request.user,
            )
            messages.success(
                request,
                "Looking for possible matches in gallery database "
                "for non-matched archives (cutoff: {}, max matches: {}) "
                'using provider filter "{}" and method filter: {}'.format(cutoff, max_matches, provider, method),
            )
        elif "clear_possible_matches" in p:

            for archive in archives:
                archive.possible_matches.clear()

            logger.info("User {}: Clearing possible matches for archives".format(request.user.username))
            messages.success(request, "Clearing possible matches.")
        elif "auto_select_first_match" in p:
            for archive in archives:
                try:
                    possible_gallery = archive.possible_matches.first()
                    if possible_gallery:
                        archive.select_as_match(possible_gallery.id)
                        if archive.gallery:
                            logger.info(
                                "User: {}: Archive {} ({}) was matched with gallery {} ({}).".format(
                                    request.user.username,
                                    archive,
                                    reverse("viewer:archive", args=(archive.pk,)),
                                    archive.gallery,
                                    reverse("viewer:gallery", args=(archive.gallery.pk,)),
                                )
                            )
                            event_log(
                                request.user,
                                "MATCH_ARCHIVE",
                                # reason=user_reason,
                                data=reverse("viewer:gallery", args=(archive.gallery.pk,)),
                                content_object=archive,
                                result="matched",
                            )
                except ValueError:
                    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

            messages.success(request, "Matching with first possible match.")

    params: dict[str, str] = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_archives_simple(params, authenticated=request.user.is_authenticated)

    if "show-matched" not in get:
        results = results.filter(gallery__isnull=True)

    results = results.prefetch_related( # type:ignore[misc]
        Prefetch(
            "archivematches_set",
            queryset=ArchiveMatches.objects.select_related("gallery", "archive").prefetch_related(
                Prefetch("gallery__tags", queryset=Tag.objects.filter(scope__exact="artist"), to_attr="artist_tags"),
                Prefetch(
                    "gallery__tags", queryset=Tag.objects.filter(scope__exact="magazine"), to_attr="magazine_tags"
                ),
            ),
            to_attr="possible_galleries",
        ),
        "possible_galleries__gallery",
    )

    if "with-possible-matches" in get:
        results = results.annotate(n_possible_matches=Count("possible_matches")).filter(n_possible_matches__gt=0)

    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    available_providers = Gallery.objects.all().values_list("provider", flat=True).distinct()

    matchers = crawler_settings.provider_context.get_matchers(crawler_settings, force=True)

    matcher_types = list(dict.fromkeys([x[0].type for x in matchers]))

    d = {
        "results": results_page,
        "providers": available_providers,
        "matcher_types": matcher_types,
        "form": form,
        "inline_thumbnails": inline_thumbnails,
    }
    return render(request, "viewer/collaborators/unmatched_archives.html", d)



def archives_similar_by_fields(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    if "clear" in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        groups = defaultdict(list)
        group_mains = defaultdict(list)
        for k, v in p.items():
            if isinstance(v, str):
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    _, pk = k.split("-")
                    pks.append(int(pk))
                    groups[int(pk)].append(int(v))
                if k.startswith("main-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    _, pk = k.split("-")
                    group_mains[int(v)].append(int(pk))

        archives = Archive.objects.filter(id__in=pks).order_by("-create_date")

        user_reason = p.get("reason", "")

        if "delete_archives" in p and request.user.has_perm("viewer.delete_archive"):
            for archive in archives:
                message = "Removing archive: {} and deleting file: {}".format(
                    archive.get_absolute_url(), archive.zipped.path
                )
                logger.info(message)
                messages.success(request, message)

                gallery = archive.gallery
                archive_report = archive.delete_text_report()

                if gallery:
                    for group_id in groups[archive.pk]:
                        if group_id in group_mains:
                            for main_id in group_mains[group_id]:
                                try:
                                    main_archive = Archive.objects.get(pk=main_id)
                                except Archive.DoesNotExist:
                                    continue
                                main_archive.alternative_sources.add(gallery)
                                message = "Adding to archive: {}, gallery as alternative source: {}".format(
                                    main_archive.get_absolute_url(), gallery.get_absolute_url()
                                )
                                if main_archive.public and not gallery.public:
                                    gallery.set_public()
                                logger.info(message)
                                messages.success(request, message)

                if gallery and gallery.archive_set.count() <= 1 and gallery.alternative_sources.count() < 1:
                    gallery.mark_as_deleted()
                archive.delete_all_files()
                archive.delete()

                event_log(
                    request.user,
                    "DELETE_ARCHIVE",
                    reason=user_reason,
                    content_object=gallery,
                    result="deleted",
                    data=archive_report,
                )

        elif "delete_objects" in p and request.user.has_perm("viewer.delete_archive"):
            for archive in archives:
                message = "Removing archive: {}, keeping file: {}".format(
                    archive.get_absolute_url(), archive.zipped.path
                )
                logger.info(message)
                messages.success(request, message)

                gallery = archive.gallery
                archive_report = archive.delete_text_report()

                if gallery:
                    for group_id in groups[archive.pk]:
                        if group_id in group_mains:
                            for main_id in group_mains[group_id]:
                                try:
                                    main_archive = Archive.objects.get(pk=main_id)
                                except Archive.DoesNotExist:
                                    continue
                                main_archive.alternative_sources.add(gallery)
                                message = "Adding to archive: {}, gallery as alternative source: {}".format(
                                    main_archive.get_absolute_url(), gallery.get_absolute_url()
                                )
                                if main_archive.public and not gallery.public:
                                    gallery.set_public()
                                logger.info(message)
                                messages.success(request, message)

                if gallery and gallery.archive_set.count() <= 1 and gallery.alternative_sources.count() < 1:
                    gallery.mark_as_deleted()
                archive.delete_files_but_archive()
                archive.delete()

                event_log(
                    request.user,
                    "DELETE_ARCHIVE",
                    reason=user_reason,
                    content_object=gallery,
                    result="deleted",
                    data=archive_report,
                )

        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    params = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ""

    after_source_type = params["source_type"]
    after_reason = params["reason"]

    def source_lambda(x):
        return x.source_type == after_source_type

    def reason_lambda(x):
        return x.reason == after_reason

    if "clear-title" in get:

        def clear_archive_title(x: Archive):
            return (
                re.sub(r"[^A-Za-z0-9 ]+", "", re.sub(r"\s+\(.+?\)", r"", re.sub(r"\[.+?\]\s*", r"", x.best_title)))
                .lower()
                .strip()
            )

    else:

        def clear_archive_title(x: Archive):
            return x.best_title

    if "filter-after" in get:
        params["source_type"] = ""
        params["reason"] = ""

    results = filter_archives_simple(params, request.user.is_authenticated)

    if "no-custom-tags" in get:
        results = results.annotate(
            num_custom_tags=Count("tags", filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))
        ).filter(num_custom_tags=0)

    by_size_count = {}
    by_crc32 = {}
    by_title = {}
    group_count = 1
    hard_limit_groups = 400

    if "filter-fileinfo" in get:
        for k_filesize, v_filesize in groupby(
            results.exclude(filesize__isnull=True)
            .exclude(filecount__isnull=True)
            .order_by("filesize", "filecount")
            .distinct(),
            lambda x: (x.filesize, x.filecount),
        ):
            objects = list(v_filesize)
            if len(objects) > 1:
                if "filter-after" in get:
                    if after_source_type and len(list(filter(source_lambda, objects))) < 1:
                        continue
                    if after_reason and len(list(filter(reason_lambda, objects))) < 1:
                        continue

                by_size_count[group_count] = objects
                group_count += 1
                if "limit-groups" in get and len(by_size_count.keys()) >= hard_limit_groups:
                    break

    if "filter-crc32" in get:
        for k_crc32, v_crc32 in groupby(
            results.exclude(crc32__isnull=True).exclude(crc32="").order_by("crc32").distinct(), lambda x: x.crc32
        ):
            objects = list(v_crc32)
            if len(objects) > 1:
                if "filter-after" in get:
                    if after_source_type and len(list(filter(source_lambda, objects))) < 1:
                        continue
                    if after_reason and len(list(filter(reason_lambda, objects))) < 1:
                        continue
                by_crc32[group_count] = objects
                group_count += 1
                if "limit-groups" in get and len(by_crc32.keys()) >= hard_limit_groups:
                    break

    if "filter-title" in get:
        query_result = results.exclude(title__isnull=True).exclude(title="").order_by("title").distinct()
        for k_title, v_title in groupby(sorted(query_result, key=clear_archive_title), clear_archive_title):
            objects = list(v_title)
            if len(objects) > 1:
                if "filter-after" in get:
                    if after_source_type and len(list(filter(source_lambda, objects))) < 1:
                        continue
                    if after_reason and len(list(filter(reason_lambda, objects))) < 1:
                        continue
                by_title[group_count] = objects
                group_count += 1
                if "limit-groups" in get and len(by_title.keys()) >= hard_limit_groups:
                    break

    d = {"by_size_count": by_size_count, "by_crc32": by_crc32, "by_title": by_title, "form": form}
    return render(request, "viewer/archives_similar_by_fields.html", d)


