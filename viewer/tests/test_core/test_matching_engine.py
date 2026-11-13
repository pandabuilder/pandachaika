import io
import os
import shutil
import tempfile
import zipfile
from typing import Any
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import TestCase, override_settings
from PIL import Image as PILImage

from core.base.errors import ProviderBannedError
from core.base.matchers import Matcher
from core.base.types import GalleryData
from core.matching.engine import MatchingEngine, MatchingOptions
from core.matching.local import GalleryScope, LocalCrc32Matcher, LocalSizeMatcher, LocalTitleMatcher
from core.matching.types import MatchingMode, MatchTarget, SearchResult
from core.providers.chaika.matchers import ChaikaMatcher
from viewer.models import Archive, ArchiveMatches, Gallery, GalleryMatch, WantedGallery
from viewer.utils.matching import (
    create_matches_wanted_galleries_from_providers,
    create_matches_wanted_galleries_from_providers_internal,
    generate_possible_matches_for_archives,
)

crawler_settings = settings.CRAWLER_SETTINGS


def ensure_default_user() -> None:
    """Archives default to user 1."""
    from django.contrib.auth import get_user_model

    get_user_model().objects.get_or_create(pk=1, defaults={"username": "matching-test-user"})


def make_zip(path: str, images: int = 2) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(images):
            buffer = io.BytesIO()
            PILImage.new("RGB", (8, 8), (i * 40, 10, 10)).save(buffer, format="PNG")
            zf.writestr("{:03d}.png".format(i + 1), buffer.getvalue())


def fake_response(text: str = "", json_data: Any = None) -> MagicMock:
    response = MagicMock()
    response.__bool__.return_value = True
    response.text = text
    response.status_code = 200
    response.json.return_value = [] if json_data is None else json_data
    return response


class MatcherRateLimitTests(TestCase):
    """Every provider matcher must send its requests through the shared rate limiter, never directly."""

    def setUp(self) -> None:
        self.media_root = tempfile.mkdtemp()
        self.old_media_root = crawler_settings.MEDIA_ROOT
        crawler_settings.MEDIA_ROOT = self.media_root
        make_zip(os.path.join(self.media_root, "files", "[Artist] Some Long Title.zip"))

    def tearDown(self) -> None:
        crawler_settings.MEDIA_ROOT = self.old_media_root
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_all_matchers_use_provider_rate_limit_bucket(self) -> None:
        matcher_classes = crawler_settings.provider_context.matchers
        self.assertTrue(matcher_classes)
        target = MatchTarget(file_path="files/[Artist] Some Long Title.zip")
        used_buckets: dict[str, tuple[str, int, float]] = {}

        for matcher_class in matcher_classes:
            matcher = matcher_class(crawler_settings)
            calls: list[tuple[str, dict]] = []

            def requester(url: str, request_dict: dict, post: bool = False, _calls=calls) -> MagicMock:
                _calls.append((url, request_dict))
                return fake_response()

            with (
                patch("core.base.matchers.request_by_provider", return_value=requester) as by_provider,
                patch("requests.get", side_effect=AssertionError("direct request")),
                patch("requests.post", side_effect=AssertionError("direct request")),
                patch.object(matcher.own_settings, "api_key", "test-key", create=True),
            ):
                query = matcher.search_input(target)
                self.assertTrue(query, str(matcher))
                matcher.search(query)

            self.assertTrue(calls, "{} made no request".format(matcher))
            bucket, calls_count, period = by_provider.call_args.args
            self.assertEqual(bucket, "{}_web".format(matcher.provider), str(matcher))
            self.assertEqual(calls_count, 1)
            self.assertEqual(period, float(matcher.own_settings.wait_timer))
            used_buckets[str(matcher)] = by_provider.call_args.args

        self.assertIn("chaika_hash", used_buckets)
        self.assertIn("panda_title", used_buckets)

    def test_j18_does_not_modify_shared_headers(self) -> None:
        matcher = next(x for x in crawler_settings.provider_context.matchers if x.provider == "j18")(crawler_settings)
        headers_before = dict(crawler_settings.requests_headers)
        with patch(
            "core.base.matchers.request_by_provider",
            return_value=lambda *args, **kwargs: fake_response(json_data={"data": {"data": []}}),
        ):
            matcher.search("title")
        self.assertEqual(crawler_settings.requests_headers, headers_before)


PROVIDERS_DIR = os.path.join(settings.BASE_DIR, "core", "providers")


class ParserRateLimitTests(TestCase):
    """Parsers and wanted generators fetch through the provider's shared "<provider>_web" bucket."""

    def test_no_provider_module_requests_directly(self) -> None:
        offenders = []
        for root, _dirs, files in os.walk(PROVIDERS_DIR):
            for file_name in files:
                if not file_name.endswith(".py"):
                    continue
                with open(os.path.join(root, file_name)) as f:
                    if "request_with_retries" in f.read():
                        offenders.append(os.path.relpath(os.path.join(root, file_name), PROVIDERS_DIR))
        self.assertEqual(offenders, [])

    def test_parsers_use_provider_web_bucket(self) -> None:
        gallery_urls = {
            "fakku": "https://www.fakku.net/hentai/some-title-english",
            "irodori": "https://irodoricomics.com/index.php?route=product/product&product_id=1",
            "nhentai": "https://nhentai.net/g/1/",
            "j18": "https://www.j18.example/item/1",
            "nexus": "https://hentainexus.com/view/1",
            "twodmarket": "https://2d-market.com/Comic/1",
        }
        for name, url in gallery_urls.items():
            parser = crawler_settings.provider_context.get_parsers(crawler_settings, filter_name=name)[0]
            self.assertEqual(parser.name, name)
            buckets: list[str] = []

            def by_provider(bucket: str, calls: int, period: float, _buckets=buckets) -> Any:
                _buckets.append(bucket)
                return lambda *args, **kwargs: None

            with (
                patch("core.base.utilities.request_by_provider", side_effect=by_provider),
                patch("core.base.utilities.request_with_retries", side_effect=AssertionError("unlimited request")),
                patch("time.sleep", side_effect=AssertionError("parsers are paced by the rate limiter")),
            ):
                parser.fetch_multiple_gallery_data([url, url])

            self.assertEqual(buckets, ["{}_web".format(name)] * 2, name)


class FakeTitleMatcher(Matcher):
    name = "fake_title"
    provider = "chaika"
    type = "title"
    default_cutoff = 0.6

    def __init__(self, *args: Any, results: list[GalleryData] | None = None, error: Exception | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results = results or []
        self.error = error
        self.queries: list[str] = []

    def search(self, query: str) -> SearchResult:
        self.queries.append(query)
        if self.error:
            raise self.error
        return SearchResult(links=[x.link or "" for x in self.results], galleries=self.results)


class FakeExactMatcher(FakeTitleMatcher):
    name = "fake_hash"
    type = "hash"
    exact = True
    uses_file = True
    default_cutoff = 0.0


class OnlineEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        ensure_default_user()

    def gallery_data(self, gid: str, title: str, provider: str = "panda") -> GalleryData:
        return GalleryData(gid, provider, token="t" + gid, title=title, link="https://example.org/g/{}/".format(gid))

    def test_title_matcher_scores_and_auto_links_best(self) -> None:
        matcher = FakeTitleMatcher(
            crawler_settings,
            results=[
                self.gallery_data("1", "[Artist] Some Title"),
                self.gallery_data("2", "Completely different"),
            ],
        )
        engine = MatchingEngine(
            crawler_settings, MatchingOptions(mode=MatchingMode.ONLINE, auto_link=True), matchers=[matcher]
        )
        outcome = engine.process(MatchTarget(file_path="a/[Artist] Some Title.zip"))
        self.assertIsNotNone(outcome.best)
        assert outcome.best is not None
        self.assertEqual(outcome.best.gallery_data.gid, "1")  # type: ignore[union-attr]
        self.assertEqual(outcome.best.found_by, "fake_title")
        # The search query is the cleaned file name.
        self.assertEqual(matcher.queries, ["Some Title"])

    def test_title_below_auto_link_cutoff_is_not_linked(self) -> None:
        matcher = FakeTitleMatcher(crawler_settings, results=[self.gallery_data("1", "Some Other Words")])
        engine = MatchingEngine(
            crawler_settings,
            MatchingOptions(mode=MatchingMode.ONLINE, auto_link=True, cutoff=0.1),
            matchers=[matcher],
        )
        outcome = engine.process(MatchTarget(title="Some Title"))
        self.assertIsNone(outcome.best)
        self.assertTrue(outcome.candidates)

    def test_exact_matcher_picks_closest_title(self) -> None:
        matcher = FakeExactMatcher(
            crawler_settings,
            results=[self.gallery_data("1", "Unrelated"), self.gallery_data("2", "My File Name")],
        )
        engine = MatchingEngine(
            crawler_settings, MatchingOptions(mode=MatchingMode.ONLINE, auto_link=True), matchers=[matcher]
        )
        outcome = engine.process(MatchTarget(file_path="x/My File Name.zip"))
        assert outcome.best is not None
        self.assertEqual(outcome.best.accuracy, 1.0)
        self.assertEqual(outcome.best.gallery_data.gid, "2")  # type: ignore[union-attr]

    def test_banned_provider_is_skipped_for_the_rest_of_the_run(self) -> None:
        banned = FakeTitleMatcher(crawler_settings, error=ProviderBannedError("chaika_web", 3600))
        same_provider = FakeExactMatcher(crawler_settings, results=[self.gallery_data("1", "x")])
        engine = MatchingEngine(
            crawler_settings, MatchingOptions(mode=MatchingMode.ONLINE), matchers=[banned, same_provider]
        )
        engine.process(MatchTarget(file_path="a/one.zip"))
        engine.process(MatchTarget(file_path="a/two.zip"))
        self.assertEqual(len(banned.queries), 1)
        self.assertEqual(same_provider.queries, [])
        self.assertIn("chaika", engine.report.skipped_providers)
        self.assertIn("skipped providers: chaika", engine.report.summary())

    def test_matcher_errors_do_not_stop_the_run(self) -> None:
        failing = FakeTitleMatcher(crawler_settings, error=ValueError("parse error"))
        working = FakeExactMatcher(crawler_settings, results=[self.gallery_data("1", "x")])
        engine = MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.ONLINE), matchers=[failing, working])
        outcome = engine.process(MatchTarget(file_path="a/one.zip"))
        self.assertEqual(len(outcome.candidates), 1)
        self.assertEqual(engine.report.errors, 1)

    def test_online_possible_matches_create_galleries(self) -> None:
        archive = Archive.objects.create(title="[Artist] Some Title", zipped="a/[Artist] Some Title.zip", match_type="non-match")
        matcher = FakeTitleMatcher(crawler_settings, results=[self.gallery_data("77", "[Artist] Some Title")])
        engine = MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.ONLINE), matchers=[matcher])
        engine.match_archive(archive)
        gallery = Gallery.objects.get(gid="77", provider="panda")
        match = ArchiveMatches.objects.get(archive=archive, gallery=gallery)
        self.assertEqual(match.match_type, "title")
        # The cleaned search title ("Some Title") against the full gallery title.
        self.assertGreater(match.match_accuracy or 0.0, 0.6)
        archive.refresh_from_db()
        self.assertIsNone(archive.gallery)

    def test_identical_requests_are_sent_once_per_target(self) -> None:
        """chaika's title matchers usually send the same query: only the first one reaches the site."""
        from core.providers.chaika.matchers import CleanTitleMatcher, TitleMatcher, TitleMetaMatcher

        matchers = [cls(crawler_settings) for cls in (TitleMatcher, CleanTitleMatcher, TitleMetaMatcher)]
        sent: list[tuple[str, dict]] = []

        def requester(url: str, request_dict: dict, post: bool = False) -> MagicMock:
            sent.append((url, dict(request_dict["params"])))
            return fake_response(json_data=[])

        engine = MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.ONLINE), matchers=matchers)
        with patch("core.base.matchers.request_by_provider", return_value=requester):
            engine.process(MatchTarget(file_path="a/Some Title.zip"))
            self.assertEqual(len(sent), 1)
            # The cache only lives for one target.
            engine.process(MatchTarget(file_path="a/Some Title.zip"))
            self.assertEqual(len(sent), 2)
            # A different query is sent.
            engine.process(MatchTarget(file_path="a/Other Title.zip"))
            self.assertEqual(len(sent), 3)
        self.assertEqual(engine.response_cache, {})

    def test_chaika_response_parsing(self) -> None:
        container = Gallery.objects.create(gid="500", provider="panda", title="Container")
        result = ChaikaMatcher.galleries_from_response(
            [
                {"gid": "1", "provider": "panda", "title": "A", "link": "l1", "posted": 1600000000, "gallery_container": "500"},
                {"gid": "2", "provider": "panda", "title": "B", "link": "l2", "posted": 0},
                {"result": "no link, skipped"},
            ]
        )
        assert result.galleries is not None
        self.assertEqual(result.links, ["l1", "l2"])
        self.assertEqual([g.gid for g in result.galleries], ["1", "2"])
        self.assertEqual(result.galleries[0].gallery_container_gid, container.gid)
        self.assertIsNone(result.galleries[1].posted)

    def test_wanted_galleries_online(self) -> None:
        wanted = WantedGallery.objects.create(title="Wanted", search_title="Some Title")
        matcher = FakeTitleMatcher(crawler_settings, results=[self.gallery_data("9", "Some Title")])
        with patch("core.matching.engine.MatchingEngine._online_matchers", return_value=[matcher]):
            create_matches_wanted_galleries_from_providers(WantedGallery.objects.filter(pk=wanted.pk), "chaika")
        match = GalleryMatch.objects.get(wanted_gallery=wanted)
        self.assertEqual(match.gallery.gid, "9")
        self.assertEqual(match.gallery.dl_type, "gallery_match")


class OfflineEngineTests(TestCase):
    panda_gallery: Gallery
    fakku_gallery: Gallery
    other_gallery: Gallery

    @classmethod
    def setUpTestData(cls) -> None:
        ensure_default_user()
        cls.panda_gallery = Gallery.objects.create(
            gid="1001", provider="panda", token="a", title="[Circle (Artist)] Wonderful Title", filesize=123456
        )
        cls.fakku_gallery = Gallery.objects.create(gid="abc", provider="fakku", title="Wonderful Title")
        cls.other_gallery = Gallery.objects.create(gid="1002", provider="panda", token="b", title="Unrelated Thing")

    def non_match(self, name: str, **kwargs: Any) -> Archive:
        return Archive.objects.create(title=name, zipped="files/{}.zip".format(name), match_type="non-match", **kwargs)

    def test_title_possible_matches_with_provider_scope(self) -> None:
        archive = self.non_match("[Circle (Artist)] Wonderful Title")
        engine = MatchingEngine(
            crawler_settings,
            MatchingOptions(mode=MatchingMode.OFFLINE, filters=("panda",), methods=("title",)),
        )
        engine.match_archive(archive)
        galleries = set(archive.possible_matches.values_list("gid", flat=True))
        self.assertIn("1001", galleries)
        self.assertNotIn("abc", galleries)

    def test_all_providers_when_no_filter(self) -> None:
        archive = self.non_match("Wonderful Title")
        MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.OFFLINE, methods=("title",))).match_archive(
            archive
        )
        self.assertEqual(
            set(archive.possible_matches.values_list("gid", flat=True)) & {"1001", "abc"}, {"1001", "abc"}
        )

    def test_auto_link_title(self) -> None:
        archive = self.non_match("[Circle (Artist)] Wonderful Title")
        engine = MatchingEngine(
            crawler_settings, MatchingOptions(mode=MatchingMode.OFFLINE, filters=("panda",), auto_link=True)
        )
        outcome = engine.match_archive(archive)
        archive.refresh_from_db()
        self.assertEqual(archive.gallery, self.panda_gallery)
        self.assertEqual(archive.match_type, "local_title")
        self.assertEqual(archive.title, self.panda_gallery.title)
        self.assertEqual(archive.possible_matches.count(), 0)
        assert outcome.best is not None
        self.assertEqual(engine.report.linked, 1)

    def test_auto_link_off_only_stores_candidates(self) -> None:
        archive = self.non_match("[Circle (Artist)] Wonderful Title")
        MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.OFFLINE, filters=("panda",))).match_archive(
            archive
        )
        archive.refresh_from_db()
        self.assertIsNone(archive.gallery)
        self.assertGreater(archive.possible_matches.count(), 0)

    def test_size_and_crc32(self) -> None:
        archive = self.non_match("zzz qqq", filesize=123456, crc32="DEADBEEF")
        Archive.objects.create(title="dup", zipped="files/dup.zip", crc32="DEADBEEF", gallery=self.other_gallery)
        scope = GalleryScope()
        target = MatchTarget.from_archive(archive, use_title=False)
        size_candidates = LocalSizeMatcher(scope).find(target, 0.4, 10)
        self.assertEqual([c.gallery_id for c in size_candidates], [self.panda_gallery.pk])
        crc_candidates = LocalCrc32Matcher(scope).find(target, 0.4, 10)
        self.assertEqual([c.gallery_id for c in crc_candidates], [self.other_gallery.pk])

    def test_crc32_auto_link_wins_over_title(self) -> None:
        archive = self.non_match("[Circle (Artist)] Wonderful Title", crc32="CAFEBABE")
        Archive.objects.create(title="dup", zipped="files/dup2.zip", crc32="CAFEBABE", gallery=self.other_gallery)
        engine = MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.OFFLINE, auto_link=True))
        engine.match_archive(archive)
        archive.refresh_from_db()
        self.assertEqual(archive.gallery, self.other_gallery)
        self.assertEqual(archive.match_type, "local_crc32")

    def test_title_index_loaded_once_per_run(self) -> None:
        archives = [self.non_match("Wonderful Title {}".format(i)) for i in range(3)]
        engine = MatchingEngine(crawler_settings, MatchingOptions(mode=MatchingMode.OFFLINE, methods=("title",)))
        title_matcher = engine.matchers[0]
        assert isinstance(title_matcher, LocalTitleMatcher)
        with patch.object(GalleryScope, "galleries", wraps=title_matcher.scope.galleries) as galleries:
            engine.match_archives(archives)
        self.assertEqual(galleries.call_count, 1)

    def test_archive_generate_possible_matches(self) -> None:
        archive = self.non_match("[Circle (Artist)] Wonderful Title")
        archive.generate_possible_matches(provider_filter="panda")
        self.assertIn(self.panda_gallery, archive.possible_matches.all())
        archive.generate_possible_matches(provider_filter="panda", auto_link=True)
        archive.refresh_from_db()
        self.assertEqual(archive.gallery, self.panda_gallery)

    def test_generate_possible_matches_for_archives_offline_auto_link(self) -> None:
        archive = self.non_match("[Circle (Artist)] Wonderful Title")
        generate_possible_matches_for_archives(
            Archive.objects.filter(pk=archive.pk), filters=("panda",), match_local=True, match_web=False, auto_link=True
        )
        archive.refresh_from_db()
        self.assertEqual(archive.gallery, self.panda_gallery)

    def test_wanted_galleries_offline(self) -> None:
        wanted = WantedGallery.objects.create(title="Wanted", search_title="Wonderful Title")
        create_matches_wanted_galleries_from_providers_internal(
            WantedGallery.objects.filter(pk=wanted.pk), provider_filter="fakku"
        )
        self.assertEqual(list(wanted.possible_matches.values_list("gid", flat=True)), ["abc"])

    def test_gid_method_uses_panda_formatting(self) -> None:
        archive = self.non_match("1002 some name")
        engine = MatchingEngine(
            crawler_settings, MatchingOptions(mode=MatchingMode.OFFLINE, filters=("panda",), methods=("gid",))
        )
        engine.match_archive(archive)
        self.assertEqual(list(archive.possible_matches.values_list("gid", flat=True)), ["1002"])


class FolderCrawlerMatchingTests(TestCase):
    def setUp(self) -> None:
        self.media_root = tempfile.mkdtemp()
        self.old_media_root = crawler_settings.MEDIA_ROOT
        crawler_settings.MEDIA_ROOT = self.media_root
        self.media_patch = override_settings(MEDIA_ROOT=self.media_root)
        self.media_patch.enable()

    def tearDown(self) -> None:
        self.media_patch.disable()
        crawler_settings.MEDIA_ROOT = self.old_media_root
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_offline_mode_links_new_files_to_local_galleries(self) -> None:
        from core.base.setup import Settings
        from core.local.foldercrawler import FolderCrawler

        ensure_default_user()
        gallery = Gallery.objects.create(gid="4242", provider="panda", token="t", title="[Artist] Local Gallery Title")
        make_zip(os.path.join(self.media_root, "incoming", "[Artist] Local Gallery Title.zip"))
        make_zip(os.path.join(self.media_root, "incoming", "nothing alike at all.zip"), images=1)

        folder_settings = Settings(load_from_config=crawler_settings.config)
        folder_settings.MEDIA_ROOT = self.media_root
        folder_settings.internal_matches_for_non_matches = False
        with patch("core.base.matchers.request_by_provider", side_effect=AssertionError("no online requests")):
            FolderCrawler(folder_settings).start_crawling(["incoming", "--match-mode", "offline"])

        matched = Archive.objects.get(zipped="incoming/[Artist] Local Gallery Title.zip")
        self.assertEqual(matched.gallery, gallery)
        self.assertEqual(matched.match_type, "local_title")
        unmatched = Archive.objects.get(zipped="incoming/nothing alike at all.zip")
        self.assertEqual(unmatched.match_type, "non-match")
        self.assertIsNone(unmatched.gallery)


class ProviderTitleCleaningTests(TestCase):
    def test_nexus_clean_title_keeps_words(self) -> None:
        from core.providers.nexus.utilities import clean_title

        self.assertEqual(clean_title("[Artist]Some Title (Extra) ch.1"), "Artist Some Title Extra")
