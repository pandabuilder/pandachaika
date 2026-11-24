"""Stats of a monitored link's runs and captured links, to judge whether its configuration fits the source.

The overlap between runs is the main signal: a capture run should find a few links that an earlier run had already
seen. If it doesn't, and it didn't read until the end of the results, the links published in between were missed
(a gap). If almost everything it finds was already known, it runs more often than the source changes."""

import dataclasses
from collections import Counter
from datetime import timedelta
from typing import Any

import django.utils.timezone as django_tz
from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Max, Min, Q

from viewer.models import MonitoredLink, MonitoredLinkRun, ProcessedLinks

# Runs considered for the aggregated stats and the hints.
ANALYSIS_DAYS = 14
ANALYSIS_MAX_RUNS = 500
# Minimum number of runs before suggesting frequency changes.
MIN_RUNS_FOR_HINTS = 5


@dataclasses.dataclass
class Hint:
    level: str  # Bootstrap contextual class: danger, warning, info, success.
    title: str
    text: str


@dataclasses.dataclass
class CaptureStats:
    runs: int = 0
    pages: int = 0
    found: int = 0
    new: int = 0
    known: int = 0
    zero_new_runs: int = 0
    gap_runs: int = 0
    no_response_runs: int = 0
    error_runs: int = 0
    unfinished_runs: int = 0
    stop_reasons: list[tuple[str, int]] = dataclasses.field(default_factory=list)
    avg_duration: timedelta | None = None
    # New links per hour, measured over the span of the capture runs.
    new_per_hour: float | None = None
    links_per_page: float | None = None
    # How many links a run can read before hitting the stop page, if set.
    run_capacity: int | None = None

    @property
    def overlap_ratio(self) -> float | None:
        return self.known / self.found if self.found else None

    @property
    def avg_new(self) -> float | None:
        return self.new / self.runs if self.runs else None

    @property
    def avg_known(self) -> float | None:
        return self.known / self.runs if self.runs else None

    @property
    def avg_pages(self) -> float | None:
        return self.pages / self.runs if self.runs else None


@dataclasses.dataclass
class ProcessStats:
    runs: int = 0
    idle_runs: int = 0
    to_process: int = 0
    pending_loaded: int = 0
    recent_added: int = 0
    in_database: int = 0
    evaluated: int = 0
    accepted: int = 0
    wanted_matches: int = 0
    banned: int = 0
    api_errors: int = 0
    pending_processed: int = 0
    pending_retried: int = 0
    pending_failed: int = 0
    error_runs: int = 0
    avg_duration: timedelta | None = None


@dataclasses.dataclass
class LinkStats:
    total: int = 0
    pending: int = 0
    processed: int = 0
    failed: int = 0
    oldest_pending: Any = None
    last_captured: Any = None
    captured_last_day: int = 0
    captured_last_week: int = 0
    avg_wait: timedelta | None = None
    max_wait: timedelta | None = None


@dataclasses.dataclass
class Schedule:
    separate: bool
    capture_every: timedelta
    process_every: timedelta
    last_capture: Any
    last_process: Any
    next_capture: Any
    next_process: Any


def effective_stop_on_seen(link: MonitoredLink, crawler_settings: Any = None) -> bool:
    """The stop on seen links value the runs use, resolving "follow global" like the parser does."""
    if link.stop_on_seen_link == MonitoredLink.StopOnSeenChoices.ON:
        return True
    if link.stop_on_seen_link == MonitoredLink.StopOnSeenChoices.OFF:
        return False
    if crawler_settings is None:
        from django.conf import settings

        crawler_settings = getattr(settings, "CRAWLER_SETTINGS", None)
    if crawler_settings is None:
        return False
    providers = getattr(crawler_settings, "providers", None) or {}
    provider_settings = providers.get(link.provider.slug) if link.provider and hasattr(providers, "get") else None
    return bool(
        getattr(provider_settings, "stop_on_seen_links", False) or getattr(crawler_settings, "stop_on_seen_links", False)
    )


def _avg_duration(runs: list[MonitoredLinkRun]) -> timedelta | None:
    durations = [r.duration for r in runs if r.duration is not None]
    if not durations:
        return None
    return sum(durations, timedelta()) / len(durations)


def schedule_for(link: MonitoredLink) -> Schedule:
    separate = link.uses_separate_frequencies
    capture_every = (link.capture_frequency if separate else None) or link.frequency
    scheduler_state = link.get_scheduler_state()
    state_last_run = getattr(scheduler_state, "last_run", None)
    last_capture = link.last_capture_run if separate else state_last_run
    last_process = link.last_process_run if separate else state_last_run
    if separate:
        # Same fallback as the scheduler: a phase that never ran is due on the last combined run's timer.
        capture_base = last_capture or state_last_run
        process_base = last_process or state_last_run
        next_capture = capture_base + capture_every if capture_base else None
        next_process = process_base + link.frequency if process_base else None
    else:
        next_capture = next_process = getattr(scheduler_state, "next_run", None)
    return Schedule(
        separate=separate,
        capture_every=capture_every,
        process_every=link.frequency,
        last_capture=last_capture,
        last_process=last_process,
        next_capture=next_capture,
        next_process=next_process,
    )


def link_stats_for(link: MonitoredLink) -> LinkStats:
    now = django_tz.now()
    links = ProcessedLinks.objects.filter(monitored_link=link)
    counts = links.aggregate(
        total=Count("pk"),
        pending=Count("pk", filter=Q(status=ProcessedLinks.Status.PENDING)),
        processed=Count("pk", filter=Q(status=ProcessedLinks.Status.PROCESSED)),
        failed=Count("pk", filter=Q(status=ProcessedLinks.Status.FAILED)),
        oldest_pending=Min("create_date", filter=Q(status=ProcessedLinks.Status.PENDING)),
        last_captured=Max("create_date"),
        captured_last_day=Count("pk", filter=Q(create_date__gte=now - timedelta(days=1))),
        captured_last_week=Count("pk", filter=Q(create_date__gte=now - timedelta(days=7))),
    )
    # Only links captured in separate frequencies mode have a processed date: the wait for the process run.
    wait = ExpressionWrapper(F("processed_date") - F("create_date"), output_field=DurationField())
    waits = links.filter(
        processed_date__isnull=False, processed_date__gte=now - timedelta(days=ANALYSIS_DAYS)
    ).aggregate(avg_wait=Avg(wait), max_wait=Max(wait))
    return LinkStats(**counts, **waits)


def recent_runs(link: MonitoredLink) -> list[MonitoredLinkRun]:
    """The runs of the analysis window, oldest first."""
    since = django_tz.now() - timedelta(days=ANALYSIS_DAYS)
    runs = list(
        MonitoredLinkRun.objects.filter(monitored_link=link, started__gte=since).order_by("-started")[
            :ANALYSIS_MAX_RUNS
        ]
    )
    runs.reverse()
    return runs


def first_capture_run_id(link: MonitoredLink) -> int | None:
    """The very first capture run finds everything new, by definition, so it can't tell a gap."""
    return (
        MonitoredLinkRun.objects.filter(
            monitored_link=link, kind__in=(MonitoredLinkRun.Kind.CAPTURE, MonitoredLinkRun.Kind.FULL)
        )
        .order_by("started")
        .values_list("pk", flat=True)
        .first()
    )


def is_gap(run: MonitoredLinkRun, first_capture_id: int | None) -> bool:
    return run.possible_gap and run.pk != first_capture_id


def capture_stats_for(
    link: MonitoredLink, runs: list[MonitoredLinkRun], first_capture_id: int | None
) -> CaptureStats:
    capture_runs = [r for r in runs if r.captures]
    stats = CaptureStats(runs=len(capture_runs))
    if not capture_runs:
        return stats
    finished = [r for r in capture_runs if r.finished is not None]
    stats.unfinished_runs = len(capture_runs) - len(finished)
    for run in finished:
        stats.pages += run.pages_crawled
        stats.found += run.links_found
        stats.new += run.links_new
        stats.known += run.links_known
        if run.error:
            stats.error_runs += 1
        elif run.stop_reason == MonitoredLinkRun.StopReason.NO_RESPONSE:
            stats.no_response_runs += 1
        elif run.links_new == 0:
            stats.zero_new_runs += 1
        if is_gap(run, first_capture_id):
            stats.gap_runs += 1
    labels = dict(MonitoredLinkRun.StopReason.choices)
    stats.stop_reasons = [
        (str(labels.get(reason, reason)) if reason else "Unknown", count)
        for reason, count in Counter(r.stop_reason for r in finished).most_common()
    ]
    stats.runs = len(finished)
    stats.avg_duration = _avg_duration(finished)
    if len(finished) >= 2:
        span = finished[-1].started - finished[0].started
        # The first run's new links were published before the span started.
        new_in_span = sum(r.links_new for r in finished[1:])
        if span.total_seconds() > 0:
            stats.new_per_hour = new_in_span / (span.total_seconds() / 3600)
    if stats.pages:
        stats.links_per_page = stats.found / stats.pages
        if link.stop_page is not None:
            # Pages are counted from 0, the stop page itself is read too.
            stats.run_capacity = round(stats.links_per_page * (link.stop_page + 1))
    return stats


def process_stats_for(runs: list[MonitoredLinkRun]) -> ProcessStats:
    process_runs = [r for r in runs if r.processes and r.finished is not None]
    stats = ProcessStats(runs=len(process_runs))
    for run in process_runs:
        stats.to_process += run.links_to_process
        stats.pending_loaded += run.pending_loaded
        stats.recent_added += run.recent_added
        stats.in_database += run.links_in_database
        stats.evaluated += run.links_evaluated
        stats.accepted += run.links_accepted
        stats.wanted_matches += run.wanted_matches
        stats.banned += run.links_banned
        stats.api_errors += run.api_errors
        stats.pending_processed += run.pending_processed
        stats.pending_retried += run.pending_retried
        stats.pending_failed += run.pending_failed
        if run.error:
            stats.error_runs += 1
        if run.links_to_process == 0:
            stats.idle_runs += 1
    stats.avg_duration = _avg_duration(process_runs)
    return stats


def _fmt_interval(interval: timedelta) -> str:
    seconds = int(interval.total_seconds())
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{max(seconds // 60, 1)}m"


def build_hints(
    link: MonitoredLink, schedule: Schedule, capture: CaptureStats, process: ProcessStats, links: LinkStats
) -> list[Hint]:
    hints: list[Hint] = []
    capture_label = "capture frequency" if schedule.separate else "frequency"
    stop_on_seen_off = not effective_stop_on_seen(link)

    if capture.gap_runs:
        text = (
            f"{capture.gap_runs} of {capture.runs} capture runs only found new links and stopped before getting back "
            f"to the ones seen by the previous run, so the links published in between were likely missed."
        )
        if link.stop_page is not None:
            text += f" Run more often than every {_fmt_interval(schedule.capture_every)} or raise the stop page ({link.stop_page})."
        else:
            text += f" Run more often than every {_fmt_interval(schedule.capture_every)}."
        hints.append(Hint("danger", "Possible missed links", text))

    if capture.new_per_hour is not None and capture.run_capacity:
        expected = capture.new_per_hour * schedule.capture_every.total_seconds() / 3600
        if expected > capture.run_capacity * 0.8:
            hints.append(
                Hint(
                    "warning",
                    "Close to the stop page limit",
                    f"About {expected:.0f} new links show up between capture runs, and a run reads up to about "
                    f"{capture.run_capacity} links before the stop page. Bursts can push new links past it.",
                )
            )

    judged = capture.runs - capture.error_runs - capture.no_response_runs
    if judged >= MIN_RUNS_FOR_HINTS and capture.zero_new_runs / judged >= 0.8 and not capture.gap_runs:
        text = (
            f"{capture.zero_new_runs} of {judged} capture runs found nothing new. The {capture_label} "
            f"({_fmt_interval(schedule.capture_every)}) is likely shorter than needed"
        )
        if capture.new_per_hour:
            text += f": about {capture.new_per_hour * 24:.1f} new links per day."
        else:
            text += "."
        hints.append(Hint("info", "Mostly empty capture runs", text))

    if stop_on_seen_off and capture.runs >= MIN_RUNS_FOR_HINTS and (capture.avg_known or 0) >= 25:
        hints.append(
            Hint(
                "info",
                "Re-reading known links",
                f"Each capture run reads {capture.avg_known:.0f} already seen links on average, and stop on seen is "
                f"off. Turning it on would end the runs at the first seen link and save page requests.",
            )
        )

    if schedule.separate and links.pending:
        oldest_age = django_tz.now() - links.oldest_pending if links.oldest_pending else None
        if oldest_age and oldest_age > schedule.process_every * 2 + timedelta(minutes=5):
            hints.append(
                Hint(
                    "warning",
                    "Pending links are waiting",
                    f"{links.pending} captured links are pending, the oldest one for {_fmt_interval(oldest_age)}, "
                    f"more than two process intervals ({_fmt_interval(schedule.process_every)}). Check that the "
                    f"scheduler is running and that the process runs succeed.",
                )
            )

    if process.pending_failed or process.pending_retried:
        hints.append(
            Hint(
                "warning",
                "Links failing to process",
                f"{process.pending_retried} captured links couldn't be processed and were left for the next run, "
                f"{process.pending_failed} were given up on after too many attempts.",
            )
        )

    if (
        schedule.separate
        and process.runs >= MIN_RUNS_FOR_HINTS
        and process.idle_runs / process.runs >= 0.8
        and schedule.process_every < schedule.capture_every
    ):
        hints.append(
            Hint(
                "info",
                "Mostly idle process runs",
                f"{process.idle_runs} of {process.runs} process runs had nothing to process. Processing every "
                f"{_fmt_interval(schedule.process_every)} while capturing every "
                f"{_fmt_interval(schedule.capture_every)} only helps to retry failed links quickly.",
            )
        )

    failed_runs = capture.error_runs + capture.no_response_runs + process.error_runs
    if failed_runs:
        hints.append(
            Hint(
                "warning",
                "Failed runs",
                f"{failed_runs} run{'s' if failed_runs != 1 else ''} failed or got no response from the site. "
                f"See the issues in the runs below.",
            )
        )

    if not hints and capture.runs >= MIN_RUNS_FOR_HINTS:
        hints.append(
            Hint(
                "success",
                "Looks good",
                "Capture runs keep overlapping with the previous ones and find new links regularly.",
            )
        )
    return hints


def monitored_link_report(link: MonitoredLink) -> dict[str, Any]:
    schedule = schedule_for(link)
    runs = recent_runs(link)
    first_capture_id = first_capture_run_id(link)
    capture = capture_stats_for(link, runs, first_capture_id)
    process = process_stats_for(runs)
    links = link_stats_for(link)
    new_per_hour = capture.new_per_hour
    return {
        "new_per_day": new_per_hour * 24 if new_per_hour is not None else None,
        "expected_new_per_capture": (
            new_per_hour * schedule.capture_every.total_seconds() / 3600 if new_per_hour is not None else None
        ),
        "schedule": schedule,
        "capture": capture,
        "process": process,
        "links": links,
        "hints": build_hints(link, schedule, capture, process, links),
        "stop_on_seen": effective_stop_on_seen(link),
        "first_capture_id": first_capture_id,
        "analysis_days": ANALYSIS_DAYS,
        "analysis_runs": len(runs),
    }
