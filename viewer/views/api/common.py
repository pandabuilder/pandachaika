"""Shared helpers for the JSON API package."""
from django.db.models import QuerySet
from django.http.request import QueryDict

from viewer.models import Gallery, Tag
from viewer.views.head import gallery_filter_keys
from viewer.views.api.filters import filter_galleries_no_request

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

