"""
This file demonstrates two different styles of tests (one doctest and one
unittest). These will both pass when you run "manage.py test".

Replace these with more appropriate tests for your application.
"""

from django.test import TestCase, Client
from django.contrib.auth.models import User, Permission
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile

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

    def test_wanted_gallery_permissions_and_aliases(self):
        from django.contrib.auth.models import Permission

        collab_user = User.objects.create_user(username="collab1", password="12345")
        perm_view = Permission.objects.get(codename="view_wantedgallery")
        collab_user.user_permissions.add(perm_view)

        c_collab = Client()
        c_collab.login(username="collab1", password="12345")

        # Collaborator can view private wanted gallery
        response = c_collab.get(reverse("viewer:wanted-gallery", args=[self.test_wanted_gallery1.pk]))
        self.assertEqual(response.status_code, 200)

        # Alias col-wanted-gallery works and renders the same view
        response = c_collab.get(reverse("viewer:col-wanted-gallery", args=[self.test_wanted_gallery1.pk]))
        self.assertEqual(response.status_code, 200)

        # Collaborator can access wanted-galleries list
        response = c_collab.get(reverse("viewer:wanted-galleries"))
        self.assertEqual(response.status_code, 200)

        # Alias col-wanted-galleries works
        response = c_collab.get(reverse("viewer:col-wanted-galleries"))
        self.assertEqual(response.status_code, 200)

        # Anonymous can view wanted-galleries (public list)
        c_anon = Client()
        response = c_anon.get(reverse("viewer:wanted-galleries"))
        self.assertEqual(response.status_code, 200)

        # Collaborator with change permission can edit
        perm_change = Permission.objects.get(codename="change_wantedgallery")
        collab_user.user_permissions.add(perm_change)
        response = c_collab.post(
            reverse("viewer:wanted-gallery", args=[self.test_wanted_gallery1.pk]),
            {
                "submit-wanted-gallery": "Submit",
                "title": "updated title",
                "search_title": "sample non",
                "page_count": 25,
                "reason": "wanimagazine",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.test_wanted_gallery1.refresh_from_db()
        self.assertEqual(self.test_wanted_gallery1.title, "updated title")


class JsonApiEndpointsTest(GeneralPagesTest):
    """API handlers: gid, gids, wanted-gallery, wanted-galleries, wanted-galleries-search."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        FoundGallery.objects.create(
            wanted_gallery=cls.test_wanted_gallery2,
            gallery=cls.test_gallery3,
        )

        cls.perm_user_add = User.objects.create_user(username="perm_add", password="12345")
        cls.perm_user_change = User.objects.create_user(username="perm_change", password="12345")
        cls.perm_user_delete = User.objects.create_user(username="perm_delete", password="12345")
        cls.perm_user_all = User.objects.create_user(username="perm_all", password="12345")

        perm_add = Permission.objects.get(codename="add_wantedgallery", content_type__app_label="viewer")
        perm_change = Permission.objects.get(codename="change_wantedgallery", content_type__app_label="viewer")
        perm_delete = Permission.objects.get(codename="delete_wantedgallery", content_type__app_label="viewer")

        cls.perm_user_add.user_permissions.add(perm_add)
        cls.perm_user_change.user_permissions.add(perm_change)
        cls.perm_user_delete.user_permissions.add(perm_delete)
        cls.perm_user_all.user_permissions.add(perm_add, perm_change, perm_delete)

    def _api_get(self, client, **params):
        return client.get(reverse("viewer:api"), params)

    def _api_post(self, client, query_str="", body=None):
        url = reverse("viewer:api")
        if query_str:
            url = f"{url}?{query_str}"
        return client.post(url, data=json.dumps(body or {}), content_type="application/json")

    def _api_put(self, client, query_str="", body=None):
        url = reverse("viewer:api")
        if query_str:
            url = f"{url}?{query_str}"
        return client.put(url, data=json.dumps(body or {}), content_type="application/json")

    def _api_delete(self, client, query_str="", body=None):
        url = reverse("viewer:api")
        if query_str:
            url = f"{url}?{query_str}"
        return client.delete(url, data=json.dumps(body or {}), content_type="application/json")

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

    def test_api_wanted_gallery_post_create_success(self):
        client = Client()
        client.login(username="perm_add", password="12345")
        payload = {
            "title": "New Created Wanted Gallery",
            "title_jpn": "新規募集ギャラリー",
            "search_title": "Search Query",
            "should_search": True,
            "keep_searching": False,
            "notify_when_found": True,
            "public": True,
            "wait_for_time": 300,
            "wanted_tags": ["artist:new_artist", "female:twintails"],
            "category": "Manga",
            "reason": "Test Create",
        }
        response = self._api_post(client, "wanted-gallery=", payload)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertIn("id", data)

        created = WantedGallery.objects.get(pk=data["id"])
        self.assertEqual(created.title, "New Created Wanted Gallery")
        self.assertEqual(created.title_jpn, "新規募集ギャラリー")
        self.assertTrue(created.public)
        self.assertTrue(created.should_search)
        self.assertTrue(created.notify_when_found)
        self.assertEqual(created.wait_for_time.total_seconds(), 300)
        self.assertIn("artist:new_artist", created.wanted_tags_list())

    def test_api_wanted_gallery_post_unauthorized(self):
        # Unauthenticated
        response = self._api_post(self.client, "wanted-gallery=", {"title": "Denied"})
        self.assertEqual(response.status_code, 403)

        # Authenticated but missing add_wantedgallery permission
        client = Client()
        client.login(username="testuser1", password="12345")
        response = self._api_post(client, "wanted-gallery=", {"title": "Denied"})
        self.assertEqual(response.status_code, 403)

    def test_api_wanted_gallery_put_modify_success(self):
        client = Client()
        client.login(username="perm_change", password="12345")
        payload = {
            "title": "Modified Title Put",
            "should_search": False,
            "public": True,
            "notify_when_found": True,
            "wait_for_time": 600,
            "wanted_tags": ["artist:updated_artist"],
            "reason": "Updated Reason",
        }
        response = self._api_put(client, f"wanted-gallery={self.test_wanted_gallery1.pk}", payload)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["id"], self.test_wanted_gallery1.pk)
        self.assertEqual(data["title"], "Modified Title Put")
        self.assertTrue(data["public"])
        self.assertFalse(data["should_search"])
        self.assertEqual(data["wait_for_time"], 600)
        self.assertIn("artist:updated_artist", data["wanted_tags"])

        self.test_wanted_gallery1.refresh_from_db()
        self.assertEqual(self.test_wanted_gallery1.title, "Modified Title Put")
        self.assertTrue(self.test_wanted_gallery1.public)
        self.assertFalse(self.test_wanted_gallery1.should_search)
        self.assertEqual(self.test_wanted_gallery1.wait_for_time.total_seconds(), 600)
        self.assertIn("artist:updated_artist", self.test_wanted_gallery1.wanted_tags_list())

    def test_api_wanted_gallery_put_modify_partial(self):
        client = Client()
        client.login(username="perm_change", password="12345")
        original_title = self.test_wanted_gallery2.title
        payload = {"should_search": False}
        response = self._api_put(client, f"wanted-gallery={self.test_wanted_gallery2.pk}", payload)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], original_title)
        self.assertFalse(data["should_search"])

        self.test_wanted_gallery2.refresh_from_db()
        self.assertEqual(self.test_wanted_gallery2.title, original_title)
        self.assertFalse(self.test_wanted_gallery2.should_search)

    def test_api_wanted_gallery_put_with_id_in_body(self):
        client = Client()
        client.login(username="perm_change", password="12345")
        payload = {
            "id": self.test_wanted_gallery2.pk,
            "reason": "Updated Reason Via Body ID",
        }
        response = self._api_put(client, "wanted-gallery=", payload)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["reason"], "Updated Reason Via Body ID")

        self.test_wanted_gallery2.refresh_from_db()
        self.assertEqual(self.test_wanted_gallery2.reason, "Updated Reason Via Body ID")

    def test_api_wanted_gallery_put_not_found(self):
        client = Client()
        client.login(username="perm_change", password="12345")
        response = self._api_put(client, "wanted-gallery=999999", {"title": "Non-existent"})
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertIn("does not exist", data["result"])

    def test_api_wanted_gallery_put_unauthorized(self):
        # Unauthenticated
        response = self._api_put(self.client, f"wanted-gallery={self.test_wanted_gallery1.pk}", {"title": "Denied"})
        self.assertEqual(response.status_code, 403)

        # Authenticated without change_wantedgallery
        client = Client()
        client.login(username="perm_add", password="12345")
        response = self._api_put(client, f"wanted-gallery={self.test_wanted_gallery1.pk}", {"title": "Denied"})
        self.assertEqual(response.status_code, 403)

    def test_api_wanted_gallery_delete_success(self):
        temp = WantedGallery.objects.create(title="To be deleted", public=True)
        temp_pk = temp.pk

        client = Client()
        client.login(username="perm_delete", password="12345")
        response = self._api_delete(client, f"wanted-gallery={temp_pk}")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["id"], temp_pk)
        self.assertFalse(WantedGallery.objects.filter(pk=temp_pk).exists())

    def test_api_wanted_gallery_delete_not_found_and_unauthorized(self):
        # Unauthenticated
        response = self._api_delete(self.client, f"wanted-gallery={self.test_wanted_gallery1.pk}")
        self.assertEqual(response.status_code, 403)

        # Authorized but not found
        client = Client()
        client.login(username="perm_delete", password="12345")
        response = self._api_delete(client, "wanted-gallery=999999")
        self.assertEqual(response.status_code, 404)

    def test_api_wanted_galleries_search_include_found_galleries(self):
        response = self._api_get(
            self.client,
            **{
                "wanted-galleries-search": "",
                "title": "public gallery",
                "include_found_galleries": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertGreaterEqual(len(data), 1)
        found_entry = next((item for item in data if item["id"] == self.test_wanted_gallery2.pk), None)
        self.assertIsNotNone(found_entry)
        self.assertIn("found_galleries", found_entry)
        self.assertEqual(len(found_entry["found_galleries"]), 1)
        self.assertEqual(found_entry["found_galleries"][0]["gid"], self.test_gallery3.gid)


class OperationalFixesTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_user(username="admin_op", password="password")
        cls.admin_user.is_staff = True
        cls.admin_user.save()

        cls.normal_user = User.objects.create_user(username="user_op", password="password")

        cls.archive_without_thumb = Archive.objects.create(
            title="Archive No Thumb",
            user=cls.admin_user,
            public=True,
        )
        cls.gallery_without_thumb = Gallery.objects.create(
            title="Gallery No Thumb",
            gid="99901",
            provider="test",
            public=True,
        )

    def test_admin_api_status_code_unauthorized_and_invalid(self):
        c = Client()
        # 1. Anonymous user to tools-api -> 401
        response = c.get(reverse("viewer:tools-api", args=["main"]))
        self.assertEqual(response.status_code, 401)
        data = json.loads(response.content)
        self.assertEqual(data.get("error"), "Not allowed")

        # 2. Non-staff user to tools-api -> 401
        c.login(username="user_op", password="password")
        response = c.get(reverse("viewer:tools-api", args=["main"]))
        self.assertEqual(response.status_code, 401)
        data = json.loads(response.content)
        self.assertEqual(data.get("error"), "Not allowed")

        # 3. Staff user with invalid arguments -> 401
        c_staff = Client()
        c_staff.login(username="admin_op", password="password")
        response = c_staff.get(reverse("viewer:tools-api", args=["update_newer_than"]) + "?newer_than=invalid-date")
        self.assertEqual(response.status_code, 401)
        data = json.loads(response.content)
        self.assertEqual(data.get("error"), "Invalid date format.")

        response = c_staff.get(reverse("viewer:tools-api", args=["update_missing_thumbnails"]) + "?limit_number=notanumber")
        self.assertEqual(response.status_code, 401)
        data = json.loads(response.content)
        self.assertEqual(data.get("error"), "Invalid limit.")

    def test_thumbnail_missing_fallback(self):
        c = Client()
        # Archive with no thumbnail file
        response = c.get(reverse("viewer:archive-thumb", args=[self.archive_without_thumb.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/static/imgs/no_cover.png", response.url)

        # Gallery with no thumbnail file
        response = c.get(reverse("viewer:gallery-thumb", args=[self.gallery_without_thumb.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/static/imgs/no_cover.png", response.url)

    def test_elasticsearch_views_empty_json_response(self):
        from unittest.mock import patch
        from viewer.views.elasticsearch import (
            autocomplete_view,
            title_suggest_view,
            title_suggest_archive_view,
        )
        from django.test.client import RequestFactory

        rf = RequestFactory()
        req = rf.get("/elasticsearch/autocomplete/?q=test")

        with patch("viewer.views.elasticsearch.settings.ES_ENABLED", False):
            resp = autocomplete_view(req)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.headers.get("Content-Type", "").startswith("application/json"))
            self.assertEqual(json.loads(resp.content), {})

            resp = title_suggest_view(req)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.headers.get("Content-Type", "").startswith("application/json"))
            self.assertEqual(json.loads(resp.content), {})

            resp = title_suggest_archive_view(req)
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.headers.get("Content-Type", "").startswith("application/json"))
            self.assertEqual(json.loads(resp.content), {})


class GalleryAndArchiveViewsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.test_admin = User.objects.create_user(username="test_admin_views", password="password123", is_staff=True)
        cls.test_user = User.objects.create_user(username="test_user_views", password="password123")

        small_gif = (
            b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x00\x00\x00\x21\xf9\x04'
            b'\x01\x0a\x00\x01\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02'
            b'\x02\x4c\x01\x00\x3b'
        )
        thumb_file1 = SimpleUploadedFile("thumb1.gif", small_gif, content_type="image/gif")
        thumb_file2 = SimpleUploadedFile("thumb2.gif", small_gif, content_type="image/gif")

        cls.gallery_public = Gallery.objects.create(
            title="Sample Public Gallery",
            gid="1001",
            provider="panda",
            category="Manga",
            public=True,
            rating=4.5,
            comment="This is a test comment",
            thumbnail=thumb_file1,
        )
        cls.gallery_private = Gallery.objects.create(
            title="Sample Private Gallery",
            gid="1002",
            provider="panda",
            category="Non-H",
            public=False,
            disowned=True,
            expunged=True,
        )

        cls.archive_public = Archive.objects.create(
            title="Sample Public Archive",
            user=cls.test_admin,
            public=True,
            gallery=cls.gallery_public,
            crc32="ABCD1234",
            filecount=10,
            filesize=1048576,
            thumbnail=thumb_file2,
        )
        cls.archive_private = Archive.objects.create(
            title="Sample Private Archive",
            user=cls.test_admin,
            public=False,
            gallery=cls.gallery_private,
            crc32="EFGH5678",
            filecount=5,
            filesize=524288,
        )

    def test_gallery_view_anonymous_public(self):
        resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_public.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sample Public Gallery")
        self.assertContains(resp, "Public")
        self.assertNotContains(resp, "btn-toggle-inline-cover")
        self.assertNotContains(resp, "inline-cover-container")

    def test_gallery_view_anonymous_private_redirect_or_404(self):
        resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_private.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_gallery_view_authenticated_private(self):
        self.client.login(username="test_user_views", password="password123")
        resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_private.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sample Private Gallery")
        self.assertContains(resp, "Disowned")
        self.assertContains(resp, "Expunged")

    def test_archive_view_anonymous_public(self):
        resp = self.client.get(reverse("viewer:archive", args=[self.archive_public.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sample Public Archive")
        self.assertContains(resp, "ABCD1234")
        self.assertNotContains(resp, "btn-toggle-inline-cover")
        self.assertNotContains(resp, "inline-cover-container")

    def test_archive_view_anonymous_private_404(self):
        resp = self.client.get(reverse("viewer:archive", args=[self.archive_private.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_archive_view_authenticated_private(self):
        self.client.login(username="test_user_views", password="password123")
        resp = self.client.get(reverse("viewer:archive", args=[self.archive_private.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Sample Private Archive")
        self.assertContains(resp, "EFGH5678")

    def test_refined_views_opt_in_routing(self):
        from unittest.mock import patch
        from django.conf import settings

        # 1. When refined_archive_gallery_views is False (default):
        # Authenticated user gets old design
        self.client.login(username="test_user_views", password="password123")
        with patch.object(settings.CRAWLER_SETTINGS, "refined_archive_gallery_views", False):
            resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_public.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, "btn-toggle-inline-cover")

            resp = self.client.get(reverse("viewer:archive", args=[self.archive_public.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, "btn-toggle-inline-cover")

        # 2. When refined_archive_gallery_views is True:
        # Authenticated user gets new design
        with patch.object(settings.CRAWLER_SETTINGS, "refined_archive_gallery_views", True):
            resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_public.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "btn-toggle-inline-cover")
            self.assertContains(resp, "inline-cover-container")

            resp = self.client.get(reverse("viewer:archive", args=[self.archive_public.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "btn-toggle-inline-cover")
            self.assertContains(resp, "inline-cover-container")

            # 3. Authenticated user can explicitly choose old design via ?view_style=old
            resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_public.pk]) + "?view_style=old")
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, "btn-toggle-inline-cover")

            # 4. Anonymous user STILL gets old design even when setting is True
            self.client.logout()
            resp = self.client.get(reverse("viewer:gallery", args=[self.gallery_public.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, "btn-toggle-inline-cover")

            resp = self.client.get(reverse("viewer:archive", args=[self.archive_public.pk]))
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, "btn-toggle-inline-cover")


class TemplateTagsTest(TestCase):
    def test_boolean_badge_defaults(self):
        from viewer.templatetags.viewer_extras import boolean_badge
        self.assertIn("bg-success", boolean_badge(True))
        self.assertIn("Yes", boolean_badge(True))
        self.assertIn("bg-secondary", boolean_badge(False))
        self.assertIn("No", boolean_badge(False))

    def test_boolean_badge_custom_labels(self):
        from viewer.templatetags.viewer_extras import boolean_badge
        res = boolean_badge(True, "Public,Private")
        self.assertIn("bg-success", res)
        self.assertIn("Public", res)

        res_false = boolean_badge(False, "Public,Private")
        self.assertIn("bg-secondary", res_false)
        self.assertIn("Private", res_false)

    def test_boolean_badge_danger(self):
        from viewer.templatetags.viewer_extras import boolean_badge
        res = boolean_badge(True, "danger")
        self.assertIn("bg-danger", res)
        self.assertIn("Yes", res)

        res_custom = boolean_badge(True, "Expunged,Active,danger")
        self.assertIn("bg-danger", res_custom)
        self.assertIn("Expunged", res_custom)

    def test_boolean_text(self):
        from viewer.templatetags.viewer_extras import boolean_text
        self.assertIn("text-success", boolean_text(True))
        self.assertIn("text-secondary", boolean_text(False))
        self.assertIn("text-danger", boolean_text(True, "danger"))
