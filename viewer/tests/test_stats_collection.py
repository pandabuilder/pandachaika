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

    def test_stats_collection_aggregations_breakdown(self):
        """Test stats_collection properly aggregates providers, categories, reasons, and languages."""
        from viewer.models import Archive, Provider, WantedGallery

        provider_panda, _ = Provider.objects.get_or_create(slug="panda", defaults={"name": "Panda"})
        provider_fakku, _ = Provider.objects.get_or_create(slug="fakku", defaults={"name": "Fakku"})

        # Create galleries with categories and providers
        gal_fakku = Gallery.objects.create(
            title="Fakku Gallery",
            gid="2001",
            provider="fakku",
            category="Manga",
            filesize=5000,
            filecount=50,
            public=True,
        )
        self.gallery_en.category = "Manga"
        self.gallery_en.save()
        self.gallery_jp.category = "Doujinshi"
        self.gallery_jp.save()

        # Create archives with reasons linked to galleries
        Archive.objects.create(
            title="Archive 1",
            user=self.admin_user,
            gallery=self.gallery_en,
            reason="corrupted",
            filesize=10000,
            filecount=100,
        )
        Archive.objects.create(
            title="Archive 2",
            user=self.admin_user,
            gallery=gal_fakku,
            reason="replaced",
            filesize=5000,
            filecount=50,
        )

        # Create wanted gallery
        wanted = WantedGallery.objects.create(title="Wanted 1")
        wanted.wanted_providers.add(provider_panda)

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("viewer:stats-collection"))
        self.assertEqual(response.status_code, 200)

        # Providers
        providers = response.context["providers"]
        self.assertIn("panda", providers)
        self.assertIn("fakku", providers)
        self.assertEqual(providers["panda"]["galleries"], 5)  # 3 untranslated + 2 translated
        self.assertEqual(providers["panda"]["archives"], 1)
        self.assertEqual(providers["panda"]["wanted_galleries"], 1)
        self.assertEqual(providers["fakku"]["galleries"], 1)
        self.assertEqual(providers["fakku"]["archives"], 1)
        self.assertEqual(providers["fakku"]["wanted_galleries"], 0)

        # Categories
        categories = response.context["gallery_categories"]
        self.assertIn("Manga", categories)
        self.assertIn("Doujinshi", categories)
        self.assertEqual(categories["Manga"]["n_galleries"], 2)  # gallery_en + gal_fakku
        self.assertEqual(categories["Manga"]["gallery"]["filesize__sum"], 15000)
        self.assertEqual(categories["Doujinshi"]["n_galleries"], 1)
        self.assertEqual(categories["Doujinshi"]["gallery"]["filesize__sum"], 20000)

        # Reasons
        reasons = response.context["archive_reasons"]
        self.assertIn("corrupted", reasons)
        self.assertIn("replaced", reasons)
        self.assertEqual(reasons["corrupted"]["n_archives"], 1)
        self.assertEqual(reasons["corrupted"]["archive"]["filesize__sum"], 10000)
        self.assertEqual(reasons["replaced"]["n_archives"], 1)
        self.assertEqual(reasons["replaced"]["archive"]["filesize__sum"], 5000)

    def test_no_n_plus_one_queries_stats_collection(self):
        """Assert that query count in stats_collection does not grow with the number of categories/providers/reasons/languages."""
        from viewer.models import Archive, Provider, WantedGallery

        self.client.force_login(self.admin_user)

        # Initial baseline query count
        with self.assertNumQueries(30):
            response = self.client.get(reverse("viewer:stats-collection"))
            self.assertEqual(response.status_code, 200)

        # Add 5 more providers, categories, reasons, languages
        for i in range(5):
            provider_slug = f"provider_{i}"
            prov = Provider.objects.create(name=f"Provider {i}", slug=provider_slug)
            lang_tag = Tag.objects.create(scope="language", name=f"lang_{i}")
            g = Gallery.objects.create(
                title=f"Gallery {i}",
                gid=f"300{i}",
                provider=provider_slug,
                category=f"Category_{i}",
                filesize=1000 * (i + 1),
                filecount=10,
                public=True,
            )
            g.tags.add(lang_tag)
            Archive.objects.create(
                title=f"Archive {i}",
                user=self.admin_user,
                gallery=g,
                reason=f"reason_{i}",
                filesize=1000 * (i + 1),
                filecount=10,
            )
            wg = WantedGallery.objects.create(title=f"Wanted {i}")
            wg.wanted_providers.add(prov)

        # Query count MUST be identical (no N+1 loop queries)
        with self.assertNumQueries(30):
            response = self.client.get(reverse("viewer:stats-collection"))
            self.assertEqual(response.status_code, 200)

    @patch("viewer.views.head.search.crawler_settings.urls.enable_public_stats", True)
    def test_no_n_plus_one_queries_public_stats(self):
        """Assert that query count in public_stats does not grow with the number of categories/languages."""
        # Initial baseline query count
        with self.assertNumQueries(10):
            response = self.client.get(reverse("viewer:public-stats"))
            self.assertEqual(response.status_code, 200)

        # Add 5 more categories and languages
        for i in range(5):
            lang_tag = Tag.objects.create(scope="language", name=f"public_lang_{i}")
            g = Gallery.objects.create(
                title=f"Public Gallery {i}",
                gid=f"400{i}",
                provider="panda",
                category=f"PublicCategory_{i}",
                filesize=2000 * (i + 1),
                filecount=20,
                public=True,
            )
            g.tags.add(lang_tag)

        # Query count MUST be identical (no N+1 loop queries)
        with self.assertNumQueries(10):
            response = self.client.get(reverse("viewer:public-stats"))
            self.assertEqual(response.status_code, 200)
