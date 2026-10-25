"""Shared helpers for the JSON API package."""
import logging
from datetime import datetime, timedelta
from typing import Any

from django.db.models import QuerySet
from django.http.request import QueryDict
from django.utils import timezone as django_tz
from django.utils.dateparse import parse_date, parse_datetime, parse_duration

from viewer.models import ArchiveGroup, Category, Gallery, Provider, Tag, WantedGallery
from viewer.views.head import gallery_filter_keys
from viewer.views.api.filters import filter_galleries_no_request

logger = logging.getLogger(__name__)

def _get_int(data, key):
    val = data.get(key)
    if isinstance(val, list):
        return int(val[0])
    elif val is not None:
        return int(val)
    raise ValueError(f"Key {key} not found")


def _get_str(data, key):
    val = data.get(key)
    if isinstance(val, list):
        return str(val[0])
    elif val is not None:
        return str(val)
    return ""


def _get_int_from_data_or_body(data: QueryDict, key: str, body: dict) -> int:
    val = data.get(key)
    if val not in (None, ""):
        return _get_int(data, key)
    if "id" in body and body["id"] not in (None, ""):
        return int(body["id"])
    raise ValueError(f"Key {key} not found")


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


def parse_wait_for_time(val: Any) -> timedelta | None:
    if val is None or val == "":
        return None
    if isinstance(val, timedelta):
        return val
    if isinstance(val, (int, float)):
        return timedelta(seconds=float(val))
    if isinstance(val, str):
        try:
            return timedelta(seconds=float(val))
        except ValueError:
            parsed = parse_duration(val)
            if parsed is not None:
                return parsed
    return None


def parse_datetime_or_timestamp(val: Any) -> datetime | None:
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        if django_tz.is_naive(val):
            return django_tz.make_aware(val, django_tz.get_current_timezone())
        return val
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=django_tz.get_current_timezone())
        except (ValueError, OSError):
            return None
    if isinstance(val, str):
        try:
            return datetime.fromtimestamp(float(val), tz=django_tz.get_current_timezone())
        except (ValueError, OSError):
            pass
        parsed_dt = parse_datetime(val)
        if parsed_dt is not None:
            if django_tz.is_naive(parsed_dt):
                return django_tz.make_aware(parsed_dt, django_tz.get_current_timezone())
            return parsed_dt
        parsed_d = parse_date(val)
        if parsed_d is not None:
            combined = datetime.combine(parsed_d, datetime.min.time())
            return django_tz.make_aware(combined, django_tz.get_current_timezone())
        try:
            import dateutil.parser

            parsed_dateutil = dateutil.parser.parse(val)
            if django_tz.is_naive(parsed_dateutil):
                return django_tz.make_aware(parsed_dateutil, django_tz.get_current_timezone())
            return parsed_dateutil
        except Exception:
            return None
    return None


def apply_wanted_gallery_fields(
    wanted_gallery: WantedGallery,
    body: dict,
    is_create: bool = False,
) -> None:
    # String / scalar fields
    simple_fields = [
        ("title", str, ""),
        ("title_jpn", str, ""),
        ("search_title", str, ""),
        ("unwanted_title", str, ""),
        ("category", str, ""),
        ("reason", str, "backup"),
        ("book_type", str, ""),
        ("publisher", str, ""),
        ("match_expression", str, None),
        ("exclusive_scope_name", str, ""),
        ("wanted_tags_accept_if_none_scope", str, ""),
    ]
    for field_name, cast_fn, default_str in simple_fields:
        if field_name in body:
            raw_val = body[field_name]
            wanted_gallery.__setattr__(
                field_name,
                cast_fn(raw_val) if raw_val is not None else default_str,
            )
        elif is_create and default_str is not None:
            wanted_gallery.__setattr__(field_name, default_str)

    # Boolean fields
    bool_fields = [
        ("regexp_search_title", False),
        ("regexp_search_title_icase", False),
        ("regexp_unwanted_title", False),
        ("regexp_unwanted_title_icase", False),
        ("wanted_tags_exclusive_scope", False),
        ("should_search", False),
        ("keep_searching", False),
        ("notify_when_found", True),
        ("restricted_to_links", False),
        ("public", False),
    ]
    for field_name, default_bool in bool_fields:
        if field_name in body:
            wanted_gallery.__setattr__(field_name, bool(body[field_name]))
        elif is_create:
            wanted_gallery.__setattr__(field_name, default_bool)

    # Integer fields
    int_fields = [
        ("wanted_page_count_lower", 0),
        ("wanted_page_count_upper", 0),
        ("page_count", 0),
    ]
    for field_name, default_int in int_fields:
        if field_name in body:
            raw_val = body[field_name]
            try:
                wanted_gallery.__setattr__(field_name, int(raw_val) if raw_val is not None else 0)
            except (ValueError, TypeError):
                wanted_gallery.__setattr__(field_name, default_int)
        elif is_create:
            wanted_gallery.__setattr__(field_name, default_int)

    # wait_for_time
    if "wait_for_time" in body:
        wanted_gallery.wait_for_time = parse_wait_for_time(body["wait_for_time"])
    elif is_create and "wait_for_time" not in body:
        wanted_gallery.wait_for_time = None

    # release_date
    if "release_date" in body:
        wanted_gallery.release_date = parse_datetime_or_timestamp(body["release_date"])
    elif is_create and "release_date" not in body:
        wanted_gallery.release_date = django_tz.now()

    # found and date_found
    if "found" in body:
        wanted_gallery.found = bool(body["found"])
        if wanted_gallery.found and not wanted_gallery.date_found and "date_found" not in body:
            wanted_gallery.date_found = django_tz.now()
        elif not wanted_gallery.found and "date_found" not in body:
            wanted_gallery.date_found = None
    elif is_create:
        wanted_gallery.found = False

    if "date_found" in body:
        wanted_gallery.date_found = parse_datetime_or_timestamp(body["date_found"])

    # add_to_archive_group
    if "add_to_archive_group" in body:
        group_id = body["add_to_archive_group"]
        if group_id in (None, ""):
            wanted_gallery.add_to_archive_group = None
        else:
            try:
                wanted_gallery.add_to_archive_group = ArchiveGroup.objects.get(pk=int(group_id))
            except (ArchiveGroup.DoesNotExist, ValueError, TypeError):
                wanted_gallery.add_to_archive_group = None


def apply_wanted_gallery_m2m_fields(
    wanted_gallery: WantedGallery,
    body: dict,
) -> None:
    if "wanted_tags" in body:
        tag_objects = get_tag_objects_from_tag_list(body["wanted_tags"])
        wanted_gallery.wanted_tags.set(tag_objects)

    if "unwanted_tags" in body:
        tag_objects = get_tag_objects_from_tag_list(body["unwanted_tags"])
        wanted_gallery.unwanted_tags.set(tag_objects)

    if "wanted_providers" in body:
        slugs = body["wanted_providers"]
        if isinstance(slugs, str):
            slugs = [s.strip() for s in slugs.split(",") if s.strip()]
        providers = Provider.objects.filter(slug__in=slugs)
        wanted_gallery.wanted_providers.set(providers)

    if "unwanted_providers" in body:
        slugs = body["unwanted_providers"]
        if isinstance(slugs, str):
            slugs = [s.strip() for s in slugs.split(",") if s.strip()]
        providers = Provider.objects.filter(slug__in=slugs)
        wanted_gallery.unwanted_providers.set(providers)

    if "categories" in body:
        cat_names = body["categories"]
        if isinstance(cat_names, str):
            cat_names = [c.strip() for c in cat_names.split(",") if c.strip()]
        cat_objs = []
        for cat_name in cat_names:
            clean_name = cat_name.strip()
            if clean_name:
                cat_obj, _ = Category.objects.get_or_create(name=clean_name)
                cat_objs.append(cat_obj)
        wanted_gallery.categories.set(cat_objs)


