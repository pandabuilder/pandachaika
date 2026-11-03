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


