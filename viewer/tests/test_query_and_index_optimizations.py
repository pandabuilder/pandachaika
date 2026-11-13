from unittest.mock import MagicMock
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, RequestFactory

from viewer.models import Archive, ArchiveFileEntry, ArchiveTag, Gallery, Tag
from viewer.templatetags.viewer_extras import changes_archive_delta, changes_gallery_delta
from viewer.views.head.search import filter_archives, filter_archives_simple
from viewer.feeds import LatestArchivesFeed


class DatabaseIndexesOptimizationTest(TestCase):
    def test_model_indexes_defined(self):
        # Verify indexes defined on Archive
        archive_index_names = [idx.name for idx in Archive._meta.indexes]
        self.assertIn("archive_binned_pub_date_idx", archive_index_names)
        self.assertIn("archive_binned_create_idx", archive_index_names)
        self.assertIn("archive_source_type_idx", archive_index_names)
        self.assertIn("archive_reason_idx", archive_index_names)

        # Verify indexes defined on Gallery
        gallery_index_names = [idx.name for idx in Gallery._meta.indexes]
        self.assertIn("gallery_stat_pub_posted_idx", gallery_index_names)
        self.assertIn("gallery_stat_pub_create_idx", gallery_index_names)

        # Verify index defined on Tag
        tag_index_names = [idx.name for idx in Tag._meta.indexes]
        self.assertIn("tag_name_idx", tag_index_names)

        # Verify index defined on ArchiveTag
        archivetag_index_names = [idx.name for idx in ArchiveTag._meta.indexes]
        self.assertIn("archivetag_tag_arch_idx", archivetag_index_names)

    def test_indexes_exist_in_db(self):
        expected_indexes = {
            "tag_name_idx": "viewer_tag",
            "archive_binned_pub_date_idx": "viewer_archive",
            "archive_binned_create_idx": "viewer_archive",
            "archive_source_type_idx": "viewer_archive",
            "archive_reason_idx": "viewer_archive",
            "archivetag_tag_arch_idx": "viewer_archivetag",
            "gallery_stat_pub_posted_idx": "viewer_gallery",
            "gallery_stat_pub_create_idx": "viewer_gallery",
        }

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    i.relname AS index_name,
                    t.relname AS table_name
                FROM pg_class i
                JOIN pg_index idx ON idx.indexrelid = i.oid
                JOIN pg_class t ON t.oid = idx.indrelid
                WHERE i.relname = ANY(%s);
                """,
                [list(expected_indexes.keys())],
            )
            rows = cursor.fetchall()

        found = {row[0]: row[1] for row in rows}
        for index_name, table_name in expected_indexes.items():
            self.assertIn(index_name, found, f"Index {index_name} was not found in the database")
            self.assertEqual(found[index_name], table_name)


class QueryOptimizationsTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user, _ = User.objects.get_or_create(id=1, defaults={"username": "testuser", "is_staff": True})
        self.tag_parody = Tag.objects.create(name="touhou_project", scope="parody")
        self.tag_lang = Tag.objects.create(name="translated", scope="language")
        self.gallery = Gallery.objects.create(
            title="Test Gallery",
            provider="ex",
            gid=999991,
            public=True,
            status=Gallery.StatusChoices.NORMAL,
        )
        self.archive = Archive.objects.create(
            title="Test Archive",
            gallery=self.gallery,
            user=self.user,
            public=True,
            binned=False,
            source_type="test",
            reason="irodori",
        )
        ArchiveTag.objects.create(archive=self.archive, tag=self.tag_parody, origin=ArchiveTag.ORIGIN_SYSTEM)
        ArchiveTag.objects.create(archive=self.archive, tag=self.tag_lang, origin=ArchiveTag.ORIGIN_USER)
        ArchiveFileEntry.objects.create(archive=self.archive, file_name="001.jpg", file_size=1024)
        ArchiveFileEntry.objects.create(archive=self.archive, file_name="002.jpg", file_size=2048)

    def test_negative_tags_do_not_trigger_distinct(self):
        params = {"tags": "-language:translated", "title": "", "reason": "irodori"}
        qs_simple = filter_archives_simple(params, authenticated=True)
        sql_simple = str(qs_simple.query)
        self.assertNotIn("DISTINCT", sql_simple)

        request = self.factory.get("/search/")
        request.user = self.user
        qs_full = filter_archives(request, {}, params, authenticated=True)
        sql_full = str(qs_full.query)
        self.assertNotIn("DISTINCT", sql_full)

    def test_positive_tags_trigger_distinct(self):
        params = {"tags": "parody:touhou_project", "title": ""}
        qs_simple = filter_archives_simple(params, authenticated=True)
        sql_simple = str(qs_simple.query)
        self.assertIn("DISTINCT", sql_simple)

        request = self.factory.get("/search/")
        request.user = self.user
        qs_full = filter_archives(request, {}, params, authenticated=True)
        sql_full = str(qs_full.query)
        self.assertIn("DISTINCT", sql_full)

    def test_change_log_cached_prev_record(self):
        mock_prev = MagicMock()
        mock_prev.title = "Old Title"

        mock_curr = MagicMock()
        mock_curr.title = "New Title"
        mock_curr.cached_prev_record = mock_prev

        diff_result = MagicMock()
        diff_result.changes = [{"field": "title", "old": "Old Title", "new": "New Title"}]
        mock_curr.diff_against.return_value = diff_result

        # Test archive changes filter
        changes = changes_archive_delta(mock_curr)
        self.assertEqual(len(changes), 1)
        mock_curr.diff_against.assert_called_once()
        self.assertFalse(mock_curr.prev_record.called)

        # Test gallery changes filter
        mock_curr.diff_against.reset_mock()
        mock_curr.prev_record.reset_mock()
        changes = changes_gallery_delta(mock_curr, is_authenticated=True)
        self.assertEqual(len(changes), 1)
        mock_curr.diff_against.assert_called_once()
        self.assertFalse(mock_curr.prev_record.called)

    def test_archive_details_optimizations(self):
        self.client.force_login(self.user)
        response = self.client.get(f"/archive/{self.archive.pk}/")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(response.context["tag_count"], 1)
        self.assertEqual(response.context["custom_tag_count"], 1)
        self.assertEqual(response.context["file_entry_total"], 3072)

    def test_gallery_details_select_related(self):
        self.client.force_login(self.user)
        response = self.client.get(f"/gallery/{self.gallery.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["gallery"].pk, self.gallery.pk)

    def test_latest_archives_feed_items_slice_and_prefetch(self):
        feed = LatestArchivesFeed()
        request = self.factory.get("/feed/")
        request.user = self.user

        with self.assertNumQueries(2):
            items = list(feed.items(request))
            self.assertGreaterEqual(len(items), 1)
            tags_str = feed.item_description(items[0])
            self.assertIn("touhou_project", tags_str)
