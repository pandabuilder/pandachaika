"""Auto updater: when the metadata of each provider's galleries is refreshed (``AutoUpdaterSchedule``).

A schedule lists the gallery ages (time since the gallery was posted) at which a gallery gets updated. Tags are added
by users, mostly in the first days after a release, so a schedule usually updates often while a gallery is new and
rarely (or never) after that.

Each run takes the galleries that reached one of those ages between the end of the previous run's window and now: for
an age ``a`` and a window ``(start, end]``, the galleries posted in ``(start - a, end - a]``. Consecutive windows
don't overlap, so every gallery is queued once per age, whatever ``queue_every`` is. The galleries are queued as a
``gallery_updater`` task, which uses each provider's rate limited requests.
"""

import dataclasses
import datetime
import json
import logging
import re
import typing
from collections.abc import Iterable
from typing import Any

import django.utils.timezone as django_tz
from django.db.models import Count, DurationField, ExpressionWrapper, F, Q
from django.utils.dateparse import parse_duration

from viewer.models import AutoUpdaterSchedule, Gallery, GalleryChange

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)

EXPORT_VERSION = 1
# A scheduler that was stopped for longer than this only queues the galleries of the last MAX_CATCH_UP, so a long
# downtime doesn't end in one huge update task.
MAX_CATCH_UP = datetime.timedelta(days=1)
MIN_EVERY = datetime.timedelta(minutes=15)
MAX_AGE = datetime.timedelta(days=3650)
MAX_UPDATES = 500
MIN_QUEUE_EVERY = datetime.timedelta(minutes=5)
DEFAULT_QUEUE_EVERY = datetime.timedelta(hours=1)

_UNITS = {"w": 7 * 24 * 3600, "d": 24 * 3600, "h": 3600, "m": 60}
_AGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([wdhm])")


class ScheduleError(ValueError):
    pass


def _settings() -> "Settings":
    from django.conf import settings

    return settings.CRAWLER_SETTINGS


# --- Ages ---


def parse_age(value: Any) -> datetime.timedelta:
    """An age: ``"6h"``, ``"2d"``, ``"1d12h"``, ``"30m"``, ``"1w"``, or a number of hours. Raises ScheduleError."""
    if isinstance(value, datetime.timedelta):
        return value
    if isinstance(value, bool):
        raise ScheduleError(f"not an age: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ScheduleError(f"negative age: {value!r}")
        return datetime.timedelta(hours=value)
    if not isinstance(value, str):
        raise ScheduleError(f"not an age: {value!r}")
    text = value.strip().lower()
    if not text:
        raise ScheduleError("empty age")
    try:
        hours = float(text)
    except ValueError:
        hours = None
    if hours is not None:
        if hours < 0:
            raise ScheduleError(f"negative age: {value!r}")
        return datetime.timedelta(hours=hours)
    if not re.fullmatch(r"(?:\s*\d+(?:\.\d+)?\s*[wdhm])+\s*", text):
        # Also take Django's duration format, as used by the frequency fields (1 00:00:00).
        parsed = parse_duration(text)
        if parsed is None or parsed < datetime.timedelta(0):
            raise ScheduleError(f"not an age: {value!r} (use 6h, 2d, 1d12h, 30m)")
        return parsed
    seconds = sum(float(n) * _UNITS[unit] for n, unit in _AGE_RE.findall(text))
    return datetime.timedelta(seconds=round(seconds))


def format_age(age: datetime.timedelta) -> str:
    """The short form of an age: ``1d12h``, ``6h``, ``30m``, ``0h``."""
    seconds = int(round(age.total_seconds()))
    if seconds <= 0:
        return "0h"
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        count, seconds = divmod(seconds, size)
        if count:
            parts.append(f"{count}{unit}")
    if seconds:
        parts.append(f"{seconds}s")
    return "".join(parts)


# --- Rules ---


@dataclasses.dataclass(frozen=True)
class Rule:
    start: datetime.timedelta
    end: datetime.timedelta
    every: datetime.timedelta | None

    def ages(self) -> list[datetime.timedelta]:
        if self.every is None or self.end == self.start:
            return [self.start]
        ages = []
        age = self.start
        while age <= self.end:
            ages.append(age)
            age += self.every
        return ages

    def as_dict(self) -> dict[str, str]:
        d = {"from": format_age(self.start), "to": format_age(self.end)}
        if self.every is not None and self.end != self.start:
            d["every"] = format_age(self.every)
        return d


def parse_rules(raw: Any) -> list[Rule]:
    """The rules of a schedule, validated. Takes the stored list or its JSON text. Raises ScheduleError."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except ValueError as e:
            raise ScheduleError(f"rules aren't valid JSON: {e}") from e
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScheduleError("rules must be a list")
    rules = []
    total = 0
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ScheduleError(f"rule {position} must be an object with from, to and every")
        unknown = set(item) - {"from", "to", "every", "at"}
        if unknown:
            raise ScheduleError(f"rule {position}: unknown keys {', '.join(sorted(unknown))}")
        try:
            if "at" in item:
                start = end = parse_age(item["at"])
            else:
                if "from" not in item:
                    raise ScheduleError("missing 'from'")
                start = parse_age(item["from"])
                end = parse_age(item["to"]) if item.get("to") not in (None, "") else start
            every_raw = item.get("every")
            every = parse_age(every_raw) if every_raw not in (None, "") and end != start else None
        except ScheduleError as e:
            raise ScheduleError(f"rule {position}: {e}") from e
        if end < start:
            raise ScheduleError(f"rule {position}: 'to' ({format_age(end)}) is before 'from' ({format_age(start)})")
        if end > MAX_AGE:
            raise ScheduleError(f"rule {position}: ages go up to {MAX_AGE.days} days")
        if end != start:
            if every is None:
                raise ScheduleError(f"rule {position}: 'every' is needed when 'to' is after 'from'")
            if every < MIN_EVERY:
                raise ScheduleError(f"rule {position}: 'every' must be at least {format_age(MIN_EVERY)}")
        rule = Rule(start, end, every)
        total += len(rule.ages())
        if total > MAX_UPDATES:
            raise ScheduleError(f"more than {MAX_UPDATES} updates per gallery, use a longer 'every'")
        rules.append(rule)
    return rules


def clean_rules(raw: Any) -> list[dict[str, str]]:
    """The rules in their stored form (short ages). Raises ScheduleError."""
    return [rule.as_dict() for rule in parse_rules(raw)]


def update_ages(rules: Iterable[Rule]) -> list[datetime.timedelta]:
    """Every age a gallery is updated at, sorted, without repeats."""
    return sorted({age for rule in rules for age in rule.ages()})


def schedule_ages(schedule: AutoUpdaterSchedule) -> list[datetime.timedelta]:
    try:
        return update_ages(parse_rules(schedule.rules))
    except ScheduleError:
        logger.warning("Auto updater schedule %s has invalid rules: %r", schedule.provider, schedule.rules)
        return []


def summarize(ages: list[datetime.timedelta]) -> str:
    """``6 updates, 1d to 6d``."""
    if not ages:
        return "no updates"
    count = f"{len(ages)} update{'s' if len(ages) != 1 else ''}"
    if len(ages) == 1:
        return f"{count}, at {format_age(ages[0])}"
    return f"{count}, {format_age(ages[0])} to {format_age(ages[-1])}"


# --- Galleries to update ---


def posted_ranges(
    ages: Iterable[datetime.timedelta], start: datetime.datetime, end: datetime.datetime
) -> list[tuple[datetime.datetime, datetime.datetime]]:
    """The posted date ranges ``(low, high]`` of the galleries that reach one of the ages in ``(start, end]``.

    Overlapping ranges (ages closer than the window length) are merged.
    """
    ranges = sorted((start - age, end - age) for age in ages)
    merged: list[tuple[datetime.datetime, datetime.datetime]] = []
    for low, high in ranges:
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return merged


def galleries_reaching(
    provider: str, ages: Iterable[datetime.timedelta], start: datetime.datetime, end: datetime.datetime
):
    """The provider's galleries that reach one of the ages in ``(start, end]``."""
    ranges = posted_ranges(ages, start, end)
    if not ranges or end <= start:
        return Gallery.objects.none()
    condition = Q()
    for low, high in ranges:
        condition |= Q(posted__gt=low, posted__lte=high)
    return Gallery.objects.eligible_for_use(provider=provider).filter(condition)


def galleries_in_range(schedule: AutoUpdaterSchedule, now: datetime.datetime | None = None):
    """The galleries currently between the schedule's first and last update age."""
    ages = schedule_ages(schedule)
    if not ages:
        return Gallery.objects.none()
    now = now or django_tz.now()
    return Gallery.objects.eligible_for_use(
        provider=schedule.provider, posted__gte=now - ages[-1], posted__lte=now - ages[0]
    )


# --- Running ---


@dataclasses.dataclass
class RunResult:
    provider: str
    queued: int = 0
    task_id: int | None = None
    window_start: datetime.datetime | None = None
    window_end: datetime.datetime | None = None
    skipped: str = ""
    error: str = ""


def is_due(schedule: AutoUpdaterSchedule, now: datetime.datetime | None = None) -> bool:
    next_run = schedule.next_run_at()
    return next_run is None or next_run <= (now or django_tz.now())


def window_start(schedule: AutoUpdaterSchedule, now: datetime.datetime) -> datetime.datetime:
    """Where the next window starts: the previous one's end, or one ``queue_every`` back for the first run."""
    start = schedule.last_run_at if schedule.last_run_at is not None else now - schedule.queue_every
    return max(start, now - MAX_CATCH_UP)


def _queue(provider: str, galleries, reason: str, user: Any = None) -> tuple[int, int | None]:
    from workers.client import enqueue_gallery_update

    links = [gallery.get_link() for gallery in galleries.order_by("posted").iterator()]
    if not links:
        return 0, None
    task = enqueue_gallery_update(links, user=user, reason=reason)
    return len(links), getattr(task, "pk", None)


def run_schedule(
    schedule: AutoUpdaterSchedule, now: datetime.datetime | None = None, user: Any = None
) -> RunResult:
    """Queue the galleries that reached an update age since the previous run, and move the window to now.

    The window is claimed with a conditional update, so two runs at the same time (the scheduler and "Run now")
    don't queue the same galleries.
    """
    now = now or django_tz.now()
    result = RunResult(provider=schedule.provider)
    previous = schedule.last_run_at
    start = window_start(schedule, now)
    result.window_start, result.window_end = start, now
    if start >= now:
        result.skipped = "nothing new since the last run"
        return result
    claimed = (
        AutoUpdaterSchedule.objects.filter(pk=schedule.pk, last_run_at=previous)
        if previous is not None
        else AutoUpdaterSchedule.objects.filter(pk=schedule.pk, last_run_at__isnull=True)
    ).update(last_run_at=now)
    if not claimed:
        result.skipped = "another run took this window"
        return result
    schedule.last_run_at = now
    try:
        ages = update_ages(parse_rules(schedule.rules))
        galleries = galleries_reaching(schedule.provider, ages, start, now)
        result.queued, result.task_id = _queue(
            schedule.provider, galleries, f"Auto updater ({schedule.provider})", user=user
        )
    except Exception as e:
        logger.exception("Auto updater schedule %s failed", schedule.provider)
        result.error = f"{type(e).__name__}: {e}"
        # Give the window back, so the next run queues these galleries.
        AutoUpdaterSchedule.objects.filter(pk=schedule.pk, last_run_at=now).update(
            last_run_at=previous, last_run_status=AutoUpdaterSchedule.RunStatus.FAILED, last_run_message=result.error
        )
        schedule.last_run_at = previous
        return result
    AutoUpdaterSchedule.objects.filter(pk=schedule.pk).update(
        last_run_status=AutoUpdaterSchedule.RunStatus.OK,
        last_run_message="",
        last_run_queued=result.queued,
        last_run_task_id=result.task_id,
    )
    if result.queued:
        logger.info(
            "Auto updater %s: queued %d galleries reaching an update age between %s and %s (task #%s).",
            schedule.provider,
            result.queued,
            start,
            now,
            result.task_id,
        )
    else:
        logger.info("Auto updater %s: no galleries reached an update age between %s and %s.", schedule.provider, start, now)
    return result


def run_due(settings: "Settings", now: datetime.datetime | None = None, force: bool = False) -> list[RunResult]:
    """Run the active schedules that are due (every active one when forced), skipping unknown providers."""
    now = now or django_tz.now()
    results = []
    for schedule in AutoUpdaterSchedule.objects.filter(active=True):
        if schedule.provider not in settings.providers:
            logger.warning("Auto updater schedule for unknown provider %s, skipping it.", schedule.provider)
            continue
        if not force and not is_due(schedule, now):
            continue
        results.append(run_schedule(schedule, now=now))
    return results


def seconds_until_next_due(
    now: datetime.datetime | None = None, providers: Iterable[str] | None = None
) -> float | None:
    """Seconds until the next active schedule is due (0 when one is), None without active schedules.

    Only the schedules of ``providers``, when given (the scheduler skips the unknown ones).
    """
    now = now or django_tz.now()
    soonest: float | None = None
    schedules = AutoUpdaterSchedule.objects.filter(active=True)
    if providers is not None:
        schedules = schedules.filter(provider__in=list(providers))
    for schedule in schedules.only("last_run_at", "queue_every"):
        next_run = schedule.next_run_at()
        seconds = 0.0 if next_run is None else max((next_run - now).total_seconds(), 0.0)
        soonest = seconds if soonest is None else min(soonest, seconds)
    return soonest


def queue_whole_range(schedule: AutoUpdaterSchedule, user: Any = None) -> RunResult:
    """Queue every gallery between the first and last update age now, without moving the window."""
    now = django_tz.now()
    result = RunResult(provider=schedule.provider)
    result.queued, result.task_id = _queue(
        schedule.provider, galleries_in_range(schedule, now), f"Auto updater ({schedule.provider}, whole range)", user
    )
    return result


# --- Providers ---


def updatable_providers(settings: "Settings | None" = None) -> list[str]:
    """The providers whose galleries can be updated by link."""
    settings = settings or _settings()
    names = []
    for parser in settings.provider_context.get_parsers(settings):
        try:
            if parser.supports_gallery_update() and parser.name not in names:
                names.append(parser.name)
        except Exception:
            continue
    return sorted(names)


# --- Statistics for the editor ---

# Age buckets of the changes chart, in hours.
CHANGE_BUCKETS_HOURS = (0, 1, 3, 6, 12, 24, 48, 72, 120, 168, 240, 336, 504, 720, 1440, 2160, 4320, 8760)
CHANGE_STATS_DAYS = 90


def galleries_per_day(provider: str, days: int = 30, now: datetime.datetime | None = None) -> float:
    """How many of the provider's galleries were posted per day, over the last ``days`` (skipping the last day,
    still being crawled)."""
    now = now or django_tz.now()
    count = Gallery.objects.eligible_for_use(
        provider=provider, posted__gte=now - datetime.timedelta(days=days + 1), posted__lt=now - datetime.timedelta(days=1)
    ).count()
    return count / days


def change_stats(provider: str, days: int = CHANGE_STATS_DAYS, now: datetime.datetime | None = None) -> dict[str, Any]:
    """The changes recorded in the gallery change log over the last ``days``, by gallery age when they were found.

    Changes are found by updates, so these follow the schedule they ran with: an age without updates finds nothing.
    """
    now = now or django_tz.now()
    edges = [datetime.timedelta(hours=h) for h in CHANGE_BUCKETS_HOURS]
    rows = (
        GalleryChange.objects.filter(
            changed_at__gte=now - datetime.timedelta(days=days),
            gallery__provider=provider,
            gallery__posted__isnull=False,
        )
        .annotate(age=ExpressionWrapper(F("changed_at") - F("gallery__posted"), output_field=DurationField()))
        .filter(age__gte=datetime.timedelta(0))
    )
    tags = ~Q(tags_added=[]) | ~Q(tags_removed=[])
    aggregates = {}
    for i, low in enumerate(edges):
        bucket = Q(age__gte=low)
        if i + 1 < len(edges):
            bucket &= Q(age__lt=edges[i + 1])
        aggregates[f"t{i}"] = Count("id", filter=bucket & tags)
        aggregates[f"o{i}"] = Count("id", filter=bucket & ~tags)
    counts = rows.aggregate(**aggregates)
    buckets: list[dict[str, Any]] = []
    total = 0
    for i, hours in enumerate(CHANGE_BUCKETS_HOURS):
        buckets.append(
            {
                "from": hours,
                "to": CHANGE_BUCKETS_HOURS[i + 1] if i + 1 < len(CHANGE_BUCKETS_HOURS) else None,
                "tags": counts[f"t{i}"] or 0,
                "other": counts[f"o{i}"] or 0,
            }
        )
        total += buckets[-1]["tags"] + buckets[-1]["other"]
    return {"days": days, "buckets": buckets, "total": total}


# --- Presets and the old settings ---

PRESETS: list[dict[str, Any]] = [
    {
        "key": "release",
        "name": "Close to release",
        "description": "Every 6h the first day, every 12h until day 3, daily until a week, then at 10, 14 and 30 days.",
        "rules": [
            {"from": "6h", "to": "1d", "every": "6h"},
            {"from": "1d12h", "to": "3d", "every": "12h"},
            {"from": "4d", "to": "7d", "every": "1d"},
            {"from": "10d", "to": "10d"},
            {"from": "14d", "to": "14d"},
            {"from": "30d", "to": "30d"},
        ],
    },
    {
        "key": "light",
        "name": "Light",
        "description": "Four updates: 1 day, 3 days, a week and a month after release.",
        "rules": [
            {"from": "1d", "to": "1d"},
            {"from": "3d", "to": "3d"},
            {"from": "7d", "to": "7d"},
            {"from": "30d", "to": "30d"},
        ],
    },
    {
        "key": "first_week",
        "name": "First week, daily",
        "description": "Once a day for the first week.",
        "rules": [{"from": "1d", "to": "7d", "every": "1d"}],
    },
]


def legacy_rules(settings: "Settings", provider: str) -> list[dict[str, str]]:
    """The rules equivalent to the old settings of a provider (``autoupdater.cycle_timer``, ``buffer_back`` and
    ``buffer_after``, in days, or the provider's ``autoupdater_*`` overrides).

    The old updater ran every ``cycle_timer`` days on the galleries posted between ``cycle_timer + buffer_back`` and
    ``buffer_after`` days ago: an update every ``cycle_timer`` days from ``buffer_after`` to ``cycle_timer +
    buffer_back`` days of age.
    """
    provider_settings = settings.providers.get(provider) if provider in settings.providers else None
    cycle = float(getattr(provider_settings, "autoupdater_timer", settings.autoupdater.cycle_timer))
    back = float(getattr(provider_settings, "autoupdater_buffer_back", settings.autoupdater.buffer_back))
    after = float(getattr(provider_settings, "autoupdater_buffer_after", settings.autoupdater.buffer_after))
    if cycle <= 0:
        cycle = 1
    start = datetime.timedelta(days=max(after, 0))
    end = max(start, datetime.timedelta(days=cycle + back))
    every = max(datetime.timedelta(days=cycle), MIN_EVERY)
    return clean_rules([{"from": format_age(start), "to": format_age(end), "every": format_age(every)}])


def legacy_schedules(settings: "Settings | None" = None) -> list[dict[str, Any]]:
    """The old settings (``autoupdater.providers``) as schedules, in the export format."""
    settings = settings or _settings()
    schedules = []
    for provider in settings.autoupdater.providers:
        if provider not in settings.providers:
            continue
        provider_settings = settings.providers[provider]
        schedules.append(
            {
                "provider": provider,
                "active": bool(getattr(provider_settings, "autoupdater_enable", settings.autoupdater.enable)),
                "queue_every": format_age(DEFAULT_QUEUE_EVERY),
                "rules": legacy_rules(settings, provider),
                "notes": "Imported from the autoupdater settings.",
            }
        )
    return schedules


def legacy_pending(settings: "Settings | None" = None) -> list[dict[str, Any]]:
    """The old settings' providers that don't have a schedule yet."""
    existing = set(AutoUpdaterSchedule.objects.values_list("provider", flat=True))
    return [s for s in legacy_schedules(settings) if s["provider"] not in existing]


# --- Export / import ---


def schedule_to_dict(schedule: AutoUpdaterSchedule) -> dict[str, Any]:
    return {
        "provider": schedule.provider,
        "active": schedule.active,
        "queue_every": format_age(schedule.queue_every),
        "rules": list(schedule.rules or []),
        "notes": schedule.notes,
    }


def export_data(schedules: Iterable[AutoUpdaterSchedule]) -> dict[str, Any]:
    return {"version": EXPORT_VERSION, "schedules": [schedule_to_dict(s) for s in schedules]}


def parse_import(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("schedules", [])
    if not isinstance(data, list):
        raise ValueError("expected a list of schedules, or an object with 'schedules'")
    return data


@dataclasses.dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


def import_schedules(items: Iterable[Any], user: Any = None, skip_existing: bool = False) -> ImportResult:
    """Create or update schedules by provider. Fields left out keep their value (or the default)."""
    result = ImportResult()
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not isinstance(item.get("provider"), str) or not item["provider"].strip():
            result.errors.append(f"schedule {position}: needs a provider")
            continue
        provider = item["provider"].strip()
        unknown = set(item) - {"provider", "active", "queue_every", "rules", "notes"}
        if unknown:
            result.errors.append(f"{provider}: unknown fields {', '.join(sorted(unknown))}")
            continue
        schedule = AutoUpdaterSchedule.objects.filter(provider=provider).first()
        if schedule is not None and skip_existing:
            result.skipped.append(provider)
            continue
        is_new = schedule is None
        if schedule is None:
            schedule = AutoUpdaterSchedule(provider=provider, user=user if getattr(user, "pk", None) else None)
        try:
            if "rules" in item:
                schedule.rules = clean_rules(item["rules"])
            if "queue_every" in item:
                schedule.queue_every = clean_queue_every(item["queue_every"])
        except ScheduleError as e:
            result.errors.append(f"{provider}: {e}")
            continue
        if "active" in item:
            schedule.active = bool(item["active"])
        if "notes" in item:
            schedule.notes = str(item["notes"] or "")
        schedule.save()
        if is_new:
            result.created += 1
        else:
            result.updated += 1
    return result


def clean_queue_every(value: Any) -> datetime.timedelta:
    if value in (None, ""):
        return DEFAULT_QUEUE_EVERY
    every = parse_age(value)
    if every < MIN_QUEUE_EVERY:
        raise ScheduleError(f"queue_every must be at least {format_age(MIN_QUEUE_EVERY)}")
    return every
