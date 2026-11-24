"""Shared pieces of the provider wanted generators (auto wanted).

A provider takes part in auto wanted with a ``wanted`` submodule that has:

- ``WANTED_PARAMS``: the query parameters its sources take (a list of ``WantedParam``), shown as form fields and
  validated on save and import.
- ``wanted_generator(settings, source, stats)``: runs one ``AutoWantedSource``, whose ``params`` are already cleaned
  with ``WANTED_PARAMS``, and counts what it did in ``stats``.
"""

import dataclasses
import datetime
import logging
import typing
from typing import Any

if typing.TYPE_CHECKING:
    from viewer.models import AutoWantedSource, WantedGallery

logger = logging.getLogger(__name__)

PARAM_KINDS = ("text", "bool", "int", "choice", "date")
FALSE_STRINGS = {"", "0", "false", "no", "off", "n", "none"}


@dataclasses.dataclass(frozen=True)
class WantedParam:
    """A query parameter of a provider's auto wanted sources."""

    name: str
    label: str
    kind: str = "text"
    help: str = ""
    required: bool = False
    choices: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "kind": self.kind,
            "help": self.help,
            "required": self.required,
            "choices": [list(c) for c in self.choices],
        }


class ParamError(ValueError):
    pass


def coerce_param(param: WantedParam, value: Any) -> Any:
    """The value of a parameter in its type, or None when it's empty (not set). Raises ParamError."""
    if value is None:
        return None
    if param.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() not in FALSE_STRINGS
        return bool(value)
    if param.kind == "int":
        if isinstance(value, bool):
            raise ParamError(f"{param.name}: expected a number")
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ParamError(f"{param.name}: expected a number, got {value!r}")
    if isinstance(value, (datetime.date, datetime.datetime)):
        value = value.strftime("%Y-%m-%d")
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ParamError(f"{param.name}: expected text, got {value!r}")
    text = str(value).strip()
    if not text:
        return None
    if param.kind == "choice" and param.choices and text not in {c[0] for c in param.choices}:
        raise ParamError(f"{param.name}: {text!r} is not one of {', '.join(c[0] for c in param.choices if c[0])}")
    if param.kind == "date":
        try:
            datetime.date.fromisoformat(text)
        except ValueError:
            raise ParamError(f"{param.name}: expected a date (yyyy-mm-dd), got {text!r}")
    return text


def clean_params(schema: list[WantedParam], params: Any) -> dict[str, Any]:
    """Validate the parameters of a source with its provider's schema. Empty values are left out.

    Raises ParamError with every problem found: unknown names, wrong types, missing required parameters.
    """
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ParamError("the parameters must be an object (name: value)")
    by_name = {p.name: p for p in schema}
    errors = []
    cleaned: dict[str, Any] = {}
    unknown = [name for name in params if name not in by_name]
    if unknown:
        allowed = ", ".join(by_name) or "none"
        errors.append(f"unknown parameter(s): {', '.join(sorted(unknown))} (allowed: {allowed})")
    for param in schema:
        try:
            value = coerce_param(param, params.get(param.name))
        except ParamError as e:
            errors.append(str(e))
            continue
        if value is None or (param.kind == "bool" and value is False):
            if param.required:
                errors.append(f"{param.name}: required")
            continue
        cleaned[param.name] = value
    if errors:
        raise ParamError("; ".join(errors))
    return cleaned


@dataclasses.dataclass
class WantedRunStats:
    """What a run of a source did."""

    links: int = 0
    created: int = 0
    mentions: int = 0
    errors: list[str] = dataclasses.field(default_factory=list)

    def error(self, message: str, log: logging.Logger = logger) -> None:
        log.error(message)
        self.errors.append(message)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def create_wanted_gallery(
    source: "AutoWantedSource", stats: WantedRunStats | None = None, **values: Any
) -> "WantedGallery":
    """Create a wanted gallery found by a source, with the source's values (reason, search flags, providers...).

    ``values`` are the WantedGallery fields from the provider (title, search_title, book_type...).
    """
    from viewer.models import Provider, WantedGallery

    wanted_gallery = WantedGallery.objects.create(
        reason=source.reason,
        public=source.public,
        should_search=source.should_search,
        keep_searching=source.keep_searching,
        notify_when_found=source.notify_when_found,
        unwanted_title=source.unwanted_title,
        regexp_unwanted_title=source.regexp_unwanted_title,
        regexp_unwanted_title_icase=source.regexp_unwanted_title_icase,
        auto_wanted_source=source if source.pk else None,
        **values,
    )
    slugs = source.wanted_provider_slugs()
    if slugs:
        wanted_gallery.wanted_providers.add(*Provider.objects.filter(slug__in=slugs))
    if stats is not None:
        stats.created += 1
    return wanted_gallery
