from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from viewer.models import Gallery, WantedGallery, FoundGallery, GalleryMatch, Mention


class WantedGalleryPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_user = User.objects.create_user(username="staff_user", password="password123")
        cls.staff_user.is_staff = True
        cls.staff_user.save()

        cls.wanted = WantedGallery.objects.create(
            title="Paging Test Wanted Gallery",
            search_title="paging test",
            public=True,
        )

        # Create galleries for found galleries and possible matches
        cls.galleries = [
            Gallery.objects.create(
                title=f"Gallery {i}",
                gid=str(1000 + i),
                provider="test_provider",
            )
            for i in range(30)
        ]

        # Create 30 FoundGallery records
        for i in range(30):
            FoundGallery.objects.create(
                wanted_gallery=cls.wanted,
                gallery=cls.galleries[i],
            )

        # Create 30 GalleryMatch records
        for i in range(30):
            GalleryMatch.objects.create(
                wanted_gallery=cls.wanted,
                gallery=cls.galleries[i],
                match_accuracy=0.5 + (i * 0.01),
            )

        # Create 30 Mention records
        mentions = [
            Mention.objects.create(
                source=f"source_{i}",
                comment=f"comment {i}",
            )
            for i in range(30)
        ]
        cls.wanted.mentions.add(*mentions)

    def setUp(self):
        self.client = Client()
        self.client.login(username="staff_user", password="password123")

    def test_default_pagination_first_page(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Found Galleries
        found_galleries = response.context["found_galleries"]
        self.assertEqual(found_galleries.paginator.count, 30)
        self.assertEqual(found_galleries.paginator.num_pages, 2)
        self.assertEqual(found_galleries.number, 1)
        self.assertEqual(len(found_galleries), 25)

        # Possible Matches
        possible_matches = response.context["possible_matches"]
        self.assertEqual(possible_matches.paginator.count, 30)
        self.assertEqual(possible_matches.paginator.num_pages, 2)
        self.assertEqual(possible_matches.number, 1)
        self.assertEqual(len(possible_matches), 25)

        # Mentions
        mentions = response.context["mentions"]
        self.assertEqual(mentions.paginator.count, 30)
        self.assertEqual(mentions.paginator.num_pages, 2)
        self.assertEqual(mentions.number, 1)
        self.assertEqual(len(mentions), 25)

        # Check pagination controls present in HTML
        content = response.content.decode("utf-8")
        self.assertIn("Found galleries top pagination", content)
        self.assertIn("Found galleries bottom pagination", content)
        self.assertIn("Possible matches top pagination", content)
        self.assertIn("Possible matches bottom pagination", content)
        self.assertIn("Mentions top pagination", content)
        self.assertIn("Mentions bottom pagination", content)

    def test_found_galleries_second_page(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        response = self.client.get(f"{url}?found_page=2")
        self.assertEqual(response.status_code, 200)

        found_galleries = response.context["found_galleries"]
        self.assertEqual(found_galleries.number, 2)
        self.assertEqual(len(found_galleries), 5)

        # Other datasets stay on page 1
        self.assertEqual(response.context["possible_matches"].number, 1)
        self.assertEqual(response.context["mentions"].number, 1)

    def test_possible_matches_second_page(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        response = self.client.get(f"{url}?matches_page=2")
        self.assertEqual(response.status_code, 200)

        possible_matches = response.context["possible_matches"]
        self.assertEqual(possible_matches.number, 2)
        self.assertEqual(len(possible_matches), 5)

        # Other datasets stay on page 1
        self.assertEqual(response.context["found_galleries"].number, 1)
        self.assertEqual(response.context["mentions"].number, 1)

    def test_mentions_second_page(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        response = self.client.get(f"{url}?mentions_page=2")
        self.assertEqual(response.status_code, 200)

        mentions = response.context["mentions"]
        self.assertEqual(mentions.number, 2)
        self.assertEqual(len(mentions), 5)

        # Other datasets stay on page 1
        self.assertEqual(response.context["found_galleries"].number, 1)
        self.assertEqual(response.context["possible_matches"].number, 1)

    def test_independent_multi_dataset_pagination(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        response = self.client.get(f"{url}?found_page=2&matches_page=1&mentions_page=2")
        self.assertEqual(response.status_code, 200)

        self.assertEqual(response.context["found_galleries"].number, 2)
        self.assertEqual(response.context["possible_matches"].number, 1)
        self.assertEqual(response.context["mentions"].number, 2)

    def test_out_of_bounds_and_invalid_pages_handled_gracefully(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])

        # Far beyond max pages -> gracefully redirects/falls back to last page
        response = self.client.get(f"{url}?found_page=999&matches_page=-5&mentions_page=invalid")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["found_galleries"].number, 2)
        self.assertEqual(response.context["possible_matches"].number, 1)
        self.assertEqual(response.context["mentions"].number, 1)

    def test_custom_page_size_parameters(self):
        url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])

        # Global per_page / limit
        response = self.client.get(f"{url}?limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["found_galleries"].paginator.per_page, 10)
        self.assertEqual(response.context["found_galleries"].paginator.num_pages, 3)
        self.assertEqual(len(response.context["found_galleries"]), 10)

        # Per-dataset limits
        response = self.client.get(f"{url}?found_limit=5&matches_limit=15&mentions_limit=10")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["found_galleries"].paginator.per_page, 5)
        self.assertEqual(response.context["found_galleries"].paginator.num_pages, 6)
        self.assertEqual(response.context["possible_matches"].paginator.per_page, 15)
        self.assertEqual(response.context["possible_matches"].paginator.num_pages, 2)
        self.assertEqual(response.context["mentions"].paginator.per_page, 10)
        self.assertEqual(response.context["mentions"].paginator.num_pages, 3)

    def test_no_pagination_controls_when_items_fit_single_page(self):
        small_wanted = WantedGallery.objects.create(
            title="Small Wanted Gallery",
            public=True,
        )
        url = reverse("viewer:wanted-gallery", args=[small_wanted.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        content = response.content.decode("utf-8")
        self.assertNotIn("Found galleries top pagination", content)
        self.assertNotIn("Possible matches top pagination", content)
        self.assertNotIn("Mentions top pagination", content)
