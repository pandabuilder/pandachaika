import json
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import elasticsearch
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse, QueryDict
from django.template.response import TemplateResponse
from django.test import RequestFactory, SimpleTestCase, override_settings
from elasticsearch.exceptions import NotFoundError, RequestError

from viewer.views import elasticsearch as es_views


@contextmanager
def _patch_es(fake_client, responses, calls):
    with ExitStack() as stack:
        stack.enter_context(patch.object(es_views, "es_client", fake_client))
        stack.enter_context(patch.object(es_views, "elasticsearch", elasticsearch, create=True))
        stack.enter_context(
            patch.object(es_views, "Search", side_effect=lambda **kw: _FakeSearch(responses, calls), create=True)
        )
        yield


class _FakeUser:
    def __init__(self, *, authenticated: bool = True, perms: set[str] | None = None) -> None:
        self.is_authenticated = authenticated
        self.is_active = authenticated
        self.is_staff = False
        self.is_superuser = False
        self.username = "es_tester"
        self.pk = 1 if authenticated else None
        self._perms = perms or set()

    def has_perm(self, perm: str, obj=None) -> bool:
        return perm in self._perms

    def has_module_perms(self, app_label: str) -> bool:
        return any(p.startswith(f"{app_label}.") for p in self._perms)


class _FakeBucket:
    def __init__(self, key: str, doc_count: int) -> None:
        self.key = key
        self.doc_count = doc_count


class _FakeAggBucketCollection:
    def __init__(self, buckets: list[_FakeBucket]) -> None:
        self.buckets = buckets


class _FakeAggregations:
    def __init__(self, buckets_map: dict[str, list[_FakeBucket]], metrics: dict[str, float] | None = None) -> None:
        self._buckets_map = {k: _FakeAggBucketCollection(v) for k, v in buckets_map.items()}
        self._metrics = metrics or {}

    def to_dict(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {k: {"buckets": []} for k in self._buckets_map}
        for k, v in self._metrics.items():
            out[k] = {"value": v}
        return out

    def __getitem__(self, key: str) -> _FakeAggBucketCollection:
        return self._buckets_map[key]


class _FakeHit:
    def __init__(self, doc_id: str, data: dict) -> None:
        self.meta = SimpleNamespace(id=doc_id)
        self._data = dict(data)
        for k, v in data.items():
            setattr(self, k, v)

    def to_dict(self) -> dict:
        return dict(self._data)


class _FakeResponse:
    def __init__(
        self,
        hits: list[_FakeHit],
        total: int,
        aggregations: _FakeAggregations | None = None,
    ) -> None:
        self._hits = hits
        self.hits = SimpleNamespace(total=SimpleNamespace(value=total))
        if aggregations is not None:
            self.aggregations = aggregations

    def __iter__(self):
        return iter(self._hits)

    def __len__(self) -> int:
        return len(self._hits)

    def __getitem__(self, idx: int) -> _FakeHit:
        return self._hits[idx]


class _FakeSearch:
    """Captures Search DSL operations and returns scripted responses without a live ES daemon."""

    def __init__(self, responses: list[_FakeResponse | Exception], calls: list[dict]) -> None:
        self._responses = responses
        self._calls = calls
        self._state: dict = {
            "extra": {},
            "source": None,
            "sort": [],
            "slice": None,
            "aggs_buckets": [],
            "aggs_metrics": [],
            "filters": [],
            "excludes": [],
            "queries": [],
        }
        self.aggs = SimpleNamespace(
            bucket=self._add_bucket,
            metric=self._add_metric,
        )

    def _clone(self) -> "_FakeSearch":
        clone = _FakeSearch(self._responses, self._calls)
        clone._state = {
            "extra": dict(self._state["extra"]),
            "source": deepcopy(self._state["source"]),
            "sort": list(self._state["sort"]),
            "slice": self._state["slice"],
            "aggs_buckets": list(self._state["aggs_buckets"]),
            "aggs_metrics": list(self._state["aggs_metrics"]),
            "filters": list(self._state["filters"]),
            "excludes": list(self._state["excludes"]),
            "queries": list(self._state["queries"]),
        }
        return clone

    def _add_bucket(self, name: str, agg_type: str, **kwargs):
        self._state["aggs_buckets"].append((name, agg_type, kwargs))
        return self

    def _add_metric(self, name: str, agg_type: str, **kwargs):
        self._state["aggs_metrics"].append((name, agg_type, kwargs))
        return self

    def extra(self, **kwargs) -> "_FakeSearch":
        c = self._clone()
        c._state["extra"].update(kwargs)
        return c

    def source(self, *args, **kwargs) -> "_FakeSearch":
        c = self._clone()
        c._state["source"] = (args, kwargs)
        return c

    def sort(self, *args) -> "_FakeSearch":
        c = self._clone()
        c._state["sort"].extend(args)
        return c

    def filter(self, *args, **kwargs) -> "_FakeSearch":
        c = self._clone()
        c._state["filters"].append((args, kwargs))
        return c

    def exclude(self, *args, **kwargs) -> "_FakeSearch":
        c = self._clone()
        c._state["excludes"].append((args, kwargs))
        return c

    def query(self, *args, **kwargs) -> "_FakeSearch":
        c = self._clone()
        c._state["queries"].append((args, kwargs))
        return c

    def suggest(self, *args, **kwargs) -> "_FakeSearch":
        return self._clone()

    def __getitem__(self, item: slice) -> "_FakeSearch":
        c = self._clone()
        c._state["slice"] = (item.start, item.stop)
        return c

    def count(self) -> int:
        raise AssertionError("s.count() should not be called; use track_total_hits=True in a single execute()")

    def execute(self):
        self._calls.append(deepcopy(self._state))
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def _naive_prepare_facet_data(aggregations: _FakeAggregations, get_args: QueryDict) -> dict:
    resp: dict = {}
    for area, agg in aggregations.to_dict().items():
        if "value" in agg:
            resp[area] = agg["value"]
            continue
        resp[area] = []
        for item in aggregations[area].buckets:
            url_args, is_active = es_views.ESHomePageView.facet_url_args(
                url_args=deepcopy(get_args.dict()), field_name=area, field_value=str(item.key)
            )
            resp[area].append(
                {
                    "url_args": urlencode(url_args),
                    "name": item.key,
                    "count": item.doc_count,
                    "is_active": is_active,
                }
            )
    return resp


class ElasticsearchViewOptimizationTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.user = _FakeUser(authenticated=True)

    def _make_request(self, path: str, user=None, method: str = "GET", post_data: dict | None = None):
        if method == "POST":
            request = self.factory.post(path, post_data or {})
        else:
            request = self.factory.get(path)
        request.user = user if user is not None else AnonymousUser()
        request.session = cast(Any, {})
        return request

    def test_prepare_facet_data_matches_naive_implementation_byte_for_byte(self) -> None:
        aggs = _FakeAggregations(
            buckets_map={
                "tags__full": [
                    _FakeBucket("female:glasses", 42),
                    _FakeBucket("language:english", 100),
                    _FakeBucket("artist:foo bar", 5),
                ],
                "source_type": [_FakeBucket("panda", 80)],
                "reason": [_FakeBucket("wanimagazine", 12), _FakeBucket("irodori", 7)],
            },
            metrics={"avg_size": 12345.0},
        )
        for qs in (
            "",
            "tags__full=female%3Aglasses",
            "q=hello+world&tags__full=female%3Aglasses%2Cartist%3Afoo+bar&sort=size",
            "reason=irodori&page=2&order=asc",
            "tags__full=&reason=irodori",
        ):
            qd = QueryDict(qs)
            expected = _naive_prepare_facet_data(aggs, qd)
            actual = es_views._build_facet_data(cast(Any, aggs), qd, include_url_args=True)
            self.assertEqual(actual, expected, f"Mismatch for query string: {qs!r}")

    def test_prepare_facet_data_without_url_args_omits_url_args_and_tracks_active(self) -> None:
        aggs = _FakeAggregations(
            buckets_map={
                "tags__full": [
                    _FakeBucket("female:glasses", 42),
                    _FakeBucket("language:english", 100),
                ],
            }
        )
        qd = QueryDict("tags__full=female%3Aglasses")
        actual = es_views._build_facet_data(cast(Any, aggs), qd, include_url_args=False)
        self.assertEqual(
            actual["tags__full"],
            [
                {"name": "female:glasses", "count": 42, "is_active": True},
                {"name": "language:english", "count": 100, "is_active": False},
            ],
        )

    def test_convert_hit_to_template_parses_iso_dates_and_missing_create_date(self) -> None:
        hit = _FakeHit(
            "123",
            {
                "title": "Test Archive",
                "public_date": "2024-05-09T12:34:56+00:00",
                "original_date": "2023-11-01T00:00:00Z",
            },
        )
        converted = es_views.ESHomePageView.convert_hit_to_template(hit, "/archive/")
        self.assertEqual(converted.pk, "123")
        self.assertEqual(converted.url, "/archive/123/")
        self.assertIsNone(converted.create_date_c)
        self.assertEqual(converted.create_date_formatted, "")
        self.assertEqual(converted.public_date_formatted, "05/09/2024")
        self.assertEqual(converted.original_date_formatted, "11/01/2023")

        gallery_hit = _FakeHit(
            "456",
            {
                "title": "Test Gallery",
                "create_date": "2025-01-02T03:04:05+00:00",
                "posted_date": "2024-12-31T23:59:59+00:00",
            },
        )
        converted_g = es_views.ESHomeGalleryPageView.convert_hit_to_template(gallery_hit, "/gallery/")
        self.assertEqual(converted_g.create_date_formatted, "01/02/2025")
        self.assertEqual(converted_g.posted_date_formatted, "12/31/2024")

    @override_settings(ES_ENABLED=True)
    def test_es_home_page_single_execute_no_exists_no_count_and_renders_facet_buttons(self) -> None:
        fake_client = MagicMock()
        fake_client.indices.exists.side_effect = AssertionError("indices.exists() must not be called")
        calls: list[dict] = []
        resp = _FakeResponse(
            hits=[
                _FakeHit(
                    "10",
                    {
                        "title": "Sample Archive",
                        "title_jpn": "",
                        "thumbnail": "",
                        "image_count": 20,
                        "size": 1024,
                        "public_date": "2026-01-15T10:00:00+00:00",
                        "original_date": "2026-01-10T10:00:00+00:00",
                    },
                )
            ],
            total=1,
            aggregations=_FakeAggregations(
                buckets_map={
                    "tags__full": [_FakeBucket("language:english", 1)],
                    "source_type": [_FakeBucket("panda", 1)],
                    "reason": [_FakeBucket("wanimagazine", 1)],
                }
            ),
        )
        responses = [resp]

        with _patch_es(fake_client, responses, calls):
            request = self._make_request("/es-archive-search/?tags__full=language%3Aenglish")
            response = cast(TemplateResponse, es_views.ESHomePageView.as_view()(request))
            response.render()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["extra"].get("track_total_hits"), True)
        self.assertEqual(calls[0]["slice"], (0, 48))
        source_fields = calls[0]["source"][0][0]
        self.assertNotIn("tags", source_fields)
        self.assertNotIn("create_date", source_fields)
        self.assertNotIn("title_suggest", source_fields)

        html = response.content.decode("utf-8")
        self.assertIn('class="nav-link es-facet-btn"', html)
        self.assertIn('data-facet-field="tags__full"', html)
        self.assertIn('data-facet-value="language:english"', html)
        self.assertIn("data-es-facet-base=", html)
        self.assertNotIn('href="/es-archive-search/?tags__full=', html)

    @override_settings(ES_ENABLED=True)
    def test_es_gallery_page_renders_facet_buttons_and_supports_show_url_source(self) -> None:
        fake_client = MagicMock()
        fake_client.indices.exists.side_effect = AssertionError("indices.exists() must not be called")
        calls: list[dict] = []
        resp = _FakeResponse(
            hits=[
                _FakeHit(
                    "22",
                    {
                        "title": "Sample Gallery",
                        "title_jpn": "",
                        "thumbnail": "",
                        "image_count": 30,
                        "size": 2048,
                        "category": "Manga",
                        "posted_date": "2026-01-12T10:00:00+00:00",
                        "source_url": "https://example.org/g/22/abc/",
                    },
                )
            ],
            total=1,
            aggregations=_FakeAggregations(
                buckets_map={
                    "tags__full": [_FakeBucket("female:glasses", 5)],
                    "category": [_FakeBucket("Manga", 5)],
                    "provider": [_FakeBucket("panda", 5)],
                    "reason": [_FakeBucket("wanimagazine", 5)],
                }
            ),
        )
        responses = [resp]

        with _patch_es(fake_client, responses, calls):
            request = self._make_request("/es-gallery-search/?show_url=1")
            response = cast(TemplateResponse, es_views.ESHomeGalleryPageView.as_view()(request))
            response.render()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        source_fields = calls[0]["source"][0][0]
        self.assertIn("source_url", source_fields)
        self.assertNotIn("tags", source_fields)
        html = response.content.decode("utf-8")
        self.assertIn('class="nav-link es-facet-btn"', html)
        self.assertIn('data-facet-field="category"', html)
        self.assertIn('data-facet-field="provider"', html)
        self.assertNotIn('href="/es-gallery-search/?tags__full=', html)

    @override_settings(ES_ENABLED=True)
    def test_es_home_page_honors_no_agg_and_extended_source_fields(self) -> None:
        fake_client = MagicMock()
        calls: list[dict] = []
        resp = _FakeResponse(
            hits=[
                _FakeHit(
                    "10",
                    {
                        "title": "Sample",
                        "title_jpn": "",
                        "thumbnail": "",
                        "image_count": 10,
                        "size": 500,
                        "create_date": "2026-01-15T10:00:00+00:00",
                        "public_date": "2026-01-15T10:00:00+00:00",
                        "original_date": None,
                        "tags": [],
                        "category": "Manga",
                        "reason": "test",
                    },
                )
            ],
            total=1,
        )
        responses = [resp]

        with _patch_es(fake_client, responses, calls):
            request = self._make_request("/es-archive-search/?no_agg=1&view=extended", user=self.user)
            view = es_views.ESHomePageView()
            view.setup(request)
            ctx = view.get_context_data()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["aggs_buckets"], [])
        self.assertNotIn("aggregations", ctx)
        source_fields = calls[0]["source"][0][0]
        self.assertIn("tags", source_fields)
        self.assertIn("create_date", source_fields)

    @override_settings(ES_ENABLED=True)
    def test_es_home_page_clamps_out_of_range_page_with_second_fetch(self) -> None:
        fake_client = MagicMock()
        calls: list[dict] = []
        hit = _FakeHit(
            "77",
            {
                "title": "Last Page Hit",
                "title_jpn": "",
                "thumbnail": "",
                "image_count": 5,
                "size": 100,
                "public_date": "2026-01-15T10:00:00+00:00",
                "original_date": None,
            },
        )
        responses = [_FakeResponse(hits=[], total=50), _FakeResponse(hits=[hit], total=50)]

        with _patch_es(fake_client, responses, calls):
            request = self._make_request("/es-archive-search/?page=5")
            view = es_views.ESHomePageView()
            view.setup(request)
            ctx = view.get_context_data()

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["slice"], (192, 240))
        self.assertEqual(calls[1]["slice"], (48, 50))
        self.assertEqual(ctx["paginator"]["number"], 2)
        self.assertEqual(len(ctx["hits"]), 1)
        self.assertEqual(ctx["hits"][0].pk, "77")

    @override_settings(ES_ENABLED=True)
    def test_es_home_page_handles_missing_index_and_request_error(self) -> None:
        fake_client = MagicMock()
        calls: list[dict] = []
        not_found = NotFoundError("index_not_found_exception", MagicMock(status=404), {})
        bad_req = RequestError("search_phase_execution_exception", MagicMock(status=400), {})

        with _patch_es(fake_client, [not_found], calls):
            request = self._make_request("/es-archive-search/")
            view = es_views.ESHomePageView()
            view.setup(request)
            ctx = view.get_context_data()
        self.assertEqual(ctx["message"], "Expected index does not exist.")

        with _patch_es(fake_client, [bad_req], calls):
            request = self._make_request("/es-archive-search/?q=[broken")
            view = es_views.ESHomePageView()
            view.setup(request)
            ctx = view.get_context_data()
        self.assertEqual(ctx["message"], es_views.ES_QUERY_PARSE_ERROR_MSG)

    @override_settings(ES_ENABLED=True)
    def test_es_gallery_recall_api_skips_aggs_and_fetches_minimal_source(self) -> None:
        updater_user = _FakeUser(authenticated=True, perms={"viewer.update_metadata"})
        fake_client = MagicMock()
        calls: list[dict] = []
        responses = [
            _FakeResponse(
                hits=[_FakeHit("1", {"source_url": "https://example.org/g/1/a/", "provider": "panda"})],
                total=1,
            )
        ]

        with _patch_es(fake_client, responses, calls):
            request = self._make_request(
                "/es-gallery-search/?q=provider:panda",
                user=updater_user,
                method="POST",
                post_data={"recall_api": "1"},
            )
            view = es_views.ESHomeGalleryPageView()
            view.setup(request)
            ctx = view.get_context_data()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["aggs_buckets"], [])
        self.assertEqual(calls[0]["aggs_metrics"], [])
        self.assertEqual(calls[0]["source"][0][0], ["source_url", "provider"])
        self.assertEqual(ctx["gallery_links"], ["https://example.org/g/1/a/"])
        self.assertEqual(ctx["gallery_providers"], ["panda"])

    @override_settings(ES_ENABLED=True)
    def test_es_archive_json_view_single_execute_source_excludes_and_url_args(self) -> None:
        fake_client = MagicMock()
        fake_client.indices.exists.side_effect = AssertionError("indices.exists() must not be called")
        calls: list[dict] = []
        responses = [
            _FakeResponse(
                hits=[_FakeHit("99", {"title": "JSON Archive", "public_date": "2026-01-15T10:00:00+00:00"})],
                total=1,
                aggregations=_FakeAggregations(
                    buckets_map={"tags__full": [_FakeBucket("language:english", 1)]}
                ),
            )
        ]

        with _patch_es(fake_client, responses, calls):
            request = self._make_request("/es-archive-json/?q=test")
            response = cast(HttpResponse, es_views.ESArchiveJSONView.as_view()(request))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["extra"].get("track_total_hits"), True)
        self.assertEqual(
            calls[0]["source"][1]["excludes"],
            ["doc", "title_suggest", "title_complete", "create_date"],
        )
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["paginator"]["count"], 1)
        self.assertEqual(
            payload["aggregations"]["tags__full"][0]["url_args"],
            "q=test&tags__full=language%3Aenglish",
        )
