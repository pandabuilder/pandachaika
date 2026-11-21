"""POST command handlers for the public JSON API."""

import json
import logging

from django.contrib.auth.models import User, AnonymousUser
from django.http import HttpResponse, HttpRequest
from django.http.request import QueryDict

from core.base.utilities import timestamp_or_zero
from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    Gallery,
    WantedGallery,
)
from viewer.utils.functions import archive_group_entry_to_json
from viewer.views.api.errors import ApiError
from viewer.views.api.handlers.tasks import task_reference
from viewer.views.api.common import (
    _get_int,
    _get_str,
    apply_wanted_gallery_fields,
    apply_wanted_gallery_m2m_fields,
    get_gallery_by_gid,
    get_str_from_data_or_body,
)
from workers import client

logger = logging.getLogger(__name__)

def handle_post_archive_group(request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None):
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
                raise ApiError.not_found("Archive does not exist.")

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
        raise ApiError.not_found("ArchiveGroup does not exist.")


def handle_post_archive_group_entry(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    try:
        archive_group_id = _get_int(data, "archive-group-entry")
    except ValueError:
        raise ApiError.not_found("ArchiveGroup does not exist.")
    try:
        archive_group = ArchiveGroup.objects.get(pk=archive_group_id)
    except ArchiveGroup.DoesNotExist:
        raise ApiError.not_found("ArchiveGroup does not exist.")

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
        raise ApiError.not_found("Archive does not exist.")


def handle_post_wanted_gallery(request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None):
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
        raise ApiError.bad_request(f"Error creating WantedGallery: {str(e)}")


def handle_post_update_metadata(
    request: HttpRequest, data: QueryDict, body: dict, user: User | AnonymousUser | None
):
    reason = get_str_from_data_or_body(data, body, "reason")[:200]
    provider = get_str_from_data_or_body(data, body, "provider")

    # 1. Batch lookup by gids (query param ?gids=1&gids=2 or JSON body {"gids": [...]})
    raw_gids: list[str] = []
    if isinstance(body, dict) and "gids" in body and body["gids"] is not None:
        if isinstance(body["gids"], list):
            raw_gids = [str(g).strip() for g in body["gids"] if str(g).strip()]
        elif isinstance(body["gids"], str):
            raw_gids = [g.strip() for g in body["gids"].split(",") if g.strip()]
    elif "gids" in data:
        raw_gids = [g.strip() for g in data.getlist("gids") if g.strip()]

    if raw_gids:
        galleries: list[Gallery] = []
        seen_pks: set[int] = set()
        for gid_val in raw_gids:
            if provider:
                g = Gallery.objects.filter_first(gid=gid_val, provider=provider)
            else:
                g = Gallery.objects.filter_first(gid=gid_val)
            if g and g.pk not in seen_pks:
                seen_pks.add(g.pk)
                galleries.append(g)

        if not galleries:
            raise ApiError.not_found("Gallery does not exist.")

        valid_galleries = [g for g in galleries if g.provider]
        if not valid_galleries:
            raise ApiError.bad_request("No galleries with a provider to update.")

        links = [g.get_link() for g in valid_galleries]
        task = client.enqueue_gallery_update(
            links,
            user=user,
            reason=reason or None,
            override_options={"gallery_reason": reason} if reason else None,
        )
        logger.info(
            "User %s: queued metadata update via API for %d gallery(ies) (task #%d)",
            getattr(user, "username", "anonymous"),
            len(valid_galleries),
            task.pk,
        )
        return HttpResponse(
            json.dumps(
                {
                    "result": "success",
                    "count": len(valid_galleries),
                    **task_reference(request, task),
                    "galleries": [
                        {
                            "id": g.pk,
                            "gid": g.gid,
                            "provider": g.provider,
                            "link": g.get_link(),
                        }
                        for g in valid_galleries
                    ],
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
            content_type="application/json; charset=utf-8",
        )

    # 2. Single gallery lookup by gid, gallery ID, or archive ID
    gallery: Gallery | None = None
    has_gid = bool(_get_str(data, "gid").strip()) or (
        isinstance(body, dict) and body.get("gid") not in (None, "")
    )
    has_gallery_id = bool(_get_str(data, "gallery").strip()) or (
        isinstance(body, dict) and (body.get("gallery") not in (None, "") or body.get("id") not in (None, ""))
    )
    has_archive_id = bool(_get_str(data, "archive").strip()) or (
        isinstance(body, dict) and body.get("archive") not in (None, "")
    )

    if has_gid:
        gallery = get_gallery_by_gid(data, body)
        if not gallery:
            raise ApiError.not_found("Gallery does not exist.")
    elif has_gallery_id:
        raw_gallery_id = _get_str(data, "gallery").strip()
        if not raw_gallery_id and isinstance(body, dict):
            raw_gallery_id = str(body.get("gallery") if body.get("gallery") not in (None, "") else body.get("id", ""))
        try:
            gallery = Gallery.objects.get(pk=int(raw_gallery_id))
        except (ValueError, TypeError, Gallery.DoesNotExist):
            raise ApiError.not_found("Gallery does not exist.")
    elif has_archive_id:
        raw_archive_id = _get_str(data, "archive").strip()
        if not raw_archive_id and isinstance(body, dict):
            raw_archive_id = str(body.get("archive", ""))
        try:
            archive = Archive.objects.select_related("gallery").get(pk=int(raw_archive_id))
        except (ValueError, TypeError, Archive.DoesNotExist):
            raise ApiError.not_found("Archive does not exist.")
        if not archive.gallery:
            raise ApiError.bad_request("No gallery associated with this archive.")
        gallery = archive.gallery
    else:
        raise ApiError.bad_request("Missing gallery identifier (gid, gids, gallery, or archive).")

    if not gallery.provider:
        raise ApiError.bad_request("Gallery has no provider.")

    link = gallery.get_link()
    task = client.enqueue_gallery_update(
        [link],
        user=user,
        reason=reason or None,
        override_options={"gallery_reason": reason} if reason else None,
    )
    logger.info(
        "Updating gallery API data for gallery: %s and related archives (task #%d)",
        gallery.get_absolute_url(),
        task.pk,
    )
    return HttpResponse(
        json.dumps(
            {
                "result": "success",
                "id": gallery.pk,
                "gid": gallery.gid,
                "provider": gallery.provider,
                "link": link,
                **task_reference(request, task),
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
        content_type="application/json; charset=utf-8",
    )

