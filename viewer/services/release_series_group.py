"""Put a release series' archives in an archive group, in issue order.

The series' archives are the ones the completeness check counts: archives matched by the series' filters whose title
has an issue, and the archives of matched galleries whose title has one (a gallery with an archive counts as archived,
even when the archive's own title doesn't match). Unparsed archives (no issue in the title) are left out.

A new group gets the chosen archives. An existing group is updated in one of three ways (``GroupMode``):

- ``add``: the chosen archives not in the group yet are appended, in issue order. Nothing else changes.
- ``merge``: like ``add``, then the group is renumbered: the series' archives in issue order, followed by its other
  entries in their current order.
- ``replace``: the group becomes the chosen archives, in issue order. Every other entry is removed.
"""

import dataclasses
import datetime
import typing
from collections import defaultdict

from django.db import transaction
from django.db.models import F, Max
from django.urls import reverse

from viewer.models import Archive, ArchiveGroup, ArchiveGroupEntry, ReleaseSeries
from viewer.services import release_series as rs


class GroupMode:
    ADD = "add"
    MERGE = "merge"
    REPLACE = "replace"
    choices = (ADD, MERGE, REPLACE)


@dataclasses.dataclass
class SeriesArchive:
    pk: int
    title: str
    keys: list[rs.Key]
    issue: str  # the issue labels, "2026-01, 2026-02" for a combined issue
    binned: bool
    file_deleted: bool
    public: bool
    via_gallery: bool  # its own title doesn't match the series: found through its gallery
    create_date: datetime.datetime | None

    @property
    def flags(self) -> list[str]:
        flags = []
        if self.binned:
            flags.append("in Recycle Bin")
        if self.file_deleted:
            flags.append("file deleted")
        return flags

    @property
    def suggested(self) -> bool:
        """Chosen by default: not in the Recycle Bin, and its file is there."""
        return not self.binned and not self.file_deleted

    def as_dict(self) -> dict[str, typing.Any]:
        return {
            "id": self.pk,
            "title": self.title,
            "issue": self.issue,
            "flags": self.flags,
            "public": self.public,
            "via_gallery": self.via_gallery,
            "suggested": self.suggested,
            "url": reverse("viewer:archive", args=(self.pk,)),
        }


def series_archives(series: ReleaseSeries) -> list[SeriesArchive]:
    """The archives of a series' issues, in issue order (then oldest first)."""
    patterns = rs.compile_title_patterns(series)
    if not patterns:
        raise rs.ConfigError("Set a title regex to read the issue from the titles.")
    entries, _ = rs.collect_candidates(series)

    archive_keys: dict[int, set[rs.Key]] = defaultdict(set)
    gallery_keys: dict[int, set[rs.Key]] = defaultdict(set)
    for entry in entries:
        if entry.kind == "gallery" and not entry.has_archive:
            continue
        keys = rs.keys_from_title(series.key_type, patterns, entry.title)
        if keys:
            (archive_keys if entry.kind == "archive" else gallery_keys)[entry.pk].update(keys)

    # An archive's own title wins over its gallery's.
    via_gallery: set[int] = set()
    gallery_ids = list(gallery_keys)
    for start in range(0, len(gallery_ids), rs.CHUNK):
        pairs = Archive.objects.filter(gallery_id__in=gallery_ids[start : start + rs.CHUNK]).values_list(
            "id", "gallery_id"
        )
        for pk, gallery_id in pairs:
            if pk not in archive_keys:
                via_gallery.add(pk)
                archive_keys[pk].update(gallery_keys[gallery_id])

    archives = []
    ids = list(archive_keys)
    for start in range(0, len(ids), rs.CHUNK):
        rows = Archive.objects.filter(pk__in=ids[start : start + rs.CHUNK]).values(
            "id", "title", "binned", "file_deleted", "public", "create_date"
        )
        for row in rows:
            keys = sorted(archive_keys[row["id"]])
            archives.append(
                SeriesArchive(
                    pk=row["id"],
                    title=row["title"] or "",
                    keys=keys,
                    issue=", ".join(rs.key_label(series.key_type, key) for key in keys),
                    binned=row["binned"],
                    file_deleted=row["file_deleted"],
                    public=row["public"],
                    via_gallery=row["id"] in via_gallery,
                    create_date=row["create_date"],
                )
            )
    archives.sort(key=lambda a: (a.keys, a.create_date.timestamp() if a.create_date else float("inf"), a.pk))
    return archives


def group_entries(group: ArchiveGroup) -> list[ArchiveGroupEntry]:
    """A group's entries in their order (entries without a position last)."""
    return list(
        ArchiveGroupEntry.objects.filter(archive_group=group)
        .select_related("archive")
        .order_by(F("position").asc(nulls_last=True), "pk")
    )


def suggested_groups(archive_ids: list[int], limit: int = 5) -> list[dict[str, typing.Any]]:
    """The groups that already have some of these archives, the ones with the most first."""
    counts: dict[int, int] = defaultdict(int)
    for start in range(0, len(archive_ids), rs.CHUNK):
        rows = ArchiveGroupEntry.objects.filter(archive_id__in=archive_ids[start : start + rs.CHUNK]).values_list(
            "archive_group_id", flat=True
        )
        for group_id in rows:
            counts[group_id] += 1
    best = sorted(counts, key=lambda pk: -counts[pk])[:limit]
    titles = dict(ArchiveGroup.objects.filter(pk__in=best).values_list("pk", "title"))
    return [{"id": pk, "title": titles[pk], "series_archives": counts[pk]} for pk in best if pk in titles]


@dataclasses.dataclass
class GroupChange:
    group: ArchiveGroup
    created: bool = False
    added: int = 0
    removed: int = 0
    moved: int = 0
    total: int = 0

    def summary(self) -> str:
        parts = [f"{self.added} added"]
        if self.removed:
            parts.append(f"{self.removed} removed")
        if self.moved:
            parts.append(f"{self.moved} moved")
        return f"{'Created' if self.created else 'Updated'} archive group '{self.group.title}': {', '.join(parts)} ({self.total} in the group)."

    def as_dict(self) -> dict[str, typing.Any]:
        return {
            "group": {"id": self.group.pk, "title": self.group.title, "url": self.group.get_absolute_url()},
            "created": self.created,
            "added": self.added,
            "removed": self.removed,
            "moved": self.moved,
            "total": self.total,
            "message": self.summary(),
        }


def create_group(title: str, archives: list[SeriesArchive], public: bool = False) -> GroupChange:
    """A new group with these archives, in this order."""
    title = title.strip()
    if not title:
        raise rs.ConfigError("The new group needs a title.")
    with transaction.atomic():
        position = ArchiveGroup.objects.aggregate(last=Max("position", default=0))["last"] + 1
        group = ArchiveGroup.objects.create(title=title[:500], position=position, public=public)
        ArchiveGroupEntry.objects.bulk_create(
            ArchiveGroupEntry(archive_group=group, archive_id=a.pk, title=a.title[:500], position=i)
            for i, a in enumerate(archives, start=1)
        )
    return GroupChange(group=group, created=True, added=len(archives), total=len(archives))


def update_group(group: ArchiveGroup, series: list[SeriesArchive], chosen: set[int], mode: str) -> GroupChange:
    """Add the chosen series archives to a group (see the module's docstring for the modes).

    :param series: every archive of the series, in issue order (the ones not chosen still sort in ``merge``).
    :param chosen: the ids of the series archives to put in the group.
    """
    if mode not in GroupMode.choices:
        raise rs.ConfigError(f"Unknown mode '{mode}'.")
    change = GroupChange(group=group)
    with transaction.atomic():
        ArchiveGroup.objects.select_for_update().filter(pk=group.pk).first()
        entries = group_entries(group)
        existing = {e.archive_id: e for e in entries}
        by_pk = {a.pk: a for a in series}
        new = [a for a in series if a.pk in chosen and a.pk not in existing]

        if mode == GroupMode.ADD:
            last = max((e.position or 0 for e in entries), default=0)
            ArchiveGroupEntry.objects.bulk_create(
                ArchiveGroupEntry(archive_group=group, archive_id=a.pk, title=a.title[:500], position=last + i)
                for i, a in enumerate(new, start=1)
            )
            change.added = len(new)
            change.total = len(entries) + len(new)
        else:
            if mode == GroupMode.MERGE:
                order = [a.pk for a in series if a.pk in existing or a.pk in chosen]
                order += [e.archive_id for e in entries if e.archive_id not in by_pk]
            else:
                order = [a.pk for a in series if a.pk in chosen]
            keep = set(order)
            # Entries not kept, and an archive's extra entries if it's in the group more than once.
            first_entry = {e.archive_id: e.pk for e in reversed(entries)}
            removed = [e.pk for e in entries if e.archive_id not in keep or first_entry[e.archive_id] != e.pk]
            if removed:
                ArchiveGroupEntry.objects.filter(pk__in=removed).delete()
            ArchiveGroupEntry.objects.bulk_create(
                ArchiveGroupEntry(archive_group=group, archive_id=a.pk, title=a.title[:500]) for a in new
            )
            # Renumber: the positions of the kept entries that changed, and the new ones.
            current = {e.archive_id: e for e in ArchiveGroupEntry.objects.filter(archive_group=group)}
            changed = []
            for position, archive_id in enumerate(order, start=1):
                entry = current[archive_id]
                if entry.position != position:
                    if archive_id in existing:
                        change.moved += 1
                    entry.position = position
                    changed.append(entry)
            ArchiveGroupEntry.objects.bulk_update(changed, ["position"])
            change.added = len(new)
            change.removed = len(removed)
            change.total = len(order)
        if change.added or change.removed or change.moved:
            group.save(update_fields=["last_modified"])
    return change
