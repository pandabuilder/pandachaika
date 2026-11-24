"""The providers' ssl_verify setting: requests' verify for the provider's requests and its thumbnails."""

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from core.base.types import ProviderSettings, parse_ssl_verify
from core.base.utilities import construct_request_dict
from core.providers.doujinshi_mirror.parsers import Parser
from viewer.models import Gallery

CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


class ParseSslVerifyTests(SimpleTestCase):
    def test_values(self):
        for value, expected in [
            (True, True),
            (False, False),
            (None, True),
            ("", True),
            ("true", True),
            ("False", False),
            ("no", False),
            ("0", False),
            (" {} ".format(CA_BUNDLE), CA_BUNDLE),
        ]:
            with self.subTest(value=value):
                self.assertEqual(parse_ssl_verify(value), expected)


class RequestDictTests(SimpleTestCase):
    def own_settings(self, general: dict) -> ProviderSettings:
        return ProviderSettings(settings.CRAWLER_SETTINGS, {"general": general})

    def test_default_verifies_with_requests_own_cas(self):
        own_settings = self.own_settings({})
        self.assertIs(own_settings.ssl_verify, True)
        self.assertNotIn("verify", construct_request_dict(settings.CRAWLER_SETTINGS, own_settings))

    def test_disabled(self):
        request_dict = construct_request_dict(settings.CRAWLER_SETTINGS, self.own_settings({"ssl_verify": False}))
        self.assertIs(request_dict["verify"], False)

    def test_ca_bundle(self):
        request_dict = construct_request_dict(settings.CRAWLER_SETTINGS, self.own_settings({"ssl_verify": CA_BUNDLE}))
        self.assertEqual(request_dict["verify"], CA_BUNDLE)


class ProviderRequestsTests(TestCase):
    def setUp(self):
        super().setUp()
        own_settings = settings.CRAWLER_SETTINGS.providers["doujinshi_mirror"]
        for name, value in (("url", "https://mirror.test/"), ("ssl_verify", CA_BUNDLE)):
            patcher = mock.patch.object(own_settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_api_requests(self):
        with mock.patch.object(Parser, "web_request", return_value=None) as web_request:
            Parser(settings.CRAWLER_SETTINGS).fetch_books([1])
        self.assertEqual(web_request.call_args.args[1]["verify"], CA_BUNDLE)

    def test_thumbnail_fetch(self):
        from viewer.services import gallery_ingestion

        gallery = Gallery.objects.create(
            gid="mugi-B7", provider="doujinshi_mirror", thumbnail_url="https://img.doujinshi.org/tn/3/7.jpg"
        )
        with mock.patch.object(gallery_ingestion, "request_with_retries", return_value=None) as request:
            gallery.fetch_thumbnail()
        self.assertEqual(request.call_args.args[1]["verify"], CA_BUNDLE)
