"""Select archives with the archive manager filters.

Used by the archive manager listing, the archive picker widget API, and by worker jobs that run on "every archive
matching this filter" (e.g. the archive file check). Which filters a user may use depends on their profile; filters
are always cleaned on the server, hiding them in the widget is only cosmetic.
"""

import datetime
import typing
from collections.abc import Mapping

from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet

from viewer.models import Archive, ArchiveGroupEntry, ArchiveManageEntry, ArchiveTag, Gallery, UserArchivePrefs
from viewer.services import cold_storage
from viewer.views.head import archive_filter_keys, archive_order_fields, filter_archives_simple

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import AnonymousUser, User

TEXT_FILTERS = [
    "title",
    "tags",
    "filename",
    "source_type",
    "reason",
    "category",
    "provider",
    "uploader",
    "match_type",
    "extra_files",
    "extra_file_name",
    "extra_file_type",
    "mark_reason",
    "mark_comment",
    "mark_extra",
    "origin",
    "storage",
    "archive_group",
]
NUMBER_FILTERS = [
    "filecount_from",
    "filecount_to",
    "filesize_from",
    "filesize_to",
    "extra_file_size_from",
    "extra_file_size_to",
    "priority_from",
    "priority_to",
]
DATE_FILTERS = ["created_from", "created_to"]
FLAG_FILTERS = [
    "non_public",
    "public",
    "hidden",
    "extracted",
    "downloading",
    "diff-filesize",
    "gallery-other-archives",
    "gallery-has-child",
    "no-custom-tags",
    "with-custom-tags",
    "with-scopeless-tags",
    "gallery-in-queue",
    "marked",
    "and-mark-conditions",
    "recycled",
    "file_deleted",
]
SORT_KEYS = ["sort", "asc_desc"]

MARK_FILTERS = {
    "marked",
    "mark_reason",
    "mark_comment",
    "mark_extra",
    "origin",
    "priority_from",
    "priority_to",
    "and-mark-conditions",
}
ADMIN_FILTERS = {"recycled", "file_deleted", "storage"}
# "storage": where the archive file is, "local" or "cold" (offloaded to the cold storage bucket).
STORAGE_CHOICES = ("local", "cold")
# Three-way filters: "1" (only these archives), "any" (ignore the filter); anything else excludes them (the default).
TRISTATE_FILTERS = {"recycled", "file_deleted"}
TRISTATE_ANY = "any"
# "archive_group": an archive group id, or "none" for the archives that aren't in any group.
ARCHIVE_GROUP_NONE = "none"

ALL_FILTERS = TEXT_FILTERS + NUMBER_FILTERS + DATE_FILTERS + FLAG_FILTERS
MAX_TEXT_LENGTH = 500


def can_use_archive_picker(user: "User | AnonymousUser | None") -> bool:
    if user is None or not user.is_authenticated:
        return False
    return bool(getattr(user, "is_staff", False) or user.has_perm("viewer.manage_archive"))


def allowed_filter_keys(user: "User | AnonymousUser | None") -> list[str]:
    """Filters the user may use: everything for staff; for archive managers, everything but marks and admin filters."""
    if not can_use_archive_picker(user):
        return []
    if getattr(user, "is_staff", False):
        return list(ALL_FILTERS)
    return [key for key in ALL_FILTERS if key not in MARK_FILTERS and key not in ADMIN_FILTERS]


def tristate_filter(params: Mapping[str, typing.Any], key: str) -> bool | None:
    """Value of a three-way filter: True (only), False (exclude, the default) or None (ignore)."""
    value = params.get(key)
    if value is None:
        return False
    value = str(value).strip().lower()
    if value == TRISTATE_ANY:
        return None
    return value not in ("", "0", "false", "off", "no")


def clean_filter(params: Mapping[str, typing.Any], user: "User | AnonymousUser | None") -> dict[str, str]:
    """Keep only the filters the user may use, with valid values. Empty values are dropped.

    :raises ValueError: for a number or date filter with an invalid value.
    """
    allowed = set(allowed_filter_keys(user))
    cleaned: dict[str, str] = {}
    for key in ALL_FILTERS:
        if key not in allowed or key not in params:
            continue
        value = params[key]
        if isinstance(value, bool):
            value = "1" if value else ""
        if not isinstance(value, (str, int, float)):
            continue
        value = str(value).strip()[:MAX_TEXT_LENGTH]
        if key in TRISTATE_FILTERS and value.lower() == TRISTATE_ANY:
            cleaned[key] = TRISTATE_ANY
            continue
        if not value or (key in FLAG_FILTERS and value.lower() in ("0", "false", "off")):
            continue
        if key in FLAG_FILTERS:
            value = "1"
        elif key in NUMBER_FILTERS:
            try:
                float(value)
            except ValueError:
                raise ValueError(f"Filter '{key}' must be a number, got '{value}'.")
        elif key == "origin":
            if value not in {str(choice) for choice, _ in ArchiveManageEntry.ORIGIN_CHOICES}:
                raise ValueError(f"Filter 'origin' must be one of the mark origins, got '{value}'.")
        elif key == "storage":
            if value not in STORAGE_CHOICES:
                raise ValueError(f"Filter 'storage' must be one of {', '.join(STORAGE_CHOICES)}, got '{value}'.")
        elif key == "archive_group":
            if value != ARCHIVE_GROUP_NONE and not value.isdigit():
                raise ValueError(f"Filter 'archive_group' must be an archive group id or 'none', got '{value}'.")
        elif key in DATE_FILTERS:
            try:
                datetime.date.fromisoformat(value)
            except ValueError:
                raise ValueError(f"Filter '{key}' must be a date (YYYY-MM-DD), got '{value}'.")
        cleaned[key] = value
    sort = str(params.get("sort", "") or "")
    if sort in archive_order_fields:
        cleaned["sort"] = sort
    asc_desc = str(params.get("asc_desc", "") or "")
    if asc_desc in ("asc", "desc"):
        cleaned["asc_desc"] = asc_desc
    return cleaned


def describe_filter(params: Mapping[str, str]) -> str:
    parts = []
    for key, value in params.items():
        if key in SORT_KEYS:
            continue
        parts.append(key if key in FLAG_FILTERS else f"{key}={value}")
    return ", ".join(parts) or "no filters"


def filter_by_marks(archives: QuerySet[Archive], params: Mapping[str, typing.Any]) -> tuple[QuerySet[Archive], bool]:
    mark_filters = False

    if "and-mark-conditions" in params and params["and-mark-conditions"]:

        q_objects = Q()

        if "marked" in params and params["marked"]:
            q_objects.add(Q(manage_entries__mark_check=True, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "mark_reason" in params and params["mark_reason"]:
            q_objects.add(Q(manage_entries__mark_reason__contains=str(params["mark_reason"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "mark_comment" in params and params["mark_comment"]:
            q_objects.add(Q(manage_entries__mark_comment__contains=str(params["mark_comment"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "mark_extra" in params and params["mark_extra"]:
            q_objects.add(Q(manage_entries__mark_extra__contains=str(params["mark_extra"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        if "origin" in params and params["origin"]:
            q_objects.add(Q(manage_entries__origin=str(params["origin"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        priority_to = params.get("priority_to", None)
        if priority_to is not None and priority_to != "":
            q_objects.add(Q(manage_entries__mark_priority__lte=float(priority_to), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        priority_from = params.get("priority_from", None)
        if priority_from is not None and priority_from != "":
            q_objects.add(Q(manage_entries__mark_priority__gte=float(priority_from), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL), Q.AND)
            mark_filters = True
        elif mark_filters:
            # By default, don't filter marks with less than 1 priority (low level priorities)
            archives = archives.annotate(
                num_manage_total=Count("manage_entries", filter=Q(manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
                num_manage_below_1=Count("manage_entries", filter=Q(manage_entries__mark_priority__lt=1, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
            )
            q_objects.add(~Q(num_manage_below_1=F("num_manage_total")), Q.AND)
        archives = archives.filter(q_objects)
    else:
        if "marked" in params and params["marked"]:
            archives = archives.filter(manage_entries__mark_check=True, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "mark_reason" in params and params["mark_reason"]:
            archives = archives.filter(manage_entries__mark_reason__contains=str(params["mark_reason"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "mark_comment" in params and params["mark_comment"]:
            archives = archives.filter(manage_entries__mark_comment__contains=str(params["mark_comment"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "mark_extra" in params and params["mark_extra"]:
            archives = archives.filter(manage_entries__mark_extra__contains=str(params["mark_extra"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        if "origin" in params and params["origin"]:
            archives = archives.filter(manage_entries__origin=str(params["origin"]), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        priority_to = params.get("priority_to", None)
        if priority_to is not None and priority_to != "":
            archives = archives.filter(manage_entries__mark_priority__lte=float(priority_to), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        priority_from = params.get("priority_from", None)
        if priority_from is not None and priority_from != "":
            archives = archives.filter(manage_entries__mark_priority__gte=float(priority_from), manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)
            mark_filters = True
        elif mark_filters:
            # By default, don't filter marks with less than 1 priority (low level priorities)
            archives = archives.annotate(
                num_manage_total=Count("manage_entries", filter=Q(manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
                num_manage_below_1=Count("manage_entries", filter=Q(manage_entries__mark_priority__lt=1, manage_entries__status=ArchiveManageEntry.StatusChoices.NORMAL)),
            ).exclude(num_manage_below_1=F("num_manage_total"))

    archives = archives.distinct()

    return archives, mark_filters


def filter_by_extra_files(archives: QuerySet[Archive], params: Mapping[str, typing.Any]) -> tuple[QuerySet[Archive], bool]:
    extra_file_filters = False

    if "extra_file_name" in params and params["extra_file_name"]:
        archives = archives.filter(archivefileentry__file_name__icontains=str(params["extra_file_name"]))
        extra_file_filters = True
    if "extra_file_type" in params and params["extra_file_type"]:
        archives = archives.filter(archivefileentry__file_type__icontains=str(params["extra_file_type"]))
        extra_file_filters = True
    extra_file_size_to = params.get("extra_file_size_to", None)
    if extra_file_size_to is not None and extra_file_size_to != "":
        archives = archives.filter(archivefileentry__file_size__lte=float(extra_file_size_to))
        extra_file_filters = True
    extra_file_size_from = params.get("extra_file_size_from", None)
    if extra_file_size_from is not None and extra_file_size_from != "":
        archives = archives.filter(archivefileentry__file_size__gte=float(extra_file_size_from))
        extra_file_filters = True

    archives = archives.distinct()

    return archives, extra_file_filters


def filter_recycled_and_file_deleted(
    archives: QuerySet[Archive], params: Mapping[str, typing.Any], can_view_recycled: bool
) -> QuerySet[Archive]:
    """Apply the three-way "recycled" and "file_deleted" filters. Without ``can_view_recycled`` both exclude."""
    for key, field in (("recycled", "binned"), ("file_deleted", "file_deleted")):
        wanted = tristate_filter(params, key) if can_view_recycled else False
        if wanted is not None:
            archives = archives.filter(**{field: wanted})
    return archives


def filter_by_archive_group(archives: QuerySet[Archive], value: typing.Any) -> QuerySet[Archive]:
    """Keep the archives in the archive group with this id, or the ones in no group for "none". Other values (empty,
    not an id) don't filter."""
    value = str(value or "").strip()
    in_group = ArchiveGroupEntry.objects.filter(archive=OuterRef("pk"))
    if value == ARCHIVE_GROUP_NONE:
        return archives.exclude(Exists(in_group))
    if value.isdigit():
        return archives.filter(Exists(in_group.filter(archive_group_id=int(value))))
    return archives


def filter_archives(
    params: Mapping[str, typing.Any],
    can_view_recycled: bool = False,
    flags: dict[str, bool] | None = None,
    favorites_user_id: int | None = None,
) -> QuerySet[Archive]:
    """Archives matching the archive manager filters, ordered by its sort parameters.

    Binned and file deleted archives are excluded unless ``can_view_recycled``. Users who can view them choose with
    "recycled" and "file_deleted": "1" for only those archives, "any" to ignore the filter, absent or "0" to exclude
    them. ``flags`` receives whether mark and extra file filters were used. "only_favorites" keeps the archives that
    ``favorites_user_id`` marked as favorite, and is ignored without it. "archive_group" keeps the archives in that
    group (by id), or the ones in no group with "none". "gallery-has-child" keeps the archives whose gallery has a child
    gallery (a newer version, ``Gallery.parent_gallery``). "storage" keeps the "local" or the "cold"
    (offloaded) archives; it doesn't check who asks, callers drop it for users who can't use the cold storage.
    """
    query: dict[str, typing.Any] = {"sort": "create_date", "asc_desc": "desc"}
    for key, value in params.items():
        if isinstance(value, str):
            query[key] = value
    for key in archive_filter_keys:
        query.setdefault(key, "")

    results: QuerySet[Archive] = filter_archives_simple(query, authenticated=True, show_binned=True)
    results, mark_filters = filter_by_marks(results, params)
    results, extra_file_filters = filter_by_extra_files(results, params)
    if flags is not None:
        flags["mark_filters"] = mark_filters
        flags["extra_file_filters"] = extra_file_filters

    if "diff-filesize" in params:
        results = (
            results.exclude(gallery__filesize__isnull=True)
            .exclude(filesize=0)
            .filter(gallery__filesize__gt=0)
            .exclude(filesize=F("gallery__filesize"))
        )

    if "gallery-other-archives" in params:
        # The gallery has at least one other archive that isn't in the Recycle Bin.
        other_archives = Archive.objects.filter(gallery=OuterRef("gallery"), binned=False).exclude(pk=OuterRef("pk"))
        results = results.filter(gallery__isnull=False).filter(Exists(other_archives))

    if "gallery-has-child" in params:
        # A newer version of the gallery is known (a gallery whose parent is this one): the archive may be replaceable.
        children = Gallery.objects.filter(parent_gallery=OuterRef("gallery")).exclude(pk=OuterRef("gallery"))
        results = results.filter(gallery__isnull=False).filter(Exists(children))

    results = filter_recycled_and_file_deleted(results, params, can_view_recycled)
    results = cold_storage.filter_by_storage(results, params.get("storage"))
    results = filter_by_archive_group(results, params.get("archive_group"))

    if params.get("only_favorites") and favorites_user_id is not None:
        favorites = UserArchivePrefs.objects.filter(user_id=favorites_user_id, favorite_group__gt=0).values("archive")
        results = results.filter(pk__in=favorites)

    if "downloading" in params:
        results = results.filter(crc32="")

    if "no-custom-tags" in params:
        results = results.annotate(
            num_custom_tags=Count("tags", filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))
        ).filter(num_custom_tags=0)

    if "with-custom-tags" in params:
        results = results.annotate(
            num_custom_tags=Count("tags", filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))
        ).filter(num_custom_tags__gt=0)

    if "with-scopeless-tags" in params:
        results = results.annotate(
            num_scopeless_tags=Count(
                "tags", filter=Q(~Q(archivetag__origin=ArchiveTag.ORIGIN_USER) & Q(archivetag__tag__scope=""))
            )
        ).filter(num_scopeless_tags__gt=0)

    if "gallery-in-queue" in params:
        results = results.annotate(num_galleries_in_submit=Count("gallery__gallerysubmitentry")).filter(
            num_galleries_in_submit__gt=0
        )

    return results


def archives_matching(params: Mapping[str, typing.Any]) -> QuerySet[Archive]:
    """A plain (unordered, not annotated) queryset of the archives matching an already cleaned filter.

    Safe to count, aggregate and paginate by primary key, unlike the joined and annotated ``filter_archives``.
    """
    matching = filter_archives(params, can_view_recycled=True).order_by().values("pk")
    return Archive.objects.filter(pk__in=matching)
