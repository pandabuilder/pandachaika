"""Permission checks on collaborator views whose decorators were misplaced when collaborators.py was split."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

User = get_user_model()


class CollaboratorViewPermissionTests(TestCase):
    def _client_with_perms(self, *codenames: str) -> Client:
        username = "user_" + "_".join(codenames or ("none",))
        user = User.objects.create_user(username=username, password="password123")
        if codenames:
            user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
        client = Client()
        client.login(username=username, password="password123")
        return client

    def assertRedirectsToLogin(self, response) -> None:
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("viewer:login"), response["Location"])

    def test_match_archives_requires_match_archive(self):
        url = reverse("viewer:match-archives")
        post_data = {"sel-1": "1", "clear_possible_matches": "1"}
        self.assertRedirectsToLogin(Client().get(url))
        self.assertRedirectsToLogin(Client().post(url, post_data))
        self.assertRedirectsToLogin(self._client_with_perms().post(url, post_data))
        self.assertEqual(self._client_with_perms("match_archive").get(url).status_code, 200)

    def test_archives_by_field_requires_manage_archive(self):
        url = reverse("viewer:archives-by-field")
        self.assertRedirectsToLogin(Client().get(url))
        self.assertRedirectsToLogin(self._client_with_perms().get(url))
        self.assertEqual(self._client_with_perms("manage_archive").get(url).status_code, 200)

    def test_upload_archive_only_requires_upload_permission(self):
        url = reverse("viewer:upload-archive")
        self.assertRedirectsToLogin(Client().get(url))
        self.assertEqual(self._client_with_perms("upload_with_metadata_archive").get(url).status_code, 200)

    def test_galleries_by_field_only_requires_manage_gallery(self):
        url = reverse("viewer:galleries-by-field")
        self.assertRedirectsToLogin(Client().get(url))
        self.assertEqual(self._client_with_perms("manage_gallery").get(url).status_code, 200)
