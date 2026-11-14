import copy

from django.test import SimpleTestCase
from pydantic import ValidationError

from core.base.config_models import DatabaseSettings, TorrentSettings
from core.base.setup import Settings
from workers.serialization import deserialize_settings_options, serialize_settings_options


class SettingsDelegationTest(SimpleTestCase):
    """Top-level Settings attributes are stored on the config section models, not copied from them."""

    def test_attributes_read_and_write_the_section_models(self):
        settings = Settings(load_from_config={"general": {"wait_timer": 2}, "allowed": {"replace_metadata": "yes"}})
        self.assertEqual(settings.wait_timer, 2.0)
        self.assertTrue(settings.replace_metadata)

        settings.wait_timer = 7
        settings.replace_metadata = False
        settings.MEDIA_ROOT = "/media/root"
        settings.log_level = "DEBUG"
        self.assertEqual(settings.general_model.wait_timer, 7.0)
        self.assertFalse(settings.allowed_model.replace_metadata)
        self.assertEqual(settings.locations_model.media_root, "/media/root")
        self.assertEqual(settings.general_model.force_log_level, "DEBUG")
        self.assertNotIn("wait_timer", vars(settings))

    def test_assignment_is_validated(self):
        settings = Settings()
        settings.retry_failed = "yes"  # type: ignore[assignment]
        self.assertIs(settings.retry_failed, True)
        with self.assertRaises(ValidationError):
            settings.timeout_timer = "not a number"  # type: ignore[assignment]

    def test_requeue_downloader_is_an_alias(self):
        settings = Settings()
        settings.requeue_downloader = "panda_torrent"
        self.assertEqual(settings.failed_download_downloader, "panda_torrent")

    def test_non_current_links_as_deleted_precedence(self):
        only_general = Settings(load_from_config={"general": {"non_current_links_as_deleted": True}})
        self.assertTrue(only_general.non_current_links_as_deleted)

        both = Settings(
            load_from_config={
                "general": {"non_current_links_as_deleted": True},
                "allowed": {"non_current_links_as_deleted": False},
            }
        )
        self.assertFalse(both.non_current_links_as_deleted)

    def test_requeue_precedence(self):
        allowed_only = Settings(load_from_config={"allowed": {"requeue_failed": "no", "requeue_downloader": "x_dl"}})
        self.assertFalse(allowed_only.requeue_failed_downloads)
        self.assertEqual(allowed_only.failed_download_downloader, "x_dl")

        # 'general' wins over 'allowed' when present, even without the key.
        with_general = Settings(load_from_config={"allowed": {"requeue_failed": "no"}, "general": {"wait_timer": 3}})
        self.assertTrue(with_general.requeue_failed_downloads)

        top_level = Settings(
            load_from_config={
                "general": {"requeue_failed_downloads": True},
                "requeue_failed": "no",
                "failed_download_downloader": "a_dl",
            }
        )
        self.assertFalse(top_level.requeue_failed_downloads)
        self.assertEqual(top_level.requeue_downloader, "a_dl")

    def test_archive_download_queue_section_overrides_general(self):
        settings = Settings(
            load_from_config={
                "general": {"parallel_archive_downloaders": 3},
                "archive_download_queue": {"enable": 0, "cycle_timer": "5", "parallel_downloaders": "7"},
            }
        )
        self.assertFalse(settings.queue_archive_downloads)
        self.assertEqual(settings.archive_download_queue_cycle_timer, 5.0)
        self.assertEqual(settings.parallel_archive_downloaders, 7)

    def test_log_location_defaults_to_config_dir(self):
        settings = Settings()
        settings.default_dir = "/config"
        settings.dict_to_settings({"locations": {"log_location": ""}})
        self.assertEqual(settings.log_location, "/config/viewer.log")
        settings.dict_to_settings({"locations": {"log_location": "/logs/panda.log"}})
        self.assertEqual(settings.log_location, "/logs/panda.log")

    def test_banned_lists_default_to_empty(self):
        settings = Settings(load_from_config={"general": {"wait_timer": 1}})
        self.assertEqual(settings.banned_tags, [])
        self.assertEqual(settings.banned_uploaders, [])
        self.assertEqual(settings.banned_search_queries, [])
        self.assertEqual(Settings(load_from_config={"general": {"discard_tags": ["a:b"]}}).banned_tags, ["a:b"])

    def test_sections_are_models_with_dict_access(self):
        settings = Settings(load_from_config={"torrent": {"pass": "pw", "download_dir": "/dl"}, "database": {"postgresql_port": 5433}})
        self.assertIsInstance(settings.torrent, TorrentSettings)
        self.assertEqual(settings.torrent["pass"], "pw")
        self.assertEqual(settings.torrent.get("pass"), "pw")
        self.assertIn("pass", settings.torrent)
        self.assertEqual(settings.torrent["download_dir"], "/dl")

        self.assertIsInstance(settings.database, DatabaseSettings)
        settings.database["postgresql_port"] = 6000
        self.assertEqual(settings.database.postgresql_port, "6000")

    def test_default_ftp_configs_do_not_leak_into_config(self):
        config = {"general": {"download_ftp_torrent": "box"}, "ftps": {"user": "u"}, "ftp_configs": {"other": {"address": "h"}}}
        settings = Settings(load_from_config=config)
        self.assertEqual(settings.ftp_configs["box"]["user"], "u")
        self.assertEqual(settings.ftp_configs["other"], {"address": "h"})
        self.assertEqual(set(settings.config["ftp_configs"]), {"other"})

    def test_deepcopy_does_not_share_models(self):
        base = Settings(load_from_config={"allowed": {"replace_metadata": "no"}})
        clone = copy.deepcopy(base)
        clone.replace_metadata = True
        clone.banned_tags.append("x:y")
        self.assertFalse(base.replace_metadata)
        self.assertEqual(base.banned_tags, [])


class SettingsSerializationTest(SimpleTestCase):
    def test_model_backed_attributes_round_trip(self):
        base = Settings(load_from_config={"general": {"wait_timer": 6, "django_secret_key": "secret"}})
        task_settings = copy.deepcopy(base)
        task_settings.replace_metadata = True
        task_settings.wait_timer = 1.5
        task_settings.banned_tags = ["tag:one"]

        data = serialize_settings_options(task_settings)
        self.assertTrue(data["replace_metadata"])
        self.assertEqual(data["wait_timer"], 1.5)
        self.assertNotIn("general_model", data)
        self.assertNotIn("allowed_model", data)
        self.assertNotIn("django_secret_key", data)
        self.assertNotIn("secret", repr(data))

        hydrated = deserialize_settings_options(base, data)
        self.assertTrue(hydrated.replace_metadata)
        self.assertEqual(hydrated.wait_timer, 1.5)
        self.assertEqual(hydrated.banned_tags, ["tag:one"])
        self.assertEqual(hydrated.django_secret_key, "secret")
        self.assertFalse(base.replace_metadata)

    def test_legacy_section_model_keys_are_ignored(self):
        """Payloads queued by older versions also carry the section models; the top-level keys win."""
        base = Settings(load_from_config={"allowed": {"replace_metadata": "no"}})
        legacy_payload = {
            "replace_metadata": True,
            "allowed_model": {"replace_metadata": False},
            "database_model": {"postgresql_password": "x"},
        }
        hydrated = deserialize_settings_options(base, legacy_payload)
        self.assertTrue(hydrated.replace_metadata)
        self.assertNotIn("database_model", vars(hydrated))
