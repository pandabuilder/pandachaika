"""DELETE command handlers for the public JSON API."""

import json
from typing import Any, Optional

from django.contrib.auth.models import User, AnonymousUser
from django.db import transaction
from django.http import HttpResponse, HttpRequest, HttpResponseNotFound
from django.http.request import QueryDict

from viewer.models import ArchiveGroup, ArchiveGroupEntry
from viewer.views.api.common import _get_int

def handle_delete_archive_group(
    request: HttpRequest, data: QueryDict, body: dict, user: Optional[User | AnonymousUser]
):
    try:
        archive_group_id = _get_int(data, "archive-group")
    except ValueError:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
        )
    try:
        with transaction.atomic():
            archive_group = ArchiveGroup.objects.select_for_update(of=("self",)).get(pk=archive_group_id)

            delete_number = archive_group.delete()[0]

        return HttpResponse(json.dumps({"result": delete_number}), content_type="application/json; charset=utf-8")

    except ArchiveGroup.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroup does not exist."}), content_type="application/json; charset=utf-8"
        )


def handle_delete_archive_group_entry(
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
            archive_group_entry = ArchiveGroupEntry.objects.select_for_update(of=("self",)).get(
                pk=archive_group_entry_id
            )

            delete_number = archive_group_entry.delete()[0]

        return HttpResponse(json.dumps({"result": delete_number}), content_type="application/json; charset=utf-8")

    except ArchiveGroupEntry.DoesNotExist:
        return HttpResponseNotFound(
            json.dumps({"result": "ArchiveGroupEntry does not exist."}),
            content_type="application/json; charset=utf-8",
        )

