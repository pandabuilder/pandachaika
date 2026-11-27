"""The doujinshi_mirror provider (doujinshi-browser JSON API), and what is left of the old mugimugi provider.

Every request is mocked: the responses have the shape of the mirror's ``/api/v1/books`` results.
"""

import copy
from importlib import import_module
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase, TestCase

from core.base.auto_wanted import WantedRunStats
from core.matching.types import MatchTarget
from core.providers.doujinshi_mirror import utilities
from core.providers.doujinshi_mirror.matchers import TitleMatcher
from core.providers.doujinshi_mirror.parsers import Parser
from core.providers.doujinshi_mirror.wanted import wanted_generator
from viewer.models import AutoWantedSource, Gallery, WantedGallery

MIRROR = "https://mirror.test/"


def item(type_name: str, item_id: int, name_en: str, name_jp: str = "", frq: int = 0) -> dict:
    return {"key": "X{}".format(item_id), "type": type_name, "id": item_id, "name_en": name_en, "name_jp": name_jp, "frq": frq}


BOOK = {
    "key": "B999999",
    "id": 999999,
    "name_en": "Hibiki to Pool!",
    "name_jp": "響とプール!",
    "name_r": None,
    "alt_names": [],
    "released": "2017-05-05",
    "pages": 36,
    "adult": True,
    "anthology": False,
    "copyshi": False,
    "magazine": False,
    "language": {"id": 3, "name": "Japanese"},
    "info": None,
    "type": "Doujinshi",
    "url": "https://www.doujinshi.org/book/999999/",
    "images": {
        "big": MIRROR + "images/big/499/999999.jpg",
        "thumb": MIRROR + "images/tn/499/999999.jpg",
    },
    "links": {
        "author": [item("author", 15518, "Tsurui", "鶴井", 2)],
        "circle": [item("circle", 8594, "PLANT", "PLANT", 2)],
        "character": [item("character", 3469, "Ganaha Hibiki", "我那覇響")],
        "collections": [item("collections", 2, "No Collections", "シリーズでない")],
        "contents": [
            item("contents", 1, "Swimsuit (Female)"),
            item("contents", 2, "", "褐色肌"),
            item("contents", 7, "Unknown", "不明"),
        ],
        "convention": [item("convention", 3, "MyBestFriends 10"), item("convention", 8, "No Convention")],
        "genre": [item("genre", 4, "For men", "男性向け")],
        "parody": [item("parody", 5, "PROJECT IM@S")],
        "publisher": [item("publisher", 6, "No Publisher", "出版社なし")],
        "type": [item("type", 1, "Doujinshi", "同人誌")],
    },
}


def book(book_id: int, **values) -> dict:
    data = copy.deepcopy(BOOK)
    data.update(id=book_id, key="B{}".format(book_id), name_en="Book {}".format(book_id), name_jp="本 {}".format(book_id))
    data.update(values)
    return data


class FakeResponse:
    def __init__(self, data, status_code: int = 200):
        self.data = data
        self.status_code = status_code
        self.text = str(data)

    def json(self):
        return self.data


class MirrorSettingsMixin:
    def setUp(self):
        super().setUp()
        self.own_settings = settings.CRAWLER_SETTINGS.providers["doujinshi_mirror"]
        patcher = mock.patch.object(self.own_settings, "url", MIRROR)
        patcher.start()
        self.addCleanup(patcher.stop)


class UtilitiesTests(MirrorSettingsMixin, SimpleTestCase):
    def test_book_to_gallery_data(self):
        gallery = utilities.book_to_gallery_data(BOOK, MIRROR)
        assert gallery is not None
        self.assertEqual(gallery.gid, "mugi-B999999")
        self.assertEqual(gallery.provider, "doujinshi_mirror")
        self.assertEqual(gallery.link, MIRROR + "#/book/999999")
        self.assertEqual(gallery.title, "Hibiki to Pool!")
        self.assertEqual(gallery.title_jpn, "響とプール!")
        self.assertEqual(gallery.category, "Doujinshi")
        self.assertEqual(gallery.filecount, 36)
        # Stored without the mirror's address.
        self.assertEqual(gallery.thumbnail_url, "https://img.doujinshi.org/big/499/999999.jpg")
        assert gallery.posted is not None
        self.assertEqual(gallery.posted.strftime("%Y-%m-%d %z"), "2017-05-05 +0000")
        self.assertEqual(
            sorted(gallery.tags),
            sorted(
                [
                    "artist:tsurui",
                    "group:plant",
                    "character:ganaha_hibiki",
                    "contents:swimsuit_(female)",
                    # No English name: the Japanese one.
                    "contents:褐色肌",
                    "convention:mybestfriends_10",
                    "genre:for_men",
                    "parody:project_im@s",
                    "language:japanese",
                ]
            ),
        )

    def test_thumbnail_big_then_thumb(self):
        def thumbnail(big, thumb):
            gallery = utilities.book_to_gallery_data(book(7, images={"big": big, "thumb": thumb}), MIRROR)
            assert gallery is not None
            return gallery.thumbnail_url

        big, tn = MIRROR + "images/big/3/7.jpg", MIRROR + "images/tn/3/7.jpg"
        self.assertEqual(thumbnail(big, tn), "https://img.doujinshi.org/big/3/7.jpg")
        self.assertEqual(thumbnail(None, tn), "https://img.doujinshi.org/tn/3/7.jpg")
        self.assertEqual(thumbnail(None, None), "")
        # An external image host set on the server is kept as is.
        self.assertEqual(thumbnail("https://cdn.test/big/3/7.jpg", tn), "https://cdn.test/big/3/7.jpg")

    def test_thumbnail_url_follows_the_setting(self):
        gallery = Gallery(gid="mugi-B7", provider="doujinshi_mirror", thumbnail_url="https://img.doujinshi.org/tn/3/7.jpg")
        self.assertEqual(gallery.get_thumbnail_url(), MIRROR + "images/tn/3/7.jpg")
        self.assertEqual(gallery.get_es_source_thumbnail(), MIRROR + "images/tn/3/7.jpg")
        with mock.patch.object(self.own_settings, "url", "http://other.host:8080/mirror"):
            self.assertEqual(gallery.get_thumbnail_url(), "http://other.host:8080/mirror/images/tn/3/7.jpg")
        with mock.patch.object(self.own_settings, "url", ""):
            self.assertEqual(gallery.get_thumbnail_url(), "https://img.doujinshi.org/tn/3/7.jpg")
        # Other URLs, and other providers' galleries (the old mugimugi ones too), are left alone.
        external = Gallery(gid="mugi-B7", provider="doujinshi_mirror", thumbnail_url="https://cdn.test/tn/3/7.jpg")
        self.assertEqual(external.get_thumbnail_url(), "https://cdn.test/tn/3/7.jpg")
        old = Gallery(gid="mugi-B7", provider="mugimugi", thumbnail_url="https://img.doujinshi.org/big/3/7.jpg")
        self.assertEqual(old.get_thumbnail_url(), "https://img.doujinshi.org/big/3/7.jpg")

    def test_non_h_unknown_language_and_no_date(self):
        gallery = utilities.book_to_gallery_data(
            book(5, adult=False, language={"id": 1, "name": "Unknown"}, released=None, links={}), MIRROR
        )
        assert gallery is not None
        self.assertEqual(gallery.tags, ["non-h"])
        self.assertIsNone(gallery.posted)

    def test_resolve_url_follows_the_setting(self):
        gallery = Gallery(gid="mugi-B123", provider="doujinshi_mirror")
        self.assertEqual(gallery.get_link(), MIRROR + "#/book/123")
        with mock.patch.object(self.own_settings, "url", "http://other.host:8080"):
            self.assertEqual(gallery.get_link(), "http://other.host:8080/#/book/123")
        # Without a mirror, the original site's URL.
        with mock.patch.object(self.own_settings, "url", ""):
            self.assertEqual(gallery.get_link(), "https://www.doujinshi.org/book/123/")

    def test_old_mugimugi_galleries_keep_their_url(self):
        self.assertEqual(
            Gallery(gid="mugi-B123", provider="mugimugi").get_link(), "https://www.doujinshi.org/book/123/"
        )
        # Only the resolver and the constants are left.
        for submodule in ("parsers", "matchers", "downloaders", "wanted", "settings"):
            with self.assertRaises(ImportError):
                import_module("core.providers.mugimugi." + submodule)
        self.assertNotIn("mugimugi", settings.PROVIDER_CONTEXT.wanted_provider_names())
        self.assertFalse([p for p in settings.PROVIDER_CONTEXT.parsers if p.name == "mugimugi"])

    def test_clean_title_query(self):
        self.assertEqual(utilities.clean_title_query('"COMIC LO 2017-01" -language:translated$'), "COMIC LO 2017-01")
        self.assertEqual(
            utilities.clean_title_query('Hibiki to Pool! language:"english" -anthology'), "Hibiki to Pool!"
        )
        self.assertEqual(utilities.clean_title_query("Re:Zero kara"), "Re:Zero kara")

    def test_search_api_request(self):
        books = MIRROR + "api/v1/books"
        self.assertEqual(
            utilities.search_api_request(MIRROR + "#/books?circle=PLANT&sort=id", MIRROR),
            (books, {"circle": "PLANT", "sort": "id"}),
        )
        self.assertEqual(
            utilities.search_api_request("http://mirror.test/api/v1/books?q=pool", MIRROR), (books, {"q": "pool"})
        )
        self.assertEqual(
            utilities.search_api_request(MIRROR + "#/item/circle/8594", MIRROR),
            (MIRROR + "api/v1/items/circle/8594/books", {}),
        )
        self.assertEqual(
            utilities.search_api_request(MIRROR + "api/v1/items/author/1/books?sort=released", MIRROR),
            (MIRROR + "api/v1/items/author/1/books", {"sort": "released"}),
        )
        # General link parsing keeps query parameters verbatim; only clean_query=True strips foreign syntax.
        self.assertEqual(
            utilities.search_api_request(
                MIRROR + "api/v1/books?q=hana-man+vol&per_page=48&not_collections=COMIC HANA-MAN GOLD", MIRROR
            ),
            (books, {"q": "hana-man vol", "per_page": "48", "not_collections": "COMIC HANA-MAN GOLD"}),
        )
        raw_query_url = MIRROR + 'api/v1/books?q="COMIC LO" -language:translated$'
        self.assertEqual(
            utilities.search_api_request(raw_query_url, MIRROR),
            (books, {"q": '"COMIC LO" -language:translated$'}),
        )
        self.assertEqual(
            utilities.search_api_request(raw_query_url, MIRROR, clean_query=True),
            (books, {"q": "COMIC LO"}),
        )
        for url in (
            MIRROR + "#/book/123",
            MIRROR + "api/v1/books/123",
            MIRROR,
            "https://elsewhere.test/#/books?q=a",
            "https://www.doujinshi.org/book/123/",
        ):
            self.assertIsNone(utilities.search_api_request(url, MIRROR), url)


class BacklogSearchTests(MirrorSettingsMixin, TestCase):
    def test_mirror_search_page_as_backlog_source(self):
        from viewer.services import release_series

        source = MIRROR + "api/v1/books?sort=released&order=desc&q="
        parser = release_series._search_page_parser(source)
        self.assertIsInstance(parser, Parser)
        response = FakeResponse({"results": [book(1628807, name_en="COMIC LO 2017-01")]})
        with mock.patch.object(Parser, "web_request", side_effect=[response, FakeResponse({"results": [book(1628807)]})]) as web_request:
            raws = release_series._search_page_source(source, '"COMIC LO 2017-01" -language:translated$', 10)
        params = web_request.call_args_list[0].args[1]["params"]
        self.assertEqual((params["q"], params["sort"], params["page"]), ("COMIC LO 2017-01", "released", 1))
        self.assertEqual([(x["gid"], x["provider"], x["source_url"]) for x in raws], [
            ("mugi-B1628807", "doujinshi_mirror", MIRROR + "#/book/1628807")
        ])
        # Without a mirror set, its URLs aren't anyone's.
        with mock.patch.object(self.own_settings, "url", ""):
            self.assertIsNone(release_series._search_page_parser(source))


class ParserTests(MirrorSettingsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.parser = Parser(settings.CRAWLER_SETTINGS)

    def test_accepted_urls_and_ids(self):
        urls = [
            MIRROR + "#/book/123",
            MIRROR + "api/v1/books/B124",
            MIRROR + "#/books?circle=PLANT",
            "https://www.doujinshi.org/book/125/",
            "http://www.mugimugi.org/book/126/",
            "https://other.test/#/book/127",
            MIRROR + "#/item/circle/8594",
            "https://e-hentai.org/g/1/abc/",
        ]
        self.assertEqual(self.parser.filter_accepted_urls(urls), urls[:5] + [urls[6]])
        self.assertEqual(self.parser.filter_update_urls(urls), urls[:2] + urls[3:5])
        self.assertEqual(
            [Parser.id_from_url(x) for x in urls[:5]],
            ["mugi-B123", "mugi-B124", None, "mugi-B125", "mugi-B126"],
        )

    def test_fetch_multiple_gallery_data(self):
        responses = [FakeResponse({"results": [book(2), book(1)]})]
        with mock.patch.object(Parser, "web_request", side_effect=responses) as web_request:
            galleries = self.parser.fetch_multiple_gallery_data(
                ["https://www.doujinshi.org/book/1/", MIRROR + "#/book/2", MIRROR + "#/book/3"]
            )
        # In the order asked, without the books the mirror doesn't have.
        self.assertEqual([x.gid for x in galleries], ["mugi-B1", "mugi-B2"])
        url, request_dict = web_request.call_args.args[:2]
        self.assertEqual(url, MIRROR + "api/v1/books")
        self.assertEqual(request_dict["params"]["ids"], "1,2,3")

    def test_errors_and_missing_url(self):
        with mock.patch.object(Parser, "web_request", return_value=FakeResponse({"error": "boom"}, 500)):
            self.assertEqual(self.parser.fetch_multiple_gallery_data([MIRROR + "#/book/1"]), [])
        with mock.patch.object(self.own_settings, "url", ""), mock.patch.object(Parser, "web_request") as web_request:
            self.assertEqual(self.parser.fetch_multiple_gallery_data(["https://www.doujinshi.org/book/1/"]), [])
            web_request.assert_not_called()

    def test_search_pages(self):
        pages = [
            FakeResponse({"results": [book(3), book(2)]}),
            FakeResponse({"results": [book(1)]}),
        ]
        with mock.patch.object(Parser, "web_request", side_effect=pages) as web_request:
            found = list(self.parser.search_pages(MIRROR + "#/books?circle=PLANT&per_page=2"))
        self.assertEqual([[x.gid for x in page] for page in found], [["mugi-B3", "mugi-B2"], ["mugi-B1"]])
        self.assertEqual([c.args[1]["params"]["page"] for c in web_request.call_args_list], [1, 2])
        self.assertEqual(web_request.call_args.args[1]["params"]["circle"], "PLANT")

        with mock.patch.object(Parser, "web_request", side_effect=[FakeResponse({"results": [book(4)]})]) as web_request:
            found = list(self.parser.search_pages(MIRROR + 'api/v1/books?q="hana-man" -extra$'))
        self.assertEqual([[x.gid for x in page] for page in found], [["mugi-B4"]])
        self.assertEqual(web_request.call_args.args[1]["params"]["q"], '"hana-man" -extra$')

        with mock.patch.object(Parser, "web_request", side_effect=[FakeResponse({"results": [book(4)]})]) as web_request:
            self.assertEqual(
                self.parser.links_from_search_page(MIRROR + '#/books?q="COMIC LO" -language:translated$'),
                [MIRROR + "#/book/4"],
            )
        self.assertEqual(web_request.call_args.args[1]["params"]["q"], "COMIC LO")

    def test_crawl_urls(self):
        Gallery.objects.create(gid="mugi-B2", provider="doujinshi_mirror", title="known")
        self.parser.downloaders = [(mock.Mock(), 1)]
        # Settings other tests may leave changed (properties, which mock.patch can't restore).
        crawler_settings = settings.CRAWLER_SETTINGS
        saved = (crawler_settings.update_metadata_mode, crawler_settings.replace_metadata)
        crawler_settings.update_metadata_mode, crawler_settings.replace_metadata = False, False
        self.addCleanup(setattr, crawler_settings, "update_metadata_mode", saved[0])
        self.addCleanup(setattr, crawler_settings, "replace_metadata", saved[1])
        with (
            mock.patch.object(Parser, "web_request", side_effect=[FakeResponse({"results": [book(1)]})]) as web_request,
            mock.patch.object(Parser, "pass_gallery_data_to_downloaders") as passed,
        ):
            self.parser.crawl_urls(["https://www.doujinshi.org/book/1/", MIRROR + "#/book/2", "https://other.test/x"])
        # The known one isn't fetched.
        self.assertEqual(web_request.call_args.args[1]["params"]["ids"], "1")
        self.assertEqual([x.gid for x in passed.call_args.args[0]], ["mugi-B1"])

    def test_crawl_urls_results_link_preserves_params(self):
        self.parser.downloaders = [(mock.Mock(), 1)]
        crawler_settings = settings.CRAWLER_SETTINGS
        saved = (crawler_settings.update_metadata_mode, crawler_settings.replace_metadata)
        crawler_settings.update_metadata_mode, crawler_settings.replace_metadata = False, False
        self.addCleanup(setattr, crawler_settings, "update_metadata_mode", saved[0])
        self.addCleanup(setattr, crawler_settings, "replace_metadata", saved[1])
        results_url = MIRROR + "api/v1/books?q=hana-man+vol&per_page=48&not_collections=COMIC HANA-MAN GOLD"
        with (
            mock.patch.object(
                Parser, "web_request", side_effect=[FakeResponse({"results": [book(10), book(11)]})]
            ) as web_request,
            mock.patch.object(Parser, "pass_gallery_data_to_downloaders") as passed,
        ):
            self.parser.crawl_urls([results_url])
        url, request_dict = web_request.call_args.args[:2]
        self.assertEqual(url, MIRROR + "api/v1/books")
        self.assertEqual(
            request_dict["params"],
            {
                "q": "hana-man vol",
                "per_page": "48",
                "not_collections": "COMIC HANA-MAN GOLD",
                "count": 0,
                "page": 1,
            },
        )
        self.assertEqual([x.gid for x in passed.call_args.args[0]], ["mugi-B10", "mugi-B11"])


class MatcherTests(MirrorSettingsMixin, TestCase):
    def test_title_part(self):
        self.assertEqual(
            utilities.title_part("(C92) [PLANT (Tsurui)] Hibiki to Pool! (THE IDOLM@STER) [English]"), "Hibiki to Pool!"
        )
        self.assertEqual(utilities.title_part("【Group】 COMIC_LO 2017-01 {x}"), "COMIC LO 2017-01")
        self.assertEqual(utilities.title_part("(only brackets)"), "(only brackets)")

    def test_search_and_candidates(self):
        matcher = TitleMatcher(settings.CRAWLER_SETTINGS)
        response = FakeResponse(
            {"query": "q", "results": [book(999999, name_en="Hibiki to Pool!"), book(858653, name_en="JK to Pool")]}
        )
        with mock.patch.object(TitleMatcher, "request", return_value=response) as request:
            candidates = matcher.find_online(
                MatchTarget(file_path="(C92) [PLANT (Tsurui)] Hibiki to Pool! (THE IDOLM@STER) [English].zip")
            )
        url, request_dict = request.call_args.args[:2]
        self.assertEqual(url, MIRROR + "api/v1/books/match")
        # The whole name goes to the server, which reads the circle and author from it.
        self.assertEqual(
            request_dict["params"]["title"], "(C92) [PLANT (Tsurui)] Hibiki to Pool! (THE IDOLM@STER) [English]"
        )
        best = matcher.best_candidate(candidates)
        assert best is not None and best.gallery_data is not None
        self.assertEqual(best.gallery_data.gid, "mugi-B999999")
        self.assertEqual(best.matcher, "doujinshi_mirror_title")


class WantedGeneratorTests(MirrorSettingsMixin, TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(Gallery, "schedule_thumbnail_fetch")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_source(self, responses, **values):
        source = AutoWantedSource.objects.create(
            name="mirror {}".format(AutoWantedSource.objects.count()), provider="doujinshi_mirror", params=values.pop("params", {"type": "7"}), **values
        )
        stats = WantedRunStats()
        with mock.patch.object(Parser, "web_request", side_effect=responses) as web_request:
            wanted_generator(settings.CRAWLER_SETTINGS, source, stats)
        return stats, web_request

    def test_creates_galleries_and_wanted_galleries(self):
        # Known as an old mugimugi gallery: same book, not added again.
        Gallery.objects.create(gid="mugi-B3", provider="mugimugi", title="old")
        page = [book(4), book(3)]
        stats, web_request = self.run_source([FakeResponse({"results": page})], reason="mags")

        params = web_request.call_args.args[1]["params"]
        self.assertEqual((params["type"], params["sort"], params["order"], params["page"]), ("7", "id", "desc", 1))
        self.assertEqual(stats.errors, [])
        self.assertEqual(stats.links, 1)
        self.assertEqual(stats.created, 1)
        gallery = Gallery.objects.get(gid="mugi-B4", provider="doujinshi_mirror")
        self.assertEqual(gallery.reason, "mags")
        wanted = WantedGallery.objects.get(title_jpn="本 4")
        self.assertEqual(wanted.book_type, "Doujinshi")
        self.assertEqual(list(wanted.artists.values_list("name", flat=True)), ["tsurui"])
        self.assertEqual(wanted.mentions.count(), 1)

    def test_stops_at_a_known_page_and_at_stop_page(self):
        Gallery.objects.create(gid="mugi-B1", provider="doujinshi_mirror", title="known")
        full_page = [book(100 + x) for x in range(50)]
        stats, web_request = self.run_source(
            [FakeResponse({"results": full_page}), FakeResponse({"results": [book(1)]})]
        )
        self.assertEqual(web_request.call_count, 2)
        self.assertEqual(stats.links, 50)

        stats, web_request = self.run_source(
            [FakeResponse({"results": [book(200 + x) for x in range(50)]})], params={"stop_page": 1}
        )
        self.assertEqual(web_request.call_count, 1)

    def test_no_url(self):
        with mock.patch.object(self.own_settings, "url", ""):
            stats, web_request = self.run_source([])
        web_request.assert_not_called()
        self.assertIn("general.url is not set", stats.errors[0])


class ThumbnailFetchTests(MirrorSettingsMixin, TestCase):
    def test_fetch_uses_the_current_mirror(self):
        from viewer.services import gallery_ingestion

        gallery = Gallery.objects.create(
            gid="mugi-B7", provider="doujinshi_mirror", thumbnail_url="https://img.doujinshi.org/tn/3/7.jpg"
        )
        with mock.patch.object(gallery_ingestion, "request_with_retries", return_value=None) as request:
            gallery.fetch_thumbnail()
        self.assertEqual(request.call_args.args[0], MIRROR + "images/tn/3/7.jpg")
        gallery.refresh_from_db()
        # The stored URL doesn't change.
        self.assertEqual(gallery.thumbnail_url, "https://img.doujinshi.org/tn/3/7.jpg")


class MigrationTests(TestCase):
    def test_mugimugi_sources_are_converted(self):
        migration = import_module("viewer.migrations.0236_mugimugi_auto_wanted_to_doujinshi_mirror")
        source = AutoWantedSource.objects.create(
            name="mugi", provider="mugimugi", params={"flist": "7", "slist_C": "A", "FREE": "Y"}, notes="Mine."
        )
        other = AutoWantedSource.objects.create(name="wani", provider="wanimagazine", params={"subpath": "x"})
        migration.forwards(apps, None)
        source.refresh_from_db()
        self.assertEqual(source.provider, "doujinshi_mirror")
        self.assertEqual(source.params, {"type": "7", "circle": "A"})
        self.assertTrue(source.notes.startswith("Mine.\nConverted from a mugimugi source"))
        self.assertIn("Left out (no equivalent on the mirror): FREE.", source.notes)
        other.refresh_from_db()
        self.assertEqual(other.params, {"subpath": "x"})
