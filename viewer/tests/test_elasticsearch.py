import logging
import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

import elasticsearch
from django.conf import settings as django_settings
from django.test import SimpleTestCase, TestCase, override_settings

from core.base.setup import Settings
from core.base.types import GalleryData
from core.providers.panda.parsers import Parser as PandaParser
from viewer.models import Archive, Gallery, WantedGallery, Tag
from viewer.utils import elasticsearch as es_utils


def es_match_available() -> bool:
    if not getattr(django_settings, "ES_MATCH_ENABLED", False):
        return False
    client = getattr(django_settings, "ES_CLIENT", None)
    if not client:
        return False
    try:
        return bool(client.ping())
    except Exception:
        return False


@unittest.skipUnless(es_match_available(), "Elasticsearch match service is not available")
class WantedGalleryElasticSearchTest(TestCase):
    def setUp(self):
        # Tags
        english_tag = Tag.objects.create(scope="language", name="english")
        artist_tag = Tag.objects.create(scope="artist", name="suzunomoku")
        artist2_tag = Tag.objects.create(scope="artist", name="mitsuya")

        # Galleries
        self.test_gallery1 = Gallery.objects.create(title="sample non public gallery 1", gid="344", provider="panda")
        self.test_gallery2 = Gallery.objects.create(title="sample non public gallery 2", gid="342", provider="test")

        # WantedGalleries
        self.test_wanted_gallery1 = WantedGallery.objects.create(
            title="test wanted gallery",
            book_type="Manga",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            match_expression='tags.full:"artist:suzunomoku"'
        )

        self.test_wanted_gallery3 = WantedGallery.objects.create(
            title="test wanted gallery 3",
            publisher="wanimagazine",
            reason="wanimagazine",
            should_search=True,
            keep_searching=True,
            notify_when_found=False,
            found=True,
            match_expression='provider:panda AND tags.full:"artist:suzunomoku"'
        )

        self.test_gallery4 = Gallery.objects.create(
            title="New Release 4", gid="34665", provider="panda", category="Manga", token="4324239"
        )
        self.test_gallery4.tags.set([english_tag, artist_tag, artist2_tag])

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

        # 5 viewer related queries, plus 1 StatusLog query when db_log records INFO
        raw_db_level = str(getattr(settings, "sql_log_level", "INFO") or "INFO").upper()
        db_level_num = logging.getLevelName(raw_db_level)
        logs_info_to_db = (
            not settings.disable_sql_log
            and isinstance(db_level_num, int)
            and db_level_num <= logging.INFO
        )
        expected_queries = 6 if logs_info_to_db else 5
        with self.assertNumQueries(expected_queries):
            parser.compare_gallery_with_wanted_filters(
                incoming_gallery, gallery_link, wanted_galleries, gallery_wanted_lists
            )

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery.gid]), 2)
        self.assertEqual(WantedGallery.objects.filter(found=True).count(), 2)

        incoming_gallery1 = self.test_gallery1.as_gallery_data()
        incoming_gallery4 = self.test_gallery4.as_gallery_data()

        gallery_wanted_lists = defaultdict(list)

        # 2 are not already found, remaining it is.
        parser.compare_gallery_with_wanted_filters(
            incoming_gallery1, incoming_gallery1.link, wanted_galleries, gallery_wanted_lists
        )

        parser.compare_gallery_with_wanted_filters(
            incoming_gallery4, incoming_gallery4.link, wanted_galleries, gallery_wanted_lists
        )

        self.assertEqual(len(gallery_wanted_lists[incoming_gallery1.gid]), 0)
        self.assertEqual(len(gallery_wanted_lists[incoming_gallery4.gid]), 2)

        rematch_result_1 = self.test_gallery1.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)
        rematch_result_4 = self.test_gallery4.match_against_wanted_galleries(wanted_galleries, skip_already_found=False)

        # Make sure compare_gallery_with_wanted_filters gives the same result as match_against_wanted_galleries
        self.assertEqual(rematch_result_1, gallery_wanted_lists[incoming_gallery1.gid])
        self.assertEqual(rematch_result_4, gallery_wanted_lists[incoming_gallery4.gid])


class RemoveGalleryFromMatchIndexTest(SimpleTestCase):
    @override_settings(ES_MATCH_ENABLED=True)
    def test_delete_uses_client_timeout_without_refresh(self) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True

        with patch.object(es_utils, "es_client", mock_client):
            result = es_utils.remove_gallery_from_match_index("doc-uuid-1")

        self.assertTrue(result)
        mock_client.options.assert_not_called()
        mock_client.delete.assert_called_once_with(
            index=es_utils.es_match_index_name,
            id="doc-uuid-1",
        )

    @override_settings(ES_MATCH_ENABLED=True)
    def test_not_found_returns_false(self) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete.side_effect = elasticsearch.exceptions.NotFoundError(
            "not found", MagicMock(), {}
        )

        with patch.object(es_utils, "es_client", mock_client):
            result = es_utils.remove_gallery_from_match_index("doc-uuid-2")

        self.assertFalse(result)

    @override_settings(ES_MATCH_ENABLED=True)
    def test_connection_timeout_on_delete_logs_warning_and_returns_false(self) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True
        mock_client.delete.side_effect = elasticsearch.exceptions.ConnectionTimeout("Connection timed out")

        with (
            patch.object(es_utils, "es_client", mock_client),
            self.assertLogs(es_utils.logger, level="WARNING") as logs,
        ):
            result = es_utils.remove_gallery_from_match_index("doc-uuid-3")

        self.assertFalse(result)
        self.assertTrue(any("doc-uuid-3" in msg for msg in logs.output))

    @override_settings(ES_MATCH_ENABLED=True)
    def test_connection_timeout_on_exists_logs_warning_and_returns_false(self) -> None:
        mock_client = MagicMock()
        mock_client.indices.exists.side_effect = elasticsearch.exceptions.ConnectionTimeout("Connection timed out")

        with (
            patch.object(es_utils, "es_client", mock_client),
            self.assertLogs(es_utils.logger, level="WARNING") as logs,
        ):
            result = es_utils.remove_gallery_from_match_index("doc-uuid-4")

        self.assertFalse(result)
        mock_client.delete.assert_not_called()
        self.assertTrue(any("doc-uuid-4" in msg for msg in logs.output))


class ElasticSearchTimeoutInheritanceTest(SimpleTestCase):
    def test_archive_delete_uses_client_timeout(self) -> None:
        archive = Archive(pk=42, title="ES Delete Archive", thumbnail="")
        mock_client = MagicMock()

        with (
            override_settings(ES_CLIENT=mock_client, ES_AUTOREFRESH=True),
            patch("django.db.models.Model.delete", return_value=(1, {})),
            patch("viewer.models.archive.transaction.on_commit", side_effect=lambda fn: fn()),
        ):
            archive.delete()

        mock_client.options.assert_not_called()
        mock_client.delete.assert_called_once_with(
            index=getattr(Archive._meta, "es_index_name"),
            id="42",
            refresh=True,
        )

    def test_gallery_delete_uses_client_timeout(self) -> None:
        gallery = Gallery(pk=99, title="ES Delete Gallery", gid="999001", provider="panda")
        mock_client = MagicMock()

        with (
            override_settings(ES_CLIENT=mock_client, ES_AUTOREFRESH_GALLERY=True),
            patch.object(Gallery, "remove_wanted_relations"),
            patch("django.db.models.Model.delete", return_value=(1, {})),
            patch("viewer.models.gallery.transaction.on_commit", side_effect=lambda fn: fn()),
        ):
            gallery.delete()

        mock_client.options.assert_not_called()
        mock_client.delete.assert_called_once_with(
            index=getattr(Gallery._meta, "es_index_name"),
            id="99",
            refresh=True,
        )

    def test_push_to_index_bulk_does_not_override_request_timeout(self) -> None:
        from importlib import import_module

        push_mod = import_module("viewer.management.commands.push-to-index")
        fake_gallery = MagicMock()
        fake_gallery.es_repr.return_value = {"_id": "1", "title": "ES Bulk Gallery"}
        fake_gallery._meta.es_index_name = "viewer_gallery"

        mock_qs = MagicMock()
        mock_qs.prefetch_related.return_value.order_by.return_value = [fake_gallery]
        mock_client = MagicMock()

        with (
            override_settings(ES_ONLY_INDEX_PUBLIC=False),
            patch("elasticsearch.Elasticsearch", return_value=mock_client),
            patch("elasticsearch.helpers.bulk") as mock_bulk,
            patch.object(Gallery.objects, "all", return_value=mock_qs),
        ):
            cmd = push_mod.Command()
            cmd.push_db_to_index_model(Gallery, bulk_size=0)
            cmd.push_db_to_index_model(Gallery, bulk_size=1)

        self.assertEqual(mock_bulk.call_count, 2)
        for call in mock_bulk.call_args_list:
            self.assertNotIn("request_timeout", call.kwargs)
            self.assertIs(call.kwargs["client"], mock_client)
