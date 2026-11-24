# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import MagicMock, patch

import django.utils.timezone as django_tz
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse

from core.base.setup import Settings
from core.providers.chaika.parsers import Parser as ChaikaParser
from core.providers.panda import constants as panda_constants
from core.providers.panda.parsers import Parser
from viewer.models import MonitoredLink, MonitoredLinkRun, ProcessedLinks, Provider
from viewer.services import monitored_link_stats
from viewer.services.retention import get_entity_definitions
from viewer.templatetags.viewer_extras import as_percent, short_duration

User = get_user_model()

INDEX_URL = "https://e-hentai.org/?f_search=test"


def gallery_url(gid: int) -> str:
    return f"https://e-hentai.org/g/{gid}/abcdef{gid}/"


def search_page(*gids: int) -> MagicMock:
    response = MagicMock()
    response.text = "".join(f'<a href="{gallery_url(gid)}">g</a>' for gid in gids)
    return response


def no_hits_page() -> MagicMock:
    response = MagicMock()
    response.text = "No hits found"
    return response


def make_link(**kwargs) -> MonitoredLink:
    defaults = {
        "name": "Index",
        "url": INDEX_URL,
        "frequency": timedelta(minutes=10),
        "capture_frequency": timedelta(hours=1),
        "separate_frequencies": True,
        "stop_on_seen_link": MonitoredLink.StopOnSeenChoices.OFF,
    }
    defaults.update(kwargs)
    return MonitoredLink.objects.create(**defaults)


def make_run(link: MonitoredLink, **kwargs) -> MonitoredLinkRun:
    defaults = {"kind": MonitoredLinkRun.Kind.CAPTURE, "finished": django_tz.now()}
    defaults.update(kwargs)
    return MonitoredLinkRun.objects.create(monitored_link=link, **defaults)


class PandaRunRecordingTests(TestCase):

    def setUp(self):
        # It would close the test transaction's connection.
        close_patch = patch("core.base.parsers.close_old_connections")
        close_patch.start()
        self.addCleanup(close_patch.stop)
        self.provider = Provider.objects.create(name="Panda", slug="panda")
        self.link = make_link()
        self.crawler_settings = Settings(load_from_disk=True)
        self.crawler_settings.monitored_link = self.link
        self.parser = Parser(self.crawler_settings)
        self.parser.own_settings.monitored_link = None
        self.parser.own_settings.stop_page_number = None

    def seen(self, *gids: int) -> None:
        for gid in gids:
            ProcessedLinks.objects.create(
                provider=self.provider, monitored_link=self.link, source_id=str(gid), url=gallery_url(gid)
            )

    def capture(self, *pages: MagicMock) -> MonitoredLinkRun:
        self.crawler_settings.capture_links_only = True
        with patch.object(self.parser, "web_request", side_effect=list(pages)):
            self.parser.crawl_urls_caller([self.link.url])
        return MonitoredLinkRun.objects.get(monitored_link=self.link)

    def test_capture_run_counts_new_and_seen_links(self):
        self.seen(1001)
        self.parser.own_settings.stop_page_number = 0
        run = self.capture(search_page(1003, 1002, 1001))
        self.assertEqual(run.kind, MonitoredLinkRun.Kind.CAPTURE)
        self.assertEqual(run.provider, self.provider)
        self.assertEqual(run.pages_crawled, 1)
        self.assertEqual((run.links_found, run.links_new, run.links_known), (3, 2, 1))
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.STOP_PAGE)
        self.assertIsNotNone(run.finished)
        self.assertEqual(run.error, "")
        self.assertFalse(run.possible_gap)

    def test_stop_on_seen_ends_the_run_at_the_first_seen_link(self):
        self.link.stop_on_seen_link = MonitoredLink.StopOnSeenChoices.ON
        self.link.save()
        self.seen(1001)
        run = self.capture(search_page(1003, 1002, 1001, 1000))
        self.assertEqual((run.links_found, run.links_new, run.links_known), (3, 2, 1))
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.SEEN_LINK)
        self.assertTrue(run.reached_end)

    def test_only_new_links_up_to_the_stop_page_is_a_possible_gap(self):
        self.seen(1001)
        self.parser.own_settings.stop_page_number = 1
        run = self.capture(search_page(1005, 1004), search_page(1003, 1002))
        self.assertEqual(run.pages_crawled, 2)
        self.assertEqual((run.links_new, run.links_known), (4, 0))
        self.assertTrue(run.possible_gap)

    def test_reaching_the_end_of_results_is_not_a_gap(self):
        run = self.capture(search_page(1002, 1001), no_hits_page())
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.END_OF_RESULTS)
        self.assertEqual(run.links_new, 2)
        self.assertFalse(run.possible_gap)

    def test_no_response(self):
        run = self.capture(None)
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.NO_RESPONSE)
        self.assertEqual(run.pages_crawled, 0)
        self.assertFalse(run.possible_gap)

    def test_chained_tasks_are_not_runs(self):
        # Tasks queued from a run (e.g. newer versions of a gallery) carry the monitored link in their settings.
        with patch.object(self.parser, "crawl_urls"):
            self.parser.crawl_urls_caller([gallery_url(1001)])
        self.assertFalse(MonitoredLinkRun.objects.exists())

    def test_on_demand_crawls_are_not_runs(self):
        self.crawler_settings.monitored_link = None
        with patch.object(self.parser, "crawl_urls"):
            self.parser.crawl_urls_caller([self.link.url])
        self.assertFalse(MonitoredLinkRun.objects.exists())

    def test_failed_run_keeps_the_error(self):
        with patch.object(self.parser, "crawl_urls", side_effect=RuntimeError("boom")):
            self.parser.crawl_urls_caller([self.link.url])
        run = MonitoredLinkRun.objects.get()
        self.assertEqual(run.kind, MonitoredLinkRun.Kind.FULL)
        self.assertIn("RuntimeError: boom", run.error)
        self.assertIsNotNone(run.finished)

    def test_process_run_counts_the_pending_links(self):
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=self.link, source_id="1001", url=gallery_url(1001),
            status=ProcessedLinks.Status.PENDING,
        )
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=self.link, source_id="1002", url=gallery_url(1002),
            status=ProcessedLinks.Status.PENDING,
        )
        self.crawler_settings.skip_link_capture = True
        self.parser.downloaders = [(MagicMock(type="archive"), 1)]
        # The API doesn't answer: both links are evaluated with errors and left for the next run.
        with patch.object(self.parser, "api_request_function", return_value=None), \
             patch.object(self.parser, "pass_gallery_data_to_downloaders"):
            self.parser.crawl_urls_caller([self.link.url])
        run = MonitoredLinkRun.objects.get()
        self.assertEqual(run.kind, MonitoredLinkRun.Kind.PROCESS)
        self.assertEqual(run.pending_loaded, 2)
        self.assertEqual(run.links_to_process, 2)
        self.assertEqual(run.links_evaluated, 2)
        self.assertEqual(run.api_errors, 2)
        self.assertEqual((run.pending_processed, run.pending_retried, run.pending_failed), (0, 2, 0))
        self.assertEqual(run.pages_crawled, 0)

    def test_finishing_pending_links_counts_processed_and_given_up(self):
        for gid in (1001, 1002):
            ProcessedLinks.objects.create(
                provider=self.provider, monitored_link=self.link, source_id=str(gid), url=gallery_url(gid),
                status=ProcessedLinks.Status.PENDING, process_attempts=4,
            )
        self.parser.link_run = MonitoredLinkRun(monitored_link=self.link, kind=MonitoredLinkRun.Kind.PROCESS)
        self.parser._load_pending_links(set())
        self.parser._resolved_links.add("1001")
        self.parser._finish_pending_links()
        run = self.parser.link_run
        self.assertEqual((run.pending_processed, run.pending_retried, run.pending_failed), (1, 0, 1))

    def test_summary_hooks_add_up(self):
        self.parser.link_run = MonitoredLinkRun(monitored_link=self.link, kind=MonitoredLinkRun.Kind.FULL)
        self.parser.log_database_check_summary(total=10, existing=4, discarded=1)
        self.parser.log_metadata_evaluation_summary(
            total_evaluated=6, accepted=3, matched_wanted=2, banned=1, errors=1, wanted_filters=True
        )
        run = self.parser.link_run
        self.assertEqual(
            (run.links_in_database, run.links_evaluated, run.links_accepted, run.links_banned, run.api_errors),
            (4, 6, 3, 1, 1),
        )


class PandaFeedRunRecordingTests(TestCase):
    """The feed links are seen links too, also when capturing and processing in a single run."""

    def setUp(self):
        close_patch = patch("core.base.parsers.close_old_connections")
        close_patch.start()
        self.addCleanup(close_patch.stop)
        self.provider = Provider.objects.create(name="Panda", slug="panda")
        self.link = make_link(url=panda_constants.rss_url, separate_frequencies=False)
        self.crawler_settings = Settings(load_from_disk=True)
        self.crawler_settings.monitored_link = self.link
        self.parser = Parser(self.crawler_settings)
        self.parser.own_settings.monitored_link = None
        self.parser.downloaders = [(MagicMock(type="archive"), 1)]

    def run_feed(self, *gids: int) -> MonitoredLinkRun:
        with patch.object(self.parser, "crawl_feed", return_value=[gallery_url(gid) for gid in gids]), \
             patch.object(self.parser, "api_request_function", return_value=None), \
             patch.object(self.parser, "pass_gallery_data_to_downloaders"):
            self.parser.crawl_urls_caller([self.link.url])
        return MonitoredLinkRun.objects.get(monitored_link=self.link)

    def test_combined_run_records_the_feed_links(self):
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=self.link, source_id="1001", url=gallery_url(1001)
        )
        run = self.run_feed(1003, 1002, 1001)
        self.assertEqual(run.kind, MonitoredLinkRun.Kind.FULL)
        self.assertEqual(run.pages_crawled, 1)
        self.assertEqual((run.links_found, run.links_new, run.links_known), (3, 2, 1))
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.FEED)
        self.assertEqual(run.links_to_process, 3)
        rows = ProcessedLinks.objects.filter(monitored_link=self.link)
        self.assertEqual(set(rows.values_list("source_id", flat=True)), {"1001", "1002", "1003"})
        self.assertFalse(rows.exclude(status=ProcessedLinks.Status.PROCESSED).exists())

    def test_combined_run_stops_at_the_first_seen_feed_link(self):
        self.link.stop_on_seen_link = MonitoredLink.StopOnSeenChoices.ON
        self.link.save()
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=self.link, source_id="1001", url=gallery_url(1001)
        )
        run = self.run_feed(1003, 1002, 1001, 1000)
        self.assertEqual((run.links_found, run.links_new, run.links_known), (3, 2, 1))
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.SEEN_LINK)
        self.assertEqual(run.links_to_process, 2)


ES_URL = "https://panda.chaika.moe/meta/es-gallery-json/?q=test"


def es_hit(gid: int) -> dict:
    return {
        "provider": "panda",
        "source_url": gallery_url(gid),
        "posted_date": "2026-11-01T00:00:00+0000",
        "title": f"Gallery {gid}",
        "tags": [{"full": "language:english"}],
    }


def es_page(*gids: int, num_pages: int = 1) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"hits": [es_hit(gid) for gid in gids], "paginator": {"num_pages": num_pages}}
    return response


class ChaikaRunRecordingTests(TestCase):
    """Chaika keeps its own seen links for the galleries of the real providers behind it."""

    def setUp(self):
        close_patch = patch("core.base.parsers.close_old_connections")
        close_patch.start()
        self.addCleanup(close_patch.stop)
        self.provider = Provider.objects.create(name="Chaika", slug="chaika")
        self.link = make_link(url=ES_URL, separate_frequencies=False)
        self.crawler_settings = Settings(load_from_disk=True)
        self.crawler_settings.monitored_link = self.link
        self.parser = ChaikaParser(self.crawler_settings)
        self.parser.own_settings.monitored_link = None
        self.parser.own_settings.stop_page_number = None
        self.parser.own_settings.url = "https://panda.chaika.moe/"

    def seen(self, *gids: int, **kwargs) -> None:
        for gid in gids:
            ProcessedLinks.objects.create(
                provider=self.provider, monitored_link=self.link, source_id=f"panda:{gid}", url=gallery_url(gid),
                **kwargs,
            )

    def crawl(self, *pages: MagicMock) -> tuple[MonitoredLinkRun, MagicMock]:
        with patch.object(self.parser, "web_request_function", side_effect=list(pages)) as request:
            self.parser.crawl_urls_caller([self.link.url])
        return MonitoredLinkRun.objects.get(monitored_link=self.link), request

    def test_run_counts_new_and_seen_links(self):
        self.seen(1001)
        run, request = self.crawl(es_page(1004, 1003, num_pages=2), es_page(1002, 1001, num_pages=2))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(run.kind, MonitoredLinkRun.Kind.FULL)
        self.assertEqual(run.provider, self.provider)
        self.assertEqual(run.pages_crawled, 2)
        self.assertEqual((run.links_found, run.links_new, run.links_known), (4, 3, 1))
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.END_OF_RESULTS)
        self.assertEqual(run.links_to_process, 4)
        rows = ProcessedLinks.objects.filter(monitored_link=self.link, provider=self.provider)
        self.assertEqual(
            set(rows.values_list("source_id", "url")), {(f"panda:{gid}", gallery_url(gid)) for gid in range(1001, 1005)}
        )

    def test_stop_on_seen_ends_the_crawl(self):
        self.link.stop_on_seen_link = MonitoredLink.StopOnSeenChoices.ON
        self.link.save()
        self.seen(1001)
        run, request = self.crawl(es_page(1004, 1003, num_pages=3), es_page(1002, 1001, 1000, num_pages=3))
        self.assertEqual(request.call_count, 2)
        self.assertEqual((run.links_found, run.links_new, run.links_known), (4, 3, 1))
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.SEEN_LINK)
        self.assertEqual(run.links_to_process, 3)
        self.assertFalse(ProcessedLinks.objects.filter(source_id="panda:1000").exists())

    def test_recent_seen_links_on_the_stop_page_are_processed_again(self):
        self.link.stop_on_seen_link = MonitoredLink.StopOnSeenChoices.ON
        self.link.already_seen_links_mode = MonitoredLink.AlreadySeenLinksMode.USE_BEFORE_NOW
        self.link.how_long_after_already_seen = timedelta(days=1)
        self.link.save()
        self.seen(1001, 1000)
        with patch.object(self.parser, "web_request_function", return_value=es_page(1002, 1001, 1000)):
            galleries = self.parser.crawl_elastic_json_paginated(ES_URL)
        self.assertEqual({gallery.gid for gallery in galleries}, {"1002", "1001", "1000"})

    def test_stop_page(self):
        self.parser.own_settings.stop_page_number = 1
        run, request = self.crawl(es_page(1002, 1001, num_pages=5))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.STOP_PAGE)
        self.assertTrue(run.possible_gap)

    def test_no_response(self):
        run, _ = self.crawl(None)
        self.assertEqual(run.stop_reason, MonitoredLinkRun.StopReason.NO_RESPONSE)
        self.assertEqual(run.pages_crawled, 0)

    def test_on_demand_crawls_record_nothing(self):
        self.crawler_settings.monitored_link = None
        with patch.object(self.parser, "web_request_function", return_value=es_page(1002, 1001)):
            galleries = self.parser.crawl_elastic_json_paginated(ES_URL)
        self.assertEqual(len(galleries), 2)
        self.assertFalse(ProcessedLinks.objects.exists())


class MonitoredLinkStatsTests(TestCase):

    def setUp(self):
        self.link = make_link(stop_page=1)
        self.now = django_tz.now()

    def report(self) -> dict:
        return monitored_link_stats.monitored_link_report(self.link)

    def hint_titles(self) -> list[str]:
        return [hint.title for hint in self.report()["hints"]]

    def test_first_capture_run_is_never_a_gap(self):
        first = make_run(self.link, started=self.now - timedelta(hours=3), links_found=40, links_new=40,
                         stop_reason=MonitoredLinkRun.StopReason.STOP_PAGE)
        later = make_run(self.link, started=self.now - timedelta(hours=1), links_found=40, links_new=40,
                         stop_reason=MonitoredLinkRun.StopReason.STOP_PAGE)
        first_id = monitored_link_stats.first_capture_run_id(self.link)
        self.assertEqual(first_id, first.pk)
        self.assertFalse(monitored_link_stats.is_gap(first, first_id))
        self.assertTrue(monitored_link_stats.is_gap(later, first_id))
        report = self.report()
        self.assertEqual(report["capture"].gap_runs, 1)
        self.assertIn("Possible missed links", self.hint_titles())

    def test_capture_aggregates_and_rates(self):
        for hours, new, known in ((4, 10, 0), (3, 4, 6), (2, 2, 8), (1, 0, 10)):
            make_run(self.link, started=self.now - timedelta(hours=hours), pages_crawled=1, links_found=new + known,
                     links_new=new, links_known=known, stop_reason=MonitoredLinkRun.StopReason.STOP_PAGE)
        capture = self.report()["capture"]
        self.assertEqual(capture.runs, 4)
        self.assertEqual((capture.found, capture.new, capture.known), (40, 16, 24))
        self.assertAlmostEqual(capture.overlap_ratio, 0.6)
        self.assertEqual(capture.zero_new_runs, 1)
        # 6 new links over the 3 hours after the first run.
        self.assertAlmostEqual(capture.new_per_hour, 2.0, places=3)
        self.assertEqual(capture.links_per_page, 10)
        self.assertEqual(capture.run_capacity, 20)

    def test_mostly_empty_runs_suggest_a_longer_interval(self):
        make_run(self.link, started=self.now - timedelta(hours=10), links_found=5, links_new=5,
                 stop_reason=MonitoredLinkRun.StopReason.END_OF_RESULTS)
        for hours in range(1, 7):
            make_run(self.link, started=self.now - timedelta(hours=hours), links_found=5, links_known=5,
                     stop_reason=MonitoredLinkRun.StopReason.SEEN_LINK)
        self.assertIn("Mostly empty capture runs", self.hint_titles())

    def test_pending_links_waiting_too_long(self):
        provider = Provider.objects.create(name="Panda", slug="panda")
        row = ProcessedLinks.objects.create(
            provider=provider, monitored_link=self.link, source_id="1", url=gallery_url(1),
            status=ProcessedLinks.Status.PENDING,
        )
        ProcessedLinks.objects.filter(pk=row.pk).update(create_date=self.now - timedelta(hours=2))
        self.assertIn("Pending links are waiting", self.hint_titles())
        links = self.report()["links"]
        self.assertEqual((links.total, links.pending), (1, 1))

    def test_healthy_link(self):
        for hours in range(1, 7):
            make_run(self.link, started=self.now - timedelta(hours=hours), links_found=6, links_new=3,
                     links_known=3, stop_reason=MonitoredLinkRun.StopReason.STOP_PAGE)
        self.assertEqual(self.hint_titles(), ["Looks good"])

    def test_schedule_of_separate_frequencies(self):
        self.link.last_capture_run = self.now - timedelta(minutes=30)
        self.link.last_process_run = self.now - timedelta(minutes=5)
        schedule = monitored_link_stats.schedule_for(self.link)
        self.assertTrue(schedule.separate)
        self.assertEqual(schedule.next_capture, self.link.last_capture_run + timedelta(hours=1))
        self.assertEqual(schedule.next_process, self.link.last_process_run + timedelta(minutes=10))

    def test_effective_stop_on_seen_follows_global(self):
        self.link.stop_on_seen_link = MonitoredLink.StopOnSeenChoices.FOLLOW_GLOBAL
        settings = Settings(load_from_disk=True)
        settings.stop_on_seen_links = True
        self.assertTrue(monitored_link_stats.effective_stop_on_seen(self.link, settings))
        settings.stop_on_seen_links = False
        self.assertFalse(monitored_link_stats.effective_stop_on_seen(self.link, settings))
        self.link.stop_on_seen_link = MonitoredLink.StopOnSeenChoices.ON
        self.assertTrue(monitored_link_stats.effective_stop_on_seen(self.link, settings))

    def test_retention_prunes_old_runs(self):
        old = make_run(self.link, started=self.now - timedelta(days=400))
        make_run(self.link)
        entity = next(e for e in get_entity_definitions() if e["key"] == "monitored_link_runs")
        self.assertEqual(list(entity["build_prunable_qs"](self.now - timedelta(days=180))), [old])


class MonitoredLinkDetailViewTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="ml_viewer", password="password123")
        self.user.user_permissions.add(Permission.objects.get(codename="view_monitored_links"))
        self.client = Client()
        self.client.login(username="ml_viewer", password="password123")
        self.provider = Provider.objects.create(name="Panda", slug="panda")
        self.link = make_link(provider=self.provider, stop_page=2)
        now = django_tz.now()
        make_run(self.link, started=now - timedelta(hours=2), pages_crawled=2, links_found=50, links_new=50,
                 stop_reason=MonitoredLinkRun.StopReason.STOP_PAGE)
        make_run(self.link, started=now - timedelta(hours=1), pages_crawled=3, links_found=75, links_new=75,
                 stop_reason=MonitoredLinkRun.StopReason.STOP_PAGE)
        make_run(self.link, kind=MonitoredLinkRun.Kind.PROCESS, pending_loaded=75, links_to_process=75,
                 links_evaluated=70, wanted_matches=3, pending_processed=70, pending_retried=5)
        ProcessedLinks.objects.create(
            provider=self.provider, monitored_link=self.link, source_id="1001", url=gallery_url(1001),
            title="Some gallery", status=ProcessedLinks.Status.PENDING,
        )

    def test_detail_page(self):
        res = self.client.get(reverse("viewer:col-monitored-link", args=[self.link.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Separate frequencies")
        self.assertContains(res, "Capture every")
        self.assertContains(res, "Possible missed links")
        self.assertContains(res, "Some gallery")
        self.assertEqual(res.context["report"]["capture"].gap_runs, 1)
        self.assertEqual(len(res.context["runs"]), 3)
        # View only: no management controls.
        self.assertNotContains(res, reverse("viewer:col-edit-monitored-link", args=[self.link.pk]))

    def test_run_and_link_filters(self):
        url = reverse("viewer:col-monitored-link", args=[self.link.pk])
        res = self.client.get(url, {"run_kind": "process"})
        self.assertEqual([r.kind for r in res.context["runs"]], [MonitoredLinkRun.Kind.PROCESS])
        res = self.client.get(url, {"run_kind": "issues"})
        self.assertEqual(len(res.context["runs"]), 3)
        res = self.client.get(url, {"link_status": "failed"})
        self.assertEqual(len(res.context["links"]), 0)
        res = self.client.get(url, {"runs_page": "99", "links_page": "abc"})
        self.assertEqual(res.status_code, 200)

    def test_requires_permission(self):
        res = Client().get(reverse("viewer:col-monitored-link", args=[self.link.pk]))
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.client.get(reverse("viewer:col-monitored-link", args=[9999])).status_code, 404)

    def test_list_shows_the_last_capture(self):
        res = self.client.get(reverse("viewer:monitored-links"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, reverse("viewer:col-monitored-link", args=[self.link.pk]))
        self.assertContains(res, "+75 new")
        self.assertContains(res, "1 pending")
        self.assertTrue(res.context["last_captures"][self.link.pk].gap)


class TemplateFilterTests(SimpleTestCase):

    def test_short_duration(self):
        self.assertEqual(short_duration(timedelta(seconds=42)), "42s")
        self.assertEqual(short_duration(timedelta(minutes=4, seconds=10)), "4m 10s")
        self.assertEqual(short_duration(timedelta(hours=1)), "1h")
        self.assertEqual(short_duration(timedelta(days=2, hours=3, minutes=5)), "2d 3h")
        self.assertEqual(short_duration(None), "")

    def test_as_percent(self):
        self.assertEqual(as_percent(0.375), "38%")
        self.assertEqual(as_percent(0.375, 1), "37.5%")
        self.assertEqual(as_percent(None), "")
