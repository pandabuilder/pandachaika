"""
This file demonstrates two different styles of tests (one doctest and one
unittest). These will both pass when you run "manage.py test".

Replace these with more appropriate tests for your application.
"""

from django.test import TestCase, Client
from django.contrib.auth.models import User
from django.urls import reverse

import json

from viewer.models import Tag, Archive, Gallery, WantedGallery, ArchiveTag, FoundGallery


class TagTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.test_user1 = User.objects.create_user(username="testuser1", password="12345")

        cls.gallery_tag1 = Tag.objects.create(name="sister", scope="female")
        cls.gallery_tag2 = Tag.objects.create(name="hisasi", scope="artist")
        cls.archive_tag1 = Tag.objects.create(name="distance", scope="artist")
        cls.custom_tag = Tag.objects.create(scope="custom", name="adjective", source="user")
        cls.test_gallery1 = Gallery.objects.create(title="sample non public gallery 1", gid="344", provider="panda")
        cls.test_gallery1.tags.set([cls.gallery_tag1, cls.gallery_tag2])
        cls.archive1 = Archive.objects.create(title="sample Archive", user=cls.test_user1)
        ArchiveTag.objects.create(
            archive=cls.archive1, tag=cls.archive_tag1, origin=ArchiveTag.ORIGIN_SYSTEM
        )

    def test_first_artist_tag(self):
        """Test obtain first artist tag"""
        artist_tag = Tag.objects.first_artist_tag()
        self.assertIsNotNone(artist_tag)

    def test_tag_formatting(self):
        """Test default tag formatting"""
        tag = Tag.objects.get(scope="female", name="sister")
        self.assertIn(":", str(tag))

    def test_preserve_custom_tag_on_gallery_update(self):
        archive_custom_tag = ArchiveTag(archive=self.archive1, tag=self.custom_tag, origin=ArchiveTag.ORIGIN_USER)
        archive_custom_tag.save()
        self.archive1.set_tags_from_gallery(self.test_gallery1)

        all_tags = self.archive1.tags.all()

        self.assertIn(archive_custom_tag.tag, all_tags)
        self.assertListEqual(list(all_tags), [archive_custom_tag.tag] + list(self.test_gallery1.tags.all()))

        self.archive1.set_tags_from_gallery(self.test_gallery1, preserve_custom=False)

        self.assertQuerySetEqual(self.archive1.tags.all(), self.test_gallery1.tags.all())


class PrivateURLsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.test_admin1 = User.objects.create_user(username="admin1", password="12345")
        cls.test_admin1.is_staff = True
        cls.test_admin1.save()

        cls.test_user1 = User.objects.create_user(username="testuser1", password="12345")

        cls.tag1 = Tag.objects.create(name="sister", scope="female")
        cls.tag2 = Tag.objects.create(name="hisasi", scope="artist")
        cls.tag3 = Tag.objects.create(name="fue", scope="artist")
        cls.tag4 = Tag.objects.create(name="anzuame", scope="artist")
        cls.tag_english = Tag.objects.create(name="english", scope="language")

        cls.tag_custom1 = Tag.objects.create(name="special_edition", scope="")
        cls.tag_custom2 = Tag.objects.create(name="limited_edition", scope="")

        cls.test_gallery1 = Gallery.objects.create(
            title="sample non public gallery 1", gid="344", provider="panda", category="Manga"
        )
        cls.test_gallery1.tags.add(cls.tag1, cls.tag2, cls.tag_english)
        cls.test_gallery2 = Gallery.objects.create(
            title="sample non public gallery 2", gid="342", provider="test", category="Doujinshi"
        )
        cls.test_gallery2.tags.add(cls.tag1, cls.tag3, cls.tag_english)
        cls.test_gallery3 = Gallery.objects.create(
            title="sample non public gallery 3", gid="897", provider="test", category="Manga", public=True
        )
        cls.test_gallery3.tags.add(cls.tag1, cls.tag4)

        cls.test_book1 = Archive.objects.create(title="sample non public archive", user=cls.test_admin1)
        cls.test_book2 = Archive.objects.create(
            title="sample public archive", user=cls.test_admin1, public=True, gallery=cls.test_gallery2
        )
        cls.test_book3 = Archive.objects.create(
            title="new public archive sample", user=cls.test_admin1, public=True, gallery=cls.test_gallery3
        )
        cls.test_book4 = Archive.objects.create(
            title="new private archive sample", user=cls.test_admin1, public=False, gallery=cls.test_gallery1
        )

        ArchiveTag.objects.bulk_create(
            [
                ArchiveTag(archive=cls.test_book4, tag=cls.tag_custom1, origin=ArchiveTag.ORIGIN_USER),
                ArchiveTag(archive=cls.test_book4, tag=cls.tag_custom2, origin=ArchiveTag.ORIGIN_USER),
            ]
        )

        bulk_archives = []
        for i in range(3):
            bulk_archives.append(
                Archive(
                    title="new private archive sample {}".format(i + 1),
                    user=cls.test_admin1,
                    public=False,
                    gallery=cls.test_gallery3,
                )
            )
        for i in range(3):
            bulk_archives.append(
                Archive(
                    title="new private archive sample {}".format(i + 1),
                    user=cls.test_admin1,
                    public=False,
                    gallery=cls.test_gallery1,
                )
            )
        for i in range(50):
            bulk_archives.append(
                Archive(
                    title="new public archive sample {}".format(i + 1),
                    user=cls.test_admin1,
                    public=True,
                )
            )
        for i in range(10):
            bulk_archives.append(
                Archive(
                    title="new private file sample {}".format(i + 1),
                    user=cls.test_admin1,
                    public=False,
                )
            )
        cls.test_books = Archive.objects.bulk_create(bulk_archives)

    def test_redirect_if_not_logged_in(self):
        """Test to deny access to log page"""
        resp = self.client.get(reverse("viewer:logs"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith("/login/?next=/logs/"))

    def test_HTTP404_for_invalid_archive(self):
        self.client.login(username="testuser1", password="12345")
        resp = self.client.get(reverse("viewer:archive", args=[9999]))
        self.assertEqual(resp.status_code, 404)
        self.client.logout()
        resp = self.client.get(reverse("viewer:archive", args=[9999]))
        self.assertEqual(resp.status_code, 404)

    def test_HTTP404_for_non_public_archive_if_not_logged_in(self):
        resp = self.client.get(reverse("viewer:archive", args=[self.test_book1.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_for_public_archive_if_not_logged_in(self):
        resp = self.client.get(reverse("viewer:archive", args=[self.test_book2.pk]))
        self.assertEqual(resp.status_code, 200)

    def test_public_archive_search(self):
        response = self.client.get(reverse("viewer:archive-search"), {"title": "archive"})
        self.assertEqual(response.status_code, 200)
        # Check that pagination works (only 1 page)
        self.assertFalse(response.context["results"].has_next())
        self.assertFalse(response.context["results"].has_previous())
        # Check that the rendered context contains 52 archives.
        self.assertEqual(len(response.context["results"]), 52)

    def test_logged_in_archive_search(self):
        # Login, and get the 3 archives.
        c = Client()
        c.login(username="testuser1", password="12345")
        # List view
        response = c.get(reverse("viewer:archive-search"), {"title": "archive", "view": "list"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 60)
        self.assertFalse(response.context["results"].has_next())
        self.assertFalse(response.context["results"].has_previous())
        # Cover view
        response = c.get(reverse("viewer:archive-search"), {"title": "archive", "view": "cover"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 24)
        self.assertTrue(response.context["results"].has_next())
        self.assertFalse(response.context["results"].has_previous())
        self.assertEqual(response.context["results"].paginator.count, 60)
        # Extended view
        response = c.get(reverse("viewer:archive-search"), {"title": "archive", "view": "extended"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["results"]), 24)
        self.assertTrue(response.context["results"].has_next())
        self.assertFalse(response.context["results"].has_previous())
        self.assertEqual(response.context["results"].paginator.count, 60)

    def test_quick_search(self):
        # c.login(username='fred', password='secret')
        response = self.client.get(reverse("viewer:archive-search"), {"qsearch": "archive"})
        self.assertEqual(response.status_code, 200)
        # Check that pagination works (only 1 page)
        self.assertFalse(response.context["results"].has_next())
        self.assertFalse(response.context["results"].has_previous())
        # Check that the rendered context contains 2 archives.
        self.assertEqual(len(response.context["results"]), 52)


class GeneralPagesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.test_admin1 = User.objects.create_user(username="admin1", password="12345")
        cls.test_admin1.is_staff = True
        cls.test_admin1.save()

        cls.test_user1 = User.objects.create_user(username="testuser1", password="12345")

        cls.tag1 = Tag.objects.create(name="sister", scope="female")
        cls.tag2 = Tag.objects.create(name="hisasi", scope="artist")
        cls.tag3 = Tag.objects.create(name="fue", scope="artist")
        cls.tag4 = Tag.objects.create(name="anzuame", scope="artist")
        cls.tag_english = Tag.objects.create(name="english", scope="language")

        cls.test_gallery1 = Gallery.objects.create(
            title="sample non public gallery 1", gid="344", provider="panda", category="Manga"
        )
        cls.test_gallery1.tags.add(cls.tag1, cls.tag2, cls.tag_english)
        cls.test_gallery2 = Gallery.objects.create(
            title="sample non public gallery 2", gid="342", provider="test", category="Doujinshi"
        )
        cls.test_gallery2.tags.add(cls.tag1, cls.tag3, cls.tag_english)
        cls.test_gallery3 = Gallery.objects.create(
            title="sample non public gallery 3", gid="897", provider="test", category="Manga", public=True
        )
        cls.test_gallery3.tags.add(cls.tag1, cls.tag4)

        cls.test_book1 = Archive.objects.create(title="archive 1", user=cls.test_admin1, gallery=cls.test_gallery1)
        cls.test_book1b = Archive.objects.create(title="archive 1b", user=cls.test_admin1, gallery=cls.test_gallery1)
        cls.test_book2 = Archive.objects.create(
            title="archive 2", user=cls.test_admin1, gallery=cls.test_gallery2, public=True
        )
        cls.test_book3 = Archive.objects.create(title="archive 3", user=cls.test_admin1, public=True)
        cls.test_book4 = Archive.objects.create(
            title="book 4", user=cls.test_admin1, gallery=cls.test_gallery2, public=True
        )
        cls.test_book5 = Archive.objects.create(
            title="archive 5", user=cls.test_admin1, gallery=cls.test_gallery2, public=True
        )

        cls.test_wanted_gallery1 = WantedGallery.objects.create(
            title="test wanted gallery",
            title_jpn="テスト募集ギャラリー",
            search_title="sample non",
            book_type="Manga",
            page_count=23,
            publisher="wanimagazine",
            reason="wanimagazine",
            public=False,
            should_search=True,
            keep_searching=False,
            notify_when_found=False,
        )
        cls.test_wanted_gallery2 = WantedGallery.objects.create(
            title="test wanted gallery 2",
            title_jpn="テスト募集ギャラリー",
            search_title="public gallery",
            book_type="Doujinshi",
            page_count=50,
            publisher="wanimagazine",
            reason="wanimagazine",
            public=True,
            should_search=True,
            keep_searching=False,
            notify_when_found=False,
        )

    def test_repeated_archives(self):
        c = Client()
        c.login(username="admin1", password="12345")
        response = c.get(reverse("viewer:repeated-archives"), {"title": "gallery"})
        self.assertEqual(response.status_code, 200)
        # Check that pagination works (only 1 page)
        self.assertFalse(response.context["results"].has_next())
        self.assertFalse(response.context["results"].has_previous())
        # Check that the rendered context contains 2 archives.
        self.assertEqual(len(response.context["results"]), 2)
        self.assertEqual(response.context["results"][0].archive_set.count(), 3)
        self.assertEqual(response.context["results"][1].archive_set.count(), 2)

        # Test delete
        response = c.post(
            reverse("viewer:repeated-archives"), {"del-{}".format(self.test_gallery1.pk): [self.test_book1b.pk]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Archive.objects.count(), 5)
        self.assertEqual(len(response.context["results"]), 1)

        # Tools page
        response = c.get(reverse("viewer:tools"))
        self.assertEqual(response.status_code, 200)

    def test_main_pages_anonymous(self):
        c = Client()
        # c.login(username='admin1', password='12345')
        response = c.get(reverse("viewer:archive-search"))
        self.assertEqual(response.status_code, 200)
        response = c.get(reverse("viewer:archive-search"), {"view": "cover"})
        self.assertEqual(response.status_code, 200)
        response = c.get(reverse("viewer:archive-search"), {"view": "extended"})
        self.assertEqual(response.status_code, 200)

        response = c.get(reverse("viewer:gallery-list"))
        self.assertEqual(response.status_code, 200)

        response = c.get(reverse("viewer:public-missing-archives"))
        self.assertEqual(response.status_code, 200)

        response = c.get(reverse("viewer:wanted-galleries"))
        self.assertEqual(response.status_code, 200)

        # Depends on settings.yaml
        # response = c.get(reverse('viewer:url-submit'))
        # self.assertEqual(response.status_code, 200)

        response = c.get(reverse("viewer:about"))
        self.assertEqual(response.status_code, 200)

    def test_element_pages_anonymous(self):
        c = Client()
        # c.login(username='admin1', password='12345')
        c_normal = Client()
        c_normal.login(username="testuser1", password="12345")
        c_staff = Client()
        c_staff.login(username="admin1", password="12345")
        response = c.get(reverse("viewer:archive", args=[self.test_book2.pk]))
        self.assertEqual(response.status_code, 200)
        response = c.get(reverse("viewer:gallery", args=[self.test_gallery3.pk]))
        self.assertEqual(response.status_code, 200)

        response = c.get(reverse("viewer:wanted-gallery", args=[self.test_wanted_gallery1.pk]))
        self.assertEqual(response.status_code, 302)
        response = c_normal.get(reverse("viewer:wanted-gallery", args=[self.test_wanted_gallery1.pk]))
        self.assertEqual(response.status_code, 404)
        response = c_staff.get(reverse("viewer:wanted-gallery", args=[self.test_wanted_gallery1.pk]))
        self.assertEqual(response.status_code, 200)
        response = c_staff.get(reverse("viewer:wanted-gallery", args=[99999]))
        self.assertEqual(response.status_code, 404)
        response = c_normal.get(reverse("viewer:wanted-gallery", args=[self.test_wanted_gallery2.pk]))
        self.assertEqual(response.status_code, 200)


class JsonApiEndpointsTest(GeneralPagesTest):
    """GET /api handlers: gid, gids, wanted-gallery, wanted-galleries."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        FoundGallery.objects.create(
            wanted_gallery=cls.test_wanted_gallery2,
            gallery=cls.test_gallery3,
        )

    def _api_get(self, client, **params):
        return client.get(reverse("viewer:api"), params)

    def test_api_gid_public_gallery_anonymous(self):
        response = self._api_get(
            self.client,
            gid=self.test_gallery3.gid,
            provider=self.test_gallery3.provider,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["gid"], self.test_gallery3.gid)
        self.assertEqual(data["title"], self.test_gallery3.title)

    def test_api_gid_private_gallery_anonymous_returns_404(self):
        response = self._api_get(
            self.client,
            gid=self.test_gallery1.gid,
            provider=self.test_gallery1.provider,
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("does not exist", json.loads(response.content)["result"])

    def test_api_gid_private_gallery_authenticated(self):
        client = Client()
        client.login(username="testuser1", password="12345")
        response = self._api_get(
            client,
            gid=self.test_gallery1.gid,
            provider=self.test_gallery1.provider,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["gid"], self.test_gallery1.gid)

    def test_api_gids_returns_only_accessible_galleries(self):
        response = self._api_get(
            self.client,
            gids=[self.test_gallery1.gid, self.test_gallery3.gid],
            provider=self.test_gallery3.provider,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["gid"], self.test_gallery3.gid)

    def test_api_gids_authenticated_returns_multiple(self):
        client = Client()
        client.login(username="testuser1", password="12345")
        response = self._api_get(
            client,
            gids=[self.test_gallery1.gid, self.test_gallery3.gid],
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        gids = {item["gid"] for item in data}
        self.assertEqual(gids, {self.test_gallery1.gid, self.test_gallery3.gid})

    def test_api_wanted_gallery_public_anonymous(self):
        response = self._api_get(self.client, **{"wanted-gallery": self.test_wanted_gallery2.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.test_wanted_gallery2.pk)
        self.assertTrue(data["public"])

    def test_api_wanted_gallery_private_anonymous_returns_404(self):
        response = self._api_get(self.client, **{"wanted-gallery": self.test_wanted_gallery1.pk})
        self.assertEqual(response.status_code, 404)

    def test_api_wanted_gallery_private_authenticated(self):
        client = Client()
        client.login(username="testuser1", password="12345")
        response = self._api_get(client, **{"wanted-gallery": self.test_wanted_gallery1.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.test_wanted_gallery1.pk)
        self.assertFalse(data["public"])

    def test_api_wanted_galleries_by_id(self):
        client = Client()
        client.login(username="testuser1", password="12345")
        response = self._api_get(
            client,
            **{
                "wanted-galleries": [
                    self.test_wanted_gallery1.pk,
                    self.test_wanted_gallery2.pk,
                ]
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        ids = {item["id"] for item in data}
        self.assertEqual(ids, {self.test_wanted_gallery1.pk, self.test_wanted_gallery2.pk})

    def test_api_wanted_gallery_include_found_galleries(self):
        response = self._api_get(
            self.client,
            **{
                "wanted-gallery": self.test_wanted_gallery2.pk,
                "include_found_galleries": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data["found_galleries"]), 1)
        self.assertEqual(data["found_galleries"][0]["gid"], self.test_gallery3.gid)
