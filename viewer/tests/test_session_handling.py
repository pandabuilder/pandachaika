import importlib
import json
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import yaml
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase

from core.base.config_models import PandaGalleryConfig, RetentionSettings
from viewer.management.commands.prune_db import Command as PruneDbCommand
from viewer.services.retention import get_entity_definitions, prune_database
from viewer.views import elasticsearch as es_views
from viewer.views.head import auth as auth_views
from viewer.views.head import gallery as gallery_views

search_views = importlib.import_module("viewer.views.head.search")


class _TrackingSession(dict):
    """Minimal dict-like session that tracks whether any mutation occurred."""

    def __init__(self, initial: dict | None = None) -> None:
        super().__init__(initial or {})
        self.modified = False

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.modified = True

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self.modified = True


class _FakeAuthUser:
    is_authenticated = True
    is_active = True
    is_staff = False
    is_superuser = False
    username = "tester"
    pk = 42
    id = 42


class _EmptyQuerySet:
    def __iter__(self):
        return iter(())

    def __len__(self) -> int:
        return 0

    def __getitem__(self, item):
        return []

    def select_related(self, *args, **kwargs):
        return self

    def prefetch_related(self, *args, **kwargs):
        return self

    def defer(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def values_list(self, *args, **kwargs):
        return self

    def iterator(self, *args, **kwargs):
        return iter(self)

    def count(self) -> int:
        return 0


class AnonymousSessionWriteTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    def _make_request(
        self,
        path: str,
        *,
        user=None,
        session_data: dict | None = None,
        method: str = "GET",
        body: bytes = b"",
    ):
        if method == "POST":
            request = self.factory.post(path, data=body, content_type="application/json")
        else:
            request = self.factory.get(path)
            if body:
                request._body = body
        request.user = user if user is not None else AnonymousUser()
        request.session = cast(Any, _TrackingSession(session_data))
        return request

    def test_search_does_not_create_session_for_anonymous_or_stateless_requests(self) -> None:
        for path in (
            "/",
            "/search/",
            "/search/?sort=posted&asc_desc=desc&tags=language:english",
            "/search/?sort=posted&tags=language:english&json=",
            "/search/?sort=public_date&gen-ddl=",
            "/search/?view=list",
            "/search/?view=invalid_mode",
        ):
            request = self._make_request(path)
            with (
                patch.object(search_views, "filter_archives", return_value=_EmptyQuerySet()),
                patch.object(search_views, "render", return_value=HttpResponse("ok")),
            ):
                resp = search_views.search(request)
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(request.session.modified, f"Session was mutated for {path}")
            self.assertNotIn("parameters", request.session)

    def test_search_persists_anonymous_view_in_cookie_not_session_and_avoids_redundant_auth_writes(self) -> None:
        # Anonymous switching to cover view uses a cookie, NEVER mutating django_session
        anon_req = self._make_request("/search/?view=cover")
        with (
            patch.object(search_views, "filter_archives", return_value=_EmptyQuerySet()),
            patch.object(search_views, "render", return_value=HttpResponse("ok")) as mock_render,
        ):
            resp = search_views.search(anon_req)
        self.assertFalse(anon_req.session.modified)
        self.assertNotIn("parameters", anon_req.session)
        self.assertEqual(resp.cookies["panda_view"].value, "cover")
        self.assertEqual(mock_render.call_args[0][2]["prm"]["view"], "cover")

        # Subsequent anonymous request carrying cookie restores cover view without session write or redundant cookie
        anon_cookie_req = self._make_request("/search/?page=2")
        anon_cookie_req.COOKIES["panda_view"] = "cover"
        with (
            patch.object(search_views, "filter_archives", return_value=_EmptyQuerySet()),
            patch.object(search_views, "render", return_value=HttpResponse("ok")) as mock_render2,
        ):
            resp2 = search_views.search(anon_cookie_req)
        self.assertFalse(anon_cookie_req.session.modified)
        self.assertNotIn("panda_view", resp2.cookies)
        self.assertEqual(mock_render2.call_args[0][2]["prm"]["view"], "cover")

        # Switching back to list deletes the cookie without touching session
        anon_reset_req = self._make_request("/search/?view=list")
        anon_reset_req.COOKIES["panda_view"] = "cover"
        with (
            patch.object(search_views, "filter_archives", return_value=_EmptyQuerySet()),
            patch.object(search_views, "render", return_value=HttpResponse("ok")),
        ):
            resp3 = search_views.search(anon_reset_req)
        self.assertFalse(anon_reset_req.session.modified)
        self.assertEqual(resp3.cookies["panda_view"].value, "")

        # Authenticated user with identical preferences does NOT trigger a redundant session write
        existing = {"sort": "create_date", "asc_desc": "desc", "view": "list", "main_filters": True}
        auth_req = self._make_request("/search/", user=_FakeAuthUser(), session_data={"parameters": existing})
        with (
            patch.object(search_views, "filter_archives", return_value=_EmptyQuerySet()),
            patch.object(search_views, "render", return_value=HttpResponse("ok")),
        ):
            search_views.search(auth_req)
        self.assertFalse(auth_req.session.modified)

        # Authenticated user changing sort DOES persist
        auth_change_req = self._make_request(
            "/search/?sort=posted", user=_FakeAuthUser(), session_data={"parameters": existing}
        )
        with (
            patch.object(search_views, "filter_archives", return_value=_EmptyQuerySet()),
            patch.object(search_views, "render", return_value=HttpResponse("ok")),
        ):
            search_views.search(auth_change_req)
        self.assertTrue(auth_change_req.session.modified)
        self.assertEqual(auth_change_req.session["parameters"]["sort"], "posted")

    def test_gallery_list_does_not_create_session_for_anonymous_or_json_requests(self) -> None:
        for path in (
            "/galleries/",
            "/galleries/?tags=artist:kago&sort=posted&asc_desc=desc",
            "/galleries/?tags=artist:kago&json=",
            "/galleries/?view=list",
            "/galleries/?view=bogus",
        ):
            request = self._make_request(path)
            with (
                patch.object(gallery_views, "filter_galleries", return_value=_EmptyQuerySet()),
                patch.object(gallery_views, "render", return_value=HttpResponse("ok")),
            ):
                resp = gallery_views.gallery_list(request)
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(request.session.modified, f"Session was mutated for {path}")
            self.assertNotIn("gallery_parameters", request.session)

        # Explicit non-default view change for anonymous persists in cookie, NOT django_session
        anon_ext = self._make_request("/galleries/?view=extended")
        with (
            patch.object(gallery_views, "filter_galleries", return_value=_EmptyQuerySet()),
            patch.object(gallery_views, "render", return_value=HttpResponse("ok")) as mock_render,
        ):
            resp_ext = gallery_views.gallery_list(anon_ext)
        self.assertFalse(anon_ext.session.modified)
        self.assertNotIn("gallery_parameters", anon_ext.session)
        self.assertEqual(resp_ext.cookies["panda_gallery_view"].value, "extended")
        self.assertEqual(mock_render.call_args[0][2]["prm"]["view"], "extended")

        # Subsequent anonymous request with cookie restores extended view
        anon_cookie = self._make_request("/galleries/?page=2")
        anon_cookie.COOKIES["panda_gallery_view"] = "extended"
        with (
            patch.object(gallery_views, "filter_galleries", return_value=_EmptyQuerySet()),
            patch.object(gallery_views, "render", return_value=HttpResponse("ok")) as mock_render2,
        ):
            gallery_views.gallery_list(anon_cookie)
        self.assertFalse(anon_cookie.session.modified)
        self.assertEqual(mock_render2.call_args[0][2]["prm"]["view"], "extended")

    def test_session_settings_get_does_not_mutate_session(self) -> None:
        request = self._make_request("/session-settings/", body=json.dumps({"viewer_parameters": True}).encode("utf-8"))
        resp = auth_views.session_settings(request)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content.decode("utf-8")), {"image_width": 900})
        self.assertFalse(request.session.modified)
        self.assertNotIn("viewer_parameters", request.session)

    def test_es_view_uses_cookie_not_session_for_anonymous_view_switch(self) -> None:
        from viewer.tests.test_elasticsearch_views import _FakeResponse, _patch_es

        fake_client = MagicMock()
        for path in ("/es-archive-search/?view=bogus", "/es-archive-search/?view=list"):
            request = self._make_request(path)
            calls: list[dict] = []
            with (
                patch.object(es_views.settings, "ES_ENABLED", True),
                _patch_es(fake_client, [_FakeResponse(hits=[], total=0)], calls),
            ):
                view = es_views.ESHomePageView()
                view.setup(request)
                view.get_context_data()
            self.assertFalse(request.session.modified, f"Session mutated for {path}")

        # Explicit valid non-default view change for anonymous uses cookie, NEVER django_session
        request_ext = self._make_request("/es-archive-search/?view=extended")
        calls_ext: list[dict] = []
        with (
            patch.object(es_views.settings, "ES_ENABLED", True),
            _patch_es(fake_client, [_FakeResponse(hits=[], total=0)], calls_ext),
        ):
            view = es_views.ESHomePageView()
            view.setup(request_ext)
            ctx = view.get_context_data()
            with patch.object(es_views.TemplateView, "render_to_response", return_value=HttpResponse("ok")):
                resp = view.render_to_response(ctx)
        self.assertFalse(request_ext.session.modified)
        self.assertEqual(ctx["view_parameters"]["view"], "extended")
        self.assertEqual(resp.cookies["panda_view"].value, "extended")

        # Gallery ES view uses panda_gallery_view cookie
        request_gal = self._make_request("/es-gallery-search/?view=extended")
        calls_gal: list[dict] = []
        with (
            patch.object(es_views.settings, "ES_ENABLED", True),
            _patch_es(fake_client, [_FakeResponse(hits=[], total=0)], calls_gal),
        ):
            gview = es_views.ESHomeGalleryPageView()
            gview.setup(request_gal)
            gctx = gview.get_context_data()
            with patch.object(es_views.TemplateView, "render_to_response", return_value=HttpResponse("ok")):
                gresp = gview.render_to_response(gctx)
        self.assertFalse(request_gal.session.modified)
        self.assertEqual(gctx["view_parameters"]["view"], "extended")
        self.assertEqual(gresp.cookies["panda_gallery_view"].value, "extended")

        # Authenticated user changing view DOES persist to session
        request_auth = self._make_request("/es-archive-search/?view=extended", user=_FakeAuthUser())
        calls_auth: list[dict] = []
        with (
            patch.object(es_views.settings, "ES_ENABLED", True),
            _patch_es(fake_client, [_FakeResponse(hits=[], total=0)], calls_auth),
        ):
            aview = es_views.ESHomePageView()
            aview.setup(request_auth)
            aview.get_context_data()
        self.assertTrue(request_auth.session.modified)
        self.assertEqual(request_auth.session["parameters"]["view"], "extended")


class SessionRetentionConfigAndServiceTests(SimpleTestCase):
    def test_retention_settings_and_default_yaml_include_session_days(self) -> None:
        retention = RetentionSettings()
        self.assertEqual(retention.session_days, 1)

        cfg = PandaGalleryConfig()
        cfg.update_from_dict({"retention": {"session_days": 7}})
        self.assertEqual(cfg.retention.session_days, 7)

        default_yaml_path = Path(__file__).resolve().parents[2] / "default.yaml"
        parsed = yaml.safe_load(default_yaml_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["retention"]["session_days"], 1)

    def test_entity_definitions_include_sessions_and_chunked_pruning(self) -> None:
        defs = {d["key"]: d for d in get_entity_definitions()}
        self.assertIn("sessions", defs)
        session_def = defs["sessions"]
        self.assertEqual(session_def["config_field"], "session_days")
        self.assertEqual(session_def["date_field"], "expire_date")

        fake_qs = MagicMock()
        fake_pks = MagicMock()
        fake_pks.__getitem__.side_effect = [["sess1", "sess2"], ["sess3"], []]
        fake_qs.values_list.return_value = fake_pks

        fake_model = MagicMock()
        fake_model.objects.filter.return_value.delete.side_effect = [(2, {}), (1, {})]

        patched_def = dict(session_def)
        patched_def["model"] = fake_model
        patched_def["build_prunable_qs"] = lambda cutoff: fake_qs

        with (
            patch("viewer.services.retention.get_entity_definitions", return_value=[patched_def]),
            patch("viewer.services.retention.logger"),
            patch("viewer.utils.actions.event_log"),
        ):
            res = prune_database(entity="sessions", days_override=1, dry_run=False, chunk_size=2)

        self.assertEqual(res["total_deleted"], 3)
        self.assertEqual(res["entities"]["sessions"]["pruned"], 3)
        fake_qs.count.assert_not_called()

    def test_prune_db_command_registers_session_days_option(self) -> None:
        cmd = PruneDbCommand()
        parser = cmd.create_parser("manage.py", "prune_db")
        opts = parser.parse_args(["--models", "sessions", "--session-days", "3", "--dry-run"])
        self.assertEqual(opts.models, "sessions")
        self.assertEqual(opts.session_days, 3)
        self.assertTrue(opts.dry_run)
