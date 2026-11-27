"""Background executors for batch archive/gallery mutations, commands, maintenance, and email delivery."""

import logging
from typing import Any

from django.core import management
from django.db.models import Case, When
from django.urls import reverse

from core.base.cancellation import TaskCancelledError
from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    ArchiveRecycleEntry,
    Gallery,
    GalleryGroupPossibleMatches,
    GalleryMatchGroup,
    GalleryMatchGroupEntry,
)
from viewer.utils.actions import event_log
from viewer.utils.functions import send_mass_html_mail
from workers import client

logger = logging.getLogger(__name__)


def _check_cancelled(settings_obj: Any, message: str) -> None:
    is_cancelled = getattr(settings_obj, "is_cancelled", None)
    if callable(is_cancelled) and is_cancelled():
        raise TaskCancelledError(message)


def _touch_activity(settings_obj: Any) -> None:
    activity_cb = getattr(settings_obj, "activity_callback", None)
    if callable(activity_cb):
        activity_cb()


BATCH_FETCH_CHUNK_SIZE = 500


def _ordered_archives(pks: list[int], *, select_related: tuple[str, ...] = ()):
    """Yield Archive rows in the order of ``pks``, fetched in bounded chunks without large SQL CASE expressions."""
    unique_pks = list(dict.fromkeys(pks))
    for start in range(0, len(unique_pks), BATCH_FETCH_CHUNK_SIZE):
        chunk = unique_pks[start : start + BATCH_FETCH_CHUNK_SIZE]
        qs = Archive.objects.filter(pk__in=chunk)
        if select_related:
            qs = qs.select_related(*select_related)
        by_pk = qs.in_bulk()
        for pk in chunk:
            archive = by_pk.get(pk)
            if archive is not None:
                yield archive


def _ordered_galleries(pks: list[int]):
    """Yield Gallery rows in the order of ``pks``, fetched in bounded chunks without large SQL CASE expressions."""
    unique_pks = list(dict.fromkeys(pks))
    for start in range(0, len(unique_pks), BATCH_FETCH_CHUNK_SIZE):
        chunk = unique_pks[start : start + BATCH_FETCH_CHUNK_SIZE]
        by_pk = Gallery.objects.in_bulk(chunk)
        for pk in chunk:
            gallery = by_pk.get(pk)
            if gallery is not None:
                yield gallery


def run_archive_batch_task(
    archive_pks: list[int],
    options: dict[str, Any],
    settings_obj: Any,
    user: Any = None,
    reason: str = "",
) -> dict[str, Any]:
    """Execute a batch mutation across archives in a background worker."""
    operation = str(options.get("operation") or "")
    pks = [int(pk) for pk in archive_pks]
    processed = 0
    skipped = 0

    if operation == "unpublish":
        for archive in _ordered_archives(pks):
            _check_cancelled(settings_obj, "Unpublish archives batch cancelled")
            _touch_activity(settings_obj)
            archive.set_private(reason=reason)
            if user:
                event_log(user, "UNPUBLISH_ARCHIVE", reason=reason, content_object=archive, result="unpublished")
            processed += 1

    elif operation == "change_reason":
        archive_reason = str(options.get("archive_reason", ""))
        for archive in _ordered_archives(pks):
            _check_cancelled(settings_obj, "Change archive reason batch cancelled")
            _touch_activity(settings_obj)
            archive.set_reason(archive_reason)
            if user:
                event_log(user, "CHANGE_ARCHIVE_REASON", reason=reason, content_object=archive, result="changed")
            processed += 1

    elif operation == "release_gallery":
        for archive in _ordered_archives(pks, select_related=("gallery",)):
            _check_cancelled(settings_obj, "Release gallery batch cancelled")
            _touch_activity(settings_obj)
            if not archive.gallery:
                skipped += 1
                continue
            archive.release_gallery()
            if user:
                event_log(
                    user,
                    "RELEASE_GALLERY_FROM_ARCHIVE",
                    reason=reason,
                    content_object=archive,
                    result="success",
                )
            processed += 1

    elif operation == "gallery_to_alternative":
        for archive in _ordered_archives(pks, select_related=("gallery",)):
            _check_cancelled(settings_obj, "Move gallery to alternative batch cancelled")
            _touch_activity(settings_obj)
            if not archive.gallery:
                skipped += 1
                continue
            archive.move_gallery_to_alternative()
            if user:
                event_log(
                    user,
                    "GALLERY_TO_ALTERNATIVE",
                    reason=reason,
                    content_object=archive,
                    result="success",
                )
            processed += 1

    elif operation == "recycle":
        for archive in _ordered_archives(pks):
            _check_cancelled(settings_obj, "Recycle archives batch cancelled")
            _touch_activity(settings_obj)
            if archive.is_recycled():
                skipped += 1
                continue
            entry = ArchiveRecycleEntry(
                archive=archive,
                reason=reason,
                user=user,
                origin=ArchiveRecycleEntry.ORIGIN_USER,
            )
            entry.save()
            archive.binned = True
            archive.simple_save()
            if user:
                event_log(
                    user,
                    "MOVE_TO_RECYCLE_BIN",
                    reason=reason,
                    content_object=archive,
                    result="recycled",
                )
            if archive.public:
                archive.set_private()
                if user:
                    event_log(user, "UNPUBLISH_ARCHIVE", content_object=archive, result="unpublished")
            processed += 1

    elif operation == "add_to_group":
        group_ids = [int(gid) for gid in options.get("archive_group_ids") or []]
        if group_ids:
            group_order = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(group_ids)])
            archive_groups = list(ArchiveGroup.objects.filter(pk__in=group_ids).order_by(group_order))
            for archive in _ordered_archives(pks):
                _check_cancelled(settings_obj, "Add archives to group batch cancelled")
                _touch_activity(settings_obj)
                for archive_group in archive_groups:
                    if ArchiveGroupEntry.objects.filter(archive=archive, archive_group=archive_group).exists():
                        skipped += 1
                        continue
                    ArchiveGroupEntry.objects.create(archive=archive, archive_group=archive_group)
                    if user:
                        event_log(
                            user,
                            "ADD_ARCHIVE_TO_GROUP",
                            content_object=archive,
                            reason=reason,
                            result="added",
                        )
                    processed += 1

    elif operation == "auto_select_first_match":
        for archive in _ordered_archives(pks):
            _check_cancelled(settings_obj, "Auto-select first match batch cancelled")
            _touch_activity(settings_obj)
            possible_gallery = archive.possible_matches.first()
            if not possible_gallery:
                skipped += 1
                continue
            try:
                archive.select_as_match(possible_gallery.pk)
            except ValueError:
                skipped += 1
                continue
            if archive.gallery:
                logger.info(
                    "Archive %s (%s) was matched with gallery %s (%s).",
                    archive,
                    reverse("viewer:archive", args=(archive.pk,)),
                    archive.gallery,
                    reverse("viewer:gallery", args=(archive.gallery.pk,)),
                )
                if user:
                    event_log(
                        user,
                        "MATCH_ARCHIVE",
                        reason=reason or None,
                        data=reverse("viewer:gallery", args=(archive.gallery.pk,)),
                        content_object=archive,
                        result="matched",
                    )
                processed += 1
            else:
                skipped += 1

    elif operation == "delete_similar_archives":
        delete_files = bool(options.get("delete_files", True))
        groups_raw = options.get("groups") or {}
        group_mains_raw = options.get("group_mains") or {}
        groups = {int(k): [int(x) for x in v] for k, v in groups_raw.items()}
        group_mains = {int(k): [int(x) for x in v] for k, v in group_mains_raw.items()}

        for archive in Archive.objects.filter(pk__in=pks).select_related("gallery").order_by("-create_date"):
            _check_cancelled(settings_obj, "Delete similar archives batch cancelled")
            _touch_activity(settings_obj)
            gallery = archive.gallery
            archive_report = archive.delete_text_report()

            if gallery:
                for group_id in groups.get(archive.pk, []):
                    for main_id in group_mains.get(group_id, []):
                        main_archive = Archive.objects.filter(pk=main_id).first()
                        if not main_archive:
                            continue
                        main_archive.alternative_sources.add(gallery)
                        if main_archive.public and not gallery.public:
                            gallery.set_public()

            if gallery and gallery.archive_set.count() <= 1 and gallery.alternative_sources.count() < 1:
                gallery.mark_as_deleted()

            if delete_files:
                archive.delete_all_files()
            else:
                archive.delete_files_but_archive()
            archive.delete()

            if user:
                event_log(
                    user,
                    "DELETE_ARCHIVE",
                    reason=reason,
                    content_object=gallery,
                    result="deleted",
                    data=archive_report,
                )
            processed += 1

    else:
        raise ValueError(f"Unknown archive_batch_worker operation: {operation!r}")

    return {"operation": operation, "processed": processed, "skipped": skipped}


def run_gallery_batch_task(
    gallery_pks: list[int],
    options: dict[str, Any],
    settings_obj: Any,
    user: Any = None,
    reason: str = "",
) -> dict[str, Any]:
    """Execute a batch mutation across galleries in a background worker."""
    operation = str(options.get("operation") or "")
    pks = [int(pk) for pk in gallery_pks]
    processed = 0
    skipped = 0

    if operation == "mark_deleted":
        result_label = str(options.get("event_result") or "success")
        for gallery in _ordered_galleries(pks):
            _check_cancelled(settings_obj, "Mark galleries deleted batch cancelled")
            _touch_activity(settings_obj)
            gallery.mark_as_deleted()
            if user:
                event_log(
                    user,
                    "MARK_DELETE_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result=result_label,
                )
            processed += 1

    elif operation == "delete":
        for gallery in _ordered_galleries(pks):
            _check_cancelled(settings_obj, "Delete galleries batch cancelled")
            _touch_activity(settings_obj)
            old_gallery_link = gallery.get_link()
            gallery.delete()
            if user:
                event_log(
                    user,
                    "DELETE_GALLERY",
                    reason=reason,
                    data=old_gallery_link,
                    result="success",
                )
            processed += 1

    elif operation == "publish":
        for gallery in _ordered_galleries(pks):
            _check_cancelled(settings_obj, "Publish galleries batch cancelled")
            _touch_activity(settings_obj)
            gallery.set_public()
            if user:
                event_log(
                    user,
                    "PUBLISH_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
            processed += 1

    elif operation == "private":
        for gallery in _ordered_galleries(pks):
            _check_cancelled(settings_obj, "Make galleries private batch cancelled")
            _touch_activity(settings_obj)
            gallery.set_private()
            if user:
                event_log(
                    user,
                    "UNPUBLISH_GALLERY",
                    reason=reason,
                    content_object=gallery,
                    result="success",
                )
            processed += 1

    elif operation == "auto_select_first_match":
        for gallery in _ordered_galleries(pks):
            _check_cancelled(settings_obj, "Auto-select gallery match batch cancelled")
            _touch_activity(settings_obj)
            gallery_match_group = GalleryMatchGroup.objects.filter(galleries__in=(gallery,)).first()
            if not gallery_match_group:
                skipped += 1
                continue
            gallery_match = gallery_match_group.possible_matches.first()
            if not gallery_match:
                skipped += 1
                continue
            try:
                gallery_match_group.add_gallery(gallery_match)
                gallery_match_group.save()
                GalleryGroupPossibleMatches.objects.filter(gallery_match_group=gallery_match_group).delete()
            except ValueError:
                skipped += 1
                continue
            logger.info(
                "Gallery %s (%s) was matched in a group with gallery %s (%s).",
                gallery,
                reverse("viewer:gallery", args=(gallery.pk,)),
                gallery_match,
                reverse("viewer:gallery", args=(gallery_match.pk,)),
            )
            if user:
                event_log(
                    user,
                    "ADD_GALLERY_TO_MATCH_GROUP",
                    data=reverse("viewer:gallery", args=(gallery_match.pk,)),
                    content_object=gallery_match,
                    result="matched",
                )
            processed += 1

    elif operation == "create_match_groups":
        groups_list: list[list[int]] = [[int(gid) for gid in group] for group in (options.get("groups") or [])]
        all_pks = {gid for group in groups_list for gid in group}
        gallery_mappings = Gallery.objects.in_bulk(all_pks)

        for ordered_ids in groups_list:
            _check_cancelled(settings_obj, "Create gallery match groups batch cancelled")
            _touch_activity(settings_obj)
            gallery_group = [gallery_mappings[gid] for gid in ordered_ids if gid in gallery_mappings]
            if not gallery_group:
                skipped += 1
                continue
            GalleryMatchGroup.objects.filter(galleries__in=gallery_group).delete()
            gallery_match_group = GalleryMatchGroup()
            gallery_match_group.save()
            for count, gallery in enumerate(gallery_group, start=1):
                GalleryMatchGroupEntry.objects.update_or_create(
                    gallery=gallery,
                    defaults={"gallery_match_group": gallery_match_group, "gallery_position": count},
                )
            gallery_match_group.save()
            if user:
                event_log(
                    user,
                    "CREATE_GALLERY_MATCH_GROUP",
                    reason=reason,
                    content_object=gallery_match_group,
                    result="created",
                )
            processed += 1

    elif operation == "create_and_match_groups":
        gallery_match_group_pks: list[int] = []
        for gallery in _ordered_galleries(pks):
            _check_cancelled(settings_obj, "Create and match gallery groups batch cancelled")
            _touch_activity(settings_obj)
            gallery_match_group, created = GalleryMatchGroup.objects.get_or_create(galleries__in=(gallery,))
            if created:
                gallery_match_group.galleries.add(gallery)
                gallery_match_group.save()
                if user:
                    event_log(
                        user,
                        "CREATE_GALLERY_MATCH_GROUP",
                        reason=reason or None,
                        content_object=gallery_match_group,
                        result="created",
                    )
                processed += 1
            gallery_match_group_pks.append(gallery_match_group.pk)

        if gallery_match_group_pks:
            matching_kwargs = options.get("matching_kwargs") or {}
            client.enqueue_matching_task(
                task_type="match_unmatched_gallery_groups_worker",
                args=list(dict.fromkeys(gallery_match_group_pks)),
                matching_kwargs=matching_kwargs,
                user=user,
            )

    else:
        raise ValueError(f"Unknown gallery_batch_worker operation: {operation!r}")

    return {"operation": operation, "processed": processed, "skipped": skipped}


def run_update_provider_wanted_task(
    options: dict[str, Any],
    settings_obj: Any,
    user: Any = None,
) -> dict[str, Any]:
    """Run the update_provider_wanted management command in a background worker."""
    from viewer.management.commands import update_provider_wanted

    _touch_activity(settings_obj)
    wanted_publisher = options.get("wanted_publisher")
    artist_or_group_tag = list(options.get("artist_or_group_tag") or [])
    wanted_providers = list(options.get("wanted_providers") or [])
    categories = list(options.get("categories") or [])
    should_search = bool(options.get("should_search", True))

    command_desc = (
        f"wanted_publisher: {wanted_publisher}, artist_or_group_tag: {artist_or_group_tag}, "
        f"wanted_providers: {wanted_providers}, categories: {categories}"
    )
    management.call_command(
        update_provider_wanted.Command(),
        wanted_publisher,
        wanted_providers=wanted_providers,
        categories=categories,
        artist_list=artist_or_group_tag,
        should_search=should_search,
    )
    if user:
        event_log(user, "CREATE_WANTED_GALLERY_FROM_COMMAND", data=command_desc, result="run")

    return {
        "wanted_publisher": wanted_publisher,
        "artists_count": len(artist_or_group_tag),
        "wanted_providers": wanted_providers,
        "categories": categories,
    }


def run_send_email_task(options: dict[str, Any], settings_obj: Any) -> dict[str, Any]:
    """Send batch HTML emails in a background worker so SMTP never blocks HTTP requests."""
    _touch_activity(settings_obj)
    raw_messages = options.get("messages") or []
    datatuples = tuple(
        (
            str(item[0]),
            str(item[1]),
            str(item[2]),
            item[3],
            tuple(item[4]),
        )
        for item in raw_messages
        if isinstance(item, (list, tuple)) and len(item) == 5
    )
    sent = 0
    if datatuples:
        try:
            sent = send_mass_html_mail(datatuples, fail_silently=bool(options.get("fail_silently", True)))
        except ValueError:
            logger.error("Failed sending emails: Invalid header found.")
    return {"attempted": len(datatuples), "sent": sent}
