"""POST command handlers for the public JSON API."""

import json
import logging
from typing import Optional

from django.contrib.auth.models import User, AnonymousUser
from django.http import HttpResponse, HttpRequest, HttpResponseBadRequest, HttpResponseNotFound
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
from viewer.views.api.common import (
    _get_int,
    apply_wanted_gallery_fields,
    apply_wanted_gallery_m2m_fields,
    get_tag_objects_from_tag_list,
)

logger = logging.getLogger(__name__)

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
        wanted_gallery = WantedGallery()
        apply_wanted_gallery_fields(wanted_gallery, body, is_create=True)
        wanted_gallery.save()
        apply_wanted_gallery_m2m_fields(wanted_gallery, body)
        wanted_gallery.save()

        return HttpResponse(
            json.dumps({"result": "success", "id": wanted_gallery.id}),
            content_type="application/json; charset=utf-8",
        )

    except Exception as e:
        logger.exception("Error creating WantedGallery: %s", e)
        return HttpResponseBadRequest(
            json.dumps({"result": f"Error creating WantedGallery: {str(e)}"}),
            content_type="application/json; charset=utf-8",
        )

