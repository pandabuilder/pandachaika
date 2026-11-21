"""DELETE command handlers for the public JSON API."""

import json
import logging
from typing import Any

from django.contrib.auth.models import User, AnonymousUser
from django.db import transaction
from django.http import HttpResponse, HttpRequest
from django.http.request import QueryDict

from viewer.models import Archive, ArchiveGroup, ArchiveGroupEntry, Gallery, WantedGallery
from viewer.utils.actions import event_log
from viewer.views.api.common import (
    _get_int,
    _get_int_from_data_or_body,
    get_gallery_by_gid,
    get_str_from_data_or_body,
    parse_bool_from_data_or_body,
)
from viewer.views.api.errors import ApiError
from viewer.views.api.handlers.tasks import task_reference, tasks_reference
from workers import client

logger = logging.getLogger(__name__)

def handle_delete_archive_group(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    try:
        archive_group_id = _get_int(data, "archive-group")
    except ValueError:
        raise ApiError.not_found("ArchiveGroup does not exist.")
    try:
        with transaction.atomic():
            archive_group = ArchiveGroup.objects.select_for_update(of=("self",)).get(pk=archive_group_id)

            delete_number = archive_group.delete()[0]

        return HttpResponse(json.dumps({"result": delete_number}), content_type="application/json; charset=utf-8")

    except ArchiveGroup.DoesNotExist:
        raise ApiError.not_found("ArchiveGroup does not exist.")


def handle_delete_archive_group_entry(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    try:
        archive_group_entry_id = _get_int(data, "archive-group-entry")
    except ValueError:
        raise ApiError.not_found("ArchiveGroupEntry does not exist.")
    try:
        with transaction.atomic():
            archive_group_entry = ArchiveGroupEntry.objects.select_for_update(of=("self",)).get(
                pk=archive_group_entry_id
            )

            delete_number = archive_group_entry.delete()[0]

        return HttpResponse(json.dumps({"result": delete_number}), content_type="application/json; charset=utf-8")

    except ArchiveGroupEntry.DoesNotExist:
        raise ApiError.not_found("ArchiveGroupEntry does not exist.")


def handle_delete_wanted_gallery(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    try:
        wanted_gallery_id = _get_int_from_data_or_body(data, "wanted-gallery", body)
    except (ValueError, TypeError):
        raise ApiError.not_found("WantedGallery does not exist.")
    try:
        with transaction.atomic():
            wanted_gallery = WantedGallery.objects.select_for_update(of=("self",)).get(pk=wanted_gallery_id)
            delete_number = wanted_gallery.delete()[0]

        return HttpResponse(
            json.dumps({"result": "success", "id": wanted_gallery_id, "deleted": delete_number}),
            content_type="application/json; charset=utf-8",
        )

    except WantedGallery.DoesNotExist:
        raise ApiError.not_found("WantedGallery does not exist.")


def _parse_archive_delete_options(data: QueryDict, body: dict, user: User | AnonymousUser | None) -> dict[str, Any]:
    delete_zipped, has_zipped = parse_bool_from_data_or_body(data, body, "delete_zipped", "delete-zipped")
    delete_archive, has_archive = parse_bool_from_data_or_body(data, body, "delete_archive", "delete-archive")
    delete_file, has_file = parse_bool_from_data_or_body(data, body, "delete_file", "delete-file")

    mark_gallery_deleted, _ = parse_bool_from_data_or_body(
        data, body, "mark_gallery_deleted", "mark-gallery-deleted"
    )
    mark_gallery_denied, _ = parse_bool_from_data_or_body(data, body, "mark_gallery_denied", "mark-gallery-denied")
    delete_gallery, _ = parse_bool_from_data_or_body(data, body, "delete_gallery", "delete-gallery")

    # Enforce permissions for optional gallery actions before doing any work.
    if mark_gallery_deleted and (not user or not user.has_perm("viewer.mark_delete_gallery")):
        raise ApiError.forbidden("Not authorized")
    if mark_gallery_denied and (not user or not user.has_perm("viewer.approve_gallery")):
        raise ApiError.forbidden("Not authorized")
    if delete_gallery and (not user or not user.has_perm("viewer.delete_gallery")):
        raise ApiError.forbidden("Not authorized")

    gallery_actions = sum(bool(x) for x in (mark_gallery_deleted, mark_gallery_denied, delete_gallery))
    if gallery_actions > 1:
        raise ApiError.bad_request("Only one gallery action may be specified at a time.")

    if not (has_zipped or has_archive or has_file):
        # Default when no archive deletion flags are explicitly passed: delete archive entry and associated file.
        delete_archive = True
        delete_file = True
        delete_zipped = False
    elif delete_zipped:
        # Matches UI and ArchiveWorker: delete_zipped overrides delete_archive/delete_file (recycles archive).
        delete_archive = False
        delete_file = False

    if not any((delete_zipped, delete_archive, delete_file, mark_gallery_deleted, mark_gallery_denied, delete_gallery)):
        raise ApiError.bad_request("No deletion option selected.")

    user_reason = get_str_from_data_or_body(data, body, "reason")[:200]
    options: dict[str, Any] = {
        "delete_zipped": delete_zipped,
        "delete_archive": delete_archive,
        "delete_file": delete_file,
        "delete_gallery": delete_gallery,
        "mark_gallery_deleted": mark_gallery_deleted,
        "mark_gallery_denied": mark_gallery_denied,
        "user_reason": user_reason,
    }
    return options


def handle_delete_archive(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    options = _parse_archive_delete_options(data, body, user)

    try:
        archive_id = _get_int_from_data_or_body(data, "archive", body)
    except (ValueError, TypeError):
        raise ApiError.not_found("Archive does not exist.")

    try:
        archive = Archive.objects.get(pk=archive_id)
    except Archive.DoesNotExist:
        raise ApiError.not_found("Archive does not exist.")

    task = client.enqueue(
        args=[archive.pk],
        task_type="delete_archive_worker",
        override_options=options,
        user=user,
        reason=options["user_reason"],
        event_action="DELETE_ARCHIVE",
    )
    logger.info(
        "User %s: queued deletion via API for archive #%d (task #%d)",
        getattr(user, "username", "anonymous"),
        archive.pk,
        task.pk,
    )
    return HttpResponse(
        json.dumps(
            {
                "result": "success",
                "id": archive.pk,
                **task_reference(request, task),
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        content_type="application/json; charset=utf-8",
    )


def _delete_archives_for_gallery(
    request: HttpRequest, gallery: Gallery, data: QueryDict, body: dict, user: User | AnonymousUser | None
) -> HttpResponse:
    options = _parse_archive_delete_options(data, body, user)

    archives_qs = gallery.archive_set.all().order_by("pk")

    # Optional subset of archive IDs belonging to this gallery.
    raw_archive_ids: list[Any] = []
    if isinstance(body, dict) and "archive_ids" in body and body["archive_ids"] is not None:
        if isinstance(body["archive_ids"], list):
            raw_archive_ids = body["archive_ids"]
        else:
            raw_archive_ids = [body["archive_ids"]]
    elif "archive_ids" in data:
        raw_archive_ids = data.getlist("archive_ids")
    elif "archive_id" in data:
        raw_archive_ids = data.getlist("archive_id")

    if raw_archive_ids:
        try:
            archive_id_filter = [int(x) for x in raw_archive_ids]
        except (ValueError, TypeError):
            raise ApiError.bad_request("Invalid archive_ids parameter.")
        archives_qs = archives_qs.filter(pk__in=archive_id_filter)

    archives = list(archives_qs)
    gallery_pk = gallery.pk
    gallery_gid = gallery.gid
    gallery_provider = gallery.provider
    user_reason = options["user_reason"]

    task_ids: list[int] = []
    archive_ids: list[int] = []
    gallery_action_applied: str | None = None

    if archives:
        for idx, archive in enumerate(archives):
            task_options = dict(options)
            if idx > 0:
                task_options["mark_gallery_deleted"] = False
                task_options["mark_gallery_denied"] = False
                task_options["delete_gallery"] = False
            task = client.enqueue(
                args=[archive.pk],
                task_type="delete_archive_worker",
                override_options=task_options,
                user=user,
                reason=user_reason,
                event_action="DELETE_ARCHIVE",
            )
            task_ids.append(task.pk)
            archive_ids.append(archive.pk)
        if options["mark_gallery_deleted"]:
            gallery_action_applied = "mark_deleted"
        elif options["mark_gallery_denied"]:
            gallery_action_applied = "mark_denied"
        elif options["delete_gallery"]:
            gallery_action_applied = "delete"
    elif options["mark_gallery_deleted"] or options["mark_gallery_denied"] or options["delete_gallery"]:
        with transaction.atomic():
            locked_gallery = Gallery.objects.select_for_update(of=("self",)).get(pk=gallery_pk)
            if options["mark_gallery_deleted"]:
                locked_gallery.mark_as_deleted()
                gallery_action_applied = "mark_deleted"
                if user and getattr(user, "is_authenticated", False):
                    event_log(
                        user,
                        "MARK_DELETE_GALLERY",
                        content_object=locked_gallery,
                        result="deleted",
                        reason=user_reason,
                    )
            elif options["mark_gallery_denied"]:
                locked_gallery.mark_as_denied()
                gallery_action_applied = "mark_denied"
                if user and getattr(user, "is_authenticated", False):
                    event_log(
                        user,
                        "MARK_DENIED_GALLERY",
                        content_object=locked_gallery,
                        result="success",
                        reason=user_reason,
                    )
            elif options["delete_gallery"]:
                old_gallery_link = locked_gallery.get_link()
                locked_gallery.delete()
                gallery_action_applied = "delete"
                if user and getattr(user, "is_authenticated", False):
                    event_log(
                        user,
                        "DELETE_GALLERY",
                        reason=user_reason,
                        data=old_gallery_link,
                        result="deleted",
                    )

    logger.info(
        "User %s: API delete for gallery #%d (gid=%s): %d archive task(s) queued%s",
        getattr(user, "username", "anonymous"),
        gallery_pk,
        gallery_gid,
        len(task_ids),
        f", gallery_action={gallery_action_applied}" if gallery_action_applied else "",
    )

    return HttpResponse(
        json.dumps(
            {
                "result": "success",
                "id": gallery_pk,
                "gid": gallery_gid,
                "provider": gallery_provider,
                "archives": archive_ids,
                **tasks_reference(request, task_ids),
                "gallery_action": gallery_action_applied,
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        content_type="application/json; charset=utf-8",
    )


def handle_delete_gid(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    options = _parse_archive_delete_options(data, body, user)

    gallery = get_gallery_by_gid(data, body)
    if not gallery:
        raise ApiError.not_found("Gallery does not exist.")

    return _delete_archives_for_gallery(request, gallery, data, body, user)


def handle_delete_gallery(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    options = _parse_archive_delete_options(data, body, user)

    try:
        gallery_id = _get_int_from_data_or_body(data, "gallery", body)
    except (ValueError, TypeError):
        raise ApiError.not_found("Gallery does not exist.")

    try:
        gallery = Gallery.objects.get(pk=gallery_id)
    except Gallery.DoesNotExist:
        raise ApiError.not_found("Gallery does not exist.")

    return _delete_archives_for_gallery(request, gallery, data, body, user)


