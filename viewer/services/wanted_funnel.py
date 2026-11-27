"""Wanted-gallery funnel analytics: conversion stages, time-to-find, hit rates, and overdue unfound items.

``build_wanted_funnel`` is the single data source for ``/stats/wanted/`` (HTML and ``?format=json``).
It only reads the database in a fixed, small number of queries (no per-group N+1 loops) and returns
plain JSON-serializable structures (datetimes aside, which ``DjangoJSONEncoder`` handles).

Key methodological details:
- **First-found timestamp**: ``WantedGallery.date_found`` is updated every time a recurring
  (``keep_searching=True``) rule matches a new gallery. To measure true time-to-first-find without
  skewing recurring rules, we use ``MIN(FoundGallery.create_date)`` when ``FoundGallery`` rows exist
  and fall back to ``WantedGallery.date_found`` otherwise.
- **Baselines**: time-to-find and overdue age can be measured from ``create_date`` (default, present
  on every row), ``effective`` (``max(create_date, release_date)``, ignoring wait time before a
  pre-announced release), or ``release_date`` (for items with a release date).
- **Overdue items**: unfound items whose release date is not in the future and whose age since the
  selected baseline is at least ``overdue_days``. Paused items (``should_search=False``) are excluded
  by default so intentionally disabled backlog does not hide actively searched items, and can be
  included via ``include_paused=True``.
"""

from __future__ import annotations

import dataclasses
import datetime
import math
from collections import defaultdict
from typing import Any

from django.db.models import Count, Min, Q, QuerySet
from django.urls import reverse
from django.utils import timezone

from viewer.models import FoundGallery, GalleryMatch, WantedGallery

SCOPE_ALL = "all"
SCOPE_ACTIVE_OR_FOUND = "active_or_found"
SCOPE_SEARCHING = "searching"
SCOPE_ONE_SHOT = "one_shot"
SCOPE_RECURRING = "recurring"

SCOPE_CHOICES: tuple[tuple[str, str], ...] = (
    (SCOPE_ALL, "All wanted"),
    (SCOPE_ACTIVE_OR_FOUND, "Searched or found"),
    (SCOPE_SEARCHING, "Actively searching (should_search)"),
    (SCOPE_ONE_SHOT, "One-shot (keep_searching off)"),
    (SCOPE_RECURRING, "Recurring (keep_searching on)"),
)
SCOPE_KEYS = frozenset(key for key, _ in SCOPE_CHOICES)

BASELINE_CREATE = "create_date"
BASELINE_EFFECTIVE = "effective"
BASELINE_RELEASE = "release_date"

BASELINE_CHOICES: tuple[tuple[str, str], ...] = (
    (BASELINE_CREATE, "Created date"),
    (BASELINE_EFFECTIVE, "Later of created / release"),
    (BASELINE_RELEASE, "Release date"),
)
BASELINE_KEYS = frozenset(key for key, _ in BASELINE_CHOICES)

WINDOW_ALL = "all"
WINDOW_PRESETS: tuple[tuple[str, str, int | None], ...] = (
    ("all", "All time", None),
    ("30d", "Created last 30 days", 30),
    ("90d", "Created last 90 days", 90),
    ("1y", "Created last 1 year", 365),
    ("2y", "Created last 2 years", 730),
)
WINDOW_DAYS: dict[str, int | None] = {key: days for key, _, days in WINDOW_PRESETS}

OVERDUE_SORT_AGE = "age"
OVERDUE_SORT_RELEASE = "release_date"
OVERDUE_SORT_CREATED = "create_date"
OVERDUE_SORT_POSSIBLE = "possible_matches"
OVERDUE_SORT_TITLE = "title"
OVERDUE_SORT_PUBLISHER = "publisher"

OVERDUE_SORT_CHOICES: tuple[tuple[str, str], ...] = (
    (OVERDUE_SORT_AGE, "Longest waiting"),
    (OVERDUE_SORT_POSSIBLE, "Possible matches"),
    (OVERDUE_SORT_RELEASE, "Release date"),
    (OVERDUE_SORT_CREATED, "Created date"),
    (OVERDUE_SORT_PUBLISHER, "Publisher"),
    (OVERDUE_SORT_TITLE, "Title"),
)
OVERDUE_SORT_KEYS = frozenset(key for key, _ in OVERDUE_SORT_CHOICES)

DEFAULT_OVERDUE_DAYS = 30
MIN_OVERDUE_DAYS = 1
MAX_OVERDUE_DAYS = 3650
OVERDUE_DAY_PRESETS: tuple[int, ...] = (7, 14, 30, 90, 180, 365)

DEFAULT_OVERDUE_PER_PAGE = 50
MAX_OVERDUE_PER_PAGE = 200

EMPTY_SENTINEL = "__empty__"

# Histogram bucket definitions: (key, label, min_seconds_inclusive, max_seconds_exclusive)
TTF_BUCKETS: tuple[tuple[str, str, float | None, float | None], ...] = (
    ("lt_0", "Before baseline", None, 0.0),
    ("lt_1d", "< 1 day", 0.0, 86400.0),
    ("d1_7", "1–7 days", 86400.0, 7 * 86400.0),
    ("d7_30", "7–30 days", 7 * 86400.0, 30 * 86400.0),
    ("d30_90", "30–90 days", 30 * 86400.0, 90 * 86400.0),
    ("d90_365", "90–365 days", 90 * 86400.0, 365 * 86400.0),
    ("ge_365", "1+ year", 365 * 86400.0, None),
)


@dataclasses.dataclass(frozen=True)
class FunnelOptions:
    """Parameters controlling the wanted-gallery funnel report."""

    scope: str = SCOPE_ALL
    baseline: str = BASELINE_CREATE
    overdue_days: int = DEFAULT_OVERDUE_DAYS
    include_paused: bool = False
    created_window: str = WINDOW_ALL
    publisher: str = ""
    category: str = ""
    book_type: str = ""
    reason: str = ""
    provider: str = ""
    overdue_sort: str = OVERDUE_SORT_AGE
    overdue_page: int = 1
    overdue_per_page: int = DEFAULT_OVERDUE_PER_PAGE

    def __post_init__(self) -> None:
        if self.scope not in SCOPE_KEYS:
            raise ValueError(f"Unknown scope: {self.scope!r}")
        if self.baseline not in BASELINE_KEYS:
            raise ValueError(f"Unknown baseline: {self.baseline!r}")
        if not MIN_OVERDUE_DAYS <= self.overdue_days <= MAX_OVERDUE_DAYS:
            raise ValueError(f"overdue_days must be between {MIN_OVERDUE_DAYS} and {MAX_OVERDUE_DAYS}.")
        if self.created_window not in WINDOW_DAYS:
            raise ValueError(f"Unknown created_window: {self.created_window!r}")
        if self.overdue_sort not in OVERDUE_SORT_KEYS:
            raise ValueError(f"Unknown overdue_sort: {self.overdue_sort!r}")
        if self.overdue_page < 1:
            raise ValueError("overdue_page must be >= 1.")
        if not 1 <= self.overdue_per_page <= MAX_OVERDUE_PER_PAGE:
            raise ValueError(f"overdue_per_page must be between 1 and {MAX_OVERDUE_PER_PAGE}.")


def format_duration(seconds: float | None) -> str:
    """Format a duration in seconds into a compact human-readable label (e.g. '4h 12m', '14.2d', '1y 4mo')."""
    if seconds is None:
        return "—"
    sign = "-" if seconds < 0 else ""
    abs_sec = abs(seconds)
    if abs_sec < 60:
        return f"{sign}{int(round(abs_sec))}s"
    if abs_sec < 3600:
        minutes = int(round(abs_sec / 60))
        return f"{sign}{minutes}m"
    if abs_sec < 86400:
        hours = int(abs_sec // 3600)
        rem_mins = int((abs_sec % 3600) // 60)
        return f"{sign}{hours}h {rem_mins}m" if rem_mins else f"{sign}{hours}h"
    days = abs_sec / 86400.0
    if days < 10:
        return f"{sign}{days:.1f}d"
    if days < 365:
        return f"{sign}{int(round(days))}d"
    years = int(days // 365)
    rem_months = int(round((days % 365) / 30.4375))
    if rem_months == 12:
        years += 1
        rem_months = 0
    return f"{sign}{years}y {rem_months}mo" if rem_months else f"{sign}{years}y"


def _percentile(sorted_values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile (matching PostgreSQL PERCENTILE_CONT) on an already-sorted list."""
    n = len(sorted_values)
    if n == 0:
        return None
    if n == 1:
        return sorted_values[0]
    pos = (n - 1) * fraction
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100.0, 1)


def _clean_text(value: str | None) -> str:
    return (value or "").strip()


def _baseline_datetime(
    create_date: datetime.datetime,
    release_date: datetime.datetime | None,
    baseline: str,
) -> datetime.datetime | None:
    if baseline == BASELINE_CREATE:
        return create_date
    if baseline == BASELINE_RELEASE:
        return release_date
    # BASELINE_EFFECTIVE: later of create_date and release_date
    if release_date is not None and release_date > create_date:
        return release_date
    return create_date


def _apply_scope_and_filters(
    queryset: QuerySet[WantedGallery],
    options: FunnelOptions,
    now: datetime.datetime,
) -> QuerySet[WantedGallery]:
    if options.scope == SCOPE_ACTIVE_OR_FOUND:
        queryset = queryset.filter(Q(should_search=True) | Q(found=True))
    elif options.scope == SCOPE_SEARCHING:
        queryset = queryset.filter(should_search=True)
    elif options.scope == SCOPE_ONE_SHOT:
        queryset = queryset.filter(keep_searching=False)
    elif options.scope == SCOPE_RECURRING:
        queryset = queryset.filter(keep_searching=True)

    window_days = WINDOW_DAYS[options.created_window]
    if window_days is not None:
        queryset = queryset.filter(create_date__gte=now - datetime.timedelta(days=window_days))

    if options.publisher:
        if options.publisher == EMPTY_SENTINEL:
            queryset = queryset.filter(Q(publisher__isnull=True) | Q(publisher__regex=r"^\s*$"))
        else:
            # Match trimmed publisher values too (some legacy rows have leading tabs/newlines).
            queryset = queryset.filter(
                Q(publisher=options.publisher)
                | Q(publisher__regex=rf"^\s*{_regex_escape(options.publisher)}\s*$")
            )

    if options.category:
        if options.category == EMPTY_SENTINEL:
            queryset = queryset.filter(Q(category__isnull=True) | Q(category=""), categories__isnull=True)
        else:
            queryset = queryset.filter(
                Q(category=options.category) | Q(categories__name=options.category)
            ).distinct()

    if options.book_type:
        if options.book_type == EMPTY_SENTINEL:
            queryset = queryset.filter(Q(book_type__isnull=True) | Q(book_type=""))
        else:
            queryset = queryset.filter(book_type=options.book_type)

    if options.reason:
        if options.reason == EMPTY_SENTINEL:
            queryset = queryset.filter(Q(reason__isnull=True) | Q(reason=""))
        else:
            queryset = queryset.filter(reason=options.reason)

    if options.provider:
        if options.provider == EMPTY_SENTINEL:
            queryset = queryset.filter(wanted_providers__isnull=True)
        else:
            queryset = queryset.filter(wanted_providers__slug=options.provider).distinct()

    return queryset


def _regex_escape(text: str) -> str:
    """Escape regex metacharacters for PostgreSQL POSIX regex (~)."""
    specials = r"\.^$*+?()[]{}|"
    out: list[str] = []
    for ch in text:
        if ch in specials:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


@dataclasses.dataclass
class _GroupAccumulator:
    key: str
    total: int = 0
    found: int = 0
    searching: int = 0
    paused: int = 0
    upcoming: int = 0
    overdue: int = 0
    archived: int = 0
    with_possible: int = 0
    ttf_seconds: list[float] = dataclasses.field(default_factory=list)
    raw_values: set[str] = dataclasses.field(default_factory=set)

    def to_dict(self, *, exact_filter_value: str | None = None) -> dict[str, Any]:
        sorted_ttf = sorted(self.ttf_seconds)
        median_ttf = _percentile(sorted_ttf, 0.5)
        avg_ttf = (sum(sorted_ttf) / len(sorted_ttf)) if sorted_ttf else None
        active_denom = self.found + self.searching
        return {
            "key": self.key,
            "label": self.key or "(none)",
            "filter_value": exact_filter_value if exact_filter_value is not None else (self.key or EMPTY_SENTINEL),
            "total": self.total,
            "found": self.found,
            "searching": self.searching,
            "paused": self.paused,
            "upcoming": self.upcoming,
            "overdue": self.overdue,
            "archived": self.archived,
            "with_possible": self.with_possible,
            "hit_rate": _pct(self.found, self.total),
            "active_hit_rate": _pct(self.found, active_denom),
            "archive_rate": _pct(self.archived, self.found),
            "ttf_sample_count": len(sorted_ttf),
            "median_ttf_seconds": round(median_ttf, 1) if median_ttf is not None else None,
            "median_ttf_display": format_duration(median_ttf),
            "avg_ttf_seconds": round(avg_ttf, 1) if avg_ttf is not None else None,
            "avg_ttf_display": format_duration(avg_ttf),
        }


def _finalize_breakdown(groups: dict[str, _GroupAccumulator]) -> list[dict[str, Any]]:
    rows = [acc.to_dict() for acc in groups.values()]
    rows.sort(key=lambda r: (-r["total"], r["key"].lower()))
    return rows


def _compute_ttf_summary(durations: list[float], missing_count: int) -> dict[str, Any]:
    sorted_vals = sorted(durations)
    n = len(sorted_vals)
    neg_count = sum(1 for v in sorted_vals if v < 0)

    def _stat(val: float | None) -> dict[str, Any]:
        return {
            "seconds": round(val, 1) if val is not None else None,
            "days": round(val / 86400.0, 2) if val is not None else None,
            "display": format_duration(val),
        }

    buckets: list[dict[str, Any]] = []
    for key, label, lo, hi in TTF_BUCKETS:
        count = 0
        for val in sorted_vals:
            if lo is not None and val < lo:
                continue
            if hi is not None and val >= hi:
                continue
            count += 1
        if key == "lt_0" and count == 0:
            continue
        buckets.append(
            {
                "key": key,
                "label": label,
                "count": count,
                "percent": _pct(count, n) or 0.0,
            }
        )

    mean_val = (sum(sorted_vals) / n) if n else None
    return {
        "sample_count": n,
        "missing_timestamp_count": missing_count,
        "before_baseline_count": neg_count,
        "min": _stat(sorted_vals[0] if n else None),
        "p25": _stat(_percentile(sorted_vals, 0.25)),
        "median": _stat(_percentile(sorted_vals, 0.50)),
        "mean": _stat(mean_val),
        "p75": _stat(_percentile(sorted_vals, 0.75)),
        "p90": _stat(_percentile(sorted_vals, 0.90)),
        "max": _stat(sorted_vals[-1] if n else None),
        "buckets": buckets,
    }


def _collect_filter_choices() -> dict[str, list[dict[str, Any]]]:
    """Distinct filter dropdown values across all WantedGalleries (constant query count)."""
    raw_rows = WantedGallery.objects.values_list("publisher", "category", "book_type", "reason")
    pub_counts: dict[str, int] = defaultdict(int)
    cat_counts: dict[str, int] = defaultdict(int)
    bt_counts: dict[str, int] = defaultdict(int)
    reason_counts: dict[str, int] = defaultdict(int)

    for publisher, category, book_type, reason in raw_rows:
        pub_counts[_clean_text(publisher)] += 1
        if category:
            cat_counts[category] += 1
        else:
            cat_counts[""] += 1
        bt_counts[book_type or ""] += 1
        reason_counts[reason or ""] += 1

    # Include M2M categories in the category choices as well.
    m2m_cats = (
        WantedGallery.categories.through.objects.values("category__name")
        .annotate(c=Count("wantedgallery_id", distinct=True))
    )
    for row in m2m_cats:
        name = row["category__name"]
        if name and name not in cat_counts:
            cat_counts[name] = row["c"]

    provider_rows = (
        WantedGallery.wanted_providers.through.objects.values("provider__slug")
        .annotate(c=Count("wantedgallery_id", distinct=True))
        .order_by("-c", "provider__slug")
    )

    def _to_options(counts: dict[str, int]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [
            {
                "value": k if k else EMPTY_SENTINEL,
                "label": k if k else "(none)",
                "count": c,
            }
            for k, c in counts.items()
            if c > 0
        ]
        items.sort(key=lambda item: (-item["count"], item["label"].lower()))
        return items

    providers = [
        {"value": row["provider__slug"], "label": row["provider__slug"], "count": row["c"]}
        for row in provider_rows
        if row["provider__slug"]
    ]
    unrestricted = WantedGallery.objects.filter(wanted_providers__isnull=True).count()
    if unrestricted:
        providers.append({"value": EMPTY_SENTINEL, "label": "(any provider)", "count": unrestricted})
        providers.sort(key=lambda item: (-item["count"], item["label"].lower()))

    return {
        "publishers": _to_options(pub_counts),
        "categories": _to_options(cat_counts),
        "book_types": _to_options(bt_counts),
        "reasons": _to_options(reason_counts),
        "providers": providers,
    }


def build_wanted_funnel(
    options: FunnelOptions | None = None,
    *,
    now: datetime.datetime | None = None,
    include_filter_choices: bool = True,
) -> dict[str, Any]:
    """Compute the complete wanted-gallery funnel analytics payload."""
    if options is None:
        options = FunnelOptions()
    if now is None:
        now = timezone.now()

    base_qs = _apply_scope_and_filters(WantedGallery.objects.all(), options, now)

    # Query 1: core wanted-gallery fields (no heavy text/M2M joins).
    wg_rows = list(
        base_qs.order_by().values(
            "id",
            "found",
            "should_search",
            "keep_searching",
            "date_found",
            "create_date",
            "release_date",
            "publisher",
            "category",
            "book_type",
            "reason",
            "restricted_to_links",
            "auto_wanted_source_id",
            "auto_wanted_source__name",
        )
    )
    wg_ids = [row["id"] for row in wg_rows]

    # Query 2-6: related aggregates in fixed bulk queries (no N+1).
    first_fg_by_wg: dict[int, datetime.datetime] = {}
    archived_wg_ids: set[int] = set()
    possible_counts: dict[int, int] = {}
    m2m_categories_by_wg: dict[int, list[str]] = defaultdict(list)
    wanted_providers_by_wg: dict[int, list[str]] = defaultdict(list)
    found_provider_rows: list[dict[str, Any]] = []

    if wg_ids:
        id_subquery = base_qs.values("pk")

        first_fg_by_wg = {
            row["wanted_gallery_id"]: row["first_fg"]
            for row in FoundGallery.objects.filter(wanted_gallery_id__in=id_subquery)
            .order_by()
            .values("wanted_gallery_id")
            .annotate(first_fg=Min("create_date"))
        }

        archived_wg_ids = set(
            FoundGallery.objects.filter(
                wanted_gallery_id__in=id_subquery,
                gallery__archive__binned=False,
            )
            .values_list("wanted_gallery_id", flat=True)
            .distinct()
        )

        possible_counts = {
            row["wanted_gallery_id"]: row["cnt"]
            for row in GalleryMatch.objects.filter(wanted_gallery_id__in=id_subquery)
            .order_by()
            .values("wanted_gallery_id")
            .annotate(cnt=Count("id"))
        }

        for wg_id, cat_name in (
            WantedGallery.categories.through.objects.filter(wantedgallery_id__in=id_subquery)
            .values_list("wantedgallery_id", "category__name")
        ):
            if cat_name:
                m2m_categories_by_wg[wg_id].append(cat_name)

        for wg_id, prov_slug in (
            WantedGallery.wanted_providers.through.objects.filter(wantedgallery_id__in=id_subquery)
            .values_list("wantedgallery_id", "provider__slug")
        ):
            if prov_slug:
                wanted_providers_by_wg[wg_id].append(prov_slug)

        found_provider_rows = [
            {
                "provider": row["gallery__provider"] or "(none)",
                "found_entries": row["found_entries"],
                "wanted_galleries": row["wanted_galleries"],
            }
            for row in FoundGallery.objects.filter(wanted_gallery_id__in=id_subquery)
            .order_by()
            .values("gallery__provider")
            .annotate(
                found_entries=Count("id"),
                wanted_galleries=Count("wanted_gallery_id", distinct=True),
            )
            .order_by("-wanted_galleries", "-found_entries", "gallery__provider")
        ]

    threshold_seconds = float(options.overdue_days * 86400)

    # Accumulators
    total = len(wg_rows)
    searched_or_found = 0
    found_count = 0
    archived_count = 0
    searching_unfound = 0
    paused_unfound = 0
    upcoming_unfound = 0
    fresh_searching_unfound = 0
    overdue_active_count = 0
    overdue_paused_count = 0
    overdue_missing_baseline_count = 0
    unfound_with_possible_count = 0
    recurring_count = 0
    one_shot_count = 0
    ttf_missing_timestamp = 0
    all_ttf_seconds: list[float] = []

    by_publisher: dict[str, _GroupAccumulator] = {}
    by_category: dict[str, _GroupAccumulator] = {}
    by_book_type: dict[str, _GroupAccumulator] = {}
    by_reason: dict[str, _GroupAccumulator] = {}
    by_provider: dict[str, _GroupAccumulator] = {}
    by_auto_source: dict[str, _GroupAccumulator] = {}

    # Monthly trend: YYYY-MM -> {created, created_found, first_finds}
    monthly_created: dict[str, int] = defaultdict(int)
    monthly_created_found: dict[str, int] = defaultdict(int)
    monthly_first_finds: dict[str, int] = defaultdict(int)

    # Candidate overdue entries (id + sort keys + computed wait)
    overdue_candidates: list[dict[str, Any]] = []

    def _get_acc(store: dict[str, _GroupAccumulator], key: str) -> _GroupAccumulator:
        acc = store.get(key)
        if acc is None:
            acc = _GroupAccumulator(key=key)
            store[key] = acc
        return acc

    def _record_group(
        acc: _GroupAccumulator,
        *,
        is_found: bool,
        is_searching: bool,
        is_paused: bool,
        is_upcoming: bool,
        is_overdue_for_view: bool,
        is_archived: bool,
        has_possible: bool,
        ttf: float | None,
    ) -> None:
        acc.total += 1
        if is_found:
            acc.found += 1
        if is_searching:
            acc.searching += 1
        if is_paused:
            acc.paused += 1
        if is_upcoming:
            acc.upcoming += 1
        if is_overdue_for_view:
            acc.overdue += 1
        if is_archived:
            acc.archived += 1
        if has_possible:
            acc.with_possible += 1
        if ttf is not None:
            acc.ttf_seconds.append(ttf)

    for row in wg_rows:
        wg_id = row["id"]
        is_found: bool = bool(row["found"])
        should_search: bool = bool(row["should_search"])
        keep_searching: bool = bool(row["keep_searching"])
        create_date: datetime.datetime = row["create_date"]
        release_date: datetime.datetime | None = row["release_date"]
        date_found: datetime.datetime | None = row["date_found"]

        if keep_searching:
            recurring_count += 1
        else:
            one_shot_count += 1

        if should_search or is_found:
            searched_or_found += 1

        possible_cnt = possible_counts.get(wg_id, 0)
        is_archived = is_found and (wg_id in archived_wg_ids)
        if is_archived:
            archived_count += 1

        created_month = timezone.localtime(create_date).strftime("%Y-%m")
        monthly_created[created_month] += 1

        ttf_val: float | None = None
        is_searching_unfound = False
        is_paused_unfound = False
        is_upcoming = False
        is_overdue_for_view = False

        if is_found:
            found_count += 1
            monthly_created_found[created_month] += 1
            first_found_at = first_fg_by_wg.get(wg_id) or date_found
            if first_found_at is not None:
                found_month = timezone.localtime(first_found_at).strftime("%Y-%m")
                monthly_first_finds[found_month] += 1
                start_dt = _baseline_datetime(create_date, release_date, options.baseline)
                if start_dt is not None:
                    ttf_val = (first_found_at - start_dt).total_seconds()
                    all_ttf_seconds.append(ttf_val)
            else:
                ttf_missing_timestamp += 1
        else:
            if possible_cnt > 0:
                unfound_with_possible_count += 1

            is_future_release = release_date is not None and release_date > now
            if should_search:
                searching_unfound += 1
                is_searching_unfound = True
            else:
                paused_unfound += 1
                is_paused_unfound = True

            if is_future_release:
                is_upcoming = True
                if should_search or options.include_paused:
                    upcoming_unfound += 1
            else:
                start_dt = _baseline_datetime(create_date, release_date, options.baseline)
                if start_dt is None:
                    if should_search or options.include_paused:
                        overdue_missing_baseline_count += 1
                else:
                    wait_seconds = (now - start_dt).total_seconds()
                    if wait_seconds >= threshold_seconds:
                        if should_search:
                            overdue_active_count += 1
                        else:
                            overdue_paused_count += 1

                        if should_search or options.include_paused:
                            is_overdue_for_view = True
                            overdue_candidates.append(
                                {
                                    "id": wg_id,
                                    "publisher": _clean_text(row["publisher"]),
                                    "create_date": create_date,
                                    "release_date": release_date,
                                    "baseline_date": start_dt,
                                    "wait_seconds": wait_seconds,
                                    "possible_match_count": possible_cnt,
                                    "should_search": should_search,
                                }
                            )
                    elif should_search:
                        fresh_searching_unfound += 1

        # Record across dimensions
        pub_key = _clean_text(row["publisher"])
        _record_group(
            _get_acc(by_publisher, pub_key),
            is_found=is_found,
            is_searching=is_searching_unfound,
            is_paused=is_paused_unfound,
            is_upcoming=is_upcoming,
            is_overdue_for_view=is_overdue_for_view,
            is_archived=is_archived,
            has_possible=(not is_found and possible_cnt > 0),
            ttf=ttf_val,
        )

        # Effective categories: primary CharField plus any M2M categories (deduplicated per WG).
        cat_keys: list[str] = []
        primary_cat = _clean_text(row["category"])
        if primary_cat:
            cat_keys.append(primary_cat)
        for m2m_cat in m2m_categories_by_wg.get(wg_id, ()):
            if m2m_cat not in cat_keys:
                cat_keys.append(m2m_cat)
        if not cat_keys:
            cat_keys = [""]
        for cat_key in cat_keys:
            _record_group(
                _get_acc(by_category, cat_key),
                is_found=is_found,
                is_searching=is_searching_unfound,
                is_paused=is_paused_unfound,
                is_upcoming=is_upcoming,
                is_overdue_for_view=is_overdue_for_view,
                is_archived=is_archived,
                has_possible=(not is_found and possible_cnt > 0),
                ttf=ttf_val,
            )

        bt_key = _clean_text(row["book_type"])
        _record_group(
            _get_acc(by_book_type, bt_key),
            is_found=is_found,
            is_searching=is_searching_unfound,
            is_paused=is_paused_unfound,
            is_upcoming=is_upcoming,
            is_overdue_for_view=is_overdue_for_view,
            is_archived=is_archived,
            has_possible=(not is_found and possible_cnt > 0),
            ttf=ttf_val,
        )

        reason_key = _clean_text(row["reason"])
        _record_group(
            _get_acc(by_reason, reason_key),
            is_found=is_found,
            is_searching=is_searching_unfound,
            is_paused=is_paused_unfound,
            is_upcoming=is_upcoming,
            is_overdue_for_view=is_overdue_for_view,
            is_archived=is_archived,
            has_possible=(not is_found and possible_cnt > 0),
            ttf=ttf_val,
        )

        prov_keys = wanted_providers_by_wg.get(wg_id) or [""]
        for prov_key in prov_keys:
            _record_group(
                _get_acc(by_provider, prov_key),
                is_found=is_found,
                is_searching=is_searching_unfound,
                is_paused=is_paused_unfound,
                is_upcoming=is_upcoming,
                is_overdue_for_view=is_overdue_for_view,
                is_archived=is_archived,
                has_possible=(not is_found and possible_cnt > 0),
                ttf=ttf_val,
            )

        auto_source_name = row["auto_wanted_source__name"]
        if auto_source_name:
            _record_group(
                _get_acc(by_auto_source, auto_source_name),
                is_found=is_found,
                is_searching=is_searching_unfound,
                is_paused=is_paused_unfound,
                is_upcoming=is_upcoming,
                is_overdue_for_view=is_overdue_for_view,
                is_archived=is_archived,
                has_possible=(not is_found and possible_cnt > 0),
                ttf=ttf_val,
            )

    # Sort and paginate overdue candidates, then hydrate only the current page (1 query).
    overdue_total = len(overdue_candidates)
    overdue_with_possible = sum(1 for item in overdue_candidates if item["possible_match_count"] > 0)

    # For title sorting we need titles; fetch them only when sorting by title.
    if options.overdue_sort == OVERDUE_SORT_TITLE and overdue_candidates:
        candidate_ids = [item["id"] for item in overdue_candidates]
        titles_by_id = dict(
            WantedGallery.objects.filter(id__in=candidate_ids).values_list("id", "title")
        )
        overdue_candidates.sort(key=lambda item: ((titles_by_id.get(item["id"]) or "").lower(), -item["wait_seconds"]))
    elif options.overdue_sort == OVERDUE_SORT_POSSIBLE:
        overdue_candidates.sort(key=lambda item: (-item["possible_match_count"], -item["wait_seconds"]))
    elif options.overdue_sort == OVERDUE_SORT_RELEASE:
        max_dt = datetime.datetime.max.replace(tzinfo=datetime.UTC)
        overdue_candidates.sort(key=lambda item: (item["release_date"] or max_dt, -item["wait_seconds"]))
    elif options.overdue_sort == OVERDUE_SORT_CREATED:
        overdue_candidates.sort(key=lambda item: (item["create_date"], -item["wait_seconds"]))
    elif options.overdue_sort == OVERDUE_SORT_PUBLISHER:
        overdue_candidates.sort(key=lambda item: (item["publisher"].lower(), -item["wait_seconds"]))
    else:
        overdue_candidates.sort(key=lambda item: (-item["wait_seconds"], item["id"]))

    num_pages = max(1, math.ceil(overdue_total / options.overdue_per_page))
    current_page = min(options.overdue_page, num_pages)
    start_idx = (current_page - 1) * options.overdue_per_page
    page_slice = overdue_candidates[start_idx : start_idx + options.overdue_per_page]

    overdue_items: list[dict[str, Any]] = []
    if page_slice:
        page_ids = [item["id"] for item in page_slice]
        details_by_id = {
            obj["id"]: obj
            for obj in WantedGallery.objects.filter(id__in=page_ids).values(
                "id",
                "title",
                "title_jpn",
                "search_title",
                "publisher",
                "category",
                "book_type",
                "reason",
                "should_search",
                "keep_searching",
                "restricted_to_links",
                "release_date",
                "create_date",
            )
        }
        for item in page_slice:
            wg_id = item["id"]
            detail = details_by_id.get(wg_id)
            if detail is None:
                continue
            cats: list[str] = []
            primary_cat = _clean_text(detail["category"])
            if primary_cat:
                cats.append(primary_cat)
            for c in m2m_categories_by_wg.get(wg_id, ()):
                if c not in cats:
                    cats.append(c)
            wait_sec = item["wait_seconds"]
            overdue_items.append(
                {
                    "id": wg_id,
                    "title": detail["title"] or detail["search_title"] or f"Wanted #{wg_id}",
                    "title_jpn": detail["title_jpn"] or "",
                    "publisher": _clean_text(detail["publisher"]),
                    "categories": cats,
                    "category_display": ", ".join(cats) if cats else "",
                    "book_type": _clean_text(detail["book_type"]),
                    "reason": _clean_text(detail["reason"]),
                    "should_search": bool(detail["should_search"]),
                    "keep_searching": bool(detail["keep_searching"]),
                    "restricted_to_links": bool(detail["restricted_to_links"]),
                    "release_date": detail["release_date"],
                    "create_date": detail["create_date"],
                    "baseline_date": item["baseline_date"],
                    "wait_seconds": round(wait_sec, 1),
                    "wait_days": round(wait_sec / 86400.0, 1),
                    "wait_display": format_duration(wait_sec),
                    "possible_match_count": item["possible_match_count"],
                    "wanted_providers": wanted_providers_by_wg.get(wg_id, []),
                    "url": reverse("viewer:wanted-gallery", args=[wg_id]),
                }
            )

    # Monthly trend (sorted chronologically across all months that had creations or first finds)
    all_months = sorted(set(monthly_created.keys()) | set(monthly_first_finds.keys()))
    monthly_trend = [
        {
            "month": month,
            "created": monthly_created.get(month, 0),
            "created_found": monthly_created_found.get(month, 0),
            "cohort_hit_rate": _pct(monthly_created_found.get(month, 0), monthly_created.get(month, 0)),
            "first_finds": monthly_first_finds.get(month, 0),
        }
        for month in all_months
    ]

    # Funnel stages (ordered progression)
    stages = [
        {
            "key": "wanted",
            "label": "Wanted created",
            "count": total,
            "percent_of_total": 100.0 if total else 0.0,
            "step_rate": 100.0 if total else None,
            "description": "All wanted gallery rules in scope",
        },
        {
            "key": "tracked",
            "label": "Searched or found",
            "count": searched_or_found,
            "percent_of_total": _pct(searched_or_found, total) or 0.0,
            "step_rate": _pct(searched_or_found, total),
            "description": "Actively searching or already found (excludes paused unfound)",
        },
        {
            "key": "found",
            "label": "Found (matched)",
            "count": found_count,
            "percent_of_total": _pct(found_count, total) or 0.0,
            "step_rate": _pct(found_count, searched_or_found),
            "description": "Matched to at least one gallery",
        },
        {
            "key": "archived",
            "label": "Archived (downloaded)",
            "count": archived_count,
            "percent_of_total": _pct(archived_count, total) or 0.0,
            "step_rate": _pct(archived_count, found_count),
            "description": "Found wanted galleries with a non-binned local archive",
        },
    ]

    provider_rows = _finalize_breakdown(by_provider)
    for p_row in provider_rows:
        if p_row["key"] == "":
            p_row["label"] = "(any provider)"

    result: dict[str, Any] = {
        "generated_at": now,
        "options": dataclasses.asdict(options),
        "summary": {
            "total": total,
            "searched_or_found": searched_or_found,
            "found": found_count,
            "archived": archived_count,
            "unfound": total - found_count,
            "searching_unfound": searching_unfound,
            "paused_unfound": paused_unfound,
            "upcoming_unfound": upcoming_unfound,
            "fresh_searching_unfound": fresh_searching_unfound,
            "overdue_active": overdue_active_count,
            "overdue_paused": overdue_paused_count,
            "overdue_displayed": overdue_total,
            "unfound_with_possible": unfound_with_possible_count,
            "recurring": recurring_count,
            "one_shot": one_shot_count,
            "hit_rate": _pct(found_count, total),
            "active_hit_rate": _pct(found_count, searched_or_found),
            "archive_rate": _pct(archived_count, found_count),
            "stages": stages,
        },
        "time_to_find": _compute_ttf_summary(all_ttf_seconds, ttf_missing_timestamp),
        "monthly_trend": monthly_trend,
        "breakdowns": {
            "publishers": _finalize_breakdown(by_publisher),
            "categories": _finalize_breakdown(by_category),
            "book_types": _finalize_breakdown(by_book_type),
            "reasons": _finalize_breakdown(by_reason),
            "wanted_providers": provider_rows,
            "found_providers": found_provider_rows,
            "auto_wanted_sources": _finalize_breakdown(by_auto_source),
        },
        "overdue": {
            "threshold_days": options.overdue_days,
            "baseline": options.baseline,
            "include_paused": options.include_paused,
            "total_count": overdue_total,
            "active_count": overdue_active_count,
            "paused_count": overdue_paused_count,
            "upcoming_count": upcoming_unfound,
            "missing_baseline_count": overdue_missing_baseline_count,
            "with_possible_matches_count": overdue_with_possible,
            "sort": options.overdue_sort,
            "page": current_page,
            "num_pages": num_pages,
            "per_page": options.overdue_per_page,
            "has_previous": current_page > 1,
            "has_next": current_page < num_pages,
            "previous_page": current_page - 1 if current_page > 1 else None,
            "next_page": current_page + 1 if current_page < num_pages else None,
            "items": overdue_items,
        },
    }

    if include_filter_choices:
        result["filter_choices"] = _collect_filter_choices()

    return result
