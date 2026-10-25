"""PUT command handlers for the public JSON API."""

import json
import logging

from django.contrib.auth.models import User, AnonymousUser
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse, HttpRequest, HttpResponseBadRequest, HttpResponseNotFound
from django.http.request import QueryDict

from core.base.utilities import timestamp_or_zero
from viewer.models import Archive, ArchiveGroup, ArchiveGroupEntry, WantedGallery
from viewer.utils.functions import archive_group_entry_to_json, wanted_gallery_to_json
from viewer.views.api.common import (
    _get_int,
    _get_int_from_data_or_body,
    apply_wanted_gallery_fields,
    apply_wanted_gallery_m2m_fields,
)

logger = logging.getLogger(__name__)

def handle_put_archive_group(request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None):
    try:
        archive_group_id = _get_int(data, "archive-group")
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


def handle_put_archive_group_entry(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    try:
        archive_group_entry_id = _get_int(data, "archive-group-entry")
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


def handle_put_wanted_gallery(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    try:
        wanted_gallery_id = _get_int_from_data_or_body(data, "wanted-gallery", body)
    except (ValueError, TypeError):
        return HttpResponseNotFound(
            json.dumps({"result": "WantedGallery does not exist."}),
            content_type="application/json; charset=utf-8",
        )

    try:
        with transaction.atomic():
            wanted_gallery = WantedGallery.objects.select_for_update(of=("self",)).get(pk=wanted_gallery_id)
            apply_wanted_gallery_fields(wanted_gallery, body, is_create=False)
            wanted_gallery.save()
            apply_wanted_gallery_m2m_fields(wanted_gallery, body)
            wanted_gallery.save()

        updated_wanted_gallery = (
            WantedGallery.objects.prefetch_related(
                "wanted_tags",
                "unwanted_tags",
                "wanted_providers",
                "unwanted_providers",
                "categories",
            ).get(pk=wanted_gallery_id)
        )

        response_data = wanted_gallery_to_json(
            updated_wanted_gallery,
            request=request,
            user_is_authenticated=True,
        )
        response_data["result"] = "success"
        return HttpResponse(
            json.dumps(response_data, sort_keys=True, ensure_ascii=False),
            content_type="application/json; charset=utf-8",
        )

    except WantedGallery.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "WantedGallery does not exist."}),
            content_type="application/json; charset=utf-8",
        )
    except Exception as e:
        logger.exception("Error updating WantedGallery: %s", e)
        return HttpResponseBadRequest(
            json.dumps({"result": f"Error updating WantedGallery: {str(e)}"}),
            content_type="application/json; charset=utf-8",
        )


