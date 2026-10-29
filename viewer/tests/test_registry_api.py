import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from viewer.models import (
    Archive,
    ArchiveFileEntry,
    ArchiveGroup,
    ArchiveGroupEntry,
    ArchiveTag,
    FoundGallery,
    Gallery,
    Image,
    Tag,
    UserLongLivedToken,
    WantedGallery,
)


class BaseRegistryApiMixin:
    @classmethod
    def setUpTestData(cls):
        # Users
        cls.superuser = User.objects.create_superuser(
            username=f"super_api_{cls.__name__}",
            email=f"super_{cls.__name__}@example.com",
            password="password123",
        )
        cls.regular_user = User.objects.create_user(
            username=f"regular_api_{cls.__name__}",
            password="password123",
        )

        # Long-lived token setup
        cls.token_str = f"valid_token_{cls.__name__}"
        salted_key = UserLongLivedToken.create_salted_key_from_key(cls.token_str)
        UserLongLivedToken.objects.create(
            user=cls.superuser,
            name=f"valid_token_name_{cls.__name__}",
            key=salted_key,
            expire_date=timezone.now() + timedelta(days=30),
        )

        cls.expired_token_str = f"expired_token_{cls.__name__}"
        expired_salted_key = UserLongLivedToken.create_salted_key_from_key(cls.expired_token_str)
        UserLongLivedToken.objects.create(
            user=cls.regular_user,
            name=f"expired_token_name_{cls.__name__}",
            key=expired_salted_key,
            expire_date=timezone.now() - timedelta(days=1),
        )

    def setUp(self):
        self.anon_client = Client()
        self.auth_client = Client()
        self.auth_client.login(username=self.superuser.username, password="password123")
        self.regular_client = Client()
        self.regular_client.login(username=self.regular_user.username, password="password123")

    def _api_get(self, client=None, path=None, **params):
        client = client or self.anon_client
        url = path or reverse("viewer:api")
        return client.get(url, params)

    def _api_post(self, client=None, path=None, query_str="", body=None, **headers):
        client = client or self.anon_client
        url = path or reverse("viewer:api")
        if query_str:
            url = f"{url}?{query_str}"
        return client.post(
            url,
            data=json.dumps(body or {}) if not isinstance(body, (str, bytes)) else body,
            content_type="application/json",
            **headers,
        )

    def _api_put(self, client=None, path=None, query_str="", body=None, **headers):
        client = client or self.anon_client
        url = path or reverse("viewer:api")
        if query_str:
            url = f"{url}?{query_str}"
        return client.put(
            url,
            data=json.dumps(body or {}) if not isinstance(body, (str, bytes)) else body,
            content_type="application/json",
            **headers,
        )

    def _api_delete(self, client=None, path=None, query_str="", body=None, **headers):
        client = client or self.anon_client
        url = path or reverse("viewer:api")
        if query_str:
            url = f"{url}?{query_str}"
        return client.delete(
            url,
            data=json.dumps(body or {}) if not isinstance(body, (str, bytes)) else body,
            content_type="application/json",
            **headers,
        )


class RegistryApiDispatchTests(BaseRegistryApiMixin, TestCase):
    """Tests for routing, dispatching, HTTP methods, and bearer token auth."""

    def test_unknown_command_get(self):
        response = self._api_get(self.anon_client, non_existent_key="123")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Unknown command")

    def test_unknown_command_post_authenticated(self):
        response = self._api_post(self.auth_client, query_str="unknown_cmd=1", body={"key": "val"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Unknown command")

    def test_unknown_command_put_authenticated(self):
        response = self._api_put(self.auth_client, query_str="unknown_cmd=1", body={"key": "val"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Unknown command")

    def test_unknown_command_delete_authenticated(self):
        response = self._api_delete(self.auth_client, query_str="unknown_cmd=1")
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Unknown command")

    def test_unauthenticated_post_returns_403(self):
        response = self._api_post(self.anon_client, query_str="wanted-gallery=", body={"title": "Test"})
        self.assertEqual(response.status_code, 403)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Not authorized")

    def test_unauthenticated_put_returns_403(self):
        response = self._api_put(self.anon_client, query_str="wanted-gallery=1", body={"title": "Test"})
        self.assertEqual(response.status_code, 403)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Not authorized")

    def test_unauthenticated_delete_returns_403(self):
        response = self._api_delete(self.anon_client, query_str="wanted-gallery=1")
        self.assertEqual(response.status_code, 403)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Not authorized")

    def test_invalid_json_body_post(self):
        url = f"{reverse('viewer:api')}?wanted-gallery="
        response = self.auth_client.post(url, data="invalid-json{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Invalid JSON body")

    def test_invalid_json_body_put(self):
        url = f"{reverse('viewer:api')}?wanted-gallery=1"
        response = self.auth_client.put(url, data="invalid-json{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Invalid JSON body")

    def test_unsupported_http_method(self):
        url = reverse("viewer:api")
        response = self.anon_client.patch(url)
        self.assertEqual(response.status_code, 405)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Unsupported request method")

    def test_bearer_token_authentication_valid(self):
        archive = Archive.objects.create(title="Bearer Token Archive", user=self.superuser, public=False)
        response = self.anon_client.get(
            reverse("viewer:api"),
            {"archive": archive.pk},
            HTTP_AUTHORIZATION=f"Bearer {self.token_str}",
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Bearer Token Archive")

    def test_bearer_token_authentication_expired(self):
        archive = Archive.objects.create(title="Bearer Token Expired Archive", user=self.regular_user, public=False)
        response = self.anon_client.get(
            reverse("viewer:api"),
            {"archive": archive.pk},
            HTTP_AUTHORIZATION=f"Bearer {self.expired_token_str}",
        )
        self.assertEqual(response.status_code, 404)

    def test_bearer_token_authentication_invalid(self):
        archive = Archive.objects.create(title="Bearer Token Invalid Archive", user=self.superuser, public=False)
        response = self.anon_client.get(
            reverse("viewer:api"),
            {"archive": archive.pk},
            HTTP_AUTHORIZATION="Bearer invalid_nonexistent_token",
        )
        self.assertEqual(response.status_code, 404)

    def test_jsearch_alias_routes_to_api(self):
        archive = Archive.objects.create(title="JSearch Public Archive", user=self.superuser, public=True)
        response = self._api_get(self.anon_client, path=reverse("viewer:json-search"), archive=archive.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "JSearch Public Archive")


class RegistryApiArchiveEndpointsTests(BaseRegistryApiMixin, TestCase):
    """Tests for archive-related GET endpoints in registry.py."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.tag_artist = Tag.objects.create(name="testartist", scope="artist")
        cls.tag_female = Tag.objects.create(name="testtag", scope="female")

        cls.gallery_pub = Gallery.objects.create(
            title="Gallery For Public Archive",
            gid="20001",
            provider="panda",
            public=True,
            category="Manga",
            uploader="pub_uploader",
            rating="4.8",
        )
        cls.gallery_pub.tags.add(cls.tag_artist, cls.tag_female)

        cls.gallery_priv = Gallery.objects.create(
            title="Gallery For Private Archive",
            gid="20002",
            provider="panda",
            public=False,
            category="Doujinshi",
            uploader="priv_uploader",
        )

        cls.archive_pub = Archive.objects.create(
            title="Public Test Archive",
            title_jpn="公開テストアーカイブ",
            user=cls.superuser,
            gallery=cls.gallery_pub,
            public=True,
            crc32="A1B2C3D4",
            filesize=1048576,
            filecount=12,
            reason="Initial public upload",
            source_type="web",
        )
        ArchiveTag.objects.create(
            archive=cls.archive_pub, tag=cls.tag_artist, origin=ArchiveTag.ORIGIN_SYSTEM
        )
        ArchiveTag.objects.create(
            archive=cls.archive_pub, tag=cls.tag_female, origin=ArchiveTag.ORIGIN_USER
        )

        cls.archive_priv = Archive.objects.create(
            title="Private Test Archive",
            title_jpn="非公開テストアーカイブ",
            user=cls.regular_user,
            gallery=cls.gallery_priv,
            public=False,
            crc32="E5F6A7B8",
            filesize=2097152,
            filecount=24,
            reason="Initial private upload",
            source_type="local",
        )

        cls.img_pub = Image.objects.create(
            archive=cls.archive_pub,
            sha1="1111111111111111111111111111111111111111",
            image_name="001.jpg",
            image_size=524288,
            image_width=1200,
            image_height=1600,
            image_format="JPEG",
            archive_position=1,
            position=1,
        )

        cls.img_priv = Image.objects.create(
            archive=cls.archive_priv,
            sha1="2222222222222222222222222222222222222222",
            image_name="001_priv.jpg",
            image_size=600000,
            image_width=1000,
            image_height=1400,
            image_format="JPEG",
            position=1,
        )

        cls.file_entry = ArchiveFileEntry.objects.create(
            archive=cls.archive_pub,
            file_name="info.txt",
            file_size=256,
            sha1="3333333333333333333333333333333333333333",
            position=1,
        )

    def test_get_archive_public_anon(self):
        response = self._api_get(self.anon_client, archive=self.archive_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Public Test Archive")
        self.assertEqual(data["title_jpn"], "公開テストアーカイブ")
        self.assertEqual(data["crc32"], "A1B2C3D4")
        self.assertEqual(data["filecount"], 12)
        self.assertEqual(data["filesize"], 1048576)
        self.assertEqual(data["category"], "Manga")
        self.assertEqual(data["uploader"], "pub_uploader")
        self.assertEqual(data["gallery"], self.gallery_pub.pk)
        self.assertIn("download", data)

    def test_get_archive_private_anon_returns_404(self):
        response = self._api_get(self.anon_client, archive=self.archive_priv.pk)
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertIn("does not exist", data["result"])

    def test_get_archive_private_auth_returns_200(self):
        response = self._api_get(self.auth_client, archive=self.archive_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Private Test Archive")

    def test_get_archive_not_found(self):
        response = self._api_get(self.anon_client, archive=999999)
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertIn("does not exist", data["result"])

    def test_get_archive_invalid_id(self):
        response = self._api_get(self.anon_client, archive="not-an-int")
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertIn("does not exist", data["result"])

    def test_get_archives_multiple_anon_only_public(self):
        response = self._api_get(
            self.anon_client,
            archives=[self.archive_pub.pk, self.archive_priv.pk],
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        pks = [item["id"] for item in data]
        self.assertIn(self.archive_pub.pk, pks)
        self.assertNotIn(self.archive_priv.pk, pks)

    def test_get_archives_multiple_auth_all(self):
        response = self._api_get(
            self.auth_client,
            archives=[self.archive_pub.pk, self.archive_priv.pk],
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        pks = [item["id"] for item in data]
        self.assertIn(self.archive_pub.pk, pks)
        self.assertIn(self.archive_priv.pk, pks)

    def test_get_archives_nonexistent_id(self):
        response = self._api_get(self.anon_client, archives=[999999])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data, [])

    def test_get_archives_invalid_id(self):
        response = self._api_get(self.anon_client, archives=["abc", "123"])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Invalid Archive ID.")

    def test_get_at_archive_tags_public(self):
        response = self._api_get(self.anon_client, at=self.archive_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("tags", data)
        self.assertIn("artist:testartist", data["tags"])

    def test_get_at_archive_tags_private_anon_404(self):
        response = self._api_get(self.anon_client, at=self.archive_priv.pk)
        self.assertEqual(response.status_code, 404)

    def test_get_at_archive_tags_private_auth_200(self):
        response = self._api_get(self.auth_client, at=self.archive_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("tags", data)

    def test_get_at_archive_tags_not_found(self):
        response = self._api_get(self.anon_client, at=999999)
        self.assertEqual(response.status_code, 404)

    def test_get_at_archive_tags_invalid_id(self):
        response = self._api_get(self.anon_client, at="invalid")
        self.assertEqual(response.status_code, 404)

    def test_get_ah_archive_hashes_public(self):
        response = self._api_get(self.anon_client, ah=self.archive_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("image_hashes", data)
        self.assertIn(self.img_pub.sha1, data["image_hashes"])

    def test_get_ah_archive_hashes_private_anon_404(self):
        response = self._api_get(self.anon_client, ah=self.archive_priv.pk)
        self.assertEqual(response.status_code, 404)

    def test_get_ah_archive_hashes_private_auth_200(self):
        response = self._api_get(self.auth_client, ah=self.archive_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn(self.img_priv.sha1, data["image_hashes"])

    def test_get_ah_archive_hashes_not_found(self):
        response = self._api_get(self.anon_client, ah=999999)
        self.assertEqual(response.status_code, 404)

    def test_get_ah_archive_hashes_invalid_id(self):
        response = self._api_get(self.anon_client, ah="invalid")
        self.assertEqual(response.status_code, 404)

    def test_get_aof_other_files_public(self):
        response = self._api_get(self.anon_client, aof=self.archive_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("other_files", data)
        self.assertEqual(len(data["other_files"]), 1)
        self.assertEqual(data["other_files"][0]["name"], "info.txt")
        self.assertEqual(data["other_files"][0]["size"], 256)

    def test_get_aof_other_files_private_anon_404(self):
        response = self._api_get(self.anon_client, aof=self.archive_priv.pk)
        self.assertEqual(response.status_code, 404)

    def test_get_aof_other_files_private_auth_200(self):
        response = self._api_get(self.auth_client, aof=self.archive_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["other_files"], [])

    def test_get_aof_other_files_not_found(self):
        response = self._api_get(self.anon_client, aof=999999)
        self.assertEqual(response.status_code, 404)

    def test_get_aof_other_files_invalid_id(self):
        response = self._api_get(self.anon_client, aof="invalid")
        self.assertEqual(response.status_code, 404)

    def test_get_aid_archive_images_data_public(self):
        response = self._api_get(self.anon_client, aid=[self.archive_pub.pk])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn(str(self.archive_pub.pk), data)
        images = data[str(self.archive_pub.pk)]
        self.assertEqual(len(images), 1)
        self.assertEqual(images["1"]["filename"], "001.jpg")

    def test_get_aid_archive_images_data_nonexistent_id(self):
        response = self._api_get(self.anon_client, aid=[999999])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data, {})

    def test_get_aid_archive_images_data_invalid_id(self):
        response = self._api_get(self.anon_client, aid=["abc"])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Invalid Archive ID.")

    def test_get_aid_archive_images_data_private_anon_filtered(self):
        response = self._api_get(self.anon_client, aid=[self.archive_priv.pk])
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertNotIn(str(self.archive_priv.pk), data)

    def test_get_sha1_public_anon(self):
        response = self._api_get(self.anon_client, sha1=self.img_pub.sha1)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.archive_pub.pk)

    def test_get_sha1_private_anon_empty(self):
        response = self._api_get(self.anon_client, sha1=self.img_priv.sha1)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data, [])

    def test_get_sha1_private_auth_returns_archive(self):
        response = self._api_get(self.auth_client, sha1=self.img_priv.sha1)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.archive_priv.pk)

    def test_get_qa_filtered_archives(self):
        response = self._api_get(self.anon_client, qa="", title="Public Test Archive")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.archive_pub.pk)
        self.assertIn("url", data[0])

    def test_get_qa_private_excluded_for_anon(self):
        response = self._api_get(self.anon_client, qa="", title="Private Test Archive")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 0)

    def test_get_q_simple_search(self):
        response = self._api_get(self.anon_client, q="Public Test")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        pks = [item["id"] for item in data]
        self.assertIn(self.archive_pub.pk, pks)

    def test_get_as_archive_search_associated_galleries(self):
        response = self._api_get(self.anon_client, **{"as": "", "title": "Public Test Archive"})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.gallery_pub.pk)
        self.assertIn("archives", data[0])
        self.assertEqual(data[0]["archives"][0]["source"], "web")
        self.assertEqual(data[0]["archives"][0]["reason"], "Initial public upload")
        self.assertIn(
            reverse("viewer:archive-download", args=(self.archive_pub.pk,)),
            data[0]["archives"][0]["link"],
        )

    def test_get_archive_group_entry_archive_public_anon(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry-archive": self.archive_pub.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.archive_pub.pk)
        self.assertEqual(data["title"], "Public Test Archive")

    def test_get_archive_group_entry_archive_private_anon_404(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry-archive": self.archive_priv.pk})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_group_entry_archive_private_auth_200(self):
        response = self._api_get(self.auth_client, **{"archive-group-entry-archive": self.archive_priv.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.archive_priv.pk)

    def test_get_archive_group_entry_archive_not_found(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry-archive": 999999})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_wanted_image_unauthenticated_404(self):
        response = self._api_get(self.anon_client, **{"archive-wanted-image": self.archive_pub.pk})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_wanted_image_invalid_id(self):
        response = self._api_get(self.auth_client, **{"archive-wanted-image": "not-an-int"})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_wanted_image_not_found(self):
        response = self._api_get(self.auth_client, **{"archive-wanted-image": 999999})
        self.assertEqual(response.status_code, 404)

    @patch.object(Archive, "get_wanted_images_similarity_mark")
    def test_get_archive_wanted_image_no_active_wanted_images(self, mock_mark):
        mock_mark.return_value = (-2, [])
        response = self._api_get(self.auth_client, **{"archive-wanted-image": self.archive_pub.pk})
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "No active WantedImages.")

    @patch.object(Archive, "get_wanted_images_similarity_mark")
    def test_get_archive_wanted_image_cannot_run_match(self, mock_mark):
        mock_mark.return_value = (-1, [])
        response = self._api_get(self.auth_client, **{"archive-wanted-image": self.archive_pub.pk})
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Could not run Image Match.")

    @patch.object(Archive, "get_wanted_images_similarity_mark")
    def test_get_archive_wanted_image_success(self, mock_mark):
        mock_wanted_img = MagicMock()
        mock_wanted_img.get_image_url.return_value = "/media/wanted/test.jpg"
        mock_wanted_img.image_name = "test.jpg"
        mock_wanted_img.minimum_features = 10

        mock_mark.return_value = (0, [(mock_wanted_img, 15, True, b"dummy_img_data")])
        response = self._api_get(self.auth_client, **{"archive-wanted-image": self.archive_pub.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("archive", data)
        self.assertIn("matches", data)
        self.assertEqual(len(data["matches"]), 1)
        self.assertEqual(data["matches"][0]["name"], "test.jpg")
        self.assertTrue(data["matches"][0]["found_match"])
        self.assertIn("data:image/jpeg;base64,", data["matches"][0]["found_image"])


class RegistryApiGalleryEndpointsTests(BaseRegistryApiMixin, TestCase):
    """Tests for gallery-related GET endpoints in registry.py."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.tag1 = Tag.objects.create(name="comic", scope="category")
        cls.tag2 = Tag.objects.create(name="solo", scope="female")

        cls.parent_gallery = Gallery.objects.create(
            title="Parent Container Gallery",
            gid="30000",
            provider="panda",
            public=True,
        )

        cls.gallery_pub = Gallery.objects.create(
            title="Public Gallery Endpoint Test",
            title_jpn="公開ギャラリーエンドポイントテスト",
            gid="30001",
            provider="panda",
            token="aabbcc1122",
            category="Manga",
            uploader="gallery_uploader",
            public=True,
            rating="4.2",
            filecount=15,
            filesize=5000000,
            gallery_container=cls.parent_gallery,
            parent_gallery=cls.parent_gallery,
            reason="Sample public gallery",
        )
        cls.gallery_pub.tags.add(cls.tag1, cls.tag2)

        cls.archive_pub = Archive.objects.create(
            title="Public Gallery Archive",
            user=cls.superuser,
            gallery=cls.gallery_pub,
            public=True,
            source_type="web",
            reason="Linked archive",
        )

        cls.gallery_priv = Gallery.objects.create(
            title="Private Gallery Endpoint Test",
            gid="30002",
            provider="panda",
            token="334455ddff",
            category="Doujinshi",
            public=False,
            rating="3.5",
            filecount=20,
            filesize=8000000,
        )

        cls.gallery_diff_provider = Gallery.objects.create(
            title="NHentai Gallery Test",
            gid="30001",
            provider="nhentai",
            public=True,
        )

    def test_get_gallery_public_anon(self):
        response = self._api_get(self.anon_client, gallery=self.gallery_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Public Gallery Endpoint Test")
        self.assertEqual(data["uploader"], "gallery_uploader")
        self.assertEqual(data["filecount"], 15)
        self.assertEqual(data["filesize"], 5000000)
        self.assertEqual(len(data["archives"]), 1)
        self.assertEqual(data["archives"][0]["id"], self.archive_pub.pk)

    def test_get_gallery_private_anon_404(self):
        response = self._api_get(self.anon_client, gallery=self.gallery_priv.pk)
        self.assertEqual(response.status_code, 404)
        data = json.loads(response.content)
        self.assertIn("does not exist", data["result"])

    def test_get_gallery_private_auth_200(self):
        response = self._api_get(self.auth_client, gallery=self.gallery_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Private Gallery Endpoint Test")

    def test_get_gallery_not_found(self):
        response = self._api_get(self.anon_client, gallery=999999)
        self.assertEqual(response.status_code, 404)

    def test_get_gallery_invalid_id(self):
        response = self._api_get(self.anon_client, gallery="invalid")
        self.assertEqual(response.status_code, 404)

    def test_get_gid_public_anon(self):
        response = self._api_get(self.anon_client, gid="30001", provider="panda")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["gid"], "30001")
        self.assertEqual(data["title"], "Public Gallery Endpoint Test")

    def test_get_gid_different_provider(self):
        response = self._api_get(self.anon_client, gid="30001", provider="nhentai")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["gid"], "30001")
        self.assertEqual(data["title"], "NHentai Gallery Test")

    def test_get_gid_private_anon_404(self):
        response = self._api_get(self.anon_client, gid="30002", provider="panda")
        self.assertEqual(response.status_code, 404)

    def test_get_gid_private_auth_200(self):
        response = self._api_get(self.auth_client, gid="30002", provider="panda")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["gid"], "30002")

    def test_get_gid_not_found(self):
        response = self._api_get(self.anon_client, gid="999999")
        self.assertEqual(response.status_code, 404)

    def test_get_gids_multiple_anon_only_public(self):
        response = self._api_get(self.anon_client, gids=["30001", "30002"], provider="panda")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        gids = [item["gid"] for item in data]
        self.assertIn("30001", gids)
        self.assertNotIn("30002", gids)

    def test_get_gids_multiple_auth_all(self):
        response = self._api_get(self.auth_client, gids=["30001", "30002"], provider="panda")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        gids = [item["gid"] for item in data]
        self.assertIn("30001", gids)
        self.assertIn("30002", gids)

    def test_get_gids_nonexistent(self):
        response = self._api_get(self.anon_client, gids=["999999"], provider="panda")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data, [])

    def test_get_gt_gallery_tags_public(self):
        response = self._api_get(self.anon_client, gt=self.gallery_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("tags", data)
        self.assertIn("female:solo", data["tags"])

    def test_get_gt_gallery_tags_private_anon_404(self):
        response = self._api_get(self.anon_client, gt=self.gallery_priv.pk)
        self.assertEqual(response.status_code, 404)

    def test_get_gt_gallery_tags_private_auth_200(self):
        response = self._api_get(self.auth_client, gt=self.gallery_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("tags", data)

    def test_get_gt_gallery_tags_not_found(self):
        response = self._api_get(self.anon_client, gt=999999)
        self.assertEqual(response.status_code, 404)

    def test_get_gt_gallery_tags_invalid_id(self):
        response = self._api_get(self.anon_client, gt="invalid")
        self.assertEqual(response.status_code, 404)

    def test_get_gd_gallery_details_public(self):
        response = self._api_get(self.anon_client, gd=self.gallery_pub.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.gallery_pub.pk)
        self.assertEqual(data["gid"], "30001")
        self.assertEqual(len(data["archives"]), 1)
        self.assertEqual(data["archives"][0]["source"], "web")
        self.assertEqual(data["archives"][0]["reason"], "Linked archive")

    def test_get_gd_gallery_details_private_anon_404(self):
        response = self._api_get(self.anon_client, gd=self.gallery_priv.pk)
        self.assertEqual(response.status_code, 404)

    def test_get_gd_gallery_details_private_auth_200(self):
        response = self._api_get(self.auth_client, gd=self.gallery_priv.pk)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.gallery_priv.pk)

    def test_get_gd_gallery_details_not_found(self):
        response = self._api_get(self.anon_client, gd=999999)
        self.assertEqual(response.status_code, 404)

    def test_get_gd_gallery_details_invalid_id(self):
        response = self._api_get(self.anon_client, gd="invalid")
        self.assertEqual(response.status_code, 404)

    def test_get_g_compact_gallery_search(self):
        response = self._api_get(self.anon_client, g="", title="Public Gallery Endpoint")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Public Gallery Endpoint Test")
        self.assertEqual(data[0]["source"], "panda")

    def test_get_gc_crawler_sync_gallery_search(self):
        response = self._api_get(self.anon_client, gc="", title="Public Gallery Endpoint")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["gid"], "30001")
        self.assertEqual(data[0]["token"], "aabbcc1122")
        self.assertEqual(data[0]["provider"], "panda")

    def test_get_gs_gallery_search_unpaginated(self):
        response = self._api_get(self.anon_client, gs="", title="Public Gallery Endpoint")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.gallery_pub.pk)
        self.assertIn("archives", data[0])

    def test_get_gsp_gallery_search_paginated(self):
        response = self._api_get(
            self.anon_client,
            gsp="",
            title="Public Gallery Endpoint",
            page=1,
            count=24,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("galleries", data)
        self.assertIn("count", data)
        self.assertIn("num_pages", data)
        self.assertIn("number", data)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["galleries"][0]["id"], self.gallery_pub.pk)

    def test_get_gsp_gallery_search_page_out_of_range(self):
        response = self._api_get(
            self.anon_client,
            gsp="",
            title="Public Gallery Endpoint",
            page=9999,
            count=24,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["number"], 1)

    def test_get_match_returns_matched_galleries_with_relations(self):
        response = self._api_get(self.anon_client, match="", title="Public Gallery Endpoint")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.gallery_pub.pk)
        self.assertEqual(data[0]["gallery_container"], "30000")
        self.assertEqual(data[0]["parent_gid"], "30000")


class RegistryApiArchiveGroupEndpointsTests(BaseRegistryApiMixin, TestCase):
    """Tests for ArchiveGroup and ArchiveGroupEntry GET, POST, PUT, DELETE."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        perm_change_group = Permission.objects.get(codename="change_archivegroup")
        perm_delete_group = Permission.objects.get(codename="delete_archivegroup")
        perm_change_entry = Permission.objects.get(codename="change_archivegroupentry")
        perm_delete_entry = Permission.objects.get(codename="delete_archivegroupentry")

        cls.user_group_manage = User.objects.create_user(
            username="user_group_manage", password="password123"
        )
        cls.user_group_manage.user_permissions.add(
            perm_change_group,
            perm_delete_group,
            perm_change_entry,
            perm_delete_entry,
        )

        cls.archive1 = Archive.objects.create(title="Group Archive 1", user=cls.superuser, public=True)
        cls.archive2 = Archive.objects.create(title="Group Archive 2", user=cls.superuser, public=True)
        cls.archive_priv = Archive.objects.create(title="Group Archive Priv", user=cls.superuser, public=False)

        cls.group_pub = ArchiveGroup.objects.create(
            title="Public Anthology Series",
            title_slug="public-anthology-series",
            details="Public series details",
            position=1,
            public=True,
        )
        cls.entry_pub = ArchiveGroupEntry.objects.create(
            archive_group=cls.group_pub,
            archive=cls.archive1,
            title="Volume 1",
            position=1,
        )

        cls.group_priv = ArchiveGroup.objects.create(
            title="Private Anthology Series",
            title_slug="private-anthology-series",
            details="Private series details",
            position=2,
            public=False,
        )
        cls.entry_priv = ArchiveGroupEntry.objects.create(
            archive_group=cls.group_priv,
            archive=cls.archive_priv,
            title="Private Volume 1",
            position=1,
        )

    def setUp(self):
        super().setUp()
        self.manage_client = Client()
        self.manage_client.login(username="user_group_manage", password="password123")

    def test_get_archive_group_public_anon(self):
        response = self._api_get(self.anon_client, **{"archive-group": self.group_pub.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.group_pub.pk)
        self.assertEqual(data["title"], "Public Anthology Series")
        self.assertEqual(len(data["archive_group_entries"]), 1)
        self.assertEqual(data["archive_group_entries"][0]["title"], "Volume 1")

    def test_get_archive_group_private_anon_404(self):
        response = self._api_get(self.anon_client, **{"archive-group": self.group_priv.pk})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_group_private_auth_200(self):
        response = self._api_get(self.auth_client, **{"archive-group": self.group_priv.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.group_priv.pk)

    def test_get_archive_group_not_found(self):
        response = self._api_get(self.anon_client, **{"archive-group": 999999})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_group_invalid_id(self):
        response = self._api_get(self.anon_client, **{"archive-group": "invalid"})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_group_entry_public_anon(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry": self.entry_pub.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.entry_pub.pk)
        self.assertEqual(data["title"], "Volume 1")

    def test_get_archive_group_entry_private_anon_404(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry": self.entry_priv.pk})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_group_entry_private_auth_200(self):
        response = self._api_get(self.auth_client, **{"archive-group-entry": self.entry_priv.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.entry_priv.pk)

    def test_get_archive_group_entry_not_found(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry": 999999})
        self.assertEqual(response.status_code, 404)

    def test_get_archive_group_entry_invalid_id(self):
        response = self._api_get(self.anon_client, **{"archive-group-entry": "invalid"})
        self.assertEqual(response.status_code, 404)

    def test_post_archive_group_unauthorized(self):
        payload = {
            "title": "New Series",
            "title_slug": "new-series",
            "details": "Details",
            "position": 1,
            "archive_group_entries": [],
        }
        # Anon -> 403
        response = self._api_post(self.anon_client, query_str="archive-group=", body=payload)
        self.assertEqual(response.status_code, 403)

        # User without change_archivegroup perm -> 403
        response = self._api_post(self.regular_client, query_str="archive-group=", body=payload)
        self.assertEqual(response.status_code, 403)

    def test_post_archive_group_success(self):
        payload = {
            "title": "Created Group Success",
            "title_slug": "created-group-success",
            "details": "Brand new collection",
            "position": 3,
            "archive_group_entries": [
                {
                    "archive": {"id": self.archive1.pk},
                    "title": "Entry Vol 1",
                    "position": 1,
                },
                {
                    "archive": {"id": self.archive2.pk},
                    "title": "Entry Vol 2",
                    "position": 2,
                },
            ],
        }
        response = self._api_post(self.manage_client, query_str="archive-group=", body=payload)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Created Group Success")
        self.assertEqual(len(data["archive_group_entries"]), 2)
        group_id = data["id"]
        self.assertTrue(ArchiveGroup.objects.filter(pk=group_id).exists())
        self.assertEqual(ArchiveGroupEntry.objects.filter(archive_group_id=group_id).count(), 2)

    def test_post_archive_group_nonexistent_archive_404(self):
        payload = {
            "title": "Failed Group",
            "title_slug": "failed-group",
            "details": "Nonexistent archive inside",
            "position": 1,
            "archive_group_entries": [
                {
                    "archive": {"id": 999999},
                    "title": "Bad Entry",
                    "position": 1,
                }
            ],
        }
        response = self._api_post(self.manage_client, query_str="archive-group=", body=payload)
        self.assertEqual(response.status_code, 404)

    def test_post_archive_group_entry_unauthorized(self):
        payload = {"archive": {"id": self.archive2.pk}, "title": "New Vol"}
        response = self._api_post(
            self.regular_client,
            query_str=f"archive-group-entry={self.group_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 403)

    def test_post_archive_group_entry_success(self):
        payload = {
            "archive": {"id": self.archive2.pk},
            "title": "New Volume Added",
            "position": 2,
        }
        response = self._api_post(
            self.manage_client,
            query_str=f"archive-group-entry={self.group_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "New Volume Added")
        self.assertEqual(data["position"], 2)
        self.assertTrue(
            ArchiveGroupEntry.objects.filter(
                archive_group=self.group_pub, archive=self.archive2
            ).exists()
        )

    def test_post_archive_group_entry_group_not_found(self):
        payload = {"archive": {"id": self.archive1.pk}, "title": "Orphan"}
        response = self._api_post(
            self.manage_client, query_str="archive-group-entry=999999", body=payload
        )
        self.assertEqual(response.status_code, 404)

    def test_post_archive_group_entry_archive_not_found(self):
        payload = {"archive": {"id": 999999}, "title": "Bad Archive"}
        response = self._api_post(
            self.manage_client,
            query_str=f"archive-group-entry={self.group_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 404)

    def test_post_archive_group_entry_invalid_id(self):
        payload = {"archive": {"id": self.archive1.pk}}
        response = self._api_post(
            self.manage_client, query_str="archive-group-entry=invalid", body=payload
        )
        self.assertEqual(response.status_code, 404)

    def test_put_archive_group_unauthorized(self):
        payload = {"title": "Denied Update", "details": "", "position": 1, "archive_group_entries": []}
        response = self._api_put(
            self.regular_client,
            query_str=f"archive-group={self.group_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 403)

    def test_put_archive_group_success(self):
        payload = {
            "title": "Updated Group Title",
            "details": "Updated Details",
            "position": 5,
            "archive_group_entries": [
                {
                    "id": self.entry_pub.pk,
                    "title": "Updated Volume 1 Title",
                    "position": 1,
                    "archive": {"id": self.archive1.pk},
                }
            ],
        }
        response = self._api_put(
            self.manage_client,
            query_str=f"archive-group={self.group_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Updated Group Title")
        self.assertEqual(data["details"], "Updated Details")
        self.assertEqual(data["position"], 5)
        self.entry_pub.refresh_from_db()
        self.assertEqual(self.entry_pub.title, "Updated Volume 1 Title")

    def test_put_archive_group_not_found(self):
        payload = {"title": "Not Found", "details": "", "position": 1, "archive_group_entries": []}
        response = self._api_put(
            self.manage_client, query_str="archive-group=999999", body=payload
        )
        self.assertEqual(response.status_code, 404)

    def test_put_archive_group_invalid_id(self):
        payload = {"title": "Invalid ID", "details": "", "position": 1, "archive_group_entries": []}
        response = self._api_put(
            self.manage_client, query_str="archive-group=invalid", body=payload
        )
        self.assertEqual(response.status_code, 404)

    def test_put_archive_group_entry_unauthorized(self):
        payload = {"title": "Denied", "position": 2, "archive": {"id": self.archive1.pk}}
        response = self._api_put(
            self.regular_client,
            query_str=f"archive-group-entry={self.entry_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 403)

    def test_put_archive_group_entry_success(self):
        payload = {
            "title": "Volume 1 Revised",
            "position": 10,
            "archive": {"id": self.archive2.pk},
        }
        response = self._api_put(
            self.manage_client,
            query_str=f"archive-group-entry={self.entry_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Volume 1 Revised")
        self.assertEqual(data["position"], 10)
        self.entry_pub.refresh_from_db()
        self.assertEqual(self.entry_pub.title, "Volume 1 Revised")
        self.assertEqual(self.entry_pub.archive, self.archive2)

    def test_put_archive_group_entry_not_found(self):
        payload = {"title": "Not Found", "position": 1, "archive": {"id": self.archive1.pk}}
        response = self._api_put(
            self.manage_client, query_str="archive-group-entry=999999", body=payload
        )
        self.assertEqual(response.status_code, 404)

    def test_put_archive_group_entry_invalid_id(self):
        payload = {"title": "Invalid ID", "position": 1, "archive": {"id": self.archive1.pk}}
        response = self._api_put(
            self.manage_client, query_str="archive-group-entry=invalid", body=payload
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_archive_group_unauthorized(self):
        response = self._api_delete(
            self.regular_client, query_str=f"archive-group={self.group_pub.pk}"
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_archive_group_success(self):
        group_to_del = ArchiveGroup.objects.create(
            title="Delete Me", title_slug="delete-me", position=99, public=True
        )
        response = self._api_delete(
            self.manage_client, query_str=f"archive-group={group_to_del.pk}"
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertGreater(data["result"], 0)
        self.assertFalse(ArchiveGroup.objects.filter(pk=group_to_del.pk).exists())

    def test_delete_archive_group_not_found(self):
        response = self._api_delete(self.manage_client, query_str="archive-group=999999")
        self.assertEqual(response.status_code, 404)

    def test_delete_archive_group_invalid_id(self):
        response = self._api_delete(self.manage_client, query_str="archive-group=invalid")
        self.assertEqual(response.status_code, 404)

    def test_delete_archive_group_entry_unauthorized(self):
        response = self._api_delete(
            self.regular_client, query_str=f"archive-group-entry={self.entry_pub.pk}"
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_archive_group_entry_success(self):
        entry_to_del = ArchiveGroupEntry.objects.create(
            archive_group=self.group_pub,
            archive=self.archive1,
            title="Temp Entry",
            position=99,
        )
        entry_id = entry_to_del.pk
        response = self._api_delete(
            self.manage_client, query_str=f"archive-group-entry={entry_id}"
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertGreater(data["result"], 0)
        self.assertFalse(ArchiveGroupEntry.objects.filter(pk=entry_id).exists())

    def test_delete_archive_group_entry_not_found(self):
        response = self._api_delete(
            self.manage_client, query_str="archive-group-entry=999999"
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_archive_group_entry_invalid_id(self):
        response = self._api_delete(
            self.manage_client, query_str="archive-group-entry=invalid"
        )
        self.assertEqual(response.status_code, 404)


class RegistryApiWantedGalleryEndpointsTests(BaseRegistryApiMixin, TestCase):
    """Tests for WantedGallery GET, POST, PUT, DELETE."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        perm_add_wg = Permission.objects.get(codename="add_wantedgallery")
        perm_change_wg = Permission.objects.get(codename="change_wantedgallery")
        perm_delete_wg = Permission.objects.get(codename="delete_wantedgallery")

        cls.user_wanted_manage = User.objects.create_user(
            username="user_wanted_manage", password="password123"
        )
        cls.user_wanted_manage.user_permissions.add(
            perm_add_wg,
            perm_change_wg,
            perm_delete_wg,
        )

        cls.gallery_match = Gallery.objects.create(
            title="Matched Target Gallery",
            gid="40001",
            provider="panda",
            public=True,
        )

        cls.wanted_pub = WantedGallery.objects.create(
            title="Public Wanted Manga",
            title_jpn="公開募集マンガ",
            search_title="Public Wanted",
            book_type="Manga",
            page_count=30,
            publisher="TestPublisher",
            reason="Looking for high quality scan",
            public=True,
            should_search=True,
            keep_searching=False,
            notify_when_found=True,
        )

        cls.found_entry = FoundGallery.objects.create(
            wanted_gallery=cls.wanted_pub,
            gallery=cls.gallery_match,
        )

        cls.wanted_priv = WantedGallery.objects.create(
            title="Private Wanted Doujinshi",
            title_jpn="非公開募集同人誌",
            search_title="Private Wanted",
            book_type="Doujinshi",
            page_count=45,
            public=False,
            should_search=False,
            keep_searching=False,
            notify_when_found=False,
        )

    def setUp(self):
        super().setUp()
        self.manage_client = Client()
        self.manage_client.login(username="user_wanted_manage", password="password123")

    def test_get_wanted_gallery_public_anon(self):
        response = self._api_get(self.anon_client, **{"wanted-gallery": self.wanted_pub.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.wanted_pub.pk)
        self.assertEqual(data["title"], "Public Wanted Manga")
        self.assertTrue(data["public"])
        self.assertNotIn("found_galleries", data)

    def test_get_wanted_gallery_with_include_found_galleries(self):
        response = self._api_get(
            self.anon_client,
            **{"wanted-gallery": self.wanted_pub.pk, "include_found_galleries": ""},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("found_galleries", data)
        self.assertEqual(len(data["found_galleries"]), 1)
        self.assertEqual(data["found_galleries"][0]["gid"], "40001")

    def test_get_wanted_gallery_private_anon_404(self):
        response = self._api_get(self.anon_client, **{"wanted-gallery": self.wanted_priv.pk})
        self.assertEqual(response.status_code, 404)

    def test_get_wanted_gallery_private_auth_200(self):
        response = self._api_get(self.auth_client, **{"wanted-gallery": self.wanted_priv.pk})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.wanted_priv.pk)

    def test_get_wanted_gallery_not_found(self):
        response = self._api_get(self.anon_client, **{"wanted-gallery": 999999})
        self.assertEqual(response.status_code, 404)

    def test_get_wanted_gallery_invalid_id(self):
        response = self._api_get(self.anon_client, **{"wanted-gallery": "invalid"})
        self.assertEqual(response.status_code, 404)

    def test_get_wanted_galleries_multiple_anon_only_public(self):
        response = self._api_get(
            self.anon_client,
            **{"wanted-galleries": [self.wanted_pub.pk, self.wanted_priv.pk]},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        ids = [item["id"] for item in data]
        self.assertIn(self.wanted_pub.pk, ids)
        self.assertNotIn(self.wanted_priv.pk, ids)

    def test_get_wanted_galleries_multiple_auth_all(self):
        response = self._api_get(
            self.auth_client,
            **{"wanted-galleries": [self.wanted_pub.pk, self.wanted_priv.pk]},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        ids = [item["id"] for item in data]
        self.assertIn(self.wanted_pub.pk, ids)
        self.assertIn(self.wanted_priv.pk, ids)

    def test_get_wanted_galleries_nonexistent_id(self):
        response = self._api_get(self.anon_client, **{"wanted-galleries": [999999]})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data, [])

    def test_get_wanted_galleries_invalid_id(self):
        response = self._api_get(self.anon_client, **{"wanted-galleries": ["abc"]})
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Invalid WantedGallery ID.")

    def test_get_wanted_galleries_search_by_title(self):
        response = self._api_get(
            self.anon_client,
            **{"wanted-galleries-search": "", "title": "Public Wanted"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertGreaterEqual(len(data), 1)
        self.assertEqual(data[0]["id"], self.wanted_pub.pk)

    def test_get_wanted_galleries_search_empty_result(self):
        response = self._api_get(
            self.anon_client,
            **{"wanted-galleries-search": "", "title": "NonExistentWantedXYZ"},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data, [])

    def test_post_wanted_gallery_unauthorized(self):
        payload = {"title": "Denied Wanted"}
        response = self._api_post(
            self.regular_client, query_str="wanted-gallery=", body=payload
        )
        self.assertEqual(response.status_code, 403)

    def test_post_wanted_gallery_success(self):
        payload = {
            "title": "Created Wanted Gallery Post",
            "title_jpn": "作成された募集ギャラリー",
            "search_title": "Created Search",
            "should_search": True,
            "keep_searching": False,
            "notify_when_found": True,
            "public": True,
            "wait_for_time": 600,
            "wanted_tags": ["artist:sampleartist", "female:twintails"],
            "category": "Manga",
            "reason": "Created via test",
        }
        response = self._api_post(
            self.manage_client, query_str="wanted-gallery=", body=payload
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        new_id = data["id"]
        created = WantedGallery.objects.get(pk=new_id)
        self.assertEqual(created.title, "Created Wanted Gallery Post")
        self.assertEqual(created.title_jpn, "作成された募集ギャラリー")
        self.assertTrue(created.public)
        self.assertEqual(created.wait_for_time.total_seconds(), 600)
        self.assertIn("artist:sampleartist", created.wanted_tags_list())

    def test_put_wanted_gallery_unauthorized(self):
        response = self._api_put(
            self.regular_client,
            query_str=f"wanted-gallery={self.wanted_pub.pk}",
            body={"title": "Unauthorized"},
        )
        self.assertEqual(response.status_code, 403)

    def test_put_wanted_gallery_via_query_param(self):
        payload = {
            "title": "Updated Title via Query Param",
            "should_search": False,
            "public": True,
            "wanted_tags": ["artist:updatedartist"],
        }
        response = self._api_put(
            self.manage_client,
            query_str=f"wanted-gallery={self.wanted_pub.pk}",
            body=payload,
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["title"], "Updated Title via Query Param")
        self.wanted_pub.refresh_from_db()
        self.assertEqual(self.wanted_pub.title, "Updated Title via Query Param")
        self.assertFalse(self.wanted_pub.should_search)

    def test_put_wanted_gallery_via_body_id(self):
        payload = {
            "id": self.wanted_pub.pk,
            "reason": "Updated Reason via Body ID",
        }
        response = self._api_put(
            self.manage_client, query_str="wanted-gallery=", body=payload
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["reason"], "Updated Reason via Body ID")
        self.wanted_pub.refresh_from_db()
        self.assertEqual(self.wanted_pub.reason, "Updated Reason via Body ID")

    def test_put_wanted_gallery_not_found(self):
        response = self._api_put(
            self.manage_client,
            query_str="wanted-gallery=999999",
            body={"title": "Not Found"},
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_wanted_gallery_unauthorized(self):
        response = self._api_delete(
            self.regular_client, query_str=f"wanted-gallery={self.wanted_pub.pk}"
        )
        self.assertEqual(response.status_code, 403)

    def test_delete_wanted_gallery_via_query_param(self):
        to_del = WantedGallery.objects.create(title="Delete Query Param", public=True)
        del_id = to_del.pk
        response = self._api_delete(
            self.manage_client, query_str=f"wanted-gallery={del_id}"
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["id"], del_id)
        self.assertFalse(WantedGallery.objects.filter(pk=del_id).exists())

    def test_delete_wanted_gallery_without_id_returns_404(self):
        response = self._api_delete(
            self.manage_client,
            query_str="wanted-gallery=",
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_wanted_gallery_not_found(self):
        response = self._api_delete(
            self.manage_client, query_str="wanted-gallery=999999"
        )
        self.assertEqual(response.status_code, 404)
