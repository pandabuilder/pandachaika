from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from viewer.models import Gallery, Tag


class StatsCollectionTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_user",
            email="admin@example.com",
            password="testpassword",
        )

        self.lang_en = Tag.objects.create(scope="language", name="english")
        self.lang_jp = Tag.objects.create(scope="language", name="japanese")
        self.artist_tag = Tag.objects.create(scope="artist", name="some_artist")

        # Untranslated gallery 1 (no language tag)
        self.gallery_untranslated_1 = Gallery.objects.create(
            title="Untranslated Gallery 1",
            gid="1001",
            provider="panda",
            filesize=1000,
            filecount=10,
            public=True,
        )

        # Untranslated gallery 2 (no language tag, has non-language tag)
        self.gallery_untranslated_2 = Gallery.objects.create(
            title="Untranslated Gallery 2",
            gid="1002",
            provider="panda",
            filesize=2000,
            filecount=20,
            public=True,
        )
        self.gallery_untranslated_2.tags.add(self.artist_tag)

        # Untranslated gallery 3 (non-public)
        self.gallery_untranslated_private = Gallery.objects.create(
            title="Untranslated Gallery Private",
            gid="1003",
            provider="panda",
            filesize=4000,
            filecount=40,
            public=False,
        )

        # Translated gallery English
        self.gallery_en = Gallery.objects.create(
            title="English Gallery",
            gid="1004",
            provider="panda",
            filesize=10000,
            filecount=100,
            public=True,
        )
        self.gallery_en.tags.add(self.lang_en)

        # Translated gallery Japanese
        self.gallery_jp = Gallery.objects.create(
            title="Japanese Gallery",
            gid="1005",
            provider="panda",
            filesize=20000,
            filecount=200,
            public=True,
        )
        self.gallery_jp.tags.add(self.lang_jp)

    def test_stats_collection_untranslated_aggregation(self):
        """Test stats_collection properly aggregates untranslated galleries without including translated ones."""
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("viewer:stats-collection"))
        self.assertEqual(response.status_code, 200)

        gallery_languages = response.context["gallery_languages"]
        self.assertIn("untranslated", gallery_languages)
        self.assertIn("english", gallery_languages)
        self.assertIn("japanese", gallery_languages)

        untranslated = gallery_languages["untranslated"]
        # In stats_collection (private stats), all galleries (both public and private) are included
        # Total untranslated = gallery_untranslated_1 (1000, 10) + gallery_untranslated_2 (2000, 20) + gallery_untranslated_private (4000, 40) = 7000, 70
        self.assertEqual(untranslated["n_galleries"], 3)
        self.assertEqual(untranslated["gallery"]["filesize__sum"], 7000)
        self.assertEqual(untranslated["gallery"]["filecount__sum"], 70)
        self.assertEqual(untranslated["gallery"]["filesize__min"], 1000)
        self.assertEqual(untranslated["gallery"]["filesize__max"], 4000)

        # English gallery stats
        english = gallery_languages["english"]
        self.assertEqual(english["n_galleries"], 1)
        self.assertEqual(english["gallery"]["filesize__sum"], 10000)
        self.assertEqual(english["gallery"]["filecount__sum"], 100)

    @patch("viewer.views.head.search.crawler_settings.urls.enable_public_stats", True)
    def test_public_stats_untranslated_aggregation(self):
        """Test public_stats properly aggregates untranslated galleries for public galleries only."""
        response = self.client.get(reverse("viewer:public-stats"))
        self.assertEqual(response.status_code, 200)

        gallery_languages = response.context["gallery_languages"]
        self.assertIn("untranslated", gallery_languages)
        self.assertIn("english", gallery_languages)
        self.assertIn("japanese", gallery_languages)

        untranslated = gallery_languages["untranslated"]
        # In public_stats, only public galleries are included
        # Public untranslated = gallery_untranslated_1 (1000, 10) + gallery_untranslated_2 (2000, 20) = 3000, 30
        self.assertEqual(untranslated["n_galleries"], 2)
        self.assertEqual(untranslated["gallery"]["filesize__sum"], 3000)
        self.assertEqual(untranslated["gallery"]["filecount__sum"], 30)
        self.assertEqual(untranslated["gallery"]["filesize__min"], 1000)
        self.assertEqual(untranslated["gallery"]["filesize__max"], 2000)

        # English gallery stats
        english = gallery_languages["english"]
        self.assertEqual(english["n_galleries"], 1)
        self.assertEqual(english["gallery"]["filesize__sum"], 10000)
        self.assertEqual(english["gallery"]["filecount__sum"], 100)
