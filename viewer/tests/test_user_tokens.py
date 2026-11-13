import re
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import AnonymousUser, User
from django.core.management import call_command
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from viewer.models import EventLog, UserLongLivedToken
from viewer.utils.requests import authenticate_by_token, double_check_auth


def bearer(secret: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {secret}"}


class TokenModelTests(TestCase):
    user: User
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="token_model_user", password="password123")

    def test_create_for_user_defaults_to_all_scopes_and_stores_only_the_hash(self):
        token, secret = UserLongLivedToken.create_for_user(self.user, "default")
        self.assertEqual(set(token.scopes), set(UserLongLivedToken.ALL_SCOPES))
        self.assertNotEqual(token.key, secret)
        self.assertEqual(token.key, UserLongLivedToken.create_salted_key_from_key(secret))

    def test_no_expiry_means_never_expires(self):
        token, _ = UserLongLivedToken.create_for_user(self.user, "forever", expire_date=None)
        token.refresh_from_db()
        self.assertIsNone(token.expire_date)
        self.assertTrue(token.never_expires)
        self.assertFalse(token.is_expired)
        self.assertIn(token, UserLongLivedToken.active())

    def test_expired_token_is_not_active(self):
        token, _ = UserLongLivedToken.create_for_user(
            self.user, "old", expire_date=timezone.now() - timedelta(seconds=1)
        )
        self.assertTrue(token.is_expired)
        self.assertNotIn(token, UserLongLivedToken.active())

    def test_default_expiry_is_still_ten_years(self):
        token = UserLongLivedToken.objects.create(user=self.user, name="legacy-style", key="legacy-key")
        self.assertGreater(token.expire_date, timezone.now() + timedelta(days=3600))
        self.assertEqual(set(token.scopes), set(UserLongLivedToken.ALL_SCOPES))

    def test_same_name_allowed_for_different_users_but_not_the_same_user(self):
        other = User.objects.create_user(username="token_model_other", password="password123")
        UserLongLivedToken.create_for_user(self.user, "shared-name")
        UserLongLivedToken.create_for_user(other, "shared-name")
        with self.assertRaises(Exception):
            UserLongLivedToken.create_for_user(self.user, "shared-name")

    def test_mark_used_is_throttled(self):
        token, _ = UserLongLivedToken.create_for_user(self.user, "throttle")
        self.assertIsNone(token.last_used_date)

        token.mark_used()
        token.refresh_from_db()
        first_use = token.last_used_date
        self.assertIsNotNone(first_use)

        token.mark_used()
        token.refresh_from_db()
        self.assertEqual(token.last_used_date, first_use)

        UserLongLivedToken.objects.filter(pk=token.pk).update(
            last_used_date=timezone.now() - timedelta(seconds=UserLongLivedToken.LAST_USED_RESOLUTION_SECONDS + 5)
        )
        token.mark_used()
        token.refresh_from_db()
        self.assertGreater(token.last_used_date, timezone.now() - timedelta(seconds=5))


class TokenAuthenticationTests(TestCase):
    user: User
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="token_auth_user", password="password123")

    def _fake_request(self, secret: str, method: str = "GET"):
        request = RequestFactory().generic(method, "/", headers={"Authorization": f"Bearer {secret}"})
        request.user = AnonymousUser()
        return request

    def test_valid_token_authenticates_and_records_usage(self):
        token, secret = UserLongLivedToken.create_for_user(self.user, "ok")
        ok, user = authenticate_by_token(self._fake_request(secret), UserLongLivedToken.SCOPE_READ)
        self.assertTrue(ok)
        self.assertEqual(user, self.user)
        token.refresh_from_db()
        self.assertIsNotNone(token.last_used_date)

    def test_never_expiring_token_authenticates(self):
        _, secret = UserLongLivedToken.create_for_user(self.user, "forever", expire_date=None)
        self.assertTrue(authenticate_by_token(self._fake_request(secret), UserLongLivedToken.SCOPE_READ)[0])

    def test_expired_token_is_rejected_and_not_marked_used(self):
        token, secret = UserLongLivedToken.create_for_user(
            self.user, "expired", expire_date=timezone.now() - timedelta(days=1)
        )
        self.assertEqual(
            authenticate_by_token(self._fake_request(secret), UserLongLivedToken.SCOPE_READ), (False, None)
        )
        token.refresh_from_db()
        self.assertIsNone(token.last_used_date)

    def test_token_without_scope_is_rejected(self):
        token, secret = UserLongLivedToken.create_for_user(self.user, "readonly", scopes=["read"])
        self.assertTrue(authenticate_by_token(self._fake_request(secret), UserLongLivedToken.SCOPE_READ)[0])
        self.assertEqual(
            authenticate_by_token(self._fake_request(secret), UserLongLivedToken.SCOPE_WRITE), (False, None)
        )
        self.assertEqual(
            authenticate_by_token(self._fake_request(secret), UserLongLivedToken.SCOPE_REMOTE), (False, None)
        )

    def test_unknown_and_missing_tokens_are_rejected(self):
        self.assertEqual(
            authenticate_by_token(self._fake_request("nope"), UserLongLivedToken.SCOPE_READ), (False, None)
        )
        request = self._fake_request("nope")
        request.META.pop("HTTP_AUTHORIZATION")
        self.assertEqual(authenticate_by_token(request, UserLongLivedToken.SCOPE_READ), (False, None))

    def test_double_check_auth_infers_scope_from_method(self):
        _, secret = UserLongLivedToken.create_for_user(self.user, "readonly", scopes=["read"])
        self.assertTrue(double_check_auth(self._fake_request(secret, "GET"))[0])
        self.assertFalse(double_check_auth(self._fake_request(secret, "POST"))[0])


class TokenScopeEnforcementTests(TestCase):
    user: User
    """The scope is enforced on real endpoints, not only in the helper."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_superuser(
            username="token_scope_user", email="token_scope@example.com", password="password123"
        )

    def test_json_api_write_requires_write_scope(self):
        _, read_secret = UserLongLivedToken.create_for_user(self.user, "read-only", scopes=["read"])
        _, write_secret = UserLongLivedToken.create_for_user(self.user, "writer", scopes=["read", "write"])
        client = Client()
        url = reverse("viewer:api") + "?unknown_cmd=1"

        denied = client.post(url, data="{}", content_type="application/json", **bearer(read_secret))
        self.assertEqual(denied.status_code, 403)

        allowed = client.post(url, data="{}", content_type="application/json", **bearer(write_secret))
        self.assertEqual(allowed.status_code, 200)

    def test_json_parser_requires_remote_scope(self):
        _, no_remote = UserLongLivedToken.create_for_user(self.user, "no-remote", scopes=["read", "write"])
        _, remote = UserLongLivedToken.create_for_user(self.user, "remote", scopes=["remote"])
        client = Client()

        denied = client.post(
            reverse("viewer:json-parser"), data="{}", content_type="application/json", **bearer(no_remote)
        )
        self.assertEqual(denied.status_code, 401)

        allowed = client.post(
            reverse("viewer:json-parser"), data="{}", content_type="application/json", **bearer(remote)
        )
        self.assertNotEqual(allowed.status_code, 401)

    def test_expired_token_cannot_use_the_api(self):
        _, secret = UserLongLivedToken.create_for_user(
            self.user, "expired", expire_date=timezone.now() - timedelta(days=1)
        )
        response = Client().post(
            reverse("viewer:api") + "?unknown_cmd=1", data="{}", content_type="application/json", **bearer(secret)
        )
        self.assertEqual(response.status_code, 403)

    def test_session_login_is_not_limited_by_scopes(self):
        client = Client()
        client.login(username=self.user.username, password="password123")
        response = client.post(reverse("viewer:api") + "?unknown_cmd=1", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)


class TokenManagementViewTests(TestCase):
    user: User
    other: User
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="token_view_user", password="password123")
        cls.other = User.objects.create_user(username="token_view_other", password="password123")

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.client.login(username=self.user.username, password="password123")
        self.create_url = reverse("viewer:user-token", args=["create"])
        self.delete_url = reverse("viewer:user-token", args=["delete"])

    def _csrf(self) -> str:
        self.client.get(reverse("viewer:change-profile"))
        return self.client.cookies["csrftoken"].value

    def _create(self, **data):
        data.setdefault("csrfmiddlewaretoken", self._csrf())
        return self.client.post(self.create_url, data)

    def test_get_is_rejected_and_changes_nothing(self):
        token, _ = UserLongLivedToken.create_for_user(self.user, "keep-me")
        self.assertEqual(self.client.get(self.create_url, {"token-name": "via-get"}).status_code, 405)
        self.assertEqual(self.client.get(self.delete_url, {"id": token.pk}).status_code, 405)
        self.assertEqual(self.user.long_lived_tokens.count(), 1)

    def test_post_without_csrf_token_is_rejected(self):
        response = self.client.post(self.create_url, {"name": "no-csrf", "expiry_days": "30", "scopes": ["read"]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.user.long_lived_tokens.exists())

    def test_anonymous_is_redirected_to_login(self):
        response = Client().post(self.create_url, {"name": "anon"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        self.assertFalse(UserLongLivedToken.objects.exists())

    def test_create_with_expiry_and_scopes(self):
        response = self._create(name="ci", expiry_days="30", scopes=["read", "remote"])
        self.assertRedirects(response, reverse("viewer:change-profile"), fetch_redirect_response=False)

        token = self.user.long_lived_tokens.get(name="ci")
        self.assertEqual(sorted(token.scopes), ["read", "remote"])
        self.assertAlmostEqual(
            token.expire_date, timezone.now() + timedelta(days=30), delta=timedelta(minutes=1)
        )
        self.assertTrue(EventLog.objects.filter(action="CREATE_USER_TOKEN", user=self.user).exists())

        # The secret is shown exactly once, in the flash message, and works.
        page = self.client.get(reverse("viewer:change-profile"))
        shown = [str(m) for m in page.context["messages"]]
        self.assertEqual(len(shown), 1)
        secret = re.search(r"<([^<>]+)>", shown[0]).group(1)
        self.assertEqual(UserLongLivedToken.create_salted_key_from_key(secret), token.key)

    def test_create_never_expiring_token(self):
        self._create(name="forever", expiry_days="0", scopes=["read"])
        self.assertIsNone(self.user.long_lived_tokens.get(name="forever").expire_date)

    def test_create_validation_errors(self):
        UserLongLivedToken.create_for_user(self.user, "taken")
        for data in (
            {"name": "taken", "expiry_days": "30", "scopes": ["read"]},  # duplicate name
            {"name": "", "expiry_days": "30", "scopes": ["read"]},  # missing name
            {"name": "bad-scope", "expiry_days": "30", "scopes": ["root"]},  # unknown scope
            {"name": "no-scope", "expiry_days": "30"},  # at least one scope
            {"name": "bad-expiry", "expiry_days": "-5", "scopes": ["read"]},  # unknown expiry choice
        ):
            self._create(**data)
        self.assertEqual(self.user.long_lived_tokens.count(), 1)

    def test_same_name_as_another_users_token_is_allowed(self):
        UserLongLivedToken.create_for_user(self.other, "common")
        response = self._create(name="common", expiry_days="30", scopes=["read"])
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.user.long_lived_tokens.filter(name="common").exists())

    def test_revoke_own_token(self):
        token, secret = UserLongLivedToken.create_for_user(self.user, "revoke-me")
        response = self.client.post(self.delete_url, {"id": token.pk, "csrfmiddlewaretoken": self._csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserLongLivedToken.objects.filter(pk=token.pk).exists())
        self.assertTrue(EventLog.objects.filter(action="REVOKE_USER_TOKEN", user=self.user).exists())
        self.assertFalse(authenticate_by_token(_Req(secret), UserLongLivedToken.SCOPE_READ)[0])

    def test_cannot_revoke_someone_elses_token(self):
        token, _ = UserLongLivedToken.create_for_user(self.other, "not-yours")
        self.client.post(self.delete_url, {"id": token.pk, "csrfmiddlewaretoken": self._csrf()})
        self.assertTrue(UserLongLivedToken.objects.filter(pk=token.pk).exists())

    def test_revoke_with_invalid_id_does_not_crash(self):
        for bad_id in ("", "abc", "999999"):
            response = self.client.post(self.delete_url, {"id": bad_id, "csrfmiddlewaretoken": self._csrf()})
            self.assertEqual(response.status_code, 302)

    def test_unknown_operation_is_404(self):
        response = self.client.post(
            reverse("viewer:user-token", args=["nope"]), {"csrfmiddlewaretoken": self._csrf()}
        )
        self.assertEqual(response.status_code, 404)

    def test_profile_lists_tokens_with_status_scopes_and_last_used(self):
        UserLongLivedToken.create_for_user(self.user, "listed-forever", expire_date=None, scopes=["read"])
        expired, _ = UserLongLivedToken.create_for_user(
            self.user, "listed-expired", expire_date=timezone.now() - timedelta(days=1)
        )
        UserLongLivedToken.objects.filter(pk=expired.pk).update(last_used_date=timezone.now())
        UserLongLivedToken.create_for_user(self.other, "someone-elses")

        page = self.client.get(reverse("viewer:change-profile"))
        self.assertContains(page, "listed-forever")
        self.assertContains(page, "listed-expired")
        self.assertContains(page, "Expired")
        self.assertContains(page, "Never used")
        self.assertNotContains(page, "someone-elses")
        self.assertEqual(len(page.context["tokens"]), 2)


class _Req:
    """Minimal request stand-in carrying only a bearer header."""

    def __init__(self, secret: str):
        self.META = {"HTTP_AUTHORIZATION": f"Bearer {secret}"}


class UserManagementCommandTests(TestCase):
    user: User
    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="token_cmd_user", password="password123")

    def _run(self, *args) -> str:
        out = StringIO()
        call_command("user-management", "-un", self.user.username, *args, stdout=out)
        return out.getvalue()

    def test_create_token_defaults_keep_legacy_behaviour(self):
        self._run("-ct", "cli-default")
        token = self.user.long_lived_tokens.get(name="cli-default")
        self.assertEqual(set(token.scopes), set(UserLongLivedToken.ALL_SCOPES))
        self.assertGreater(token.expire_date, timezone.now() + timedelta(days=3600))

    def test_create_token_without_expiry_and_with_scopes(self):
        self._run("-ct", "cli-forever", "--token-expiry-days", "0", "--token-scopes", "read,write")
        token = self.user.long_lived_tokens.get(name="cli-forever")
        self.assertIsNone(token.expire_date)
        self.assertEqual(sorted(token.scopes), ["read", "write"])

    def test_invalid_scope_creates_nothing(self):
        output = self._run("-ct", "cli-bad", "--token-scopes", "read,root")
        self.assertIn("Invalid scopes", output)
        self.assertFalse(self.user.long_lived_tokens.exists())

    def test_delete_token_by_name(self):
        UserLongLivedToken.create_for_user(self.user, "cli-delete")
        self._run("-dt", "cli-delete")
        self.assertFalse(self.user.long_lived_tokens.exists())
