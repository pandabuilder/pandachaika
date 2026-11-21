from django.contrib.auth.models import User
from django.test import Client, RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse

from viewer.utils.general import get_safe_referer, safe_redirect_back

FALLBACK = "/fallback/"


class GetSafeRefererTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _referer(self, referer=None, secure=False):
        extra = {"HTTP_HOST": "panda.example"}
        if referer is not None:
            extra["HTTP_REFERER"] = referer
        request = self.factory.get("/action/", secure=secure, **extra)
        return get_safe_referer(request, FALLBACK)

    def test_missing_referer_uses_fallback(self):
        self.assertEqual(self._referer(), FALLBACK)

    def test_empty_referer_uses_fallback(self):
        self.assertEqual(self._referer(""), FALLBACK)
        self.assertEqual(self._referer("   "), FALLBACK)

    def test_same_host_referer_is_kept(self):
        url = "http://panda.example/archive/1/?view=full"
        self.assertEqual(self._referer(url), url)

    def test_relative_referer_is_kept(self):
        self.assertEqual(self._referer("/archive/1/"), "/archive/1/")

    def test_header_name_prefix_is_stripped(self):
        self.assertEqual(self._referer("Referer: http://panda.example/tools/"), "http://panda.example/tools/")

    def test_other_host_uses_fallback(self):
        self.assertEqual(self._referer("http://evil.example/"), FALLBACK)
        self.assertEqual(self._referer("//evil.example/path"), FALLBACK)

    def test_unsafe_scheme_uses_fallback(self):
        self.assertEqual(self._referer("javascript:alert(1)"), FALLBACK)

    def test_https_request_rejects_http_referer(self):
        self.assertEqual(self._referer("http://panda.example/tools/", secure=True), FALLBACK)
        self.assertEqual(
            self._referer("https://panda.example/tools/", secure=True), "https://panda.example/tools/"
        )

    def test_safe_redirect_back(self):
        request = self.factory.get("/action/", HTTP_HOST="panda.example")
        response = safe_redirect_back(request, FALLBACK)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], FALLBACK)


class MissingRefererViewTests(TestCase):
    """Views that used to index request.META["HTTP_REFERER"] must not 500 without it."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="plain_user", password="testpassword")
        self.client.force_login(self.user)

    def test_archive_update_without_referer_redirects_to_fallback(self):
        response = self.client.post(reverse("viewer:archive-update", args=[123]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("viewer:archive", args=[123]))

    def test_archive_update_with_referer_redirects_back(self):
        response = self.client.post(reverse("viewer:archive-update", args=[123]), HTTP_REFERER="/search/?q=x")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/search/?q=x")

    def test_archive_update_ignores_foreign_referer(self):
        response = self.client.post(
            reverse("viewer:archive-update", args=[123]), HTTP_REFERER="https://evil.example/phish"
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("viewer:archive", args=[123]))

    def test_render_error_ignores_foreign_referer(self):
        # Non-staff users hitting the tools page get render_error().
        response = self.client.get(reverse("viewer:tools"), HTTP_REFERER="https://evil.example/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("viewer:main-page"))
