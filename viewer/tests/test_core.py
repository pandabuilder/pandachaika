import os
import ssl
import subprocess
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
from viewer.models import Archive, DownloadEvent, FoundGallery, Gallery, Image, Provider, Tag, WantedGallery


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
        self.assertTrue(settings.download_progress_checker_startup)

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
        self.assertTrue(settings.download_progress_checker_startup)

    def test_download_progress_checker_startup_disabled_in_config(self):
        config = {
            "general": {
                "download_progress_checker_startup": False,
            }
        }
        settings = Settings(load_from_config=config)
        self.assertFalse(settings.download_progress_checker_startup)

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

    @patch("core.workers.download_progress.close_old_connections")
    @patch("core.workers.download_progress.get_torrent_client")
    def test_job_integration_torrent_not_in_client_still_expires_and_requeues(
        self, mock_get_torrent_client, mock_close_old
    ):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 50
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery missing in client", gid="666", provider="panda")
        archive = Archive.objects.create(title="job archive missing", crc32="", gallery=gallery, user=None)
        event = DownloadEvent.objects.create(
            name="test_missing_torrent",
            method="torrent",
            download_id="dl_missing",
            archive=archive,
            gallery=gallery,
            progress=10.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        mock_client = MagicMock()
        mock_client.connect.return_value = True
        # Torrent client returns nothing (torrent removed or ID not found)
        mock_client.get_download_progress.return_value = []
        mock_get_torrent_client.return_value = mock_client

        checker.force_run_once = True
        with patch.object(checker.stop, "wait", side_effect=[False, True]), \
             patch.object(checker.stop, "is_set", side_effect=[False, True]):
            checker.job()

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())
        mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("core.workers.download_progress.close_old_connections")
    @patch("core.workers.download_progress.get_torrent_client")
    def test_job_integration_client_connect_failure_still_expires_and_requeues(
        self, mock_get_torrent_client, mock_close_old
    ):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 50
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery client failure", gid="555", provider="panda")
        archive = Archive.objects.create(title="job archive client failure", crc32="", gallery=gallery, user=None)
        event = DownloadEvent.objects.create(
            name="test_connect_failure_torrent",
            method="torrent",
            download_id="dl_failure",
            archive=archive,
            gallery=gallery,
            progress=10.0,
        )
        DownloadEvent.objects.filter(pk=event.pk).update(
            create_date=django_tz.now() - timedelta(seconds=100)
        )
        event.refresh_from_db()

        mock_client = MagicMock()
        mock_client.connect.return_value = False
        mock_get_torrent_client.return_value = mock_client

        checker.force_run_once = True
        with patch.object(checker.stop, "wait", side_effect=[False, True]), \
             patch.object(checker.stop, "is_set", side_effect=[False, True]):
            checker.job()

        event.refresh_from_db()
        self.assertTrue(event.completed)
        self.assertTrue(event.failed)
        self.assertFalse(Archive.objects.filter(pk=archive.pk).exists())
        mock_web_queue.enqueue_args_list.assert_called_once()

    def test_check_torrent_progress_fallback_to_event_name_url(self):
        settings = Settings(load_from_disk=False)
        settings.max_torrent_lifespan = 30
        settings.max_torrent_lifespan_type = "seconds"
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        event = DownloadEvent.objects.create(
            name="https://e-hentai.org/g/12345/67890abcdef/",
            method="torrent",
            download_id="dl_no_gallery",
            archive=None,
            gallery=None,
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
        call_args, _ = mock_web_queue.enqueue_args_list.call_args
        self.assertEqual(call_args[0], ("https://e-hentai.org/g/12345/67890abcdef/",))

    def test_check_torrent_progress_disables_custom_downloader_wrapping_torrent(self):
        config = {
            "general": {
                "max_torrent_lifespan": 30,
                "max_torrent_lifespan_type": "seconds",
            },
            "downloaders": {
                "custom_with_torrent": 1,
                "panda_torrent": 2,
                "panda_archive": 3,
            },
        }
        settings = Settings(load_from_config=config)
        settings.custom_downloaders = {
            "custom_with_torrent": {"panda_torrent": 1, "panda_archive": 2}
        }
        mock_web_queue = MagicMock()
        checker = DownloadProgressChecker(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery custom dl", gid="444", provider="panda")
        event = DownloadEvent.objects.create(
            name="test_custom_dl_event",
            method="torrent",
            download_id="dl_custom",
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
        _, call_kwargs = mock_web_queue.enqueue_args_list.call_args
        override = call_kwargs["override_options"]
        self.assertEqual(override.downloaders.get("panda_torrent"), -1)
        self.assertEqual(override.downloaders.get("custom_with_torrent"), -1)
        self.assertEqual(override.downloaders.get("panda_archive"), 3)

    def test_transmission_get_download_progress_handles_invalid_ids(self):
        from core.downloaders.torrent.transmission import Transmission
        client = Transmission()
        client.trans = MagicMock()
        # transmission mock returns progress for id 10
        mock_torr = MagicMock()
        mock_torr.id = 10
        mock_torr.progress = 75.0
        client.trans.get_torrents.return_value = [mock_torr]

        dummy_key = MagicMock()
        results = client.get_download_progress([
            ("", dummy_key),
            ("not_an_int", dummy_key),
            ("10", dummy_key),
        ])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0], (dummy_key, 75.0))
        # Ensure get_torrents was called only with valid integer IDs: [10]
        client.trans.get_torrents.assert_called_once_with([10], timeout=25)

    def test_qbittorrent_initialization_and_url_building(self):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        client = qBittorrent(address="localhost", port=8080)
        self.assertEqual(client.name, "qbittorrent")
        self.assertEqual(str(client), "qbittorrent")
        self.assertFalse(client.convert_to_base64)
        self.assertFalse(client.send_url)
        self.assertEqual(client._build_base_url(), "http://localhost:8080")

        client2 = qBittorrent(address="http://127.0.0.1:9090/", port=8080)
        self.assertEqual(client2._build_base_url(), "http://127.0.0.1:9090")

        client3 = qBittorrent(address="https://seedbox.local/qbt/", port=8080)
        self.assertEqual(client3._build_base_url(), "https://seedbox.local:8080/qbt")

    @patch("requests.Session")
    def test_qbittorrent_connect_with_auth_success(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        mock_session.post.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080", user="admin", password="password123")
        self.assertTrue(client.connect())
        mock_session.post.assert_called_once_with(
            "http://localhost:8080/api/v2/auth/login",
            data={"username": "admin", "password": "password123"},
            timeout=25,
        )

    @patch("requests.Session")
    def test_qbittorrent_connect_with_auth_failure(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Fails."
        mock_session.post.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080", user="admin", password="wrongpassword")
        self.assertFalse(client.connect())
        self.assertIn("Authentication failed", client.error)

    @patch("requests.Session")
    def test_qbittorrent_connect_without_auth(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_session.get.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080", user="", password="")
        self.assertTrue(client.connect())
        mock_session.get.assert_called_once_with("http://localhost:8080/api/v2/app/version", timeout=25)

    @patch("requests.Session")
    def test_qbittorrent_add_torrent_success(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent
        from core.libs.bencoding import Encoder

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        mock_session.post.return_value = mock_resp

        torrent_payload = {
            b"announce": b"http://tracker.example.com",
            b"info": {
                b"name": b"sample_archive.zip",
                b"length": 1048576,
                b"piece length": 32768,
                b"pieces": b"12345678901234567890",
            },
        }
        torrent_bytes = bytes(Encoder(torrent_payload).encode())

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        success, info_hash = client.add_torrent(torrent_bytes, download_dir="/downloads/100")
        self.assertTrue(success)
        self.assertIsNotNone(info_hash)
        assert info_hash is not None
        self.assertEqual(len(info_hash), 40)
        self.assertEqual(client.expected_torrent_name, "sample_archive")
        self.assertEqual(client.expected_torrent_extension, ".zip")
        self.assertEqual(client.total_size, 1048576)

        mock_session.post.assert_called_once()
        call_args, call_kwargs = mock_session.post.call_args
        self.assertEqual(call_args[0], "http://localhost:8080/api/v2/torrents/add")
        self.assertEqual(call_kwargs["data"], {"savepath": "/downloads/100"})
        self.assertIn("torrents", call_kwargs["files"])

    @patch("requests.Session")
    def test_qbittorrent_add_torrent_duplicate(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent
        from core.libs.bencoding import Encoder

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "Conflict"
        mock_session.post.return_value = mock_resp

        torrent_payload = {
            b"info": {
                b"name": b"dup_archive.zip",
                b"length": 2048,
                b"piece length": 2048,
                b"pieces": b"12345678901234567890",
            },
        }
        torrent_bytes = bytes(Encoder(torrent_payload).encode())

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        success, info_hash = client.add_torrent(torrent_bytes)
        self.assertTrue(success)
        self.assertIsNotNone(info_hash)

    @patch("requests.Session")
    def test_qbittorrent_add_url_magnet(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        mock_session.post.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        magnet_link = (
            "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"
            "&dn=Sample%20Gallery.zip&tr=http%3A%2F%2Ftracker"
        )
        success, info_hash = client.add_url(magnet_link, download_dir="/downloads/200")
        self.assertTrue(success)
        self.assertEqual(info_hash, "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual(client.expected_torrent_name, "Sample Gallery")
        self.assertEqual(client.expected_torrent_extension, ".zip")

        mock_session.post.assert_called_once_with(
            "http://localhost:8080/api/v2/torrents/add",
            data={"urls": magnet_link, "savepath": "/downloads/200"},
            timeout=25,
        )

    @patch("requests.Session")
    def test_qbittorrent_get_download_progress(self, mock_session_cls):
        from core.downloaders.torrent.qbittorrent import qBittorrent

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"hash": "0123456789abcdef0123456789abcdef01234567", "progress": 0.852},
            {"hash": "fedcba9876543210fedcba9876543210fedcba98", "progress": 1.0},
        ]
        mock_session.get.return_value = mock_resp

        client = qBittorrent(address="http://localhost:8080")
        client.session = mock_session
        client.base_url = "http://localhost:8080"

        key1 = MagicMock()
        key2 = MagicMock()
        results = client.get_download_progress([
            ("0123456789abcdef0123456789abcdef01234567", key1),
            ("fedcba9876543210fedcba9876543210fedcba98", key2),
            ("invalid_hash", MagicMock()),
            ("", MagicMock()),
        ])

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], (key1, 85.2))
        self.assertEqual(results[1], (key2, 100.0))

        mock_session.get.assert_called_once()
        call_args, call_kwargs = mock_session.get.call_args
        self.assertEqual(call_args[0], "http://localhost:8080/api/v2/torrents/info")
        self.assertIn("hashes", call_kwargs["params"])
        self.assertIn("0123456789abcdef0123456789abcdef01234567", call_kwargs["params"]["hashes"])
        self.assertIn("fedcba9876543210fedcba9876543210fedcba98", call_kwargs["params"]["hashes"])

    def test_get_torrent_client_factory(self):
        from core.downloaders.torrent import get_torrent_client
        from core.downloaders.torrent.qbittorrent import qBittorrent
        from core.downloaders.torrent.transmission import Transmission

        qbt = get_torrent_client({"client": "qbittorrent", "address": "localhost", "port": 8080})
        self.assertIsInstance(qbt, qBittorrent)

        qbt_case = get_torrent_client({"client": "qBittorrent", "address": "localhost", "port": 8080})
        self.assertIsInstance(qbt_case, qBittorrent)

        trans = get_torrent_client({"client": "transmission", "address": "http://localhost", "port": 9091})
        self.assertIsInstance(trans, Transmission)

        # uTorrent is removed and must return None
        utorrent_client = get_torrent_client({"client": "utorrent", "address": "localhost", "port": 8080})
        self.assertIsNone(utorrent_client)

        unknown = get_torrent_client({"client": "unknown_client"})
        self.assertIsNone(unknown)

    @patch("zipfile.ZipFile")
    def test_archive_save_with_unsupported_zip_compression(self, mock_zipfile):
        mock_zip_instance = MagicMock()
        mock_zip_instance.testzip.side_effect = NotImplementedError("That compression method is not supported")
        mock_zipfile.return_value = mock_zip_instance

        with patch("os.path.isfile", return_value=True):
            archive = Archive.objects.create(
                title="unsupported zip archive",
                zipped="test.zip",
                crc32="12345678",
                user=None,
            )
            # save() should catch NotImplementedError without crashing
            with self.assertLogs("viewer.models", level="ERROR") as cm:
                archive.save()
            self.assertTrue(any("That compression method is not supported" in msg for msg in cm.output))

    @patch("core.downloaders.postdownload.ZipFile")
    def test_postdownload_process_archive_unsupported_compression(self, mock_zipfile):
        mock_zip_instance = MagicMock()
        mock_zip_instance.testzip.side_effect = NotImplementedError("That compression method is not supported")
        mock_zipfile.return_value = mock_zip_instance

        settings = Settings(load_from_disk=False)
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="888", provider="panda")
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="87654321",
            source_type="panda",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save") as mock_save:
            with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("NotImplementedError: That compression method is not supported" in msg for msg in cm.output))
            mock_save.assert_called_once()
            mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("time.sleep", return_value=None)
    @patch("requests.get")
    def test_request_with_retries_on_503_and_success(self, mock_get, mock_sleep):
        from core.base.utilities import request_with_retries

        resp_503 = MagicMock()
        resp_503.status_code = 503
        resp_200 = MagicMock()
        resp_200.status_code = 200

        mock_get.side_effect = [resp_503, resp_200]

        res = request_with_retries("https://example.com", {})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(mock_get.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    @patch("time.sleep", return_value=None)
    @patch("requests.post")
    def test_request_with_retries_on_continuous_504(self, mock_post, mock_sleep):
        from core.base.utilities import request_with_retries

        resp_504 = MagicMock()
        resp_504.status_code = 504

        mock_post.return_value = resp_504

        res = request_with_retries("https://example.com", {}, post=True, retries=3)
        self.assertEqual(res.status_code, 504)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep", return_value=None)
    def test_hath_downloader_retries_on_cache_error(self, mock_sleep):
        from core.providers.panda.downloaders import HathDownloader
        from core.providers.panda.settings import OwnSettings as PandaSettings
        from core.base.types import GalleryData

        settings = Settings(load_from_disk=False)
        settings.providers["panda"] = PandaSettings(settings, {})
        general_utils = MagicMock()
        downloader = HathDownloader(settings, general_utils)

        gallery = GalleryData("123456", "panda")
        gallery.root = "https://exhentai.org"
        gallery.token = "abcdef1234"
        gallery.title = "Test Gallery"
        downloader.gallery = gallery

        resp_cache_error = MagicMock()
        resp_cache_error.status_code = 200
        resp_cache_error.content = b"<html><body><p>Cache error</p></body></html>"
        resp_cache_error.text = "Cache error"

        resp_success = MagicMock()
        resp_success.status_code = 200
        resp_success.content = b"<html><body><div><p>An original resolution download has been queued for client <strong>12345</strong></p></div></body></html>"
        resp_success.text = "Queued"

        with patch.object(downloader, "request_hath_download", side_effect=[resp_cache_error, resp_success]) as mock_req, \
             patch("core.providers.panda.downloaders.available_filename", return_value="/tmp/test.zip"):
            downloader.start_download()

            self.assertEqual(downloader.return_code, 1)
            self.assertEqual(downloader.fileDownloaded, 1)
            self.assertEqual(mock_req.call_count, 2)
            mock_sleep.assert_called_once_with(2)


class RequeueFailedDownloadTest(TestCase):
    def test_settings_requeue_defaults(self):
        settings = Settings(load_from_disk=False)
        self.assertTrue(settings.requeue_failed_downloads)
        self.assertEqual(settings.failed_download_downloader, "panda_archive")
        self.assertEqual(settings.requeue_downloader, "panda_archive")

    def test_settings_requeue_from_config(self):
        config = {
            "general": {
                "requeue_failed_downloads": False,
                "failed_download_downloader": "panda_hath",
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertFalse(settings.requeue_failed_downloads)
        self.assertEqual(settings.failed_download_downloader, "panda_hath")
        self.assertEqual(settings.requeue_downloader, "panda_hath")

    def test_settings_requeue_from_allowed_and_aliases(self):
        config = {
            "allowed": {
                "requeue_failed": "no",
                "requeue_downloader": "custom_dl",
            },
            "downloaders": {},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        self.assertFalse(settings.requeue_failed_downloads)
        self.assertEqual(settings.failed_download_downloader, "custom_dl")
        self.assertEqual(settings.requeue_downloader, "custom_dl")

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_requeue_disabled(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.side_effect = NotImplementedError("Corrupt zip")
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.requeue_failed_downloads = False
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="111", provider="panda")
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="11111111",
            source_type="panda",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("Check the file manually" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_requeue_custom_downloader(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.side_effect = NotImplementedError("Corrupt zip")
        mock_zipfile.return_value = mock_zip

        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive_then_hath",
            },
            "downloaders": {
                "panda_archive": 4,
                "panda_torrent": 3,
                "chaika_archive": 5,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="222", provider="panda")
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="22222222",
            source_type="panda",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"):
            with self.assertLogs("core.downloaders.postdownload", level="ERROR") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("forcing download as panda_archive_then_hath to fix it" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_called_once()
            call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
            override_options = call_kwargs.get("override_options")
            self.assertIsNotNone(override_options)
            self.assertEqual(override_options.downloaders["panda_archive"], 1)
            self.assertEqual(override_options.downloaders["panda_hath"], 2)
            self.assertEqual(override_options.downloaders.get("chaika_archive"), -1)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_provider_mismatch_no_requeue(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.side_effect = NotImplementedError("Corrupt zip")
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.requeue_failed_downloads = True
        settings.failed_download_downloader = "panda_archive"
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery chaika", gid="333", provider="chaika")
        archive = Archive.objects.create(
            title="chaika archive",
            zipped="chaika_dl.zip",
            crc32="33333333",
            source_type="chaika",
            gallery=gallery,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("Check the file manually" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_filesize_mismatch_custom_downloader(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive_then_hath",
            },
            "downloaders": {
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="444", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="44444444",
            source_type="panda",
            gallery=gallery,
            filesize=1000,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 1000
            with self.assertLogs("core.downloaders.postdownload", level="INFO") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("downloading again from panda_archive_then_hath" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_called_once()
            call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
            override_options = call_kwargs.get("override_options")
            self.assertIsNotNone(override_options)
            self.assertEqual(override_options.downloaders["panda_archive"], 1)
            self.assertEqual(override_options.downloaders["panda_hath"], 2)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_sha1_mismatch_custom_downloader(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        config = {
            "custom_downloaders": {
                "panda_archive_then_hath": [
                    {"panda_archive": 1},
                    {"panda_hath": 2},
                ]
            },
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive_then_hath",
            },
            "downloaders": {
                "panda_archive": 4,
            },
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="555", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="55555555",
            source_type="panda",
            match_type="torrent",
            gallery=gallery,
            filesize=2000,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("forcing download as panda_archive_then_hath to fix it" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_called_once()
            call_args, call_kwargs = mock_web_queue.enqueue_args_list.call_args
            override_options = call_kwargs.get("override_options")
            self.assertIsNotNone(override_options)
            self.assertEqual(override_options.downloaders["panda_archive"], 1)
            self.assertEqual(override_options.downloaders["panda_hath"], 2)

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_sha1_mismatch_requeue_disabled(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.requeue_failed_downloads = False
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda", gid="666", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda archive",
            zipped="panda_dl.zip",
            crc32="66666666",
            source_type="panda",
            match_type="torrent",
            gallery=gallery,
            filesize=2000,
            user=None,
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                post_downloader.process_downloaded_archive(archive)

            self.assertTrue(any("Check the file manually" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_chaika_sha1_mismatch_with_links(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        config = {
            "general": {
                "requeue_failed_downloads": True,
                "failed_download_downloader": "panda_archive",
            },
            "downloaders": {"panda_archive": 1},
            "providers": {},
        }
        settings = Settings(load_from_config=config)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings
        chaika_settings = MagicMock()
        chaika_settings.url = "https://panda.chaika.moe/"
        settings.providers["chaika"] = chaika_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda chaika", gid="777", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda chaika archive",
            zipped="chaika_dl.zip",
            crc32="77777777",
            source_type="panda",
            match_type="chaika",
            gallery=gallery,
            filesize=2000,
            user=None,
        )
        DownloadEvent.objects.create(
            name=gallery.get_link(),
            method="chaika",
            download_id="chaika_dl.zip",
            archive=archive,
            gallery=gallery,
            download_url="https://panda.chaika.moe/archive/42/download/",
        )

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                result = post_downloader.process_downloaded_archive(archive)

            self.assertFalse(result)
            self.assertTrue(any("forcing download as panda_archive to fix it" in msg for msg in cm.output))
            self.assertTrue(
                any(
                    "SHA1 mismatch detected for Chaika download" in msg
                    and "https://panda.chaika.moe/archive/42/" in msg
                    and gallery.get_link() in msg
                    for msg in cm.output
                )
            )
            mock_web_queue.enqueue_args_list.assert_called_once()

    @patch("core.downloaders.postdownload.ZipFile")
    def test_process_downloaded_archive_chaika_sha1_match(self, mock_zipfile):
        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_match"]
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        post_downloader = PostDownloader(settings=settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery panda chaika match", gid="888", provider="panda", filesize=2000)
        archive = Archive.objects.create(
            title="panda chaika archive match",
            zipped="chaika_dl.zip",
            crc32="88888888",
            source_type="panda",
            match_type="chaika",
            gallery=gallery,
            filesize=2000,
            user=None,
        )
        Image.objects.create(archive=archive, position=1, archive_position=1, sha1="hash_match")

        with patch("os.path.isfile", return_value=True), \
             patch.object(archive, "save"), \
             patch.object(archive, "recalc_fileinfo"):
            archive.filesize = 2000
            with self.assertLogs("core.downloaders.postdownload", level="INFO") as cm:
                result = post_downloader.process_downloaded_archive(archive)

            self.assertTrue(result)
            self.assertTrue(any("validation passes" in msg for msg in cm.output))
            mock_web_queue.enqueue_args_list.assert_not_called()

    @patch("core.providers.panda.downloaders.request_by_provider")
    @patch("core.downloaders.postdownload.ZipFile")
    def test_panda_chaika_downloader_direct_sha1_check_mismatch(self, mock_zipfile, mock_req_by_prov):
        from core.base.setup_utilities import GeneralUtils
        from core.providers.panda.downloaders import PandaChaikaDownloader

        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        settings.queue_archive_downloads = False
        settings.parallel_archive_downloaders = 0
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        panda_settings.queue_archive_downloads = False
        settings.providers["panda"] = panda_settings
        chaika_settings = MagicMock()
        chaika_settings.queue_archive_downloads = False
        chaika_settings.url = "https://panda.chaika.moe/"
        chaika_settings.archive_dl_folder = "archives/chaika"
        chaika_settings.wait_timer = 0.01
        chaika_settings.headers = {}
        chaika_settings.cookies = {}
        chaika_settings.proxy = None
        settings.providers["chaika"] = chaika_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_api_resp = MagicMock()
        mock_api_resp.status_code = 200
        mock_api_resp.json.return_value = {
            "gid": "999",
            "archives": [{"id": 99, "download": "/archive/99/download/"}],
        }
        mock_file_resp = MagicMock()
        mock_file_resp.status_code = 200
        mock_file_resp.headers = {"Content-Length": "100"}
        mock_file_resp.iter_content.return_value = [b"dummycontent"]

        def req_side_effect(name, calls, period):
            def req_call(url, req_dict, post=False):
                if "api/" in url:
                    return mock_api_resp
                return mock_file_resp
            return req_call

        mock_req_by_prov.side_effect = req_side_effect

        from django.contrib.auth import get_user_model
        get_user_model().objects.get_or_create(id=1, defaults={"username": "test_user"})

        mock_web_queue = MagicMock()
        settings.workers.web_queue = mock_web_queue

        Gallery.objects.create(gid="999", title="Test Gallery 999", provider="panda", filesize=100)
        gallery_data = GalleryData(gid="999", title="Test Gallery 999", provider="panda")
        gallery_data.link = "https://exhentai.org/g/999/abcdef/"
        gallery_data.filesize = 100
        gallery_data.filecount = 1

        downloader = PandaChaikaDownloader(settings, GeneralUtils(settings))

        with patch("builtins.open", MagicMock()), \
             patch("core.providers.panda.downloaders.available_filename", return_value="archives/chaika/test999.zip"), \
             patch("os.path.isfile", return_value=True), \
             patch.object(Archive, "save", lambda self, *a, **kw: self.simple_save(*a, **kw)), \
             patch("core.providers.panda.downloaders.get_zip_fileinfo_for_gallery", return_value=(100, 1)), \
             patch("core.providers.panda.downloaders.calc_crc32", return_value="99999999"), \
             patch.object(Archive, "recalc_fileinfo"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                downloader.init_download(gallery_data)

            self.assertEqual(downloader.return_code, 0)
            self.assertTrue(any("SHA1 mismatch detected for Chaika download" in msg for msg in cm.output))

    @patch("core.workers.archive_download_queue.request_by_provider")
    @patch("core.downloaders.postdownload.ZipFile")
    def test_archive_download_queue_chaika_sha1_check_mismatch(self, mock_zipfile, mock_req_prov):
        from core.workers.archive_download_queue import ArchiveDownloadQueue

        mock_zip = MagicMock()
        mock_zip.testzip.return_value = None
        mock_zipfile.return_value = mock_zip

        settings = Settings(load_from_disk=False)
        panda_settings = MagicMock()
        panda_settings.confirm_files_after_download = True
        settings.providers["panda"] = panda_settings
        chaika_settings = MagicMock()
        chaika_settings.url = "https://panda.chaika.moe/"
        settings.providers["chaika"] = chaika_settings

        mock_parser = MagicMock(spec=PandaParser)
        mock_parser.get_sha1_hashes_from_panda.return_value = ["hash_panda"]
        settings.provider_context.get_parsers = MagicMock(return_value=[mock_parser])

        mock_web_queue = MagicMock()
        queue_worker = ArchiveDownloadQueue(settings, web_queue=mock_web_queue)

        gallery = Gallery.objects.create(title="gallery q", gid="1010", provider="panda", filesize=50)
        archive = Archive.objects.create(
            title="archive q",
            zipped="q.zip",
            crc32="10101010",
            source_type="panda",
            match_type="chaika",
            gallery=gallery,
            filesize=50,
            user=None,
        )
        event = DownloadEvent.objects.create(
            name="event_chaika",
            method="chaika",
            download_id="/fake/q.zip",
            provider="chaika",
            download_url="https://panda.chaika.moe/archive/55/download/",
            archive=archive,
            gallery=gallery,
            total_size=50,
            status=DownloadEvent.Status.IN_PROGRESS,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Length": "50"}
        mock_resp.iter_content.return_value = [b"dummy"]
        mock_req_prov.return_value = lambda url, d: mock_resp

        with patch("builtins.open", MagicMock()), \
             patch("os.makedirs"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.getsize", return_value=50), \
             patch("os.path.isfile", return_value=True), \
             patch.object(Archive, "save", lambda self, *a, **kw: self.simple_save(*a, **kw)), \
             patch("os.remove"), \
             patch("os.replace"), \
             patch.object(Archive, "recalc_fileinfo"):
            with self.assertLogs("core.downloaders.postdownload", level="WARNING") as cm:
                queue_worker._download_worker(event.id)

            event.refresh_from_db()
            self.assertTrue(event.failed)
            self.assertTrue(any("SHA1 mismatch detected for Chaika download" in msg for msg in cm.output))

    def test_gallery_dl_downloader_stderr_and_cleanup(self):
        from core.base.setup_utilities import GeneralUtils
        from core.providers.generic.downloaders import GenericGalleryDLDownloader

        settings = Settings(load_from_disk=True)
        gen_utils = GeneralUtils(settings)
        downloader = GenericGalleryDLDownloader(settings, gen_utils)
        downloader.gallery = GalleryData(
            gid="123",
            provider="generic",
            link="https://example.com/gallery/123",
            title="Sample Generic Gallery",
        )

        captured_dir = []

        def mock_subprocess_run(cmd, **kwargs):
            dest_idx = cmd.index("--dest") + 1
            dest_dir = cmd[dest_idx]
            captured_dir.append(dest_dir)
            mock_zip = os.path.join(dest_dir, "test_gallery.zip")
            with open(mock_zip, "w") as f:
                f.write("mock zip content")
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="Downloaded 1 item",
                stderr="WARNING: non-fatal warning occurred",
            )

        with patch("shutil.which", return_value="/usr/bin/gallery-dl"), \
             patch("subprocess.run", side_effect=mock_subprocess_run), \
             patch("shutil.move"), \
             patch("core.downloaders.handlers.get_zip_fileinfo_for_gallery", return_value=(100, 5)), \
             patch("core.downloaders.handlers.calc_crc32", return_value="1234abcd"):
            downloader.start_download()

        # Non-fatal stderr warning should NOT cause download failure
        self.assertEqual(downloader.return_code, 1)
        self.assertEqual(downloader.fileDownloaded, 1)
        # Temporary directory must be cleaned up
        self.assertTrue(len(captured_dir) > 0)
        self.assertFalse(os.path.exists(captured_dir[0]))

        # Failure with non-zero returncode should clean up directory as well
        failed_dir = []

        def mock_subprocess_failed(cmd, **kwargs):
            dest_idx = cmd.index("--dest") + 1
            dest_dir = cmd[dest_idx]
            failed_dir.append(dest_dir)
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="ERROR: Failed to download",
            )

        downloader.return_code = 0
        downloader.fileDownloaded = 0
        with patch("shutil.which", return_value="/usr/bin/gallery-dl"), \
             patch("subprocess.run", side_effect=mock_subprocess_failed):
            downloader.start_download()

        self.assertEqual(downloader.return_code, 0)
        self.assertEqual(downloader.fileDownloaded, 0)
        self.assertTrue(len(failed_dir) > 0)
        self.assertFalse(os.path.exists(failed_dir[0]))

    def test_autoupdate_provider_name_formatting(self):
        from core.workers.autoupdate import ProviderTimedAutoUpdater

        settings = Settings(load_from_disk=True)
        updater = ProviderTimedAutoUpdater(settings, provider_name="panda")
        start_date = django_tz.now()
        end_date = django_tz.now()
        msg = "No galleries posted from {} to {} need updating. Provider: {}".format(
            start_date, end_date, updater.provider_name
        )
        self.assertIn("Provider: panda", msg)
        self.assertNotIn("p, a, n, d, a", msg)

    def test_settings_load_elasticsearch_from_environment(self):
        settings = Settings(load_from_disk=False)
        self.assertFalse(settings.elasticsearch.enable)

        env = {
            "ELASTICSEARCH_ENABLE": "true",
            "ELASTICSEARCH_ENABLE_MATCH": "1",
            "ELASTICSEARCH_URL": "http://elasticsearch:9200/",
            "ELASTICSEARCH_INDEX_NAME": "custom_viewer",
            "ELASTICSEARCH_GALLERY_INDEX_NAME": "custom_gallery",
            "ELASTICSEARCH_TIMEOUT": "45",
        }
        with patch.dict(os.environ, env):
            settings.load_from_environment()

        self.assertTrue(settings.elasticsearch.enable)
        self.assertTrue(settings.elasticsearch.enable_match)
        self.assertEqual(settings.elasticsearch.url, "http://elasticsearch:9200/")
        self.assertEqual(settings.elasticsearch.index_name, "custom_viewer")
        self.assertEqual(settings.elasticsearch.gallery_index_name, "custom_gallery")
        self.assertEqual(settings.elasticsearch.timeout, 45)

    def test_settings_load_elasticsearch_empty_environment_ignored(self):
        settings = Settings(load_from_disk=False)
        settings.elasticsearch.enable = False
        settings.elasticsearch.url = "http://localhost:9200/"

        env = {
            "ELASTICSEARCH_ENABLE": "",
            "ELASTICSEARCH_URL": "",
        }
        with patch.dict(os.environ, env):
            settings.load_from_environment()

        self.assertFalse(settings.elasticsearch.enable)
        self.assertEqual(settings.elasticsearch.url, "http://localhost:9200/")








