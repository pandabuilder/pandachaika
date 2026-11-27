from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from viewer.models import Profile


class ChangePasswordViewTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="password_user", password="old-Passw0rd!x")

    def setUp(self):
        self.client = Client()
        self.client.login(username=self.user.username, password="old-Passw0rd!x")
        self.url = reverse("viewer:change-password")

    def test_requires_login(self):
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_renders_three_hidden_fields(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        for name in ("old_password", "new_password1", "new_password2"):
            self.assertContains(response, f'name="{name}"')
        self.assertEqual(response.content.decode().count('type="password"'), 3)

    def test_wrong_old_password_shows_error_and_keeps_password(self):
        response = self.client.post(
            self.url, {"old_password": "nope", "new_password1": "n3w-Passw0rd!y", "new_password2": "n3w-Passw0rd!y"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is-invalid")
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("old-Passw0rd!x"))

    def test_success_changes_password_and_keeps_session(self):
        response = self.client.post(
            self.url,
            {"old_password": "old-Passw0rd!x", "new_password1": "n3w-Passw0rd!y", "new_password2": "n3w-Passw0rd!y"},
        )
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("n3w-Passw0rd!y"))
        self.assertEqual(self.client.get(self.url).status_code, 200)


class ProfileFormTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="profile_user", password="password123")

    def setUp(self):
        self.client = Client()
        self.client.login(username=self.user.username, password="password123")
        self.url = reverse("viewer:change-profile")

    def test_saves_email_and_notifications(self):
        response = self.client.post(self.url, {"email": "me@example.com", "notify_new_submissions": "on"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.user.refresh_from_db()
        profile = Profile.objects.get(user=self.user)
        self.assertEqual(self.user.email, "me@example.com")
        self.assertTrue(profile.notify_new_submissions)
        self.assertFalse(profile.notify_wanted_gallery_found)

    def test_private_archive_notification_is_shown_disabled_and_not_saved(self):
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="notify_new_private_archive"')
        self.assertContains(response, 'id="notify-private-archive" disabled')

        self.client.post(self.url, {"email": "me@example.com", "notify_new_private_archive": "on"})
        self.assertFalse(Profile.objects.get(user=self.user).notify_new_private_archive)
