import datetime
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from core.base.types import GalleryData
from viewer.models import ArchiveTag, ReleaseSeries, ReleaseSeriesDismissal, Tag
from viewer.services import release_series as rs
from viewer.tests.factories import ArchiveFactory, GalleryFactory

User = get_user_model()
KT = ReleaseSeries.KeyType


def patterns_for(name: str, key_type: str):
    series = ReleaseSeries(name=name, key_type=key_type, title_regex=rs.default_title_regex(name, key_type))
    return rs.compile_title_patterns(series)


def aware(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime(day.year, day.month, day.day, 12))


class TitleParsingTests(SimpleTestCase):
    def test_year_month(self):
        patterns = patterns_for("COMIC Kairakuten BEAST", KT.YEAR_MONTH)
        parse = lambda t: rs.keys_from_title(KT.YEAR_MONTH, patterns, t)
        self.assertEqual(parse("COMIC Kairakuten BEAST 2026-12 [Digital]"), [(2026, 12)])
        self.assertEqual(parse("comic kairakuten beast 2024-4"), [(2024, 4)])
        self.assertEqual(parse("[Anthology] (Some prefix) COMIC Kairakuten BEAST 2026-01・02"), [(2026, 1), (2026, 2)])
        self.assertEqual(parse("COMIC Kairakuten BEAST 2025-12-01"), [(2025, 12), (2026, 1)])
        # A chapter that only mentions the issue, another magazine, an invalid month.
        self.assertEqual(parse("[Artist] Title (COMIC Kairakuten BEAST 2024-05) [English]"), [])
        self.assertEqual(parse("COMIC Kairakuten 2024-05"), [])
        self.assertEqual(parse("COMIC Kairakuten BEAST 2024-13"), [])

    def test_year_number_and_number(self):
        weekly = patterns_for("WEEKLY Kairakuten", KT.YEAR_NUMBER)
        self.assertEqual(rs.keys_from_title(KT.YEAR_NUMBER, weekly, "WEEKLY Kairakuten 2026 No.42"), [(2026, 42)])
        self.assertEqual(rs.keys_from_title(KT.YEAR_NUMBER, weekly, "WEEKLY Kairakuten 2026 No.10-11"), [(2026, 10), (2026, 11)])
        self.assertEqual(rs.keys_from_title(KT.YEAR_NUMBER, weekly, "WEEKLY Kairakuten Vol.10"), [])
        xeros = patterns_for("COMIC X-EROS", KT.NUMBER)
        self.assertEqual(rs.keys_from_title(KT.NUMBER, xeros, "COMIC X-EROS #141 [Digital]"), [(0, 141)])
        self.assertEqual(rs.keys_from_title(KT.NUMBER, xeros, "COMIC X-EROS ＃20"), [(0, 20)])

    def test_year_number_without_year(self):
        # Before the yearly numbering: "WEEKLY Kairakuten Vol.37", read by a line without the year group.
        series = ReleaseSeries(
            name="WEEKLY Kairakuten",
            key_type=KT.YEAR_NUMBER,
            title_regex=rs.default_title_regex("WEEKLY Kairakuten", KT.YEAR_NUMBER)
            + "\n"
            + r"^WEEKLY\s+Kairakuten\s+Vol\.?\s*(?P<number>\d+)(?:\s*[-&]\s*(?P<number_end>\d+))?(?!\d)",
        )
        patterns = rs.compile_title_patterns(series)
        parse = lambda t: rs.keys_from_title(KT.YEAR_NUMBER, patterns, t)
        self.assertEqual(parse("WEEKLY Kairakuten 2021 No.01"), [(2021, 1)])
        self.assertEqual(parse("WEEKLY Kairakuten Vol.37"), [(0, 37)])
        self.assertEqual(parse("WEEKLY Kairakuten Vol.25 & 26"), [(0, 25), (0, 26)])
        # A line with a year group that didn't capture a year doesn't fall back to the numbering without a year.
        series.title_regex = r"^W (?:(?P<year>\d{4}) )?No\.(?P<number>\d+)"
        self.assertEqual(rs.keys_from_title(KT.YEAR_NUMBER, rs.compile_title_patterns(series), "W No.3"), [])

    def test_regex_needs_groups(self):
        series = ReleaseSeries(name="X", key_type=KT.YEAR_MONTH, title_regex=r"^X (?P<year>\d{4})")
        with self.assertRaisesRegex(rs.ConfigError, "month"):
            rs.compile_title_patterns(series)
        series.key_type = KT.YEAR_NUMBER
        series.title_regex = r"^X (?P<year>\d{4})"
        with self.assertRaisesRegex(rs.ConfigError, "number"):
            rs.compile_title_patterns(series)
        series.title_regex = "("
        with self.assertRaisesRegex(rs.ConfigError, "Invalid"):
            rs.compile_title_patterns(series)

    def test_keys(self):
        self.assertEqual(rs.parse_key(KT.YEAR_MONTH, "2024-05"), (2024, 5))
        self.assertEqual(rs.parse_key(KT.YEAR_MONTH, "2024"), (2024, 1))
        self.assertEqual(rs.parse_key(KT.YEAR_NUMBER, "2024 No.9"), (2024, 9))
        self.assertEqual(rs.parse_key(KT.NUMBER, "#109"), (0, 109))
        self.assertEqual(rs.key_label(KT.YEAR_NUMBER, (2024, 9)), "2024 No.9")
        # Year and number series' issues numbered without a year.
        self.assertEqual(rs.parse_key(KT.YEAR_NUMBER, "#37"), (0, 37))
        self.assertEqual(rs.parse_key(KT.YEAR_NUMBER, "Vol.37"), (0, 37))
        self.assertEqual(rs.parse_key(KT.YEAR_NUMBER, "2024"), (2024, 1))
        self.assertEqual(rs.key_label(KT.YEAR_NUMBER, (0, 37)), "#37")
        with self.assertRaises(rs.ConfigError):
            rs.parse_key(KT.YEAR_MONTH, "2024-13")


class ScheduleTests(SimpleTestCase):
    def test_parse(self):
        periods = rs.parse_schedule(KT.YEAR_MONTH, "2024-02 even\n2010-01 monthly\n2026-06 none\n2025-01 3,6,9,12")
        self.assertEqual([p.start for p in periods], [(2010, 1), (2024, 2), (2025, 1), (2026, 6)])
        self.assertEqual(periods[1].months, frozenset({2, 4, 6, 8, 10, 12}))
        self.assertEqual(periods[2].describe(KT.YEAR_MONTH), "quarterly")
        self.assertTrue(periods[3].stopped)
        weekly = rs.parse_schedule(KT.YEAR_NUMBER, "2021 48\n2025 auto")
        self.assertEqual((weekly[0].per_year, weekly[1].per_year), (48, None))
        weekly = rs.parse_schedule(KT.YEAR_NUMBER, "2021 48\n#1 all\n#80 none")
        self.assertEqual([p.start for p in weekly], [(0, 1), (0, 80), (2021, 1)])
        self.assertEqual([p.describe(KT.YEAR_NUMBER) for p in weekly], ["every number", "no issues", "48 per year"])
        with self.assertRaisesRegex(rs.ConfigError, "without a year"):
            rs.parse_schedule(KT.YEAR_NUMBER, "#1 48")
        for bad in ("2024-02", "2024-02 weekly", "2024-02 13"):
            with self.assertRaises(rs.ConfigError, msg=bad):
                rs.parse_schedule(KT.YEAR_MONTH, bad)

    def test_suggest_year_month(self):
        keys = [(2003, 7), (2003, 9)]  # a few scattered old issues
        keys += [(y, m) for y in range(2005, 2024) for m in range(1, 13)]
        keys += [(y, m) for y in (2024, 2025) for m in range(2, 13, 2)]
        self.assertEqual(rs.suggest_schedule(KT.YEAR_MONTH, keys), "2005-01 monthly\n2024-02 even")

    def test_suggest_year_number(self):
        keys = [(y, n) for y in (2021, 2022, 2023) for n in range(1, 49) if (y, n) != (2022, 30)]
        keys += [(2024, n) for n in range(1, 40)]
        self.assertEqual(rs.suggest_schedule(KT.YEAR_NUMBER, keys), "2021 48")
        absolute = [(0, n) for n in range(1, 73)]
        self.assertEqual(rs.suggest_schedule(KT.YEAR_NUMBER, absolute + keys), "#1 all\n2021 48")
        self.assertEqual(rs.suggest_schedule(KT.YEAR_NUMBER, absolute), "#1 all")
        self.assertEqual(rs.suggest_schedule(KT.NUMBER, [(0, 5), (0, 9)]), "#5 all")


class CheckSeriesTests(TestCase):
    def setUp(self):
        self.anthology = Tag.objects.create(scope="other", name="anthology")
        self.japanese = Tag.objects.create(scope="language", name="japanese")
        self.english = Tag.objects.create(scope="language", name="english")
        self.translated = Tag.objects.create(scope="language", name="translated")
        self.today = datetime.date(2026, 11, 22)

    def gallery(self, title, posted=None, tags=None):
        gallery = GalleryFactory.create(title=title, posted=aware(posted) if posted else None)
        gallery.tags.add(*(tags if tags is not None else [self.anthology]))
        return gallery

    def archive(self, title, tags=None):
        archive = ArchiveFactory.create(title=title, gallery=None)
        for tag in tags if tags is not None else [self.anthology]:
            ArchiveTag.objects.create(archive=archive, tag=tag)
        return archive

    def monthly_series(self, **kwargs):
        values = {
            "name": "COMIC Test",
            "key_type": KT.YEAR_MONTH,
            "title_contains": "COMIC Test",
            "title_regex": rs.default_title_regex("COMIC Test", KT.YEAR_MONTH),
            "tags": "other:anthology",
            "schedule": "2026-01 monthly",
        }
        values.update(kwargs)
        return ReleaseSeries.objects.create(**values)

    def test_missing_dismissed_and_filters(self):
        series = self.monthly_series()
        for month in (1, 2, 4, 6):
            self.gallery(f"COMIC Test 2026-{month:02d}")
        self.archive("COMIC Test 2026-07 [Digital]", tags=[self.anthology, self.japanese])
        # Not counted: translated, without the tag, excluded spin-off, chapter mentioning the issue.
        self.gallery("COMIC Test 2026-03", tags=[self.anthology, self.english, self.translated])
        self.gallery("COMIC Test 2026-03 [Digital]", tags=[])
        self.gallery("[Artist] Story (COMIC Test 2026-05)")
        ReleaseSeriesDismissal.objects.create(series=series, key="2026-05")

        result = rs.check_series(series, today=self.today)
        self.assertEqual(result.error, "")
        self.assertEqual([i.label for i in result.missing], ["2026-03"])
        self.assertEqual([i.label for i in result.dismissed], ["2026-05"])
        self.assertEqual([i.label for i in result.present], ["2026-01", "2026-02", "2026-04", "2026-06", "2026-07"])
        self.assertEqual(result.skipped_language, 1)
        self.assertEqual([e.title for e in result.unparsed], ["[Artist] Story (COMIC Test 2026-05)"])
        # No lead days: the schedule can't tell if 2026-08 onwards is out yet, so it ends at the last issue found.
        self.assertEqual(result.last.label, "2026-07")
        self.assertIn('title:"COMIC Test 2026-03"', result.missing[0].query)

    def test_gallery_only(self):
        series = self.monthly_series(schedule="2026-01 monthly")
        self.gallery("COMIC Test 2026-01")
        # Its archive doesn't match the series' filters, but it's still the gallery's archive.
        ArchiveFactory.create(title="renamed", gallery=self.gallery("COMIC Test 2026-02"))
        self.archive("COMIC Test 2026-03")
        self.gallery("COMIC Test 2026-04")
        self.archive("COMIC Test 2026-04")

        result = rs.check_series(series, today=self.today)
        self.assertEqual([i.label for i in result.present], ["2026-01", "2026-02", "2026-03", "2026-04"])
        self.assertEqual([i.label for i in result.gallery_only], ["2026-01"])
        self.assertEqual([i.label for i in result.archived], ["2026-02", "2026-03", "2026-04"])
        self.assertEqual(result.completeness, 1.0)
        self.assertEqual(result.archive_completeness, 0.75)
        self.assertEqual(result.gallery_only_share, 0.25)
        data = result.as_dict(include_entries=True)
        self.assertEqual(data["gallery_only"], ["2026-01"])
        self.assertEqual(data["archived"], 3)
        self.assertEqual(data["archive_completeness"], 0.75)
        self.assertEqual([i["gallery_only"] for i in data["issues"]], [True, False, False, False])

    def test_excludes_and_suggested_schedule(self):
        series = self.monthly_series(schedule="", title_contains="COMIC Test", title_excludes="BEAST", tags="")
        for month in range(1, 7):
            self.gallery(f"COMIC Test 2025-{month:02d}")
            self.gallery(f"COMIC Test BEAST 2025-{month:02d}")
        result = rs.check_series(series, today=self.today)
        self.assertTrue(result.using_suggested_schedule)
        self.assertEqual(result.suggested_schedule, "2025-01 monthly")
        self.assertEqual(result.candidates, 6)
        self.assertEqual(result.missing, [])

    def test_lead_days_due_and_upcoming(self):
        # Issues come out 15 days before their cover month.
        series = self.monthly_series(schedule="2026-01 monthly")
        for month in range(1, 11):
            self.gallery(f"COMIC Test 2026-{month:02d}", datetime.date(2026, month, 1) - datetime.timedelta(days=15))
        result = rs.check_series(series, today=self.today)
        self.assertEqual(result.measured_lead_days, 15)
        # 2026-11 came out 2026-10-17 (missing); 2026-12 on 2026-11-16, within the grace days (due); then 2027-01.
        self.assertEqual([i.label for i in result.missing], ["2026-11"])
        self.assertEqual(result.missing[0].release, datetime.date(2026, 10, 17))
        self.assertEqual([(i.label, i.status) for i in result.upcoming], [("2026-12", "due"), ("2027-01", "upcoming")])

    def test_year_number_and_gap_windows(self):
        weekly = ReleaseSeries.objects.create(
            name="WEEKLY Test",
            key_type=KT.YEAR_NUMBER,
            title_contains="WEEKLY Test",
            title_regex=rs.default_title_regex("WEEKLY Test", KT.YEAR_NUMBER),
            schedule="2025 4",
        )
        start = datetime.date(2025, 1, 1)
        for year, number in ((2025, 1), (2025, 2), (2025, 4), (2026, 1), (2026, 3)):
            self.gallery(f"WEEKLY Test {year} No.{number}", start + datetime.timedelta(days=7 * ((year - 2025) * 4 + number)))
        result = rs.check_series(weekly, today=self.today)
        # 2025 has 4 issues; 2026 is the current year, checked up to the last number found.
        self.assertEqual([i.label for i in result.missing], ["2025 No.3", "2026 No.2"])
        self.assertEqual(result.missing[0].release, start + datetime.timedelta(days=21))
        self.assertTrue(result.missing[0].estimated)

        monthly = self.monthly_series(schedule="2025-01 monthly", lead_days=0)
        self.gallery("COMIC Test 2025-01", datetime.date(2025, 1, 1))
        self.gallery("COMIC Test 2025-03", datetime.date(2025, 3, 1))
        monthly_result = rs.check_series(monthly, today=datetime.date(2025, 3, 20))
        windows = rs.gap_windows([result, monthly_result])
        # 2025 No.3 (released 2025-01-22), 2025-02 (2025-02-01) and 2026 No.2 (2025-02-12) are close together.
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].series_count, 2)
        self.assertEqual([g.issue.label for g in windows[0].gaps], ["2025 No.3", "2025-02", "2026 No.2"])
        # Grouping needs the release dates to be close.
        monthly_result.missing[0].release = datetime.date(2025, 6, 1)
        self.assertEqual([len(w.gaps) for w in rs.gap_windows([result, monthly_result])], [1, 2])

    def test_year_number_with_issues_numbered_without_a_year(self):
        weekly = ReleaseSeries.objects.create(
            name="WEEKLY Test",
            key_type=KT.YEAR_NUMBER,
            title_contains="WEEKLY Test",
            title_regex=rs.default_title_regex("WEEKLY Test", KT.YEAR_NUMBER)
            + "\n"
            + r"^WEEKLY\s+Test\s+Vol\.(?P<number>\d+)(?:\s*&\s*(?P<number_end>\d+))?(?!\d)",
            schedule="#1 all\n2021 3",
        )
        start = datetime.date(2020, 1, 5)
        for i, title in enumerate(["Vol.1", "Vol.2 & 3", "Vol.5", "2021 No.1", "2021 No.3", "2022 No.1"]):
            self.gallery(f"WEEKLY Test {title}", start + datetime.timedelta(days=7 * i))
        result = rs.check_series(weekly, today=self.today)
        self.assertEqual(result.error, "")
        self.assertEqual(
            [(i.label, i.status) for i in result.issues],
            [
                ("#1", "present"),
                ("#2", "present"),
                ("#3", "present"),
                ("#4", "missing"),
                ("#5", "present"),
                ("2021 No.1", "present"),
                ("2021 No.2", "missing"),
                ("2021 No.3", "present"),
                ("2022 No.1", "present"),
            ],
        )
        # Missing issues get a release date between their neighbors: #4 halfway between #3 (day 7) and #5 (day 14).
        self.assertEqual(result.missing[0].release, start + datetime.timedelta(days=11))
        self.assertEqual([row.label for row in result.rows()], ["#1", "2021", "2022"])
        self.assertEqual(rs.suggest_schedule(KT.YEAR_NUMBER, [i.key for i in result.present]), "#1 all\n2021 3")

        # Dismissing an issue numbered without a year.
        ReleaseSeriesDismissal.objects.create(series=weekly, key="#4")
        result = rs.check_series(weekly, today=self.today)
        self.assertEqual([i.label for i in result.dismissed], ["#4"])

        # Without a schedule line for them, they come before the start.
        weekly.schedule = "2021 3"
        weekly.save()
        result = rs.check_series(weekly, today=self.today)
        self.assertEqual(result.before_start, 4)
        self.assertEqual([i.label for i in result.missing], ["2021 No.2"])

    def test_config_error(self):
        series = self.monthly_series(title_regex="(")
        result = rs.check_series(series, today=self.today)
        self.assertIn("Invalid title regex", result.error)

    def test_backlog_search_marks_matching_and_known(self):
        series = self.monthly_series()
        known = self.gallery("COMIC Test 2026-03 (other upload)")
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "hits": [
                {"title": "COMIC Test 2026-03 [Digital]", "source_url": "https://example.com/g/1/", "provider": "fake",
                 "posted_date": "2026-01-30T00:00:00+00:00", "tags": [{"full": "other:anthology"}]},
                {"title": "[Artist] Story (COMIC Test 2026-03) [English]", "source_url": "https://example.com/g/2/",
                 "provider": "fake", "tags": [{"full": "language:english"}, {"full": "language:translated"}]},
                {"title": "COMIC Test 2026-03", "source_url": "https://example.com/g/3/", "provider": "fake", "tags": []},
            ]
        }
        sources = [{"name": "backlog", "url": "https://backlog.example/meta/es-gallery-json/?q="}]
        parser = mock.Mock()
        parser.id_from_url.side_effect = lambda url: url.rstrip("/").rsplit("/", 1)[1]
        known.provider, known.gid = "fake", "3"
        known.save()
        with mock.patch.object(rs, "backlog_sources", return_value=sources), \
                mock.patch("requests.get", return_value=response) as get, \
                mock.patch.object(rs.settings.CRAWLER_SETTINGS.provider_context, "get_parsers", return_value=[parser]):
            queries, hits, errors = rs.search_backlog(series, (2026, 3))
        self.assertEqual(errors, [])
        self.assertEqual(queries, {"backlog": 'title:"COMIC Test 2026-03" AND NOT tags.full:"language:translated"'})
        self.assertIn("COMIC+Test+2026-03", get.call_args[0][0])
        by_url = {h.url: h for h in hits}
        self.assertTrue(by_url["https://example.com/g/1/"].matches)
        self.assertFalse(by_url["https://example.com/g/1/"].known)
        self.assertFalse(by_url["https://example.com/g/2/"].matches)
        self.assertTrue(by_url["https://example.com/g/3/"].known)
        self.assertEqual(by_url["https://example.com/g/3/"].not_counted, "")
        self.assertEqual(hits[0].url, "https://example.com/g/1/")

        # Another title to search, for uploads titled in an unusual way: the hits are still read as the issue.
        with mock.patch.object(rs, "backlog_sources", return_value=sources), \
                mock.patch("requests.get", return_value=response) as get, \
                mock.patch.object(rs.settings.CRAWLER_SETTINGS.provider_context, "get_parsers", return_value=[parser]):
            queries, hits, errors = rs.search_backlog(series, (2026, 3), title="Comic Test - Vol.3")
        self.assertEqual(queries, {"backlog": 'title:"Comic Test - Vol.3" AND NOT tags.full:"language:translated"'})
        self.assertIn("Comic+Test+-+Vol.3", get.call_args[0][0])
        self.assertTrue({h.url: h for h in hits}["https://example.com/g/1/"].matches)

    def test_backlog_search_not_counted(self):
        """A hit that's here but that the series doesn't count as the issue says why, for it and its archives."""
        series = self.monthly_series(title_excludes="BEAST", allowed_languages="japanese")
        self.gallery("COMIC Test 2026-05")
        here = self.gallery("COMIC Test 2026-03", tags=[])
        here.provider, here.gid = "fake", "3"
        here.save()
        archive = ArchiveFactory.create(title="COMIC Test 2026-04 (BEAST)", gallery=here)
        ArchiveTag.objects.create(archive=archive, tag=self.anthology)
        ArchiveTag.objects.create(archive=archive, tag=self.english)
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "hits": [{"title": "COMIC Test 2026-03", "source_url": "https://example.com/g/3/", "provider": "fake"}]
        }
        sources = [{"name": "backlog", "url": "https://backlog.example/meta/es-gallery-json/?q="}]
        parser = mock.Mock()
        parser.id_from_url.side_effect = lambda url: url.rstrip("/").rsplit("/", 1)[1]

        def search():
            with mock.patch.object(rs, "backlog_sources", return_value=sources), \
                    mock.patch("requests.get", return_value=response), \
                    mock.patch.object(rs.settings.CRAWLER_SETTINGS.provider_context, "get_parsers",
                                      return_value=[parser]):
                return rs.search_backlog(series, (2026, 3))[1][0]

        hit = search()
        self.assertTrue(hit.known)
        self.assertEqual(
            hit.not_counted.split("\n"),
            [
                "Gallery here: it doesn't have any of the series' tags (other:anthology).",
                f'Archive {archive.pk}: its title contains "beast", which the series excludes; its language (english) '
                "isn't allowed; its title (COMIC Test 2026-04 (BEAST)) is read as 2026-04.",
            ],
        )
        self.assertIn("2026-03", [i.label for i in rs.check_series(series, today=self.today).missing])

        # Once the gallery has the tag, it counts: the issue is here and the hit isn't flagged.
        here.tags.add(self.anthology)
        self.assertEqual(search().not_counted, "")
        self.assertNotIn("2026-03", [i.label for i in rs.check_series(series, today=self.today).missing])

    def test_backlog_query_title(self):
        series = self.monthly_series()
        self.assertEqual(rs.search_title(series, (2026, 3)), "COMIC Test 2026-03")
        default_page_query = rs.backlog_query_for(series, (2026, 3), page=True)
        self.assertEqual(default_page_query, '"COMIC Test 2026-03" -language:translated$')
        self.assertEqual(rs.backlog_query_for(series, (2026, 3), page=True, title="X"), '"X" -language:translated$')
        # A query with {title} keeps the rest; one without it can't use another title, so the default query does.
        series.backlog_page_query = '"{title}" f_cats=1021'
        self.assertEqual(rs.backlog_query_for(series, (2026, 3), page=True, title="X"), '"X" f_cats=1021')
        series.backlog_page_query = '"{name} {key}" f_cats=1021'
        self.assertEqual(rs.backlog_query_for(series, (2026, 3), page=True), '"COMIC Test 2026-03" f_cats=1021')
        self.assertEqual(rs.backlog_query_for(series, (2026, 3), page=True, title="X"), '"X" -language:translated$')
        rs.validate_backlog_query('title:"{title}"')
        self.assertEqual(rs.clean_search_title('  COMIC "Test"\n 2026-03 '), "COMIC Test 2026-03")

    def test_backlog_search_page_source(self):
        """Sources that aren't es-gallery-json are read as a provider's search page, with the page query."""
        series = self.monthly_series()
        parser = mock.Mock()
        parser.name = "fake"
        parser.links_from_search_page.return_value = ["https://example.com/g/5/aa/", "https://example.com/g/6/bb/"]
        parser.fetch_multiple_gallery_data.return_value = [
            GalleryData("5", "fake", link="https://example.com/g/5/aa/", title="COMIC Test 2026-03 [Digital]",
                        tags=["language:japanese", "other:anthology"], filecount=300,
                        posted=datetime.datetime(2026, 1, 30, tzinfo=datetime.timezone.utc)),
            GalleryData("6", "fake", link="https://example.com/g/6/bb/", title="",
                        title_jpn="COMIC Test 2026-03", tags=["language:english", "language:translated"]),
        ]
        parser.id_from_url.side_effect = lambda url: url.split("/g/")[1].split("/")[0]
        sources = [
            {"name": "panda", "url": "https://example.com/?f_search="},
            {"name": "broken", "url": "https://backlog.example/meta/es-gallery-json/?q="},
        ]
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("Expecting value")
        with mock.patch.object(rs, "backlog_sources", return_value=sources), \
                mock.patch.object(rs, "_search_page_parser", return_value=parser), \
                mock.patch("requests.get", return_value=response), \
                mock.patch.object(rs.settings.CRAWLER_SETTINGS.provider_context, "get_parsers", return_value=[parser]):
            queries, hits, errors = rs.search_backlog(series, (2026, 3))
        self.assertEqual(queries["panda"], '"COMIC Test 2026-03" -language:translated$')
        self.assertEqual(
            parser.links_from_search_page.call_args[0][0],
            "https://example.com/?f_search=%22COMIC+Test+2026-03%22+-language%3Atranslated%24",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("isn't JSON", errors[0])
        by_url = {h.url: h for h in hits}
        first = by_url["https://example.com/g/5/aa/"]
        self.assertTrue(first.matches)
        self.assertEqual((first.posted, first.image_count, first.languages), ("2026-01-30", 300, ["japanese"]))
        # Read from the Japanese title, but translated.
        second = by_url["https://example.com/g/6/bb/"]
        self.assertEqual(second.keys, ["2026-03"])
        self.assertFalse(second.matches)

    def test_backlog_search_chains_and_suggestion(self):
        """A chain (2 replaced by 3, expunged) and a separate upload (4) found by both sources: 4 is suggested."""
        series = self.monthly_series()
        link = "https://example.com/g/{}/t{}/".format
        chain = [link(2, 2), link(3, 3)]
        tags = [{"full": "other:anthology"}, {"full": "other:already_uploaded"}]
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "hits": [
                {"gid": "3", "title": "COMIC Test 2026-03 [Digital]", "source_url": link(3, 3), "provider": "fake",
                 "image_count": "470", "expunged": True, "last_in_chain": True, "gallery_chain_urls": chain, "tags": tags},
                {"gid": "4", "title": "COMIC Test 2026-03 [Digital]", "source_url": link(4, 4), "provider": "fake",
                 "image_count": "470", "expunged": False, "last_in_chain": True, "gallery_chain_urls": [link(4, 4)]},
                {"gid": "2", "title": "COMIC Test 2026-03 [Digital]", "source_url": link(2, 2), "provider": "fake",
                 "image_count": "457", "expunged": False, "last_in_chain": False, "gallery_chain_urls": chain, "tags": tags},
            ]
        }
        page_data = GalleryData("4", "fake", link=link(4, 4), title="COMIC Test 2026-03 [Digital]", filecount=470,
                                expunged=False, tags=["language:japanese", "other:anthology"])
        parser = mock.Mock()
        parser.name = "fake"
        parser.links_from_search_page.return_value = [link(4, 4)]
        parser.fetch_multiple_gallery_data.return_value = [page_data]
        parser.id_from_url.side_effect = lambda url: url.split("/g/")[1].split("/")[0]
        sources = [
            {"name": "page", "url": "https://example.com/?f_search="},
            {"name": "json", "url": "https://backlog.example/meta/es-gallery-json/?q="},
        ]
        with mock.patch.object(rs, "backlog_sources", return_value=sources), \
                mock.patch.object(rs, "_search_page_parser", return_value=parser), \
                mock.patch("requests.get", return_value=response), \
                mock.patch.object(rs.settings.CRAWLER_SETTINGS.provider_context, "get_parsers", return_value=[parser]):
            queries, hits, errors = rs.search_backlog(series, (2026, 3))
        self.assertEqual(errors, [])
        self.assertEqual([h.gid for h in hits], ["4", "2", "3"])
        by_gid = {h.gid: h for h in hits}
        self.assertTrue(by_gid["4"].suggested)
        self.assertEqual(by_gid["4"].source, "page, json")
        self.assertEqual(by_gid["4"].languages, ["japanese"])
        self.assertEqual(by_gid["2"].replaced_by, link(3, 3))
        self.assertEqual(by_gid["2"].flags, ["already_uploaded"])
        self.assertEqual(by_gid["2"].image_count, 457)
        self.assertTrue(by_gid["3"].expunged)
        self.assertEqual(by_gid["3"].replaces, link(2, 2))
        self.assertFalse(by_gid["2"].suggested or by_gid["3"].suggested)

        # The newer version already here: nothing is suggested for the issue.
        GalleryFactory(provider="fake", gid="3", title="COMIC Test 2026-03 (newer)")
        with mock.patch.object(rs, "backlog_sources", return_value=sources[1:]), \
                mock.patch("requests.get", return_value=response), \
                mock.patch.object(rs.settings.CRAWLER_SETTINGS.provider_context, "get_parsers", return_value=[parser]):
            queries, hits, errors = rs.search_backlog(series, (2026, 3))
        by_gid = {h.gid: h for h in hits}
        self.assertTrue(by_gid["2"].replaced_by_known)
        self.assertTrue(by_gid["3"].known)
        self.assertFalse(any(h.suggested for h in hits))

    def test_panda_replacement_links(self):
        self.assertEqual(
            rs._same_site_link("https://e-hentai.org/g/2475282/9b7c7420da/", "2475313", "8cd3983d50"),
            "https://e-hentai.org/g/2475313/8cd3983d50/",
        )
        self.assertEqual(rs._same_site_link("https://e-hentai.org/g/1/a/", None, None), "")

    def test_backlog_search_unknown_page_source(self):
        series = self.monthly_series()
        with mock.patch.object(rs, "backlog_sources", return_value=[{"name": "x", "url": "https://nowhere.example/?q="}]):
            queries, hits, errors = rs.search_backlog(series, (2026, 3))
        self.assertEqual(hits, [])
        self.assertIn("not an es-gallery-json endpoint", errors[0])

    def test_search_page_parser_lookup(self):
        parser = rs._search_page_parser("https://e-hentai.org/?f_search=")
        self.assertIsNotNone(parser)
        self.assertEqual(parser.name, "panda")
        self.assertIsNone(rs._search_page_parser("https://panda.chaika.moe/meta/es-gallery-json/?q="))


class ReleaseSeriesViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="rs_admin", password="password", email="rs@example.com")
        self.client.force_login(self.admin)
        self.series = ReleaseSeries.objects.create(
            name="COMIC Test",
            key_type=KT.YEAR_MONTH,
            title_contains="COMIC Test",
            title_regex=rs.default_title_regex("COMIC Test", KT.YEAR_MONTH),
            schedule="2026-01 monthly",
        )
        for month in (1, 3):
            GalleryFactory.create(title=f"COMIC Test 2026-{month:02d}")

    def test_staff_only(self):
        self.client.logout()
        user = User.objects.create_user(username="rs_user", password="password")
        self.client.force_login(user)
        for url in (reverse("viewer:release-series"), reverse("viewer:release-series-detail", args=(self.series.pk,))):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_pages_render(self):
        response = self.client.get(reverse("viewer:release-series"))
        self.assertContains(response, "COMIC Test")
        self.assertContains(response, "2026-02")
        response = self.client.get(reverse("viewer:release-series-detail", args=(self.series.pk,)))
        self.assertContains(response, 'data-key="2026-02"')
        with mock.patch.object(rs, "backlog_sources", return_value=[{"name": "src", "url": "https://example.com/?q="}]):
            detail_with_sources = self.client.get(reverse("viewer:release-series-detail", args=(self.series.pk,)))
        self.assertContains(detail_with_sources, 'id="rs-backlog-run"')
        self.assertContains(detail_with_sources, "renderPendingSection")
        self.assertEqual(self.client.get(reverse("viewer:release-series-create")).status_code, 200)
        self.assertEqual(self.client.get(reverse("viewer:release-series-edit", args=(self.series.pk,))).status_code, 200)

    def test_json(self):
        data = self.client.get(reverse("viewer:release-series") + "?format=json").json()
        self.assertEqual(data["series"][0]["missing"], ["2026-02"])
        detail = self.client.get(reverse("viewer:release-series-detail", args=(self.series.pk,)) + "?format=json").json()
        self.assertEqual([i["status"] for i in detail["issues"]], ["present", "missing", "present"])

    def test_create_fills_defaults(self):
        response = self.client.post(
            reverse("viewer:release-series-create"),
            {"name": "COMIC New", "key_type": KT.NUMBER, "allowed_languages": "japanese", "active": "on"},
        )
        series = ReleaseSeries.objects.get(name="COMIC New")
        self.assertRedirects(response, reverse("viewer:release-series-detail", args=(series.pk,)))
        self.assertEqual(series.title_contains, "COMIC New")
        self.assertIn("(?P<number>", series.title_regex)

    def test_create_validates(self):
        response = self.client.post(
            reverse("viewer:release-series-create"),
            {"name": "Bad", "key_type": KT.YEAR_MONTH, "schedule": "2024-02 weekly", "backlog_query": "{nope}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ReleaseSeries.objects.filter(name="Bad").exists())
        self.assertContains(response, "Unknown schedule rule")
        self.assertContains(response, "Unknown placeholder")

    def test_dismiss_and_restore(self):
        url = reverse("viewer:release-series-detail", args=(self.series.pk,))
        self.client.post(url, {"action": "dismiss", "key": ["2026-02"], "reason": "combined", "comment": "with 03"})
        dismissal = self.series.dismissals.get()
        self.assertEqual((dismissal.key, dismissal.reason, dismissal.user), ("2026-02", "combined", self.admin))
        self.assertEqual(rs.check_series(self.series).missing, [])
        self.client.post(url, {"action": "undismiss", "key": "2026-02"})
        self.assertFalse(self.series.dismissals.exists())

    def test_apply_schedule_and_lead_days(self):
        url = reverse("viewer:release-series-detail", args=(self.series.pk,))
        self.client.post(url, {"action": "apply_schedule", "schedule": "2026-01 odd"})
        self.client.post(url, {"action": "apply_lead_days", "lead_days": "15"})
        self.series.refresh_from_db()
        self.assertEqual((self.series.schedule, self.series.lead_days), ("2026-01 odd", 15))
        self.client.post(url, {"action": "apply_schedule", "schedule": "nonsense"})
        self.series.refresh_from_db()
        self.assertEqual(self.series.schedule, "2026-01 odd")

    def test_export_import_roundtrip(self):
        ReleaseSeriesDismissal.objects.create(series=self.series, key="2026-02", reason="not_released")
        exported = self.client.get(reverse("viewer:release-series") + "?export=1").json()
        exported["series"][0]["schedule"] = "2026-01 even"
        exported["series"].append({"name": "COMIC Other", "key_type": "number"})
        exported["series"].append({"name": "Broken", "key_type": "year_month", "schedule": "x y z"})
        response = self.client.post(
            reverse("viewer:release-series"), {"action": "import", "data": json.dumps(exported)}, follow=True
        )
        self.assertContains(response, "1 created, 1 updated")
        self.assertContains(response, "Not imported: Broken")
        self.series.refresh_from_db()
        self.assertEqual(self.series.schedule, "2026-01 even")
        self.assertEqual(self.series.dismissals.count(), 1)
        other = ReleaseSeries.objects.get(name="COMIC Other")
        self.assertIn("(?P<number>", other.title_regex)
        # Fields left out get the defaults.
        self.assertTrue(other.active)
        self.assertEqual(other.allowed_languages, "japanese")

    def test_backlog_search_endpoint(self):
        url = reverse("viewer:release-series-backlog-search", args=(self.series.pk,))
        hit = rs.BacklogHit("src", "COMIC Test 2026-02", "https://example.com/g/1/", "fake", "", None, None, [], ["2026-02"], True, False)
        with mock.patch.object(rs, "search_backlog", return_value=({"src": "q"}, [hit], [])) as search:
            data = self.client.post(url, {"key": "2026-02", "source": ["src"]}).json()
        self.assertEqual(data["queries"], {"src": "q"})
        self.assertEqual(search.call_args[0][1], (2026, 2))
        self.assertEqual(search.call_args[0][2], ["src"])
        self.assertEqual(data["hits"][0]["url"], "https://example.com/g/1/")
        self.assertEqual(search.call_args[1]["title"], "")
        self.assertEqual((data["title"], data["default_title"]), ("COMIC Test 2026-02", "COMIC Test 2026-02"))
        # Another title to search; the default title is the same as none.
        with mock.patch.object(rs, "search_backlog", return_value=({"src": "q"}, [hit], [])) as search:
            data = self.client.post(url, {"key": "2026-02", "title": ' Comic "Test" Vol.2 '}).json()
            self.client.post(url, {"key": "2026-02", "title": "COMIC Test 2026-02"})
        self.assertEqual(search.call_args_list[0][1]["title"], "Comic Test Vol.2")
        self.assertEqual(data["title"], "Comic Test Vol.2")
        self.assertEqual(search.call_args_list[1][1]["title"], "")
        self.assertEqual(self.client.post(url, {"key": "nope"}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_backlog_add(self):
        url = reverse("viewer:release-series-backlog-add")
        task = mock.Mock(pk=7)
        with mock.patch("viewer.views.release_series.client.enqueue", return_value=task) as enqueue:
            data = self.client.post(
                url, {"url": ["https://example.com/g/1/", "https://example.com/g/1/", "javascript:x"], "reason": "r"}
            ).json()
        self.assertEqual(data, {"queued": 1, "task_id": 7})
        self.assertEqual(enqueue.call_args.kwargs["args"], ["https://example.com/g/1/"])
        self.assertEqual(enqueue.call_args.kwargs["task_type"], "web_crawler")
        self.assertEqual(self.client.post(url, {}).status_code, 400)

    def test_backlog_add_needs_permission(self):
        staff = User.objects.create_user(username="rs_staff", password="password", is_staff=True)
        self.client.force_login(staff)
        response = self.client.post(reverse("viewer:release-series-backlog-add"), {"url": "https://example.com/g/1/"})
        self.assertEqual(response.status_code, 403)

    def test_delete(self):
        self.client.post(reverse("viewer:release-series-edit", args=(self.series.pk,)), {"action": "delete"})
        self.assertFalse(ReleaseSeries.objects.exists())
