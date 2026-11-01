# -*- coding: utf-8 -*-
from datetime import timedelta
import os
import unittest
from unittest.mock import MagicMock, patch

import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pandabackup.settings")
django.setup()

from core.base.setup import Settings
from core.base.types import ProviderSettings
from core.providers.panda.parsers import Parser
from viewer.models import MonitoredLink, ProcessedLinks, Provider
from workers.serialization import deserialize_settings_options, serialize_settings_options


class ProcessedLinksStopOnSeenUnitTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from django_db_logger.models import StatusLog
        cls.status_log_patcher = patch.object(StatusLog.objects, "create")
        cls.status_log_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls.status_log_patcher.stop()
        super().tearDownClass()

    def setUp(self):
        self.settings = Settings(load_from_disk=True)
        self.parser = Parser(self.settings)

    def test_default_setting_is_off(self):
        """stop_on_seen_links should default to False in global Settings, ProviderSettings, and panda own_settings."""
        self.assertFalse(self.settings.stop_on_seen_links)
        self.assertFalse(self.settings.stop_on_seen_link)
        panda_settings = self.settings.providers.get("panda")
        if panda_settings:
            self.assertFalse(panda_settings.stop_on_seen_links)
            self.assertFalse(panda_settings.stop_on_seen_link)

    def test_monitored_link_stop_on_seen_choices(self):
        """MonitoredLink should support follow_global, on, and off."""
        self.assertEqual(MonitoredLink.StopOnSeenChoices.FOLLOW_GLOBAL, "follow_global")
        self.assertEqual(MonitoredLink.StopOnSeenChoices.ON, "on")
        self.assertEqual(MonitoredLink.StopOnSeenChoices.OFF, "off")

        ml = MonitoredLink(name="Test Link", url="https://example.com", stop_on_seen_link=MonitoredLink.StopOnSeenChoices.FOLLOW_GLOBAL)
        self.assertEqual(ml.stop_on_seen_link, "follow_global")
        self.assertEqual(ml.stop_on_seen, "follow_global")
        self.assertEqual(ml.stop_on_seen_links, "follow_global")

        ml.stop_on_seen = "on"
        self.assertEqual(ml.stop_on_seen_link, "on")
        ml.stop_on_seen_links = "off"
        self.assertEqual(ml.stop_on_seen_link, "off")

    def test_monitored_link_already_seen_choices_and_duration(self):
        """MonitoredLink should support already_seen_links_mode choices and how_long_after_already_seen duration."""
        self.assertEqual(MonitoredLink.AlreadySeenLinksMode.DONT_USE, "dont_use")
        self.assertEqual(MonitoredLink.AlreadySeenLinksMode.USE_BEFORE_NOW, "use_before_now")

        ml = MonitoredLink(
            name="Test Link",
            url="https://example.com",
            already_seen_links_mode=MonitoredLink.AlreadySeenLinksMode.DONT_USE,
            how_long_after_already_seen=timedelta(hours=2),
        )
        self.assertEqual(ml.already_seen_links_mode, "dont_use")
        self.assertEqual(ml.already_seen_mode, "dont_use")
        self.assertEqual(ml.how_long_after_already_seen_mode, "dont_use")
        self.assertEqual(ml.how_long_after_already_seen, timedelta(hours=2))
        self.assertEqual(ml.already_seen_duration, timedelta(hours=2))
        self.assertEqual(ml.seen_links_window, timedelta(hours=2))

        ml.already_seen_mode = "use_before_now"
        self.assertEqual(ml.already_seen_links_mode, "use_before_now")
        ml.already_seen_duration = timedelta(days=1)
        self.assertEqual(ml.how_long_after_already_seen, timedelta(days=1))

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_on_demand_pages_bypass_stop_on_seen_and_link_capture(self, mock_processed_links, mock_provider_filter):
        """On-demand pages (monitored_link=None) must NOT capture links or stop on seen links."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        # Even if global stop_on_seen_links is True
        self.parser.own_settings.stop_on_seen_links = True
        self.parser.own_settings.monitored_link = None
        self.parser.settings.monitored_link = None

        unique_urls = set()
        test_urls = [
            "https://e-hentai.org/g/1001/token1/",
            "https://e-hentai.org/g/1002/token2/",
        ]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertFalse(should_stop)
        self.assertEqual(len(unique_urls), 2)
        self.assertIn("https://e-hentai.org/g/1001/token1/", unique_urls)
        self.assertIn("https://e-hentai.org/g/1002/token2/", unique_urls)

        # ProcessedLinks should NOT be queried or created for on-demand crawls
        mock_processed_links.filter.assert_not_called()
        mock_processed_links.bulk_create.assert_not_called()

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_monitored_link_captures_links_when_stop_on_seen_off(self, mock_processed_links, mock_provider_filter):
        """When monitored_link is present and stop_on_seen_links is False, links are captured and crawling continues."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        ml = MonitoredLink(id=1, name="Test ML", url="https://example.com", stop_on_seen_link=MonitoredLink.StopOnSeenChoices.OFF)
        self.parser.own_settings.monitored_link = ml
        self.parser.own_settings.stop_on_seen_links = False

        mock_query = MagicMock()
        mock_query.values_list.return_value = []
        mock_processed_links.filter.return_value = mock_query

        unique_urls = set()
        test_urls = [
            "https://e-hentai.org/g/1001/token1/",
            "https://e-hentai.org/g/1002/token2/",
        ]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertFalse(should_stop)
        self.assertEqual(len(unique_urls), 2)

        mock_processed_links.bulk_create.assert_called_once()
        created_records = mock_processed_links.bulk_create.call_args[0][0]
        self.assertEqual(len(created_records), 2)
        self.assertEqual(created_records[0].monitored_link, ml)

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_record_and_check_processed_links_stops_when_on_and_seen(self, mock_processed_links, mock_provider_filter):
        """When stop_on_seen_links is True for a monitored link, processing stops at the first previously seen link."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        ml = MonitoredLink(id=1, name="Test ML", url="https://example.com", stop_on_seen_link=MonitoredLink.StopOnSeenChoices.ON)
        self.parser.own_settings.monitored_link = ml
        self.parser.own_settings.stop_on_seen_links = True

        mock_query = MagicMock()
        mock_query.values_list.return_value = ["1002"]
        mock_processed_links.filter.return_value = mock_query

        unique_urls = set()
        test_urls = [
            "https://e-hentai.org/g/1001/token1/",  # new
            "https://e-hentai.org/g/1002/token2/",  # seen before -> stop!
            "https://e-hentai.org/g/1003/token3/",  # should not be reached
        ]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertTrue(should_stop)
        self.assertIn("https://e-hentai.org/g/1001/token1/", unique_urls)
        self.assertNotIn("https://e-hentai.org/g/1002/token2/", unique_urls)
        self.assertNotIn("https://e-hentai.org/g/1003/token3/", unique_urls)

        mock_processed_links.bulk_create.assert_called_once()
        created_records = mock_processed_links.bulk_create.call_args[0][0]
        self.assertEqual(len(created_records), 1)
        self.assertEqual(created_records[0].source_id, "1001")
        self.assertEqual(created_records[0].monitored_link, ml)

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_monitored_link_use_already_seen_window(self, mock_processed_links, mock_provider_filter):
        """When already seen link is found and mode is USE_BEFORE_NOW, loads links from seen table within window."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        ml = MonitoredLink(
            id=1,
            name="Window ML",
            url="https://example.com",
            stop_on_seen_link=MonitoredLink.StopOnSeenChoices.ON,
            already_seen_links_mode=MonitoredLink.AlreadySeenLinksMode.USE_BEFORE_NOW,
            how_long_after_already_seen=timedelta(hours=3),
        )
        self.parser.own_settings.monitored_link = ml
        self.parser.own_settings.stop_on_seen_links = True

        # First query: checking if current page has seen links
        # Second query: fetching recent seen links within time window
        check_query = MagicMock()
        check_query.values_list.return_value = ["1002"]

        window_query = MagicMock()
        window_query.values_list.return_value = [
            "https://e-hentai.org/g/9001/tok9001/",
            "https://e-hentai.org/g/9002/tok9002/",
        ]

        def filter_side_effect(*args, **kwargs):
            if "source_id__in" in kwargs:
                return check_query
            if "create_date__gte" in kwargs:
                return window_query
            return MagicMock()

        mock_processed_links.filter.side_effect = filter_side_effect

        unique_urls = set()
        test_urls = [
            "https://e-hentai.org/g/1001/token1/",
            "https://e-hentai.org/g/1002/token2/",
        ]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertTrue(should_stop)

        # Newly seen link from current page was added
        self.assertIn("https://e-hentai.org/g/1001/token1/", unique_urls)
        # Seen links within the time window were added
        self.assertIn("https://e-hentai.org/g/9001/tok9001/", unique_urls)
        self.assertIn("https://e-hentai.org/g/9002/tok9002/", unique_urls)

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_monitored_link_dont_use_already_seen(self, mock_processed_links, mock_provider_filter):
        """When mode is DONT_USE, seen table is not queried for recent links when stopping."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        ml = MonitoredLink(
            id=1,
            name="No Window ML",
            url="https://example.com",
            stop_on_seen_link=MonitoredLink.StopOnSeenChoices.ON,
            already_seen_links_mode=MonitoredLink.AlreadySeenLinksMode.DONT_USE,
            how_long_after_already_seen=timedelta(hours=3),
        )
        self.parser.own_settings.monitored_link = ml
        self.parser.own_settings.stop_on_seen_links = True

        check_query = MagicMock()
        check_query.values_list.return_value = ["1002"]
        mock_processed_links.filter.return_value = check_query

        unique_urls = set()
        test_urls = [
            "https://e-hentai.org/g/1001/token1/",
            "https://e-hentai.org/g/1002/token2/",
        ]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertTrue(should_stop)
        self.assertIn("https://e-hentai.org/g/1001/token1/", unique_urls)
        # create_date__gte should NOT have been queried
        for call in mock_processed_links.filter.call_args_list:
            self.assertNotIn("create_date__gte", call.kwargs)

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_monitored_link_isolation(self, mock_processed_links, mock_provider_filter):
        """MonitoredLink scopes its search to links seen by itself."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        ml1 = MonitoredLink(id=42, name="ML1", url="https://example.com/ml1")
        self.parser.own_settings.monitored_link = ml1
        self.parser.own_settings.stop_on_seen_links = True

        mock_filter_res = MagicMock()
        mock_filter_res.values_list.return_value = []
        mock_processed_links.filter.return_value = mock_filter_res

        unique_urls = set()
        test_urls = ["https://e-hentai.org/g/2001/token1/"]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertFalse(should_stop)

        mock_processed_links.filter.assert_called_with(
            provider=test_provider,
            source_id__in=["2001"],
            monitored_link=ml1,
        )

        created_records = mock_processed_links.bulk_create.call_args[0][0]
        self.assertEqual(created_records[0].monitored_link, ml1)

    @patch("core.workers.link_monitor.close_old_connections")
    @patch("viewer.models.MonitoredLink.objects.get")
    def test_link_monitor_override_options(self, mock_ml_get, _mock_close):
        """LinkMonitor should override stop_on_seen_links, already_seen_links_mode, and how_long_after_already_seen."""
        from core.workers.link_monitor import LinkMonitor

        ml_on = MonitoredLink(
            id=10,
            name="ML On Test",
            url="https://example.com/feed",
            enabled=True,
            priority=10,
            frequency=timedelta(hours=1),
            stop_on_seen_link=MonitoredLink.StopOnSeenChoices.ON,
            already_seen_links_mode=MonitoredLink.AlreadySeenLinksMode.USE_BEFORE_NOW,
            how_long_after_already_seen=timedelta(hours=4),
        )
        mock_ml_get.return_value = ml_on

        web_queue = MagicMock()
        monitor = LinkMonitor(
            settings=self.settings,
            link_monitor_id=ml_on.pk,
            name=ml_on.name,
            timer=ml_on.frequency,
            monitored_link=ml_on,
            web_queue=web_queue,
        )

        with patch.object(monitor, "wait_until_next_run", return_value=0), \
             patch.object(monitor.stop, "wait", side_effect=[False, True]), \
             patch.object(monitor, "update_last_run"):
            monitor.job()

        web_queue.enqueue_args_list.assert_called_once()
        _, kwargs = web_queue.enqueue_args_list.call_args
        override = kwargs["override_options"]
        self.assertTrue(override.stop_on_seen_links)
        self.assertEqual(override.monitored_link, ml_on)
        self.assertEqual(override.already_seen_links_mode, "use_before_now")
        self.assertEqual(override.how_long_after_already_seen, timedelta(hours=4))
        for prov in override.providers.values():
            self.assertTrue(prov.stop_on_seen_links)
            self.assertEqual(prov.monitored_link, ml_on)
            self.assertEqual(prov.already_seen_links_mode, "use_before_now")
            self.assertEqual(prov.how_long_after_already_seen, timedelta(hours=4))

    def test_serialization_of_monitored_link_and_already_seen(self):
        """Serialization should preserve monitored_link_id, already_seen_links_mode, and how_long_after_already_seen."""
        settings = Settings(load_from_disk=False)
        settings.stop_on_seen_links = True
        settings.already_seen_links_mode = "use_before_now"
        settings.how_long_after_already_seen = timedelta(hours=5)

        ml = MagicMock(spec=MonitoredLink, pk=77)
        settings.monitored_link = ml
        if "panda" in settings.providers:
            settings.providers["panda"].stop_on_seen_links = True
            settings.providers["panda"].monitored_link = ml
            settings.providers["panda"].already_seen_links_mode = "use_before_now"
            settings.providers["panda"].how_long_after_already_seen = timedelta(hours=5)

        data = serialize_settings_options(settings)
        self.assertEqual(data.get("monitored_link_id"), 77)
        self.assertTrue(data.get("stop_on_seen_links"))
        self.assertEqual(data.get("already_seen_links_mode"), "use_before_now")
        self.assertEqual(data.get("how_long_after_already_seen"), 18000.0)

        with patch("viewer.models.MonitoredLink.objects.filter") as mock_filter:
            mock_filter.return_value.first.return_value = ml
            hydrated = deserialize_settings_options(settings, data)
            self.assertEqual(hydrated.monitored_link, ml)
            self.assertEqual(hydrated.already_seen_links_mode, "use_before_now")
            self.assertEqual(hydrated.how_long_after_already_seen, timedelta(hours=5))
            if "panda" in hydrated.providers:
                self.assertEqual(hydrated.providers["panda"].monitored_link, ml)
                self.assertTrue(hydrated.providers["panda"].stop_on_seen_links)
                self.assertEqual(hydrated.providers["panda"].how_long_after_already_seen, timedelta(hours=5))

    @patch("core.providers.panda.parsers.request_with_retries")
    def test_get_galleries_from_main_page_link_stops_early(self, mock_request):
        """get_galleries_from_main_page_link stops pagination when _record_and_check_processed_links returns True."""
        mock_response = MagicMock()
        mock_response.text = (
            '<html><body>'
            '<a href="https://e-hentai.org/g/3001/tok1/">Link 1</a>'
            '<a href="https://e-hentai.org/g/3002/tok2/">Link 2</a>'
            '</body></html>'
        )
        mock_request.return_value = mock_response

        with patch.object(self.parser, "_record_and_check_processed_links", return_value=True) as mock_record:
            res = self.parser.get_galleries_from_main_page_link("https://e-hentai.org/")
            mock_record.assert_called_once()
            self.assertEqual(mock_request.call_count, 1)

    @patch("core.providers.panda.parsers.request_with_retries")
    def test_get_galleries_from_lofi_page_link_stops_early(self, mock_request):
        """get_galleries_from_lofi_page_link stops pagination when _record_and_check_processed_links returns True."""
        mock_response = MagicMock()
        mock_response.text = (
            '<html><body>'
            '<table><tr><td class="ii"><a href="https://e-hentai.org/lofi/g/4001/tok1/">Lofi 1</a></td></tr></table>'
            '</body></html>'
        )
        mock_request.return_value = mock_response

        with patch.object(self.parser, "_record_and_check_processed_links", return_value=True) as mock_record:
            res = self.parser.get_galleries_from_lofi_page_link("https://e-hentai.org/lofi/")
            mock_record.assert_called_once()
            self.assertEqual(mock_request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
