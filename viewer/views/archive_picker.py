"""JSON API for the archive picker widget (chaika-apps ``admin-widgets``)."""

import typing

from django.core.paginator import EmptyPage, InvalidPage, Paginator
from django.http import HttpRequest, JsonResponse
from django.urls import reverse

from core.base.utilities import timestamp_or_null
from viewer.models import Archive
from viewer.services.archive_selection import (
    allowed_filter_keys,
    can_use_archive_picker,
    clean_filter,
    filter_archives,
)
from viewer.utils.requests import double_check_auth

PAGE_SIZES = (10, 20, 50, 100)

# Native (database) autocompletes for the picker's filter fields. Not the Elasticsearch ones, which are optional.
AUTOCOMPLETE_URL_NAMES = {
    "tags": "tag-json-autocomplete",
    "source_type": "source-json-autocomplete",
    "reason": "reason-json-autocomplete",
    "provider": "provider-json-autocomplete",
    "category": "category-json-autocomplete",
    "mark_reason": "archive-manager-reason-json-autocomplete",
}
MAX_IDS_LOOKUP = 1000


def _archive_to_json(request: HttpRequest, archive: Archive, is_staff: bool) -> dict[str, typing.Any]:
    gallery = archive.gallery
    data: dict[str, typing.Any] = {
        "id": archive.pk,
        "title": archive.title or "",
        "title_jpn": archive.title_jpn or "",
        "filecount": archive.filecount,
        "filesize": archive.filesize,
        "create_date": timestamp_or_null(archive.create_date),
        "source_type": archive.source_type or "",
        "reason": archive.reason or "",
        "public": archive.public,
        "binned": archive.binned,
        "file_deleted": archive.file_deleted,
        "url": reverse("viewer:archive", args=(archive.pk,)),
        "thumbnail": archive.thumbnail.url if archive.thumbnail else None,
        "gallery": (
            {
                "id": gallery.pk,
                "provider": gallery.provider,
                "gid": gallery.gid,
                "category": gallery.category,
                "link": gallery.get_link(),
                "url": reverse("viewer:gallery", args=(gallery.pk,)),
            }
            if gallery
            else None
        ),
    }
    if is_staff:
        data["file"] = archive.zipped.name if archive.zipped else ""
    return data


def _parse_int(value: str | None, default: int) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def archive_picker_api(request: HttpRequest) -> JsonResponse:
    """List archives for the picker.

    ``?ids=1,2,3`` returns those archives (to show a selection). Otherwise the archive manager filters in the query
    string are applied (only the ones the user may use), with ``page`` and ``size`` for pagination.
    """
    _, user = double_check_auth(request)
    if not can_use_archive_picker(user):
        return JsonResponse({"error": "You don't have permission to select archives."}, status=403)
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
            ids = [int(x) for x in ids_param.split(",") if x.strip()][:MAX_IDS_LOOKUP]
        except ValueError:
            return JsonResponse({"error": "ids must be a comma separated list of numbers."}, status=400)
        archives = Archive.objects.filter(pk__in=ids).select_related("gallery")
        by_id = {archive.pk: archive for archive in archives}
        return JsonResponse(
            {
                **meta,
                "results": [_archive_to_json(request, by_id[pk], is_staff) for pk in ids if pk in by_id],
                "missing_ids": [pk for pk in ids if pk not in by_id],
            }
        )

    try:
        cleaned = clean_filter(request.GET, user)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)

    results = filter_archives(cleaned, can_view_recycled=True).select_related("gallery")
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
            "results": [_archive_to_json(request, archive, is_staff) for archive in page],
            "count": paginator.count,
            "num_pages": paginator.num_pages,
            "number": page.number,
            "has_next": page.has_next(),
            "has_previous": page.has_previous(),
        }
    )
