"""Select galleries with a small set of filters.

Used by the gallery picker widget API (chaika-apps ``admin-widgets``) and by the pages that receive its value, e.g.
marking galleries as found for a wanted gallery. Filters are always cleaned on the server.
"""

import datetime
import re
import typing
from collections.abc import Mapping

from django.conf import settings
from django.db.models import F, Q

from viewer.models import Gallery
from viewer.views.head.common import apply_tag_filters

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, User

    from viewer.models.gallery import GalleryQuerySet

TEXT_FILTERS = ["title", "tags", "link", "provider", "category", "uploader"]
NUMBER_FILTERS = ["filecount_from", "filecount_to", "filesize_from", "filesize_to"]
DATE_FILTERS = ["posted_from", "posted_to"]
FLAG_FILTERS = ["not_used", "public", "non_public", "hidden", "any_status"]
STAFF_FILTERS = {"any_status"}

ALL_FILTERS = TEXT_FILTERS + NUMBER_FILTERS + DATE_FILTERS + FLAG_FILTERS
ORDER_FIELDS = ("posted", "create_date", "title", "filecount", "filesize", "category", "provider", "uploader")
MAX_TEXT_LENGTH = 500

MAX_GALLERY_IDS = 1000
MAX_RANGE_SIZE = 1000
_ID_TOKEN = re.compile(r"^(\d+)(?:-(\d+))?$")
_GALLERY_LINK = re.compile(r"/gallery/(\d+)")


def can_use_gallery_picker(user: "User | AnonymousUser | None") -> bool:
    """Logged in users already see every gallery in the gallery list, so they may search them here too."""
    return user is not None and user.is_authenticated


def allowed_filter_keys(user: "User | AnonymousUser | None") -> list[str]:
    if not can_use_gallery_picker(user):
        return []
    if getattr(user, "is_staff", False):
        return list(ALL_FILTERS)
    return [key for key in ALL_FILTERS if key not in STAFF_FILTERS]


def parse_gallery_ids(text: str) -> list[int]:
    """Parse gallery ids separated by commas, semicolons or whitespace. Ranges (``10-20``) and gallery links
    (``/gallery/42/``) are accepted.

    :raises ValueError: on a token that is not an id, a range or a gallery link, or on too many ids.
    """
    ids: list[int] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        url_match = _GALLERY_LINK.search(token)
        if url_match:
            ids.append(int(url_match.group(1)))
            continue
        match = _ID_TOKEN.match(token)
        if not match:
            raise ValueError(f"'{token}' is not a gallery id, a range (10-20) or a gallery link.")
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            start, end = end, start
        if end - start + 1 > MAX_RANGE_SIZE:
            raise ValueError(f"Range '{token}' is too large (max {MAX_RANGE_SIZE} ids).")
        ids.extend(range(start, end + 1))
        if len(ids) > MAX_GALLERY_IDS:
            raise ValueError(f"Too many gallery ids (max {MAX_GALLERY_IDS}).")
    return [x for x in dict.fromkeys(ids) if x > 0]


def clean_filter(params: Mapping[str, typing.Any], user: "User | AnonymousUser | None") -> dict[str, str]:
    """Keep only the filters the user may use, with valid values. Empty values are dropped.

    :raises ValueError: for a number or date filter with an invalid value.
    """
    allowed = set(allowed_filter_keys(user))
    cleaned: dict[str, str] = {}
    for key in ALL_FILTERS:
        if key not in allowed or key not in params:
            continue
        value = params[key]
        if isinstance(value, bool):
            value = "1" if value else ""
        if not isinstance(value, (str, int, float)):
            continue
        value = str(value).strip()[:MAX_TEXT_LENGTH]
        if not value or (key in FLAG_FILTERS and value.lower() in ("0", "false", "off")):
            continue
        if key in FLAG_FILTERS:
            value = "1"
        elif key in NUMBER_FILTERS:
            try:
                float(value)
            except ValueError:
                raise ValueError(f"Filter '{key}' must be a number, got '{value}'.")
        elif key in DATE_FILTERS:
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"Filter '{key}' must be a date (YYYY-MM-DD), got '{value}'.")
        cleaned[key] = value
    sort = str(params.get("sort", "") or "")
    if sort in ORDER_FIELDS:
        cleaned["sort"] = sort
    asc_desc = str(params.get("asc_desc", "") or "")
    if asc_desc in ("asc", "desc"):
        cleaned["asc_desc"] = asc_desc
    return cleaned


def _link_query(value: str) -> Q:
    """Galleries for a provider URL (resolved by the parsers), or with this gid."""
    query = Q(gid=value)
    crawler_settings = settings.CRAWLER_SETTINGS
    for parser in crawler_settings.provider_context.get_parsers(crawler_settings):
        if not parser.id_from_url_implemented():
            continue
        for url in parser.filter_accepted_urls((value,)):
            gid = parser.id_from_url(url)
            if gid:
                query |= Q(gid=gid, provider=parser.name)
    return query


def filter_galleries(params: Mapping[str, str]) -> "GalleryQuerySet":
    """Galleries matching a cleaned filter (from :func:`clean_filter`). Only normal galleries unless ``any_status``."""
    order = params.get("sort") or "posted"
    if params.get("asc_desc") == "asc":
        results = Gallery.objects.all().order_by(F(order).asc(nulls_last=True), "pk")
    else:
        results = Gallery.objects.all().order_by(F(order).desc(nulls_last=True), "-pk")

    if not params.get("any_status"):
        results = results.eligible_for_use()

    if params.get("title"):
        q_formatted = "%" + params["title"].replace(" ", "%") + "%"
        results = results.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))
    if params.get("link"):
        results = results.filter(_link_query(params["link"]))
    if params.get("provider"):
        results = results.filter(provider=params["provider"])
    if params.get("category"):
        results = results.filter(category__icontains=params["category"])
    if params.get("uploader"):
        results = results.filter(uploader=params["uploader"])
    if params.get("filecount_from"):
        results = results.filter(filecount__gte=int(float(params["filecount_from"])))
    if params.get("filecount_to"):
        results = results.filter(filecount__lte=int(float(params["filecount_to"])))
    if params.get("filesize_from"):
        results = results.filter(filesize__gte=float(params["filesize_from"]))
    if params.get("filesize_to"):
        results = results.filter(filesize__lte=float(params["filesize_to"]))
    if params.get("posted_from"):
        results = results.filter(posted__date__gte=datetime.date.fromisoformat(params["posted_from"]))
    if params.get("posted_to"):
        results = results.filter(posted__date__lte=datetime.date.fromisoformat(params["posted_to"]))
    if params.get("public"):
        results = results.filter(public=True)
    if params.get("non_public"):
        results = results.filter(public=False)
    if params.get("hidden"):
        results = results.filter(hidden=True)
    if params.get("not_used"):
        results = results.filter(archive__isnull=True)
    if params.get("tags"):
        results = apply_tag_filters(results, params["tags"], through_model=Gallery.tags.through, fk_field="gallery_id")
    return results


def galleries_by_ids(ids: list[int], user: "User | AnonymousUser | None") -> "GalleryQuerySet":
    """The galleries with these ids that the user may select (non staff: only normal galleries)."""
    results = Gallery.objects.filter(pk__in=ids)
    if not getattr(user, "is_staff", False):
        results = results.eligible_for_use()
    return results
