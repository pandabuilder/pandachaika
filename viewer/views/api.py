# This file is intended for views designed to interact via JSON with
# other tools (userscript, happypanda, etc.)

import json
import logging
import re
from collections import defaultdict

from typing import Any, Union, Optional

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.models import User, AnonymousUser
from django.core.paginator import Paginator, EmptyPage
from django.db import transaction
from django.db.models import Q, QuerySet, Prefetch
from django.http import (
    HttpResponse,
    HttpRequest,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.http.request import QueryDict
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

from core.base.setup import Settings
from core.base.utilities import str_to_int, timestamp_or_zero
from viewer.models import Archive, Gallery, UserArchivePrefs, ArchiveGroup, ArchiveGroupEntry, WantedGallery, Tag, \
    Category, Provider
from viewer.utils.matching import generate_possible_matches_for_archives
from viewer.utils.requests import authenticate_by_token, double_check_auth
from viewer.views.head import gallery_filter_keys, gallery_order_fields, filter_archives_simple, archive_filter_keys
from viewer.utils.functions import (
    gallery_search_results_to_json,
    gallery_search_dict_to_json,
    archive_search_result_to_json,
    images_data_to_json,
    archive_group_entry_to_json,
    archive_entry_archive_to_json,
)

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


def get_galleries_from_request(data: QueryDict, user_is_authenticated: bool) -> QuerySet[Gallery]:
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
    else:
        args["public"] = ""
    galleries_matcher = filter_galleries_no_request(args).prefetch_related("tags")
    return galleries_matcher


@csrf_exempt
def api_login(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        if not username or not password:
            return HttpResponse(
                json.dumps({"success": False, "message": "Username or password is empty."}),
                content_type="application/json",
            )
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                data: dict[str, Any] = {"success": True}
            else:
                data = {"success": False, "message": "This account has been disabled."}
        else:
            data = {"success": False, "message": "Invalid login credentials."}

        return HttpResponse(json.dumps(data), content_type="application/json")

    return HttpResponseBadRequest()


@csrf_exempt
def api_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    data = {"success": True, "message": "Logged out."}
    return HttpResponse(json.dumps(data), content_type="application/json")


def json_api_handle_get(request: HttpRequest, user_is_authenticated: bool):
    data = request.GET
    # Get fields from a specific archive.
    if "archive" in data:
        try:
            archive_id = int(data["archive"])
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
    elif "archives" in data:
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
    elif "at" in data:
        try:
            archive_id = int(data["at"])
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
    elif "ah" in data:
        try:
            archive_id = int(data["ah"])
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
    elif "aof" in data:
        try:
            archive_id = int(data["aof"])
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
    elif "aid" in data:
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
    elif "gallery" in data:
        try:
            gallery_id = int(data["gallery"])
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
                    for archive in gallery.archive_set.filter_by_authenticated_status(
                        authenticated=user_is_authenticated
                    )
                ],
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")
    # Get fields from a specific gallery (Using gid).
    elif "gid" in data:
        gallery_gid = data["gid"]
        if "provider" in data:
            possible_gallery = Gallery.objects.filter_first(gid=gallery_gid, provider=data["provider"])
        else:
            possible_gallery = Gallery.objects.filter_first(gid=gallery_gid)
        if not possible_gallery:
            return HttpResponseNotFound(
                json.dumps({"result": "Gallery does not exist."}), content_type="application/json; charset=utf-8"
            )
        else:
            gallery = possible_gallery
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
                    for archive in gallery.archive_set.filter_by_authenticated_status(
                        authenticated=user_is_authenticated
                    )
                ],
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")
    # Get tags from a specific Gallery.
    elif "gt" in data:
        try:
            gallery_id = int(data["gt"])
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
    elif "sha1" in data:
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
    elif "qa" in data:

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
    elif "q" in data:
        q_args = data["q"]
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
    elif "match" in data:
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
    elif "g" in data:
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
    elif "gc" in data:
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
    elif "gs" in data:

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
    elif "gsp" in data:

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

        paginator = Paginator(results_gallery, 48)
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
    elif "gd" in data:
        try:
            gallery_id = int(data["gd"])
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
                    for archive in gallery.archive_set.filter_by_authenticated_status(
                        authenticated=user_is_authenticated
                    )
                ],
            },
            # indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        return HttpResponse(response, content_type="application/json; charset=utf-8")
    # Archive search, no pagination (Used to download from another instance,
    # that's why search is done on archives, and it returns Gallery info)
    elif "as" in data:

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
    elif "archive-group" in data:
        try:
            archive_group_id = int(data["archive-group"])
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
    elif "archive-group-entry" in data:
        try:
            archive_group_entry_id = int(data["archive-group-entry"])
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
    elif "archive-group-entry-archive" in data:
        try:
            archive_id = int(data["archive-group-entry-archive"])
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
    elif "archive-wanted-image" in data:
        try:
            archive_id = int(data["archive-wanted-image"])
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
    else:
        return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")

def json_api_handle_post(request: HttpRequest, user: Optional[User | AnonymousUser]):
    data = request.GET
    body = json.loads(request.body)
    if "archive-group" in data and user and user.has_perm("viewer.change_archivegroup"):
        try:
            archive_group = ArchiveGroup(
                title=body["title"],
                title_slug=body["title_slug"],
            )

            archive_group.title = body["title"]
            archive_group.details = body["details"]
            archive_group.position = body["position"]

            archive_group.save()

            archive_group_entries = []

            for entry in body["archive_group_entries"]:
                try:
                    archive = Archive.objects.get(pk=entry["archive"]["id"])
                    archive_group_entry = ArchiveGroupEntry(
                        title=entry["title"] if "title" in entry else "",
                        position=entry["position"] if "position" in entry else None,
                        archive=archive,
                        archive_group=archive_group,
                    )
                    archive_group_entry.save()
                    archive_group_entries.append(archive_group_entry)
                except Archive.DoesNotExist:
                    return HttpResponseNotFound(
                        json.dumps({"result": "Archive does not exist."}),
                        content_type="application/json; charset=utf-8",
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
                        archive_group_entry_to_json(archive_entry, True, request)
                        for archive_entry in sorted(archive_group_entries, key=lambda x: x.position or 0)
                    ],
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")

        except ArchiveGroup.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
            )
    elif "archive-group-entry" in data and user and user.has_perm("viewer.change_archivegroupentry"):
        try:
            archive_group_id = int(data["archive-group-entry"])
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

        try:
            archive = Archive.objects.get(pk=body["archive"]["id"])
            archive_group_entry = ArchiveGroupEntry(
                title=body["title"] if "title" in body else "",
                position=body["position"] if "position" in body else None,
                archive=archive,
                archive_group=archive_group,
            )
            archive_group_entry.save()

            response = json.dumps(
                archive_group_entry_to_json(archive_group_entry, True, request),
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )

            return HttpResponse(response, content_type="application/json; charset=utf-8")

        except Archive.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "Archive does not exist."}), content_type="application/json; charset=utf-8"
            )
    elif "wanted-gallery" in data and user and user.has_perm("viewer.add_wantedgallery"):
        try:
            wanted_gallery = WantedGallery(
                title=body.get("title", ""),
                title_jpn=body.get("title_jpn", ""),
                search_title=body.get("search_title", ""),
                regexp_search_title=body.get("regexp_search_title", False),
                regexp_search_title_icase=body.get("regexp_search_title_icase", False),
                unwanted_title=body.get("unwanted_title", ""),
                regexp_unwanted_title=body.get("regexp_unwanted_title", False),
                regexp_unwanted_title_icase=body.get("regexp_unwanted_title_icase", False),
                wanted_page_count_lower=body.get("wanted_page_count_lower", 0),
                wanted_page_count_upper=body.get("wanted_page_count_upper", 0),
                match_expression=body.get("match_expression", None),
                wanted_tags_exclusive_scope=body.get("wanted_tags_exclusive_scope", False),
                exclusive_scope_name=body.get("exclusive_scope_name", ""),
                wanted_tags_accept_if_none_scope=body.get("wanted_tags_accept_if_none_scope", ""),
                category=body.get("category", ""),
                wait_for_time=body.get("wait_for_time"),
                should_search=body.get("should_search", False),
                keep_searching=body.get("keep_searching", False),
                reason=body.get("reason", ""),
                book_type=body.get("book_type", ""),
                publisher=body.get("publisher", ""),
                page_count=body.get("page_count", 0),
                restricted_to_links=body.get("restricted_to_links", False),
            )

            if "release_date" in body and body["release_date"]:
                wanted_gallery.release_date = body["release_date"]

            if "add_to_archive_group" in body and body["add_to_archive_group"]:
                try:
                    group = ArchiveGroup.objects.get(pk=body["add_to_archive_group"])
                    wanted_gallery.add_to_archive_group = group
                except ArchiveGroup.DoesNotExist:
                    pass

            wanted_gallery.save()

            if "wanted_tags" in body:
                tag_objects = get_tag_objects_from_tag_list(body["wanted_tags"])
                for tag in tag_objects:
                    wanted_gallery.wanted_tags.add(tag)
            if "unwanted_tags" in body:
                tag_objects = get_tag_objects_from_tag_list(body["unwanted_tags"])
                for tag in tag_objects:
                    wanted_gallery.unwanted_tags.add(tag)
            if "wanted_providers" in body:
                providers = Provider.objects.filter(slug__in=body["wanted_providers"])
                wanted_gallery.wanted_providers.set(providers)
            if "unwanted_providers" in body:
                providers = Provider.objects.filter(slug__in=body["unwanted_providers"])
                wanted_gallery.unwanted_providers.set(providers)
            if "categories" in body:
                for category in body["categories"]:
                    category_obj, _ = Category.objects.get_or_create(name=category)
                    wanted_gallery.categories.add(category_obj)

            wanted_gallery.save()

            return HttpResponse(
                json.dumps({"result": "success", "id": wanted_gallery.id}),
                content_type="application/json; charset=utf-8",
            )

        except WantedGallery.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "Error creating WantedGallery"}),
                content_type="application/json; charset=utf-8",
            )
    else:
        return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def get_tag_objects_from_tag_list(tag_list) -> list[Tag]:
    tag_objects = []
    if isinstance(tag_list, str):
        tag_list = tag_list.split(",")
    if isinstance(tag_list, list):
        for tag_entry in tag_list:

            tag_clean = tag_entry.strip().replace(" ", "_")
            scope_name = tag_clean.split(":", maxsplit=1)
            if len(scope_name) > 1:
                tag_scope = scope_name[0]
                tag_name = scope_name[1]
            else:
                tag_scope = ""
                tag_name = scope_name[0]
            tag_obj, tag_created = Tag.objects.get_or_create(scope=tag_scope, name=tag_name)
            tag_objects.append(tag_obj)
    return tag_objects


def json_api_handle_put(request: HttpRequest, user: Optional[User | AnonymousUser]):
    data = request.GET
    body = json.loads(request.body)
    if "archive-group" in data and user and user.has_perm("viewer.change_archivegroup"):
        try:
            archive_group_id = int(data["archive-group"])
        except ValueError:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
            )
        try:

            body_entries = {entry["id"]: entry for entry in body["archive_group_entries"]}

            with transaction.atomic():
                archive_group = ArchiveGroup.objects.select_for_update(of=("self",)).get(pk=archive_group_id)

                archive_group_entries = (
                    ArchiveGroupEntry.objects.filter(archive_group=archive_group)
                    .select_related("archive")
                    .select_for_update(of=("self",))
                    .prefetch_related(
                        Prefetch(
                            "archive__tags",
                        )
                    )
                )

                archive_group.title = body["title"]
                archive_group.details = body["details"]
                archive_group.position = body["position"]
                archive_group.save()

                for entry in archive_group_entries:
                    if entry.pk in body_entries:
                        entry.title = body_entries[entry.pk]["title"]
                        entry.position = body_entries[entry.pk]["position"]

                        if entry.archive.id != body_entries[entry.pk]["archive"]["id"]:
                            try:
                                new_archive = Archive.objects.get(pk=body_entries[entry.pk]["archive"]["id"])
                                entry.archive = new_archive
                            except Archive.DoesNotExist:
                                pass

                        entry.save()

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
                        archive_group_entry_to_json(archive_entry, True, request)
                        for archive_entry in sorted(archive_group_entries, key=lambda x: x.position or 0)
                    ],
                },
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response, content_type="application/json; charset=utf-8")

        except ArchiveGroup.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
            )
    elif "archive-group-entry" in data and user and user.has_perm("viewer.change_archivegroupentry"):
        try:
            archive_group_entry_id = int(data["archive-group-entry"])
        except ValueError:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroupEntry does not exist."}),
                content_type="application/json; charset=utf-8",
            )
        try:
            with transaction.atomic():
                archive_group_entry = (
                    ArchiveGroupEntry.objects.select_related("archive")
                    .prefetch_related(
                        Prefetch(
                            "archive__tags",
                        )
                    )
                    .select_for_update(of=("self",))
                    .get(pk=archive_group_entry_id)
                )

                archive_group_entry.title = body["title"]
                archive_group_entry.position = body["position"]

                if archive_group_entry.archive.id != body["archive"]["id"]:
                    try:
                        new_archive = Archive.objects.get(pk=body["archive"]["id"])
                        archive_group_entry.archive = new_archive
                    except Archive.DoesNotExist:
                        pass

                archive_group_entry.save()

                response = json.dumps(
                    archive_group_entry_to_json(archive_group_entry, True, request),
                    # indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )

                return HttpResponse(response, content_type="application/json; charset=utf-8")

        except ArchiveGroupEntry.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroupEntry does not exist."}),
                content_type="application/json; charset=utf-8",
            )

    else:
        return HttpResponse(json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8")


def json_api_handle_delete(request: HttpRequest, user: Optional[User | AnonymousUser]):
    data = request.GET
    if "archive-group" in data and user and user.has_perm("viewer.delete_archivegroup"):
        try:
            archive_group_id = int(data["archive-group"])
        except ValueError:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
            )
        try:

            with transaction.atomic():
                archive_group = ArchiveGroup.objects.select_for_update(of=("self",)).get(pk=archive_group_id)

                delete_number = archive_group.delete()[0]

            return HttpResponse(json.dumps({"result": delete_number}), content_type="application/json; charset=utf-8")

        except ArchiveGroup.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
            )
    elif "archive-group-entry" in data and user and user.has_perm("viewer.delete_archivegroupentry"):
        try:
            archive_group_entry_id = int(data["archive-group-entry"])
        except ValueError:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroupEntry does not exist."}),
                content_type="application/json; charset=utf-8",
            )
        try:
            with transaction.atomic():
                archive_group_entry = ArchiveGroupEntry.objects.select_for_update(of=("self",)).get(
                    pk=archive_group_entry_id
                )

                delete_number = archive_group_entry.delete()[0]

            return HttpResponse(json.dumps({"result": delete_number}), content_type="application/json; charset=utf-8")

        except ArchiveGroupEntry.DoesNotExist:
            return HttpResponseNotFound(
                json.dumps({"result": "ArchiveGroupEntry does not exist."}),
                content_type="application/json; charset=utf-8",
            )

    else:
        return HttpResponseNotFound(
            json.dumps({"result": "Unknown command"}), content_type="application/json; charset=utf-8"
        )


# NOTE: This is used by 3rd parties, do not modify, at most create a new function if something needs changing
# Public API, does not check for any token, but filters if the user is authenticated or not.
@csrf_exempt
def json_api(request: HttpRequest) -> HttpResponse:

    token_valid, token_user = authenticate_by_token(request)

    user_is_authenticated = request.user.is_authenticated or token_valid

    if request.method == "GET":
        return json_api_handle_get(request, user_is_authenticated)
    elif request.method == "POST":
        if user_is_authenticated:
            return json_api_handle_post(request, token_user or request.user)
        else:
            return HttpResponseForbidden(
                json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
            )
    elif request.method == "PUT":
        if user_is_authenticated:
            return json_api_handle_put(request, token_user or request.user)
        else:
            return HttpResponseForbidden(
                json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
            )
    elif request.method == "DELETE":
        if user_is_authenticated:
            return json_api_handle_delete(request, token_user or request.user)
        else:
            return HttpResponseForbidden(
                json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
            )
    else:
        return HttpResponseNotAllowed(
            ["GET", "PUT", "DELETE"],
            json.dumps({"result": "Unsupported request method"}),
            content_type="application/json; charset=utf-8",
        )


# Private API, checks for the API key.
@csrf_exempt
def json_parser(request: HttpRequest) -> HttpResponse:
    response = {}

    authenticated, actual_user = double_check_auth(request)

    if not authenticated or not actual_user or not actual_user.has_perm("viewer.use_remote_api"):
        response["error"] = "Not authorized"
        return HttpResponse(json.dumps(response), status=401, content_type="application/json; charset=utf-8")

    if request.method == "POST":
        if not request.body:
            response["error"] = "Empty request"
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        data = json.loads(request.body.decode("utf-8"))

        if "operation" not in data or "args" not in data:
            response["error"] = "Wrong format"
        else:
            args = data["args"]
            response = {}
            # Used by internal pages and userscript
            if data["operation"] == "webcrawler" and "link" in args:
                if not crawler_settings.workers.web_queue:
                    response["error"] = "The webqueue is not running"
                elif "downloader" in args:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    if not current_settings.workers.web_queue:
                        response["error"] = "The webqueue is not running"
                    else:
                        current_settings.allow_downloaders_only([args["downloader"]], True, True, True)
                        archive = None
                        parsers = current_settings.provider_context.get_parsers(current_settings)
                        current_settings.archive_user = actual_user
                        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                        for parser in parsers:
                            if parser.id_from_url_implemented():
                                urls_filtered = parser.filter_accepted_urls((args["link"],))
                                for url_filtered in urls_filtered:
                                    gallery_gid = parser.id_from_url(url_filtered)
                                    if gallery_gid:
                                        archive = Archive.objects.filter(
                                            gallery__gid=gallery_gid, gallery__provider=parser.name
                                        ).first()
                                if urls_filtered:
                                    break
                        current_settings.workers.web_queue.enqueue_args_list(
                            (args["link"],), override_options=current_settings
                        )
                        if archive:
                            response["message"] = "Archive exists, crawling to check for redownload: " + args["link"]
                        else:
                            response["message"] = "Crawling: " + args["link"]
                else:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.archive_user = actual_user
                    current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                    extra_args = []
                    if "reason" in args and args["reason"]:
                        extra_args.append("-reason " + args["reason"])

                    if "parentLink" in args:
                        parent_archive = None
                        if not current_settings.workers.web_queue:
                            response["error"] = "The webqueue is not running"
                            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
                        parsers = current_settings.provider_context.get_parsers(current_settings)
                        for parser in parsers:
                            if parser.id_from_url_implemented():
                                urls_filtered = parser.filter_accepted_urls((args["parentLink"],))
                                for url_filtered in urls_filtered:
                                    gallery_gid = parser.id_from_url(url_filtered)
                                    if gallery_gid:
                                        parent_archive = Archive.objects.filter(
                                            gallery__gid=gallery_gid, gallery__provider=parser.name
                                        ).first()
                                if urls_filtered:
                                    break
                        if parent_archive and parent_archive.gallery:
                            link = parent_archive.gallery.get_link()
                            if "action" in args and args["action"] == "replaceFound":

                                # Preserve old archive extra data
                                old_user_favorites = UserArchivePrefs.objects.filter(archive=parent_archive).values(
                                    "user", "favorite_group"
                                )
                                old_extracted = parent_archive.extracted

                                parent_archive.gallery.mark_as_deleted()
                                parent_archive.gallery = None
                                parent_archive.delete_all_files()
                                parent_archive.delete_files_but_archive()
                                parent_archive.delete()
                                response["message"] = "Crawling: " + args["link"] + ", deleting parent: " + link

                                def archive_callback(
                                    x: Optional["Archive"], crawled_url: Optional[str], result: str
                                ) -> None:

                                    if x:
                                        logger.info(
                                            "Preserving old extra info for archive: {}".format(x.get_absolute_url())
                                        )
                                        for old_user_favorite in old_user_favorites:
                                            UserArchivePrefs.objects.get_or_create(
                                                archive=x,
                                                user=old_user_favorite["user"],
                                                favorite_group=old_user_favorite["favorite_group"],
                                            )

                                        if old_extracted and not x.extracted and x.crc32:
                                            x.extract()

                                current_settings.workers.web_queue.enqueue_args_list(
                                    [args["link"]] + extra_args, archive_callback=archive_callback
                                )
                            elif "action" in args and args["action"] == "queueFound":
                                response["message"] = "Crawling: " + args["link"] + ", keeping parent: " + link
                                current_settings.workers.web_queue.enqueue_args_list(
                                    [args["link"]] + extra_args, override_options=current_settings
                                )
                            else:
                                response["message"] = "Please confirm deletion of parent: " + link
                                response["action"] = "confirmDeletion"
                        else:
                            archive = None
                            parsers = current_settings.provider_context.get_parsers(current_settings)
                            for parser in parsers:
                                if parser.id_from_url_implemented():
                                    urls_filtered = parser.filter_accepted_urls((args["link"],))
                                    for url_filtered in urls_filtered:
                                        gallery_gid = parser.id_from_url(url_filtered)
                                        if gallery_gid:
                                            archive = Archive.objects.filter(
                                                gallery__gid=gallery_gid, gallery__provider=parser.name
                                            ).first()
                                    if urls_filtered:
                                        break
                            if archive:
                                response["message"] = (
                                    "Archive exists, crawling to check for redownload: " + args["link"]
                                )
                            else:
                                response["message"] = "Crawling: " + args["link"]

                            current_settings.workers.web_queue.enqueue_args_list(
                                [args["link"]] + extra_args, override_options=current_settings
                            )
                    else:
                        archive = None
                        if not current_settings.workers.web_queue:
                            response["error"] = "The webqueue is not running"
                            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
                        parsers = current_settings.provider_context.get_parsers(current_settings)
                        for parser in parsers:
                            if parser.id_from_url_implemented():
                                urls_filtered = parser.filter_accepted_urls((args["link"],))
                                for url_filtered in urls_filtered:
                                    gallery_gid = parser.id_from_url(url_filtered)
                                    if gallery_gid:
                                        archive = Archive.objects.filter(
                                            gallery__gid=gallery_gid, gallery__provider=parser.name
                                        ).first()
                                if urls_filtered:
                                    break
                        if archive:
                            response["message"] = "Archive exists, crawling to check for redownload: " + args["link"]
                        else:
                            response["message"] = "Crawling: " + args["link"]
                        current_settings.workers.web_queue.enqueue_args_list(
                            [args["link"]] + extra_args, override_options=current_settings
                        )
                if not response:
                    response["error"] = "Could not parse request"
                return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
            # Used by remotesite command
            elif data["operation"] == "archive_request":
                provider_dict: dict[str, list[str]] = defaultdict(list)
                for gid_provider in args:
                    provider_dict[gid_provider[1]].append(gid_provider[0])
                gallery_ids: list[int] = []
                for provider, gid_list in provider_dict.items():
                    pks = Gallery.objects.filter(provider=provider, gid__in=gid_list).values_list("pk", flat=True)
                    gallery_ids.extend(pks)
                archives_query = Archive.objects.filter_non_existent(
                    crawler_settings.MEDIA_ROOT, gallery__pk__in=gallery_ids
                )
                archives = [
                    {
                        "gid": archive.gallery.gid,
                        "provider": archive.gallery.provider,
                        "id": archive.id,
                        "zipped": archive.zipped.name,
                        "filesize": archive.filesize,
                    }
                    for archive in archives_query
                    if archive.gallery
                ]
                response_text = json.dumps({"result": archives})
                return HttpResponse(response_text, content_type="application/json; charset=utf-8")
            elif data["operation"] == "force_queue_archives":
                pages_links = args
                if len(pages_links) > 0:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    if "archive_reason" in data:
                        current_settings.archive_reason = data["archive_reason"]
                    if "archive_details" in data:
                        current_settings.archive_details = data["archive_details"]
                    current_settings.allow_type_downloaders_only("fake")
                    current_settings.set_enable_download()
                    if current_settings.workers.web_queue:
                        current_settings.archive_user = actual_user
                        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                        current_settings.workers.web_queue.enqueue_args_list(
                            pages_links, override_options=current_settings
                        )
                    else:
                        pages_links = []
                return HttpResponse(
                    json.dumps({"result": str(len(pages_links))}), content_type="application/json; charset=utf-8"
                )

            # Used by remotesite command
            elif data["operation"] in ("queue_archives", "queue_galleries"):
                urls = args
                new_urls_set = set()
                gids_set = set()

                parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
                for parser in parsers:
                    if parser.id_from_url_implemented():
                        urls_filtered = parser.filter_accepted_urls(urls)
                        for url in urls_filtered:
                            gid = parser.id_from_url(url)
                            gids_set.add(gid)

                gids_list = list(gids_set)

                existing_galleries = Gallery.objects.filter(gid__in=gids_list).exclude(
                    status=Gallery.StatusChoices.DELETED
                )
                for gallery_object in existing_galleries:
                    if gallery_object.is_submitted():
                        gallery_object.delete()
                    # Delete queue galleries that failed, and does not have archives.
                    elif (
                        data["operation"] == "queue_archives"
                        and "failed" in gallery_object.dl_type
                        and not gallery_object.archive_set.all()
                    ):
                        gallery_object.delete()
                    elif data["operation"] == "queue_archives" and not gallery_object.archive_set.all():
                        gallery_object.delete()
                already_present_gids = list(Gallery.objects.filter(gid__in=gids_list).values_list("gid", flat=True))
                # new_gids = list(gids_set - set(already_present_gids))

                for parser in parsers:
                    if parser.id_from_url_implemented():
                        urls_filtered = parser.filter_accepted_urls(urls)
                        for url in urls_filtered:
                            gid = parser.id_from_url(url)
                            if gid not in already_present_gids:
                                new_urls_set.add(url)

                pages_links = list(new_urls_set)
                if len(pages_links) > 0:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    if data["operation"] == "queue_galleries":
                        current_settings.allow_type_downloaders_only("info")
                    elif data["operation"] == "queue_archives":
                        if "archive_reason" in data:
                            current_settings.archive_reason = data["archive_reason"]
                        if "archive_details" in data:
                            current_settings.archive_details = data["archive_details"]
                        current_settings.allow_type_downloaders_only("fake")
                    if current_settings.workers.web_queue:
                        current_settings.archive_user = actual_user
                        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                        current_settings.workers.web_queue.enqueue_args_list(
                            pages_links, override_options=current_settings
                        )
                    else:
                        pages_links = []
                return HttpResponse(
                    json.dumps({"result": str(len(pages_links))}), content_type="application/json; charset=utf-8"
                )
            # Used by remotesite command
            elif data["operation"] == "links":
                links = args
                if len(links) > 0 and crawler_settings.workers.web_queue:
                    crawler_settings.workers.web_queue.enqueue_args_list(links)
                return HttpResponse(
                    json.dumps({"result": str(len(links))}), content_type="application/json; charset=utf-8"
                )
            # Used by archive page
            elif data["operation"] == "match_archive":
                archive_obj = Archive.objects.filter(pk=args["archive"])
                if archive_obj:
                    generate_possible_matches_for_archives(
                        archive_obj,
                        filters=(args["match_filter"],),
                        match_local=False,
                        match_web=True,
                    )
                return HttpResponse(
                    json.dumps({"message": "web matcher done, check the logs for results"}),
                    content_type="application/json; charset=utf-8",
                )
            elif data["operation"] == "match_archive_internally":
                archive = Archive.objects.get(pk=args["archive"])
                if archive:
                    clear_title = True if "clear" in args else False
                    provider_filter = args.get("provider", "")
                    try:
                        cutoff = float(request.GET.get("cutoff", "0.4"))
                    except ValueError:
                        cutoff = 0.4
                    try:
                        max_matches = int(request.GET.get("max-matches", "10"))
                    except ValueError:
                        max_matches = 10

                    archive.generate_possible_matches(
                        clear_title=clear_title, provider_filter=provider_filter, cutoff=cutoff, max_matches=max_matches
                    )
                    archive.save()
                return HttpResponse(
                    json.dumps({"message": "internal matcher done, check the archive for results"}),
                    content_type="application/json; charset=utf-8",
                )
            else:
                response["error"] = "Unknown function"
    elif request.method == "GET":
        data = request.GET
        if "gc" in data:
            args = data.copy()

            for k in gallery_filter_keys:
                if k not in args:
                    args[k] = ""

            keys = ("sort", "asc_desc")

            for k in keys:
                if k not in args:
                    args[k] = ""

            # args = data
            # Already authorized by api key.
            args["public"] = ""

            results = filter_galleries_no_request(args).prefetch_related("tags")
            if not results:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            response_text = json.dumps(
                [
                    {
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
                        "rating": gallery.rating,
                        "hidden": gallery.hidden,
                        "fjord": gallery.fjord,
                        "public": gallery.public,
                        "provider": gallery.provider,
                        "dl_type": gallery.dl_type,
                        "tags": gallery.tag_list(),
                        "link": gallery.get_link(),
                        "thumbnail": (
                            request.build_absolute_uri(reverse("viewer:gallery-thumb", args=(gallery.pk,)))
                            if gallery.thumbnail
                            else ""
                        ),
                        "thumbnail_url": gallery.thumbnail_url,
                    }
                    for gallery in results
                ],
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response_text, content_type="application/json; charset=utf-8")
        else:
            response["error"] = "Unknown function"
    else:
        response["error"] = "Unsupported method: {}".format(request.method)
    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")


def simple_archive_filter(args: str, public: bool = True) -> "QuerySet[Archive]":
    """Simple filtering of archives."""

    # sort and filter results by parameters
    # order = '-gallery__posted'

    if public:
        order = "-public_date"
        results = Archive.objects.order_by(order).filter(public=True)
    else:
        order = "-create_date"
        results = Archive.objects.order_by(order)

    q_formatted = "%" + args.replace(" ", "%") + "%"
    results_title = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))

    tags = args.split(",")
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
    results = results | results_title

    results = results.distinct()

    return results


def filter_galleries_no_request(filter_args: Union[dict[str, Any], QueryDict]) -> "QuerySet[Gallery]":

    # sort and filter results by parameters
    order = "posted"
    sort = filter_args["sort"]
    asc_desc = filter_args["asc_desc"]
    if sort and isinstance(sort, str) and sort in gallery_order_fields:
        order = sort
    if asc_desc and isinstance(asc_desc, str) and asc_desc == "desc":
        order = "-" + order

    results = Gallery.objects.eligible_for_use().order_by(order)

    if filter_args["public"]:
        results = results.filter(public=bool(filter_args["public"]))

    title = filter_args["title"]
    if title and isinstance(title, str):
        q_formatted = "%" + title.replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))
    rating_from = filter_args["rating_from"]
    if rating_from and isinstance(rating_from, str):
        results = results.filter(rating__gte=float(rating_from))
    rating_to = filter_args["rating_to"]
    if rating_to and isinstance(rating_to, str):
        results = results.filter(rating__lte=float(rating_to))
    filecount_from = filter_args["filecount_from"]
    if filecount_from and isinstance(filecount_from, str):
        results = results.filter(filecount__gte=int(float(filecount_from)))
    filecount_to = filter_args["filecount_to"]
    if filecount_to and isinstance(filecount_to, str):
        results = results.filter(filecount__lte=int(float(filecount_to)))
    filesize_from = filter_args["filesize_from"]
    if filesize_from and isinstance(filesize_from, str):
        results = results.filter(filesize__gte=float(filesize_from))
    filesize_to = filter_args["filesize_to"]
    if filesize_to and isinstance(filesize_to, str):
        results = results.filter(filesize__lte=float(filesize_to))
    if filter_args["posted_from"]:
        results = results.filter(posted__gte=filter_args["posted_from"])
    if filter_args["posted_to"]:
        results = results.filter(posted__lte=filter_args["posted_to"])
    if filter_args["create_from"]:
        results = results.filter(create_date__gte=filter_args["create_from"])
    if filter_args["create_to"]:
        results = results.filter(create_date__lte=filter_args["create_to"])
    if filter_args["category"]:
        results = results.filter(category__icontains=filter_args["category"])
    if filter_args["expunged"]:
        results = results.filter(expunged=bool(filter_args["expunged"]))
    if filter_args["disowned"]:
        results = results.filter(disowned=bool(filter_args["disowned"]))
    if filter_args["hidden"]:
        results = results.filter(hidden=bool(filter_args["hidden"]))
    if filter_args["fjord"]:
        results = results.filter(fjord=bool(filter_args["fjord"]))
    if filter_args["uploader"]:
        results = results.filter(uploader=filter_args["uploader"])
    if filter_args["provider"]:
        results = results.filter(provider=filter_args["provider"])
    if filter_args["dl_type"]:
        results = results.filter(dl_type=filter_args["dl_type"])
    if filter_args["reason"]:
        results = results.filter(reason__icontains=filter_args["reason"])
    if filter_args["crc32"]:
        results = results.filter(archive__crc32=filter_args["crc32"])

    # Only return galleries with associated archives.
    if filter_args["used"]:
        results = results.filter(
            Q(alternative_sources__isnull=False)
            | Q(archive__isnull=False)
            | Q(gallery_container__archive__isnull=False)
        )

    filters_tags = filter_args["tags"]

    if filters_tags and isinstance(filters_tags, str):
        tags = filters_tags.split(",")
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

    return results
