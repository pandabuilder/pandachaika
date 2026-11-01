from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase

from viewer.models.archive import Archive
from viewer.models.gallery import Gallery
from viewer.models.wanted import WantedGallery


class PgTrgmGinIndexesTest(TestCase):
    def setUp(self):
        self.user, _ = User.objects.get_or_create(id=1, defaults={"username": "testuser"})

    def test_pg_trgm_extension_installed(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm';")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "pg_trgm")

    def test_gin_trgm_indexes_exist_in_db(self):
        expected_indexes = {
            "archive_title_trgm_gin": "viewer_archive",
            "archive_title_jpn_trgm_gin": "viewer_archive",
            "gallery_title_trgm_gin": "viewer_gallery",
            "gallery_title_jpn_trgm_gin": "viewer_gallery",
            "wanted_search_title_trgm_gin": "viewer_wantedgallery",
        }

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    i.relname AS index_name,
                    t.relname AS table_name,
                    am.amname AS index_type,
                    pg_get_indexdef(i.oid) AS index_def
                FROM pg_class i
                JOIN pg_index idx ON idx.indexrelid = i.oid
                JOIN pg_class t ON t.oid = idx.indrelid
                JOIN pg_am am ON am.oid = i.relam
                WHERE i.relname = ANY(%s);
                """,
                [list(expected_indexes.keys())],
            )
            rows = cursor.fetchall()

        found_indexes = {row[0]: (row[1], row[2], row[3]) for row in rows}

        for index_name, expected_table in expected_indexes.items():
            self.assertIn(index_name, found_indexes)
            table_name, index_type, index_def = found_indexes[index_name]
            self.assertEqual(table_name, expected_table)
            self.assertEqual(index_type, "gin")
            self.assertIn("gin_trgm_ops", index_def)

    def test_spaced_search_queries_and_plan(self):
        archive = Archive.objects.create(
            user=self.user,
            title="Magical Girl Adventure Vol 1",
            title_jpn="魔法少女アドベンチャー 第一巻",
        )
        gallery = Gallery.objects.create(
            title="Electric Dreams Collection",
            title_jpn="エレクトリック ドリームズ",
            gid=9999991,
            token="abc12345",
        )
        wanted = WantedGallery.objects.create(
            search_title="Special Search Anthology",
        )

        # Test SpacedSearch lookup functionality
        self.assertTrue(Archive.objects.filter(title__ss="%Girl%Adventure%").filter(pk=archive.pk).exists())
        self.assertTrue(Archive.objects.filter(title_jpn__ss="%魔法少女%第一巻%").filter(pk=archive.pk).exists())
        self.assertTrue(Gallery.objects.filter(title__ss="%Electric%Dreams%").filter(pk=gallery.pk).exists())
        self.assertTrue(Gallery.objects.filter(title_jpn__ss="%エレクトリック%").filter(pk=gallery.pk).exists())
        self.assertTrue(WantedGallery.objects.filter(search_title__ss="%Special%Anthology%").filter(pk=wanted.pk).exists())

        # Test that PostgreSQL optimizer chooses Bitmap Index Scan on GIN indexes when seq scans are disabled
        queries = [
            (Archive.objects.filter(title__ss="%Girl%"), "archive_title_trgm_gin"),
            (Archive.objects.filter(title_jpn__ss="%魔法少女%"), "archive_title_jpn_trgm_gin"),
            (Gallery.objects.filter(title__ss="%Electric%"), "gallery_title_trgm_gin"),
            (Gallery.objects.filter(title_jpn__ss="%エレクトリック%"), "gallery_title_jpn_trgm_gin"),
            (WantedGallery.objects.filter(search_title__ss="%Special%"), "wanted_search_title_trgm_gin"),
        ]

        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL enable_seqscan = off;")
            for qs, expected_index in queries:
                sql, params = qs.query.sql_with_params()
                cursor.execute(f"EXPLAIN {sql}", params)
                plan = "\n".join(row[0] for row in cursor.fetchall())
                self.assertIn(
                    expected_index,
                    plan,
                    f"Expected index {expected_index} in query plan:\n{plan}",
                )
