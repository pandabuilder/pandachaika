"""Wanted-gallery funnel view (HTML dashboard + ``?format=json``)."""

from __future__ import annotations

import typing
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse

from viewer.models import UserLongLivedToken
from viewer.services.wanted_funnel import (
    BASELINE_CHOICES,
    BASELINE_CREATE,
    DEFAULT_OVERDUE_DAYS,
    DEFAULT_OVERDUE_PER_PAGE,
    EMPTY_SENTINEL,
    MAX_OVERDUE_DAYS,
    MAX_OVERDUE_PER_PAGE,
    MIN_OVERDUE_DAYS,
    OVERDUE_DAY_PRESETS,
    OVERDUE_SORT_AGE,
    OVERDUE_SORT_CHOICES,
    SCOPE_ALL,
    SCOPE_CHOICES,
    WINDOW_ALL,
    WINDOW_PRESETS,
    FunnelOptions,
    build_wanted_funnel,
)
from viewer.utils.requests import double_check_auth
from viewer.views.head import render_error


class FunnelParamsError(ValueError):
    pass


def _can_view_funnel(user: typing.Any) -> bool:
    return bool(
        user is not None
        and user.is_authenticated
        and (
            user.is_staff
            or user.has_perm("viewer.read_private_stats")
            or user.has_perm("viewer.view_wantedgallery")
        )
    )


def _parse_bool(raw: str | None) -> bool:
    if not raw:
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_int(raw: str | None, *, default: int, name: str) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError as exc:
        raise FunnelParamsError(f"Invalid integer for '{name}': {text!r}") from exc


def parse_funnel_params(params: typing.Mapping[str, str]) -> FunnelOptions:
    scope = (params.get("scope") or "").strip() or SCOPE_ALL
    baseline = (params.get("baseline") or "").strip() or BASELINE_CREATE
    created_window = (params.get("created_window") or "").strip() or WINDOW_ALL
    overdue_sort = (params.get("overdue_sort") or "").strip() or OVERDUE_SORT_AGE
    overdue_days = _parse_int(params.get("overdue_days"), default=DEFAULT_OVERDUE_DAYS, name="overdue_days")
    overdue_page = _parse_int(params.get("overdue_page"), default=1, name="overdue_page")
    overdue_per_page = _parse_int(
        params.get("overdue_per_page"), default=DEFAULT_OVERDUE_PER_PAGE, name="overdue_per_page"
    )
    include_paused = _parse_bool(params.get("include_paused"))

    try:
        return FunnelOptions(
            scope=scope,
            baseline=baseline,
            overdue_days=overdue_days,
            include_paused=include_paused,
            created_window=created_window,
            publisher=(params.get("publisher") or "").strip(),
            category=(params.get("category") or "").strip(),
            book_type=(params.get("book_type") or "").strip(),
            reason=(params.get("reason") or "").strip(),
            provider=(params.get("provider") or "").strip(),
            overdue_sort=overdue_sort,
            overdue_page=overdue_page,
            overdue_per_page=overdue_per_page,
        )
    except ValueError as exc:
        raise FunnelParamsError(str(exc)) from exc


def _query_without(request: HttpRequest, *drop_keys: str) -> str:
    query = request.GET.copy()
    for key in drop_keys:
        query.pop(key, None)
    return query.urlencode()


def _query_with(request: HttpRequest, **updates: str | int | None) -> str:
    query = request.GET.copy()
    query.pop("format", None)
    for key, value in updates.items():
        if value is None or value == "":
            query.pop(key, None)
        else:
            query[key] = str(value)
    return query.urlencode()


def _build_active_filters(request: HttpRequest, options: FunnelOptions) -> list[dict[str, str]]:
    """Active filter chips with one-click remove links."""
    scope_labels = dict(SCOPE_CHOICES)
    baseline_labels = dict(BASELINE_CHOICES)
    window_labels = {key: label for key, label, _ in WINDOW_PRESETS}

    chips: list[dict[str, str]] = []
    base_url = reverse("viewer:stats-wanted")

    def _add(param: str, label: str, value: str) -> None:
        qs = _query_with(request, **{param: None, "overdue_page": None})
        chips.append(
            {
                "label": label,
                "value": "(none)" if value == EMPTY_SENTINEL else value,
                "remove_url": f"{base_url}?{qs}" if qs else base_url,
            }
        )

    if options.scope != SCOPE_ALL:
        _add("scope", "Scope", scope_labels.get(options.scope, options.scope))
    if options.created_window != WINDOW_ALL:
        _add("created_window", "Window", window_labels.get(options.created_window, options.created_window))
    if options.baseline != BASELINE_CREATE:
        _add("baseline", "Baseline", baseline_labels.get(options.baseline, options.baseline))
    if options.overdue_days != DEFAULT_OVERDUE_DAYS:
        _add("overdue_days", "Overdue", f">= {options.overdue_days}d")
    if options.include_paused:
        _add("include_paused", "Paused", "included in overdue")
    if options.publisher:
        _add("publisher", "Publisher", options.publisher)
    if options.category:
        _add("category", "Category", options.category)
    if options.book_type:
        _add("book_type", "Book type", options.book_type)
    if options.reason:
        _add("reason", "Reason", options.reason)
    if options.provider:
        _add(
            "provider",
            "Provider",
            "(any provider)" if options.provider == EMPTY_SENTINEL else options.provider,
        )
    return chips


def _wanted_list_url(options: FunnelOptions, *, unfound_active: bool = False) -> str:
    """Build a link to ``/wanted-galleries/`` carrying compatible filters."""
    params: dict[str, str] = {}
    if unfound_active:
        params["wanted-not-found"] = "1"
        if not options.include_paused:
            params["wanted-should-search"] = "1"
    if options.publisher and options.publisher != EMPTY_SENTINEL:
        params["publisher"] = options.publisher
    if options.book_type and options.book_type != EMPTY_SENTINEL:
        params["book_type"] = options.book_type
    if options.reason and options.reason != EMPTY_SENTINEL:
        params["reason"] = options.reason
    if options.provider and options.provider != EMPTY_SENTINEL:
        params["provider"] = options.provider
    base = reverse("viewer:wanted-galleries")
    return f"{base}?{urlencode(params)}" if params else base


def _build_chart_payload(funnel: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Compact dataset for Chart.js canvases on the wanted funnel page."""
    publishers = funnel["breakdowns"]["publishers"][:15]
    categories = funnel["breakdowns"]["categories"][:12]
    trend = funnel["monthly_trend"]
    # Keep the trend chart readable by showing up to the last 36 active months.
    if len(trend) > 36:
        trend = trend[-36:]

    return {
        "ttf_buckets": funnel["time_to_find"]["buckets"],
        "monthly_trend": trend,
        "top_publishers": [
            {
                "label": row["label"],
                "total": row["total"],
                "found": row["found"],
                "searching": row["searching"],
                "paused": row["paused"],
                "hit_rate": row["hit_rate"] or 0.0,
                "active_hit_rate": row["active_hit_rate"] or 0.0,
            }
            for row in publishers
        ],
        "categories": [
            {
                "label": row["label"],
                "total": row["total"],
                "found": row["found"],
                "searching": row["searching"],
                "paused": row["paused"],
                "hit_rate": row["hit_rate"] or 0.0,
                "active_hit_rate": row["active_hit_rate"] or 0.0,
            }
            for row in categories
        ],
    }


def stats_wanted_funnel(request: HttpRequest) -> HttpResponse:
    """Wanted-gallery funnel page. Session or bearer token (read scope) auth; ``format=json`` returns raw data."""
    wants_json = request.GET.get("format") == "json"
    _, user = double_check_auth(request, UserLongLivedToken.SCOPE_READ)

    if not _can_view_funnel(user):
        if wants_json:
            return JsonResponse({"error": "You don't have permission to view the wanted-gallery funnel."}, status=403)
        if user is None or not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url=reverse("viewer:login"))
        return render_error(request, "You don't have the permissions to read this page.")

    try:
        options = parse_funnel_params(request.GET)
    except FunnelParamsError as exc:
        if wants_json:
            return JsonResponse({"error": str(exc)}, status=400)
        messages.error(request, str(exc))
        options = FunnelOptions()

    funnel = build_wanted_funnel(options, include_filter_choices=not wants_json)

    if wants_json:
        return JsonResponse(funnel, encoder=DjangoJSONEncoder, json_dumps_params={"ensure_ascii": False})

    # Attach drill-down URLs to breakdown rows so templates stay clean.
    base_url = reverse("viewer:stats-wanted")
    wanted_base_url = reverse("viewer:wanted-galleries")
    for dim_key, param_name, wg_param in (
        ("publishers", "publisher", "publisher"),
        ("categories", "category", None),
        ("book_types", "book_type", "book_type"),
        ("reasons", "reason", "reason"),
        ("wanted_providers", "provider", "provider"),
    ):
        for row in funnel["breakdowns"][dim_key]:
            qs = _query_with(request, **{param_name: row["filter_value"], "overdue_page": None})
            row["funnel_url"] = f"{base_url}?{qs}"
            if wg_param and row["key"]:
                row["wanted_url"] = f"{wanted_base_url}?{urlencode({wg_param: row['key']})}"
            else:
                row["wanted_url"] = ""

    overdue_preset_links = [
        {
            "days": days,
            "active": options.overdue_days == days,
            "url": f"{base_url}?{_query_with(request, overdue_days=days, overdue_page=None)}",
        }
        for days in OVERDUE_DAY_PRESETS
    ]

    overdue_info = funnel["overdue"]
    prev_page_url = (
        f"{base_url}?{_query_with(request, overdue_page=overdue_info['previous_page'])}#overdue-section"
        if overdue_info["has_previous"]
        else ""
    )
    next_page_url = (
        f"{base_url}?{_query_with(request, overdue_page=overdue_info['next_page'])}#overdue-section"
        if overdue_info["has_next"]
        else ""
    )

    json_query = request.GET.copy()
    json_query["format"] = "json"

    context = {
        "page_title": "Wanted-Gallery Funnel",
        "funnel": funnel,
        "options": options,
        "scope_choices": SCOPE_CHOICES,
        "baseline_choices": BASELINE_CHOICES,
        "window_presets": [(key, label) for key, label, _ in WINDOW_PRESETS],
        "overdue_sort_choices": OVERDUE_SORT_CHOICES,
        "overdue_preset_links": overdue_preset_links,
        "min_overdue_days": MIN_OVERDUE_DAYS,
        "max_overdue_days": MAX_OVERDUE_DAYS,
        "max_overdue_per_page": MAX_OVERDUE_PER_PAGE,
        "active_filters": _build_active_filters(request, options),
        "json_query": json_query.urlencode(),
        "wanted_list_url": _wanted_list_url(options, unfound_active=False),
        "overdue_wanted_list_url": _wanted_list_url(options, unfound_active=True),
        "prev_page_url": prev_page_url,
        "next_page_url": next_page_url,
        "base_query_no_page": _query_without(request, "overdue_page", "format"),
        "chart_data": _build_chart_payload(funnel),
        "empty_sentinel": EMPTY_SENTINEL,
    }
    return render(request, "viewer/stats_wanted_funnel.html", context)
