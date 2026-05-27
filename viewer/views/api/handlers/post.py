"""POST command handlers for the public JSON API."""

import json
from typing import Optional

from django.contrib.auth.models import User, AnonymousUser
from django.http import HttpResponse, HttpRequest, HttpResponseNotFound
from django.http.request import QueryDict

from core.base.utilities import timestamp_or_zero
from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    WantedGallery,
    Category,
    Provider,
)
from viewer.utils.functions import archive_group_entry_to_json
from viewer.views.api.common import _get_int, get_tag_objects_from_tag_list

def handle_post_archive_group(request: HttpRequest, data: QueryDict, body: dict, user: Optional[User | AnonymousUser]):
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


def handle_post_archive_group_entry(
    request: HttpRequest, data: QueryDict, body: dict, user: Optional[User | AnonymousUser]
):
    try:
        archive_group_id = _get_int(data, "archive-group-entry")
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


def handle_post_wanted_gallery(request: HttpRequest, data: QueryDict, body: dict, user: Optional[User | AnonymousUser]):
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

