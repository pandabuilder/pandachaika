# -*- coding: utf-8 -*-
from unittest.mock import patch, MagicMock
import time
from django.test import TestCase

from core.base.utilities import (
    parse_panda_ban_duration,
    set_provider_cooldown,
    acquire_provider_rate_limit_slot,
    request_by_provider,
    PROVIDER_COOLDOWN_UNTIL,
    REQUESTER_BY_PROVIDER,
    PROVIDER_RATE_LIMIT_CONFIG,
)
from core.providers.panda.parsers import Parser
from core.base.setup import Settings
from workers.models import ProviderRateLimit


class PandaRateLimitBanHandlingTests(TestCase):
    def setUp(self):
        super().setUp()
        PROVIDER_COOLDOWN_UNTIL.clear()
        REQUESTER_BY_PROVIDER.clear()
        PROVIDER_RATE_LIMIT_CONFIG.clear()
        ProviderRateLimit.objects.filter(name__in=["panda", "panda_web", "test_prov"]).delete()

    def tearDown(self):
        super().tearDown()
        PROVIDER_COOLDOWN_UNTIL.clear()
        REQUESTER_BY_PROVIDER.clear()
        PROVIDER_RATE_LIMIT_CONFIG.clear()
        ProviderRateLimit.objects.filter(name__in=["panda", "panda_web", "test_prov"]).delete()

    def test_parse_panda_ban_duration_exact_message(self):
        text = (
            "This IP address has been temporarily banned due to an excessive request rate. "
            "This probably means you are using automated mirroring/harvesting software or "
            "share the IP address with someone who does. The ban expires in 4 minutes and 51 seconds"
        )
        duration = parse_panda_ban_duration(text)
        self.assertEqual(duration, 291.0)

    def test_parse_panda_ban_duration_variations(self):
        def ban(expiry: str) -> str:
            return "This IP address has been temporarily banned. The ban expires in " + expiry

        # Seconds only
        self.assertEqual(parse_panda_ban_duration(ban("45 seconds.")), 45.0)
        self.assertEqual(parse_panda_ban_duration(ban("1 second")), 1.0)

        # Minutes only
        self.assertEqual(parse_panda_ban_duration(ban("5 minutes")), 300.0)
        self.assertEqual(parse_panda_ban_duration(ban("1 minute")), 60.0)

        # Hours, minutes, seconds
        self.assertEqual(
            parse_panda_ban_duration(ban("2 hours, 10 minutes and 5 seconds")),
            2 * 3600 + 10 * 60 + 5.0,
        )

        # Days
        self.assertEqual(parse_panda_ban_duration(ban("1 days")), 86400.0)
        self.assertEqual(parse_panda_ban_duration(ban("2 days")), 172800.0)

        # HTML formatted
        self.assertEqual(parse_panda_ban_duration(ban("<b>4 minutes and 51 seconds</b>.")), 291.0)

        # Non-ban messages
        self.assertIsNone(parse_panda_ban_duration('{"gmetadata": []}'))
        self.assertIsNone(parse_panda_ban_duration("An ordinary error occurred."))
        self.assertIsNone(parse_panda_ban_duration(""))
        self.assertIsNone(parse_panda_ban_duration(None))

    def test_parse_panda_ban_duration_needs_both_phrases(self):
        self.assertIsNone(parse_panda_ban_duration("The ban expires in 45 seconds."))
        self.assertIsNone(parse_panda_ban_duration("You have been temporarily banned from the forums."))

    def test_parse_panda_ban_duration_ignores_pages_quoting_a_ban(self):
        # A gallery page whose comments quote a ban message is not a ban page.
        page = (
            "<html><body><div id='gd2'><h1>Some gallery</h1></div>"
            + "<div class='c1'>" + "x" * 3000 + "</div>"
            + "<div class='c6'>Got this today: This IP address has been temporarily banned due to an excessive "
            "request rate. The ban expires in 1 days</div></body></html>"
        )
        self.assertIsNone(parse_panda_ban_duration(page))

    def test_set_provider_cooldown_updates_db_and_memory(self):
        cooldown_seconds = 120.0
        now = time.time()
        until = set_provider_cooldown("test_prov", cooldown_seconds, url="http://test.com")

        self.assertAlmostEqual(until, now + cooldown_seconds, delta=2.0)
        self.assertAlmostEqual(PROVIDER_COOLDOWN_UNTIL["test_prov"], until, delta=0.1)

        db_entry = ProviderRateLimit.objects.get(name="test_prov")
        self.assertAlmostEqual(db_entry.cooldown_until, until, delta=0.1)

    def test_set_provider_cooldown_syncs_panda_and_panda_web(self):
        cooldown_seconds = 200.0
        now = time.time()
        until = set_provider_cooldown("panda", cooldown_seconds, url="https://api.e-hentai.org/api.php")

        self.assertAlmostEqual(PROVIDER_COOLDOWN_UNTIL["panda"], until, delta=0.1)
        self.assertAlmostEqual(PROVIDER_COOLDOWN_UNTIL["panda_web"], until, delta=0.1)

        db_panda = ProviderRateLimit.objects.get(name="panda")
        db_panda_web = ProviderRateLimit.objects.get(name="panda_web")
        self.assertAlmostEqual(db_panda.cooldown_until, until, delta=0.1)
        self.assertAlmostEqual(db_panda_web.cooldown_until, until, delta=0.1)

    @patch("time.sleep")
    def test_acquire_provider_rate_limit_slot_waits_for_cooldown(self, mock_sleep):
        provider = "test_prov"
        cooldown_secs = 50.0
        set_provider_cooldown(provider, cooldown_secs, url="http://test.com")

        # First call sees active cooldown, sleeps remaining cooldown, then acquires slot
        # Mocking time.time to simulate passage of time after sleep
        real_time = time.time
        current_time = [real_time()]

        def fake_time():
            return current_time[0]

        def fake_sleep(secs):
            current_time[0] += secs

        mock_sleep.side_effect = fake_sleep

        with patch("time.time", side_effect=fake_time):
            acquired = acquire_provider_rate_limit_slot(provider, 1, 1.0, url="http://test.com")

        self.assertTrue(acquired)
        self.assertTrue(mock_sleep.called)
        # Sleep should have waited roughly cooldown_secs
        first_sleep_arg = mock_sleep.call_args_list[0][0][0]
        self.assertAlmostEqual(first_sleep_arg, cooldown_secs, delta=2.0)

        # After cooldown has expired, cooldown_until in DB should be reset to 0
        db_entry = ProviderRateLimit.objects.get(name=provider)
        self.assertEqual(db_entry.cooldown_until, 0.0)

    @patch("core.base.utilities.request_with_retries")
    @patch("time.sleep")
    def test_request_by_provider_detects_ban_and_retries(self, mock_sleep, mock_request):
        ban_msg = (
            "This IP address has been temporarily banned due to an excessive request rate. "
            "The ban expires in 4 minutes and 51 seconds"
        )
        ban_response = MagicMock()
        ban_response.text = ban_msg
        ban_response.status_code = 200

        success_response = MagicMock()
        success_response.text = '{"gmetadata": [{"gid": 123}]}'
        success_response.json.return_value = {"gmetadata": [{"gid": 123}]}
        success_response.status_code = 200

        # First call returns ban, second call returns success
        mock_request.side_effect = [ban_response, success_response]

        real_time = time.time
        current_time = [real_time()]

        def fake_time():
            return current_time[0]

        def fake_sleep(secs):
            current_time[0] += secs

        mock_sleep.side_effect = fake_sleep

        with patch("time.time", side_effect=fake_time):
            req_fn = request_by_provider("panda", 1, 2.5)
            resp = req_fn("https://api.e-hentai.org/api.php", {})

        self.assertEqual(resp, success_response)
        self.assertEqual(mock_request.call_count, 2)
        # Verify sleep was called with ban duration + 5.0 = 296.0s
        sleep_durations = [call[0][0] for call in mock_sleep.call_args_list]
        self.assertTrue(any(abs(dur - 296.0) < 2.0 for dur in sleep_durations))

    def test_panda_parser_helper_handles_ban_response(self):
        from core.providers.panda.settings import OwnSettings as PandaSettings

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        parser = Parser(settings)

        ban_msg = (
            "This IP address has been temporarily banned due to an excessive request rate. "
            "The ban expires in 4 minutes and 51 seconds"
        )
        response = MagicMock()
        response.text = ban_msg
        response.json.side_effect = ValueError("No JSON object could be decoded")

        parsed = parser._parse_api_response_json(response, "https://api.e-hentai.org/api.php")
        self.assertIsNone(parsed)

        # Verify dynamic cooldown was set on provider
        self.assertIn("panda", PROVIDER_COOLDOWN_UNTIL)
        self.assertGreater(PROVIDER_COOLDOWN_UNTIL["panda"], time.time() + 280)


BAN_LONG = "This IP address has been temporarily banned. The ban expires in 1 hours and 2 minutes"
BAN_SHORT = "This IP address has been temporarily banned. The ban expires in 4 minutes and 51 seconds"


class ProviderBanEscalationTests(TestCase):
    def setUp(self):
        super().setUp()
        PROVIDER_COOLDOWN_UNTIL.clear()
        REQUESTER_BY_PROVIDER.clear()
        PROVIDER_RATE_LIMIT_CONFIG.clear()
        ProviderRateLimit.objects.filter(name__in=["panda", "panda_web", "panda_test", "test_prov"]).delete()
        self.addCleanup(PROVIDER_COOLDOWN_UNTIL.clear)
        self.addCleanup(REQUESTER_BY_PROVIDER.clear)
        self.addCleanup(PROVIDER_RATE_LIMIT_CONFIG.clear)

    @staticmethod
    def _response(text, content_type="text/html"):
        response = MagicMock()
        response.text = text
        response.status_code = 200
        response.headers = {"Content-Type": content_type}
        return response

    @patch("core.base.utilities.request_with_retries")
    @patch("time.sleep")
    def test_long_ban_raises_instead_of_returning_the_ban_page(self, mock_sleep, mock_request):
        from core.base.errors import ProviderBannedError

        mock_request.return_value = self._response(BAN_LONG)
        req_fn = request_by_provider("panda_test", 1, 0.01)
        with self.assertRaises(ProviderBannedError) as ctx:
            req_fn("https://example.com/x", {})
        self.assertGreater(ctx.exception.retry_after, 3600)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_request.call_count, 1)

    @patch("core.base.utilities.request_with_retries")
    @patch("time.sleep")
    def test_ban_that_persists_after_retry_raises(self, mock_sleep, mock_request):
        from core.base.errors import ProviderBannedError

        mock_request.side_effect = [self._response(BAN_SHORT), self._response(BAN_SHORT)]
        current_time = [time.time()]
        mock_sleep.side_effect = lambda secs: current_time.__setitem__(0, current_time[0] + secs)
        req_fn = request_by_provider("panda_test", 1, 0.01)
        with patch("time.time", side_effect=lambda: current_time[0]):
            with self.assertRaises(ProviderBannedError):
                req_fn("https://example.com/x", {})
        self.assertEqual(mock_request.call_count, 2)

    def test_slot_acquisition_raises_for_long_cooldown(self):
        from core.base.errors import ProviderBannedError

        set_provider_cooldown("test_prov", 7200, url="")
        with self.assertRaises(ProviderBannedError):
            acquire_provider_rate_limit_slot("test_prov", 1, 1.0)

    @patch("core.base.utilities.request_with_retries")
    @patch("time.sleep")
    def test_other_providers_are_not_checked_for_ban_pages(self, mock_sleep, mock_request):
        response = self._response(BAN_LONG)
        mock_request.return_value = response
        req_fn = request_by_provider("test_prov", 1, 0.01)
        self.assertIs(req_fn("https://example.com/x", {}), response)
        self.assertNotIn("test_prov", PROVIDER_COOLDOWN_UNTIL)
        mock_sleep.assert_not_called()

    @patch("core.base.utilities.request_with_retries")
    def test_binary_responses_are_not_decoded_to_look_for_bans(self, mock_request):
        response = self._response("", content_type="image/jpeg")
        type(response).text = property(lambda _: (_ for _ in ()).throw(AssertionError("decoded binary body")))
        mock_request.return_value = response
        req_fn = request_by_provider("panda_test", 1, 0.01)
        self.assertIs(req_fn("https://example.com/cover.jpg", {}), response)

    @patch("core.base.utilities.request_with_retries")
    def test_big_text_bodies_are_not_scanned(self, mock_request):
        response = self._response(BAN_LONG)
        response.headers = {"Content-Type": "text/html", "Content-Length": str(10 * 1024 * 1024)}
        mock_request.return_value = response
        req_fn = request_by_provider("panda_test", 1, 0.01)
        self.assertIs(req_fn("https://example.com/huge", {}), response)

    def test_interruptible_sleep_stops_on_cancel(self):
        from core.base.cancellation import (
            TaskCancelledError,
            clear_task_hooks,
            interruptible_sleep,
            set_task_hooks,
        )

        pings = []
        cancelled = {"value": False}

        def fake_sleep(_secs):
            if len(pings) >= 2:
                cancelled["value"] = True

        set_task_hooks(lambda: pings.append(1), lambda: cancelled["value"])
        self.addCleanup(clear_task_hooks)
        with patch("time.sleep", side_effect=fake_sleep):
            with self.assertRaises(TaskCancelledError):
                interruptible_sleep(100)
        self.assertGreaterEqual(len(pings), 2)
        self.assertLess(len(pings), 10)


class BannedTaskRequeueTests(TestCase):
    def test_banned_task_is_delayed_and_keeps_its_attempts(self):
        from core.base.errors import ProviderBannedError
        from workers.models import TaskQueue
        from workers.runner import WorkerRunner
        from workers.tests.factories import TaskQueueFactory

        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        task = TaskQueueFactory(args=["https://example.com/a"])
        claimed = runner.claim_next_job()
        self.assertEqual(claimed.attempts, 1)

        with patch("workers.runner.WebCrawler") as crawler:
            crawler.return_value.start_crawling.side_effect = ProviderBannedError("panda", 3600)
            runner.execute_job(claimed)

        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.attempts, 0)
        self.assertIsNotNone(task.not_before)
        self.assertGreater((task.not_before - task.created_at).total_seconds(), 3500)
        self.assertIn("temporarily banning", task.error_message)
        # And it isn't picked up while the ban lasts.
        self.assertIsNone(runner.claim_next_job())

    def test_ban_inside_a_provider_crawl_thread_delays_the_task(self):
        """The crawl of each provider runs in its own thread: a ban raised there must still reach the worker."""
        from core.base.errors import ProviderBannedError
        from workers.models import TaskQueue
        from workers.runner import WorkerRunner
        from workers.tests.factories import TaskQueueFactory

        runner = WorkerRunner(Settings(load_from_disk=True), enable_schedulers=False)
        task = TaskQueueFactory(args=["https://e-hentai.org/g/123/abcdef1234/"])
        claimed = runner.claim_next_job()

        with patch(
            "core.providers.panda.parsers.Parser._crawl_urls", side_effect=ProviderBannedError("panda_web", 3600)
        ) as crawl:
            runner.execute_job(claimed)

        crawl.assert_called_once()
        task.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.PENDING)
        self.assertEqual(task.attempts, 0)
        self.assertGreater((task.not_before - task.created_at).total_seconds(), 3500)
