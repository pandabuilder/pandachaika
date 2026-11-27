from datetime import datetime, timezone
import logging
import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

import elasticsearch
from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings

from core.base.setup import Settings
from core.base.types import GalleryData
from core.providers.panda.parsers import Parser as PandaParser
from core.web.gallery_updater import GalleryMetadataUpdater
from viewer.models import Archive, ArchiveOption, Gallery, WantedGallery, Tag
from viewer.services.elasticsearch_index import ElasticsearchIndexService
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
    def test_archive_delete_uses_client_timeout_without_forced_refresh(self) -> None:
        archive = Archive(pk=42, title="ES Delete Archive", thumbnail="")
        mock_client = MagicMock()

        with (
            override_settings(ES_CLIENT=mock_client, ES_AUTOREFRESH=True),
            patch("django.db.models.Model.delete", return_value=(1, {})),
            patch("viewer.services.elasticsearch_index.transaction.on_commit", side_effect=lambda fn: fn()),
        ):
            archive.delete()

        mock_client.options.assert_not_called()
        mock_client.delete.assert_called_once_with(
            index=getattr(Archive._meta, "es_index_name"),
            id="42",
        )

    def test_gallery_delete_uses_client_timeout_without_forced_refresh(self) -> None:
        gallery = Gallery(pk=99, title="ES Delete Gallery", gid="999001", provider="panda")
        mock_client = MagicMock()

        with (
            override_settings(ES_CLIENT=mock_client, ES_AUTOREFRESH_GALLERY=True),
            patch.object(Gallery, "remove_wanted_relations"),
            patch("django.db.models.Model.delete", return_value=(1, {})),
            patch("viewer.services.elasticsearch_index.transaction.on_commit", side_effect=lambda fn: fn()),
        ):
            gallery.delete()

        mock_client.options.assert_not_called()
        mock_client.delete.assert_called_once_with(
            index=getattr(Gallery._meta, "es_index_name"),
            id="99",
        )

    def test_push_to_index_bulk_does_not_override_request_timeout(self) -> None:
        from importlib import import_module

        push_mod = import_module("viewer.management.commands.push-to-index")
        fake_gallery = MagicMock()
        fake_gallery.es_repr.return_value = {"_id": "1", "title": "ES Bulk Gallery"}
        fake_gallery._meta.es_index_name = "viewer_gallery"

        mock_qs = MagicMock()
        mock_qs.select_related.return_value = mock_qs
        mock_qs.prefetch_related.return_value = mock_qs
        mock_qs.order_by.return_value = [fake_gallery]
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
            self.assertNotIn("refresh", call.kwargs)
            self.assertIs(call.kwargs["client"], mock_client)


class ElasticsearchIndexServiceTests(TestCase):
    user: User
    artist_tag: Tag
    lang_tag: Tag

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="es_service_user", password="password123")
        cls.artist_tag = Tag.objects.create(scope="artist", name="shared_artist")
        cls.lang_tag = Tag.objects.create(scope="language", name="english")

    def test_shared_es_repr_for_archive_and_gallery(self) -> None:
        posted = datetime(2026, 3, 15, 12, 30, 0, tzinfo=timezone.utc)
        gallery = Gallery.objects.create(
            gid="es-gal-1",
            provider="panda",
            title="Shared Gallery Title",
            title_jpn="日本語タイトル",
            category="Manga",
            filesize=123456,
            filecount=24,
            posted=posted,
            public=True,
        )
        gallery.tags.set([self.artist_tag, self.lang_tag])

        archive = Archive.objects.create(
            title="Shared Archive Title",
            title_jpn="",
            gallery=gallery,
            user=self.user,
            filesize=123000,
            filecount=24,
            public=True,
            public_date=posted,
        )
        archive.tags.set([self.artist_tag])

        gallery_repr = gallery.es_repr()
        self.assertEqual(gallery_repr["_id"], gallery.pk)
        self.assertEqual(gallery_repr["title"], "Shared Gallery Title")
        self.assertEqual(gallery_repr["title_complete"], {"input": ["Shared Gallery Title", "日本語タイトル"]})
        self.assertEqual(gallery_repr["title_suggest"], ["Shared Gallery Title", "日本語タイトル"])
        self.assertEqual(gallery_repr["size"], 123456)
        self.assertEqual(gallery_repr["image_count"], 24)
        self.assertEqual(gallery_repr["posted_date"], "2026-03-15T12:30:00+0000")
        self.assertEqual(gallery_repr["has_container"], False)
        self.assertEqual(gallery_repr["has_magazine"], False)
        self.assertEqual(gallery_repr["has_contained"], False)
        self.assertEqual(gallery_repr["has_chapters"], False)
        self.assertEqual(
            {t["full"] for t in gallery_repr["tags"]},
            {"artist:shared_artist", "language:english"},
        )

        archive_repr = archive.es_repr()
        self.assertEqual(archive_repr["_id"], archive.pk)
        self.assertEqual(archive_repr["title"], "Shared Archive Title")
        self.assertEqual(archive_repr["title_complete"], {"input": ["Shared Archive Title"]})
        self.assertEqual(archive_repr["title_suggest"], ["Shared Archive Title"])
        self.assertEqual(archive_repr["size"], 123000)
        self.assertEqual(archive_repr["image_count"], 24)
        self.assertEqual(archive_repr["category"], "Manga")
        self.assertEqual(archive_repr["original_date"], "2026-03-15T12:30:00+0000")
        self.assertEqual(archive_repr["public_date"], "2026-03-15T12:30:00+0000")
        self.assertEqual(archive_repr["tags"], [{"scope": "artist", "name": "shared_artist", "full": "artist:shared_artist"}])

    def test_update_index_uses_upsert_without_refresh_by_default(self) -> None:
        archive = Archive.objects.create(title="Upsert Archive", user=self.user, public=True)
        mock_client = MagicMock()

        with override_settings(ES_CLIENT=mock_client, ES_AUTOREFRESH=True, ES_ONLY_INDEX_PUBLIC=False):
            result = archive.update_index()

        self.assertTrue(result)
        mock_client.update.assert_called_once()
        kwargs = mock_client.update.call_args.kwargs
        self.assertEqual(kwargs["index"], getattr(Archive._meta, "es_index_name"))
        self.assertEqual(kwargs["id"], str(archive.pk))
        self.assertTrue(kwargs["doc_as_upsert"])
        self.assertNotIn("refresh", kwargs)
        self.assertNotIn("_id", kwargs["doc"])
        mock_client.create.assert_not_called()

    def test_autorefresh_flags_and_only_index_public_are_respected(self) -> None:
        gallery = Gallery.objects.create(gid="es-flag-1", provider="panda", title="Private Gallery", public=False)
        archive = Archive.objects.create(title="Private Archive", user=self.user, public=False)
        mock_client = MagicMock()

        with override_settings(
            ES_CLIENT=mock_client,
            ES_AUTOREFRESH=True,
            ES_AUTOREFRESH_GALLERY=False,
            ES_ONLY_INDEX_PUBLIC=True,
        ):
            self.assertFalse(archive.update_index())
            self.assertFalse(gallery.update_index())

        mock_client.update.assert_not_called()

        with override_settings(
            ES_CLIENT=mock_client,
            ES_AUTOREFRESH=True,
            ES_AUTOREFRESH_GALLERY=False,
            ES_ONLY_INDEX_PUBLIC=False,
        ):
            self.assertTrue(archive.update_index())
            self.assertFalse(gallery.update_index())

        self.assertEqual(mock_client.update.call_count, 1)

    def test_batch_indexing_deduplicates_handles_deletes_and_skips_rolled_back_savepoints(self) -> None:
        mock_client = MagicMock()
        existing_archive = Archive.objects.create(title="To Delete", user=self.user, public=True)
        existing_pk = existing_archive.pk

        with (
            override_settings(
                ES_CLIENT=mock_client,
                ES_AUTOREFRESH=True,
                ES_AUTOREFRESH_GALLERY=True,
                ES_ONLY_INDEX_PUBLIC=False,
            ),
            patch("elasticsearch.helpers.bulk", return_value=(3, 0)) as mock_bulk,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                with ElasticsearchIndexService.batch_indexing():
                    # Nested batch joins outer batch
                    with ElasticsearchIndexService.batch_indexing():
                        gallery = Gallery.objects.create(
                            gid="batch-gal-1", provider="panda", title="Batch Gallery", public=True
                        )
                        gallery.title = "Batch Gallery Final"
                        gallery.save()

                    archive = Archive.objects.create(
                        title="Batch Archive", gallery=gallery, user=self.user, public=True
                    )
                    archive.title = "Batch Archive Final"
                    archive.save()

                    # Save then delete same archive -> only delete should be sent
                    existing_archive.title = "Updated Before Delete"
                    existing_archive.save()
                    existing_archive.delete()

                    # Rolled-back savepoint must not be indexed
                    try:
                        with transaction.atomic():
                            Archive.objects.create(title="Rolled Back Archive", user=self.user, public=True)
                            raise RuntimeError("force rollback")
                    except RuntimeError:
                        pass

        mock_client.update.assert_not_called()
        mock_client.delete.assert_not_called()
        mock_bulk.assert_called_once()
        bulk_kwargs = mock_bulk.call_args.kwargs
        self.assertIs(bulk_kwargs["client"], mock_client)
        self.assertNotIn("refresh", bulk_kwargs)
        self.assertNotIn("request_timeout", bulk_kwargs)

        actions = bulk_kwargs["actions"]
        self.assertEqual(len(actions), 3)

        by_key = {(a["_index"], a["_id"]): a for a in actions}
        gal_action = by_key[(getattr(Gallery._meta, "es_index_name"), str(gallery.pk))]
        self.assertEqual(gal_action["_op_type"], "update")
        self.assertTrue(gal_action["doc_as_upsert"])
        self.assertEqual(gal_action["doc"]["title"], "Batch Gallery Final")

        arc_action = by_key[(getattr(Archive._meta, "es_index_name"), str(archive.pk))]
        self.assertEqual(arc_action["_op_type"], "update")
        self.assertTrue(arc_action["doc_as_upsert"])
        self.assertEqual(arc_action["doc"]["title"], "Batch Archive Final")

        del_action = by_key[(getattr(Archive._meta, "es_index_name"), str(existing_pk))]
        self.assertEqual(del_action["_op_type"], "delete")

    def test_gallery_metadata_updater_bulk_indexes_galleries_and_related_archives(self) -> None:
        g1 = Gallery.objects.create(gid="upd-1", token="t1", provider="panda", title="Old G1", public=True)
        g2 = Gallery.objects.create(gid="upd-2", token="t2", provider="panda", title="Old G2", public=True)
        a1 = Archive.objects.create(title="Old A1", gallery=g1, user=self.user, public=True)
        a2 = Archive.objects.create(title="Frozen A2", gallery=g1, user=self.user, public=True)
        ArchiveOption.objects.create(archive=a2, freeze_titles=True)
        a3 = Archive.objects.create(title="Old A3", gallery=g2, user=self.user, public=True)

        link1 = "https://e-hentai.org/g/upd-1/t1/"
        link2 = "https://e-hentai.org/g/upd-2/t2/"
        fresh1 = GalleryData(
            gid="upd-1",
            token="t1",
            provider="panda",
            title="Fresh G1",
            tags=["artist:new_one"],
            link=link1,
        )
        fresh2 = GalleryData(
            gid="upd-2",
            token="t2",
            provider="panda",
            title="Fresh G2",
            tags=["artist:new_two"],
            link=link2,
        )

        crawler_settings = Settings(load_from_disk=False)
        crawler_settings.archive_user = self.user
        updater = GalleryMetadataUpdater(crawler_settings)

        mock_parser = MagicMock()
        mock_parser.name = "panda"
        mock_parser.filter_accepted_urls.return_value = [link1, link2]
        mock_parser.supports_gallery_update.return_value = True
        mock_parser.filter_update_urls.return_value = [link1, link2]
        mock_parser.update_fetch_chunk_size = 25
        mock_parser.fetch_gallery_data_for_update.return_value = [fresh1, fresh2]
        mock_parser.general_utils = MagicMock()
        mock_parser.general_utils.discard_by_gallery_data.return_value = (False, [])
        mock_parser.is_current_link_non_current.return_value = False

        mock_client = MagicMock()
        with (
            override_settings(
                ES_CLIENT=mock_client,
                ES_AUTOREFRESH=True,
                ES_AUTOREFRESH_GALLERY=True,
                ES_ONLY_INDEX_PUBLIC=False,
            ),
            patch.object(crawler_settings.provider_context, "get_parsers", return_value=[mock_parser]),
            patch("elasticsearch.helpers.bulk", return_value=(5, 0)) as mock_bulk,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                summary = updater.update([link1, link2])

        self.assertEqual(summary.updated, 2)
        mock_client.update.assert_not_called()
        mock_bulk.assert_called_once()

        actions = mock_bulk.call_args.kwargs["actions"]
        self.assertEqual(len(actions), 5)
        by_key = {(a["_index"], a["_id"]): a for a in actions}

        gal_index = getattr(Gallery._meta, "es_index_name")
        arc_index = getattr(Archive._meta, "es_index_name")

        self.assertEqual(by_key[(gal_index, str(g1.pk))]["doc"]["title"], "Fresh G1")
        self.assertEqual(by_key[(gal_index, str(g2.pk))]["doc"]["title"], "Fresh G2")
        self.assertEqual(by_key[(arc_index, str(a1.pk))]["doc"]["title"], "Fresh G1")
        self.assertEqual(
            by_key[(arc_index, str(a1.pk))]["doc"]["tags"],
            [{"scope": "artist", "name": "new_one", "full": "artist:new_one"}],
        )
        # Archive with frozen titles still gets bulk-reindexed with its updated tags
        self.assertEqual(by_key[(arc_index, str(a2.pk))]["doc"]["title"], "Frozen A2")
        self.assertEqual(
            by_key[(arc_index, str(a2.pk))]["doc"]["tags"],
            [{"scope": "artist", "name": "new_one", "full": "artist:new_one"}],
        )
        self.assertEqual(by_key[(arc_index, str(a3.pk))]["doc"]["title"], "Fresh G2")

    def test_recreate_index_shared_by_push_to_index(self) -> None:
        from importlib import import_module

        push_mod = import_module("viewer.management.commands.push-to-index")
        mock_client = MagicMock()
        mock_client.indices.exists.return_value = True

        with patch("elasticsearch.Elasticsearch", return_value=mock_client):
            cmd = push_mod.Command()
            cmd.recreate_index_model(Archive)
            cmd.recreate_match_index_model()

        self.assertEqual(mock_client.indices.delete.call_count, 2)
        self.assertEqual(mock_client.indices.create.call_count, 2)
        self.assertEqual(mock_client.indices.put_settings.call_count, 2)
        self.assertEqual(mock_client.indices.put_mapping.call_count, 2)
        self.assertEqual(mock_client.indices.open.call_count, 2)
