# -*- coding: utf-8 -*-
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

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_record_and_check_processed_links_off_by_default(self, mock_processed_links, mock_provider_filter):
        """When stop_on_seen_links is False, all links are added and none trigger a stop."""
        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        # No links previously seen
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
        self.assertIn("https://e-hentai.org/g/1001/token1/", unique_urls)
        self.assertIn("https://e-hentai.org/g/1002/token2/", unique_urls)
        mock_processed_links.bulk_create.assert_called_once()
        created_records = mock_processed_links.bulk_create.call_args[0][0]
        self.assertEqual(len(created_records), 2)
        self.assertEqual(created_records[0].source_id, "1001")
        self.assertIsNone(created_records[0].monitored_link)

    @patch("viewer.models.Provider.objects.filter")
    @patch("viewer.models.ProcessedLinks.objects")
    def test_record_and_check_processed_links_stops_when_on_and_seen(self, mock_processed_links, mock_provider_filter):
        """When stop_on_seen_links is True, processing stops at the first previously seen link."""
        self.parser.own_settings.stop_on_seen_links = True

        test_provider = Provider(name="Panda", slug="panda")
        mock_provider_filter.return_value.first.return_value = test_provider

        # Link 1002 has already been seen
        mock_query = MagicMock()
        mock_query.filter.return_value = mock_query
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
        # Link 1001 was added before stopping
        self.assertIn("https://e-hentai.org/g/1001/token1/", unique_urls)
        # Seen link 1002 and subsequent link 1003 were not added
        self.assertNotIn("https://e-hentai.org/g/1002/token2/", unique_urls)
        self.assertNotIn("https://e-hentai.org/g/1003/token3/", unique_urls)

        # 1001 was queued for bulk_create
        mock_processed_links.bulk_create.assert_called_once()
        created_records = mock_processed_links.bulk_create.call_args[0][0]
        self.assertEqual(len(created_records), 1)
        self.assertEqual(created_records[0].source_id, "1001")

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
        mock_filter_res.filter.return_value.values_list.return_value = []
        mock_processed_links.filter.return_value = mock_filter_res

        unique_urls = set()
        test_urls = ["https://e-hentai.org/g/2001/token1/"]

        should_stop = self.parser._record_and_check_processed_links(test_urls, unique_urls)
        self.assertFalse(should_stop)

        # Verify filter was called with provider and source_id, then scoped by monitored_link
        mock_processed_links.filter.assert_called_with(provider=test_provider, source_id__in=["2001"])
        mock_filter_res.filter.assert_called_with(monitored_link=ml1)

        # Verify created record is linked to ml1
        created_records = mock_processed_links.bulk_create.call_args[0][0]
        self.assertEqual(created_records[0].monitored_link, ml1)

    @patch("core.workers.link_monitor.close_old_connections")
    @patch("viewer.models.MonitoredLink.objects.get")
    def test_link_monitor_override_options(self, mock_ml_get, _mock_close):
        """LinkMonitor should override stop_on_seen_links on current_settings and providers."""
        from datetime import timedelta
        from core.workers.link_monitor import LinkMonitor

        ml_on = MonitoredLink(
            id=10,
            name="ML On Test",
            url="https://example.com/feed",
            enabled=True,
            priority=10,
            frequency=timedelta(hours=1),
            stop_on_seen_link=MonitoredLink.StopOnSeenChoices.ON,
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
        for prov in override.providers.values():
            self.assertTrue(prov.stop_on_seen_links)
            self.assertEqual(prov.monitored_link, ml_on)

    def test_serialization_of_monitored_link_and_stop_on_seen(self):
        """Serialization should preserve monitored_link_id and stop_on_seen_links across worker boundaries."""
        settings = Settings(load_from_disk=False)
        settings.stop_on_seen_links = True
        ml = MagicMock(spec=MonitoredLink, pk=77)
        settings.monitored_link = ml
        if "panda" in settings.providers:
            settings.providers["panda"].stop_on_seen_links = True
            settings.providers["panda"].monitored_link = ml

        data = serialize_settings_options(settings)
        self.assertEqual(data.get("monitored_link_id"), 77)
        self.assertTrue(data.get("stop_on_seen_links"))

        with patch("viewer.models.MonitoredLink.objects.filter") as mock_filter:
            mock_filter.return_value.first.return_value = ml
            hydrated = deserialize_settings_options(settings, data)
            self.assertEqual(hydrated.monitored_link, ml)
            if "panda" in hydrated.providers:
                self.assertEqual(hydrated.providers["panda"].monitored_link, ml)
                self.assertTrue(hydrated.providers["panda"].stop_on_seen_links)

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
            # Only called for 1 page because should_stop was True
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

    @patch("core.workers.link_monitor.close_old_connections")
    @patch("viewer.models.MonitoredLink.objects.get")
    def test_link_monitor_override_off_and_follow_global(self, mock_ml_get, _mock_close):
        """LinkMonitor respects OFF and FOLLOW_GLOBAL."""
        from datetime import timedelta
        from core.workers.link_monitor import LinkMonitor

        # Test OFF: even if global was True, override should turn it off
        self.settings.config.setdefault("general", {})["stop_on_seen_links"] = True
        self.settings.stop_on_seen_links = True
        for prov in self.settings.providers.values():
            prov.stop_on_seen_links = True

        ml_off = MonitoredLink(
            id=11,
            name="ML Off Test",
            url="https://example.com/feed",
            enabled=True,
            priority=10,
            frequency=timedelta(hours=1),
            stop_on_seen_link=MonitoredLink.StopOnSeenChoices.OFF,
        )
        mock_ml_get.return_value = ml_off

        web_queue = MagicMock()
        monitor = LinkMonitor(
            settings=self.settings,
            link_monitor_id=ml_off.pk,
            name=ml_off.name,
            timer=ml_off.frequency,
            monitored_link=ml_off,
            web_queue=web_queue,
        )
        with patch.object(monitor, "wait_until_next_run", return_value=0), \
             patch.object(monitor.stop, "wait", side_effect=[False, True]), \
             patch.object(monitor, "update_last_run"):
            monitor.job()

        override = web_queue.enqueue_args_list.call_args[1]["override_options"]
        self.assertFalse(override.stop_on_seen_links)
        for prov in override.providers.values():
            self.assertFalse(prov.stop_on_seen_links)

        # Test FOLLOW_GLOBAL: keeps whatever global was set to
        ml_follow = MonitoredLink(
            id=12,
            name="ML Follow Test",
            url="https://example.com/feed",
            enabled=True,
            priority=10,
            frequency=timedelta(hours=1),
            stop_on_seen_link=MonitoredLink.StopOnSeenChoices.FOLLOW_GLOBAL,
        )
        mock_ml_get.return_value = ml_follow
        web_queue.reset_mock()

        monitor_follow = LinkMonitor(
            settings=self.settings,
            link_monitor_id=ml_follow.pk,
            name=ml_follow.name,
            timer=ml_follow.frequency,
            monitored_link=ml_follow,
            web_queue=web_queue,
        )
        with patch.object(monitor_follow, "wait_until_next_run", return_value=0), \
             patch.object(monitor_follow.stop, "wait", side_effect=[False, True]), \
             patch.object(monitor_follow, "update_last_run"):
            monitor_follow.job()

        override_follow = web_queue.enqueue_args_list.call_args[1]["override_options"]
        self.assertTrue(override_follow.stop_on_seen_links)


if __name__ == "__main__":
    unittest.main()
