"""GET command handlers for the public JSON API."""

import json
from collections import defaultdict
from typing import Any, Optional

from django.core.paginator import Paginator, EmptyPage
from django.db.models import Q, QuerySet, Prefetch
from django.http import HttpResponse, HttpRequest, HttpResponseNotFound
from django.http.request import QueryDict
from django.urls import reverse

from core.base.utilities import str_to_int, timestamp_or_zero
from viewer.models import (
    Archive,
    Gallery,
    ArchiveGroup,
    ArchiveGroupEntry,
    WantedGallery,
    FoundGallery,
)
from viewer.utils.functions import (
    gallery_search_results_to_json,
    gallery_search_dict_to_json,
    archive_search_result_to_json,
    images_data_to_json,
    archive_group_entry_to_json,
    archive_entry_archive_to_json,
    wanted_gallery_to_json,
)
from viewer.views.head import (
    gallery_filter_keys,
    filter_archives_simple,
    archive_filter_keys,
    wanted_gallery_filter_keys,
    filter_wanted_galleries_simple,
)
from viewer.views.api.common import _get_int, _get_str, get_galleries_from_request
from viewer.views.api.filters import filter_galleries_no_request, simple_archive_filter

ACCEPTED_PER_PAGE = [24, 48, 100, 200, 300]

def handle_get_archive(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_id = _get_int(data, "archive")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive = Archive.objects.get(pk=archive_id)
    except Archive.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )

    if not archive.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "title": archive.title,
            "title_jpn": archive.title_jpn,
            "category": archive.gallery.category if archive.gallery else "",
            "uploader": archive.gallery.uploader if archive.gallery else "",
            "posted": int(timestamp_or_zero(archive.gallery.posted)) if archive.gallery else "",
            "filecount": archive.filecount,
            "filesize": archive.filesize,
            "crc32": archive.crc32,
            "expunged": archive.gallery.expunged if archive.gallery else "",
            "disowned": archive.gallery.disowned if archive.gallery else "",
            "rating": float(str_to_int(archive.gallery.rating)) if archive.gallery else "",
            "fjord": archive.gallery.fjord if archive.gallery else "",
            "tags": archive.tag_list(),
            "download": reverse("viewer:archive-download", args=(archive.pk,)),
            "gallery": archive.gallery.pk if archive.gallery else "",
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_archives(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_ids: list[str] = data.getlist("archives", [])
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive_ids:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")

    try:
        [int(x) for x in archive_ids]
    except ValueError:
        return HttpResponse(
            json.dumps({"result": "Invalid Archive ID."}), content_type="application/json; charset=utf-8"
        )
    try:
        if user_is_authenticated:
            archives = Archive.objects.filter(pk__in=archive_ids)
        else:
            archives = Archive.objects.filter(pk__in=archive_ids, public=True)
    except Archive.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        archive_search_result_to_json(request, archives, user_is_authenticated),
        # indent=2,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get tags from a specific archive.


def handle_get_at(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_id = _get_int(data, "at")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive = Archive.objects.get(pk=archive_id)
    except Archive.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "tags": archive.tag_list_sorted(),
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get hashes from a specific archive.


def handle_get_ah(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_id = _get_int(data, "ah")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive = Archive.objects.get(pk=archive_id)
    except Archive.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "image_hashes": [x.sha1 for x in archive.image_set.all()],
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get other files data from a specific archive.


def handle_get_aof(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_id = _get_int(data, "aof")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive = Archive.objects.get(pk=archive_id)
    except Archive.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "other_files": [
                {
                    "name": x.file_name,
                    "size": x.file_size,
                    "sha1": x.sha1,
                }
                for x in archive.archivefileentry_set.all()
            ],
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get images data from a list of archives.


def handle_get_aid(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_ids = data.getlist("aid", [])
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive_ids:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
    try:
        [int(x) for x in archive_ids]
    except ValueError:
        return HttpResponse(
            json.dumps({"result": "Invalid Archive ID."}), content_type="application/json; charset=utf-8"
        )
    try:
        if user_is_authenticated:
            archives = Archive.objects.filter(pk__in=archive_ids)
        else:
            archives = Archive.objects.filter(pk__in=archive_ids, public=True)
    except Archive.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {a.pk: images_data_to_json(a.image_set.all()) for a in archives},
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get fields from a specific gallery.


def handle_get_gallery(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        gallery_id = _get_int(data, "gallery")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        gallery = Gallery.objects.get(pk=gallery_id)
    except Gallery.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not gallery.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "title": gallery.title,
            "title_jpn": gallery.title_jpn,
            "category": gallery.category,
            "uploader": gallery.uploader,
            "posted": int(timestamp_or_zero(gallery.posted)),
            "filecount": gallery.filecount,
            "filesize": gallery.filesize,
            "expunged": gallery.expunged,
            "disowned": gallery.disowned,
            "rating": float(str_to_int(gallery.rating)),
            "fjord": gallery.fjord,
            "link": gallery.get_link(),
            "tags": gallery.tag_list(),
            "archives": [
                {"id": archive.id, "download": reverse("viewer:archive-download", args=(archive.pk,))}
                for archive in gallery.archive_set.filter_by_authenticated_status(authenticated=user_is_authenticated)
            ],
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get fields from a specific gallery (Using gid).


def _gallery_by_gid_to_json(gallery: Gallery, user_is_authenticated: bool) -> dict[str, Any]:
    return {
        "gid": gallery.gid,
        "title": gallery.title,
        "title_jpn": gallery.title_jpn,
        "category": gallery.category,
        "uploader": gallery.uploader,
        "posted": int(timestamp_or_zero(gallery.posted)),
        "filecount": gallery.filecount,
        "filesize": gallery.filesize,
        "expunged": gallery.expunged,
        "disowned": gallery.disowned,
        "rating": float(str_to_int(gallery.rating)),
        "fjord": gallery.fjord,
        "link": gallery.get_link(),
        "tags": gallery.tag_list(),
        "archives": [
            {"id": archive.id, "download": reverse("viewer:archive-download", args=(archive.pk,))}
            for archive in gallery.archive_set.filter_by_authenticated_status(authenticated=user_is_authenticated)
        ],
    }


def _gallery_for_gid(data: QueryDict, gallery_gid: str | list[object]) -> Optional[Gallery]:
    if "provider" in data:
        return Gallery.objects.filter_first(gid=gallery_gid, provider=data["provider"])
    return Gallery.objects.filter_first(gid=gallery_gid)


def handle_get_gid(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    gallery = _gallery_for_gid(data, data["gid"])
    if not gallery:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not gallery.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        _gallery_by_gid_to_json(gallery, user_is_authenticated),
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_gids(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    gallery_gids = data.getlist("gids", [])
    if not gallery_gids:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")

    galleries = []
    for gallery_gid in gallery_gids:
        gallery = _gallery_for_gid(data, gallery_gid)
        if gallery and (gallery.public or user_is_authenticated):
            galleries.append(_gallery_by_gid_to_json(gallery, user_is_authenticated))

    response = json.dumps(
        galleries,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get tags from a specific Gallery.


def handle_get_gt(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        gallery_id = _get_int(data, "gt")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        gallery = Gallery.objects.get(pk=gallery_id)
    except Gallery.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not gallery.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "tags": gallery.tag_list_sorted(),
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get fields from several archives by one of its images sha1 value.


def handle_get_sha1(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    archives = Archive.objects.filter(image__sha1=data["sha1"]).select_related("gallery").prefetch_related("tags")

    if not user_is_authenticated:
        archives = archives.filter(public=True)

    if not archives:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")

    response = json.dumps(
        [
            {
                "id": archive.id,
                "title": archive.title,
                "title_jpn": archive.title_jpn,
                "category": archive.gallery.category if archive.gallery else "",
                "uploader": archive.gallery.uploader if archive.gallery else "",
                "posted": int(timestamp_or_zero(archive.gallery.posted)) if archive.gallery else "",
                "filecount": archive.filecount,
                "filesize": archive.filesize,
                "expunged": archive.gallery.expunged if archive.gallery else "",
                "disowned": archive.gallery.disowned if archive.gallery else "",
                "rating": float(str_to_int(archive.gallery.rating)) if archive.gallery else "",
                "fjord": archive.gallery.fjord if archive.gallery else "",
                "tags": archive.tag_list(),
                "download": reverse("viewer:archive-download", args=(archive.pk,)),
                "gallery": archive.gallery.pk if archive.gallery else "",
            }
            for archive in archives
        ],
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get reduced number of fields from several archives by doing filtering.


def handle_get_qa(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):

    archive_args = data.copy()

    params: dict[str, str] = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in archive_args.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_archives_simple(params, authenticated=user_is_authenticated).prefetch_related("tags")

    if not user_is_authenticated:
        results = results.filter(public=True).order_by("-public_date")

    response = json.dumps(
        [
            {
                "id": o.pk,
                "title": o.title,
                "tags": o.tag_list(),
                "url": reverse("viewer:archive-download", args=(o.pk,)),
            }
            for o in results
        ]
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get reduced number of fields from several archives by doing a simple filtering.


def handle_get_q(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    q_args = _get_str(data, "q")
    if not user_is_authenticated:
        results = simple_archive_filter(q_args, public=True).prefetch_related("tags")
    else:
        results = simple_archive_filter(q_args, public=False).prefetch_related("tags")
    response = json.dumps(
        [
            {
                "id": o.pk,
                "title": o.title,
                "tags": o.tag_list(),
                "url": reverse("viewer:archive-download", args=(o.pk,)),
            }
            for o in results
        ]
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get galleries associated with archive by crc32, used with matcher.


def handle_get_match(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    galleries_matcher = get_galleries_from_request(data, user_is_authenticated)
    if not galleries_matcher:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
    response = json.dumps(
        [
            {
                "id": gallery.pk,
                "gid": gallery.gid,
                "token": gallery.token,
                "title": gallery.title,
                "title_jpn": gallery.title_jpn,
                "category": gallery.category,
                "uploader": gallery.uploader,
                "comment": gallery.comment,
                "posted": int(timestamp_or_zero(gallery.posted)),
                "filecount": gallery.filecount,
                "filesize": gallery.filesize,
                "expunged": gallery.expunged,
                "disowned": gallery.disowned,
                "provider": gallery.provider,
                "rating": gallery.rating,
                "reason": gallery.reason,
                "fjord": gallery.fjord,
                "tags": gallery.tag_list(),
                "link": gallery.get_link(),
                "thumbnail": (
                    request.build_absolute_uri(reverse("viewer:gallery-thumb", args=(gallery.pk,)))
                    if gallery.thumbnail
                    else ""
                ),
                "thumbnail_url": gallery.thumbnail_url,
                "gallery_container": gallery.gallery_container.gid if gallery.gallery_container else "",
                "parent_gid": gallery.parent_gallery.gid if gallery.parent_gallery else "",
                "first_gid": gallery.first_gallery.gid if gallery.first_gallery else "",
                "magazine": gallery.magazine.gid if gallery.magazine else "",
            }
            for gallery in galleries_matcher
        ],
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get fields from several galleries by doing a simple filtering.


def handle_get_g(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    results_gallery = get_galleries_from_request(data, user_is_authenticated)
    if not results_gallery:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
    response = json.dumps(
        [
            {
                "title": gallery.title,
                "title_jpn": gallery.title_jpn,
                "category": gallery.category,
                "uploader": gallery.uploader,
                "posted": int(timestamp_or_zero(gallery.posted)),
                "filecount": gallery.filecount,
                "filesize": gallery.filesize,
                "expunged": gallery.expunged,
                "disowned": gallery.disowned,
                "source": gallery.provider,
                "rating": float(str_to_int(gallery.rating)),
                "fjord": gallery.fjord,
                "tags": gallery.tag_list(),
            }
            for gallery in results_gallery
        ],
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# this part should be used in conjunction with json crawler provider, to transfer easily already fetched links.
# Get more fields from several galleries by doing a simple filtering.


def handle_get_gc(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    results_gallery = get_galleries_from_request(data, user_is_authenticated)
    if not results_gallery:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
    response = json.dumps(
        [
            {
                "gid": gallery.gid,
                "token": gallery.token,
                "title": gallery.title,
                "title_jpn": gallery.title_jpn,
                "category": gallery.category,
                "uploader": gallery.uploader,
                "posted": int(timestamp_or_zero(gallery.posted)),
                "filecount": gallery.filecount,
                "filesize": gallery.filesize,
                "expunged": gallery.expunged,
                "disowned": gallery.disowned,
                "provider": gallery.provider,
                "rating": gallery.rating,
                "fjord": gallery.fjord,
                "tags": gallery.tag_list(),
                "link": gallery.get_link(),
            }
            for gallery in results_gallery
        ],
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# this part should be used in conjunction with json crawler provider, to transfer easily already fetched links.
# Get more fields from several galleries by doing a simple filtering.
# More complete version of the last one, since you also get the archives (DL link only, use the gallery data
# to create the final archive).
# Gallery search, no pagination


def handle_get_gs(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):

    args = data.copy()

    for k in gallery_filter_keys:
        if k not in args:
            args[k] = ""

    keys = ("sort", "asc_desc")

    for k in keys:
        if k not in args:
            args[k] = ""

    # args = data
    if not user_is_authenticated:
        args["public"] = "1"
        used_prefetch = Prefetch(
            "archive_set", queryset=Archive.objects.filter(public=True), to_attr="available_archives"
        )
    else:
        args["public"] = ""
        used_prefetch = Prefetch("archive_set", to_attr="available_archives")
    results_gallery = filter_galleries_no_request(args).prefetch_related("tags", used_prefetch)
    if not results_gallery:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
    response = json.dumps(
        gallery_search_results_to_json(request, results_gallery),
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Gallery search with pagination


def handle_get_gsp(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):

    args = data.copy()

    for k in gallery_filter_keys:
        if k not in args:
            args[k] = ""

    keys = ("sort", "asc_desc")

    for k in keys:
        if k not in args:
            args[k] = ""

    # args = data
    if not user_is_authenticated:
        args["public"] = "1"
        used_prefetch = Prefetch(
            "archive_set", queryset=Archive.objects.filter(public=True), to_attr="available_archives"
        )
    else:
        args["public"] = ""
        used_prefetch = Prefetch("archive_set", to_attr="available_archives")
    results_gallery = filter_galleries_no_request(args).prefetch_related("tags", used_prefetch)

    try:
        per_page = int(args.get("count", "48"))
        if per_page not in ACCEPTED_PER_PAGE:
            per_page = 48
    except ValueError:
        per_page = 48

    paginator = Paginator(results_gallery, per_page)
    try:
        page = int(args.get("page", "1"))
    except ValueError:
        page = 1
    try:
        results_page = paginator.page(page)
    except EmptyPage:
        # If page is out of range (e.g. 9999), deliver last page of results.
        results_page = paginator.page(paginator.num_pages)

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


# Gallery data


def handle_get_gd(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        gallery_id = _get_int(data, "gd")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        gallery = Gallery.objects.get(pk=gallery_id)
    except Gallery.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not gallery.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
        )
    response = json.dumps(
        {
            "id": gallery.pk,
            "gid": gallery.gid,
            "token": gallery.token,
            "title": gallery.title,
            "title_jpn": gallery.title_jpn,
            "category": gallery.category,
            "uploader": gallery.uploader,
            "comment": gallery.comment,
            "posted": int(timestamp_or_zero(gallery.posted)),
            "filecount": gallery.filecount,
            "filesize": gallery.filesize,
            "expunged": gallery.expunged,
            "disowned": gallery.disowned,
            "provider": gallery.provider,
            "rating": gallery.rating,
            "fjord": gallery.fjord,
            "tags": gallery.tag_list(),
            "link": gallery.get_link(),
            "thumbnail": (
                request.build_absolute_uri(reverse("viewer:gallery-thumb", args=(gallery.pk,)))
                if gallery.thumbnail
                else ""
            ),
            "thumbnail_url": gallery.thumbnail_url,
            "archives": [
                {
                    "link": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
                    "source": archive.source_type,
                    "reason": archive.reason,
                }
                for archive in gallery.archive_set.filter_by_authenticated_status(authenticated=user_is_authenticated)
            ],
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Archive search, no pagination (Used to download from another instance,
# that's why search is done on archives, and it returns Gallery info)


def handle_get_as(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):

    archive_args = data.copy()

    params = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in archive_args.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ""

    results_archive = (
        filter_archives_simple(params, authenticated=user_is_authenticated)
        .select_related("gallery")
        .prefetch_related("gallery__tags")
    )

    if not user_is_authenticated:
        results_archive = results_archive.filter(public=True).order_by("-public_date")

    if not results_archive:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")

    found_galleries = defaultdict(list)

    for archive in results_archive:
        if archive.gallery:
            found_galleries[archive.gallery].append(archive)

    response = json.dumps(
        gallery_search_dict_to_json(request, found_galleries),
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


# Get fields from a specific gallery.


def handle_get_archive_group(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_group_id = _get_int(data, "archive-group")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive_group = ArchiveGroup.objects.get(pk=archive_group_id)
    except ArchiveGroup.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive_group.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
        )

    archive_group_entries = (
        ArchiveGroupEntry.objects.filter_by_authenticated_status(
            authenticated=user_is_authenticated, archive_group=archive_group
        )
        .select_related("archive")
        .prefetch_related(
            Prefetch(
                "archive__tags",
            )
        )
    )

    response = json.dumps(
        {
            "id": archive_group.id,
            "title": archive_group.title,
            "title_slug": archive_group.title_slug,
            "details": archive_group.details,
            "position": archive_group.position,
            "public": archive_group.public,
            "create_date": int(timestamp_or_zero(archive_group.create_date)),
            "last_modified": int(timestamp_or_zero(archive_group.last_modified)),
            "archive_group_entries": [
                archive_group_entry_to_json(archive_entry, user_is_authenticated, request)
                for archive_entry in archive_group_entries
            ],
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_archive_group_entry(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_group_entry_id = _get_int(data, "archive-group-entry")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroupEntry does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    try:
        archive_group_entry = ArchiveGroupEntry.objects.get(pk=archive_group_entry_id)
    except ArchiveGroupEntry.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroupEntry does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    if (
        not (archive_group_entry.archive_group.public or archive_group_entry.archive.public)
        and not user_is_authenticated
    ):
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroupEntry does not exist."}),
            content_type="application/json; charset=utf-8",
        )

    response = json.dumps(
        archive_group_entry_to_json(archive_group_entry, user_is_authenticated, request),
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_archive_group_entry_archive(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_id = _get_int(data, "archive-group-entry-archive")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive = Archive.objects.get(pk=archive_id)
    except ArchiveGroupEntry.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not archive.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )

    response = json.dumps(
        archive_entry_archive_to_json(archive, user_is_authenticated, request),
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_archive_wanted_image(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        archive_id = _get_int(data, "archive-wanted-image")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        archive = Archive.objects.get(pk=archive_id)
    except ArchiveGroupEntry.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )
    if not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
        )

    result, result_data = archive.get_wanted_images_similarity_mark()
    if result == -1:
        return HttpResponseNotFound(
            json.dumps({"result": "Could not run Image Match."}), content_type="application/json; charset=utf-8"
        )
    elif result == -2:
        return HttpResponseNotFound(
            json.dumps({"result": "No active WantedImages."}), content_type="application/json; charset=utf-8"
        )

    response = json.dumps(
        {
            "archive": archive_entry_archive_to_json(archive, user_is_authenticated, request),
            "matches": [
                {
                    "url": x[0].get_image_url(),
                    "name": x[0].image_name,
                    "minimum_features": x[0].minimum_features,
                    "good_matches": x[1],
                    "found_match": x[2],
                    "found_image": "data:image/jpeg;base64," + x[3].decode("utf-8") if x[3] else None,
                }
                for x in result_data
            ],
        },
        # indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def _include_found_galleries(data: QueryDict) -> bool:
    return "include_found_galleries" in data


def _wanted_galleries_prefetch(include_found_galleries: bool = False) -> QuerySet[WantedGallery]:
    prefetches: list[str | Prefetch] = [
        "wanted_tags",
        "unwanted_tags",
        "wanted_providers",
        "unwanted_providers",
        "categories",
    ]
    if include_found_galleries:
        prefetches.append(
            Prefetch(
                "foundgallery_set",
                queryset=FoundGallery.objects.select_related("gallery").prefetch_related("gallery__tags"),
            )
        )
    return WantedGallery.objects.prefetch_related(*prefetches)


def handle_get_wanted_gallery(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        wanted_gallery_id = _get_int(data, "wanted-gallery")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "WantedGallery does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    include_found_galleries = _include_found_galleries(data)
    try:
        wanted_gallery = _wanted_galleries_prefetch(include_found_galleries).get(pk=wanted_gallery_id)
    except WantedGallery.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "WantedGallery does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    if not wanted_gallery.public and not user_is_authenticated:
        return HttpResponseNotFound(
            json.dumps({"result": "WantedGallery does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    response = json.dumps(
        wanted_gallery_to_json(
            wanted_gallery,
            request=request,
            user_is_authenticated=user_is_authenticated,
            include_found_galleries=include_found_galleries,
        ),
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_wanted_galleries(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    try:
        wanted_gallery_ids: list[str] = data.getlist("wanted-galleries", [])
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "WantedGallery does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    if not wanted_gallery_ids:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")

    try:
        [int(x) for x in wanted_gallery_ids]
    except ValueError:
        return HttpResponse(
            json.dumps({"result": "Invalid WantedGallery ID."}),
            content_type="application/json; charset=utf-8",
        )
    include_found_galleries = _include_found_galleries(data)
    if user_is_authenticated:
        wanted_galleries = _wanted_galleries_prefetch(include_found_galleries).filter(pk__in=wanted_gallery_ids)
    else:
        wanted_galleries = _wanted_galleries_prefetch(include_found_galleries).filter(
            pk__in=wanted_gallery_ids, public=True
        )

    response = json.dumps(
        [
            wanted_gallery_to_json(
                wanted_gallery,
                request=request,
                user_is_authenticated=user_is_authenticated,
                include_found_galleries=include_found_galleries,
            )
            for wanted_gallery in wanted_galleries
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")


def handle_get_wanted_galleries_search(request: HttpRequest, data: QueryDict, user_is_authenticated: bool):
    args = data.copy()

    for k in wanted_gallery_filter_keys:
        if k not in args:
            args[k] = ""

    keys = ("sort", "asc_desc")
    for k in keys:
        if k not in args:
            args[k] = ""

    if not user_is_authenticated:
        results = filter_wanted_galleries_simple(args).filter(public=True)
    else:
        results = filter_wanted_galleries_simple(args)

    results = results.prefetch_related(
        "wanted_tags",
        "unwanted_tags",
        "wanted_providers",
        "unwanted_providers",
        "categories",
    )

    if not results:
        return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")

    response = json.dumps(
        [wanted_gallery_to_json(wanted_gallery) for wanted_gallery in results],
        sort_keys=True,
        ensure_ascii=False,
    )
    return HttpResponse(response, content_type="application/json; charset=utf-8")

