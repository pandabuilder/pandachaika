"""Auto wanted: provider queries (``AutoWantedSource``) that create wanted galleries for new releases.

Runs the sources (the ``auto_wanted`` scheduler and the ``auto_wanted_worker`` task), exports and imports them as
JSON, and converts the old configuration (``wanted_params_<query>_<name>`` provider attributes) into sources.
"""

import dataclasses
import datetime
import logging
import re
import threading
import typing
from collections.abc import Callable, Iterable
from typing import Any

import django.utils.timezone as django_tz
from django.db import transaction
from django.utils.duration import duration_string

from core.base.auto_wanted import ParamError, WantedParam, WantedRunStats, clean_params
from viewer.models import Attribute, AutoWantedSource

if typing.TYPE_CHECKING:
    from core.base.setup import Settings

logger = logging.getLogger(__name__)

TASK_TYPE = "auto_wanted_worker"
EXPORT_VERSION = 1
EXPORT_FIELDS = (
    "name",
    "provider",
    "active",
    "frequency",
    "force_process",
    "reason",
    "public",
    "should_search",
    "keep_searching",
    "notify_when_found",
    "wanted_providers",
    "unwanted_title",
    "regexp_unwanted_title",
    "regexp_unwanted_title_icase",
    "notes",
)
MAX_MESSAGE_ERRORS = 20


def _settings() -> "Settings":
    from django.conf import settings

    return settings.CRAWLER_SETTINGS


# --- Providers ---


def wanted_providers(settings: "Settings | None" = None) -> list[str]:
    """The providers with a wanted generator."""
    return (settings or _settings()).provider_context.wanted_provider_names()


def params_schema(provider: str, settings: "Settings | None" = None) -> list[WantedParam]:
    return (settings or _settings()).provider_context.get_wanted_params(provider)


def clean_source_params(provider: str, params: Any, settings: "Settings | None" = None) -> dict[str, Any]:
    """The cleaned parameters of a source of this provider. Raises ParamError."""
    if provider not in wanted_providers(settings):
        raise ParamError(f"provider {provider!r} has no wanted generator")
    return clean_params(params_schema(provider, settings), params)


# --- Running ---


def run_source(settings: "Settings", source: AutoWantedSource) -> WantedRunStats:
    """Run one source with its provider's wanted generator, and save how it went on the source."""
    stats = WantedRunStats()
    failed = False
    generator = settings.provider_context.get_wanted_generator(source.provider)
    if generator is None:
        stats.error(f"Auto wanted source {source.name}: provider {source.provider} has no wanted generator.")
        failed = True
    else:
        try:
            source.params = clean_source_params(source.provider, source.params, settings)
        except ParamError as e:
            stats.error(f"Auto wanted source {source.name}: invalid parameters: {e}")
            failed = True
    if not failed and generator is not None:
        logger.info("Running auto wanted source: %s (%s)", source.name, source.provider)
        try:
            generator(settings, source, stats)
        except Exception as e:
            logger.exception("Auto wanted source %s failed", source.name)
            stats.errors.append(f"{type(e).__name__}: {e}")
            failed = True
    if failed:
        status = AutoWantedSource.RunStatus.FAILED
    elif stats.errors:
        status = AutoWantedSource.RunStatus.ERRORS
    else:
        status = AutoWantedSource.RunStatus.OK
    message = "\n".join(stats.errors[:MAX_MESSAGE_ERRORS])
    if len(stats.errors) > MAX_MESSAGE_ERRORS:
        message += f"\n… and {len(stats.errors) - MAX_MESSAGE_ERRORS} more"
    if source.pk:
        # An update, so a run doesn't undo changes saved while it ran.
        AutoWantedSource.objects.filter(pk=source.pk).update(
            last_run_at=django_tz.now(),
            last_run_status=status,
            last_run_message=message,
            last_run_links=stats.links,
            last_run_created=stats.created,
            last_run_mentions=stats.mentions,
        )
    logger.info(
        "Auto wanted source %s finished (%s): %d new links, %d wanted galleries created, %d mentions, %d errors.",
        source.name,
        status,
        stats.links,
        stats.created,
        stats.mentions,
        len(stats.errors),
    )
    return stats


def run_sources(
    settings: "Settings",
    sources: Iterable[AutoWantedSource],
    is_cancelled: Callable[[], bool] | None = None,
    activity_cb: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Run the sources one after the other (the ``auto_wanted_worker`` task). Returns the stats of each."""
    results = []
    for source in sources:
        if is_cancelled is not None and is_cancelled():
            break
        if activity_cb is not None:
            activity_cb()
        stats = run_source(settings, source)
        results.append({"id": source.pk, "name": source.name, **stats.as_dict()})
    return {
        "sources": results,
        "created": sum(r["created"] for r in results),
        "mentions": sum(r["mentions"] for r in results),
    }


def default_period(settings: "Settings | None" = None) -> datetime.timedelta:
    """How often the sources without their own frequency run: ``auto_wanted.cycle_timer`` hours."""
    return datetime.timedelta(hours=(settings or _settings()).auto_wanted.cycle_timer)


def is_due(source: AutoWantedSource, default: datetime.timedelta, now: datetime.datetime | None = None) -> bool:
    next_run = source.next_run_at(default)
    return next_run is None or next_run <= (now or django_tz.now())


def seconds_until_next_due(default: datetime.timedelta, now: datetime.datetime | None = None) -> float | None:
    """Seconds until the next active source is due (0 when one is), None when there are no active sources."""
    now = now or django_tz.now()
    waits = []
    for source in AutoWantedSource.objects.filter(active=True).only("frequency", "last_run_at"):
        next_run = source.next_run_at(default)
        waits.append(0.0 if next_run is None else max((next_run - now).total_seconds(), 0.0))
    return min(waits) if waits else None


# Providers whose scheduled sources are running in this process, so a provider's sources never run twice at once
# (a long run is still due on the next check, its last run is saved when it ends).
_running_providers: set[str] = set()
_running_lock = threading.Lock()


def running_providers() -> set[str]:
    with _running_lock:
        return set(_running_providers)


def run_active_sources_in_threads(
    settings: "Settings",
    default: datetime.timedelta | None = None,
    only_due: bool = True,
    thread_name: str = "auto_wanted",
) -> list[threading.Thread]:
    """The scheduler run: the active sources that are due (all of them with ``only_due`` off), in a thread per
    provider, one after the other in it. Providers still running from an earlier check are left for the next one.
    """
    default = default or default_period(settings)
    now = django_tz.now()
    by_provider: dict[str, list[AutoWantedSource]] = {}
    for source in AutoWantedSource.objects.filter(active=True):
        if not only_due or is_due(source, default, now):
            by_provider.setdefault(source.provider, []).append(source)

    threads = []
    for provider, sources in by_provider.items():
        with _running_lock:
            if provider in _running_providers:
                logger.info("Auto wanted: %s sources still running, they're left for the next check.", provider)
                continue
            _running_providers.add(provider)

        def _run(provider_name: str = provider, provider_sources: list[AutoWantedSource] = sources) -> None:
            try:
                for provider_source in provider_sources:
                    run_source(settings, provider_source)
            except BaseException:
                logger.exception("Auto wanted thread failed")
            finally:
                with _running_lock:
                    _running_providers.discard(provider_name)
                try:
                    from django.db import connection, connections

                    if not connection.in_atomic_block:
                        connections.close_all()
                except Exception:
                    pass

        thread = threading.Thread(name=f"{thread_name}-{provider}", target=_run, daemon=True)
        try:
            thread.start()
        except BaseException:
            with _running_lock:
                _running_providers.discard(provider)
            raise
        threads.append(thread)
    return threads


def enqueue_run(sources: Iterable[AutoWantedSource], user: Any = None) -> Any:
    """Queue an ``auto_wanted_worker`` task that runs these sources."""
    from workers import client

    return client.enqueue(
        args=[source.pk for source in sources],
        task_type=TASK_TYPE,
        user=user,
        reason="auto wanted",
    )


# --- Export / import ---


def source_to_dict(source: AutoWantedSource) -> dict[str, Any]:
    data = {field: getattr(source, field) for field in EXPORT_FIELDS}
    # Django's duration format, like the form: "06:00:00", "1 00:00:00". Empty: the scheduler's default.
    data["frequency"] = duration_string(source.frequency) if source.frequency else ""
    data["wanted_providers"] = source.wanted_provider_slugs()
    data["params"] = dict(source.params or {})
    return data


def export_data(sources: Iterable[AutoWantedSource]) -> dict[str, Any]:
    return {"version": EXPORT_VERSION, "sources": [source_to_dict(source) for source in sources]}


@dataclasses.dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: list[str] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)


def parse_import(raw: str) -> list[Any]:
    """The source entries of an export (or of a bare list of them). Raises ValueError."""
    import json

    data = json.loads(raw)
    items = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError('expected {"sources": [...]} or a list of sources')
    return items


def import_sources(items: list[Any], user: Any = None, skip_existing: bool = False) -> ImportResult:
    """Create or update sources by name. Fields left out keep their value (or the default for a new source).

    Every entry is validated like the form; invalid ones are reported and not imported. With ``skip_existing``,
    entries whose name already exists are left alone.
    """
    from viewer.forms import AutoWantedSourceForm

    result = ImportResult()
    with transaction.atomic():
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                result.errors.append("an entry without a name")
                continue
            name = str(item["name"])
            instance = AutoWantedSource.objects.filter(name=name).first()
            if instance is not None and skip_existing:
                result.skipped.append(name)
                continue
            values = {field: item[field] for field in EXPORT_FIELDS if field in item and item[field] is not None}
            if isinstance(values.get("wanted_providers"), (list, tuple)):
                values["wanted_providers"] = ", ".join(str(slug) for slug in values["wanted_providers"])
            base = instance if instance is not None else AutoWantedSource()
            form_data = {field: getattr(base, field) for field in EXPORT_FIELDS} | values
            params = item["params"] if item.get("params") is not None else (base.params or {})
            form = AutoWantedSourceForm(data=form_data, instance=instance, params=params)
            if not form.is_valid():
                messages = "; ".join(str(m) for ms in form.errors.values() for m in ms)
                result.errors.append(f"{name}: {messages}")
                continue
            source = form.save(commit=False)
            if instance is None:
                source.user = user if getattr(user, "is_authenticated", False) else None
                result.created += 1
            else:
                result.updated += 1
            source.save()
    return result


# --- Old configuration: provider attributes ---

LEGACY_NAME = re.compile(
    r"^(?:wanted_(?P<field>params|reason|public|should_search|keep_searching|notify_when_found|providers|provider)"
    r"|(?P<stop>stop_page))_(?P<rest>.+)$"
)


# Providers whose wanted generator moved to another provider: their old queries become sources of the new one.
LEGACY_PROVIDER_RENAMES = {"mugimugi": "doujinshi_mirror"}


def _legacy_provider_slugs(settings: "Settings | None" = None) -> list[str]:
    """The providers with a wanted generator, and the old ones whose queries their generator takes."""
    names = wanted_providers(settings)
    return names + [old for old, new in LEGACY_PROVIDER_RENAMES.items() if new in names and old not in names]


def _old_provider_config(settings: "Settings", provider: str) -> dict[str, Any]:
    """A provider's section in the settings file, also for providers that no longer read it."""
    config = getattr(settings, "config", None)
    providers = config.get("providers") if isinstance(config, dict) else None
    section = providers.get(provider) if isinstance(providers, dict) else None
    return section if isinstance(section, dict) else {}


def legacy_attributes(settings: "Settings | None" = None):
    """The provider attributes of the old auto wanted configuration, of the providers with a wanted generator."""
    names = _legacy_provider_slugs(settings)
    return Attribute.objects.filter(provider__slug__in=names).filter(
        name__regex=r"^(wanted_(params|reason|public|should_search|keep_searching|notify_when_found|providers?)_|"
        r"stop_page_)"
    )


def _legacy_value(attr: Attribute) -> Any:
    value = attr.value
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return value


def legacy_sources(settings: "Settings | None" = None) -> list[dict[str, Any]]:
    """The old configuration as sources, in the export format (``<provider> <query name>``).

    Each query keeps the defaults its generator used when an attribute was missing. It's active when its provider
    is in the ``auto_wanted.providers`` setting, which used to choose the providers that ran.
    """
    settings = settings or _settings()
    enabled = set(settings.auto_wanted.providers or [])
    queries: dict[tuple[str, str], dict[str, Any]] = {}
    force_process: dict[str, bool] = {}

    attrs = list(legacy_attributes(settings).select_related("provider"))
    for attr in Attribute.objects.filter(provider__slug__in=_legacy_provider_slugs(settings), name="force_process"):
        force_process[attr.provider.slug] = bool(attr.value)

    for attr in sorted(attrs, key=lambda a: (a.provider.slug, a.name)):
        match = LEGACY_NAME.match(attr.name)
        if not match:
            continue
        provider = attr.provider.slug
        rest = match.group("rest")
        field = "stop_page" if match.group("stop") else match.group("field")
        if field == "params":
            if "_" not in rest:
                continue
            query_name, param_name = rest.split("_", maxsplit=1)
        else:
            query_name, param_name = rest, ""
        query = queries.setdefault((provider, query_name), {"params": {}, "other": {}})
        if field == "params":
            query["params"][param_name] = _legacy_value(attr)
        elif field == "stop_page":
            query["other"]["stop_page"] = _legacy_value(attr)
        else:
            query["other"][field] = _legacy_value(attr)

    sources = []
    for (provider, query_name), query in queries.items():
        # A query exists when it has parameters; the other attributes alone did nothing.
        if not query["params"]:
            continue
        other = query["other"]
        params = dict(query["params"])
        if "stop_page" in other and provider == "wanimagazine":
            params["stop_page"] = other["stop_page"]
        is_mugimugi = provider == "mugimugi"
        notes = f"Imported from the {provider} provider attributes (query {query_name})."
        if is_mugimugi:
            # The doujinshi.org API is gone: its queries run on the mirror, whose parameters are the JSON API's.
            from core.providers.doujinshi_mirror.legacy import params_from_legacy

            params, dropped = params_from_legacy(params)
            if dropped:
                notes += " Parameters without an equivalent on the mirror, left out: {}.".format(
                    ", ".join(f"{k}={v}" for k, v in sorted(dropped.items()))
                )

        def flag(name: str, default: bool) -> bool:
            value = other.get(name)
            return value if isinstance(value, bool) else default

        reason = other.get("reason")
        wanted_providers_list = []
        for key in ("provider", "providers"):
            value = other.get(key)
            if isinstance(value, str):
                wanted_providers_list.extend(value.split())
        source: dict[str, Any] = {
            "name": f"{provider} {query_name}",
            "provider": LEGACY_PROVIDER_RENAMES.get(provider, provider),
            "active": provider in enabled or LEGACY_PROVIDER_RENAMES.get(provider, provider) in enabled,
            "force_process": force_process.get(provider, False),
            "params": params,
            "reason": reason if isinstance(reason, str) else "",
            "public": flag("public", False),
            # mugimugi searched and kept searching when not set.
            "should_search": flag("should_search", is_mugimugi),
            "keep_searching": flag("keep_searching", is_mugimugi),
            # mugimugi didn't set it: the WantedGallery default (True).
            "notify_when_found": True if is_mugimugi else flag("notify_when_found", False),
            "wanted_providers": list(dict.fromkeys(wanted_providers_list)),
            "notes": notes,
        }
        if is_mugimugi:
            own = _old_provider_config(settings, provider).get("wanted") or {}
            source["unwanted_title"] = own.get("unwanted_title") or settings.auto_wanted.unwanted_title or ""
            source["regexp_unwanted_title"] = bool(
                own.get("regexp_unwanted_title") or settings.auto_wanted.regexp_unwanted_title
            )
            source["regexp_unwanted_title_icase"] = bool(
                own.get("regexp_unwanted_title_icase") or settings.auto_wanted.regexp_unwanted_title_icase
            )
        sources.append(source)
    return sources


def legacy_summary(settings: "Settings | None" = None) -> dict[str, Any]:
    """The old configuration still in the provider attributes, and which of its queries aren't sources yet."""
    sources = legacy_sources(settings)
    existing = set(AutoWantedSource.objects.filter(name__in=[s["name"] for s in sources]).values_list("name", flat=True))
    return {
        "sources": sources,
        "pending": [s for s in sources if s["name"] not in existing],
        "attribute_count": legacy_attributes(settings).count(),
    }
