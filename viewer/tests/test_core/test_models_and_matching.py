from unittest.mock import patch

from django.test import TestCase

from core.base.comparison import get_list_closer_text_from_list
from core.base.setup import Settings
from viewer.models import Archive, Gallery, Image




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

    def test_type_safe_config_sections(self):
        from core.base.setup import (
            AutoWantedSettings,
            ElasticSearchSettings,
            MailSettings,
            PushoverSettings,
            UrlSettings,
        )

        # Test ElasticSearchSettings
        es = ElasticSearchSettings.from_dict({
            "enable": "true",
            "url": "http://elastic.local:9200",
            "timeout": "45",
            "max_result_window": 50000,
        })
        self.assertTrue(es.enable)
        self.assertEqual(es.url, "http://elastic.local:9200")
        self.assertEqual(es.timeout, 45)
        self.assertEqual(es.max_result_window, 50000)
        es_dict = es.to_dict()
        self.assertEqual(es_dict["url"], "http://elastic.local:9200")
        self.assertEqual(es_dict["timeout"], 45)

        # Test MailSettings with legacy "from" alias and credentials
        mail = MailSettings.from_dict({
            "enable": True,
            "mailhost": "smtp.example.com",
            "from": "bot@example.com",
            "to": "admin@example.com",
            "username": "user",
            "password": "secret_password",
        })
        self.assertTrue(mail.enable)
        self.assertEqual(mail.from_, "bot@example.com")
        self.assertEqual(mail.credentials, ("user", "secret_password"))

        # Test UrlSettings
        urls = UrlSettings.from_dict({
            "behind_proxy": "1",
            "cors_allow_all_origins": "yes",
            "cors_allowed_origins": ["https://example.com"],
        })
        self.assertTrue(urls.behind_proxy)
        self.assertTrue(urls.cors_allow_all_origins)
        self.assertEqual(urls.cors_allowed_origins, ["https://example.com"])

        # Test AutoWantedSettings
        auto_wanted = AutoWantedSettings.from_dict({
            "enable": True,
            "cycle_timer": "12.5",
            "providers": ["panda", "ehentai"],
            "regexp_unwanted_title": True,
        })
        self.assertTrue(auto_wanted.enable)
        self.assertEqual(auto_wanted.cycle_timer, 12.5)
        self.assertEqual(auto_wanted.providers, ["panda", "ehentai"])
        self.assertTrue(auto_wanted.regexp_unwanted_title)

        # Test PushoverSettings
        pushover = PushoverSettings.from_dict({
            "enable": True,
            "user_key": "ukey123",
            "token": "tok456",
        })
        self.assertTrue(pushover.enable)
        self.assertEqual(pushover.user_key, "ukey123")
        self.assertEqual(pushover.token, "tok456")

    def test_pydantic_configuration_models_and_schema_generation(self):
        import io
        import yaml
        from django.core.management import call_command
        from core.base.setup import Settings
        from core.base.config_models import (
            PandaGalleryConfig,
            get_config_schema,
            generate_default_yaml_content,
        )

        # 1. Test model instantiation and defaults
        config_model = PandaGalleryConfig()
        self.assertEqual(config_model.general.db_engine, "postgresql")
        self.assertTrue(config_model.allowed.convert_others_to_zip)
        self.assertEqual(config_model.torrent.client, "transmission")
        self.assertEqual(config_model.database.postgresql_port, "5432")
        self.assertEqual(config_model.locations.media_root, "")
        self.assertEqual(config_model.locations.static_root, "")

        # 2. Test schema generation
        schema = get_config_schema()
        self.assertIn("properties", schema)
        self.assertIn("general", schema["properties"])
        self.assertIn("providers", schema["properties"])
        self.assertIn("database", schema["properties"])
        self.assertIn("torrent", schema["properties"])

        # 3. Test generate_default_yaml_content parses back into PandaGalleryConfig
        yaml_content = generate_default_yaml_content()
        parsed_data = yaml.safe_load(yaml_content)
        validated_config = PandaGalleryConfig.model_validate(parsed_data)
        self.assertEqual(validated_config.general.db_engine, "postgresql")
        self.assertFalse(validated_config.general.stop_on_seen_links)
        self.assertEqual(validated_config.providers.panda.general.stop_page_number, 5)
        self.assertFalse(validated_config.providers.panda.general.stop_on_seen_links)
        self.assertEqual(validated_config.providers.chaika.general.url, "https://panda.chaika.moe")

        # 4. Test Settings integration with Pydantic
        runtime_settings = Settings(load_from_config=parsed_data)
        self.assertEqual(runtime_settings.db_engine, "postgresql")
        self.assertEqual(runtime_settings.general_model.db_engine, "postgresql")
        self.assertIsInstance(runtime_settings.database, dict)
        self.assertIsInstance(runtime_settings.torrent, dict)
        pydantic_export = runtime_settings.to_pydantic()
        self.assertIsInstance(pydantic_export, PandaGalleryConfig)
        self.assertEqual(pydantic_export.general.db_engine, "postgresql")

        # 5. Test config_schema management command
        out = io.StringIO()
        call_command("config_schema", "--json", stdout=out)
        json_output = out.getvalue()
        self.assertIn("PandaGalleryConfig", json_output)
        self.assertIn('"db_engine"', json_output)

        out_yaml = io.StringIO()
        call_command("config_schema", "--yaml", stdout=out_yaml)
        yaml_output = out_yaml.getvalue()
        self.assertIn("Panda Backup", yaml_output)
        self.assertIn("db_engine: postgresql", yaml_output)

        out_val = io.StringIO()
        call_command("config_schema", "--validate", "default.yaml", stdout=out_val)
        self.assertIn("is valid", out_val.getvalue())

    def test_gunicorn_application_and_static_handler(self):
        from server import MediaAndStaticFilesHandler, GunicornApplication

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

    def test_api_url_resolution(self):
        from django.urls import resolve
        self.assertEqual(resolve("/api/").view_name, "viewer:api")
        self.assertEqual(resolve("/api").view_name, "viewer:api-slashless")
        self.assertEqual(resolve("/jsearch/").view_name, "viewer:json-search")
        self.assertEqual(resolve("/jsearch").view_name, "viewer:json-search-slashless")

    def test_panda_string_filecount_and_similarity_marks(self):
        from django.contrib.contenttypes.models import ContentType
        from core.providers.panda.utilities import map_external_gallery_data_to_internal
        from viewer.models import ArchiveManageEntry, ItemProperties

        raw_api_payload = {
            "gid": 1710804,
            "token": "eb02584b59",
            "title": "Sample Title",
            "title_jpn": "",
            "thumb": "https://ehgt.org/t/00/00/00.jpg",
            "category": "Doujinshi",
            "uploader": "tester",
            "posted": "1597722247",
            "filecount": "25",
            "filesize": 31745097,
            "expunged": False,
            "rating": "4.50",
            "tags": ["language:english"],
            "torrents": [],
            "torrentcount": "0",
        }
        mapped = map_external_gallery_data_to_internal(raw_api_payload)
        self.assertIsInstance(mapped.filecount, int)
        self.assertEqual(mapped.filecount, 25)
        self.assertIsInstance(mapped.filesize, int)
        self.assertEqual(mapped.filesize, 31745097)

        image_ct = ContentType.objects.get_for_model(Image)
        other_archive = Archive.objects.create(
            title="Existing Similar Archive",
            zipped="galleries/torrent_dls/existing.zip",
            filecount=25,
            filesize=31745097,
            user=None,
        )
        other_img = Image.objects.create(archive=other_archive, position=1, archive_position=1, sha1="abc123sha1")
        ItemProperties.objects.create(
            content_type=image_ct,
            object_id=other_img.pk,
            tag="hash-compare",
            name="phash",
            value="123456789abcdef0",
        )

        target_archive = Archive.objects.create(
            title="Target Archive",
            zipped="galleries/torrent_dls/target.zip",
            filecount=25,
            filesize=31745097,
            user=None,
        )
        target_img = Image.objects.create(archive=target_archive, position=1, archive_position=1, sha1="abc123sha1")
        ItemProperties.objects.create(
            content_type=image_ct,
            object_id=target_img.pk,
            tag="hash-compare",
            name="phash",
            value="123456789abcdef0",
        )

        # Simulate update_or_create_by_values_and_gid or string filecount in memory
        updated_archive = Archive.objects.update_or_create_by_values_and_gid(
            {
                "title": mapped.title,
                "title_jpn": mapped.title_jpn,
                "zipped": "galleries/torrent_dls/target.zip",
                "crc32": "",
                "filesize": mapped.filesize,
                "filecount": mapped.filecount,
                "user": None,
            },
            (mapped.gid, mapped.provider),
            zipped="galleries/torrent_dls/target.zip",
        )
        self.assertIsInstance(updated_archive.filecount, int)
        updated_archive.create_marks_for_similar_archives()

        # Also verify defensive handling if filecount is still a string on the model instance
        updated_archive.filecount = "25"  # type: ignore[assignment]
        updated_archive.create_marks_for_similar_archives()

        phash_mark = ArchiveManageEntry.objects.filter(
            archive=updated_archive, mark_reason="images_phash_similarity"
        ).first()
        self.assertIsNotNone(phash_mark)
        self.assertAlmostEqual(phash_mark.mark_priority, (1 / 25) * 3.0)
