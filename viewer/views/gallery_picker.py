"""JSON API for the gallery picker widget (chaika-apps ``admin-widgets``)."""

import typing

from django.core.paginator import EmptyPage, InvalidPage, Paginator
from django.db.models import Count
from django.http import HttpRequest, JsonResponse
from django.urls import reverse

from core.base.utilities import timestamp_or_null
from viewer.models import Gallery
from viewer.services.gallery_selection import (
    MAX_GALLERY_IDS,
    allowed_filter_keys,
    can_use_gallery_picker,
    clean_filter,
    filter_galleries,
    galleries_by_ids,
)
from viewer.utils.requests import double_check_auth

PAGE_SIZES = (10, 20, 50, 100)

# Native (database) autocompletes for the picker's filter fields.
AUTOCOMPLETE_URL_NAMES = {
    "tags": "tag-json-autocomplete",
    "provider": "gallery-provider-json-autocomplete",
    "category": "gallery-category-json-autocomplete",
}


def _gallery_to_json(gallery: Gallery) -> dict[str, typing.Any]:
    return {
        "id": gallery.pk,
        "title": gallery.title or "",
        "title_jpn": gallery.title_jpn or "",
        "gid": gallery.gid,
        "provider": gallery.provider,
        "category": gallery.category or "",
        "uploader": gallery.uploader or "",
        "filecount": gallery.filecount,
        "filesize": gallery.filesize,
        "posted": timestamp_or_null(gallery.posted),
        "create_date": timestamp_or_null(gallery.create_date),
        "status": gallery.get_status_display(),
        "normal": gallery.status == Gallery.StatusChoices.NORMAL,
        "public": gallery.public,
        "hidden": gallery.hidden,
        "archive_count": getattr(gallery, "archive_count", None),
        "link": gallery.get_link(),
        "url": reverse("viewer:gallery", args=(gallery.pk,)),
        "thumbnail": gallery.thumbnail.url if gallery.thumbnail else None,
    }


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def gallery_picker_api(request: HttpRequest) -> JsonResponse:
    """List galleries for the picker.

    ``?ids=1,2,3`` returns those galleries (to show a selection). Otherwise the filters in the query string are applied
    (only the ones the user may use), with ``page`` and ``size`` for pagination.
    """
    _, user = double_check_auth(request)
    if not can_use_gallery_picker(user):
        return JsonResponse({"error": "You don't have permission to select galleries."}, status=403)
    is_staff = bool(getattr(user, "is_staff", False))
    allowed_filters = allowed_filter_keys(user)
    meta: dict[str, typing.Any] = {
        "allowed_filters": allowed_filters,
        "is_staff": is_staff,
        "autocomplete": {
            key: reverse(url_name) for key, url_name in AUTOCOMPLETE_URL_NAMES.items() if key in allowed_filters
        },
    }

    ids_param = request.GET.get("ids")
    if ids_param is not None:
        try:
            ids = [int(x) for x in ids_param.split(",") if x.strip()][:MAX_GALLERY_IDS]
        except ValueError:
            return JsonResponse({"error": "ids must be a comma separated list of numbers."}, status=400)
        galleries = galleries_by_ids(ids, user).annotate(archive_count=Count("archive"))
        by_id = {gallery.pk: gallery for gallery in galleries}
        return JsonResponse(
            {
                **meta,
                "results": [_gallery_to_json(by_id[pk]) for pk in ids if pk in by_id],
                "missing_ids": [pk for pk in ids if pk not in by_id],
            }
        )

    try:
        cleaned = clean_filter(request.GET, user)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    results = filter_galleries(cleaned).annotate(archive_count=Count("archive"))
    size = _parse_int(request.GET.get("size"), 20)
    if size not in PAGE_SIZES:
        size = 20
    paginator = Paginator(results, size)
    try:
        page = paginator.page(_parse_int(request.GET.get("page"), 1))
    except (InvalidPage, EmptyPage):
        page = paginator.page(paginator.num_pages)

    return JsonResponse(
        {
            **meta,
            "filter": cleaned,
            "results": [_gallery_to_json(gallery) for gallery in page],
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "number": page.number,
            "has_next": page.has_next(),
            "has_previous": page.has_previous(),
        }
    )
