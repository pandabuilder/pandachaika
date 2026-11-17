# Converts django-simple-history snapshots (viewer_historicalgallery[_tags],
# viewer_historicalarchive) into change-log rows (viewer_gallerychange, viewer_archivechange).
#
# For every object still present, its snapshots are walked in order and only real differences
# are kept, as {field: [old, new]} plus tag additions/removals ("scope:name" strings):
#
# * Bursts are merged: snapshots of one object less than BURST apart came from one operation
#   (e.g. save() + tags.set() wrote up to three snapshots per provider refresh), so only the
#   last snapshot of each burst is compared with the previous burst.
# * Identical consecutive snapshots (no-op refreshes) produce no row.
# * The first snapshot is the baseline and produces no row; history of deleted objects is
#   dropped, as is history of tags that no longer exist.
#
# The history tables can be tens of GB, written over years, so one object's snapshots are
# scattered over the whole table. To avoid random I/O, every large table is read in physical
# order:
#
# 1. One sequential pass over viewer_historicalgallery keeps the last snapshot of each burst
#    (narrow columns only, provider_metadata isn't read) in a temp table.
# 2. Tag snapshots are aggregated per snapshot in history_id order, which is the order they
#    were written in, in history_id chunks.
# 3. One pass over the temp tables, sorted per gallery, computes the diffs.
#
# Temp tables need disk space roughly the size of the tracked columns plus the tag labels.
# The field lists are frozen copies of viewer.models.change_log at the time of writing.

import sys
import time

from django.db import migrations

BURST = "5 seconds"
TAG_CHUNK = 1_000_000  # history ids per tag aggregation statement
WORK_MEM = "128MB"

GALLERY_FIELDS = (
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

# field name -> column
ARCHIVE_FIELDS = {
    "title": "title",
    "title_jpn": "title_jpn",
    "source_type": "source_type",
    "reason": "reason",
    "public_date": "public_date",
    "public": "public",
    "gallery": "gallery_id",
    "details": "details",
}


def _changes_expr(fields: dict[str, str]) -> str:
    pairs = ",\n".join(
        f"'{name}', CASE WHEN k.p_{col} IS DISTINCT FROM k.{col} THEN jsonb_build_array(k.p_{col}, k.{col}) END"
        for name, col in fields.items()
    )
    return f"jsonb_strip_nulls(jsonb_build_object({pairs}))"


def _lags(cols: list[str], alias: str) -> str:
    return ",\n".join(f"LAG({alias}.{col}) OVER w AS p_{col}" for col in cols)


def _kept_snapshots_sql(source: str, live: str, cols: list[str]) -> str:
    """Last snapshot of every burst, for objects that still exist."""
    col_list = ", ".join(f"h.{c}" for c in cols)
    return f"""
SELECT s.history_id, s.id, s.history_date, {", ".join(f"s.{c}" for c in cols)}
FROM (
    SELECT h.history_id, h.id, h.history_date, {col_list},
           LEAD(h.history_date) OVER (PARTITION BY h.id ORDER BY h.history_date, h.history_id) AS next_date
    FROM {source} h
    WHERE EXISTS (SELECT 1 FROM {live} o WHERE o.id = h.id)
) s
WHERE s.next_date IS NULL OR s.next_date > s.history_date + interval '{BURST}'
"""


GALLERY_COLS = list(GALLERY_FIELDS)

GALLERY_KEPT_SQL = (
    "CREATE TEMP TABLE conv_gallery_kept ON COMMIT DROP AS"
    + _kept_snapshots_sql("viewer_historicalgallery", "viewer_gallery", GALLERY_COLS)
)

GALLERY_TAGS_SQL = """
INSERT INTO conv_gallery_tags (history_id, tags)
SELECT ht.history_id, array_agg(CASE WHEN t.scope <> '' THEN t.scope || ':' || t.name ELSE t.name END)
FROM viewer_historicalgallery_tags ht
JOIN viewer_tag t ON t.id = ht.tag_id
WHERE ht.history_id >= %s AND ht.history_id < %s
  AND EXISTS (SELECT 1 FROM conv_gallery_kept k WHERE k.history_id = ht.history_id)
GROUP BY ht.history_id
"""

GALLERY_INSERT_SQL = f"""
INSERT INTO viewer_gallerychange (gallery_id, changed_at, changes, tags_added, tags_removed)
SELECT d.id, d.history_date, d.changes, to_jsonb(d.tags_added), to_jsonb(d.tags_removed)
FROM (
    SELECT k.id, k.history_date,
           {_changes_expr({c: c for c in GALLERY_COLS})} AS changes,
           ARRAY(SELECT unnest(k.tags) EXCEPT SELECT unnest(k.p_tags) ORDER BY 1) AS tags_added,
           ARRAY(SELECT unnest(k.p_tags) EXCEPT SELECT unnest(k.tags) ORDER BY 1) AS tags_removed
    FROM (
        SELECT g.*, COALESCE(t.tags, '{{}}'::text[]) AS tags,
               LAG(g.history_id) OVER w AS p_history_id,
               {_lags(GALLERY_COLS, "g")},
               LAG(COALESCE(t.tags, '{{}}'::text[])) OVER w AS p_tags
        FROM conv_gallery_kept g
        LEFT JOIN conv_gallery_tags t ON t.history_id = g.history_id
        WINDOW w AS (PARTITION BY g.id ORDER BY g.history_date, g.history_id)
    ) k
    WHERE k.p_history_id IS NOT NULL
) d
WHERE d.changes <> '{{}}'::jsonb OR cardinality(d.tags_added) > 0 OR cardinality(d.tags_removed) > 0
"""

ARCHIVE_COLS = list(ARCHIVE_FIELDS.values())

ARCHIVE_INSERT_SQL = f"""
INSERT INTO viewer_archivechange (archive_id, changed_at, changes)
SELECT d.id, d.history_date, d.changes
FROM (
    SELECT k.id, k.history_date, {_changes_expr(ARCHIVE_FIELDS)} AS changes
    FROM (
        SELECT a.*,
               LAG(a.history_id) OVER w AS p_history_id,
               {_lags(ARCHIVE_COLS, "a")}
        FROM ({_kept_snapshots_sql("viewer_historicalarchive", "viewer_archive", ARCHIVE_COLS)}) a
        WINDOW w AS (PARTITION BY a.id ORDER BY a.history_date, a.history_id)
    ) k
    WHERE k.p_history_id IS NOT NULL
) d
WHERE d.changes <> '{{}}'::jsonb
"""


class _Progress:
    def __init__(self, label: str) -> None:
        self.label = label
        self.started = self.step_started = time.monotonic()

    def log(self, message: str) -> None:
        now = time.monotonic()
        sys.stdout.write(f"\n    {self.label}: {message} ({now - self.step_started:.0f}s, {now - self.started:.0f}s total)")
        sys.stdout.flush()
        self.step_started = now


def _convert_galleries(cursor) -> None:
    cursor.execute("SELECT count(*) > 0 FROM (SELECT 1 FROM viewer_historicalgallery LIMIT 1) x")
    if not cursor.fetchone()[0]:
        return
    progress = _Progress("gallery")
    sys.stdout.write("\n  Converting gallery history...")
    cursor.execute("ANALYZE viewer_historicalgallery")
    cursor.execute("ANALYZE viewer_historicalgallery_tags")

    cursor.execute(GALLERY_KEPT_SQL)
    progress.log(f"{cursor.rowcount} snapshots kept after merging bursts")
    cursor.execute("CREATE INDEX ON conv_gallery_kept (history_id)")
    cursor.execute("ANALYZE conv_gallery_kept")

    cursor.execute("CREATE TEMP TABLE conv_gallery_tags (history_id integer NOT NULL, tags text[] NOT NULL) ON COMMIT DROP")
    cursor.execute("SELECT min(history_id), max(history_id) FROM conv_gallery_kept")
    lo, hi = cursor.fetchone()
    if lo is not None:
        start = lo
        while start <= hi:
            cursor.execute(GALLERY_TAGS_SQL, [start, start + TAG_CHUNK])
            start += TAG_CHUNK
            progress.log(f"tags aggregated up to history id {min(start, hi + 1) - 1} of {hi}")
    cursor.execute("ANALYZE conv_gallery_tags")

    cursor.execute(GALLERY_INSERT_SQL)
    progress.log(f"{cursor.rowcount} changes written")


def _convert_archives(cursor) -> None:
    progress = _Progress("archive")
    cursor.execute(ARCHIVE_INSERT_SQL)
    if cursor.rowcount:
        progress.log(f"{cursor.rowcount} changes written")


def convert(apps, schema_editor):
    connection = schema_editor.connection
    tables = set(connection.introspection.table_names())
    with connection.cursor() as cursor:
        # Bigger sort/hash memory for this transaction only, so the large sorts spill less.
        cursor.execute(f"SET LOCAL work_mem = '{WORK_MEM}'")
        if {"viewer_historicalgallery", "viewer_historicalgallery_tags"} <= tables:
            _convert_galleries(cursor)
        if "viewer_historicalarchive" in tables:
            _convert_archives(cursor)


class Migration(migrations.Migration):

    dependencies = [
        ("viewer", "0225_change_log"),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop, elidable=True),
    ]
