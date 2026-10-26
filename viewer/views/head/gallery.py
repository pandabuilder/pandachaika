from functools import reduce
import json
import logging
import operator
from random import randint
import re
import typing
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import EmptyPage, InvalidPage, Paginator
from django.db.models import Avg, Count, F, Max, Min, Prefetch, Q, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.http.request import QueryDict
from django.shortcuts import redirect, render
from django.templatetags.static import static
from django.urls import reverse

from core.base.setup import Settings
from core.base.types import DataDict
from viewer.forms import DivErrorList, GallerySearchForm
from viewer.models import (
    Archive,
    ArchiveGroup,
    Gallery,
    GalleryMatchGroup,
    GalleryProviderData,
    GalleryQuerySet,
    Tag,
)
from viewer.utils.actions import event_log
from viewer.utils.functions import gallery_search_results_to_json
from viewer.utils.general import clean_up_referer, should_use_refined_views
from viewer.utils.tags import sort_tags
from viewer.views.head.auth import render_error, render_message
from viewer.views.head.common import (
    crawler_settings,
    gallery_filter_keys,
    gallery_list_filters_keys,
    gallery_order_fields,
)
from workers import client

logger = logging.getLogger(__name__)


def process_gallery_page(request: HttpRequest, gallery: Gallery, tool: str | None = None) -> HttpResponse:

    if "HTTP_REFERER" in request.META:
        response = HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
    else:
        response = HttpResponseRedirect(gallery.get_absolute_url())

    if request.user.has_perm("viewer.download_gallery") and tool == "download":
        if "downloader" in request.GET and request.user.is_staff:
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.allow_downloaders_only([request.GET["downloader"]], True, True, True)
            client.enqueue(
                args=[gallery.get_link()],
                task_type="web_crawler",
                override_options=current_settings,
                user=request.user,
            )
        else:
            # Since this is used from the gallery page mainly to download an already added gallery using
            # downloader settings, force replace_metadata and retry_failed
            current_settings = Settings(load_from_config=crawler_settings.config)
            current_settings.replace_metadata = True
            current_settings.retry_failed = True
            client.enqueue(
                args=[gallery.get_link()],
                task_type="web_crawler",
                override_options=current_settings,
                user=request.user,
            )
        return response

    if request.user.is_staff and tool == "toggle-hidden":
        gallery.hidden = not gallery.hidden
        gallery.save()
        return response

    if request.user.has_perm("viewer.publish_gallery") and tool == "toggle-public":
        gallery.public_toggle()

        if gallery.public:
            event_log(request.user, "PUBLISH_GALLERY", content_object=gallery, result="success")
        else:
            event_log(request.user, "UNPUBLISH_GALLERY", content_object=gallery, result="success")

        return response

    if request.user.has_perm("viewer.mark_delete_gallery") and tool == "mark-deleted":
        gallery.mark_as_deleted()

        event_log(request.user, "MARK_DELETE_GALLERY", content_object=gallery, result="deleted")

        return response

    if request.user.has_perm("viewer.mark_delete_gallery") and tool == "mark-normal":
        gallery.mark_as_normal()

        event_log(request.user, "MARK_NORMAL_GALLERY", content_object=gallery, result="success")

        return response

    if request.user.has_perm("viewer.update_metadata") and tool == "recall-api":

        current_settings = Settings(load_from_config=crawler_settings.config)

        if gallery.provider:

            current_settings.archive_user = request.user
            current_settings.event_action = "UPDATE_METADATA"
            current_settings.set_update_metadata_options(providers=(gallery.provider,))

            client.enqueue(
                args=[gallery.get_link()],
                task_type="web_crawler",
                override_options=current_settings,
                user=request.user,
                event_action="UPDATE_METADATA",
            )

            logger.info(
                "Updating gallery API data for gallery: {} and related archives".format(gallery.get_absolute_url())
            )

        return response

    if request.user.has_perm("viewer.update_metadata") and tool == "refetch-thumbnail":

        if gallery.thumbnail_url:

            success = gallery.fetch_thumbnail(force_redownload=True)

            event_log(
                request.user,
                "REFETCH_THUMBNAIL",
                content_object=gallery,
                result="sucess" if success else "failed",
                data=gallery.thumbnail_url,
            )

            logger.info(
                "Refetching gallery: {} thumbnail from url: {}".format(
                    gallery.get_absolute_url(), gallery.thumbnail_url
                )
            )

        return response

    tag_lists = sort_tags(gallery.tags.all())

    gallery_provider_data = GalleryProviderData.objects.filter(gallery=gallery)

    d: dict[str, Any] = {
        "gallery": gallery,
        "tag_lists": tag_lists,
        "settings": crawler_settings,
        "gallery_provider_data": gallery_provider_data,
    }

    if gallery.first_gallery:
        gallery_filters = (
            Q(first_gallery=gallery.first_gallery)
            | Q(first_gallery=gallery)
            | Q(pk=gallery.pk)
            | Q(pk=gallery.first_gallery.pk)
        )
    else:
        gallery_filters = Q(first_gallery=gallery) | Q(pk=gallery.pk)

    d.update(
        {
            "gallery_chain": Gallery.objects.filter(gallery_filters, provider=gallery.provider).order_by("gid"),
            "gallery_group": GalleryMatchGroup.objects.filter(galleries=gallery).prefetch_related("galleries").first()
        }
    )

    template = (
        "viewer/gallery_new.html"
        if should_use_refined_views(request)
        else "viewer/gallery_old.html"
    )
    return render(request, template, d)


def gallery_details_gid_provider(request: HttpRequest, tool: str | None = None) -> HttpResponse:
    if "gid" in request.GET:
        gallery_gid = request.GET["gid"]
        if "provider" in request.GET:
            possible_gallery = Gallery.objects.filter_first(gid=gallery_gid, provider=request.GET["provider"])
        else:
            possible_gallery = Gallery.objects.filter_first(gid=gallery_gid)
        if not possible_gallery:
            raise Http404("Gallery does not exist")
        else:
            gallery = possible_gallery
    else:
        raise Http404("Gallery does not exist")
    if not (gallery.public or request.user.is_authenticated):
        raise Http404("Gallery does not exist")

    return process_gallery_page(request, gallery, tool)


def gallery_details(request: HttpRequest, pk: int, tool: str | None = None) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not (gallery.public or request.user.is_authenticated):
        raise Http404("Gallery does not exist")

    return process_gallery_page(request, gallery, tool)


@login_required
def gallery_enter_reason(request: HttpRequest, pk: int, tool: str | None = None) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not request.user.is_authenticated:
        raise Http404("Gallery does not exist")

    if request.method == "POST":

        p = request.POST
        user_reason = p.get("reason", "")
        if "confirm_tool" in p:

            if request.user.has_perm("viewer.download_gallery") and tool == "download":
                if "downloader" in request.GET and request.user.is_staff:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.allow_downloaders_only([request.GET["downloader"]], True, True, True)
                    client.enqueue(
                        args=[gallery.get_link()],
                        task_type="web_crawler",
                        override_options=current_settings,
                        user=request.user,
                    )
                else:
                    # Since this is used from the gallery page mainly to download an already added gallery using
                    # downloader settings, force replace_metadata and retry_failed
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True
                    client.enqueue(
                        args=[gallery.get_link()],
                        task_type="web_crawler",
                        override_options=current_settings,
                        user=request.user,
                    )
                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.is_staff and tool == "toggle-hidden":
                gallery.hidden = not gallery.hidden
                gallery.save()
                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm("viewer.publish_gallery") and tool == "toggle-public":
                gallery.public_toggle()

                if gallery.public:
                    event_log(
                        request.user, "PUBLISH_GALLERY", content_object=gallery, result="success", reason=user_reason
                    )
                else:
                    event_log(
                        request.user, "UNPUBLISH_GALLERY", content_object=gallery, result="success", reason=user_reason
                    )

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm("viewer.mark_delete_gallery") and tool == "mark-deleted":
                gallery.mark_as_deleted()

                event_log(
                    request.user, "MARK_DELETE_GALLERY", content_object=gallery, result="deleted", reason=user_reason
                )

                message = "Gallery: {} will be marked as deleted, reason: {}".format(
                    gallery.get_absolute_url(), user_reason
                )

                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm("viewer.mark_delete_gallery") and tool == "mark-normal":
                gallery.mark_as_normal()

                event_log(
                    request.user, "MARK_NORMAL_GALLERY", content_object=gallery, result="success", reason=user_reason
                )

                message = "Gallery: {} will be marked as normal, reason: {}".format(
                    gallery.get_absolute_url(), user_reason
                )

                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm("viewer.approve_gallery") and tool == "mark-denied":
                gallery.mark_as_denied()

                event_log(
                    request.user, "MARK_DENIED_GALLERY", content_object=gallery, result="success", reason=user_reason
                )

                message = "Gallery: {} will be marked as denied, reason: {}".format(
                    gallery.get_absolute_url(), user_reason
                )

                logger.info("User {}: {}".format(request.user.username, message))
                messages.success(request, message)

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm("viewer.update_metadata") and tool == "recall-api":

                current_settings = Settings(load_from_config=crawler_settings.config)

                if gallery.provider:

                    current_settings.archive_user = request.user
                    current_settings.event_action = "UPDATE_METADATA"
                    current_settings.archive_reason = user_reason
                    current_settings.gallery_reason = user_reason
                    current_settings.set_update_metadata_options(providers=(gallery.provider,))

                    client.enqueue(
                        args=[gallery.get_link()],
                        task_type="web_crawler",
                        override_options=current_settings,
                        user=request.user,
                        reason=user_reason,
                        event_action="UPDATE_METADATA",
                    )

                    logger.info(
                        "Updating gallery API data for gallery: {} and related archives".format(
                            gallery.get_absolute_url()
                        )
                    )

                return HttpResponseRedirect(gallery.get_absolute_url())

            if request.user.has_perm("viewer.update_metadata") and tool == "refetch-thumbnail":

                if gallery.thumbnail_url:

                    success = gallery.fetch_thumbnail(force_redownload=True)

                    event_log(
                        request.user,
                        "REFETCH_THUMBNAIL",
                        content_object=gallery,
                        result="sucess" if success else "failed",
                        data=gallery.thumbnail_url,
                        reason=user_reason,
                    )

                    logger.info(
                        "Refetching gallery: {} thumbnail from url: {}".format(
                            gallery.get_absolute_url(), gallery.thumbnail_url
                        )
                    )

                return HttpResponseRedirect(gallery.get_absolute_url())

    d = {"gallery": gallery, "tool": tool}

    inlined = request.GET.get("inline", None)

    if inlined:
        return render(request, "viewer/include/modals/gallery_tool_reason.html", d)
    else:
        return render(request, "viewer/gallery_display_tool.html", d)


def gallery_thumb(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        gallery = Gallery.objects.get(pk=pk)
    except Gallery.DoesNotExist:
        raise Http404("Gallery does not exist")
    if not gallery.public and not request.user.is_authenticated:
        raise Http404("Gallery is not public")

    thumb = gallery.thumbnail
    try:
        has_thumb = bool(thumb and thumb.name)
    except ValueError:
        has_thumb = False

    if not has_thumb:
        return HttpResponseRedirect(static("imgs/no_cover.png"))

    if "HTTP_X_FORWARDED_HOST" in request.META:
        response = HttpResponse()
        response["Content-Type"] = "image/jpeg"
        # response["Content-Disposition"] = 'attachment; filename*=UTF-8\'\'{0}'.format(
        #         archive.pretty_name)
        response["X-Accel-Redirect"] = "/image/{0}".format(thumb.name)
        return response
    else:
        try:
            return HttpResponseRedirect(thumb.url)
        except ValueError:
            return HttpResponseRedirect(static("imgs/no_cover.png"))


def gallery_list(request: HttpRequest, mode: str = "none", tag: str | None = None) -> HttpResponse:
    """Search, filter, sort galleries."""
    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1

    cleared_queries = request.GET.copy()
    queries_to_clear = ("page", "view", "apply")

    for entry in queries_to_clear:
        if entry in cleared_queries:
            del cleared_queries[entry]

    get = request.GET
    setattr(request, "GET", cleared_queries)

    json_request = "json" in get

    display_prms: dict[str, Any] = {}

    # init parameters
    if "gallery_parameters" in request.session:
        parameters = request.session["gallery_parameters"]
    else:
        parameters = {}

    keys = ("sort", "asc_desc")

    for k in keys:
        if k not in parameters:
            parameters[k] = ""

    for k in gallery_filter_keys:
        if k not in display_prms:
            display_prms[k] = ""

    parameters["show_source"] = False

    if "clear" in get:
        for k in gallery_filter_keys:
            display_prms[k] = ""
        request.GET = QueryDict("")
        # request.session["gallery_parameters"].clear()
    else:
        if mode == "gallery-tag" and tag:
            display_prms["tags"] = "^" + tag
        else:
            # create a dictionary of properties for each archive and a dict of
            # search/filter parameters
            for k in get:
                if k in gallery_list_filters_keys:
                    v: list[str] | str = get.getlist(k)
                else:
                    v = get[k]
                if k in parameters:
                    parameters[k] = v
                elif k in display_prms:
                    display_prms[k] = v
    # Fill default parameters
    if "view" not in parameters or parameters["view"] == "":
        parameters["view"] = "list"
    if "asc_desc" not in parameters or parameters["asc_desc"] == "":
        parameters["asc_desc"] = "desc"
    if "sort" not in parameters or parameters["sort"] == "":
        parameters["sort"] = "posted"

    parameters["main_filters"] = True
    request.session["gallery_parameters"] = parameters
    results = filter_galleries(request, parameters, display_prms, json_request=json_request)

    if json_request:
        if request.user.is_authenticated:
            used_prefetch = Prefetch("archive_set", to_attr="available_archives")
        else:
            used_prefetch = Prefetch(
                "archive_set", queryset=Archive.objects.filter(public=True), to_attr="available_archives"
            )
        results = results.prefetch_related("tags", used_prefetch).distinct()

    if "random" in get:
        count = results.count()
        if count > 0:
            random_index = randint(0, count - 1)
            return redirect(results[random_index])

    # make paginator
    if parameters["view"] == "list" or json_request:
        galleries_per_page = 100
    else:
        galleries_per_page = 24

    paginator = Paginator(results, galleries_per_page)
    try:
        results_page = paginator.page(page)

    except (InvalidPage, EmptyPage):
        results_page = paginator.page(1)

    if json_request:
        response = json.dumps(
            {
                "galleries": gallery_search_results_to_json(request, results_page),
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

    display_prms["page_range"] = list(
        range(
            max(1, results_page.number - 3 - max(0, results_page.number - (paginator.num_pages - 3))),
            min(paginator.num_pages + 1, results_page.number + 3 + 1 - min(0, results_page.number - 3 - 1)),
        )
    )

    gallery_search_form = GallerySearchForm(initial={"title": display_prms["title"], "tags": display_prms["tags"]}, form_name="filter-gallery-form")

    view_options = ("list", "extended")

    d = {
        "results": results_page,
        "prm": parameters,
        "display_prms": display_prms,
        "gallery_search_form": gallery_search_form,
        "extra_options": view_options,
    }

    return render(request, "viewer/gallery_search.html", d)


filter_galleries_defer = (
    "origin",
    "status",
    "thumbnail_url",
    "dl_type",
    "public",
    "fjord",
    "hidden",
    "rating",
    "expunged",
    "comment",
    "gallery_container",
    "magazine",
    "uploader",
    "reason",
    "last_modified",
)


def filter_galleries(
    request: HttpRequest, session_filters: dict[str, str], request_filters: dict[str, str], json_request: bool = False
) -> GalleryQuerySet:
    """Filter gallery results through parameters and return a result list."""

    # sort and filter results by parameters
    order = "posted"
    if session_filters["sort"]:
        order = session_filters["sort"]
    if session_filters["asc_desc"] == "desc":
        results = Gallery.objects.all().order_by(F(order).desc(nulls_last=True))
    else:
        results = Gallery.objects.all().order_by(F(order).asc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)
    else:
        if request_filters["public"]:
            results = results.filter(public=True)
        if request_filters["not_public"]:
            results = results.filter(public=False)

    if request_filters["title"]:
        q_formatted = "%" + request_filters["title"].replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))
    if request_filters["rating_from"]:
        results = results.filter(rating__gte=float(request_filters["rating_from"]))
    if request_filters["rating_to"]:
        results = results.filter(rating__lte=float(request_filters["rating_to"]))
    if request_filters["filecount_from"]:
        results = results.filter(filecount__gte=int(float(request_filters["filecount_from"])))
    if request_filters["filecount_to"]:
        results = results.filter(filecount__lte=int(float(request_filters["filecount_to"])))
    if request_filters["filesize_from"]:
        results = results.filter(filesize__gte=float(request_filters["filesize_from"]))
    if request_filters["filesize_to"]:
        results = results.filter(filesize__lte=float(request_filters["filesize_to"]))
    if request_filters["category"]:
        results = results.filter(category__icontains=request_filters["category"])
    if request_filters["categories"]:
        results = results.filter(category__in=request_filters["categories"])
    if request_filters["expunged"]:
        results = results.filter(expunged=bool(request_filters["expunged"]))
    if request_filters["disowned"]:
        results = results.filter(disowned=bool(request_filters["disowned"]))
    if request_filters["fjord"]:
        results = results.filter(fjord=bool(request_filters["fjord"]))
    if request_filters["uploader"]:
        results = results.filter(uploader=request_filters["uploader"])
    if request_filters["provider"]:
        results = results.filter(provider=request_filters["provider"])
    if request_filters["providers"]:
        results = results.filter(provider__in=request_filters["providers"])
    if request_filters["empty_comment"]:
        results = results.filter(comment="")
    else:
        if request_filters["comment"]:
            results = results.filter(comment__contains=request_filters["comment"])
    if request_filters["contained"]:
        results = results.filter(Q(gallery_container__isnull=False) | Q(magazine__isnull=False))
    if request_filters["contained_container"]:
        results = results.filter(Q(gallery_container__isnull=False))
    if request_filters["contained_magazine"]:
        results = results.filter(Q(magazine__isnull=False))
    if request_filters["not_contained"]:
        results = results.filter(Q(gallery_container__isnull=True) & Q(magazine__isnull=True))
    if request_filters["not_contained_container"]:
        results = results.filter(Q(gallery_container__isnull=True))
    if request_filters["not_contained_magazine"]:
        results = results.filter(Q(magazine__isnull=True))
    if request_filters["contains"]:
        results = results.annotate(
            num_contains=Count("gallery_contains"), num_chapters=Count("magazine_chapters")
        ).filter(Q(num_contains__gt=0) | Q(num_chapters__gt=0))
    if request_filters["not_contains"]:
        results = results.annotate(
            num_contains=Count("gallery_contains"), num_chapters=Count("magazine_chapters")
        ).filter(Q(num_contains=0) & Q(num_chapters=0))
    if request_filters["reason"]:
        results = results.filter(reason__contains=request_filters["reason"])
    if request.user.is_authenticated:
        if request_filters["not_used"]:
            results = results.non_used_galleries()
        if request_filters["not_used_including_groups"]:
            results = results.not_used_including_groups()
        if request_filters["only_used"]:
            results = results.only_used_galleries()
        if request_filters["hidden"]:
            results = results.filter(hidden=bool(request_filters["hidden"]))
        if "not_normal" not in request_filters or not request_filters["not_normal"]:
            results = results.eligible_for_use()
    else:
        results = results.eligible_for_use()

    if request_filters["tags"]:
        tags = request_filters["tags"].split(",")

        q_objects = Q()
        if request_filters["tags_or"]:
            tags_or = True
        else:
            tags_or = False

        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ""
                tag_name = scope_name[0]
            if tag.startswith("-^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope))
                elif tag_name != "":
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                if tags_or:
                    q_objects.add(~tag_query, Q.OR)
                else:
                    results = results.exclude(tag_query)
            elif tag.startswith("-"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope))
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                if tags_or:
                    q_objects.add(~tag_query, Q.OR)
                else:
                    results = results.exclude(tag_query)
            elif tag.startswith("^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope))
                elif tag_name != "":
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                if tags_or:
                    q_objects.add(tag_query, Q.OR)
                else:
                    results = results.filter(tag_query)
            else:
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope))
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                if tags_or:
                    q_objects.add(tag_query, Q.OR)
                else:
                    results = results.filter(tag_query)

        if tags_or:
            results = results.filter(q_objects)
        results = results.distinct()

    if session_filters["view"] == "extended":
        results = (
            results.select_related("gallery_container", "magazine", "first_gallery", "parent_gallery")
            .prefetch_related("tags")
            .defer("provider_metadata")
        )
    # Remove fields that are admin related, not public facing
    elif not json_request:
        results = results.defer(*filter_galleries_defer)

    return results



def quick_gallery_search(request: HttpRequest) -> QuerySet[Gallery]:
    """Quick search of galleries."""
    # sort and filter results by parameters
    order = "posted"

    results: QuerySet[Gallery] = Gallery.objects.order_by(F(order).desc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    qsearch = request.GET.get("qsearch", "")

    # URL search
    url = qsearch

    parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
    gallery_ids_providers = list()
    for parser in parsers:
        if parser.id_from_url_implemented():
            accepted_urls = parser.filter_accepted_urls((url,))
            gallery_ids_providers.extend([(parser.id_from_url(x), parser.name) for x in accepted_urls])

    if gallery_ids_providers:
        query = reduce(operator.or_, (Q(gid=gid, provider=provider) for gid, provider in gallery_ids_providers))

        results_url: QuerySet[Gallery] | None = results.filter(query)
    else:
        results_url = None

    q_formatted = "%" + qsearch.replace(" ", "%") + "%"
    results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))

    if results_url:
        results = results | results_url
    else:
        results = results

    results = results.distinct()

    results = results.defer(*filter_galleries_defer)

    return results



def filter_galleries_simple(params: dict[str, Any]) -> GalleryQuerySet:
    """Filter results through parameters
    and return results list.
    Note: It does not check for auth. Only call it from views that already check for Auth.
    """
    # sort and filter results by parameters
    order = "posted"
    if "sort" in params and params["sort"]:
        order = params["sort"]
    if "asc_desc" in params and params["asc_desc"] == "desc":
        results = Gallery.objects.all().order_by(F(order).desc(nulls_last=True))
    else:
        results = Gallery.objects.all().order_by(F(order).asc(nulls_last=True))

    if params["title"]:
        q_formatted = "%" + params["title"].replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))
    if params["rating_from"]:
        results = results.filter(rating__gte=float(params["rating_from"]))
    if params["rating_to"]:
        results = results.filter(rating__lte=float(params["rating_to"]))
    if params["filecount_from"]:
        results = results.filter(filecount__gte=int(float(params["filecount_from"])))
    if params["filecount_to"]:
        results = results.filter(filecount__lte=int(float(params["filecount_to"])))
    if params["filesize_from"]:
        results = results.filter(filesize__gte=float(params["filesize_from"]))
    if params["filesize_to"]:
        results = results.filter(filesize__lte=float(params["filesize_to"]))
    if params["category"]:
        results = results.filter(category__icontains=params["category"])
    if params["categories"]:
        results = results.filter(category__in=params["categories"])
    if params["expunged"]:
        results = results.filter(expunged=bool(params["expunged"]))
    if params["disowned"]:
        results = results.filter(disowned=bool(params["disowned"]))
    if params["hidden"]:
        results = results.filter(hidden=bool(params["hidden"]))
    if params["fjord"]:
        results = results.filter(fjord=bool(params["fjord"]))
    if params["uploader"]:
        results = results.filter(uploader=params["uploader"])
    if params["dl_type"]:
        results = results.filter(dl_type=params["dl_type"])
    if params["reason"]:
        results = results.filter(reason__contains=params["reason"])
    if params["provider"]:
        results = results.filter(provider=params["provider"])
    if params["providers"]:
        results = results.filter(provider__in=params["providers"])
    if params["not_used"]:
        results = results.filter(Q(archive__isnull=True))
    if params["not_used_including_groups"]:
        results = results.not_used_including_groups()
    if params["reason"]:
        results = results.filter(reason__contains=params["reason"])
    if params["empty_comment"]:
        results = results.filter(comment="")
    else:
        if params["comment"]:
            results = results.filter(comment__contains=params["comment"])

    if "tags" in params and params["tags"]:
        tags = params["tags"].split(",")

        q_objects = Q()
        if params["tags_or"]:
            tags_or = True
        else:
            tags_or = False

        for tag in tags:
            tag = tag.strip().replace(" ", "_")
            tag_clean = re.sub("^[-|^]", "", tag)
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ""
                tag_name = scope_name[0]
            if tag.startswith("-^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                if tags_or:
                    q_objects.add(~tag_query, Q.OR)
                else:
                    results = results.exclude(tag_query)
            elif tag.startswith("-"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                if tags_or:
                    q_objects.add(~tag_query, Q.OR)
                else:
                    results = results.exclude(tag_query)
            elif tag.startswith("^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                if tags_or:
                    q_objects.add(tag_query, Q.OR)
                else:
                    results = results.filter(tag_query)
            else:
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                if tags_or:
                    q_objects.add(tag_query, Q.OR)
                else:
                    results = results.filter(tag_query)

        if tags_or:
            results = results.filter(q_objects)
        results = results.distinct()

    if "non_public" in params and params["non_public"]:
        results = results.filter(public=False)

    if "public" in params and params["public"]:
        results = results.filter(public=True)

    if "status" in params and params["status"] and params["status"] != "0":
        results = results.filter(status=params["status"])

    if "contained" in params and params["contained"]:
        results = results.filter(Q(gallery_container__isnull=False) | Q(magazine__isnull=False))
    if "contained_container" in params and params["contained_container"]:
        results = results.filter(Q(gallery_container__isnull=False))
    if "contained_magazine" in params and params["contained_magazine"]:
        results = results.filter(Q(magazine__isnull=False))
    if "not_contained" in params and params["not_contained"]:
        results = results.filter(Q(gallery_container__isnull=True) & Q(magazine__isnull=True))
    if "not_contained_container" in params and params["not_contained_container"]:
        results = results.filter(Q(gallery_container__isnull=True))
    if "not_contained_magazine" in params and params["not_contained_magazine"]:
        results = results.filter(Q(magazine__isnull=True))
    if "contains" in params and params["contains"]:
        results = results.annotate(
            num_contains=Count("gallery_contains"), num_chapters=Count("magazine_chapters")
        ).filter(Q(num_contains__gt=0) | Q(num_chapters__gt=0))
    if "not_contains" in params and params["not_contains"]:
        results = results.annotate(
            num_contains=Count("gallery_contains"), num_chapters=Count("magazine_chapters")
        ).filter(Q(num_contains__eq=0) & Q(num_chapters__eq=0))
    return results



