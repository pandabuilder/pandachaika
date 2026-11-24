import datetime
import json
import threading
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.base.auto_wanted import ParamError, WantedParam, WantedRunStats, clean_params
from viewer.forms import AutoWantedSourceForm
from viewer.models import Attribute, AutoWantedSource, Provider, WantedGallery
from viewer.services import auto_wanted as aw
from workers.models import TaskQueue

User = get_user_model()

SCHEMA = [
    WantedParam("subpath", "Subpath", required=True),
    WantedParam("url_html_type", "Products page", kind="bool"),
    WantedParam("stop_page", "Stop page", kind="int"),
    WantedParam("flist", "Type", kind="choice", choices=(("", "Any"), ("5", "Manga"))),
    WantedParam("date", "From", kind="date"),
]


class CleanParamsTests(SimpleTestCase):
    def test_types_and_empty_values(self):
        self.assertEqual(
            clean_params(SCHEMA, {"subpath": " comics/feed/ ", "url_html_type": "True", "stop_page": "3", "flist": ""}),
            {"subpath": "comics/feed/", "url_html_type": True, "stop_page": 3},
        )
        # False booleans and empty values are left out.
        self.assertEqual(clean_params(SCHEMA, {"subpath": "x", "url_html_type": "false", "stop_page": ""}), {"subpath": "x"})
        self.assertEqual(clean_params(SCHEMA, {"subpath": "x", "flist": 5}), {"subpath": "x", "flist": "5"})

    def test_errors(self):
        with self.assertRaisesRegex(ParamError, "subpath: required"):
            clean_params(SCHEMA, {})
        with self.assertRaisesRegex(ParamError, "unknown parameter"):
            clean_params(SCHEMA, {"subpath": "x", "typo": 1})
        with self.assertRaisesRegex(ParamError, "stop_page: expected a number"):
            clean_params(SCHEMA, {"subpath": "x", "stop_page": "two"})
        with self.assertRaisesRegex(ParamError, "flist"):
            clean_params(SCHEMA, {"subpath": "x", "flist": "99"})
        with self.assertRaisesRegex(ParamError, "date"):
            clean_params(SCHEMA, {"subpath": "x", "date": "2026-13-01"})
        with self.assertRaisesRegex(ParamError, "object"):
            clean_params(SCHEMA, ["subpath"])

    def test_real_provider_schemas_are_registered(self):
        providers = aw.wanted_providers()
        for provider in ("wanimagazine", "fakku", "doujinshi_mirror"):
            self.assertIn(provider, providers)
            self.assertTrue(aw.params_schema(provider))
        self.assertEqual(
            aw.clean_source_params("wanimagazine", {"subpath": "comics/feed/"}), {"subpath": "comics/feed/"}
        )
        with self.assertRaisesRegex(ParamError, "no wanted generator"):
            aw.clean_source_params("panda", {})
        # The doujinshi.org API is gone: mugimugi has no generator anymore.
        self.assertNotIn("mugimugi", providers)

    def test_doujinshi_mirror_query(self):
        from core.providers.doujinshi_mirror.wanted import build_query

        params = aw.clean_source_params(
            "doujinshi_mirror", {"type": "7", "circle": "A|B", "adult": "0", "stop_page": "3", "sort": ""}
        )
        self.assertEqual(params, {"type": "7", "circle": "A|B", "adult": "0", "stop_page": 3})
        query = build_query(params)
        self.assertEqual(query["type"], "7")
        self.assertEqual(query["circle"], "A|B")
        self.assertEqual(query["sort"], "id")
        self.assertEqual(query["order"], "desc")
        self.assertNotIn("stop_page", query)

    def test_mugimugi_params_from_legacy(self):
        from core.providers.doujinshi_mirror.legacy import params_from_legacy

        params, dropped = params_from_legacy(
            {
                "flist": "7",
                "slist_C": "A|B",
                "slist_L": "Wani",
                "age": "Y",
                "bcopy": "N",
                "date": "2020-01-01",
                "order": "added",
                "flow": "DESC",
                "match": "0",
                "scen": "Y",
            }
        )
        self.assertEqual(
            params,
            {
                "type": "7",
                "circle": "A|B",
                "publisher": "Wani",
                "adult": "1",
                "copyshi": "0",
                "released_from": "2020-01-01",
                "sort": "id",
                "order": "desc",
            },
        )
        self.assertEqual(dropped, {"scen": "Y"})
        # The translated parameters are valid for the mirror.
        self.assertEqual(aw.clean_source_params("doujinshi_mirror", params), params)


class LegacyImportTests(TestCase):
    def setUp(self):
        self.wani = Provider.objects.create(slug="wanimagazine", name="wanimagazine")
        self.mugi = Provider.objects.create(slug="mugimugi", name="mugimugi")
        Provider.objects.create(slug="panda", name="panda")

    def attr(self, provider, name, value, data_type="text"):
        Attribute.objects.create(provider=provider, name=name, data_type=data_type, **{f"value_{data_type}": value})

    def test_legacy_sources(self):
        self.attr(self.wani, "wanted_params_1_subpath", "comics/feed/")
        self.attr(self.wani, "wanted_reason_1", "wani")
        self.attr(self.wani, "wanted_public_1", True, "bool")
        self.attr(self.wani, "wanted_providers_1", "panda fakku")
        self.attr(self.wani, "wanted_params_2_subpath", "comics_index/")
        self.attr(self.wani, "wanted_params_2_url_html_type", "1")
        self.attr(self.wani, "stop_page_2", 5, "int")
        self.attr(self.wani, "force_process", True, "bool")
        # Reason without parameters: no query.
        self.attr(self.wani, "wanted_reason_3", "orphan")
        self.attr(self.mugi, "wanted_params_a_flist", "7")
        # Provider state, not configuration.
        self.attr(self.mugi, "remaining_queries", 10, "int")

        with mock.patch.object(settings.CRAWLER_SETTINGS.auto_wanted, "providers", ["wanimagazine"]):
            sources = {s["name"]: s for s in aw.legacy_sources()}
        self.assertEqual(set(sources), {"wanimagazine 1", "wanimagazine 2", "mugimugi a"})
        one = sources["wanimagazine 1"]
        self.assertEqual(one["params"], {"subpath": "comics/feed/"})
        self.assertEqual(one["reason"], "wani")
        self.assertTrue(one["public"])
        self.assertTrue(one["active"])
        self.assertTrue(one["force_process"])
        self.assertFalse(one["should_search"])
        self.assertEqual(one["wanted_providers"], ["panda", "fakku"])
        self.assertEqual(sources["wanimagazine 2"]["params"], {"subpath": "comics_index/", "url_html_type": "1", "stop_page": 5})
        mugi = sources["mugimugi a"]
        self.assertFalse(mugi["active"])
        # Its queries now run on the mirror, with its parameters.
        self.assertEqual(mugi["provider"], "doujinshi_mirror")
        self.assertEqual(mugi["params"], {"type": "7"})
        # mugimugi's old defaults.
        self.assertTrue(mugi["should_search"])
        self.assertTrue(mugi["keep_searching"])
        self.assertTrue(mugi["notify_when_found"])

        self.assertEqual(aw.legacy_attributes().count(), 9)

    def test_import_legacy_view(self):
        self.attr(self.wani, "wanted_params_1_subpath", "magazine/feed/")
        self.attr(self.wani, "wanted_params_1_url_html_type", "False")
        self.attr(self.wani, "wanted_reason_1", "magazines")
        admin = User.objects.create_user(username="aw_admin", password="password", is_staff=True)
        self.client.force_login(admin)

        response = self.client.get(reverse("viewer:auto-wanted"))
        self.assertContains(response, "wanimagazine 1")
        self.assertContains(response, "Import as sources")

        response = self.client.post(reverse("viewer:auto-wanted"), {"action": "import_legacy"})
        self.assertEqual(response.status_code, 302)
        source = AutoWantedSource.objects.get(name="wanimagazine 1")
        # "False" text is false now (it used to count as true).
        self.assertEqual(source.params, {"subpath": "magazine/feed/"})
        self.assertEqual(source.reason, "magazines")

        # Imported again: left alone.
        source.reason = "edited"
        source.save()
        self.client.post(reverse("viewer:auto-wanted"), {"action": "import_legacy"})
        source.refresh_from_db()
        self.assertEqual(source.reason, "edited")

        response = self.client.get(reverse("viewer:auto-wanted"))
        self.assertContains(response, "Delete old attributes")
        self.client.post(reverse("viewer:auto-wanted"), {"action": "delete_legacy"})
        self.assertFalse(Attribute.objects.filter(name__startswith="wanted_").exists())
        self.assertTrue(AutoWantedSource.objects.filter(name="wanimagazine 1").exists())


class ImportExportViewTests(TestCase):
    def setUp(self):
        Provider.objects.create(slug="panda", name="panda")
        self.admin = User.objects.create_user(username="aw_staff", password="password", is_staff=True)
        self.client.force_login(self.admin)

    def test_staff_only(self):
        self.client.logout()
        user = User.objects.create_user(username="aw_user", password="password")
        self.client.force_login(user)
        response = self.client.get(reverse("viewer:auto-wanted"))
        self.assertEqual(response.status_code, 302)

    def test_round_trip(self):
        payload = {
            "version": 1,
            "sources": [
                {
                    "name": "wani comics",
                    "provider": "wanimagazine",
                    "params": {"subpath": "comics/feed/"},
                    "reason": "wanimagazine",
                    "should_search": True,
                    "wanted_providers": ["panda"],
                    "frequency": "06:00:00",
                },
                {"name": "bad", "provider": "wanimagazine", "params": {"nope": 1}},
                {"name": "no provider", "provider": "panda", "params": {}},
            ],
        }
        upload = SimpleUploadedFile("aw.json", json.dumps(payload).encode(), content_type="application/json")
        response = self.client.post(reverse("viewer:auto-wanted"), {"action": "import", "file": upload}, follow=True)
        self.assertContains(response, "1 created, 0 updated")
        self.assertContains(response, "Not imported: bad")
        self.assertContains(response, "Not imported: no provider")
        source = AutoWantedSource.objects.get(name="wani comics")
        self.assertTrue(source.active)
        self.assertTrue(source.should_search)
        self.assertEqual(source.wanted_providers, "panda")
        self.assertEqual(source.frequency, datetime.timedelta(hours=6))
        self.assertEqual(source.user, self.admin)

        # Update by name; fields left out keep their value.
        self.client.post(
            reverse("viewer:auto-wanted"),
            {"action": "import", "data": json.dumps([{"name": "wani comics", "active": False}])},
        )
        source.refresh_from_db()
        self.assertFalse(source.active)
        self.assertTrue(source.should_search)
        self.assertEqual(source.params, {"subpath": "comics/feed/"})

        response = self.client.get(reverse("viewer:auto-wanted") + "?export=1")
        exported = json.loads(response.content)
        self.assertEqual(exported["sources"][0]["name"], "wani comics")
        self.assertEqual(exported["sources"][0]["wanted_providers"], ["panda"])
        self.assertEqual(exported["sources"][0]["params"], {"subpath": "comics/feed/"})
        self.assertEqual(exported["sources"][0]["frequency"], "06:00:00")

        AutoWantedSource.objects.all().delete()
        self.client.post(reverse("viewer:auto-wanted"), {"action": "import", "data": json.dumps(exported)})
        again = AutoWantedSource.objects.get(name="wani comics")
        self.assertEqual(aw.source_to_dict(again), exported["sources"][0])

        data = self.client.get(reverse("viewer:auto-wanted") + "?format=json").json()
        self.assertEqual(data["sources"][0]["name"], "wani comics")

    def test_invalid_json(self):
        response = self.client.post(reverse("viewer:auto-wanted"), {"action": "import", "data": "{nope"}, follow=True)
        self.assertContains(response, "Could not read the sources to import")


class FormViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="aw_form", password="password", is_staff=True)
        self.client.force_login(self.admin)

    def post_data(self, **extra):
        data = {
            "name": "wani magazines",
            "provider": "wanimagazine",
            "active": "on",
            "reason": "magazine",
            AutoWantedSourceForm.param_field_name("wanimagazine", "subpath"): "magazine/feed/",
            AutoWantedSourceForm.param_field_name("wanimagazine", "stop_page"): "",
            # Another provider's fields are ignored.
            AutoWantedSourceForm.param_field_name("fakku", "subpath"): "books",
        }
        data.update(extra)
        return data

    def test_create_edit_run_delete(self):
        response = self.client.get(reverse("viewer:auto-wanted-create"))
        self.assertContains(response, 'data-provider="wanimagazine"')
        self.assertContains(response, 'data-provider="doujinshi_mirror"')

        response = self.client.post(reverse("viewer:auto-wanted-create"), self.post_data())
        source = AutoWantedSource.objects.get(name="wani magazines")
        self.assertRedirects(response, reverse("viewer:auto-wanted-edit", args=(source.pk,)))
        self.assertEqual(source.params, {"subpath": "magazine/feed/"})
        self.assertEqual(source.reason, "magazine")

        response = self.client.get(reverse("viewer:auto-wanted-edit", args=(source.pk,)))
        self.assertContains(response, 'value="magazine/feed/"')
        self.assertContains(response, "&quot;subpath&quot;: &quot;magazine/feed/&quot;")

        url_html = AutoWantedSourceForm.param_field_name("wanimagazine", "url_html_type")
        stop_page = AutoWantedSourceForm.param_field_name("wanimagazine", "stop_page")
        self.client.post(
            reverse("viewer:auto-wanted-edit", args=(source.pk,)),
            self.post_data(**{url_html: "on", stop_page: "4"}, then="run"),
        )
        source.refresh_from_db()
        self.assertEqual(source.params, {"subpath": "magazine/feed/", "url_html_type": True, "stop_page": 4})
        task = TaskQueue.objects.get(task_type="auto_wanted_worker")
        self.assertEqual(task.args, [source.pk])

        response = self.client.get(reverse("viewer:auto-wanted-create") + f"?copy={source.pk}")
        self.assertContains(response, "wani magazines (copy)")
        self.assertContains(response, 'value="4"')

        self.client.post(reverse("viewer:auto-wanted-edit", args=(source.pk,)), {"action": "delete"})
        self.assertFalse(AutoWantedSource.objects.exists())

    def test_validation(self):
        subpath = AutoWantedSourceForm.param_field_name("wanimagazine", "subpath")
        response = self.client.post(reverse("viewer:auto-wanted-create"), self.post_data(**{subpath: ""}))
        self.assertContains(response, "subpath: required")
        response = self.client.post(reverse("viewer:auto-wanted-create"), self.post_data(wanted_providers="nosuch"))
        self.assertContains(response, "Unknown provider(s): nosuch")
        response = self.client.post(reverse("viewer:auto-wanted-create"), self.post_data(frequency="00:00:00"))
        self.assertContains(response, "Must be greater than zero.")
        response = self.client.post(reverse("viewer:auto-wanted-create"), self.post_data(frequency="1 00:00:00"))
        self.assertEqual(AutoWantedSource.objects.get().frequency, datetime.timedelta(days=1))
        AutoWantedSource.objects.all().delete()
        self.assertFalse(AutoWantedSource.objects.exists())

    def test_list_actions(self):
        one = AutoWantedSource.objects.create(name="one", provider="wanimagazine", params={"subpath": "a/"})
        two = AutoWantedSource.objects.create(name="two", provider="wanimagazine", params={"subpath": "b/"}, active=False)
        self.client.post(reverse("viewer:auto-wanted"), {"action": "deactivate", "source": [one.pk]})
        one.refresh_from_db()
        self.assertFalse(one.active)
        self.client.post(reverse("viewer:auto-wanted"), {"action": "activate", "source": [one.pk, two.pk]})
        self.assertEqual(AutoWantedSource.objects.filter(active=True).count(), 2)
        self.client.post(reverse("viewer:auto-wanted"), {"action": "run_active"})
        task = TaskQueue.objects.get(task_type="auto_wanted_worker")
        self.assertEqual(sorted(task.args), sorted([one.pk, two.pk]))
        response = self.client.get(reverse("viewer:auto-wanted"))
        self.assertContains(response, "one")
        self.assertContains(response, "never")


class ScheduleTests(TestCase):
    DEFAULT = datetime.timedelta(hours=24)

    def source(self, name, ran_hours_ago=None, frequency=None, **kwargs):
        last_run_at = timezone.now() - datetime.timedelta(hours=ran_hours_ago) if ran_hours_ago is not None else None
        return AutoWantedSource.objects.create(
            name=name,
            provider=kwargs.pop("provider", "wanimagazine"),
            params={"subpath": "x/"},
            last_run_at=last_run_at,
            frequency=frequency,
            **kwargs,
        )

    def test_due_and_next(self):
        never = self.source("never")
        hourly = self.source("hourly", ran_hours_ago=2, frequency=datetime.timedelta(hours=1))
        daily = self.source("daily", ran_hours_ago=2)
        self.assertTrue(aw.is_due(never, self.DEFAULT))
        self.assertTrue(aw.is_due(hourly, self.DEFAULT))
        self.assertFalse(aw.is_due(daily, self.DEFAULT))
        self.assertEqual(daily.next_run_at(self.DEFAULT), daily.last_run_at + self.DEFAULT)
        self.assertEqual(aw.seconds_until_next_due(self.DEFAULT), 0)

        never.delete()
        hourly.delete()
        self.assertAlmostEqual(aw.seconds_until_next_due(self.DEFAULT), 22 * 3600, delta=60)
        daily.active = False
        daily.save()
        self.assertIsNone(aw.seconds_until_next_due(self.DEFAULT))

    def run_threads(self, **kwargs):
        ran = []
        with mock.patch.object(aw, "run_source", side_effect=lambda s, source: ran.append(source.name)):
            threads = aw.run_active_sources_in_threads(settings.CRAWLER_SETTINGS, default=self.DEFAULT, **kwargs)
            for thread in threads:
                thread.join(5)
        return threads, ran

    def test_scheduler_runs_due_sources_per_provider(self):
        self.source("a")
        self.source("b", ran_hours_ago=7, frequency=datetime.timedelta(hours=6))
        self.source("not due", ran_hours_ago=1)
        self.source("off", provider="fakku", active=False)
        threads, ran = self.run_threads()
        self.assertEqual(len(threads), 1)
        self.assertEqual(ran, ["a", "b"])

        # A forced run runs every active source.
        threads, ran = self.run_threads(only_due=False)
        self.assertEqual(ran, ["a", "b", "not due"])

    def test_provider_still_running_is_skipped(self):
        self.source("slow")
        started = threading.Event()
        release = threading.Event()

        def slow_run(s, source):
            started.set()
            release.wait(5)

        with mock.patch.object(aw, "run_source", side_effect=slow_run):
            first = aw.run_active_sources_in_threads(settings.CRAWLER_SETTINGS, default=self.DEFAULT)
            self.assertTrue(started.wait(5))
            self.assertEqual(aw.running_providers(), {"wanimagazine"})
            second = aw.run_active_sources_in_threads(settings.CRAWLER_SETTINGS, default=self.DEFAULT)
            self.assertEqual(second, [])
            release.set()
            first[0].join(5)
        self.assertEqual(aw.running_providers(), set())

    def test_scheduler_wait(self):
        from core.workers.auto_wanted import TimedAutoWanted

        scheduler = TimedAutoWanted(settings.CRAWLER_SETTINGS, timer=24)
        self.assertEqual(scheduler.seconds_until_due(), TimedAutoWanted.CHECK_INTERVAL)  # no sources
        self.source("due")
        self.assertEqual(scheduler.seconds_until_due(), TimedAutoWanted.MIN_WAIT)
        scheduler.force_run_once = True
        self.assertEqual(scheduler.wait_until_next_run(), 0)
        self.assertTrue(scheduler._forced_cycle)


class RunSourceTests(TestCase):
    def test_run_records_stats(self):
        source = AutoWantedSource.objects.create(name="s", provider="wanimagazine", params={"subpath": "comics/feed/"})

        def generator(settings, src, stats):
            self.assertEqual(src.params, {"subpath": "comics/feed/"})
            stats.links += 2
            stats.created += 1
            stats.error("one link failed")

        context = settings.CRAWLER_SETTINGS.provider_context
        with mock.patch.object(context, "get_wanted_generator", return_value=generator):
            result = aw.run_sources(settings.CRAWLER_SETTINGS, [source])
        source.refresh_from_db()
        self.assertEqual(source.last_run_status, AutoWantedSource.RunStatus.ERRORS)
        self.assertEqual((source.last_run_links, source.last_run_created), (2, 1))
        self.assertEqual(source.last_run_message, "one link failed")
        self.assertEqual(result["created"], 1)

    def test_failures(self):
        bad_params = AutoWantedSource.objects.create(name="bad", provider="wanimagazine", params={})
        aw.run_source(settings.CRAWLER_SETTINGS, bad_params)
        bad_params.refresh_from_db()
        self.assertEqual(bad_params.last_run_status, AutoWantedSource.RunStatus.FAILED)
        self.assertIn("subpath: required", bad_params.last_run_message)

        unknown = AutoWantedSource.objects.create(name="unknown", provider="nosuch", params={})
        aw.run_source(settings.CRAWLER_SETTINGS, unknown)
        unknown.refresh_from_db()
        self.assertIn("no wanted generator", unknown.last_run_message)

        crashing = AutoWantedSource.objects.create(name="crash", provider="wanimagazine", params={"subpath": "x/"})
        context = settings.CRAWLER_SETTINGS.provider_context
        with mock.patch.object(context, "get_wanted_generator", return_value=mock.Mock(side_effect=RuntimeError("boom"))):
            aw.run_source(settings.CRAWLER_SETTINGS, crashing)
        crashing.refresh_from_db()
        self.assertEqual(crashing.last_run_status, AutoWantedSource.RunStatus.FAILED)
        self.assertIn("RuntimeError: boom", crashing.last_run_message)


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>wani</title>
<item><title>Some Comic</title><link>https://www.wanimagazine.com/comics/abc123/</link>
<pubDate>Mon, 16 Nov 2026 10:00:00 +0900</pubDate><description>desc</description></item>
</channel></rss>"""

PRODUCT = """<html><body>
<div class="wmp-author"><a class="wmp-author-link">Artist One</a></div>
</body></html>"""


class WanimagazineGeneratorTests(TestCase):
    def test_feed_creates_wanted_gallery_with_source_values(self):
        from core.providers.wanimagazine import wanted

        Provider.objects.create(slug="panda", name="panda")
        source = AutoWantedSource.objects.create(
            name="wani comics",
            provider="wanimagazine",
            params={"subpath": "comics/feed/"},
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            wanted_providers="panda",
            unwanted_title="[Chinese]",
        )

        def fake_request(provider_name, wait_timer):
            def request(url, request_dict, post=False):
                response = mock.Mock()
                response.text = FEED if url.endswith("comics/feed/") else PRODUCT
                return response

            return request

        stats = WantedRunStats()
        with mock.patch.object(wanted, "provider_web_request", side_effect=fake_request):
            wanted.wanted_generator(settings.CRAWLER_SETTINGS, source, stats)

        self.assertEqual((stats.links, stats.created, stats.mentions), (1, 1, 1), stats.errors)
        wanted_gallery = WantedGallery.objects.get(title="Some Comic")
        self.assertEqual(wanted_gallery.reason, "wanimagazine")
        self.assertTrue(wanted_gallery.should_search)
        self.assertTrue(wanted_gallery.keep_searching)
        self.assertEqual(wanted_gallery.unwanted_title, "[Chinese]")
        self.assertEqual(wanted_gallery.auto_wanted_source, source)
        self.assertEqual(list(wanted_gallery.wanted_providers.values_list("slug", flat=True)), ["panda"])
        self.assertEqual(list(wanted_gallery.artists.values_list("name", flat=True)), ["Artist One"])

        # Second run: the link was processed, nothing new.
        stats = WantedRunStats()
        with mock.patch.object(wanted, "provider_web_request", side_effect=fake_request):
            wanted.wanted_generator(settings.CRAWLER_SETTINGS, source, stats)
        self.assertEqual((stats.links, stats.created), (0, 0))

        # Forced: processed again, only a mention (already there, so not a new one).
        source.force_process = True
        stats = WantedRunStats()
        with mock.patch.object(wanted, "provider_web_request", side_effect=fake_request):
            wanted.wanted_generator(settings.CRAWLER_SETTINGS, source, stats)
        self.assertEqual((stats.links, stats.created, stats.mentions), (1, 0, 0))
        self.assertEqual(WantedGallery.objects.count(), 1)
