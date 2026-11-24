import datetime
import json
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.workers.autoupdate import TimedAutoUpdater
from viewer.forms import AutoUpdaterScheduleForm
from viewer.models import AutoUpdaterSchedule, Gallery, GalleryChange
from viewer.services import auto_updater as au
from workers.models import TaskQueue

User = get_user_model()
H = datetime.timedelta(hours=1)
D = datetime.timedelta(days=1)


class AgeTests(SimpleTestCase):
    def test_parse_age(self):
        self.assertEqual(au.parse_age("6h"), 6 * H)
        self.assertEqual(au.parse_age("2d"), 2 * D)
        self.assertEqual(au.parse_age("1d12h"), 36 * H)
        self.assertEqual(au.parse_age("1D 12H"), 36 * H)
        self.assertEqual(au.parse_age("30m"), datetime.timedelta(minutes=30))
        self.assertEqual(au.parse_age("1w"), 7 * D)
        self.assertEqual(au.parse_age("1.5d"), 36 * H)
        self.assertEqual(au.parse_age(12), 12 * H)
        self.assertEqual(au.parse_age("12"), 12 * H)
        self.assertEqual(au.parse_age("1 00:00:00"), D)
        for bad in ("", "x", "6y", -1, "-3", True, None, [1]):
            with self.assertRaises(au.ScheduleError, msg=repr(bad)):
                au.parse_age(bad)

    def test_format_age(self):
        self.assertEqual(au.format_age(36 * H), "1d12h")
        self.assertEqual(au.format_age(6 * H), "6h")
        self.assertEqual(au.format_age(30 * D), "30d")
        self.assertEqual(au.format_age(datetime.timedelta(minutes=90)), "1h30m")
        self.assertEqual(au.format_age(datetime.timedelta(0)), "0h")


class RuleTests(SimpleTestCase):
    def test_ages(self):
        rules = au.parse_rules(
            [
                {"from": "6h", "to": "1d", "every": "6h"},
                {"from": "1d", "to": "3d", "every": "1d"},  # 1d repeats the first rule's last age
                {"from": "30d", "to": "30d"},
                {"at": "14d"},
            ]
        )
        self.assertEqual(
            [au.format_age(a) for a in au.update_ages(rules)],
            ["6h", "12h", "18h", "1d", "2d", "3d", "14d", "30d"],
        )

    def test_every_not_reaching_to(self):
        rules = au.parse_rules([{"from": "1d", "to": "2d12h", "every": "1d"}])
        self.assertEqual(au.update_ages(rules), [D, 2 * D])

    def test_clean_rules_normalizes(self):
        self.assertEqual(
            au.clean_rules('[{"from": "24h", "to": "3 00:00:00", "every": 24}, {"from": "7d", "to": "", "every": "1d"}]'),
            [{"from": "1d", "to": "3d", "every": "1d"}, {"from": "7d", "to": "7d"}],
        )
        self.assertEqual(au.clean_rules(""), [])

    def test_errors(self):
        cases = [
            ("not a list", "{}"),
            ("'to'", [{"from": "2d", "to": "1d", "every": "1h"}]),
            ("'every' is needed", [{"from": "1d", "to": "2d"}]),
            ("at least", [{"from": "1d", "to": "2d", "every": "1m"}]),
            ("unknown keys", [{"from": "1d", "typo": 1}]),
            ("missing 'from'", [{"to": "1d"}]),
            ("more than", [{"from": "0h", "to": "100d", "every": "15m"}]),
            ("ages go up to", [{"from": "4000d", "to": "4000d"}]),
            ("valid JSON", "[{"),
        ]
        for message, raw in cases:
            with self.assertRaisesRegex(au.ScheduleError, message if message != "not a list" else "must be a list"):
                au.parse_rules(raw)

    def test_posted_ranges_merge(self):
        end = timezone.now()
        start = end - H
        # 6h and 6h30m are closer than the 1h window: one range.
        ranges = au.posted_ranges([6 * H, 6 * H + datetime.timedelta(minutes=30), D], start, end)
        self.assertEqual(ranges, [(start - D, end - D), (start - 6.5 * H, end - 6 * H)])

    def test_summarize(self):
        self.assertEqual(au.summarize([]), "no updates")
        self.assertEqual(au.summarize([D]), "1 update, at 1d")
        self.assertEqual(au.summarize([D, 2 * D]), "2 updates, 1d to 2d")


def make_gallery(gid: str, posted: datetime.datetime | None, provider: str = "panda", **kwargs) -> Gallery:
    return Gallery.objects.create(gid=gid, token="abcdef0123", provider=provider, title=f"g{gid}", posted=posted, **kwargs)


class RunScheduleTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.schedule = AutoUpdaterSchedule.objects.create(
            provider="panda",
            rules=[{"from": "1d", "to": "2d", "every": "1d"}, {"from": "7d", "to": "7d"}],
            queue_every=H,
            last_run_at=self.now - H,
        )

    def queued_links(self) -> list[str]:
        links = []
        for task in TaskQueue.objects.filter(task_type="gallery_updater"):
            links.extend(task.args)
        return links

    def test_queues_galleries_reaching_an_age_in_the_window(self):
        reach_1d = make_gallery("1", self.now - D - 30 * datetime.timedelta(minutes=1))
        reach_2d = make_gallery("2", self.now - 2 * D - datetime.timedelta(minutes=1))
        reach_7d = make_gallery("3", self.now - 7 * D)  # window end is inclusive
        make_gallery("4", self.now - D - 2 * H)  # reached 1d before the window
        make_gallery("5", self.now - D + datetime.timedelta(minutes=5))  # not 1d old yet
        make_gallery("6", self.now - 3 * D)  # no update at 3d
        make_gallery("7", self.now - D - datetime.timedelta(minutes=10), provider="other")
        make_gallery("8", None)
        make_gallery("9", self.now - D - datetime.timedelta(minutes=10), status=Gallery.StatusChoices.DELETED)

        result = au.run_schedule(self.schedule, now=self.now)

        self.assertEqual(result.queued, 3)
        self.assertEqual(sorted(self.queued_links()), sorted(g.get_link() for g in (reach_1d, reach_2d, reach_7d)))
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.last_run_at, self.now)
        self.assertEqual(self.schedule.last_run_status, AutoUpdaterSchedule.RunStatus.OK)
        self.assertEqual(self.schedule.last_run_queued, 3)
        self.assertEqual(self.schedule.last_run_task_id, result.task_id)

    def test_consecutive_windows_queue_each_gallery_once_per_age(self):
        gallery = make_gallery("1", self.now - D + datetime.timedelta(minutes=20))
        for step in range(1, 4):
            au.run_schedule(self.schedule, now=self.now + step * datetime.timedelta(minutes=15))
        self.assertEqual(self.queued_links(), [gallery.get_link()])

    def test_first_run_takes_one_period_and_catch_up_is_capped(self):
        self.schedule.last_run_at = None
        self.schedule.save()
        make_gallery("1", self.now - D - 2 * H)  # outside the first run's 1h window
        self.assertEqual(au.run_schedule(self.schedule, now=self.now).queued, 0)

        self.schedule.last_run_at = self.now - 10 * D
        self.schedule.save()
        result = au.run_schedule(self.schedule, now=self.now + H)
        self.assertEqual(result.window_start, self.now + H - au.MAX_CATCH_UP)

    def test_window_claimed_once(self):
        make_gallery("1", self.now - D - datetime.timedelta(minutes=10))
        stale = AutoUpdaterSchedule.objects.get(pk=self.schedule.pk)
        self.assertEqual(au.run_schedule(self.schedule, now=self.now).queued, 1)
        result = au.run_schedule(stale, now=self.now)
        self.assertEqual(result.queued, 0)
        self.assertEqual(result.skipped, "another run took this window")

    def test_failure_gives_the_window_back(self):
        previous = self.schedule.last_run_at
        with mock.patch("workers.client.enqueue_gallery_update", side_effect=RuntimeError("boom")):
            make_gallery("1", self.now - D - datetime.timedelta(minutes=10))
            result = au.run_schedule(self.schedule, now=self.now)
        self.assertIn("boom", result.error)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.last_run_at, previous)
        self.assertEqual(self.schedule.last_run_status, AutoUpdaterSchedule.RunStatus.FAILED)
        self.assertEqual(au.run_schedule(self.schedule, now=self.now).queued, 1)

    def test_run_due(self):
        crawler_settings = settings.CRAWLER_SETTINGS
        self.schedule.last_run_at = self.now - 10 * datetime.timedelta(minutes=1)
        self.schedule.save()
        AutoUpdaterSchedule.objects.create(provider="not_a_provider", rules=[{"from": "1d", "to": "1d"}])
        AutoUpdaterSchedule.objects.create(provider="fakku", rules=[{"from": "1d", "to": "1d"}], active=False)
        self.assertEqual(au.run_due(crawler_settings, now=self.now), [])
        results = au.run_due(crawler_settings, now=self.now, force=True)
        self.assertEqual([r.provider for r in results], ["panda"])
        self.assertEqual(au.seconds_until_next_due(now=self.now), 0.0)  # not_a_provider never ran
        self.assertEqual(au.seconds_until_next_due(now=self.now, providers=crawler_settings.providers.keys()), 3600.0)

    def test_queue_whole_range(self):
        make_gallery("1", self.now - 3 * D)
        make_gallery("2", self.now - 8 * D)
        make_gallery("3", self.now - 12 * H)
        result = au.queue_whole_range(self.schedule)
        self.assertEqual(result.queued, 1)
        self.schedule.refresh_from_db()
        self.assertEqual(self.schedule.last_run_at, self.now - H)


class StatsTests(TestCase):
    def test_change_stats_by_age(self):
        now = timezone.now()
        gallery = make_gallery("1", now - 10 * D)
        GalleryChange.objects.create(gallery=gallery, changed_at=now - 10 * D + 2 * H, tags_added=["female:x"])
        GalleryChange.objects.create(gallery=gallery, changed_at=now - 10 * D + 2 * D + H, changes={"rating": [1, 2]})
        GalleryChange.objects.create(gallery=gallery, changed_at=now - 200 * D, tags_added=["old"])  # out of range
        stats = au.change_stats("panda", now=now)
        by_from = {b["from"]: b for b in stats["buckets"]}
        self.assertEqual(by_from[1]["tags"], 1)
        self.assertEqual(by_from[48]["other"], 1)
        self.assertEqual(stats["total"], 2)

    def test_galleries_per_day(self):
        now = timezone.now()
        for i in range(6):
            make_gallery(str(i), now - (2 + i) * D)
        make_gallery("x", now - 2 * H)  # the last day is skipped
        self.assertAlmostEqual(au.galleries_per_day("panda", days=30, now=now), 6 / 30)


class LegacyTests(TestCase):
    def legacy_settings(self, **overrides):
        autoupdater = SimpleNamespace(
            enable=True, providers=["panda", "missing"], cycle_timer=1.0, buffer_back=5, buffer_after=1
        )
        provider = SimpleNamespace(
            autoupdater_enable=True, autoupdater_timer=1.0, autoupdater_buffer_back=5, autoupdater_buffer_after=1
        )
        for k, v in overrides.items():
            setattr(provider, k, v)
        return SimpleNamespace(autoupdater=autoupdater, providers={"panda": provider})

    def test_legacy_rules(self):
        # Every day, galleries posted between 6 and 1 days ago: an update a day from 1d to 6d.
        self.assertEqual(
            au.legacy_rules(self.legacy_settings(), "panda"), [{"from": "1d", "to": "6d", "every": "1d"}]
        )
        self.assertEqual(
            au.legacy_rules(self.legacy_settings(autoupdater_timer=2.0, autoupdater_buffer_back=8, autoupdater_buffer_after=0), "panda"),
            [{"from": "0h", "to": "10d", "every": "2d"}],
        )

    def test_legacy_import(self):
        legacy = au.legacy_schedules(self.legacy_settings())
        self.assertEqual([s["provider"] for s in legacy], ["panda"])
        result = au.import_schedules(legacy)
        self.assertEqual(result.created, 1)
        schedule = AutoUpdaterSchedule.objects.get(provider="panda")
        self.assertTrue(schedule.active)
        self.assertEqual(schedule.queue_every, H)
        self.assertEqual(au.legacy_pending(self.legacy_settings()), [])
        self.assertEqual(au.import_schedules(legacy, skip_existing=True).skipped, ["panda"])


class ImportExportTests(TestCase):
    def test_round_trip_and_errors(self):
        result = au.import_schedules(
            au.parse_import(
                json.dumps(
                    {
                        "schedules": [
                            {"provider": "panda", "queue_every": "30m", "rules": [{"from": "6h", "to": "1d", "every": "6h"}]},
                            {"provider": "fakku", "rules": [{"from": "2d", "to": "1d", "every": "1h"}]},
                            {"provider": "x", "typo": 1},
                            {"rules": []},
                        ]
                    }
                )
            )
        )
        self.assertEqual(result.created, 1)
        self.assertEqual(len(result.errors), 3)
        schedule = AutoUpdaterSchedule.objects.get(provider="panda")
        exported = au.export_data([schedule])["schedules"][0]
        self.assertEqual(exported["queue_every"], "30m")
        self.assertEqual(exported["rules"], [{"from": "6h", "to": "1d", "every": "6h"}])
        # Fields left out keep their value.
        au.import_schedules([{"provider": "panda", "active": False}])
        schedule.refresh_from_db()
        self.assertFalse(schedule.active)
        self.assertEqual(schedule.queue_every, datetime.timedelta(minutes=30))


class FormTests(TestCase):
    def test_form(self):
        form = AutoUpdaterScheduleForm(
            {"active": "on", "queue_every": "2h", "rules": '[{"from": "1d", "to": "3d", "every": "1d"}]', "notes": ""},
            instance=AutoUpdaterSchedule(provider="panda"),
        )
        self.assertTrue(form.is_valid(), form.errors)
        schedule = form.save()
        self.assertEqual(schedule.queue_every, 2 * H)
        self.assertEqual(schedule.rules, [{"from": "1d", "to": "3d", "every": "1d"}])

        form = AutoUpdaterScheduleForm(
            {"queue_every": "1m", "rules": '[{"from": "3d", "to": "1d", "every": "1d"}]'},
            instance=AutoUpdaterSchedule(provider="panda"),
        )
        self.assertFalse(form.is_valid())
        self.assertIn("rules", form.errors)
        self.assertIn("queue_every", form.errors)

        unbound = AutoUpdaterScheduleForm(instance=schedule)
        self.assertEqual(unbound.initial["queue_every"], "2h")
        self.assertEqual(json.loads(unbound.initial["rules"]), schedule.rules)


class SchedulerTests(TestCase):
    def test_wait_is_clamped(self):
        crawler_settings = settings.CRAWLER_SETTINGS
        with mock.patch.object(crawler_settings.autoupdater, "enable", True):
            updater = TimedAutoUpdater(crawler_settings)
            self.assertEqual(updater.check_interval(), TimedAutoUpdater.CHECK_INTERVAL)
            self.assertEqual(updater.seconds_until_due(), TimedAutoUpdater.CHECK_INTERVAL)  # no schedules
            AutoUpdaterSchedule.objects.create(provider="panda", rules=[{"from": "1d", "to": "1d"}])
            self.assertEqual(updater.seconds_until_due(), TimedAutoUpdater.MIN_WAIT)  # never ran: due
            AutoUpdaterSchedule.objects.filter(provider="panda").update(last_run_at=timezone.now())
            self.assertEqual(updater.seconds_until_due(), TimedAutoUpdater.CHECK_INTERVAL)


class ViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def test_list_and_edit_pages(self):
        response = self.client.get(reverse("viewer:auto-updater"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "panda")
        response = self.client.get(reverse("viewer:auto-updater-edit", args=("panda",)))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "au-editor-data")

    def test_create_run_and_delete(self):
        url = reverse("viewer:auto-updater-edit", args=("panda",))
        response = self.client.post(
            url, {"active": "on", "queue_every": "1h", "rules": '[{"from": "1d", "to": "2d", "every": "1d"}]', "notes": ""}
        )
        self.assertEqual(response.status_code, 302)
        schedule = AutoUpdaterSchedule.objects.get(provider="panda")
        self.assertEqual(schedule.user, self.staff)
        make_gallery("1", timezone.now() - D - datetime.timedelta(minutes=10))
        self.client.post(url, {"action": "run"})
        schedule.refresh_from_db()
        self.assertEqual(schedule.last_run_queued, 1)
        self.assertEqual(self.client.get(reverse("viewer:auto-updater") + "?format=json").json()["schedules"][0]["provider"], "panda")
        preview = self.client.get(
            reverse("viewer:auto-updater-preview", args=("panda",)), {"rules": '[{"from": "1d", "to": "1d"}]'}
        ).json()
        self.assertEqual(preview["updates"], 1)
        self.assertIn("next_run", preview)
        self.assertEqual(
            self.client.get(reverse("viewer:auto-updater-preview", args=("panda",)), {"rules": "[{"}).status_code, 400
        )
        self.client.post(url, {"action": "delete"})
        self.assertFalse(AutoUpdaterSchedule.objects.exists())

    def test_invalid_rules_rerender(self):
        url = reverse("viewer:auto-updater-edit", args=("panda",))
        response = self.client.post(url, {"queue_every": "1h", "rules": '[{"from": "3d", "to": "1d", "every": "1d"}]'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "is before")
        self.assertFalse(AutoUpdaterSchedule.objects.exists())

    def test_unknown_provider_without_schedule(self):
        response = self.client.get(reverse("viewer:auto-updater-edit", args=("nope",)))
        self.assertEqual(response.status_code, 302)

    def test_needs_staff(self):
        self.client.logout()
        response = self.client.get(reverse("viewer:auto-updater"))
        self.assertEqual(response.status_code, 302)
