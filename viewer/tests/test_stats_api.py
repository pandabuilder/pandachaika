import json
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase

from viewer.models import Gallery, Tag
from viewer.views.stats import stats_api

User = get_user_model()


class StatsApiUnitTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User(username="testuser", email="test@example.com")

    def test_stats_api_heatmap_uses_values_list_instead_of_hydration(self):
        """Verify that stats_api uses .values_list('id', flat=True) for gallery_ids

        without hydrating Gallery model instances into memory.
        """
        request = self.factory.get("/api/stats/", {"chart": "heatmap"})
        request.user = self.user

        # Create mock filtered_galleries queryset
        mock_qs = MagicMock()
        mock_values_list = MagicMock()
        mock_qs.values_list.return_value = mock_values_list

        # Mock Tag queryset
        mock_tag = MagicMock()
        mock_tag.id = 1
        mock_tag.name = "female:sole_female"
        mock_top_tags = [mock_tag]

        with patch("viewer.views.stats.filter_galleries_simple", return_value=mock_qs), \
             patch.object(Tag.objects, "filter") as mock_tag_filter, \
             patch.object(Gallery.tags.through.objects, "filter") as mock_through_filter:

            mock_tag_filter.return_value.annotate.return_value.order_by.return_value.__getitem__.return_value = mock_top_tags

            mock_through_qs = MagicMock()
            mock_through_qs.values_list.return_value = [(101, 1)]
            mock_through_filter.return_value = mock_through_qs

            response = stats_api(request)

            # Ensure values_list('id', flat=True) was called
            mock_qs.values_list.assert_called_once_with("id", flat=True)
            # Ensure queryset was not iterated over, preventing hydration
            mock_qs.__iter__.assert_not_called()

            # Ensure ThroughModel filter received the gallery_ids from values_list
            mock_through_filter.assert_called_once_with(
                tag_id__in=[1],
                gallery_id__in=mock_values_list,
            )

            self.assertEqual(response.status_code, 200)
            data = json.loads(response.content)
            self.assertIn("heatmap", data)
            self.assertEqual(data["heatmap"]["tags"], ["female:sole_female"])
            self.assertEqual(len(data["heatmap"]["data"]), 1)
            self.assertEqual(data["heatmap"]["data"][0]["value"], 1)


class StatsApiDbTestCase(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            username="testuser",
            email="test@example.com",
            password="testpassword",
        )

    def test_stats_api_heatmap_with_db_data(self):
        """End-to-end test with test database records when database is available."""
        tag1 = Tag.objects.create(scope="female", name="sole_female")
        tag2 = Tag.objects.create(scope="artist", name="test_artist")

        gal1 = Gallery.objects.create(
            title="Gallery 1",
            gid="stats_gal_1",
            provider="panda",
            public=True,
        )
        gal1.tags.add(tag1, tag2)

        gal2 = Gallery.objects.create(
            title="Gallery 2",
            gid="stats_gal_2",
            provider="panda",
            public=True,
        )
        gal2.tags.add(tag1)

        request = self.factory.get("/api/stats/", {"chart": "heatmap"})
        request.user = self.user

        response = stats_api(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("heatmap", data)
        self.assertIn("female:sole_female", data["heatmap"]["tags"])
        self.assertIn("artist:test_artist", data["heatmap"]["tags"])
