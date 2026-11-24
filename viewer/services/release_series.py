"""Release completeness: find the issues of a periodical (magazine, numbered anthology...) missing from the collection.

A ``ReleaseSeries`` says which galleries and archives belong to a series (title texts, tags, languages), how to read
the issue from a title (regexes with named groups) and when issues come out (schedule periods). ``check_series``
compares what's in the database with what the schedule expects. A gallery or an archive is enough for an issue to be
present; issues found only as galleries without an archive are flagged (``Issue.gallery_only``).

Issues are keys of two ints, so they sort and compare the same way for the three kinds of series:

- year and month: ``(2026, 12)``, shown as ``2026-12``
- year and issue number: ``(2026, 42)``, shown as ``2026 No.42``
- issue number only: ``(0, 141)``, shown as ``#141``
"""

import dataclasses
import datetime
import logging
import re
import statistics
import typing
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable

from django.conf import settings
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from viewer.models import Archive, ArchiveTag, Gallery, ReleaseSeries, Tag

if typing.TYPE_CHECKING:
    from core.base.parsers import BaseParser

logger = logging.getLogger(__name__)

Key = tuple[int, int]

KT = ReleaseSeries.KeyType

# Year and number series also take title regexes without a year group: their issues are numbered without a year
# (#1, #2…, year 0 in the keys), as many series were before they started counting the issues per year.
REQUIRED_GROUPS: dict[str, tuple[str, ...]] = {
    KT.YEAR_MONTH: ("year", "month"),
    KT.YEAR_NUMBER: ("number",),
    KT.NUMBER: ("number",),
}

# Combined issues (2026-01・02, No.10-11) can't span more than this many issues, to ignore things like "2010-2015".
MAX_COMBINED = 4
# Candidates listed as unparsed (matched the filters but no title regex) in a result.
MAX_UNPARSED = 300
# Present issues considered when measuring the lead days.
LEAD_SAMPLE = 36
# Gaps whose estimated release dates are this close belong to the same gap window on the overview.
WINDOW_GAP_DAYS = 21
# An issue released (estimated from the lead days) less than this many days ago is due, not missing yet.
GRACE_DAYS = 10
CHUNK = 5000

MONTH_RULES: dict[str, frozenset[int]] = {
    "monthly": frozenset(range(1, 13)),
    "all": frozenset(range(1, 13)),
    "even": frozenset(range(2, 13, 2)),
    "odd": frozenset(range(1, 13, 2)),
    "quarterly": frozenset((3, 6, 9, 12)),
}
STOP_RULES = ("none", "stop", "end", "ended")
AUTO_RULES = ("auto", "all")


class ConfigError(ValueError):
    pass


# --- Keys ---


def key_label(key_type: str, key: Key) -> str:
    if key_type == KT.YEAR_MONTH:
        return f"{key[0]}-{key[1]:02d}"
    if key_type == KT.YEAR_NUMBER and key[0]:
        return f"{key[0]} No.{key[1]}"
    return f"#{key[1]}"


_YM_KEY = re.compile(r"^\s*(\d{4})(?:\s*[-./]\s*(\d{1,2}))?\s*$")
_YN_KEY = re.compile(r"^\s*(\d{4})(?:\s*(?:No\.?|#|-)\s*(\d+))?\s*$", re.IGNORECASE)
_N_KEY = re.compile(r"^\s*(?:#|No\.?|Vol\.?)?\s*(\d+)\s*$", re.IGNORECASE)


def parse_key(key_type: str, text: str) -> Key:
    """Read a key written by hand (``2024-05``, ``2024 No.9``, ``#109``). A year alone is its first issue.

    Year and number series also take ``#109`` (or ``Vol.109``): an issue numbered without a year.
    """
    if key_type == KT.YEAR_MONTH:
        m = _YM_KEY.match(text)
        if m:
            month = int(m[2]) if m[2] else 1
            if 1 <= month <= 12:
                return int(m[1]), month
    elif key_type == KT.YEAR_NUMBER:
        m = _YN_KEY.match(text)
        if m:
            return int(m[1]), int(m[2]) if m[2] else 1
        m = _N_KEY.match(text)
        if m:
            return 0, int(m[1])
    else:
        m = _N_KEY.match(text)
        if m:
            return 0, int(m[1])
    raise ConfigError(f"'{text.strip()}' is not a valid issue ({_key_example(key_type)}).")


def _key_example(key_type: str) -> str:
    examples: dict[str, str] = {KT.YEAR_MONTH: "e.g. 2024-05", KT.YEAR_NUMBER: "e.g. 2024 No.9, 2024 or #109"}
    return examples.get(key_type, "e.g. #109")


# --- Title parsing ---


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def compile_title_patterns(series: ReleaseSeries) -> list[re.Pattern[str]]:
    patterns = []
    required = REQUIRED_GROUPS[series.key_type]
    for line in _lines(series.title_regex):
        try:
            pattern = re.compile(line, re.IGNORECASE)
        except re.error as e:
            raise ConfigError(f"Invalid title regex '{line}': {e}")
        missing = [g for g in required if g not in pattern.groupindex]
        if missing:
            raise ConfigError(f"Title regex '{line}' needs the named group(s): {', '.join(missing)}.")
        patterns.append(pattern)
    return patterns


def default_title_regex(name: str, key_type: str) -> str:
    """A title regex for a series named ``name``: the name at the start (after optional [..] or (..) prefixes)."""
    words = [re.escape(w) for w in name.split()]
    base = r"^\s*(?:[\[(][^\])]*[\])]\s*)*" + r"\s+".join(words)
    if key_type == KT.YEAR_MONTH:
        return base + r"\s+(?P<year>\d{4})-(?P<month>\d{1,2})(?:\s*[-・/]\s*(?P<month_end>\d{1,2}))?(?!\d)"
    if key_type == KT.YEAR_NUMBER:
        return base + r"\s+(?P<year>\d{4})\s*No\.?\s*(?P<number>\d+)(?:\s*[-・/]\s*(?P<number_end>\d+))?(?!\d)"
    return base + r"\s*(?:[#＃]|No\.?|Vol\.?)\s*(?P<number>\d+)(?:\s*[-・/]\s*(?P<number_end>\d+))?(?!\d)"


def default_backlog_query(key_type: str) -> str:
    return 'title:"{title}" AND NOT tags.full:"language:translated"'


def default_backlog_page_query(key_type: str) -> str:
    return '"{title}" -language:translated$'


def _group_int(m: re.Match[str], name: str) -> int | None:
    if name not in m.re.groupindex:
        return None
    value = m.group(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def keys_from_title(key_type: str, patterns: list[re.Pattern[str]], title: str) -> list[Key]:
    """The issues a title is, with the first pattern that matches. Several for a combined issue."""
    for pattern in patterns:
        m = pattern.search(title or "")
        if not m:
            continue
        if key_type == KT.YEAR_MONTH:
            year, month, month_end = _group_int(m, "year"), _group_int(m, "month"), _group_int(m, "month_end")
            if year is None or month is None or not 1 <= month <= 12:
                continue
            keys = [(year, month)]
            if month_end is not None and 1 <= month_end <= 12 and month_end != month:
                span = (month_end - month) % 12
                if span < MAX_COMBINED:
                    keys = [_add_months((year, month), i) for i in range(span + 1)]
            return keys
        number, number_end = _group_int(m, "number"), _group_int(m, "number_end")
        if number is None:
            continue
        # Without a year group, a year and number series' issue is numbered without a year.
        year = _group_int(m, "year") if key_type == KT.YEAR_NUMBER and "year" in m.re.groupindex else 0
        if year is None:
            continue
        keys = [(year, number)]
        if number_end is not None and number < number_end < number + MAX_COMBINED:
            keys = [(year, n) for n in range(number, number_end + 1)]
        return keys
    return []


def _add_months(key: Key, months: int) -> Key:
    index = key[0] * 12 + key[1] - 1 + months
    return index // 12, index % 12 + 1


# --- Schedule ---


@dataclasses.dataclass(frozen=True)
class Period:
    start: Key
    months: frozenset[int] | None = None  # year and month series
    per_year: int | None = None  # year and number series: None = as many as found
    stopped: bool = False

    def describe(self, key_type: str) -> str:
        if self.stopped:
            return "no issues"
        if key_type == KT.YEAR_MONTH and self.months is not None:
            for name, months in MONTH_RULES.items():
                if months == self.months:
                    return name
            return "months " + ",".join(str(m) for m in sorted(self.months))
        if key_type == KT.YEAR_NUMBER and self.start[0]:
            return f"{self.per_year} per year" if self.per_year else "as many as found per year"
        return "every number"


def parse_schedule(key_type: str, text: str) -> list[Period]:
    """Schedule lines: ``<first issue> <rule>``, the rule applying until the next line's first issue.

    Rules: year and month series: ``monthly``, ``even``, ``odd``, ``quarterly`` or the months (``2,5,8,11``); year
    and number series: the issues per year (``48``) or ``auto`` (the highest number found that year), and ``all`` for
    issues numbered without a year (``#1 all``), which come before the yearly ones; number series: ``all``. Any series:
    ``none`` (no issues from then on, the series ended or paused).
    """
    periods: list[Period] = []
    for line in _lines(text):
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            raise ConfigError(f"Schedule line '{line}' needs a first issue and a rule, e.g. '2024-02 even'.")
        key = parse_key(key_type, parts[0])
        rule = parts[1].lower()
        if rule in STOP_RULES:
            periods.append(Period(start=key, stopped=True))
        elif key_type == KT.YEAR_MONTH:
            if rule in MONTH_RULES:
                months = MONTH_RULES[rule]
            else:
                try:
                    months = frozenset(int(x) for x in rule.split(",") if x)
                except ValueError:
                    raise ConfigError(f"Unknown schedule rule '{parts[1]}' in '{line}'.")
                if not months or not all(1 <= x <= 12 for x in months):
                    raise ConfigError(f"Invalid months '{parts[1]}' in '{line}'.")
            periods.append(Period(start=key, months=months))
        elif key_type == KT.YEAR_NUMBER and not key[0]:
            if rule not in AUTO_RULES:
                raise ConfigError(
                    f"Unknown schedule rule '{parts[1]}' in '{line}' (issues without a year: 'all' or 'none')."
                )
            periods.append(Period(start=key))
        elif key_type == KT.YEAR_NUMBER:
            if rule in AUTO_RULES:
                periods.append(Period(start=key, per_year=None))
            elif rule.isdigit() and int(rule) > 0:
                periods.append(Period(start=key, per_year=int(rule)))
            else:
                raise ConfigError(f"Unknown schedule rule '{parts[1]}' in '{line}' (issues per year or 'auto').")
        else:
            if rule not in AUTO_RULES:
                raise ConfigError(f"Unknown schedule rule '{parts[1]}' in '{line}' ('all' or 'none').")
            periods.append(Period(start=key))
    periods.sort(key=lambda p: p.start)
    return periods


def _active_period(periods: list[Period], key: Key) -> Period | None:
    active = None
    for period in periods:
        if period.start <= key:
            active = period
        else:
            break
    return active


def suggest_schedule(key_type: str, present: Iterable[Key], today: datetime.date | None = None) -> str:
    """Schedule lines guessed from the issues found: where the issues become regular, and how often they come out.

    The older issues of a series are often a few scattered ones, so it starts at the first year from which every
    year has most of its issues.
    """
    keys = sorted(set(present))
    if not keys:
        return ""
    today = today or timezone.localdate()
    if key_type == KT.NUMBER:
        return f"#{keys[0][1]} all"
    lines = []
    if key_type == KT.YEAR_NUMBER and not keys[0][0]:
        # Issues numbered without a year come first: all of them, then the yearly ones.
        lines.append(f"{key_label(key_type, keys[0])} all")
        keys = [key for key in keys if key[0]]
        if not keys:
            return lines[0]
    by_year: dict[int, set[int]] = defaultdict(set)
    for year, value in keys:
        by_year[year].add(value)
    years = list(range(keys[0][0], keys[-1][0] + 1))
    last_year = keys[-1][0]

    if key_type == KT.YEAR_MONTH:
        classes: dict[int, str] = {}
        coverage: dict[int, float] = {}
        for year in years:
            months = by_year.get(year, set())
            if len(months) >= 3 and all(m % 2 == 0 for m in months):
                cls = "even"
            elif len(months) >= 3 and all(m % 2 == 1 for m in months):
                cls = "odd"
            else:
                cls = "monthly"
            classes[year] = cls
            expected = [m for m in MONTH_RULES[cls] if year != last_year or m <= max(months, default=12)]
            coverage[year] = len(months) / len(expected) if expected else 0
        start_year = _dense_start(years, coverage)
        current = None
        for year in years:
            if year < start_year or not by_year.get(year):
                continue
            if classes[year] != current:
                current = classes[year]
                lines.append(f"{key_label(key_type, (year, min(by_year[year])))} {current}")
        return "\n".join(lines)

    # Year and number
    coverage = {year: (len(by_year[year]) / max(by_year[year])) if by_year.get(year) else 0 for year in years}
    start_year = _dense_start(years, coverage)
    full_years = [year for year in years if start_year <= year < last_year and by_year.get(year)]
    counts = Counter(max(by_year[year]) for year in full_years)
    per_year = counts.most_common(1)[0][0] if counts else None
    if per_year and counts[per_year] * 2 > len(full_years):
        lines.append(f"{start_year} {per_year}")
    else:
        lines.append(f"{start_year} auto")
    return "\n".join(lines)


def _dense_start(years: list[int], coverage: dict[int, float], threshold: float = 0.6) -> int:
    start = years[-1]
    for year in reversed(years):
        if coverage.get(year, 0) >= threshold:
            start = year
        else:
            break
    return start


# --- Candidates ---


@dataclasses.dataclass
class Entry:
    kind: str  # gallery or archive
    pk: int
    title: str
    date: datetime.date | None  # gallery: posted, archive: added
    detail: str = ""
    has_archive: bool = False  # gallery: it has an archive, even one that doesn't match the series' filters

    @property
    def url(self) -> str:
        return reverse("viewer:gallery" if self.kind == "gallery" else "viewer:archive", args=(self.pk,))

    def as_dict(self) -> dict[str, typing.Any]:
        return {
            "kind": self.kind,
            "id": self.pk,
            "title": self.title,
            "date": self.date.isoformat() if self.date else None,
            "detail": self.detail,
            "url": self.url,
            **({"has_archive": self.has_archive} if self.kind == "gallery" else {}),
        }


def parse_tag_list(text: str) -> list[tuple[str, str]]:
    tags = []
    for raw in (text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        scope, sep, name = raw.rpartition(":")
        tags.append((scope.strip(), name.strip()) if sep else ("", raw))
    return tags


def _languages_ok(languages: set[str], allowed: set[str]) -> bool:
    return not allowed or languages <= allowed


def _to_date(value: datetime.datetime | None) -> datetime.date | None:
    if value is None:
        return None
    return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()


GALLERY_STATUS_LABELS = {value: label for value, label in Gallery.StatusChoices.choices}


def _series_filters(series: ReleaseSeries) -> tuple[list[str], list[str], list[tuple[str, str]], set[str]]:
    """The title texts, lowercase title excludes, tags and lowercase allowed languages that find a series' entries."""
    contains = _lines(series.title_contains)
    excludes = [x.lower() for x in _lines(series.title_excludes)]
    allowed = {x.strip().lower() for x in (series.allowed_languages or "").split(",") if x.strip()}
    return contains, excludes, parse_tag_list(series.tags), allowed


def collect_candidates(series: ReleaseSeries) -> tuple[list[Entry], int]:
    """Galleries and archives of a series: title contains any text, has any tag, isn't excluded, language allowed.

    :return: the candidates, and how many were skipped because of their language.
    """
    contains, excludes, tag_specs, allowed = _series_filters(series)
    if not contains and not tag_specs:
        raise ConfigError("Set at least a title text or a tag, to find the series' galleries and archives.")

    title_q = Q()
    for text in contains:
        title_q |= Q(title__icontains=text)
    tag_ids: list[int] | None = None
    if tag_specs:
        tag_q = Q()
        for scope, name in tag_specs:
            tag_q |= Q(scope=scope, name=name)
        tag_ids = list(Tag.objects.filter(tag_q).values_list("id", flat=True))
        if not tag_ids:
            return [], 0

    galleries = Gallery.objects.filter(title_q)
    archives = Archive.objects.filter(title_q)
    if tag_ids is not None:
        galleries = galleries.filter(tags__in=tag_ids)
        archives = archives.filter(tags__in=tag_ids)
    gallery_rows = {
        row["id"]: row for row in galleries.values("id", "title", "posted", "status", "provider").distinct()
    }
    archive_rows = {
        row["id"]: row
        for row in archives.values("id", "title", "create_date", "binned", "file_deleted", "gallery_id").distinct()
    }

    gallery_langs = _language_tags(Gallery.tags.through, "gallery_id", list(gallery_rows))
    archive_langs = _language_tags(ArchiveTag, "archive_id", list(archive_rows))

    entries: list[Entry] = []
    skipped_language = 0
    for pk, row in gallery_rows.items():
        title = row["title"] or ""
        if any(x in title.lower() for x in excludes):
            continue
        if not _languages_ok(gallery_langs.get(pk, set()), allowed):
            skipped_language += 1
            continue
        detail = row["provider"] or ""
        if row["status"] != Gallery.StatusChoices.NORMAL:
            detail = f"{detail}, {GALLERY_STATUS_LABELS.get(row['status'], row['status'])}".strip(", ")
        entries.append(Entry("gallery", pk, title, _to_date(row["posted"]), detail))
    with_archive = _galleries_with_archives([e.pk for e in entries])
    for entry in entries:
        entry.has_archive = entry.pk in with_archive
    for pk, archive_row in archive_rows.items():
        title = archive_row["title"] or ""
        if any(x in title.lower() for x in excludes):
            continue
        if not _languages_ok(archive_langs.get(pk, set()), allowed):
            skipped_language += 1
            continue
        flags = []
        if archive_row["binned"]:
            flags.append("in Recycle Bin")
        if archive_row["file_deleted"]:
            flags.append("file deleted")
        entries.append(Entry("archive", pk, title, _to_date(archive_row["create_date"]), ", ".join(flags)))
    return entries, skipped_language


def _galleries_with_archives(ids: list[int]) -> set[int]:
    with_archive: set[int] = set()
    for start in range(0, len(ids), CHUNK):
        rows = Archive.objects.filter(gallery_id__in=ids[start : start + CHUNK]).values_list("gallery_id", flat=True)
        with_archive.update(rows.distinct())
    return with_archive


def _language_tags(through: typing.Any, fk: str, ids: list[int]) -> dict[int, set[str]]:
    langs: dict[int, set[str]] = defaultdict(set)
    for start in range(0, len(ids), CHUNK):
        rows = through.objects.filter(**{f"{fk}__in": ids[start : start + CHUNK], "tag__scope": "language"})
        for owner_id, name in rows.values_list(fk, "tag__name"):
            langs[owner_id].add(name.lower())
    return langs


# --- Check ---


@dataclasses.dataclass
class Issue:
    key: Key
    label: str
    status: str  # present, missing, dismissed, extra (present but not expected), upcoming
    entries: list[Entry] = dataclasses.field(default_factory=list)
    release: datetime.date | None = None  # when it came out: earliest gallery posted date, or estimated
    estimated: bool = False
    dismissal: typing.Any = None
    query: str = ""

    @property
    def gallery_count(self) -> int:
        return sum(1 for e in self.entries if e.kind == "gallery")

    @property
    def archive_count(self) -> int:
        return sum(1 for e in self.entries if e.kind == "archive")

    @property
    def gallery_only(self) -> bool:
        """Found, but only as galleries, and none of them has an archive."""
        return bool(self.entries) and not any(e.kind == "archive" or e.has_archive for e in self.entries)

    def as_dict(self) -> dict[str, typing.Any]:
        data: dict[str, typing.Any] = {
            "key": self.label,
            "status": self.status,
            "release_date": self.release.isoformat() if self.release else None,
            "release_estimated": self.estimated,
            "galleries": self.gallery_count,
            "archives": self.archive_count,
            "gallery_only": self.gallery_only,
        }
        if self.dismissal is not None:
            data["dismissal"] = {"reason": self.dismissal.reason, "comment": self.dismissal.comment}
        if self.entries:
            data["entries"] = [e.as_dict() for e in self.entries]
        return data


@dataclasses.dataclass
class Row:
    label: str
    cells: list[Issue | None]


@dataclasses.dataclass
class SeriesResult:
    series: ReleaseSeries
    error: str = ""
    issues: list[Issue] = dataclasses.field(default_factory=list)
    upcoming: list[Issue] = dataclasses.field(default_factory=list)
    unparsed: list[Entry] = dataclasses.field(default_factory=list)
    unparsed_count: int = 0
    candidates: int = 0
    skipped_language: int = 0
    before_start: int = 0
    measured_lead_days: int | None = None
    lead_sample: int = 0
    suggested_schedule: str = ""
    using_suggested_schedule: bool = False
    periods: list[Period] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def missing(self) -> list[Issue]:
        return [i for i in self.issues if i.status == "missing"]

    @property
    def present(self) -> list[Issue]:
        return [i for i in self.issues if i.status in ("present", "extra")]

    @property
    def gallery_only(self) -> list[Issue]:
        return [i for i in self.present if i.gallery_only]

    @property
    def archived(self) -> list[Issue]:
        """Found with an archive: what counts, galleries alone are only metadata."""
        return [i for i in self.present if not i.gallery_only]

    @property
    def dismissed(self) -> list[Issue]:
        return [i for i in self.issues if i.status == "dismissed"]

    @property
    def expected_count(self) -> int:
        return sum(1 for i in self.issues if i.status != "extra")

    @property
    def first(self) -> Issue | None:
        return self.issues[0] if self.issues else None

    @property
    def last(self) -> Issue | None:
        return self.issues[-1] if self.issues else None

    @property
    def completeness(self) -> float:
        expected = self.expected_count - len(self.dismissed)
        if expected <= 0:
            return 1.0
        return 1 - len(self.missing) / expected

    @property
    def archive_completeness(self) -> float:
        """Like ``completeness``, but an expected issue found only as galleries counts as missing."""
        expected = self.expected_count - len(self.dismissed)
        if expected <= 0:
            return 1.0
        gallery_only = sum(1 for i in self.gallery_only if i.status == "present")
        return 1 - (len(self.missing) + gallery_only) / expected

    @property
    def gallery_only_share(self) -> float:
        """The part of ``completeness`` that's only galleries, for the second segment of the bar."""
        return max(self.completeness - self.archive_completeness, 0.0)

    @property
    def lead_days(self) -> int | None:
        return self.series.lead_days if self.series.lead_days is not None else self.measured_lead_days

    def schedule_rows(self) -> list[tuple[str, str]]:
        return [(key_label(self.series.key_type, p.start), p.describe(self.series.key_type)) for p in self.periods]

    def rows(self) -> list[Row]:
        """The issues laid out for the grid: a row per year (twelve months, or its numbers), or per 20 numbers."""
        key_type = self.series.key_type
        cells = self.issues + self.upcoming
        if not cells:
            return []
        by_key = {i.key: i for i in cells}
        rows = []
        if key_type == KT.YEAR_MONTH:
            for year in range(cells[0].key[0], cells[-1].key[0] + 1):
                rows.append(Row(str(year), [by_key.get((year, m)) for m in range(1, 13)]))
        elif key_type == KT.YEAR_NUMBER:
            for year in sorted({k[0] for k in by_key}):
                numbers = [k[1] for k in by_key if k[0] == year]
                if not year:
                    rows += _number_rows(by_key, min(numbers), max(numbers))
                else:
                    rows.append(Row(str(year), [by_key.get((year, n)) for n in range(1, max(numbers) + 1)]))
        else:
            rows += _number_rows(by_key, cells[0].key[1], cells[-1].key[1])
        return rows

    def as_dict(self, include_entries: bool = False) -> dict[str, typing.Any]:
        data: dict[str, typing.Any] = {
            "id": self.series.pk,
            "name": self.series.name,
            "key_type": self.series.key_type,
            "error": self.error,
            "candidates": self.candidates,
            "expected": self.expected_count,
            "present": len(self.present),
            "archived": len(self.archived),
            "completeness": round(self.completeness, 4),
            "archive_completeness": round(self.archive_completeness, 4),
            "gallery_only": [i.label for i in self.gallery_only],
            "missing": [i.label for i in self.missing],
            "dismissed": [i.label for i in self.dismissed],
            "upcoming": [i.as_dict() for i in self.upcoming],
            "first": self.first.label if self.first else None,
            "last": self.last.label if self.last else None,
            "lead_days": self.lead_days,
            "measured_lead_days": self.measured_lead_days,
            "suggested_schedule": self.suggested_schedule,
            "using_suggested_schedule": self.using_suggested_schedule,
            "unparsed": self.unparsed_count,
            "skipped_language": self.skipped_language,
            "before_start": self.before_start,
            "warnings": self.warnings,
        }
        if include_entries:
            data["issues"] = [
                i.as_dict() if i.status != "missing" else {**i.as_dict(), "backlog_query": i.query}
                for i in self.issues
            ]
            data["unparsed_entries"] = [e.as_dict() for e in self.unparsed]
        else:
            data["missing_issues"] = [
                {"key": i.label, "release_date": i.release.isoformat() if i.release else None} for i in self.missing
            ]
        return data


def _number_rows(by_key: dict[Key, Issue], first: int, last: int) -> list[Row]:
    """Issues numbered without a year, 20 per row."""
    return [
        Row(f"#{start}", [by_key.get((0, n)) for n in range(start, start + 20)])
        for start in range(first - (first - 1) % 20, last + 1, 20)
    ]


def check_series(series: ReleaseSeries, today: datetime.date | None = None) -> SeriesResult:
    """Compare a series' galleries and archives with the issues its schedule expects."""
    today = today or timezone.localdate()
    result = SeriesResult(series=series)
    try:
        patterns = compile_title_patterns(series)
        if not patterns:
            raise ConfigError("Set a title regex to read the issue from the titles.")
        periods = parse_schedule(series.key_type, series.schedule)
        entries, result.skipped_language = collect_candidates(series)
    except ConfigError as e:
        result.error = str(e)
        return result
    key_type = series.key_type
    result.candidates = len(entries)

    found: dict[Key, list[Entry]] = defaultdict(list)
    unparsed = []
    for entry in entries:
        keys = keys_from_title(key_type, patterns, entry.title)
        if not keys:
            unparsed.append(entry)
        for key in keys:
            found[key].append(entry)
    unparsed.sort(key=lambda e: e.title.lower())
    result.unparsed_count = len(unparsed)
    result.unparsed = unparsed[:MAX_UNPARSED]

    releases = {key: _release_date(group) for key, group in found.items()}
    result.suggested_schedule = suggest_schedule(key_type, found, today)
    if key_type == KT.YEAR_MONTH:
        result.measured_lead_days, result.lead_sample = measure_lead_days(found)
    if not periods and result.suggested_schedule:
        periods = parse_schedule(key_type, result.suggested_schedule)
        result.using_suggested_schedule = True
    result.periods = periods
    if not periods:
        return result

    expected, upcoming = _expected_keys(result, periods, found, today)
    dismissals = {d.key: d for d in series.dismissals.all()}
    expected_set = set(expected)
    start = periods[0].start
    result.before_start = sum(1 for key in found if key < start)
    all_keys = sorted(expected_set | {key for key in found if key >= start})

    issues = []
    for key in all_keys:
        label = key_label(key_type, key)
        if key in found:
            status = "present" if key in expected_set else "extra"
            group = sorted(found[key], key=lambda e: (e.kind != "gallery", e.date or datetime.date.max))
            issues.append(Issue(key, label, status, entries=group, release=releases.get(key)))
        elif label in dismissals:
            issues.append(Issue(key, label, "dismissed", dismissal=dismissals[label]))
        else:
            issues.append(Issue(key, label, "missing"))
    result.issues = issues
    result.upcoming = [
        Issue(key, key_label(key_type, key), "due" if date <= today else "upcoming", release=date, estimated=True)
        for key, date in upcoming
    ]
    _estimate_releases(result)
    for issue in result.missing:
        issue.query = backlog_query_for(series, issue.key)
    return result


def _release_date(entries: list[Entry]) -> datetime.date | None:
    """When an issue came out: its earliest gallery posted date. Archives only tell when they were added."""
    dates = [e.date for e in entries if e.kind == "gallery" and e.date]
    return min(dates) if dates else None


def measure_lead_days(found: dict[Key, list[Entry]]) -> tuple[int | None, int]:
    """Median days between the release (gallery posted) and the cover month, over the last issues found."""
    samples = []
    for key in sorted(found, reverse=True):
        release = _release_date(found[key])
        if release is None:
            continue
        samples.append((datetime.date(key[0], key[1], 1) - release).days)
        if len(samples) >= LEAD_SAMPLE:
            break
    if not samples:
        return None, 0
    return int(round(statistics.median(samples))), len(samples)


def _expected_keys(
    result: SeriesResult, periods: list[Period], found: dict[Key, list[Entry]], today: datetime.date
) -> tuple[list[Key], list[tuple[Key, datetime.date]]]:
    key_type = result.series.key_type
    present = sorted(found)
    last_present = present[-1] if present else periods[0].start
    expected: list[Key] = []
    upcoming: list[tuple[Key, datetime.date]] = []

    if key_type == KT.YEAR_MONTH:
        lead = result.lead_days
        if lead is not None:
            # The last cover month that's missing if not found: out for more than the grace days.
            cutoff = today - datetime.timedelta(days=GRACE_DAYS)
            end = _add_months((today.year, today.month), 24)
            while _cover_release(end, lead) > cutoff:
                end = _add_months(end, -1)
            end = max(end, last_present)
        else:
            end = last_present
        key = periods[0].start
        while key <= end:
            period = _active_period(periods, key)
            if period and not period.stopped and period.months and key[1] in period.months:
                expected.append(key)
            key = _add_months(key, 1)
        if lead is not None:
            # The issues due (released recently, not found yet) and the next one, with when they should come out.
            for _ in range(24):
                period = _active_period(periods, key)
                if period is None or period.stopped:
                    break
                if period.months and key[1] in period.months and key not in found:
                    release = _cover_release(key, lead)
                    upcoming.append((key, release))
                    if release > today:
                        break
                key = _add_months(key, 1)
        return expected, upcoming

    if key_type == KT.YEAR_NUMBER:
        # Issues numbered without a year (year 0) come first, checked up to the highest number found.
        if not periods[0].start[0]:
            last_number = max((number for year, number in present if not year), default=0)
            for number in range(periods[0].start[1], last_number + 1):
                active = _active_period(periods, (0, number))
                if active and not active.stopped:
                    expected.append((0, number))
        yearly = [p for p in periods if p.start[0]]
        if not yearly:
            return expected, upcoming
        by_year: dict[int, int] = defaultdict(int)
        for year, number in present:
            if year:
                by_year[year] = max(by_year[year], number)
        last_year = max(by_year, default=yearly[0].start[0])
        for year in range(yearly[0].start[0], last_year + 1):
            period = _active_period(periods, (year, 10**6))
            upper = by_year.get(year, 0)
            if period and period.per_year and not period.stopped and year < last_year:
                upper = max(upper, period.per_year)
            if upper == 0 and period and not period.stopped:
                result.warnings.append(f"No issues found for {year}, so its issues can't be checked.")
            for number in range(1, upper + 1):
                active = _active_period(periods, (year, number))
                if active and not active.stopped:
                    expected.append((year, number))
        return expected, upcoming

    for number in range(periods[0].start[1], last_present[1] + 1):
        active = _active_period(periods, (0, number))
        if active and not active.stopped:
            expected.append((0, number))
    return expected, upcoming


def _cover_release(key: Key, lead: int) -> datetime.date:
    return datetime.date(key[0], key[1], 1) - datetime.timedelta(days=lead)


def _estimate_releases(result: SeriesResult) -> None:
    """Estimate when the missing issues came out: from the cover month and lead days, or between their neighbors."""
    issues = result.issues
    if result.series.key_type == KT.YEAR_MONTH and result.lead_days is not None:
        for issue in issues:
            if issue.status in ("missing", "dismissed"):
                issue.release = _cover_release(issue.key, result.lead_days)
                issue.estimated = True
        return
    known = [(i, issue.release) for i, issue in enumerate(issues) if issue.release]
    if not known:
        return
    steps = [
        (b_date - a_date).days / (b - a)
        for (a, a_date), (b, b_date) in zip(known, known[1:])
        if b > a and b_date >= a_date
    ]
    step = statistics.median(steps) if steps else None
    for i, issue in enumerate(issues):
        if issue.status not in ("missing", "dismissed"):
            continue
        before = next(((j, d) for j, d in reversed(known) if j < i), None)
        after = next(((j, d) for j, d in known if j > i), None)
        if before and after:
            days = (after[1] - before[1]).days * (i - before[0]) / (after[0] - before[0])
            issue.release = before[1] + datetime.timedelta(days=round(days))
        elif before and step is not None:
            issue.release = before[1] + datetime.timedelta(days=round(step * (i - before[0])))
        elif after and step is not None:
            issue.release = after[1] - datetime.timedelta(days=round(step * (after[0] - i)))
        issue.estimated = issue.release is not None


# --- Overview ---


@dataclasses.dataclass
class Gap:
    result: SeriesResult
    issue: Issue


@dataclasses.dataclass
class GapWindow:
    gaps: list[Gap]

    @property
    def start(self) -> datetime.date | None:
        return self.gaps[0].issue.release

    @property
    def end(self) -> datetime.date | None:
        return self.gaps[-1].issue.release

    @property
    def series_count(self) -> int:
        return len({g.result.series.pk for g in self.gaps})


def gap_windows(results: list[SeriesResult]) -> list[GapWindow]:
    """Every missing issue of the series, by estimated release date, grouped when they came out close together.

    Issues missing from several series in the same weeks usually mean the crawlers weren't running then. Missing
    issues without an estimated date go last, in their own group. Newest first.
    """
    dated: list[Gap] = []
    undated: list[Gap] = []
    for result in results:
        for issue in result.missing:
            (dated if issue.release else undated).append(Gap(result, issue))
    dated.sort(key=lambda g: (g.issue.release, g.result.series.name, g.issue.key))
    windows: list[GapWindow] = []
    for gap in dated:
        if windows and (gap.issue.release - windows[-1].end).days <= WINDOW_GAP_DAYS:  # type: ignore[operator]
            windows[-1].gaps.append(gap)
        else:
            windows.append(GapWindow([gap]))
    windows.reverse()
    if undated:
        windows.append(GapWindow(undated))
    return windows


# --- Backlog search ---


class _FormatValues(dict):
    def __missing__(self, key: str) -> str:
        raise ConfigError(f"Unknown placeholder {{{key}}} in the backlog query.")


def validate_backlog_query(template: str) -> None:
    try:
        template.format_map(
            _FormatValues(title="Name 2026-01", name="Name", key="2026-01", year=2026, month=1, number=1)
        )
    except (ValueError, IndexError, AttributeError) as e:
        raise ConfigError(f"Invalid backlog query: {e}")


def search_title(series: ReleaseSeries, key: Key) -> str:
    """The title searched for an issue by default: the series' name and the issue (``WEEKLY Kairakuten 2024 No.9``)."""
    return f"{series.name} {key_label(series.key_type, key)}"


def clean_search_title(text: str) -> str:
    """A search title typed by hand, safe inside the queries' quotes: without double quotes, on one line."""
    return " ".join(text.replace('"', " ").split())[:200]


def backlog_query_for(series: ReleaseSeries, key: Key, page: bool = False, title: str = "") -> str:
    """The backlog query for an issue: for es-gallery-json sources, or for provider search pages when page is set.

    title replaces the search title (``{title}``, by default the series' name and the issue), for uploads titled in an
    unusual way. A query without ``{title}`` can't use it, so the default query is used instead.
    """
    if page:
        template = series.backlog_page_query or default_backlog_page_query(series.key_type)
        if title and "{title}" not in template:
            template = default_backlog_page_query(series.key_type)
    else:
        template = series.backlog_query or default_backlog_query(series.key_type)
        if title and "{title}" not in template:
            template = default_backlog_query(series.key_type)
    values = _FormatValues(
        title=title or search_title(series, key),
        name=series.name,
        key=key_label(series.key_type, key),
        year=key[0],
        month=key[1] if series.key_type == KT.YEAR_MONTH else 0,
        number=key[1] if series.key_type != KT.YEAR_MONTH else 0,
    )
    try:
        return template.format_map(values)
    except (ConfigError, ValueError, IndexError, AttributeError) as e:
        logger.warning("Release series %s: invalid backlog query: %s", series.name, e)
        return ""


def is_json_source(url: str) -> bool:
    """es-gallery-json endpoints answer JSON; any other backlog source is a provider's search page (panda)."""
    return "es-gallery-json" in url


def backlog_sources() -> list[dict[str, str]]:
    crawler_settings = settings.CRAWLER_SETTINGS
    sources = []
    for item in crawler_settings.backlog_search.urls:
        name = item["name"] if isinstance(item, dict) else item.name
        url = item["url"] if isinstance(item, dict) else item.url
        sources.append({"name": name, "url": url})
    return sources


def _search_page_parser(url: str) -> "BaseParser | None":
    """The provider parser that can read the search page at url, if any.

    Providers whose address is a setting (doujinshi_mirror) only recognise their URLs with an instance.
    """
    crawler_settings = settings.CRAWLER_SETTINGS
    for parser_class in crawler_settings.provider_context.parsers:
        if getattr(parser_class, "name", "") == "generic" or not parser_class.search_page_implemented():
            continue
        if any(word in url for word in parser_class.accepted_urls):
            return parser_class(crawler_settings)
        parser = parser_class(crawler_settings)
        if parser.filter_accepted_urls([url]):
            return parser
    return None


# Tags worth seeing before queuing a backlog hit, shown without their namespace.
NOTABLE_HIT_TAGS = ("other:already_uploaded", "other:replaced")


@dataclasses.dataclass
class BacklogHit:
    source: str  # the sources that found it, comma separated
    title: str
    url: str
    provider: str
    posted: str
    image_count: int | None
    size: int | None
    languages: list[str]
    keys: list[str]
    matches: bool  # its title is this issue, with an allowed language
    known: bool  # already a gallery here
    not_counted: str = ""  # already here but not counted as this issue: why, see _not_counted
    gid: str = ""
    expunged: bool | None = None
    replaced_by: str = ""  # link to the newest version, when a newer version replaced it
    replaced_by_known: bool = False  # that newer version is already a gallery here
    replaces: str = ""  # link to the older version it's an update of
    flags: list[str] = dataclasses.field(default_factory=list)  # NOTABLE_HIT_TAGS it has
    suggested: bool = False  # the one to queue for the issue, see _suggest
    live: bool = False  # read from the provider's API (search page sources), fresher than an es-gallery-json copy

    def as_dict(self) -> dict[str, typing.Any]:
        return dataclasses.asdict(self)


def search_backlog(
    series: ReleaseSeries, key: Key, source_names: list[str] | None = None, max_hits: int = 50, title: str = ""
) -> tuple[dict[str, str], list[BacklogHit], list[str]]:
    """Search the backlog sources for an issue. Only reads the first page of results; nothing is added.

    title replaces the search title in the queries (see backlog_query_for); the hits are still read as this issue.

    es-gallery-json sources get the backlog query; other sources are read as a provider's search page (panda), with the
    page query, and the provider's API gives the galleries' titles and tags. A gallery found by several sources is one
    hit. At most one hit is suggested to queue.

    :return: the query sent to each source, the hits, and an error message per source that failed.
    """
    patterns = compile_title_patterns(series)
    allowed = {x.strip().lower() for x in (series.allowed_languages or "").split(",") if x.strip()}

    queries: dict[str, str] = {}
    hits: list[BacklogHit] = []
    errors: list[str] = []
    for source in backlog_sources():
        if source_names and source["name"] not in source_names:
            continue
        json_source = is_json_source(source["url"])
        query = backlog_query_for(series, key, page=not json_source, title=title)
        if not query:
            errors.append(f"{source['name']}: the series' backlog query is invalid.")
            continue
        queries[source["name"]] = query
        try:
            if json_source:
                raws = _search_json_source(source["url"], query, max_hits)
            else:
                raws = _search_page_source(source["url"], query, max_hits)
        except ConfigError as e:
            errors.append(f"{source['name']}: {e}")
            continue
        except Exception as e:  # Network errors, unexpected answers, a provider in cooldown.
            logger.warning("Release series backlog search on %s failed: %s", source["name"], e)
            errors.append(f"{source['name']}: {e}")
            continue
        hits.extend(_backlog_hit(series.key_type, patterns, allowed, source["name"], raw, key) for raw in raws)
    hits = _merge_hits(hits)
    _not_counted(series, key, patterns, _mark_known(hits))
    _suggest(hits)
    hits.sort(key=lambda h: (not h.suggested, not h.matches, h.known, _hit_rank(h)))
    return queries, hits, errors


def _search_json_source(source_url: str, query: str, max_hits: int) -> list[dict[str, typing.Any]]:
    import requests

    from core.base.utilities import construct_request_dict

    crawler_settings = settings.CRAWLER_SETTINGS
    own_settings = crawler_settings.providers.get("chaika") if hasattr(crawler_settings.providers, "get") else None
    if own_settings is not None:
        request_dict = construct_request_dict(crawler_settings, own_settings)
    else:
        request_dict = {"headers": crawler_settings.requests_headers, "timeout": 30}
    response = requests.get(source_url + urllib.parse.quote_plus(query) + "&no_agg=1", **request_dict)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        raise ConfigError("the answer isn't JSON, is the URL an es-gallery-json endpoint?")
    raws = (data.get("hits") or [])[:max_hits] if isinstance(data, dict) else []
    for raw in raws:
        # The gallery chain, oldest first: the versions of a gallery that replaced each other.
        chain = [u for u in raw.get("gallery_chain_urls") or [] if isinstance(u, str)]
        url = raw.get("source_url") or ""
        position = chain.index(url) if url in chain else -1
        if chain and raw.get("last_in_chain") is False and chain[-1] != url:
            raw["replaced_by"] = chain[-1]
        if position > 0:
            raw["replaces"] = chain[position - 1]
    return raws


def _search_page_source(source_url: str, query: str, max_hits: int) -> list[dict[str, typing.Any]]:
    parser = _search_page_parser(source_url)
    if parser is None:
        raise ConfigError("not an es-gallery-json endpoint or a search page of a provider that can be read.")
    links = parser.links_from_search_page(source_url + urllib.parse.quote_plus(query))[:max_hits]
    if not links:
        return []
    raws = []
    for data in parser.fetch_multiple_gallery_data(links) or []:
        link = data.link or ""
        extra = data.extra_data or {}
        raws.append(
            {
                "gid": data.gid,
                "title": data.title or data.title_jpn or "",
                "title_jpn": data.title_jpn or "",
                "source_url": link,
                "provider": data.provider or parser.name,
                "posted_date": data.posted.isoformat() if data.posted else "",
                "image_count": data.filecount,
                "size": data.filesize,
                "tags": [{"full": tag} for tag in data.tags or []],
                "expunged": data.expunged,
                # panda's API: current_gid/key when a newer version replaced it, parent_gid/key when it's an update.
                "replaced_by": _same_site_link(link, extra.get("current_gid"), extra.get("current_key")),
                "replaces": _same_site_link(link, extra.get("parent_gid"), extra.get("parent_key")),
                "live": True,
            }
        )
    return raws


def _same_site_link(link: str, gid: typing.Any, token: typing.Any) -> str:
    """A gallery link like link (…/g/<gid>/<token>/), for another gid and token."""
    if not gid or not token or not re.search(r"/g/\d+/\w+", link):
        return ""
    return re.sub(r"/g/\d+/\w+", f"/g/{gid}/{token}", link, count=1)


def _int_or_none(value: typing.Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _backlog_hit(
    key_type: str, patterns: list[re.Pattern[str]], allowed: set[str], source: str, raw: dict, key: Key
) -> BacklogHit:
    title = raw.get("title") or ""
    tags = [t.get("full", "") if isinstance(t, dict) else str(t) for t in raw.get("tags") or []]
    tags = [t.lower().replace(" ", "_") for t in tags]
    languages = sorted({t.split(":", 1)[1] for t in tags if t.startswith("language:")})
    keys = keys_from_title(key_type, patterns, title) or keys_from_title(key_type, patterns, raw.get("title_jpn") or "")
    expunged = raw.get("expunged")
    return BacklogHit(
        source=source,
        title=title,
        url=raw.get("source_url") or "",
        provider=raw.get("provider") or "",
        posted=(raw.get("posted_date") or "")[:10],
        image_count=_int_or_none(raw.get("image_count")),
        size=_int_or_none(raw.get("size")),
        languages=languages,
        keys=[key_label(key_type, k) for k in keys],
        matches=key in keys and _languages_ok(set(languages), allowed),
        known=False,
        gid=str(raw.get("gid") or ""),
        expunged=bool(expunged) if expunged is not None else None,
        replaced_by=raw.get("replaced_by") or "",
        replaces=raw.get("replaces") or "",
        flags=[t.split(":", 1)[1] for t in NOTABLE_HIT_TAGS if t in tags],
        live=bool(raw.get("live")),
    )


def _merge_hits(hits: list[BacklogHit]) -> list[BacklogHit]:
    """One hit per gallery found by several sources. The provider's API (live) wins over es-gallery-json copies."""
    merged: dict[tuple[str, str], BacklogHit] = {}
    result: list[BacklogHit] = []
    for hit in sorted(hits, key=lambda h: not h.live):
        ident = (hit.provider, hit.gid or hit.url)
        if not ident[1]:
            result.append(hit)
            continue
        first = merged.get(ident)
        if first is None:
            merged[ident] = hit
            result.append(hit)
            continue
        if hit.source not in first.source.split(", "):
            first.source += ", " + hit.source
        first.languages = sorted(set(first.languages) | set(hit.languages))
        first.matches = first.matches or hit.matches
        first.keys = first.keys or hit.keys
        if first.expunged is None:
            first.expunged = hit.expunged
        first.replaced_by = first.replaced_by or hit.replaced_by
        first.replaces = first.replaces or hit.replaces
        first.flags = first.flags or hit.flags
    return result


def _hit_rank(hit: BacklogHit) -> tuple[typing.Any, ...]:
    """Best first: not expunged, not replaced, no notable tags, most images, newest."""
    return (
        bool(hit.expunged),
        bool(hit.replaced_by or "replaced" in hit.flags),
        bool(hit.flags),
        -(hit.image_count or 0),
        _negated_date(hit.posted),
    )


def _negated_date(posted: str) -> int:
    try:
        return -datetime.date.fromisoformat(posted).toordinal()
    except ValueError:
        return 0


def _suggest(hits: list[BacklogHit]) -> None:
    """Suggest the best hit for the issue: its title is the issue, in an allowed language, not already here, not
    expunged and not replaced by a newer version. None when a hit for the issue is already here."""
    matching = [h for h in hits if h.matches]
    if any(h.known or h.replaced_by_known for h in matching):
        return
    candidates = [h for h in matching if h.url and not h.expunged and not h.replaced_by and "replaced" not in h.flags]
    if candidates:
        min(candidates, key=_hit_rank).suggested = True


def _mark_known(hits: list[BacklogHit]) -> dict[int, list[BacklogHit]]:
    """Flag the hits that are already galleries here, and the ones whose newer version is, by provider and id.

    :return: the hits that are already here, by the id of their gallery here.
    """
    crawler_settings = settings.CRAWLER_SETTINGS
    parsers: dict[str, typing.Any] = {}

    def gallery_id(provider: str, url: str) -> str | None:
        if provider not in parsers:
            try:
                found = crawler_settings.provider_context.get_parsers(crawler_settings, filter_name=provider)
                parsers[provider] = found[0] if found else None
            except Exception:  # A provider's parsers shouldn't break the search.
                parsers[provider] = None
        try:
            return parsers[provider].id_from_url(url) if parsers[provider] else None
        except Exception:
            return None

    by_provider: dict[str, dict[str, list[tuple[BacklogHit, str]]]] = defaultdict(lambda: defaultdict(list))
    for hit in hits:
        if not hit.provider:
            continue
        for field, url in (("known", hit.url), ("replaced_by_known", hit.replaced_by)):
            gid = gallery_id(hit.provider, url) if url else None
            if gid:
                by_provider[hit.provider][gid].append((hit, field))
    known: dict[int, list[BacklogHit]] = defaultdict(list)
    for provider, gids in by_provider.items():
        for pk, gid in Gallery.objects.filter(provider=provider, gid__in=list(gids)).values_list("pk", "gid"):
            for hit, field in gids[gid]:
                setattr(hit, field, True)
                if field == "known":
                    known[pk].append(hit)
    return known


def _not_counted(
    series: ReleaseSeries, key: Key, patterns: list[re.Pattern[str]], known: dict[int, list[BacklogHit]]
) -> None:
    """Explain the hits that are already here but that the series doesn't count as this issue.

    The grid only counts the galleries and archives that collect_candidates finds and whose title is the issue, so a
    gallery that's here can still leave the issue missing: then neither it nor its archives count, and adding it again
    doesn't help (it's skipped when it has an archive). The reasons are the series' filters it fails, per gallery and
    archive, so the series or the gallery can be fixed instead.
    """
    if not known:
        return
    filters = _series_filters(series)
    objects: dict[int, list[tuple[str, str, set[tuple[str, str]]]]] = defaultdict(list)
    gallery_tags = _tags_of(Gallery.tags.through, "gallery_id", list(known))
    for pk, title in Gallery.objects.filter(pk__in=list(known)).values_list("pk", "title"):
        objects[pk].append(("Gallery here", title or "", gallery_tags.get(pk, set())))
    archives = list(Archive.objects.filter(gallery_id__in=list(known)).values_list("pk", "title", "gallery_id"))
    archive_tags = _tags_of(ArchiveTag, "archive_id", [pk for pk, _, _ in archives])
    for pk, title, gallery_pk in sorted(archives):
        objects[gallery_pk].append((f"Archive {pk}", title or "", archive_tags.get(pk, set())))
    for gallery_pk, hits in known.items():
        lines = []
        for label, title, tags in objects.get(gallery_pk, []):
            reasons = _reasons_not_counted(series.key_type, key, patterns, filters, title, tags)
            if not reasons:
                break  # It counts as this issue.
            lines.append(f"{label}: {'; '.join(reasons)}.")
        else:
            for hit in hits:
                hit.not_counted = "\n".join(lines)


def _tags_of(through: typing.Any, fk: str, ids: list[int]) -> dict[int, set[tuple[str, str]]]:
    tags: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for start in range(0, len(ids), CHUNK):
        rows = through.objects.filter(**{f"{fk}__in": ids[start : start + CHUNK]})
        for owner_id, scope, name in rows.values_list(fk, "tag__scope", "tag__name"):
            tags[owner_id].add((scope, name))
    return tags


def _reasons_not_counted(
    key_type: str,
    key: Key,
    patterns: list[re.Pattern[str]],
    filters: tuple[list[str], list[str], list[tuple[str, str]], set[str]],
    title: str,
    tags: set[tuple[str, str]],
) -> list[str]:
    """Why collect_candidates and the title regex don't make a gallery or archive count as this issue."""
    contains, excludes, tag_specs, allowed = filters
    reasons = []
    lower = title.lower()
    if contains and not any(text.lower() in lower for text in contains):
        reasons.append("its title doesn't contain the series' title text")
    excluded = [text for text in excludes if text in lower]
    if excluded:
        reasons.append(f'its title contains "{excluded[0]}", which the series excludes')
    if tag_specs and not any(spec in tags for spec in tag_specs):
        wanted = ", ".join(f"{scope}:{name}" if scope else name for scope, name in tag_specs)
        reasons.append(f"it doesn't have any of the series' tags ({wanted})")
    languages = {name.lower() for scope, name in tags if scope == "language"}
    if not _languages_ok(languages, allowed):
        reasons.append(f"its language ({', '.join(sorted(languages - allowed))}) isn't allowed")
    keys = keys_from_title(key_type, patterns, title)
    if key not in keys:
        if keys:
            read_as = ", ".join(key_label(key_type, k) for k in keys)
            reasons.append(f"its title ({title}) is read as {read_as}")
        else:
            reasons.append(f"its title ({title}) isn't read as an issue by the title regex")
    return reasons
