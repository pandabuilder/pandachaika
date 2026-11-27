"""Version chains of galleries, grouped by the provider's first gallery gid."""

import importlib
import json

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from core.base.types import GalleryData
from viewer.models import Gallery

backfill_migration = importlib.import_module("viewer.migrations.0238_backfill_gallery_first_gallery_gid")


def make_gallery(gid: str, title: str, **kwargs) -> Gallery:
    return Gallery.objects.create(gid=gid, provider="panda", title=title, public=True, **kwargs)


class FirstGalleryMissingTest(TestCase):
    """The first gallery of the chain (gid 100) was never added: the versions still form one chain."""

    @classmethod
    def setUpTestData(cls):
        cls.v4 = make_gallery("400", "Series Ch.1-4", first_gallery_gid="100")
        cls.v5 = make_gallery("500", "Series Ch.1-5", first_gallery_gid="100", parent_gallery=cls.v4)
        cls.v6 = make_gallery("600", "Series Ch.1-6", first_gallery_gid="100", parent_gallery=cls.v5)
        cls.v7 = make_gallery("700", "Series Ch.1-7", first_gallery_gid="100", parent_gallery=cls.v6)
        make_gallery("800", "Other series", first_gallery_gid="101")
        Gallery.objects.create(gid="900", provider="other", title="Other provider", first_gallery_gid="100")

    def test_every_version_sees_the_whole_chain(self):
        expected = [self.v4.pk, self.v5.pk, self.v6.pk, self.v7.pk]
        for gallery in (self.v4, self.v5, self.v6, self.v7):
            self.assertEqual([g.pk for g in gallery.get_gallery_chain()], expected)

    def test_only_the_newest_is_last_and_not_replaced(self):
        self.assertEqual([g.get_es_last_in_chain() for g in (self.v4, self.v5, self.v6, self.v7)], [False] * 3 + [True])
        self.assertEqual([g.is_replaced() for g in (self.v4, self.v5, self.v6, self.v7)], [True] * 3 + [False])

    def test_gallery_page_lists_the_chain(self):
        for route, heading in (("viewer:gallery-new", "Galleries in Chain"), ("viewer:gallery-old", "Galleries in chain")):
            with self.subTest(route=route):
                resp = self.client.get(reverse(route, args=[self.v5.pk]))
                self.assertContains(resp, heading)
                for gallery in (self.v4, self.v6, self.v7):
                    self.assertContains(resp, 'href="{}"'.format(reverse("viewer:gallery", args=[gallery.pk])))
                self.assertNotContains(resp, "Other series")
                self.assertContains(resp, "gid 100 (not in the database)")

    def test_standalone_gallery_is_its_own_chain(self):
        alone = make_gallery("1000", "Alone")
        self.assertEqual(list(alone.get_gallery_chain()), [alone])
        self.assertTrue(alone.get_es_last_in_chain())
        self.assertFalse(alone.is_replaced())


class FirstGalleryPresentTest(TestCase):
    def test_first_gallery_without_link_joins_the_chain_and_is_replaced(self):
        first = make_gallery("100", "Series Ch.1")
        newer = make_gallery("200", "Series Ch.1-2", first_gallery_gid="100")
        self.assertEqual(list(first.get_gallery_chain()), [first, newer])
        self.assertEqual(list(newer.get_gallery_chain()), [first, newer])
        self.assertTrue(first.is_replaced())
        self.assertFalse(newer.is_replaced())

    def test_chains_linked_only_by_the_foreign_key_still_work(self):
        first = make_gallery("100", "Series Ch.1")
        newer = make_gallery("200", "Series Ch.1-2", first_gallery=first)
        newest = make_gallery("300", "Series Ch.1-3", first_gallery=first)
        for gallery in (first, newer, newest):
            self.assertEqual(list(gallery.get_gallery_chain()), [first, newer, newest])

    def test_adding_the_first_gallery_links_the_newer_versions(self):
        newer = make_gallery("200", "Series Ch.1-2", first_gallery_gid="100")
        other_provider = Gallery.objects.create(gid="300", provider="other", first_gallery_gid="100")

        first = Gallery.objects.update_or_create_from_values(GalleryData("100", "panda", title="Series Ch.1"))

        newer.refresh_from_db()
        other_provider.refresh_from_db()
        self.assertEqual(newer.first_gallery_id, first.pk)
        self.assertIsNone(other_provider.first_gallery_id)

    def test_first_gallery_gid_is_saved_from_provider_data(self):
        first = make_gallery("100", "Series Ch.1")
        gallery = Gallery.objects.update_or_create_from_values(
            GalleryData("200", "panda", title="Series Ch.1-2", first_gallery_gid="100")
        )
        self.assertEqual((gallery.first_gallery_gid, gallery.first_gallery_id), ("100", first.pk))

        missing = Gallery.objects.update_or_create_from_values(
            GalleryData("300", "panda", title="Other Ch.1-2", first_gallery_gid="150")
        )
        self.assertEqual((missing.first_gallery_gid, missing.first_gallery_id), ("150", None))


class BackfillMigrationTest(TestCase):
    def test_backfill_reads_the_metadata_and_links_local_first_galleries(self):
        first = make_gallery("100", "Series Ch.1")
        linked = make_gallery("200", "Ch.1-2", provider_metadata=json.dumps({"gid": 200, "first_gid": "100"}))
        missing = make_gallery("300", "Other Ch.1-2", provider_metadata=json.dumps({"gid": 300, "first_gid": "150"}))
        numeric = make_gallery("400", "Numeric", provider_metadata=json.dumps({"gid": 400, "first_gid": 150}))
        own = make_gallery("500", "Own gid", provider_metadata=json.dumps({"first_gid": "500"}))
        broken = make_gallery("600", "Broken", provider_metadata='{"first_gid": "1')
        no_chain = make_gallery("700", "No chain", provider_metadata=json.dumps({"gid": 700}))

        backfill_migration.backfill_first_gallery_gid(apps, None)

        result = {
            g.pk: (g.first_gallery_gid, g.first_gallery_id)
            for g in Gallery.objects.filter(pk__in=[linked.pk, missing.pk, numeric.pk, own.pk, broken.pk, no_chain.pk])
        }
        self.assertEqual(
            result,
            {
                linked.pk: ("100", first.pk),
                missing.pk: ("150", None),
                numeric.pk: ("150", None),
                own.pk: (None, None),
                broken.pk: (None, None),
                no_chain.pk: (None, None),
            },
        )
