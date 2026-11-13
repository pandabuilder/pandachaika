# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import MagicMock, patch

import django.utils.timezone as django_tz
from django.test import TestCase

from core.base.setup import Settings
from core.providers.panda import parsers as panda_parsers
from core.providers.panda.parsers import Parser
from core.web.crawler import WebCrawler
from core.workers.link_monitor import LinkMonitor
from viewer.forms import MonitoredLinkCreateOrEditForm
from viewer.models import MonitoredLink, ProcessedLinks, Provider
from viewer.services.retention import get_entity_definitions


def make_link(**kwargs) -> MonitoredLink:
    defaults = {
        "name": "Separate",
        "url": "https://e-hentai.org/?f_search=test",
        "frequency": timedelta(minutes=5),
        "capture_frequency": timedelta(hours=1),
        "separate_frequencies": True,
        "enabled": True,
    }
    defaults.update(kwargs)
    return MonitoredLink.objects.create(**defaults)


def make_monitor(link: MonitoredLink, web_queue: MagicMock) -> LinkMonitor:
    return LinkMonitor(
        settings=Settings(),
        link_monitor_id=link.pk,
        name=link.name,
        timer=link.frequency,
        monitored_link=link,
        web_queue=web_queue,
    )


def run_one_cycle(monitor: LinkMonitor) -> None:
    with patch("core.workers.link_monitor.close_old_connections"), \
         patch.object(monitor.stop, "wait", side_effect=[False, True]):
        monitor.job()


def enqueued_args(web_queue: MagicMock) -> list[list[str]]:
    return [call.args[0] for call in web_queue.enqueue_args_list.call_args_list]


class SeparateFrequenciesModelTests(TestCase):

    def test_off_by_default_and_uses_single_frequency(self):
        link = MonitoredLink.objects.create(name="Plain", url="https://example.com/a", frequency=timedelta(minutes=30))
        self.assertFalse(link.separate_frequencies)
        self.assertFalse(link.uses_separate_frequencies)
        self.assertEqual(link.shortest_interval, timedelta(minutes=30))

    def test_separate_needs_capture_frequency(self):
        link = MonitoredLink(name="x", url="https://example.com/a", frequency=timedelta(minutes=5), separate_frequencies=True)
        self.assertFalse(link.uses_separate_frequencies)
        link.capture_frequency = timedelta(hours=1)
        self.assertTrue(link.uses_separate_frequencies)
        self.assertEqual(link.shortest_interval, timedelta(minutes=5))

    def test_existing_processed_links_count_as_processed(self):
        provider = Provider.objects.create(name="Panda", slug="panda")
        row = ProcessedLinks.objects.create(provider=provider, source_id="1", url="https://e-hentai.org/g/1/abc/")
        self.assertEqual(row.status, ProcessedLinks.Status.PROCESSED)

    def test_retention_never_prunes_pending_links(self):
        provider = Provider.objects.create(name="Panda", slug="panda")
        old = django_tz.now() - timedelta(days=400)
        pending = ProcessedLinks.objects.create(
            provider=provider, source_id="1", url="https://e-hentai.org/g/1/a/", status=ProcessedLinks.Status.PENDING
        )
        done = ProcessedLinks.objects.create(provider=provider, source_id="2", url="https://e-hentai.org/g/2/b/")
        ProcessedLinks.objects.update(create_date=old)
        entity = next(e for e in get_entity_definitions() if e["key"] == "processed_links")
        prunable = list(entity["build_prunable_qs"](django_tz.now() - timedelta(days=30)))
        self.assertEqual(prunable, [done])
        self.assertNotIn(pending, prunable)


class SeparateFrequenciesFormTests(TestCase):

    def form_data(self, **kwargs) -> dict:
        data = {
            "name": "Feed",
            "url": "https://e-hentai.org/?f_search=test",
            "frequency": "00:05:00",
            "priority": 10,
            "stop_on_seen_link": "follow_global",
            "already_seen_links_mode": "dont_use",
        }
        data.update(kwargs)
        return data

    def test_separate_without_capture_frequency_is_invalid(self):
        form = MonitoredLinkCreateOrEditForm(self.form_data(separate_frequencies="on"))
        self.assertFalse(form.is_valid())
        self.assertIn("capture_frequency", form.errors)

    def test_separate_with_zero_capture_frequency_is_invalid(self):
        form = MonitoredLinkCreateOrEditForm(self.form_data(separate_frequencies="on", capture_frequency="00:00:00"))
        self.assertFalse(form.is_valid())
        self.assertIn("capture_frequency", form.errors)

    def test_separate_with_capture_frequency_is_valid(self):
        form = MonitoredLinkCreateOrEditForm(self.form_data(separate_frequencies="on", capture_frequency="01:00:00"))
        self.assertTrue(form.is_valid(), form.errors)
        link = form.save()
        self.assertTrue(link.uses_separate_frequencies)
        self.assertEqual(link.capture_frequency, timedelta(hours=1))

    def test_not_separate_ignores_capture_frequency(self):
        form = MonitoredLinkCreateOrEditForm(self.form_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form.save().uses_separate_frequencies)


class SeparateFrequenciesSchedulerTests(TestCase):

    def test_combined_mode_still_runs_everything_in_one_task(self):
        link = make_link(separate_frequencies=False)
        web_queue = MagicMock()
        run_one_cycle(make_monitor(link, web_queue))
        self.assertEqual(enqueued_args(web_queue), [[link.url, "-wanted"]])

    def test_separate_mode_first_cycle_captures_then_processes(self):
        link = make_link()
        web_queue = MagicMock()
        run_one_cycle(make_monitor(link, web_queue))
        self.assertEqual(
            enqueued_args(web_queue),
            [
                [link.url, "-wanted", "--capture-links-only"],
                [link.url, "-wanted", "--skip-link-capture"],
            ],
        )
        link.refresh_from_db()
        self.assertIsNotNone(link.last_capture_run)
        self.assertIsNotNone(link.last_process_run)

    def test_only_the_due_phase_runs(self):
        now = django_tz.now()
        link = make_link(
            last_capture_run=now - timedelta(minutes=10),
            last_process_run=now - timedelta(minutes=10),
        )
        web_queue = MagicMock()
        monitor = make_monitor(link, web_queue)
        self.assertGreater(monitor.seconds_until_capture(), 49 * 60)
        self.assertEqual(monitor.seconds_until_process(), 0)
        # Wakes up for the shorter timer.
        self.assertEqual(monitor.wait_until_next_run(), 0)
        run_one_cycle(monitor)
        self.assertEqual(enqueued_args(web_queue), [[link.url, "-wanted", "--skip-link-capture"]])
        link.refresh_from_db()
        self.assertEqual(link.last_capture_run, now - timedelta(minutes=10))
        self.assertGreater(link.last_process_run, now - timedelta(minutes=1))

    def test_force_run_runs_both_phases(self):
        now = django_tz.now()
        link = make_link(last_capture_run=now, last_process_run=now)
        web_queue = MagicMock()
        monitor = make_monitor(link, web_queue)
        monitor.force_run_once = True
        run_one_cycle(monitor)
        self.assertEqual(len(enqueued_args(web_queue)), 2)

    def test_switching_from_combined_does_not_fire_both_phases_at_once(self):
        link = make_link(separate_frequencies=False)
        monitor = make_monitor(link, MagicMock())
        monitor.last_run = django_tz.now() - timedelta(minutes=2)
        link.separate_frequencies = True
        monitor.monitored_link = link
        self.assertGreater(monitor.seconds_until_capture(), 57 * 60)
        self.assertLess(monitor.seconds_until_process(), 3 * 60 + 1)

    def test_phase_options_carry_the_monitored_link_settings(self):
        link = make_link(proxy="socks5://127.0.0.1:9050", priority=4)
        web_queue = MagicMock()
        run_one_cycle(make_monitor(link, web_queue))
        for call in web_queue.enqueue_args_list.call_args_list:
            self.assertEqual(call.kwargs["priority"], 4)
            self.assertEqual(call.kwargs["override_options"].proxy, "socks5://127.0.0.1:9050")
            self.assertEqual(call.kwargs["override_options"].monitored_link, link)


class SeparateFrequenciesCrawlerArgsTests(TestCase):

    def test_flags_default_to_off(self):
        args = WebCrawler(Settings()).get_args(["https://e-hentai.org/?f_search=test", "-wanted"])
        self.assertFalse(args.capture_links_only)
        self.assertFalse(args.skip_link_capture)

    def test_flags_parse(self):
        crawler = WebCrawler(Settings())
        args = crawler.get_args(["https://e-hentai.org/?f_search=test", "-wanted", "--capture-links-only"])
        self.assertTrue(args.capture_links_only)
        args = crawler.get_args(["https://e-hentai.org/?f_search=test", "-wanted", "--skip-link-capture"])
        self.assertTrue(args.skip_link_capture)


class PandaLinkCaptureTests(TestCase):

    URL_A = "https://e-hentai.org/g/1001/aaaaaaaaaa/"
    URL_B = "https://e-hentai.org/g/1002/bbbbbbbbbb/"

    def setUp(self):
        self.provider = Provider.objects.create(name="Panda", slug="panda")
        self.link = make_link()
        self.settings = Settings(load_from_disk=True)
        self.settings.monitored_link = self.link
        self.parser = Parser(self.settings)

    def pending(self):
        return set(
            ProcessedLinks.objects.filter(monitored_link=self.link, status=ProcessedLinks.Status.PENDING).values_list(
                "source_id", flat=True
            )
        )

    def test_capture_mode_stores_links_as_pending(self):
        self.settings.capture_links_only = True
        found: set[str] = set()
        self.parser._record_and_check_processed_links([self.URL_A, self.URL_B], found)
        self.assertEqual(self.pending(), {"1001", "1002"})

    def test_capture_does_not_duplicate_seen_links(self):
        self.settings.capture_links_only = True
        self.parser._record_and_check_processed_links([self.URL_A], set())
        self.parser._record_and_check_processed_links([self.URL_A, self.URL_B], set())
        self.assertEqual(ProcessedLinks.objects.filter(monitored_link=self.link).count(), 2)

    def test_combined_mode_records_links_as_processed(self):
        self.parser._record_and_check_processed_links([self.URL_A], set())
        row = ProcessedLinks.objects.get(source_id="1001")
        self.assertEqual(row.status, ProcessedLinks.Status.PROCESSED)

    def test_pending_links_are_loaded_for_processing(self):
        self.settings.capture_links_only = True
        self.parser._record_and_check_processed_links([self.URL_A], set())
        self.settings.capture_links_only = False
        self.settings.skip_link_capture = True
        urls: set[str] = set()
        self.parser._load_pending_links(urls)
        self.assertEqual(urls, {self.URL_A})
        self.assertEqual(self.parser._pending_links, {"1001": ProcessedLinks.objects.get(source_id="1001").pk})

    def test_capture_run_does_not_load_pending_links(self):
        self.settings.capture_links_only = True
        self.parser._record_and_check_processed_links([self.URL_A], set())
        urls: set[str] = set()
        self.parser._load_pending_links(urls)
        self.assertEqual(urls, set())

    def test_pending_links_of_other_monitored_links_are_left_alone(self):
        other = make_link(name="Other", url="https://e-hentai.org/?f_search=other")
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=other, source_id="1001", url=self.URL_A,
            status=ProcessedLinks.Status.PENDING,
        )
        urls: set[str] = set()
        self.parser._load_pending_links(urls)
        self.assertEqual(urls, set())

    def test_resolved_links_become_processed_and_the_rest_are_retried(self):
        self.settings.capture_links_only = True
        self.parser._record_and_check_processed_links([self.URL_A, self.URL_B], set())
        self.settings.capture_links_only = False
        self.parser._load_pending_links(set())
        self.parser._resolved_links.add("1001")
        self.parser._finish_pending_links()

        done = ProcessedLinks.objects.get(source_id="1001")
        self.assertEqual(done.status, ProcessedLinks.Status.PROCESSED)
        self.assertIsNotNone(done.processed_date)
        retry = ProcessedLinks.objects.get(source_id="1002")
        self.assertEqual(retry.status, ProcessedLinks.Status.PENDING)
        self.assertEqual(retry.process_attempts, 1)

    def test_links_that_keep_failing_are_given_up_on(self):
        self.settings.capture_links_only = True
        self.parser._record_and_check_processed_links([self.URL_A], set())
        self.settings.capture_links_only = False
        for _ in range(panda_parsers.MAX_LINK_PROCESS_ATTEMPTS):
            self.parser._load_pending_links(set())
            self.parser._finish_pending_links()
        self.assertEqual(ProcessedLinks.objects.get(source_id="1001").status, ProcessedLinks.Status.FAILED)
        urls: set[str] = set()
        self.parser._load_pending_links(urls)
        self.assertEqual(urls, set())

    def test_recent_links_window_is_applied_by_the_process_run(self):
        self.link.already_seen_links_mode = MonitoredLink.AlreadySeenLinksMode.USE_BEFORE_NOW
        self.link.how_long_after_already_seen = timedelta(days=1)
        self.link.save()
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=self.link, source_id="1001", url=self.URL_A
        )
        self.settings.monitored_link = self.link
        self.settings.skip_link_capture = True
        urls: set[str] = set()
        self.parser._load_pending_links(urls)
        self.assertEqual(urls, {self.URL_A})

        # Not on a combined run: there it only applies when a seen link stops the crawl.
        self.settings.skip_link_capture = False
        urls = set()
        self.parser._load_pending_links(urls)
        self.assertEqual(urls, set())

    def test_crawl_urls_capture_only_stops_before_the_api(self):
        self.settings.capture_links_only = True
        with patch.object(self.parser, "get_galleries_from_main_page_link", return_value={self.URL_A}) as crawl_index, \
             patch.object(self.parser, "api_request_function") as api:
            self.parser.crawl_urls(["https://e-hentai.org/?f_search=test"])
        crawl_index.assert_called_once()
        api.assert_not_called()

    def test_crawl_urls_skip_capture_does_not_crawl_the_index(self):
        self.settings.skip_link_capture = True
        with patch.object(self.parser, "get_galleries_from_main_page_link") as crawl_index, \
             patch.object(self.parser, "api_request_function") as api:
            self.parser.crawl_urls(["https://e-hentai.org/?f_search=test"])
        crawl_index.assert_not_called()
        api.assert_not_called()
