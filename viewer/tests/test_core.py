import ssl
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import django.utils.timezone as django_tz
from django.test import TestCase

from core.base.comparison import get_list_closer_text_from_list
from core.base.setup import Settings
from core.base.types import GalleryData
from core.downloaders.postdownload import PostDownloader
from core.providers.panda.parsers import Parser as PandaParser
from core.workers.download_progress import DownloadProgressChecker
from viewer.models import Archive, DownloadEvent, FoundGallery, Gallery, Provider, Tag, WantedGallery


class CoreTest(TestCase):
    test_gallery1: Gallery
    test_gallery2: Gallery

    @classmethod
    def setUpTestData(cls):
        cls.test_gallery1 = Gallery.objects.create(title="sample non public gallery 1", gid="344", provider="panda")
        cls.test_gallery2 = Gallery.objects.create(title="sample non public gallery 2", gid="342", provider="test")

    def test_repeated_archives(self):

        title_to_check = "my title"
        galleries_title_id = [(gallery.title, gallery.pk) for gallery in Gallery.objects.all()]
        cutoff = 0.4
        max_matches = 10

        similar_list = get_list_closer_text_from_list(title_to_check, galleries_title_id, cutoff, max_matches)

        self.assertIsNone(similar_list)

        title_to_check_2 = "public gallery 1"

        similar_list = get_list_closer_text_from_list(
            title_to_check_2, galleries_title_id, cutoff, max_matches
        )

        self.assertIsNotNone(similar_list)
        self.assertEqual(len(similar_list), 2)
        self.assertEqual(similar_list[0][0], "sample non public gallery 1")
        self.assertEqual(similar_list[0][2], 0.7441860465116279)

    def test_webserver_settings_defaults(self):
        settings = Settings(load_from_disk=False)
        self.assertEqual(settings.webserver.backend, "gunicorn")
        self.assertEqual(settings.webserver.socket_queue_size, 5)
        self.assertEqual(settings.webserver.thread_pool, 10)
        self.assertEqual(settings.webserver.workers, 4)
        self.assertEqual(settings.webserver.threads, 1)
        self.assertEqual(settings.webserver.timeout, 60)

    def test_webserver_settings_from_config(self):
        config = {
            "webserver": {
                "backend": "cherrypy",
                "socket_queue_size": 128,
                "thread_pool": 30,
                "workers": 8,
                "threads": 2,
                "timeout": 120,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.webserver.backend, "cherrypy")
        self.assertEqual(settings.webserver.socket_queue_size, 128)
        self.assertEqual(settings.webserver.thread_pool, 30)
        self.assertEqual(settings.webserver.workers, 8)
        self.assertEqual(settings.webserver.threads, 2)
        self.assertEqual(settings.webserver.timeout, 120)

    def test_webserver_settings_from_config_aliases(self):
        config_engine = {
            "webserver": {
                "engine": "cherrypy",
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config_engine)
        self.assertEqual(settings.webserver.backend, "cherrypy")

        config_type = {
            "webserver": {
                "type": "gunicorn",
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config_type)
        self.assertEqual(settings.webserver.backend, "gunicorn")

    def test_webserver_settings_from_config_strings(self):
        config = {
            "webserver": {
                "backend": "GUNICORN",
                "socket_queue_size": "256",
                "thread_pool": "50",
                "workers": "6",
                "threads": "3",
                "timeout": "90",
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.webserver.backend, "gunicorn")
        self.assertEqual(settings.webserver.socket_queue_size, 256)
        self.assertEqual(settings.webserver.thread_pool, 50)
        self.assertEqual(settings.webserver.workers, 6)
        self.assertEqual(settings.webserver.threads, 3)
        self.assertEqual(settings.webserver.timeout, 90)

    def test_webserver_settings_from_environment(self):
        import os
        old_backend = os.environ.get("WEBSERVER_BACKEND")
        old_workers = os.environ.get("WEBSERVER_WORKERS")
        old_threads = os.environ.get("WEBSERVER_THREADS")
        old_timeout = os.environ.get("WEBSERVER_TIMEOUT")
        try:
            os.environ["WEBSERVER_BACKEND"] = "cherrypy"
            os.environ["WEBSERVER_WORKERS"] = "12"
            os.environ["WEBSERVER_THREADS"] = "4"
            os.environ["WEBSERVER_TIMEOUT"] = "45"

            config = {
                "webserver": {
                    "backend": "gunicorn",
                    "workers": 2,
                    "threads": 1,
                    "timeout": 30,
                },
                "providers": {},
            }
            settings = Settings(load_from_config=config)
            self.assertEqual(settings.webserver.backend, "cherrypy")
            self.assertEqual(settings.webserver.workers, 12)
            self.assertEqual(settings.webserver.threads, 4)
            self.assertEqual(settings.webserver.timeout, 45)
        finally:
            if old_backend is not None:
                os.environ["WEBSERVER_BACKEND"] = old_backend
            else:
                os.environ.pop("WEBSERVER_BACKEND", None)
            if old_workers is not None:
                os.environ["WEBSERVER_WORKERS"] = old_workers
            else:
                os.environ.pop("WEBSERVER_WORKERS", None)
            if old_threads is not None:
                os.environ["WEBSERVER_THREADS"] = old_threads
            else:
                os.environ.pop("WEBSERVER_THREADS", None)
            if old_timeout is not None:
                os.environ["WEBSERVER_TIMEOUT"] = old_timeout
            else:
                os.environ.pop("WEBSERVER_TIMEOUT", None)

    def test_gunicorn_application_and_static_handler(self):
        from server import MediaAndStaticFilesHandler, GunicornApplication
        from django.http import HttpResponse

        def dummy_app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"hello from dummy app"]

        handler = MediaAndStaticFilesHandler(dummy_app)

        import io

        # Test dummy app pass-through
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/api/test",
            "SERVER_NAME": "localhost",
            "SERVER_PORT": "80",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(b""),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        res_headers = []
        def start_response(status, headers, exc_info=None):
            res_headers.extend(headers)
        result = handler(environ, start_response)
        self.assertEqual(list(result), [b"hello from dummy app"])

        # Test static file serving with CORS
        static_environ = dict(environ)
        static_environ["PATH_INFO"] = "/static/browserconfig.xml"
        static_headers = []
        static_status = []
        def static_start_response(status, headers, exc_info=None):
            static_status.append(status)
            static_headers.extend(headers)

        with patch("django.core.signals.request_started.send"), patch("django.core.signals.request_finished.send"):
            handler(static_environ, static_start_response)
        self.assertEqual(static_status[0], "200 OK")
        headers_dict = dict(static_headers)
        self.assertEqual(headers_dict.get("Access-Control-Allow-Origin"), "*")

        # Test GunicornApplication config
        g_app = GunicornApplication(handler, {
            "bind": "127.0.0.1:9999",
            "workers": 2,
            "timeout": 45,
        })
        self.assertEqual(g_app.cfg.bind, ["127.0.0.1:9999"])
        self.assertEqual(g_app.cfg.workers, 2)
        self.assertEqual(g_app.cfg.timeout, 45)
        self.assertIs(g_app.load(), handler)

        # Verify run_gunicorn does not register on_exit, registers post_fork, and closes DB connections
        import argparse
        from server import run_gunicorn
        args = argparse.Namespace(
            port=9999,
            socket_file=None,
            socket_queue_size=None,
            workers=2,
            threads=1,
            timeout=30,
            pidfile=None,
            daemonize=False,
        )
        settings = Settings(load_from_disk=False)
        with (
            patch("server.GunicornApplication") as mock_gapp,
            patch("pandabackup.settings.WORKERS.start_workers"),
            patch("django.db.connections.close_all") as mock_close_all,
            patch.object(Settings, "create_missing_directories") as mock_create_dirs,
        ):
            run_gunicorn(settings, args)
            mock_gapp.assert_called_once()
            call_options = mock_gapp.call_args[0][1]
            self.assertNotIn("on_exit", call_options)
            self.assertIn("post_fork", call_options)
            self.assertTrue(callable(call_options["post_fork"]))
            mock_close_all.assert_called()
            mock_close_all.reset_mock()
            call_options["post_fork"](None, None)
            mock_close_all.assert_called_once()
            mock_create_dirs.assert_called_once()


class WantedGalleryTest(TestCase):
    english_tag: Tag
    artist_tag: Tag
    artist2_tag: Tag
    parody_tag: Tag
    provider_instance: Provider | None
    test_gallery1: Gallery
    test_gallery2: Gallery
    test_gallery3: Gallery
    test_gallery4: Gallery
    test_gallery5: Gallery
    test_gallery6: Gallery
    test_wanted_gallery1: WantedGallery
    test_wanted_gallery3: WantedGallery
    test_wanted_gallery4: WantedGallery
    test_wanted_gallery4b: WantedGallery
    test_wanted_gallery4c: WantedGallery
    test_wanted_gallery5: WantedGallery
    test_wanted_gallery_regex: WantedGallery

    @classmethod
    def setUpTestData(cls):
        cls.english_tag = Tag.objects.create(scope="language", name="english")
        cls.artist_tag = Tag.objects.create(scope="artist", name="suzunomoku")
        cls.artist2_tag = Tag.objects.create(scope="artist", name="mitsuya")
        cls.parody_tag = Tag.objects.create(scope="parody", name="original")

        cls.provider_instance = Provider.objects.filter(slug="fakku").first()

        cls.test_gallery1 = Gallery.objects.create(title="sample non public gallery 1", gid="344", provider="panda")
        cls.test_gallery2 = Gallery.objects.create(title="sample non public gallery 2", gid="342", provider="test")

        cls.test_wanted_gallery1 = WantedGallery.objects.create(
            title="test wanted gallery",
            book_type="Manga",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
        )
        cls.test_wanted_gallery1.wanted_tags.set([cls.english_tag, cls.artist_tag])

        WantedGallery.objects.bulk_create(
            [
                WantedGallery(
                    title="repeated wanted gallery {}".format(i),
                    search_title="Dopyu",
                    book_type="Manga",
                    publisher="wanimagazine",
                    reason="wanimagazine",
                    should_search=True,
                    keep_searching=True,
                    notify_when_found=False,
                )
                for i in range(10)
            ]
        )

        cls.test_wanted_gallery3 = WantedGallery.objects.create(
            title="test wanted gallery 3",
            search_title="gallery",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            found=True,
        )

        cls.test_gallery3 = Gallery.objects.create(
            title="existing gallery", gid="345", provider="panda", category="Manga", token="4324234"
        )

        FoundGallery.objects.create(wanted_gallery=cls.test_wanted_gallery3, gallery=cls.test_gallery3)

        cls.test_wanted_gallery4 = WantedGallery.objects.create(
            title="test wanted gallery 4",
            book_type="Manga",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            wanted_tags_exclusive_scope=True,
            exclusive_scope_name="artist",
        )
        cls.test_wanted_gallery4.wanted_tags.set([cls.english_tag, cls.artist2_tag])

        cls.test_wanted_gallery4b = WantedGallery.objects.create(
            title="test wanted gallery 4b",
            book_type="Manga",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            wanted_tags_exclusive_scope=True,
            exclusive_scope_name="artist",
            wanted_tags_accept_if_none_scope="parody",
        )
        cls.test_wanted_gallery4b.wanted_tags.set([cls.artist2_tag, cls.parody_tag])

        cls.test_wanted_gallery4c = WantedGallery.objects.create(
            title="test wanted gallery 4c",
            book_type="Manga",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            wanted_tags_accept_if_none_scope="parody",
        )
        cls.test_wanted_gallery4c.wanted_tags.set([cls.artist2_tag, cls.parody_tag])

        cls.test_gallery4 = Gallery.objects.create(
            title="New Release 4", gid="34665", provider="panda", category="Manga", token="4324239"
        )
        cls.test_gallery4.tags.set([cls.english_tag, cls.artist_tag, cls.artist2_tag])

        cls.test_gallery5 = Gallery.objects.create(
            title="New Release 5", gid="346659", provider="panda", category="Manga", token="4324288"
        )
        cls.test_gallery5.tags.set([cls.english_tag, cls.artist2_tag, cls.parody_tag])

        cls.test_wanted_gallery5 = WantedGallery.objects.create(
            title="test wanted gallery 5",
            search_title="kairakuten",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            unwanted_title="[Chinese]",
        )

        if cls.provider_instance:
            cls.test_wanted_gallery5.unwanted_providers.add(cls.provider_instance)

        cls.test_gallery6 = Gallery.objects.create(
            title="COMIC Kairakuten 2022-06 [Digital]",
            title_jpn="COMIC 快楽天 2022年6月号 [DL版]",
            gid="2207323",
            token="95307725e5",
            provider="panda",
            category="Manga",
            posted=datetime.fromtimestamp(1651311800, timezone.utc),
            filecount=376,
            filesize=1043530781,
        )

        cls.test_wanted_gallery_regex = WantedGallery.objects.create(
            title="test wanted gallery regex",
            book_type="user",
            reason="kairakuten_raw",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            search_title=r"COMIC Kairakuten \d{4}-\d{2}",
            regexp_search_title=True,
            regexp_search_title_icase=True,
            category="Manga",
            wanted_page_count_lower=100,
            wanted_page_count_upper=0,
        )

    def test_match_gallery(self) -> None:
        settings = Settings(load_from_disk=True)
        parser = PandaParser(settings)

        wanted_galleries = WantedGallery.objects.all()

        gallery_link = "https://e-hentai.org/g/2079628/cec767079f/"

        incoming_gallery = GalleryData(
            "2079628",
            "panda",
            token="cec767079f",
            link=gallery_link,
            title="[Suzunomoku] Dopyu-Dopyu Of The Dead (WEEKLY Kairakuten 2021 No. 45) [English]",
            title_jpn="ドピュードピュ・オブ・ザ・デッド",
            thumbnail_url="https://ehgt.org/64/ff/64ff16f7d4ddde36be517a498a50a88911ca68de-4149126-1359-1920-png_l.jpg",
            filecount=17,
            filesize=64185347,
            category="Manga",
            uploader="GeeseIsLeese",
            tags=[
                "artist:suzunomoku",
                "parody:original",
                "female:ahegao",
                "language:english",
                "language:translated",
            ],
        )

        gallery_wanted_lists: dict[str, list["WantedGallery"]] = defaultdict(list)

        # 5 viewer related queries, 1 log related query
        if settings.disable_sql_log:
            expected_queries = 5
        else:
            expected_queries = 6
        with self.assertNumQueries(expected_queries):
            parser.compare_gallery_with_wanted_filters(
                incoming_gallery, gallery_link, wanted_galleries, gallery_wanted_lists
            )

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery.gid]), 12)
        self.assertEqual(WantedGallery.objects.filter(found=True).count(), 13)

        incoming_gallery1 = self.test_gallery1.as_gallery_data()
        incoming_gallery2 = self.test_gallery2.as_gallery_data()
        incoming_gallery3 = self.test_gallery3.as_gallery_data()
        incoming_gallery4 = self.test_gallery4.as_gallery_data()
        incoming_gallery5 = self.test_gallery5.as_gallery_data()
        incoming_gallery6 = self.test_gallery6.as_gallery_data()

        gallery_wanted_lists = defaultdict(list)

        # 2 are not already found, remaining it is.
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery1, incoming_gallery1.link, wanted_galleries, gallery_wanted_lists
        )
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery2, incoming_gallery2.link, wanted_galleries, gallery_wanted_lists
        )
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery3, incoming_gallery3.link, wanted_galleries, gallery_wanted_lists
        )
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery4, incoming_gallery4.link, wanted_galleries, gallery_wanted_lists
        )
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery5, incoming_gallery5.link, wanted_galleries, gallery_wanted_lists
        )
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery6, incoming_gallery6.link, wanted_galleries, gallery_wanted_lists
        )

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery1.gid]), 1)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery2.gid]), 1)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery3.gid]), 0)
        # Should be wg1 and wg4_c, but not wg4 and wg4_b, even if parody is set, should be rejected at multiple scopes,
        # don't accept repeated artist tag Gallery
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery4.gid]), 2)
        # Should be wg4 , wg4_b and wg4_c, because it has only 1 artist tag, and even with parody missing,
        # it has accepted none
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery5.gid]), 3)
        # 1 + regex
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery6.gid]), 2)

        rematch_result_1 = self.test_gallery1.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_4 = self.test_gallery4.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_5 = self.test_gallery5.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_6 = self.test_gallery6.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)

        # Make sure compare_gallery_with_wanted_filters gives the same result as match_against_wanted_galleries
        self.assertEqual(rematch_result_1, gallery_wanted_lists[incoming_gallery1.gid])
        self.assertEqual(rematch_result_4, gallery_wanted_lists[incoming_gallery4.gid])
        self.assertEqual(rematch_result_5, gallery_wanted_lists[incoming_gallery5.gid])
        self.assertEqual(rematch_result_6, gallery_wanted_lists[incoming_gallery6.gid])

        # Replicate the code inside core/base/handlers.py
        FoundGallery.objects.get_or_create(wanted_gallery=self.test_wanted_gallery5, gallery=self.test_gallery6)

        self.test_wanted_gallery5.found = True
        self.test_wanted_gallery5.save()

        wanted_found = WantedGallery.objects.filter(foundgallery__gallery=self.test_gallery6)

        rematch_wanted = self.test_gallery6.match_against_wanted_galleries(
            wanted_filters=wanted_found, skip_already_found=False
        )

        wanted_invalidated: list["WantedGallery"] = []

        for single_wanted_found in wanted_found:
            if single_wanted_found not in rematch_wanted:
                wanted_invalidated.append(single_wanted_found)

        self.assertEqual(len(wanted_invalidated), 0)


class VirtualDownloaderTest(TestCase):
    def test_parse_custom_downloaders_list_format(self):
        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "downloaders": {
                "chaika_archive": 5,
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertIn("panda_archive_then_hath", settings.custom_downloaders)
        self.assertEqual(settings.custom_downloaders["panda_archive_then_hath"], {"panda_archive": 1, "panda_hath": 2})
        self.assertEqual(settings.get_custom_downloader_provider("panda_archive_then_hath"), "panda")
        self.assertIn("panda_archive_then_hath", settings.downloaders)
        self.assertEqual(settings.downloaders["panda_archive_then_hath"], -1)

    def test_parse_custom_downloaders_dict_format(self):
        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": {
                    "panda_archive": 1,
                    "panda_hath": 2,
                }
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.custom_downloaders["panda_archive_then_hath"], {"panda_archive": 1, "panda_hath": 2})
        self.assertEqual(settings.get_custom_downloader_provider("panda_archive_then_hath"), "panda")

    def test_parse_custom_downloaders_explicit_provider(self):
        config = {
            "custom_downloaders": {
                "my_custom": {
                    "provider": "panda",
                    "downloaders": [
                        {"panda_archive": 1},
                        {"panda_hath": 2},
                    ],
                }
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.get_custom_downloader_provider("my_custom"), "panda")
        self.assertEqual(settings.custom_downloaders["my_custom"], {"panda_archive": 1, "panda_hath": 2})

    def test_allow_downloaders_only_virtual(self):
        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "downloaders": {
                "chaika_archive": 5,
                "panda_archive": 4,
                "panda_torrent": 3,
                "panda_hath": -1,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        settings.allow_downloaders_only(["panda_archive_then_hath"], replace_existing=True, retry_failed=True, redownload=True)

        self.assertEqual(settings.downloaders["panda_archive"], 1)
        self.assertEqual(settings.downloaders["panda_hath"], 2)
        self.assertEqual(settings.downloaders["chaika_archive"], -1)
        self.assertEqual(settings.downloaders["panda_torrent"], -1)
        self.assertTrue(settings.replace_metadata)
        self.assertTrue(settings.retry_failed)
        self.assertTrue(settings.redownload)

    def test_enable_downloader_only_virtual(self):
        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "downloaders": {
                "chaika_archive": 5,
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        settings.enable_downloader_only("panda_archive_then_hath")

        self.assertEqual(settings.downloaders["panda_archive"], 1)
        self.assertEqual(settings.downloaders["panda_hath"], 2)
        self.assertEqual(settings.downloaders["chaika_archive"], -1)

    def test_get_downloaders_name_priority(self):
        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "downloaders": {
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        all_dls = dict(settings.provider_context.get_downloaders_name_priority(settings))
        self.assertIn("panda_archive_then_hath", all_dls)

        panda_dls = dict(settings.provider_context.get_downloaders_name_priority(settings, filter_provider="panda"))
        self.assertIn("panda_archive_then_hath", panda_dls)

        chaika_dls = dict(settings.provider_context.get_downloaders_name_priority(settings, filter_provider="chaika"))
        self.assertNotIn("panda_archive_then_hath", chaika_dls)

        info_dls = dict(settings.provider_context.get_downloaders_name_priority(settings, filter_type="info"))
        self.assertNotIn("panda_archive_then_hath", info_dls)

    def test_provider_isolation_filtering(self):
        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"chaika_archive": 2},  # Mismatched provider
                ]
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        # chaika_archive should be excluded because the provider is panda
        self.assertIn("panda_archive", settings.custom_downloaders["panda_archive_then_hath"])
        self.assertNotIn("chaika_archive", settings.custom_downloaders["panda_archive_then_hath"])


class PostDownloaderFTPTest(TestCase):
    def setUp(self):
        config = {
            "ftp_configs": {
                "hath": {
                    "address": "ftp.secret-server.local",
                    "user": "super_secret_user",
                    "passwd": "ultra_secret_password_123",
                    "no_certificate_check": False,
                },
                "custom_port": {
                    "address": "ftp.custom-port.local:2121",
                    "user": "another_user",
                    "passwd": "another_password",
                    "no_certificate_check": True,
                },
            },
            "download_ftp_hath": "hath",
            "download_ftp_torrent": "hath",
            "providers": {},
        }
        self.settings = Settings(load_from_config=config)
        self.downloader = PostDownloader(self.settings)

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_start_connection_ssleof_error(self, mock_ftp_cls):
        mock_ftp_cls.side_effect = ssl.SSLEOFError(
            "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1082)"
        )
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
            result = self.downloader.start_connection("hath")
            self.assertFalse(result)
            self.assertIsNone(self.downloader.ftp_datas["hath"].ftps)

        log_output = "\n".join(cm.output)
        self.assertIn("ftp.secret-server.local:21", log_output)
        self.assertIn("SSLEOFError", log_output)
        self.assertIn("Remote server closed the connection unexpectedly", log_output)
        # Verify credentials are NOT logged
        self.assertNotIn("super_secret_user", log_output)
        self.assertNotIn("ultra_secret_password_123", log_output)

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_start_connection_connection_refused(self, mock_ftp_cls):
        mock_ftp_cls.side_effect = ConnectionRefusedError("[Errno 111] Connection refused")
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
            result = self.downloader.start_connection("hath")
            self.assertFalse(result)
            self.assertIsNone(self.downloader.ftp_datas["hath"].ftps)

        log_output = "\n".join(cm.output)
        self.assertIn("ftp.secret-server.local:21", log_output)
        self.assertIn("ConnectionRefusedError", log_output)
        self.assertIn("Connection was refused", log_output)
        self.assertNotIn("super_secret_user", log_output)
        self.assertNotIn("ultra_secret_password_123", log_output)

    def test_start_connection_missing_config(self):
        with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
            result = self.downloader.start_connection("non_existent")
            self.assertFalse(result)
            self.assertIsNone(self.downloader.ftp_datas["non_existent"].ftps)
        log_output = "\n".join(cm.output)
        self.assertIn("non_existent", log_output)

    @patch("core.downloaders.postdownload.FTP_TLS")
    def test_start_connection_success(self, mock_ftp_cls):
        mock_instance = MagicMock()
        mock_ftp_cls.return_value = mock_instance
        result = self.downloader.start_connection("hath")
        self.assertTrue(result)
        self.assertEqual(self.downloader.ftp_datas["hath"].ftps, mock_instance)
        mock_instance.prot_p.assert_called_once()

    @patch.object(PostDownloader, "download_all_missing")
    def test_do_transfer_by_method_catches_connection_errors(self, mock_download):
        mock_download.side_effect = ConnectionRefusedError("Connection refused")
        with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
            # Should not raise exception
            self.downloader.do_transfer_by_method("remote", archives=[])
        self.assertEqual(mock_download.call_count, 3)
        log_output = "\n".join(cm.output)
        self.assertIn("Download failed, restarting connection", log_output)

    def test_torrent_lifespan_settings_defaults(self):
        settings = Settings(load_from_disk=False)
        self.assertEqual(settings.max_torrent_lifespan, 0)
        self.assertEqual(settings.max_torrent_lifespan_type, "days")

    def test_torrent_lifespan_settings_from_config(self):
        config = {
            "general": {
                "max_torrent_lifespan": 12,
                "max_torrent_lifespan_type": "hours",
            }
        }
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.max_torrent_lifespan, 12)
        self.assertEqual(settings.max_torrent_lifespan_type, "hours")

    def test_check_torrent_progress_finish_on_100_percent(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 60
        settings.max_torrent_lifespan_type = "seconds"
        checker = DownloadProgressChecker(settings)

        event = DownloadEvent.objects.create(
            name="test_torrent_100",
            method="torrent",
            download_id="dl_100",
            progress=50.0,
        )
        checker.check_torrent_progress(event, 100.0)
        event.refresh_from_db()
        self.assertEqual(event.progress, 100.0)
        self.assertTrue(event.completed)
        self.assertFalse(event.failed)

    def test_check_torrent_progress_finish_when_archive_crc32_set(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 10
        settings.max_torrent_lifespan_type = "seconds"
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(
            title="completed archive",
            crc32="ABCD1234",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_crc32",
            method="torrent",
            download_id="dl_crc32",
            archive=archive,
            progress=30.0,
        )
        # Simulate create_date in the past (exceeding lifespan)
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 40.0)
        event.refresh_from_db()
        # Because archive.crc32 is set, it should finish download, NOT purge!
        self.assertTrue(event.completed)
        self.assertFalse(event.failed)
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())
        self.assertEqual(event.archive_id, archive.pk)

    def test_check_torrent_progress_default_disabled_does_not_purge(self):
        settings = Settings(load_from_disk=False)
        self.assertEqual(settings.max_torrent_lifespan, 0)
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(
            title="pending archive",
            crc32="",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_default",
            method="torrent",
            download_id="dl_default",
            archive=archive,
            progress=20.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(days=30)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 25.0)
        event.refresh_from_db()
        self.assertFalse(event.completed)
        self.assertFalse(event.failed)
        self.assertEqual(event.progress, 25.0)
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())

    def test_check_torrent_progress_not_exceeded_does_not_purge(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 10
        settings.max_torrent_lifespan_type = "hours"
        checker = DownloadProgressChecker(settings)

        archive = Archive.objects.create(
            title="active archive",
            crc32="",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_active",
            method="torrent",
            download_id="dl_active",
            archive=archive,
            progress=15.0,
        )
        # Only 5 minutes passed, limit is 10 hours
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(minutes=5)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 20.0)
        event.refresh_from_db()
        self.assertFalse(event.completed)
        self.assertFalse(event.failed)
        self.assertEqual(event.progress, 20.0)
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())

    def test_check_torrent_progress_lifespan_exceeded_purges_and_requeues(self):
        config = {
            "general": {
                "max_torrent_lifespan": 60,
                "max_torrent_lifespan_type": "seconds",
            },
            "downloaders": {
                "panda_torrent": 1,
                "panda_torrent_api": 2,
                "panda_archive": 3,
            },
        }
        settings = Settings(load_from_config=config)
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery for dead torrent", gid="999", provider="panda")
        archive = Archive.objects.create(
            title="dead archive",
            crc32="",
            gallery=gallery,
            reason="backup_reason",
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_torrent_dead",
            method="torrent",
            download_id="dl_dead",
            archive=archive,
            gallery=gallery,
            progress=10.0,
        )
        # Exceeded: 120s passed, limit is 60s
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=120)
        )
        event.refresh_from_db()

        with patch.object(archive, "delete_all_files") as mock_delete_files:
            checker.check_torrent_progress(event, 10.0)

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertIsNone(event.archive)
        # Archive should have been deleted from DB
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())

        # Check requeue call
        mock_web_queue.enqueue_args_list.assert_called_once()
        call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
        self.assertEqual(call_args[0], (gallery.get_link(),))
        override_options = call_kwargs["override_options"]
        self.assertTrue(override_options.redownload)
        self.assertTrue(override_options.replace_metadata)
        self.assertTrue(override_options.retry_failed)
        self.assertEqual(override_options.downloaders.get("panda_torrent"), -1)
        self.assertEqual(override_options.downloaders.get("panda_torrent_api"), -1)
        self.assertEqual(override_options.archive_reason, "backup_reason")

    def test_check_torrent_progress_lifespan_exceeded_without_archive(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 30
        settings.max_torrent_lifespan_type = "sec"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery without archive", gid="888", provider="panda")
        event = DownloadEvent.objects.create(
            name="test_torrent_no_archive",
            method="torrent",
            download_id="dl_no_archive",
            archive=None,
            gallery=gallery,
            progress=5.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=60)
        )
        event.refresh_from_db()

        checker.check_torrent_progress(event, 5.0)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        mock_web_queue.enqueue_args_list.assert_called_once()

    def test_check_torrent_progress_lifespan_units(self):
        settings = Settings(load_from_disk=False)
        checker = DownloadProgressChecker(settings)

        # 2 minutes = 120 seconds
        settings.max_torrent_lifespan = 2
        settings.max_torrent_lifespan_type = "minutes"
        event = DownloadEvent.objects.create(
            name="test_torrent_minutes",
            method="torrent",
            download_id="dl_min",
            progress=10.0,
        )
        # 90 seconds passed -> not exceeded
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=90)
        )
        event.refresh_from_db()
        checker.check_torrent_progress(event, 10.0)
        event.refresh_from_db()
        self.assertFalse(event.completed)

        # 150 seconds passed -> exceeded
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=150)
        )
        event.refresh_from_db()
        checker.check_torrent_progress(event, 10.0)
        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)

    @patch("core.workers.download_progress.close_old_connections")
    @patch("core.workers.download_progress.get_torrent_client")
    def test_job_integration_with_torrent_lifespan(self, mock_get_torrent_client, mock_close_old):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 50
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery job integration", gid="777", provider="panda")
        archive = Archive.objects.create(
            title="job archive",
            crc32="",
            gallery=gallery,
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="test_job_torrent",
            method="torrent",
            download_id="dl_job",
            archive=archive,
            gallery=gallery,
            progress=20.0,
        )
        # Exceeded: 100s passed, limit is 50s
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        mock_client = MagicMock()
        mock_client.connect.return_value = True
        mock_client.get_download_progress.return_value = [(event, 25.0)]
        mock_get_torrent_client.return_value = mock_client

        # Run job for exactly 1 cycle
        checker.force_run_once = True
        with patch.object(checker.stop, "wait", side_effect=[False, True]), \
             patch.object(checker.stop, "is_set", side_effect=[False, True]):
            checker.job()

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())
        mock_web_queue.enqueue_args_list.assert_called_once()




