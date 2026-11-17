"""Field-level change log for galleries and archives.

Only real changes are stored. A row holds the tracked fields that changed, as
``{field: [old, new]}``, plus for galleries the tags that were added and removed, as
``"scope:name"`` strings frozen at the time of the change. A save that changes nothing
writes nothing.

How changes are recorded:

* ``TrackedChangesMixin`` keeps a snapshot of the tracked fields when an object is loaded
  from the database (``from_db`` / ``refresh_from_db``) and after every save. A ``post_save``
  receiver diffs against it.
* Gallery tag changes come from ``m2m_changed``, which reports the exact tag ids involved.
* Archive custom tags (``ArchiveTag`` rows with ``origin=ORIGIN_USER``) come from
  ``post_save``/``post_delete`` on ``ArchiveTag``. Archive tags copied from the gallery
  aren't tracked: the gallery's change log already has them.
* Every save or tag operation writes its own row, unless it runs inside
  ``batched_changes()``. Inside the block, everything that happens to one object is merged
  into a single row, written when the block exits successfully. The provider update paths
  use it, so one refresh produces at most one row.
* Queryset ``.update()``, ``bulk_update()`` and raw SQL bypass tracking, as they bypass any
  signal-based log.
"""

import contextvars
import datetime
import typing
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from django.apps import apps
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from viewer.models.tag import Tag
from viewer.utils.tags import sort_tags

# Fields whose changes are recorded. Adding a field here starts recording it from now on;
# the one-off conversion of the old django-simple-history data (migration 0226) has its own
# frozen copy of these lists.
GALLERY_TRACKED_FIELDS: tuple[str, ...] = (
    "title",
    "title_jpn",
    "category",
    "uploader",
    "comment",
    "expunged",
    "disowned",
    "rating",
    "fjord",
    "public",
)

# Subset shown to anonymous users (tags are always public).
GALLERY_PUBLIC_FIELDS: tuple[str, ...] = (
    "title",
    "title_jpn",
    "category",
    "uploader",
    "comment",
    "expunged",
    "disowned",
    "rating",
)

ARCHIVE_TRACKED_FIELDS: tuple[str, ...] = (
    "title",
    "title_jpn",
    "source_type",
    "reason",
    "public_date",
    "public",
    "gallery",
    "details",
)

# ArchiveTag.ORIGIN_USER (not imported: viewer.models.archive imports this module)
_ARCHIVE_TAG_ORIGIN_USER = 2


def _json_value(value: typing.Any) -> typing.Any:
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return value


def _tag_label(scope: str, name: str) -> str:
    return f"{scope}:{name}" if scope else name


@dataclass(frozen=True)
class TagLabel:
    """A tag as recorded in the change log; quacks enough like ``Tag`` for ``sort_tags``."""

    scope: str
    name: str

    @classmethod
    def parse(cls, label: str) -> "TagLabel":
        scope, sep, name = label.partition(":")
        return cls(scope, name) if sep else cls("", label)

    def __str__(self) -> str:
        return _tag_label(self.scope, self.name)


@dataclass
class FieldChange:
    field: str
    old: typing.Any
    new: typing.Any


class ChangeLogEntry(models.Model):
    changed_at = models.DateTimeField(default=timezone.now)
    # {field_name: [old_value, new_value]}, JSON-encoded values (datetimes as ISO 8601).
    changes = models.JSONField(default=dict, blank=True)

    tracked_fields: typing.ClassVar[tuple[str, ...]] = ()
    target_model_name: typing.ClassVar[str] = ""

    class Meta:
        abstract = True
        ordering = ("-changed_at", "-id")

    def field_changes(self, fields: Iterable[str] | None = None) -> list[FieldChange]:
        """Changed fields in tracked-field order, with values decoded for display."""
        wanted = self.tracked_fields if fields is None else tuple(fields)
        target = apps.get_model("viewer", self.target_model_name)
        result = []
        for name in wanted:
            if name not in self.changes:
                continue
            old, new = self.changes[name]
            field = target._meta.get_field(name)
            result.append(FieldChange(name, self._decode(field, old), self._decode(field, new)))
        # Fields that are no longer tracked (or renamed) still show up, raw.
        if fields is None:
            for name, (old, new) in self.changes.items():
                if name not in wanted:
                    result.append(FieldChange(name, old, new))
        return result

    @staticmethod
    def _decode(field: models.Field, value: typing.Any) -> typing.Any:
        if value is None:
            return None
        if isinstance(field, models.DateTimeField) and isinstance(value, str):
            return parse_datetime(value) or value
        if field.choices:
            return dict(field.flatchoices).get(value, value)
        return value


class GalleryChange(ChangeLogEntry):
    gallery = models.ForeignKey(
        "viewer.Gallery", on_delete=models.CASCADE, related_name="change_log", db_index=False
    )
    tags_added = models.JSONField(default=list, blank=True)
    tags_removed = models.JSONField(default=list, blank=True)

    tracked_fields = GALLERY_TRACKED_FIELDS
    target_model_name = "Gallery"

    class Meta(ChangeLogEntry.Meta):
        indexes = [
            models.Index(fields=["gallery", "-changed_at"], name="gallerychange_gallery_date"),
            models.Index(fields=["changed_at"], name="gallerychange_date"),
        ]

    def __str__(self) -> str:
        return f"GalleryChange({self.gallery_id} @ {self.changed_at:%Y-%m-%d %H:%M})"

    @staticmethod
    def public_filter() -> Q:
        """Rows with something an anonymous user is allowed to see."""
        return Q(changes__has_any_keys=list(GALLERY_PUBLIC_FIELDS)) | ~Q(tags_added=[]) | ~Q(tags_removed=[])

    def tags_added_lists(self) -> list[tuple[str, list["Tag"]]]:
        return sort_tags([TagLabel.parse(x) for x in self.tags_added])

    def tags_removed_lists(self) -> list[tuple[str, list["Tag"]]]:
        return sort_tags([TagLabel.parse(x) for x in self.tags_removed])


class ArchiveChange(ChangeLogEntry):
    archive = models.ForeignKey(
        "viewer.Archive", on_delete=models.CASCADE, related_name="change_log", db_index=False
    )
    custom_tags_added = models.JSONField(default=list, blank=True)
    custom_tags_removed = models.JSONField(default=list, blank=True)

    tracked_fields = ARCHIVE_TRACKED_FIELDS
    target_model_name = "Archive"

    class Meta(ChangeLogEntry.Meta):
        indexes = [
            models.Index(fields=["archive", "-changed_at"], name="archivechange_archive_date"),
            models.Index(fields=["changed_at"], name="archivechange_date"),
        ]

    def __str__(self) -> str:
        return f"ArchiveChange({self.archive_id} @ {self.changed_at:%Y-%m-%d %H:%M})"

    def custom_tags_added_lists(self) -> list[tuple[str, list["Tag"]]]:
        return sort_tags([TagLabel.parse(x) for x in self.custom_tags_added])

    def custom_tags_removed_lists(self) -> list[tuple[str, list["Tag"]]]:
        return sort_tags([TagLabel.parse(x) for x in self.custom_tags_removed])


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class _Pending:
    """Changes accumulated for one object, merged until written."""

    __slots__ = ("changed_at", "changes", "tags_added", "tags_removed", "created", "deleted")

    def __init__(self) -> None:
        self.changed_at = timezone.now()
        self.changes: dict[str, list[typing.Any]] = {}
        self.tags_added: set[int] = set()
        self.tags_removed: set[int] = set()
        self.created = False
        self.deleted = False

    def add_field(self, name: str, old: typing.Any, new: typing.Any) -> None:
        if name in self.changes:
            old = self.changes[name][0]
        if _same(old, new):
            self.changes.pop(name, None)
        else:
            self.changes[name] = [old, new]

    def add_tags(self, tag_ids: Iterable[int]) -> None:
        for tag_id in tag_ids:
            if tag_id in self.tags_removed:
                self.tags_removed.discard(tag_id)
            else:
                self.tags_added.add(tag_id)

    def remove_tags(self, tag_ids: Iterable[int]) -> None:
        for tag_id in tag_ids:
            if tag_id in self.tags_added:
                self.tags_added.discard(tag_id)
            else:
                self.tags_removed.add(tag_id)

    def is_empty(self) -> bool:
        return not (self.changes or self.tags_added or self.tags_removed)


_PendingKey = tuple[type[models.Model], typing.Any]
_batch: contextvars.ContextVar[dict[_PendingKey, _Pending] | None] = contextvars.ContextVar(
    "viewer_change_log_batch", default=None
)


@contextmanager
def batched_changes() -> Iterator[None]:
    """Merge every change made to an object inside the block into one change-log row.

    Rows are written when the block exits without an exception, so put it inside the
    ``transaction.atomic()`` block of the operation (``with transaction.atomic(),
    batched_changes():``). Objects created inside the block get no row. Nested blocks
    join the outermost one.
    """
    if _batch.get() is not None:
        yield
        return
    pending: dict[_PendingKey, _Pending] = {}
    token = _batch.set(pending)
    try:
        yield
    finally:
        _batch.reset(token)
    _write(pending)


def _record(model: type[models.Model], pk: typing.Any, apply: Callable[[_Pending], None]) -> None:
    batch = _batch.get()
    if batch is None:
        entry = _Pending()
        apply(entry)
        _write({(model, pk): entry})
        return
    if (model, pk) not in batch:
        batch[(model, pk)] = _Pending()
    apply(batch[(model, pk)])


def _write(pending: dict[_PendingKey, _Pending]) -> None:
    live = {key: p for key, p in pending.items() if not (p.created or p.deleted or p.is_empty())}
    if not live:
        return
    tag_ids = set().union(*(p.tags_added | p.tags_removed for p in live.values()))
    labels = (
        {pk: _tag_label(scope, name) for pk, scope, name in Tag.objects.filter(pk__in=tag_ids).values_list("pk", "scope", "name")}
        if tag_ids
        else {}
    )

    rows: dict[type[ChangeLogEntry], list[ChangeLogEntry]] = defaultdict(list)
    for (model, pk), entry in live.items():
        tracked = typing.cast("type[TrackedChangesMixin]", model)
        change_model = tracked.change_log_model()
        values: dict[str, typing.Any] = {
            tracked.change_log_fk: pk,
            "changed_at": entry.changed_at,
            "changes": {name: [_json_value(old), _json_value(new)] for name, (old, new) in entry.changes.items()},
        }
        if entry.tags_added or entry.tags_removed:
            added_field, removed_field = tracked.change_log_tag_fields
            values[added_field] = sorted(labels[t] for t in entry.tags_added if t in labels)
            values[removed_field] = sorted(labels[t] for t in entry.tags_removed if t in labels)
        rows[change_model].append(change_model(**values))

    for change_model, objs in rows.items():
        change_model._default_manager.bulk_create(objs)


def _same(old: typing.Any, new: typing.Any) -> bool:
    try:
        return bool(old == new)
    except TypeError:  # e.g. naive vs aware datetime
        return False


class TrackedChangesMixin:
    """Mixin for models whose tracked fields are diffed on save (see module docstring)."""

    tracked_fields: typing.ClassVar[tuple[str, ...]] = ()
    change_log_model_name: typing.ClassVar[str] = ""
    change_log_fk: typing.ClassVar[str] = ""
    # Change-log fields holding the tag labels added / removed
    change_log_tag_fields: typing.ClassVar[tuple[str, str]] = ("tags_added", "tags_removed")

    # Provided by models.Model
    _meta: typing.Any
    pk: typing.Any
    __dict__: dict[str, typing.Any]

    @classmethod
    def change_log_model(cls) -> type[ChangeLogEntry]:
        return typing.cast("type[ChangeLogEntry]", apps.get_model("viewer", cls.change_log_model_name))

    @classmethod
    def from_db(
        cls,
        db: str | None,
        field_names: Collection[str],
        values: Collection[typing.Any],
        *,
        fetch_mode: typing.Any = None,
    ) -> typing.Self:
        instance = super().from_db(db, field_names, values, fetch_mode=fetch_mode)  # type: ignore[misc]
        instance._snapshot_tracked_fields()
        return instance

    def refresh_from_db(self, using: typing.Any = None, fields: typing.Any = None, from_queryset: typing.Any = None) -> None:
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)  # type: ignore[misc]
        self._snapshot_tracked_fields(fields)

    def _tracked_value(self, name: str) -> typing.Any:
        field = self._meta.get_field(name)
        value = self.__dict__[field.attname]
        try:
            # What actually gets sent to the database, so "12" vs 12 or naive datetimes
            # don't show up as changes.
            return field.get_prep_value(value)
        except (TypeError, ValueError):
            return value

    def _snapshot_tracked_fields(self, only: Iterable[str] | None = None) -> None:
        snapshot: dict[str, typing.Any] = self.__dict__.setdefault("_tracked_snapshot", {})
        only_set = set(only) if only is not None else None
        for name in self.tracked_fields:
            attname = self._meta.get_field(name).attname
            if only_set is not None and name not in only_set and attname not in only_set:
                continue
            if attname in self.__dict__:  # skip deferred fields
                snapshot[name] = self._tracked_value(name)

    def _record_save(self, created: bool, update_fields: Iterable[str] | None) -> None:
        model = typing.cast("type[models.Model]", self._meta.concrete_model)
        if created:
            if _batch.get() is not None:
                _record(model, self.pk, lambda p: setattr(p, "created", True))
            self._snapshot_tracked_fields()
            return

        snapshot = self.__dict__.get("_tracked_snapshot")
        if snapshot is None:
            # Instance wasn't loaded from the database, so there's nothing to diff against.
            self._snapshot_tracked_fields()
            return

        saved = set(update_fields) if update_fields is not None else None
        changes = []
        for name in self.tracked_fields:
            attname = self._meta.get_field(name).attname
            if saved is not None and name not in saved and attname not in saved:
                continue
            if name not in snapshot or attname not in self.__dict__:
                continue
            new = self._tracked_value(name)
            old = snapshot[name]
            snapshot[name] = new
            if not _same(old, new):
                changes.append((name, old, new))

        if changes:

            def apply(entry: _Pending) -> None:
                for change in changes:
                    entry.add_field(*change)

            _record(model, self.pk, apply)


def record_save(sender: type[models.Model], instance: models.Model, created: bool, raw: bool = False, update_fields: Iterable[str] | None = None, **kwargs: typing.Any) -> None:
    """``post_save`` receiver for tracked models."""
    if raw or not isinstance(instance, TrackedChangesMixin):
        return
    instance._record_save(created, update_fields)


def record_delete(sender: type[models.Model], instance: models.Model, **kwargs: typing.Any) -> None:
    """``post_delete`` receiver: drop batched changes of objects deleted in the same block."""
    if _batch.get() is not None and isinstance(instance, TrackedChangesMixin):
        model = typing.cast("type[models.Model]", instance._meta.concrete_model)
        _record(model, instance.pk, lambda p: setattr(p, "deleted", True))


def record_gallery_tags(
    sender: type[models.Model],
    instance: models.Model,
    action: str,
    reverse: bool,
    pk_set: set[typing.Any] | None,
    **kwargs: typing.Any,
) -> None:
    """``m2m_changed`` receiver for ``Gallery.tags.through``."""
    if action not in ("post_add", "pre_remove", "pre_clear"):
        return
    gallery_model = apps.get_model("viewer", "Gallery")
    m2m_field = gallery_model._meta.get_field("tags")
    gallery_col, tag_col = m2m_field.m2m_column_name(), m2m_field.m2m_reverse_name()
    through = sender._default_manager

    # Removals: only the links that actually exist (remove() accepts non-members).
    if not reverse:
        if action == "post_add":
            tag_ids = list(pk_set or ())
        elif action == "pre_remove":
            tag_ids = list(through.filter(**{gallery_col: instance.pk, f"{tag_col}__in": pk_set or ()}).values_list(tag_col, flat=True))
        else:
            tag_ids = list(through.filter(**{gallery_col: instance.pk}).values_list(tag_col, flat=True))
        if not tag_ids:
            return
        if action == "post_add":
            _record(gallery_model, instance.pk, lambda p: p.add_tags(tag_ids))
        else:
            _record(gallery_model, instance.pk, lambda p: p.remove_tags(tag_ids))
        return

    # Reverse side: instance is a Tag, pk_set holds gallery ids.
    if action == "post_add":
        gallery_ids = list(pk_set or ())
    elif action == "pre_remove":
        gallery_ids = list(through.filter(**{tag_col: instance.pk, f"{gallery_col}__in": pk_set or ()}).values_list(gallery_col, flat=True))
    else:
        gallery_ids = list(through.filter(**{tag_col: instance.pk}).values_list(gallery_col, flat=True))
    tag_id = instance.pk
    for gallery_id in gallery_ids:
        if action == "post_add":
            _record(gallery_model, gallery_id, lambda p: p.add_tags([tag_id]))
        else:
            _record(gallery_model, gallery_id, lambda p: p.remove_tags([tag_id]))


def record_archive_custom_tag_save(
    sender: type[models.Model], instance: models.Model, created: bool, raw: bool = False, **kwargs: typing.Any
) -> None:
    """``post_save`` receiver for ``ArchiveTag``: a custom tag was added."""
    if raw or not created or getattr(instance, "origin", None) != _ARCHIVE_TAG_ORIGIN_USER:
        return
    link = typing.cast(typing.Any, instance)
    tag_id = link.tag_id
    _record(apps.get_model("viewer", "Archive"), link.archive_id, lambda p: p.add_tags([tag_id]))


def record_archive_custom_tag_delete(sender: type[models.Model], instance: models.Model, **kwargs: typing.Any) -> None:
    """``post_delete`` receiver for ``ArchiveTag``: a custom tag was removed.

    Only deletions started on ``ArchiveTag`` itself count (``instance.delete()``, a queryset
    delete, ``archive.tags.remove()``/``set()``). Rows removed because their archive or tag is
    being deleted are skipped.
    """
    if getattr(instance, "origin", None) != _ARCHIVE_TAG_ORIGIN_USER:
        return
    origin = kwargs.get("origin")
    origin_model = origin.model if isinstance(origin, models.QuerySet) else type(origin)
    if origin_model is not sender:
        return
    link = typing.cast(typing.Any, instance)
    tag_id = link.tag_id
    _record(apps.get_model("viewer", "Archive"), link.archive_id, lambda p: p.remove_tags([tag_id]))

