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



@permission_required("viewer.manage_gallery")
def manage_galleries(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

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

        if "reason" in p and p["reason"] != "":
            reason = p["reason"]
        else:
            reason = ""

        if "run_for_all" in p and request.user.is_staff:
            params = {
                "sort": "create_date",
                "asc_desc": "desc",
            }

            for k, v in get.items():
                if isinstance(v, str):
                    params[k] = v

            for k in gallery_filter_keys:
                if k not in params:
                    params[k] = ""

            if "sort_by" in get:
                params["sort_by"] = get.get("sort_by", "")

            results_gallery = filter_galleries_simple(params)
            if "is-not-in-groups" in get:
                results_gallery = results_gallery.annotate(gallery_groups=Count("gallery_group")).filter(gallery_groups=0)
            if "is-in-single-group" in get:
                results_gallery = results_gallery.annotate(galleries_in_group=Count("gallery_group__galleries")).filter(galleries_in_group=1)
        else:
            preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])
            results_gallery = Gallery.objects.filter(id__in=pks).order_by(preserved)

        if "delete_galleries" in p and request.user.has_perm("viewer.mark_delete_gallery"):
            for gallery in results_gallery:
                message = "Marking deleted for gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.mark_as_deleted()
                event_log(
                    request.user,
                    "MARK_DELETE_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
        elif "real_delete_galleries" in p and request.user.has_perm("viewer.delete_gallery"):
            for gallery in results_gallery:
                message = "Deleting gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                old_gallery_link = gallery.get_link()
                logger.info(message)
                messages.success(request, message)
                gallery.delete()
                event_log(
                    request.user,
                    "DELETE_GALLERY",
                    reason=reason,
                    data=old_gallery_link,
                    result="success",
                )
        elif "publish_galleries" in p and request.user.has_perm("viewer.publish_gallery"):
            for gallery in results_gallery:
                message = "Publishing gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.set_public()
                event_log(
                    request.user,
                    "PUBLISH_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
        elif "private_galleries" in p and request.user.has_perm("viewer.private_gallery"):
            for gallery in results_gallery:
                message = "Making private gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.set_private()
                event_log(
                    request.user,
                    "UNPUBLISH_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
        elif "download_galleries" in p and request.user.has_perm("viewer.download_gallery"):
            for gallery in results_gallery:
                message = "Queueing gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)

                # Force replace_metadata when queueing from this list, since it's mostly used to download non used.
                current_settings = Settings(load_from_config=crawler_settings.config)

                current_settings.replace_metadata = True
                current_settings.retry_failed = True

                if reason:
                    # Force limit string length (reason field max_length)
                    current_settings.archive_reason = reason[:200]
                    current_settings.archive_details = gallery.reason or ""
                    current_settings.gallery_reason = reason[:200]
                elif gallery.reason:
                    current_settings.archive_reason = gallery.reason

                current_settings.archive_user = request.user
                current_settings.event_action = "DOWNLOAD"

                client.enqueue(
                    args=[gallery.get_link()],
                    task_type="web_crawler",
                    override_options=current_settings,
                    user=request.user,
                    reason=reason,
                    event_action="DOWNLOAD",
                )
        elif "recall_api" in p and request.user.has_perm("viewer.update_metadata"):
            message = "Recalling API for {} galleries".format(results_gallery.count())
            logger.info(message)
            messages.success(request, message)

            gallery_links = [x.get_link() for x in results_gallery]
            gallery_providers = list(results_gallery.values_list("provider", flat=True).distinct())

            galleries_update_metadata(gallery_links, gallery_providers, request.user, reason, crawler_settings)
        elif "create_gallery_match_groups" in p and request.user.has_perm("viewer.add_gallerymatchgroup"):
            gallery_match_group_pks = []
            for gallery in results_gallery:
                gallery_match_group, created = GalleryMatchGroup.objects.get_or_create(galleries__in=(gallery,))
                if created:
                    gallery_match_group.galleries.add(gallery)
                    gallery_match_group.save()
                    message = "Created gallery match group: {}, galleries: {}".format(gallery_match_group.pk, [x.get_absolute_url() for x in gallery_match_group.galleries.all()])
                    logger.info(message)
                    messages.success(request, message)
                    event_log(
                        request.user, "CREATE_GALLERY_MATCH_GROUP",
                        reason=reason, content_object=gallery_match_group, result="created"
                    )
                gallery_match_group_pks.append(gallery_match_group.pk)

            gallery_match_groups = GalleryMatchGroup.objects.filter(pk__in=gallery_match_group_pks)

            if thread_exists_in_db("match_unmatched_gallery_groups_worker"):
                return render_error(request, "Gallery Groups matching worker is already running.")
            provider = p["create_gallery_match_groups"]
            methods = p.getlist("matcher_type", ["title", "comment"])
            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            client.enqueue_matching_task(
                task_type="match_unmatched_gallery_groups_worker",
                args=gallery_match_groups,
                matching_kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "providers": (provider,),
                    "string_attributes_to_match": methods,
                },
                user=request.user,
            )
            messages.success(
                request,
                "Looking for possible matches in gallery database "
                "for Gallery Groups (cutoff: {}, max matches: {}) "
                'using provider filter "{}" and methods filter: {}'.format(cutoff, max_matches, provider, methods),
            )
        elif "delete_gallery_match_groups" in p and request.user.has_perm("viewer.delete_gallerymatchgroup"):
            delete_result = GalleryMatchGroup.objects.filter(galleries__in=results_gallery).delete()
            logger.info("User {}: Deleting {} Gallery Match Groups for galleries".format(request.user.username, delete_result[0]))
            messages.success(request, "Clearing possible matches.")
        elif "auto_select_first_match" in p:
            for gallery in results_gallery:
                try:
                    gallery_match_group_first = GalleryMatchGroup.objects.filter(galleries__in=(gallery,)).first()
                    if gallery_match_group_first:
                        gallery_match = gallery_match_group_first.possible_matches.first()
                        if gallery_match:
                            gallery_match_group_first.add_gallery(gallery_match)
                            gallery_match_group_first.save()
                            GalleryGroupPossibleMatches.objects.filter(
                                gallery_match_group=gallery_match_group_first
                            ).delete()

                            logger.info(
                                "User: {}: Gallery {} ({}) was matched in a group with gallery {} ({}).".format(
                                    request.user.username,
                                    gallery,
                                    reverse("viewer:gallery", args=(gallery.pk,)),
                                    gallery_match,
                                    reverse("viewer:gallery", args=(gallery_match.pk,)),
                                )
                            )
                            event_log(
                                request.user,
                                "ADD_GALLERY_TO_MATCH_GROUP",
                                # reason=user_reason,
                                data=reverse("viewer:gallery", args=(gallery_match.pk,)),
                                content_object=gallery_match,
                                result="matched",
                            )
                except ValueError:
                    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

            messages.success(request, "Adding the first possible match to the Match Group.")
        if json_request:
            return HttpResponse("", content_type="application/json; charset=utf-8")

    params = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    if "sort_by" in get:
        params["sort_by"] = get.get("sort_by", "")

    results = filter_galleries_simple(params).prefetch_related("foundgallery_set", "magazine_chapters")

    if "is-not-in-groups" in get:
        results = results.annotate(gallery_groups=Count("gallery_group")).filter(gallery_groups=0)
    if "is-in-single-group" in get:
        results = results.annotate(galleries_in_group=Count("gallery_group__galleries")).filter(galleries_in_group=1)

    if json_request:
        results = results.prefetch_related("tags")

    paginator = Paginator(results, size)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    if json_request:
        response = json.dumps(
            {
                "results": gallery_search_results_to_json(request, results_page),
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
        form = AllGalleriesSearchForm()
    else:
        form = AllGalleriesSearchForm(initial={"title": title, "tags": tags})

    search_form = GallerySearchSimpleForm(request.GET)

    available_providers = Gallery.objects.all().values_list("provider", flat=True).distinct()

    matcher_types = ("title", "comment")

    d = {
        "results": results_page,
        "form": form,
        "search_form": search_form,
        "providers": available_providers,
        "matcher_types": matcher_types,
    }

    return render(request, "viewer/collaborators/manage_galleries.html", d)


@permission_required("viewer.add_gallerymatchgroup")
def gallery_match_groups_possibles(request: HttpRequest) -> HttpResponse:
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
        form = GallerySearchForm()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-") and isinstance(v, str):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        galleries = Gallery.objects.filter(id__in=pks).order_by(preserved)
        if "create_gallery_match_groups" in p and request.user.has_perm("viewer.add_gallerymatchgroup"):
            gallery_match_group_pks = []
            for gallery in galleries:
                gallery_match_group, created = GalleryMatchGroup.objects.get_or_create(galleries__in=(gallery,))
                if created:
                    gallery_match_group.galleries.add(gallery)
                    gallery_match_group.save()
                    message = "Created gallery match group: {}, galleries: {}".format(gallery_match_group.pk, [x.get_absolute_url() for x in gallery_match_group.galleries.all()])
                    logger.info(message)
                    messages.success(request, message)
                    event_log(
                        request.user, "CREATE_GALLERY_MATCH_GROUP",
                        # reason=reason,
                        content_object=gallery_match_group, result="created"
                    )
                gallery_match_group_pks.append(gallery_match_group.pk)

            gallery_match_groups = GalleryMatchGroup.objects.filter(pk__in=gallery_match_group_pks)

            if thread_exists_in_db("match_unmatched_gallery_groups_worker"):
                return render_error(request, "Gallery Groups matching worker is already running.")
            provider = p["create_gallery_match_groups"]
            methods = p.getlist("matcher_type", ["title", "comment"])
            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            client.enqueue_matching_task(
                task_type="match_unmatched_gallery_groups_worker",
                args=gallery_match_groups,
                matching_kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "providers": (provider,),
                    "string_attributes_to_match": methods,
                },
                user=request.user,
            )
            messages.success(
                request,
                "Looking for possible matches in gallery database "
                "for Gallery Groups (cutoff: {}, max matches: {}) "
                'using provider filter "{}" and methods filter: {}'.format(cutoff, max_matches, provider, methods),
            )
        elif "delete_gallery_match_groups" in p and request.user.has_perm("viewer.delete_gallerymatchgroup"):
            delete_result = GalleryMatchGroup.objects.filter(galleries__in=galleries).delete()
            logger.info("User {}: Deleting {} Gallery Match Groups for galleries".format(request.user.username, delete_result[0]))
            messages.success(request, "Clearing possible matches.")
        elif "auto_select_first_match" in p:
            for gallery in galleries:
                try:
                    gallery_match_group_first = GalleryMatchGroup.objects.filter(galleries__in=(gallery,)).first()
                    if gallery_match_group_first:
                        gallery_match = gallery_match_group_first.possible_matches.first()
                        if gallery_match:
                            gallery_match_group_first.add_gallery(gallery_match)
                            gallery_match_group_first.save()
                            GalleryGroupPossibleMatches.objects.filter(
                                gallery_match_group=gallery_match_group_first
                            ).delete()

                            logger.info(
                                "User: {}: Gallery {} ({}) was matched in a group with gallery {} ({}).".format(
                                    request.user.username,
                                    gallery,
                                    reverse("viewer:gallery", args=(gallery.pk,)),
                                    gallery_match,
                                    reverse("viewer:gallery", args=(gallery_match.pk,)),
                                )
                            )
                            event_log(
                                request.user,
                                "ADD_GALLERY_TO_MATCH_GROUP",
                                # reason=user_reason,
                                data=reverse("viewer:gallery", args=(gallery_match.pk,)),
                                content_object=gallery_match,
                                result="matched",
                            )
                except ValueError:
                    return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

            messages.success(request, "Adding the first possible match to the Match Group.")

    params: dict[str, str] = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    if "is-not-in-groups" in get:
        results = results.annotate(gallery_groups=Count("gallery_group")).filter(gallery_groups=0)
    if "is-in-single-group" in get:
        results = results.annotate(galleries_in_group=Count("gallery_group__galleries")).filter(galleries_in_group=1)

    results = results.prefetch_related( # type:ignore[misc]
        Prefetch(
            "gallery_group__gallerygrouppossiblematches_set",
            queryset=GalleryGroupPossibleMatches.objects.select_related("gallery_match_group", "gallery").prefetch_related(
                Prefetch("gallery__tags", queryset=Tag.objects.filter(scope__exact="artist"), to_attr="artist_tags"),
                Prefetch(
                    "gallery__tags", queryset=Tag.objects.filter(scope__exact="magazine"), to_attr="magazine_tags"
                ),
            ),
            to_attr="possible_galleries",
        ),
        "gallery_group__possible_galleries__gallery",
    )

    if "with-possible-matches" in get:
        results = results.annotate(n_possible_galleries=Count("gallery_group__possible_matches")).filter(n_possible_galleries__gt=0)

    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    available_providers = Gallery.objects.all().values_list("provider", flat=True).distinct()

    matcher_types = ("title", "comment")

    d = {
        "results": results_page,
        "providers": available_providers,
        "matcher_types": matcher_types,
        "form": form,
        "inline_thumbnails": inline_thumbnails,
    }
    return render(request, "viewer/collaborators/gallery_match_group_tool.html", d)



@login_required
def archive_update(
    request: HttpRequest, pk: int, tool: str | None = None, tool_use_id: str | None = None
) -> HttpResponse:
    try:
        archive = Archive.objects.get(pk=pk)
    except Archive.DoesNotExist:
        raise Http404("Archive does not exist")

    if tool == "select-as-match" and tool_use_id and request.user.has_perm("viewer.match_archive"):
        try:
            gallery_id = int(tool_use_id)
            archive.select_as_match(gallery_id)
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
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "clear-possible-matches" and request.user.has_perm("viewer.match_archive"):
        archive.possible_matches.clear()
        logger.info(
            "User: {}: Archive {} ({}) was cleared from its possible matches.".format(
                request.user.username,
                archive,
                reverse("viewer:archive", args=(archive.pk,)),
            )
        )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    else:
        return render_error(request, "Unrecognized command")


@login_required
def gallery_update(
    request: HttpRequest, pk: int, tool: str | None = None, tool_use_id: str | None = None
) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")

    if tool == "add-to-match-group" and tool_use_id and request.user.has_perm("viewer.add_gallerymatchgroup"):
        try:
            gallery_id = int(tool_use_id)
            try:
                gallery_match = Gallery.objects.get(pk=gallery_id)
            except Gallery.DoesNotExist:
                raise Http404("Gallery does not exist")
            gallery_match_group, created = GalleryMatchGroup.objects.get_or_create(galleries__in=(gallery,))
            if created:
                gallery_match_group.galleries.add(gallery)
            gallery_match_group.add_gallery(gallery_match)
            gallery_match_group.save()
            GalleryGroupPossibleMatches.objects.filter(
                gallery_match_group=gallery_match_group
            ).delete()
            logger.info(
                "User: {}: Gallery {} ({}) was matched in a group with gallery {} ({}).".format(
                    request.user.username,
                    gallery,
                    reverse("viewer:gallery", args=(gallery.pk,)),
                    gallery_match,
                    reverse("viewer:gallery", args=(gallery_match.pk,)),
                )
            )
            event_log(
                request.user,
                "ADD_GALLERY_TO_MATCH_GROUP",
                # reason=user_reason,
                data=reverse("viewer:gallery", args=(gallery_match.pk,)),
                content_object=gallery_match,
                result="matched",
            )
        except ValueError:
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    elif tool == "clear-possible-matches" and request.user.has_perm("viewer.add_gallerymatchgroup"):
        gallery_group = GalleryMatchGroup.objects.filter(galleries__in=(gallery,)).first()
        if gallery_group:
            gallery_group.possible_matches.clear()
            logger.info(
                "User: {}: Gallery Group {} was cleared from its possible matches.".format(
                    request.user.username,
                    gallery_group.pk
                )
            )
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    else:
        return render_error(request, "Unrecognized command")


@permission_required("viewer.manage_missing_archives")
def missing_archives_for_galleries(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = GallerySearchForm()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-") and isinstance(v, str):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        if "reason" in p and p["reason"] != "":
            reason = p["reason"]
        else:
            reason = ""

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        results_gallery = Gallery.objects.filter(id__in=pks).order_by(preserved)

        if "delete_galleries" in p and request.user.has_perm("viewer.mark_delete_gallery"):
            for gallery in results_gallery:
                message = "Marking deleted for gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.mark_as_deleted()
                event_log(
                    request.user,
                    "MARK_DELETE_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
        elif "real_delete_galleries" in p and request.user.has_perm("viewer.delete_gallery"):
            for gallery in results_gallery:
                message = "Deleting gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                old_gallery_link = gallery.get_link()
                logger.info(message)
                messages.success(request, message)
                gallery.delete()
                event_log(
                    request.user,
                    "DELETE_GALLERY",
                    reason=reason,
                    data=old_gallery_link,
                    result="success",
                )
        elif "publish_galleries" in p and request.user.has_perm("viewer.publish_gallery"):
            for gallery in results_gallery:
                message = "Publishing gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.set_public()
                event_log(
                    request.user,
                    "PUBLISH_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
        elif "private_galleries" in p and request.user.has_perm("viewer.private_gallery"):
            for gallery in results_gallery:
                message = "Making private gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.set_private()
                event_log(
                    request.user,
                    "UNPUBLISH_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
        elif "download_galleries" in p and request.user.has_perm("viewer.download_gallery"):
            for gallery in results_gallery:
                message = "Queueing gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)

                # Force replace_metadata when queueing from this list, since it's mostly used to download non used.
                current_settings = Settings(load_from_config=crawler_settings.config)

                current_settings.replace_metadata = True
                current_settings.retry_failed = True

                if reason:
                    # Force limit string length (reason field max_length)
                    current_settings.archive_reason = reason[:200]
                    current_settings.archive_details = gallery.reason or ""
                    current_settings.gallery_reason = reason[:200]
                elif gallery.reason:
                    current_settings.archive_reason = gallery.reason

                current_settings.archive_user = request.user
                current_settings.event_action = "DOWNLOAD"

                client.enqueue(
                    args=[gallery.get_link()],
                    task_type="web_crawler",
                    override_options=current_settings,
                    user=request.user,
                    reason=reason,
                    event_action="DOWNLOAD",
                )
        elif "recall_api" in p and request.user.has_perm("viewer.update_metadata"):
            message = "Recalling API for {} galleries".format(results_gallery.count())
            logger.info(message)
            messages.success(request, message)

            gallery_links = [x.get_link() for x in results_gallery]
            gallery_providers = list(results_gallery.values_list("provider", flat=True).distinct())

            galleries_update_metadata(gallery_links, gallery_providers, request.user, reason, crawler_settings)

    providers = Gallery.objects.all().values_list("provider", flat=True).distinct()

    params = {}

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    results = results.report_as_missing_galleries().prefetch_related("foundgallery_set")

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "providers": providers, "form": form}

    return render(request, "viewer/collaborators/archives_missing_for_galleries.html", d)


@permission_required("viewer.manage_archive")

@permission_required("viewer.manage_gallery")
def repeated_galleries_by_field(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    providers =  get.getlist("providers")
    categories =  get.getlist("categories")

    if "clear" in get:
        form = GallerySearchForm()
        fields_form = GallerySearchFields()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})
        fields_form = GallerySearchFields(initial={"providers": providers, "categories": categories})

    if p:
        user_reason = p.get("reason", "")
        if "delete_galleries" in p and request.user.has_perm("viewer.mark_delete_gallery"):
            pks = []
            for k in p:
                if k.startswith("sel-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    v_keys = [int(x) for x in p.getlist(k)]
                    pks.extend(v_keys)
            results_gallery = Gallery.objects.filter(id__in=pks).order_by("-create_date")

            for gallery in results_gallery:
                message = "Removing gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.mark_as_deleted()

                event_log(
                    request.user, "MARK_DELETE_GALLERY", reason=user_reason, content_object=gallery, result="deleted"
                )
        elif "create_gallery_match_groups" in p and request.user.has_perm("viewer.add_gallerymatchgroup"):
            groups = []
            pks = []
            order_by_pk = {}
            for k in p:
                if k.startswith("sel-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    v_keys = [int(x) for x in p.getlist(k)]
                    groups.append(v_keys)
                    pks.extend(v_keys)
                elif k.startswith("ord-"):
                    k_value = p.get(k)
                    if k_value is not None:
                        order_by_pk[int(k.replace("ord-", ""))] = int(k_value)

            results_gallery = Gallery.objects.filter(id__in=pks)
            gallery_mappings = {x.pk: x for x in results_gallery}

            gallery_groups = [sorted([gallery_mappings[y] for y in x], key=lambda x: order_by_pk[x.pk]) for x in groups]

            for gallery_group in gallery_groups:
                GalleryMatchGroup.objects.filter(galleries__in=gallery_group).delete()
                gallery_match_group = GalleryMatchGroup()
                gallery_match_group.save()
                for count, gallery in enumerate(gallery_group, start=1):
                    gallery_match_group_entry, _ = GalleryMatchGroupEntry.objects.update_or_create(
                        gallery=gallery,
                        defaults={"gallery_match_group": gallery_match_group, "gallery_position": count}
                    )
                # Save again to make sure it gets the title
                gallery_match_group.save()
                message = "Created gallery match group: {}, galleries: {}".format(gallery_match_group.pk, [x.get_absolute_url() for x in gallery_match_group.galleries.all()])
                logger.info(message)
                messages.success(request, message)
                event_log(
                    request.user, "CREATE_GALLERY_MATCH_GROUP", reason=user_reason, content_object=gallery_match_group, result="created"
                )


    params: dict[str, list[str] | str] = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k in get:
        if k in gallery_list_filters_keys:
            v: list[str] | str = get.getlist(k)
        else:
            v = get[k]
        if isinstance(v, str) or isinstance(v, list):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    results = results.eligible_for_use().exclude(title__exact="")

    if "has-archives" in get:
        results = results.annotate(archives=Count("archive")).filter(archives__gt=0)

    if "is-not-in-groups" in get:
        results = results.annotate(gallery_groups=Count("gallery_group")).filter(gallery_groups=0)
    if "is-in-single-group" in get:
        results = results.annotate(galleries_in_group=Count("gallery_group__galleries")).filter(galleries_in_group=1)

    if "has-size" in get:
        results = results.filter(filesize__gt=0)

    by_title = {}
    by_filesize = {}

    if "ignore-case" in get:
        clear_function = lambda x: x.lower()
    if "clear-fields" in get:
        clear_function = lambda x: re.sub(r"[^A-Za-z0-9 ]+", "", re.sub(r"\s+\(.+?\)", r"", re.sub(r"\[.+?]\s*", r"", x))).lower().strip()
    if "clear-fields" not in get and "ignore-case" not in get:
        clear_function = lambda x: x

    max_diff_filecount = None

    if 'limit-diff-filecount' in get:
        if 'max-diff-filecount' in get and get['max-diff-filecount']:
            max_diff_filecount = float(get.get('max-diff-filecount', 0))
            if max_diff_filecount <= 0:
                max_diff_filecount = 0
        else:
            max_diff_filecount = 0

    if "by-title" in get:
        if "same-uploader" in get:
            for k_tu, v_tu in groupby(results.order_by("title", "uploader"), lambda x: (clear_function(x.title or ""), clear_function(x.uploader or ""))):
                objects = list(v_tu)
                if len(objects) > 1:
                    if max_diff_filecount is not None:
                        objects = [x for x in objects if abs((x.filecount or 0) - (objects[0].filecount or 0)) <= max_diff_filecount]
                    if len(objects) > 1:
                        by_title[str(k_tu)] = objects
        if "same-description" in get:
            for k_tu, v_tu in groupby(results.exclude(comment="").order_by("title", "comment"), lambda x: (clear_function(x.title or ""), clear_function(x.comment or ""))):
                objects = list(v_tu)
                if len(objects) > 1:
                    if max_diff_filecount is not None:
                        objects = [x for x in objects if abs((x.filecount or 0) - (objects[0].filecount or 0)) <= max_diff_filecount]
                    if len(objects) > 1:
                        by_title[str(k_tu)] = objects
        else:
            for k_title, v_title in groupby(results.order_by("title"), lambda x: clear_function(x.title or "")):
                objects = list(v_title)
                if len(objects) > 1:
                    if max_diff_filecount is not None:
                        objects = [x for x in objects if abs((x.filecount or 0) - (objects[0].filecount or 0)) <= max_diff_filecount]
                    if len(objects) > 1:
                        by_title[k_title] = objects

    if 'range-per-group-provider' in get:
        if 'min-group' in get and get['min-group']:
            min_group = int(get.get('min-group', 0))
            if min_group < 0:
                min_group = 0
        else:
            min_group = 0
        if 'max-group' in get and get['max-group']:
            max_group = float(get.get('max-group', 0))
            if max_group <= 0:
                max_group = float('inf')
        else:
            max_group = float('inf')
        by_title = {x[0]: x[1] for x in by_title.items() if all([max_group >= len(list(y[1])) >= min_group for y in groupby(x[1], lambda x: x.provider)])}
        # by_title = {x[0]: x[1] for x in by_title.items() if max_group >= len(x[1]) >= min_group}

    if "by-filesize" in get:
        for k_filesize, v_filesize in groupby(results.order_by("filesize"), lambda x: str(x.filesize or "")):
            objects = list(v_filesize)
            if len(objects) > 1:
                if max_diff_filecount is not None:
                    objects = [x for x in objects if abs((x.filecount or 0) - (objects[0].filecount or 0)) <= max_diff_filecount]
                if len(objects) > 1:
                    by_filesize[k_filesize] = objects

    try:
        inline_thumbnails = bool(get.get("inline-thumbnails", ""))
    except ValueError:
        inline_thumbnails = False

    d = {
        "by_title": by_title, "by_filesize": by_filesize, "form": form,
        "fields_form": fields_form,
        "inline_thumbnails": inline_thumbnails
    }

    return render(request, "viewer/galleries_repeated_by_fields.html", d)



