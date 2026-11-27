"""Archive and gallery filter helpers used by the JSON API."""

from typing import Any

from django.db.models import Q, QuerySet
from django.http.request import QueryDict

from core.base.utilities import normalize_crc32
from viewer.models import Archive, ArchiveTag, Gallery
from viewer.views.head import gallery_order_fields
from viewer.views.head.common import MAX_TAG_FILTERS, apply_tag_filters, parse_bool_flag

MAX_API_TAG_FILTERS = MAX_TAG_FILTERS


def simple_archive_filter(
    args: str,
    public: bool = True,
    *,
    max_tags: int | None = MAX_API_TAG_FILTERS,
) -> "QuerySet[Archive]":
    """Simple filtering of archives."""

    # sort and filter results by parameters
    # order = '-gallery__posted'

    if public:
        order = "-public_date"
        base_results = Archive.objects.order_by(order).filter(public=True)
    else:
        order = "-create_date"
        base_results = Archive.objects.order_by(order)

    q_formatted = "%" + args.replace(" ", "%") + "%"
    results_title = base_results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))

    if not args or not args.strip():
        return results_title

    results_tags = apply_tag_filters(
        base_results,
        args,
        through_model=ArchiveTag,
        fk_field="archive_id",
        max_tags=max_tags,
    )
    if results_tags.query.is_empty():
        return results_title

    return results_tags | results_title


def filter_galleries_no_request(
    filter_args: dict[str, Any] | QueryDict,
    *,
    max_tags: int | None = MAX_API_TAG_FILTERS,
) -> "QuerySet[Gallery]":

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
    category = filter_args["category"]
    if category and isinstance(category, str):
        results = results.filter(category__icontains=category)
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
    reason = filter_args["reason"]
    if reason and isinstance(reason, str):
        results = results.filter(reason__icontains=reason)
    if filter_args["crc32"]:
        crc32 = filter_args["crc32"]
        results = results.filter(archive__crc32=normalize_crc32(crc32) if isinstance(crc32, str) else crc32)

    # Only return galleries with associated archives.
    if filter_args["used"]:
        results = results.filter(
            Q(alternative_sources__isnull=False)
            | Q(archive__isnull=False)
            | Q(gallery_container__archive__isnull=False)
        )

    filters_tags = filter_args.get("tags")

    if filters_tags and isinstance(filters_tags, str):
        results = apply_tag_filters(
            results,
            filters_tags,
            through_model=Gallery.tags.through,
            fk_field="gallery_id",
            tags_or=parse_bool_flag(filter_args.get("tags_or")),
            max_tags=max_tags,
        )

    return results
