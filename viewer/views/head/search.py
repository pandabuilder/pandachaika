import json
import logging
import operator
import re
from functools import reduce
from random import randint
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
from django.utils.html import linebreaks, urlize

from core.base.setup import Settings
from core.base.types import DataDict
from viewer.forms import ArchiveSearchForm, ArchiveSearchSimpleForm, DivErrorList
from viewer.models import (
    Archive,
    ArchiveGroup,
    Gallery,
    Image,
    Tag,
    UserArchivePrefs,
    WantedGallery,
)
from viewer.models.archive import ArchiveQuerySet
from viewer.utils.actions import event_log
from viewer.utils.functions import archive_search_result_to_json
from viewer.utils.general import valid_sha1_string
from viewer.utils.tags import sort_tags
from viewer.views.head.auth import render_error, render_message
from viewer.views.head.common import (
    archive_filter_keys,
    archive_order_fields,
    archive_public_order_fields,
    archive_public_sort_by_fields,
    archive_sort_by_fields,
    crawler_settings,
    wanted_gallery_filter_keys,
    wanted_gallery_order_fields,
)

logger = logging.getLogger(__name__)


def image_url(request: HttpRequest, pk: int) -> HttpResponse:
    try:
        image = Image.objects.get(id=pk, extracted=True)
    except Image.DoesNotExist:
        raise Http404("Image does not exist")
    if not image.archive.public and not request.user.is_authenticated:
        raise Http404("Image is not public")
    if "HTTP_X_FORWARDED_HOST" in request.META:
        response = HttpResponse()
        # response["Content-Disposition"] = 'attachment; filename="{0}"'.format(
        #         archive.pretty_name)
        response["Content-Type"] = ""
        response["X-Accel-Redirect"] = "/image/{0}".format(image.image.name)
        return response
    else:
        return HttpResponseRedirect(image.image.url)



def search(request: HttpRequest, mode: str = "none", tag: str | None = None) -> HttpResponse:
    """Search, filter, sort archives."""
    try:
        page = int(request.GET.get("page", "1"))
    except ValueError:
        page = 1

    cleared_queries = request.GET.copy()

    queries_to_clear = ("view", "clear", "apply")
    view_options = ("cover", "extended", "list")

    for entry in queries_to_clear:
        if entry in cleared_queries:
            del cleared_queries[entry]

    get = request.GET

    json_request = "json" in get

    if "rss" in get:
        return redirect("viewer:archive-rss")

    setattr(request, "GET", cleared_queries)

    display_prms: dict[str, Any] = {}

    if "parameters" in request.session:
        parameters = request.session["parameters"]
    else:
        parameters = {}

    keys = ("sort", "asc_desc")

    for k in keys:
        if k not in parameters:
            parameters[k] = ""

    for k in archive_filter_keys:
        if k not in display_prms:
            display_prms[k] = ""

    if "only_favorites" not in get:
        display_prms["only_favorites"] = 0
    if "clear" in get:
        for k in keys:
            display_prms[k] = ""
        request.GET = QueryDict("")
        form = ArchiveSearchForm(initial={"title": "", "tags": ""})
        form_simple = ArchiveSearchSimpleForm(initial={"source_type": ""})
    else:
        form_simple = ArchiveSearchSimpleForm(request.GET, error_class=DivErrorList)
        form_simple.is_valid()
        for field_name, errors in form_simple.errors.items():
            messages.error(request, field_name + ": " + ", ".join([str(x) for x in errors]), extra_tags="danger")
        if mode == "tag" and tag:
            display_prms["tags"] = "^" + tag
        # create dictionary of properties for each archive and a dict of
        # search/filter parameters
        for k, v in get.items():
            if k in parameters:
                parameters[k] = v
            elif k in display_prms:
                if k in form_simple.fields:
                    if k in form_simple.cleaned_data:
                        display_prms[k] = v
                else:
                    display_prms[k] = v
        display_prms["title"] = display_prms["title"].replace("\x00", "")
        form = ArchiveSearchForm(initial={"title": display_prms["title"], "tags": display_prms["tags"]})
    if "view" not in parameters or parameters["view"] == "":
        parameters["view"] = "list"
    if "asc_desc" not in parameters or parameters["asc_desc"] == "":
        parameters["asc_desc"] = "desc"
    if "sort" not in parameters or parameters["sort"] == "":
        if request.user.is_authenticated:
            parameters["sort"] = "create_date"
        else:
            parameters["sort"] = "public_date"
    if "qsearch" in get:
        parameters["main_filters"] = False
        results = quick_search(request, parameters, display_prms)
    else:
        parameters["main_filters"] = True
        results = filter_archives(request, parameters, display_prms, request.user.is_authenticated)

    request.session["parameters"] = parameters

    if "random" in get:
        count = results.count()
        if count > 0:
            random_index = randint(0, count - 1)
            return redirect(results[random_index])
    elif "gen-ddl" in get:
        links = [request.build_absolute_uri(reverse("viewer:archive-download", args=(x.pk,))) for x in results]
        return HttpResponse("\n".join(links), content_type="text/plain; charset=utf-8")

    if json_request:
        results = results.select_related("gallery").prefetch_related("tags").defer("gallery__comment")

    # make paginator
    if parameters["view"] == "list" or json_request:
        archives_per_page = 100
    else:
        archives_per_page = 24

    paginator = Paginator(results, archives_per_page)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(1)

    if json_request:
        response = json.dumps(
            {
                "archives": archive_search_result_to_json(request, results_page, request.user.is_authenticated),
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

    d = {
        "results": results_page,
        "extra_options": view_options,
        "prm": parameters,
        "display_prms": display_prms,
        "form": form,
        "form_simple": form_simple,
    }
    return render(request, "viewer/archive_search.html", d)


def filter_archives(
    request: HttpRequest,
    session_filters: dict[str, str],
    request_filters: dict[str, str],
    authenticated: bool,
    force_private: bool = False,
) -> QuerySet[Archive]:
    """Filter results through parameters
    and return results list.
    """

    if not authenticated and not force_private:
        results: QuerySet[Archive] = Archive.objects.filter(public=True)
        to_use_order_fields = archive_public_order_fields
    else:
        results = Archive.objects.all()
        to_use_order_fields = archive_order_fields

    results = results.filter(binned=False)

    # sort and filter results by parameters
    order = "posted"
    sorting_field = order
    if session_filters["sort"] and session_filters["sort"] in to_use_order_fields:
        order = session_filters["sort"]
        sorting_field = order
    if order == "rating":
        order = "gallery__" + order
    elif order == "posted":
        order = "gallery__" + order
    elif order == "category":
        order = "gallery__" + order

    if session_filters["asc_desc"] == "desc":
        results = results.order_by(F(order).desc(nulls_last=True))
    else:
        results = results.order_by(F(order).asc(nulls_last=True))

    if request_filters["title"]:
        q_formatted = "%" + request_filters["title"].replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))
    if request_filters["filename"]:
        results = results.filter(zipped__icontains=request_filters["filename"])
    if request_filters["rating_from"]:
        results = results.filter(gallery__rating__gte=float(request_filters["rating_from"]))
    if request_filters["rating_to"]:
        results = results.filter(gallery__rating__lte=float(request_filters["rating_to"]))
    if request_filters["filecount_from"]:
        results = results.filter(filecount__gte=int(float(request_filters["filecount_from"])))
    if request_filters["filecount_to"]:
        results = results.filter(filecount__lte=int(float(request_filters["filecount_to"])))
    if request_filters["filesize_from"]:
        results = results.filter(filesize__gte=int(float(request_filters["filesize_from"])))
    if request_filters["filesize_to"]:
        results = results.filter(filesize__lte=int(float(request_filters["filesize_to"])))
    if request_filters["posted_from"]:
        results = results.filter(gallery__posted__gte=request_filters["posted_from"])
    if request_filters["posted_to"]:
        results = results.filter(gallery__posted__lte=request_filters["posted_to"])
    if request_filters["match_type"]:
        results = results.filter(match_type__icontains=request_filters["match_type"])
    if request_filters["source_type"]:
        results = results.filter(source_type__icontains=request_filters["source_type"])
    if request_filters["reason"]:
        results = results.filter(reason__icontains=request_filters["reason"])
    if request_filters["uploader"]:
        results = results.filter(gallery__uploader__icontains=request_filters["uploader"])
    if request_filters["category"]:
        results = results.filter(gallery__category__icontains=request_filters["category"])

    if request_filters["tags"]:
        tags = request_filters["tags"].split(",")
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

                results = results.exclude(tag_query)
            elif tag.startswith("-"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.exclude(tag_query)
            elif tag.startswith("^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                results = results.filter(tag_query)
            else:
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.filter(tag_query)

        # Examples where distinct is needed:
        # Same name for a tag with 2 different scopes: artist:syukurin, group:syukurin
        results = results.distinct()

    if (
        "only_favorites" in request_filters
        and request_filters["only_favorites"]
        and authenticated
        and isinstance(request.user.id, int)
    ):
        user_arch_ids = UserArchivePrefs.objects.filter(user=request.user.id, favorite_group__gt=0).values_list(
            "archive"
        )
        results = results.filter(id__in=user_arch_ids)

    if "archive-group" in request_filters and request_filters["archive-group"]:
        try:
            archive_group_instance = ArchiveGroup.objects.get(title_slug=request_filters["archive-group"])
            if archive_group_instance.public or authenticated:
                results = results.filter(archive_groups=archive_group_instance)
                if "group-order" in request_filters and request_filters["group-order"]:
                    results = results.order_by("archivegroupentry")
        except ArchiveGroup.DoesNotExist:
            pass

    if "non_public" in request_filters and request_filters["non_public"]:
        results = results.filter(public=False)

    if "public" in request_filters and request_filters["public"]:
        results = results.filter(public=True)

    if session_filters["view"] == "list":
        if sorting_field in ("rating", "posted", "last_modified"):
            results = results.select_related("gallery")
    elif session_filters["view"] == "cover":
        pass
    elif session_filters["view"] == "extended":
        results = (
            results.select_related("gallery")
            .prefetch_related("archivetag_set__tag")
            .defer("gallery__comment", "gallery__provider_metadata")
        )

    results = results.defer("details", "origin")

    return results


def quick_search(request: HttpRequest, parameters: DataDict, display_parameters: DataDict) -> QuerySet[Archive]:
    """Quick search of archives."""
    # sort and filter results by parameters
    order = "posted"
    if parameters["sort"]:
        order = parameters["sort"]
    if order == "rating":
        order = "gallery__" + order
    elif order == "posted":
        order = "gallery__" + order
    elif order == "category":
        order = "gallery__" + order

    if parameters["asc_desc"] == "desc":
        # order = '-' + order
        results: QuerySet[Archive] = Archive.objects.order_by(F(order).desc(nulls_last=True))
    else:
        results = Archive.objects.order_by(F(order).asc(nulls_last=True))

    if not request.user.is_authenticated:
        results = results.filter(public=True)

    results = results.filter(binned=False)

    q_search = display_parameters["qsearch"]

    # URL search
    parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
    gallery_ids_providers = list()
    for parser in parsers:
        if parser.id_from_url_implemented():
            accepted_urls = parser.filter_accepted_urls((q_search,))
            gallery_ids_providers.extend([(parser.id_from_url(x), parser.name) for x in accepted_urls])

    if gallery_ids_providers:
        query = reduce(
            operator.or_, (Q(gallery__gid=gid, gallery__provider=provider) for gid, provider in gallery_ids_providers)
        )

        results_url: QuerySet[Archive] | None = results.filter(query)
    else:
        results_url = None

    # SHA1 search
    is_valid_sha1_string = valid_sha1_string(q_search)
    if is_valid_sha1_string:
        results_sha1 = results.filter(image__sha1=q_search)
    else:
        results_sha1 = None

    if not gallery_ids_providers and not is_valid_sha1_string:
        clean_up_qsearch = display_parameters["qsearch"].replace("	", " ")

        q_formatted = "%" + clean_up_qsearch.replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))

    if gallery_ids_providers:
        results = results_url or Archive.objects.none()
    if is_valid_sha1_string:
        results = results_sha1 or Archive.objects.none()

    if "non_public" in display_parameters and display_parameters["non_public"]:
        results = results.filter(public=False)

    if "public" in display_parameters and display_parameters["public"]:
        results = results.filter(public=True)

    if parameters["view"] == "list":
        results = results.select_related("gallery")
    elif parameters["view"] == "cover":
        pass
    elif parameters["view"] == "extended":
        results = results.select_related("gallery").prefetch_related("tags")

    results = results.defer("details")

    return results



def public_stats(request: HttpRequest) -> HttpResponse:
    """Display public galleries and archives stats."""
    if not crawler_settings.urls.enable_public_stats:
        if not request.user.is_staff:
            raise Http404("Page not found")
        else:
            return render_error(request, "Page disabled by settings (urls: enable_public_stats).")

    stats_dict = {
        "n_archives": Archive.objects.filter(public=True).count(),
        "n_galleries": Gallery.objects.filter(public=True).count(),
        "archive": Archive.objects.filter(public=True)
        .filter(filesize__gt=0)
        .aggregate(
            Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
        ),
        "gallery": Gallery.objects.filter(public=True)
        .filter(filesize__gt=0)
        .aggregate(
            Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
        ),
        "n_tags": Tag.objects.filter(archive_tags__public=True).distinct().count(),
        "top_10_tags": Tag.objects.filter(archive_tags__public=True)
        .distinct()
        .annotate(num_archive=Count("archive_tags"))
        .order_by("-num_archive")[:10],
        "top_10_artist_tags": Tag.objects.filter(scope="artist", archive_tags__public=True)
        .distinct()
        .annotate(num_archive=Count("archive_tags"))
        .order_by("-num_archive")[:10],
    }

    # Per category
    categories = Gallery.objects.filter(public=True).values_list("category", flat=True).distinct()

    categories_dict = {}

    for category in categories:
        categories_dict[category] = {
            "n_galleries": Gallery.objects.filter(public=True, category=category).count(),
            "gallery": Gallery.objects.filter(public=True)
            .filter(filesize__gt=0, category=category)
            .aggregate(
                Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
            ),
        }

    # Per language tag
    languages = (
        Tag.objects.filter(scope="language")
        .exclude(scope="language", name="translated")
        .annotate(num_gallery=Count("gallery"))
        .order_by("-num_gallery")
        .values_list("name", flat=True)
        .distinct()
    )

    languages_dict = {}

    languages_dict["untranslated"] = {
        "n_galleries": Gallery.objects.filter(public=True).exclude(tags__scope="language").distinct().count(),
        "gallery": Gallery.objects.filter(public=True)
        .filter(filesize__gt=0, tags__scope="language")
        .distinct()
        .aggregate(
            Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
        ),
    }

    for language in languages:
        languages_dict[str(language)] = {
            "n_galleries": Gallery.objects.filter(public=True)
            .filter(tags__scope="language", tags__name=language)
            .distinct()
            .count(),
            "gallery": Gallery.objects.filter(public=True)
            .filter(filesize__gt=0, tags__scope="language", tags__name=language)
            .distinct()
            .aggregate(
                Avg("filesize"), Max("filesize"), Min("filesize"), Sum("filesize"), Avg("filecount"), Sum("filecount")
            ),
        }

    d = {"stats": stats_dict, "gallery_categories": categories_dict, "gallery_languages": languages_dict}

    return render(request, "viewer/public_stats.html", d)


def filter_archives_simple(params: dict[str, Any], authenticated=False, show_binned=False) -> ArchiveQuerySet:
    """Filter results through parameters
    and return a result list.
    """
    if "sort_by" in params and params["sort_by"]:
        if authenticated:
            to_use_order_fields = archive_sort_by_fields
        else:
            to_use_order_fields = archive_public_sort_by_fields
        try:
            sort_objs = json.loads(params["sort_by"])
        except ValueError:
            sort_objs = [{"id": "posted", "desc": True}]
        order_arguments = [
            (
                F(x["id"].replace(".", "__")).desc(nulls_last=True)
                if x["desc"]
                else F(x["id"].replace(".", "__")).asc(nulls_last=True)
            )
            for x in sort_objs
            if x["id"] in to_use_order_fields
        ]
        if order_arguments:
            results: ArchiveQuerySet = Archive.objects.order_by(*order_arguments)
        else:
            results = Archive.objects.order_by(F("gallery__posted").desc(nulls_last=True))
    else:
        if authenticated:
            to_use_order_fields = archive_order_fields
        else:
            to_use_order_fields = archive_public_order_fields
        # sort and filter results by parameters
        order = "posted"
        if params["sort"] and params["sort"] in to_use_order_fields:
            order = params["sort"]
        if order == "rating":
            order = "gallery__" + order
        elif order == "posted":
            order = "gallery__" + order
        elif order == "category":
            order = "gallery__" + order

        if params["asc_desc"] == "desc":
            # order = '-' + order
            order_argument = F(order).desc(nulls_last=True)
        else:
            order_argument = F(order).asc(nulls_last=True)

        results = Archive.objects.order_by(order_argument)

    if authenticated and "gallery__status" in params and params["gallery__status"] and params["gallery__status"] != "0":
        results = results.filter(gallery__status=params["gallery__status"])

    if params["title"]:
        q_formatted = "%" + params["title"].replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))
    if params["filename"]:
        results = results.filter(zipped__icontains=params["filename"])
    if params["rating_from"]:
        results = results.filter(gallery__rating__gte=float(params["rating_from"]))
    if params["rating_to"]:
        results = results.filter(gallery__rating__lte=float(params["rating_to"]))
    if params["filecount_from"]:
        results = results.filter(filecount__gte=int(float(params["filecount_from"])))
    if params["filecount_to"]:
        results = results.filter(filecount__lte=int(float(params["filecount_to"])))
    if params["filesize_from"]:
        results = results.filter(filesize__gte=int(float(params["filesize_from"])))
    if params["filesize_to"]:
        results = results.filter(filesize__lte=int(float(params["filesize_to"])))
    if params["posted_from"]:
        results = results.filter(gallery__posted__gte=params["posted_from"])
    if params["posted_to"]:
        results = results.filter(gallery__posted__lte=params["posted_to"])

    if authenticated:

        if "extracted" in params and params["extracted"]:
            results = results.filter(extracted=True)
        if "created_from" in params and params["created_from"]:
            results = results.filter(create_date__gte=params["created_from"])
        if "created_to" in params and params["created_to"]:
            results = results.filter(create_date__lte=params["created_to"])
        if "extra_files" in params and params["extra_files"]:
            results = results.filter(archivefileentry__file_name__icontains=params["extra_files"])

    if params["match_type"]:
        results = results.filter(match_type__icontains=params["match_type"])
    if params["source_type"]:
        if "," in params["source_type"]:
            source_types = params["source_type"].split(",")
            for source_type in source_types:
                source_type = source_type.strip()
                if source_type.startswith("-"):
                    results = results.exclude(source_type__icontains=source_type[1:])
                else:
                    results = results.filter(source_type__icontains=source_type)
        else:
            if params["source_type"].startswith("-"):
                results = results.exclude(source_type__icontains=params["source_type"][1:])
            else:
                results = results.filter(source_type__icontains=params["source_type"])
    if params["reason"]:
        if "," in params["reason"]:
            reasons = params["reason"].split(",")
            for reason in reasons:
                reason = reason.strip()
                if reason.startswith("-"):
                    results = results.exclude(reason__icontains=reason[1:])
                else:
                    results = results.filter(reason__icontains=reason)
        else:
            if params["reason"].startswith("-"):
                results = results.filter(reason__icontains=params["reason"][1:])
            else:
                results = results.filter(reason__icontains=params["reason"])
    if params["uploader"]:
        results = results.filter(gallery__uploader__icontains=params["uploader"])
    if params["category"]:
        results = results.filter(gallery__category__icontains=params["category"])
    if params["provider"]:
        results = results.filter(gallery__provider__icontains=params["provider"])

    if params["tags"]:
        tags = params["tags"].split(",")
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

                results = results.exclude(tag_query)
            elif tag.startswith("-"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.exclude(tag_query)
            elif tag.startswith("^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__exact=tag_name) & Q(tags__scope__exact=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__exact=tag_name)
                else:
                    tag_query = Q(tags__scope__exact=tag_scope)

                results = results.filter(tag_query)
            else:
                if tag_name != "" and tag_scope != "":
                    tag_query = Q(tags__name__contains=tag_name) & Q(tags__scope__contains=tag_scope)
                elif tag_name != "":
                    tag_query = Q(tags__name__contains=tag_name)
                else:
                    tag_query = Q(tags__scope__contains=tag_scope)

                results = results.filter(tag_query)

        results = results.distinct()

    if "non_public" in params and params["non_public"]:
        results = results.filter(public=False)

    if "public" in params and params["public"]:
        results = results.filter(public=True)

    if "hidden" in params and params["hidden"]:
        results = results.filter(gallery__hidden=True)

    if not show_binned:
        results = results.filter(binned=False)

    if "view" in params:
        if params["view"] == "list":
            results = results.select_related("gallery")
        elif params["view"] == "cover":
            pass
        elif params["view"] == "extended":
            results = results.select_related("gallery").prefetch_related("tags")

    return results



def filter_wanted_galleries_simple(params: dict[str, Any]) -> QuerySet[WantedGallery]:
    """Filter results through parameters
    and return results list.
    """
    # sort and filter results by parameters
    order = "release_date"
    if "sort" in params and params["sort"] and params["sort"] in wanted_gallery_order_fields:
        order = params["sort"]
    if "asc_desc" in params and params["asc_desc"] == "desc":
        order = "-" + order

    results: QuerySet[WantedGallery] = WantedGallery.objects.order_by(order)

    if params["title"]:
        q_formatted = "%" + params["title"].replace(" ", "%") + "%"
        results = results.filter(
            Q(title__ss=q_formatted)
            | Q(title_jpn__ss=q_formatted)
            | Q(search_title__ss=q_formatted)
            | Q(unwanted_title__ss=q_formatted)
        )
    if params["wanted_page_count_lower"]:
        results = results.filter(wanted_page_count_lower=int(params["wanted_page_count_lower"]))
    if params["wanted_page_count_upper"]:
        results = results.filter(wanted_page_count_upper=int(params["wanted_page_count_upper"]))
    if params["provider"]:
        results = results.filter(wanted_providers__slug=params["provider"]).distinct()
    if params["not_used"]:
        results = results.filter(Q(archive__isnull=True))
    if params["wanted-should-search"]:
        results = results.filter(should_search=True)
    if params["restricted-to-links"]:
        results = results.filter(restricted_to_links=True)
    if params["wanted-should-search-not"]:
        results = results.filter(should_search=False)
    if params["book_type"]:
        results = results.filter(book_type=params["book_type"])
    if params["publisher"]:
        results = results.filter(publisher=params["publisher"])
    if params["reason"]:
        results = results.filter(reason=params["reason"])
    if params["wanted-found"]:
        results = results.filter(found=True)
    if params["wanted-not-found"]:
        results = results.filter(found=False)
    if params["wanted-no-found-galleries"]:
        results = results.annotate(founds=Count("found_galleries")).filter(founds=0)
    if params["with-possible-matches"]:
        results = results.annotate(num_possible=Count("possible_matches")).filter(num_possible__gt=0)
    if params["mention-source"]:
        results = results.filter(mentions__source=params["mention-source"]).distinct()

    if params["tags"]:
        tags = params["tags"].split(",")
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
                    tag_query = (
                        Q(wanted_tags__name__exact=tag_name) & Q(wanted_tags__scope__exact=tag_scope)
                    ) | (Q(unwanted_tags__name__exact=tag_name) & Q(unwanted_tags__scope__exact=tag_scope))
                elif tag_name != "":
                    tag_query = Q(wanted_tags__name__exact=tag_name) | Q(unwanted_tags__name__exact=tag_name)
                else:
                    tag_query = Q(wanted_tags__scope__exact=tag_scope) | Q(unwanted_tags__scope__exact=tag_scope)
                results = results.exclude(tag_query)
            elif tag.startswith("-"):
                if tag_name != "" and tag_scope != "":
                    tag_query = (
                        Q(wanted_tags__name__contains=tag_name) & Q(wanted_tags__scope__contains=tag_scope)
                    ) | (Q(unwanted_tags__name__contains=tag_name) & Q(unwanted_tags__scope__contains=tag_scope))
                elif tag_name != "":
                    tag_query = Q(wanted_tags__name__contains=tag_name) | Q(unwanted_tags__name__contains=tag_name)
                else:
                    tag_query = Q(wanted_tags__scope__contains=tag_scope) | Q(unwanted_tags__scope__contains=tag_scope)
                results = results.exclude(tag_query)
            elif tag.startswith("^"):
                if tag_name != "" and tag_scope != "":
                    tag_query = (Q(wanted_tags__name__exact=tag_name) & Q(wanted_tags__scope__exact=tag_scope)) | (
                        Q(unwanted_tags__name__exact=tag_name) & Q(unwanted_tags__scope__exact=tag_scope)
                    )
                elif tag_name != "":
                    tag_query = Q(wanted_tags__name__exact=tag_name) | Q(unwanted_tags__name__exact=tag_name)
                else:
                    tag_query = Q(wanted_tags__scope__exact=tag_scope) | Q(unwanted_tags__scope__exact=tag_scope)
                results = results.filter(tag_query)
            else:
                if tag_name != "" and tag_scope != "":
                    tag_query = (
                        Q(wanted_tags__name__contains=tag_name) & Q(wanted_tags__scope__contains=tag_scope)
                    ) | (Q(unwanted_tags__name__contains=tag_name) & Q(unwanted_tags__scope__contains=tag_scope))
                elif tag_name != "":
                    tag_query = Q(wanted_tags__name__contains=tag_name) | Q(unwanted_tags__name__contains=tag_name)
                else:
                    tag_query = Q(wanted_tags__scope__contains=tag_scope) | Q(unwanted_tags__scope__contains=tag_scope)
                results = results.filter(tag_query)

        results = results.distinct()

    return results



