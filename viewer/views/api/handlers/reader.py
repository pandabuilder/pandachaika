"""Reader commands, used by the online image viewer (formerly ``/r-api/`` in ``viewer/views/exp.py``).

``GET ?archive-reader=<id>``, ``GET ?archive-images=<id>&i=<position>...``, ``GET ?archives-browse``,
``GET ?archives-quick``, ``GET ?archives-simple``, ``GET ?me``, ``POST ?extract-archive=<id>`` and
``POST ?reduce-archive=<id>``. The legacy ``/r-api/`` routes in ``viewer/views/api/legacy.py`` call these handlers.
"""

from typing import Any

from django.contrib.auth.models import AnonymousUser, User
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.http.request import QueryDict
from django.urls import reverse

from core.base.types import DataDict
from core.base.utilities import str_to_int, timestamp_or_zero
from viewer.models import Archive, ArchiveTag, Image, UserArchivePrefs
from viewer.views.api.auth import get_api_context, require_user
from viewer.views.api.common import _get_int, parse_bool_from_data_or_body
from viewer.views.api.errors import ApiError, json_response
from viewer.views.api.filters import MAX_API_TAG_FILTERS, simple_archive_filter
from viewer.views.api.handlers.tasks import task_reference
from viewer.views.head import archive_filter_keys, filter_archives
from viewer.views.head.common import apply_tag_filters
from workers import client


def _archive_id(data: QueryDict, key: str) -> int:
    try:
        return _get_int(data, key)
    except (ValueError, TypeError):
        raise ApiError.not_found("Archive does not exist.") from None


def get_archive_data(data: QueryDict) -> "QuerySet[Archive]":
    """Quick search of archives."""

    # sort and filter results by parameters
    order = "create_date"
    order_data = data.get("order")
    if order_data is not None:
        order = order_data
    if order == "rating":
        order = "gallery__" + order
    elif order == "posted":
        order = "gallery__" + order
    if "asc" not in data:
        order = "-" + order

    results = Archive.objects.order_by(order)

    title = data.get("title")

    if title is not None:
        q_formatted = "%" + title.replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))

    if "filename" in data and isinstance(data["filename"], str):
        results = results.filter(zipped__icontains=data["filename"])
    try:
        rating_from = data.get("rating_from")
        if rating_from is not None:
            results = results.filter(gallery__rating__gte=float(rating_from))
        rating_to = data.get("rating_to")
        if rating_to is not None:
            results = results.filter(gallery__rating__lte=float(rating_to))
        filecount_from = data.get("filecount_from")
        if filecount_from is not None:
            results = results.filter(filecount__gte=int(float(filecount_from)))
        filecount_to = data.get("filecount_to")
        if filecount_to is not None:
            results = results.filter(filecount__lte=int(float(filecount_to)))
        filesize_from = data.get("filesize_from")
        if filesize_from is not None:
            results = results.filter(filesize__gte=int(float(filesize_from)))
        filesize_to = data.get("filesize_to")
        if filesize_to is not None:
            results = results.filter(filesize__lte=int(float(filesize_to)))
    except ValueError:
        raise ApiError.bad_request("Rating, filecount and filesize filters must be numbers.") from None
    if "posted_from" in data:
        results = results.filter(gallery__posted__gte=data["posted_from"])
    if "posted_to" in data:
        results = results.filter(gallery__posted__lte=data["posted_to"])
    if "match_type" in data:
        results = results.filter(match_type__icontains=str(data["match_type"]))
    if "source_type" in data:
        results = results.filter(source_type__icontains=str(data["source_type"]))
    if "extracted" in data:
        results = results.filter(extracted=True)

    q_string = data.get("q")
    if q_string is not None:
        terms = q_string.split()
        if terms:
            results = apply_tag_filters(
                results,
                terms,
                through_model=ArchiveTag,
                fk_field="archive_id",
                max_tags=MAX_API_TAG_FILTERS,
            )

    results = results.prefetch_related("tags")

    return results


def archives_page_to_json(archives_list: "QuerySet[Archive]", request: HttpRequest, data: QueryDict) -> dict[str, Any]:
    try:
        archives_per_page = min(100, max(1, int(data.get("limit", "48"))))
    except ValueError:
        archives_per_page = 48
    paginator = Paginator(archives_list, archives_per_page)
    try:
        page = int(data.get("page", "1"))
    except ValueError:
        page = 1
    try:
        archives = paginator.page(page)
    except EmptyPage:
        # If page is out of range (e.g. 9999), deliver last page of results.
        archives = paginator.page(paginator.num_pages)
    return {
        "objects": [
            {
                "id": archive.pk,
                "title": archive.title,
                "filecount": archive.filecount,
                "filesize": archive.filesize,
                "download": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
                "url": request.build_absolute_uri(reverse("viewer:archive", args=(archive.pk,))),
                "image": (request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None),
                "image_height": (archive.thumbnail_height if archive.thumbnail else None),
                "image_width": (archive.thumbnail_width if archive.thumbnail else None),
                "tags": archive.tag_list_sorted(),
                "extracted": archive.extracted,
            }
            for archive in archives
        ],
        "has_previous": archives.has_previous(),
        "has_next": archives.has_next(),
        "num_pages": paginator.num_pages,
        "count": paginator.count,
        "number": archives.number,
    }


def handle_get_archive_reader(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    """Archive details for the image viewer, with the image at ``position`` (default 1) when extracted."""
    archive_id = _archive_id(data, "archive-reader")
    try:
        if user_is_authenticated:
            archive = Archive.objects.select_related("gallery").prefetch_related("tags").get(pk=archive_id)
        else:
            archive = (
                Archive.objects.filter(public=True, extracted=True)
                .select_related("gallery")
                .prefetch_related("tags")
                .get(pk=archive_id)
            )
    except Archive.DoesNotExist:
        raise ApiError.not_found("Archive does not exist.") from None
    try:
        position = int(data.get("position", "1"))
    except ValueError:
        raise ApiError.bad_request("Invalid position.") from None

    start_image = None
    if archive.extracted:
        image = archive.image_set.filter(position=position).first()
        if image is None:
            raise ApiError.not_found("Image does not exist.")
        start_image = image.dump_image(request)

    # startImage is None when the archive is not extracted.
    return json_response(
        {
            "id": archive.pk,
            "title": archive.title,
            "title_jpn": archive.title_jpn,
            "category": archive.gallery.category if archive.gallery else "",
            "uploader": archive.gallery.uploader if archive.gallery else "",
            "posted": int(timestamp_or_zero(archive.gallery.posted)) if archive.gallery else "",
            "filecount": archive.filecount,
            "filesize": archive.filesize,
            "download": request.build_absolute_uri(reverse("viewer:archive-download", args=(archive.pk,))),
            "url": request.build_absolute_uri(reverse("viewer:archive", args=(archive.pk,))),
            "expunged": archive.gallery.expunged if archive.gallery else "",
            "disowned": archive.gallery.disowned if archive.gallery else "",
            "rating": float(str_to_int(archive.gallery.rating)) if archive.gallery else "",
            "fjord": archive.gallery.fjord if archive.gallery else None,
            "tags": archive.tag_list_sorted(),
            "extracted": archive.extracted,
            "image": (request.build_absolute_uri(archive.thumbnail.url) if archive.thumbnail else None),
            "lastPosition": int(archive.image_set.count()) or archive.filecount,
            "startImage": start_image,
        },
        sort_keys=True,
    )


def handle_get_archive_images(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    """URLs and sizes of the extracted images at the requested positions (``i``, repeated), in that order."""
    archive_id = _archive_id(data, "archive-images")
    positions = data.getlist("i")
    images = Image.objects.filter(archive=archive_id, position__in=positions, extracted=True)
    if not user_is_authenticated:
        images = images.filter(archive__public=True)
    try:
        ordered_images = sorted(images, key=lambda img: positions.index(str(img.position)))
    except ValueError:
        raise ApiError.bad_request("Image positions must be integers.") from None
    return json_response(
        [
            {
                "position": image.position,
                "url": request.build_absolute_uri(image.image.url),
                "is_horizontal": (
                    image.image_width / image.image_height > 1 if image.image_width and image.image_height else False
                ),
                "width": image.image_width,
                "height": image.image_height,
            }
            for image in ordered_images
        ]
    )


def handle_get_archives_quick(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    """Paginated quick archive search (``title``, ``q`` tag terms, ranges, ``order``/``asc``, ``favorites``)."""
    archives_list = get_archive_data(data)
    if not user_is_authenticated:
        archives_list = archives_list.filter(public=True, extracted=True)
    user = get_api_context(request).user
    if data.get("favorites") and user is not None:
        try:
            favorite_group = int(data.get("favorites", ""))
        except ValueError:
            raise ApiError.bad_request("Invalid favorites group.") from None
        user_arch_ids = UserArchivePrefs.objects.filter(user=user.pk, favorite_group=favorite_group).values_list(
            "archive"
        )
        archives_list = archives_list.filter(id__in=user_arch_ids)
    return json_response(archives_page_to_json(archives_list, request, data))


def handle_get_archives_browse(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    """Paginated archive listing with the same filters and sorting as the archive search page."""
    display_prms: DataDict = {}
    parameters = dict(request.session["parameters"]) if "parameters" in request.session else {}

    for k in ("sort", "asc_desc"):
        if k not in parameters:
            parameters[k] = ""

    for k in archive_filter_keys:
        if k not in display_prms:
            display_prms[k] = ""

    for k, v in data.items():
        if k == "archives-browse":
            continue
        if k in parameters:
            parameters[k] = v
        else:
            display_prms[k] = v

    if not parameters.get("view"):
        parameters["view"] = "list"
    if not parameters.get("asc_desc"):
        parameters["asc_desc"] = "desc"
    if not parameters.get("sort"):
        parameters["sort"] = "create_date" if user_is_authenticated else "public_date"

    archives_list_filtered: "QuerySet[Archive]" = filter_archives(
        request,
        parameters,
        display_prms,
        user_is_authenticated,
        force_private=user_is_authenticated,
        max_tags=MAX_API_TAG_FILTERS,
    )
    if "extracted" in data:
        archives_list_filtered = archives_list_filtered.filter(extracted=True)

    return json_response(archives_page_to_json(archives_list_filtered.prefetch_related("tags"), request, data))


def handle_get_archives_simple(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    """Archive IDs and titles matching a title or tag query (``q``, or the command's own value)."""
    q_args = data.get("q") or data.get("archives-simple") or ""
    results = simple_archive_filter(q_args, public=not user_is_authenticated)
    return json_response([{"id": o.pk, "title": o.best_title} for o in results])


def handle_get_me(request: HttpRequest, data: QueryDict, user_is_authenticated: bool) -> HttpResponse:
    """The authenticated user."""
    user = require_user(request)
    return json_response({"id": user.pk, "username": user.username, "is_staff": user.is_staff})


def _toggle_extraction(
    request: HttpRequest,
    data: QueryDict,
    body: dict,
    key: str,
    user: User | AnonymousUser | None,
    extract: bool,
) -> HttpResponse:
    archive_id = _archive_id(data, key)
    try:
        archive = Archive.objects.get(pk=archive_id)
    except Archive.DoesNotExist:
        raise ApiError.not_found("Archive does not exist.") from None

    if archive.extracted == extract:
        state = "extracted" if extract else "reduced"
        return json_response(
            {
                "result": "warning",
                "message": f"Archive: {archive_id} is already {state}.",
                "change": {"field": "extracted", "value": archive.extracted},
                "task_id": None,
                "status_url": None,
            }
        )

    task_type = "extract_worker" if extract else "reduce_worker"
    active_task = client.get_active_archive_task(archive.pk, task_type)
    if active_task is not None:
        action_word = "Extraction" if extract else "Reduction"
        return json_response(
            {
                "result": "queued",
                "message": f"{action_word} is already queued.",
                **task_reference(request, active_task),
            }
        )

    if extract:
        resized, _ = parse_bool_from_data_or_body(data, body, "resized")
        task = client.enqueue(
            args=[archive.pk],
            task_type="extract_worker",
            override_options={"resized": resized},
            user=user,
            event_action="EXPAND_ARCHIVE",
        )
        message = "Extraction queued (resized)." if resized else "Extraction queued."
    else:
        task = client.enqueue(
            args=[archive.pk],
            task_type="reduce_worker",
            user=user,
            event_action="REDUCE_ARCHIVE",
        )
        message = "Reduction queued."

    return json_response(
        {
            "result": "queued",
            "message": message,
            **task_reference(request, task),
        }
    )


def handle_post_extract_archive(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
) -> HttpResponse:
    return _toggle_extraction(request, data, body, "extract-archive", user, extract=True)


def handle_post_reduce_archive(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
) -> HttpResponse:
    return _toggle_extraction(request, data, body, "reduce-archive", user, extract=False)


def handle_legacy_me(request: HttpRequest) -> HttpResponse:
    """``/r-api/me/``: anonymous callers get empty fields instead of an error."""
    user = get_api_context(request).user
    if user is None:
        return json_response({"id": None, "username": "", "is_staff": False})
    return handle_get_me(request, QueryDict(), True)

