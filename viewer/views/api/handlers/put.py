"""PUT command handlers for the public JSON API."""

import json
from typing import Optional

from django.contrib.auth.models import User, AnonymousUser
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse, HttpRequest, HttpResponseNotFound
from django.http.request import QueryDict

from core.base.utilities import timestamp_or_zero
from viewer.models import Archive, ArchiveGroup, ArchiveGroupEntry
from viewer.utils.functions import archive_group_entry_to_json
from viewer.views.api.common import _get_int

def handle_put_archive_group(request: HttpRequest, data: QueryDict, body: dict, user: Optional[User | AnonymousUser]):
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
    request: HttpRequest, data: QueryDict, body: dict, user: Optional[User | AnonymousUser]
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

