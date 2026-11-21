import importlib
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import AnonymousUser
from django.db.models import F, QuerySet
from django.test import RequestFactory, SimpleTestCase

from core.base.setup import Settings
from core.base.types import GalleryData
from viewer.feeds import LatestArchivesFeed
from viewer.models import Archive, ArchiveTag, Gallery, ItemProperties, WantedGallery
from viewer.views.head import gallery as gallery_views
from viewer.views.head.common import TAG_ID_PREFETCH_LIMIT, apply_tag_filters
from workers.models import TaskQueue
from workers.serialization import deserialize_settings_options, serialize_settings_options

search_views = importlib.import_module("viewer.views.head.search")


class _FakeAuthUser:
    is_authenticated = True
    is_active = True
    is_staff = True
    is_superuser = False
    username = "tester"
    pk = 7
    id = 7


class TagFilterExistsTests(SimpleTestCase):
    def _mock_tag_ids(self, return_values):
        mock_qs = MagicMock()
        mock_qs.values_list.return_value.__getitem__.side_effect = return_values
        return patch("viewer.models.Tag.objects.filter", return_value=mock_qs)

    def test_positive_and_negative_tags_use_exists_without_distinct(self) -> None:
        base_qs = Archive.objects.filter(public=True, binned=False)
        with self._mock_tag_ids([[11], [22, 23]]):
            qs = apply_tag_filters(
                base_qs,
                "language:english, -female:lolicon",
                through_model=ArchiveTag,
                fk_field="archive_id",
            )
        sql = str(qs.query)
        self.assertIn("EXISTS(", sql)
        self.assertIn("NOT (EXISTS(", sql)
        self.assertNotIn("DISTINCT", sql)
        self.assertNotIn("viewer_tag", sql)
        self.assertIn('"tag_id" = 11', sql)
        self.assertIn('"tag_id" IN (22, 23)', sql)

    def test_unmatched_positive_tag_short_circuits_to_empty_and_unmatched_negative_is_noop(self) -> None:
        base_qs = Archive.objects.filter(public=True, binned=False)
        with self._mock_tag_ids([[]]):
            empty_qs = apply_tag_filters(
                base_qs, "^language:does_not_exist", through_model=ArchiveTag, fk_field="archive_id"
            )
        self.assertTrue(empty_qs.query.is_empty())

        with self._mock_tag_ids([[]]):
            noop_qs = apply_tag_filters(
                base_qs, "-^language:does_not_exist", through_model=ArchiveTag, fk_field="archive_id"
            )
        self.assertNotIn("EXISTS", str(noop_qs.query))

    def test_broad_tag_match_above_limit_falls_back_to_through_tag_subquery(self) -> None:
        base_qs = Archive.objects.filter(public=True, binned=False)
        too_many_ids = list(range(1, TAG_ID_PREFETCH_LIMIT + 2))
        with self._mock_tag_ids([too_many_ids]):
            qs = apply_tag_filters(base_qs, "a", through_model=ArchiveTag, fk_field="archive_id")
        sql = str(qs.query)
        self.assertIn("EXISTS", sql)
        self.assertIn("viewer_tag", sql)
        self.assertNotIn("DISTINCT", sql)

    def test_gallery_tags_or_uses_exists_or_without_distinct(self) -> None:
        base_qs = Gallery.objects.filter(public=True)
        with self._mock_tag_ids([[101], [202]]):
            qs = apply_tag_filters(
                base_qs,
                "artist:alpha, -female:beta",
                through_model=Gallery.tags.through,
                fk_field="gallery_id",
                tags_or=True,
            )
        sql = str(qs.query)
        self.assertIn("EXISTS(", sql)
        self.assertIn("NOT (EXISTS(", sql)
        self.assertIn(" OR ", sql)
        self.assertNotIn("DISTINCT", sql)

        # All positive OR branches matching 0 tags -> empty queryset
        with self._mock_tag_ids([[], []]):
            empty_or = apply_tag_filters(
                base_qs,
                "none1, none2",
                through_model=Gallery.tags.through,
                fk_field="gallery_id",
                tags_or=True,
            )
        self.assertTrue(empty_or.query.is_empty())

        # Any negative OR branch matching 0 tags -> trivially true (unmodified base_qs)
        with self._mock_tag_ids([[], []]):
            trivially_true = apply_tag_filters(
                base_qs,
                "none1, -none2",
                through_model=Gallery.tags.through,
                fk_field="gallery_id",
                tags_or=True,
            )
        self.assertNotIn("EXISTS", str(trivially_true.query))


class SearchGalleryAndFeedQueryTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    def test_filter_archives_skips_tag_prefetch_for_list_view_and_defers_provider_metadata(self) -> None:
        request = self.factory.get("/search/")
        request.user = AnonymousUser()

        qs_list = search_views.filter_archives(request, {"view": "list"}, {}, authenticated=False)
        self.assertEqual(qs_list._prefetch_related_lookups, ())
        deferred_fields, _ = qs_list.query.deferred_loading
        self.assertIn("gallery__provider_metadata", deferred_fields)
        self.assertIn("gallery__comment", deferred_fields)

        qs_ext = search_views.filter_archives(request, {"view": "extended"}, {}, authenticated=False)
        self.assertIn("tags", qs_ext._prefetch_related_lookups)
        self.assertIn("archivetag_set__tag", qs_ext._prefetch_related_lookups)

    def test_gen_ddl_uses_values_list_pk_instead_of_materializing_models(self) -> None:
        request = self.factory.get("/search/?gen-ddl=")
        request.user = AnonymousUser()
        request.session = cast(Any, {})

        fake_qs = MagicMock()
        fake_qs.values_list.return_value.iterator.return_value = iter([10, 20])

        with patch.object(search_views, "filter_archives", return_value=fake_qs):
            resp = search_views.search(request)

        self.assertEqual(resp.status_code, 200)
        fake_qs.values_list.assert_called_once_with("pk", flat=True)
        body = resp.content.decode("utf-8")
        self.assertIn("/archive/10/download/", body)
        self.assertIn("/archive/20/download/", body)

    def test_gallery_defer_includes_provider_metadata(self) -> None:
        self.assertIn("provider_metadata", gallery_views.filter_galleries_defer)

    def test_feed_anonymous_orders_public_date_desc_nulls_last(self) -> None:
        feed = LatestArchivesFeed()
        request = self.factory.get("/feed/")
        request.user = AnonymousUser()

        captured = {}

        def fake_filter_archives_simple(params, authenticated=False):
            qs = Archive.objects.filter(binned=False)
            captured["base"] = qs
            return qs

        with patch("viewer.feeds.filter_archives_simple", side_effect=fake_filter_archives_simple):
            result_qs = cast(QuerySet[Archive], feed.items(request))

        self.assertEqual(result_qs.query.order_by, (F("public_date").desc(nulls_last=True),))
        self.assertIn("NULLS LAST", str(result_qs.query))


class TaskQueueDiffSerializationAndIndexTests(SimpleTestCase):
    def test_diff_serialization_omits_unchanged_settings_and_roundtrips_overrides(self) -> None:
        base = Settings(load_from_disk=False)
        base.downloaders = {"panda_archive": 4, "panda_torrent": 3}
        base.matchers = {"chaika_hash": 1, "panda_title": -2}

        modified = Settings(load_from_disk=False)
        modified.downloaders = {"panda_archive": 10, "panda_torrent": 3}
        modified.matchers = {"chaika_hash": 1, "panda_title": -2}
        modified.redownload = True
        modified.archive_reason = "manual_test"
        modified.archive_user = cast(Any, _FakeAuthUser())

        full_payload = serialize_settings_options(modified)
        diff_payload = serialize_settings_options(modified, base_settings=base)

        self.assertLess(len(diff_payload), len(full_payload))
        self.assertEqual(diff_payload["redownload"], True)
        self.assertEqual(diff_payload["archive_reason"], "manual_test")
        self.assertEqual(diff_payload["user_id"], 7)
        self.assertEqual(diff_payload["downloaders"], {"panda_archive": 10})
        self.assertNotIn("matchers", diff_payload)
        self.assertNotIn("providers", diff_payload)
        self.assertNotIn("elasticsearch", diff_payload)

        # Same object returns minimal dict (only per-task user_id if set)
        same_payload = serialize_settings_options(base, base_settings=base)
        self.assertEqual(same_payload, {})

        # Round-trip deserialization reproduces overrides and attaches _base_settings
        hydrated = deserialize_settings_options(base, diff_payload)
        self.assertIs(getattr(hydrated, "_base_settings", None), base)
        self.assertTrue(hydrated.redownload)
        self.assertEqual(hydrated.archive_reason, "manual_test")
        self.assertEqual(hydrated.downloaders["panda_archive"], 10)
        self.assertEqual(hydrated.downloaders["panda_torrent"], 3)

    def test_taskqueue_partial_indexes_and_redundant_viewer_indexes_removed(self) -> None:
        tq_indexes = {idx.name: idx for idx in TaskQueue._meta.indexes}
        self.assertIn("taskqueue_pending_order_idx", tq_indexes)
        self.assertIn("taskqueue_processing_type_idx", tq_indexes)
        self.assertIn("taskqueue_finished_order_idx", tq_indexes)
        self.assertIsNotNone(tq_indexes["taskqueue_pending_order_idx"].condition)
        self.assertIsNotNone(tq_indexes["taskqueue_processing_type_idx"].condition)
        self.assertIsNotNone(tq_indexes["taskqueue_finished_order_idx"].condition)
        self.assertEqual(list(tq_indexes["taskqueue_finished_order_idx"].fields), ["-finished_at", "-id"])

        archive_idx_names = {idx.name for idx in Archive._meta.indexes}
        for dropped in ("archive_pub_binned", "archive_pub2_binned", "archive_binned_only"):
            self.assertNotIn(dropped, archive_idx_names)
        self.assertIn("archive_binned_pub_date_idx", archive_idx_names)
        self.assertIn("archive_binned_create_idx", archive_idx_names)

        gallery_idx_names = {idx.name for idx in Gallery._meta.indexes}
        for dropped in ("gallery_pub_norm_create_date", "gallery_pub_norm_pub_date"):
            self.assertNotIn(dropped, gallery_idx_names)
        self.assertIn("gallery_stat_pub_posted_idx", gallery_idx_names)
        self.assertIn("gallery_stat_pub_create_idx", gallery_idx_names)

        self.assertEqual(ItemProperties._meta.indexes, [])


class MatchAgainstWantedOptimizationTests(SimpleTestCase):
    def test_empty_wanted_filters_queryset_does_not_fall_back_to_all(self) -> None:
        gallery = Gallery(title="Sample", provider="panda", gid="123")
        empty_qs = WantedGallery.objects.none()

        with (
            patch.object(WantedGallery.objects, "all", side_effect=AssertionError("Should not scan all WantedGallery")),
            patch.object(Gallery, "tag_list", side_effect=AssertionError("Should not load tags when 0 candidates")),
        ):
            matched = gallery.match_against_wanted_galleries(wanted_filters=empty_qs, skip_already_found=False)

        self.assertEqual(matched, [])

    def test_sync_gallery_skips_match_when_no_found_wanted_galleries(self) -> None:
        from core.base.gallery_sync import sync_gallery_db_entry

        cfg = Settings(load_from_disk=False)
        cfg.recheck_wanted_on_update = True
        general_utils = MagicMock()
        general_utils.discard_by_gallery_data.return_value = (False, [])

        fake_gallery = MagicMock()
        fake_gallery.tag_list.return_value = []
        fake_gallery.archive_set.all.return_value = []

        with (
            patch.object(Gallery.objects, "update_or_create_from_values", return_value=fake_gallery),
            patch.object(WantedGallery.objects, "filter", return_value=[]),
        ):
            sync_gallery_db_entry(
                cfg,
                general_utils,
                GalleryData(gid="123", provider="panda", title="T"),
                update_existing=True,
            )

        fake_gallery.match_against_wanted_galleries.assert_not_called()

    def test_match_against_wanted_loads_gallery_tags_once_across_many_candidates(self) -> None:
        gallery = Gallery(title="Sample Title", provider="panda", gid="999")

        class _FakeTag:
            def __init__(self, label: str) -> None:
                self._label = label

            def __str__(self) -> str:
                return self._label

        tag_en = _FakeTag("language:english")
        tag_bad = _FakeTag("female:bad")

        wg1 = SimpleNamespace(
            wanted_tags=SimpleNamespace(all=lambda: [tag_en]),
            unwanted_tags=SimpleNamespace(all=lambda: []),
            wanted_tags_accept_if_none_scope="",
            wanted_tags_exclusive_scope=False,
            exclusive_scope_name="",
            match_expression="",
        )
        wg2 = SimpleNamespace(
            wanted_tags=SimpleNamespace(all=lambda: [tag_en]),
            unwanted_tags=SimpleNamespace(all=lambda: [tag_bad]),
            wanted_tags_accept_if_none_scope="",
            wanted_tags_exclusive_scope=False,
            exclusive_scope_name="",
            match_expression="",
        )

        fake_qs = MagicMock()
        fake_qs.annotate.return_value = fake_qs
        fake_qs.filter.return_value = fake_qs
        fake_qs.distinct.return_value = fake_qs
        fake_qs.prefetch_related.return_value = [wg1, wg2]

        with patch.object(Gallery, "tag_list", return_value=["language:english", "female:bad"]) as mock_tag_list:
            matched = gallery.match_against_wanted_galleries(wanted_filters=fake_qs, skip_already_found=False)

        self.assertEqual(matched, [wg1])
        mock_tag_list.assert_called_once()
