import datetime
import typing
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.base.types import GalleryData
from viewer.models import Archive, ArchiveChange, ArchiveTag, Gallery, GalleryChange, Tag, batched_changes


def gallery_data(**overrides: typing.Any) -> GalleryData:
    values: dict[str, typing.Any] = dict(
        gid="100",
        provider="panda",
        title="Title",
        category="Manga",
        filecount=10,
        filesize=1000,
        posted=timezone.make_aware(datetime.datetime(2024, 1, 1)),
        tags=["artist:alice", "female:glasses"],
    )
    values.update(overrides)
    return GalleryData(**values)


@patch.object(Gallery, "schedule_thumbnail_fetch", lambda self, *a, **k: None)
class GalleryChangeTrackingTests(TestCase):
    def changes(self, gallery):
        return list(GalleryChange.objects.filter(gallery=gallery).order_by("id"))

    def test_creation_records_nothing(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        self.assertEqual(gallery.tags.count(), 2)
        self.assertEqual(self.changes(gallery), [])

    def test_no_op_saves_and_refreshes_record_nothing(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        Gallery.objects.get(pk=gallery.pk).save()
        Gallery.objects.get(pk=gallery.pk).simple_save()
        Gallery.objects.update_or_create_from_values(gallery_data())
        Gallery.objects.update_by_gid_provider(gallery_data())
        # provider values come in as strings/ints that normalize to the stored value
        Gallery.objects.update_or_create_from_values(gallery_data(filecount="10", filesize="1000"))
        self.assertEqual(self.changes(gallery), [])

    def test_field_change_records_old_and_new(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        obj = Gallery.objects.get(pk=gallery.pk)
        obj.title = "New title"
        obj.rating = "4.5"
        obj.save()
        (change,) = self.changes(gallery)
        self.assertEqual(change.changes, {"title": ["Title", "New title"], "rating": ["", "4.5"]})
        self.assertEqual(change.tags_added, [])
        # a second save with the same values adds nothing
        obj.save()
        self.assertEqual(len(self.changes(gallery)), 1)

    def test_untracked_fields_are_ignored(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        obj = Gallery.objects.get(pk=gallery.pk)
        obj.provider_metadata = '{"raw": "response"}'
        obj.hidden = True
        obj.posted = timezone.make_aware(datetime.datetime(2024, 2, 1))
        obj.filecount = 11
        obj.filesize = 2000
        obj.dl_type = "torrent"
        obj.reason = "wanted"
        obj.thumbnail_url = "http://example.com/t.jpg"
        obj.status = Gallery.StatusChoices.DELETED
        obj.save()
        self.assertEqual(self.changes(gallery), [])

    def test_provider_refresh_is_one_row_with_fields_and_tags(self):
        gallery = Gallery.objects.add_from_values(gallery_data())

        Gallery.objects.update_or_create_from_values(gallery_data(title="Title 2", tags=["artist:alice", "male:beard"]))
        (change,) = self.changes(gallery)
        self.assertEqual(change.changes, {"title": ["Title", "Title 2"]})
        self.assertEqual((change.tags_added, change.tags_removed), (["male:beard"], ["female:glasses"]))

        # update_by_gid_provider sets tags before saving; still one row
        GalleryChange.objects.all().delete()
        Gallery.objects.update_by_gid_provider(gallery_data(title="Title 3", tags=["artist:alice"]))
        (change,) = self.changes(gallery)
        self.assertEqual(change.changes, {"title": ["Title 2", "Title 3"]})
        self.assertEqual((change.tags_added, change.tags_removed), ([], ["male:beard"]))

    def test_tag_operations_outside_batch(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        extra = Tag.objects.create(scope="misc", name="extra")
        unrelated = Tag.objects.create(scope="misc", name="unrelated")

        gallery.tags.add(extra)
        gallery.tags.remove(unrelated)  # not a member: nothing recorded
        gallery.tags.remove(extra)
        rows = self.changes(gallery)
        self.assertEqual([(r.tags_added, r.tags_removed) for r in rows], [(["misc:extra"], []), ([], ["misc:extra"])])

        GalleryChange.objects.all().delete()
        gallery.tags.clear()
        (row,) = self.changes(gallery)
        self.assertEqual(row.tags_removed, ["artist:alice", "female:glasses"])

    def test_reverse_tag_operations(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        tag = Tag.objects.create(scope="misc", name="reverse")
        tag.gallery_set.add(gallery)
        tag.gallery_set.remove(gallery)
        rows = self.changes(gallery)
        self.assertEqual([(r.tags_added, r.tags_removed) for r in rows], [(["misc:reverse"], []), ([], ["misc:reverse"])])

    def test_batch_merges_and_cancels_out(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        tag = Tag.objects.create(scope="misc", name="temp")
        obj = Gallery.objects.get(pk=gallery.pk)
        with transaction.atomic(), batched_changes():
            obj.title = "Temp"
            obj.save()
            obj.title = "Title"  # back to the original
            obj.category = "Doujinshi"
            obj.save()
            obj.tags.add(tag)
            obj.tags.remove(tag)
        (change,) = self.changes(gallery)
        self.assertEqual(change.changes, {"category": ["Manga", "Doujinshi"]})
        self.assertEqual((change.tags_added, change.tags_removed), ([], []))

    def test_batch_with_exception_writes_nothing(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        obj = Gallery.objects.get(pk=gallery.pk)
        with self.assertRaises(RuntimeError):
            with transaction.atomic(), batched_changes():
                obj.title = "Rolled back"
                obj.save()
                raise RuntimeError
        self.assertEqual(self.changes(gallery), [])

    def test_delete_inside_batch(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        obj = Gallery.objects.get(pk=gallery.pk)
        with transaction.atomic(), batched_changes():
            obj.title = "Changed"
            obj.save()
            obj.delete()
        self.assertFalse(GalleryChange.objects.exists())

    def test_refresh_from_db_resets_snapshot(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        stale = Gallery.objects.get(pk=gallery.pk)
        other = Gallery.objects.get(pk=gallery.pk)
        other.title = "Changed elsewhere"
        other.save()
        stale.refresh_from_db()
        stale.save()
        self.assertEqual(len(self.changes(gallery)), 1)

    def test_deferred_fields_and_update_fields(self):
        gallery = Gallery.objects.add_from_values(gallery_data())
        obj = Gallery.objects.only("id", "title").get(pk=gallery.pk)
        obj.title = "Only title"
        obj.save()
        _ = obj.category  # loads the deferred field, snapshot included
        obj.category = "Doujinshi"
        obj.uploader = "someone"
        obj.save(update_fields=["category"])
        rows = self.changes(gallery)
        self.assertEqual([r.changes for r in rows], [{"title": ["Title", "Only title"]}, {"category": ["Manga", "Doujinshi"]}])


class ArchiveChangeTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner")

    def test_archive_changes(self):
        gallery = Gallery.objects.create(gid="1", provider="panda")
        archive = Archive.objects.create(title="A", zipped="a.zip", user=self.user, crc32="1")
        self.assertFalse(ArchiveChange.objects.exists())

        obj = Archive.objects.get(pk=archive.pk)
        obj.save()
        self.assertFalse(ArchiveChange.objects.exists())

        # untracked: these are logged by the activity log or never change
        obj.crc32 = "abcd"
        obj.filecount = 5
        obj.filesize = 50
        obj.extracted = True
        obj.binned = True
        obj.save()
        self.assertFalse(ArchiveChange.objects.exists())

        obj.gallery = gallery
        obj.details = "notes"
        obj.save()
        (change,) = ArchiveChange.objects.filter(archive=archive)
        self.assertEqual(change.changes, {"gallery": [None, gallery.pk], "details": ["", "notes"]})


class ArchiveCustomTagTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser("admin", password="x")
        self.archive = Archive.objects.create(title="A", zipped="a.zip", user=self.user)
        self.custom = Tag.objects.create(scope="custom", name="mine")
        self.other = Tag.objects.create(scope="custom", name="other")
        self.system = Tag.objects.create(scope="artist", name="from-gallery")

    def rows(self):
        return [(r.custom_tags_added, r.custom_tags_removed) for r in ArchiveChange.objects.filter(archive=self.archive).order_by("id")]

    def test_custom_tags_are_tracked_system_tags_are_not(self):
        ArchiveTag.objects.create(archive=self.archive, tag=self.custom, origin=ArchiveTag.ORIGIN_USER)
        ArchiveTag.objects.create(archive=self.archive, tag=self.system, origin=ArchiveTag.ORIGIN_SYSTEM)
        self.archive.tags.add(self.other)  # through default: system
        ArchiveTag.objects.filter(archive=self.archive, origin=ArchiveTag.ORIGIN_USER).delete()
        ArchiveTag.objects.filter(archive=self.archive, origin=ArchiveTag.ORIGIN_SYSTEM).delete()
        self.assertEqual(self.rows(), [(["custom:mine"], []), ([], ["custom:mine"])])

    def test_replacing_custom_tags_logs_only_the_net_change(self):
        ArchiveTag.objects.create(archive=self.archive, tag=self.custom, origin=ArchiveTag.ORIGIN_USER)
        ArchiveChange.objects.all().delete()
        with batched_changes():
            ArchiveTag.objects.filter(archive=self.archive, origin=ArchiveTag.ORIGIN_USER).delete()
            ArchiveTag.objects.create(archive=self.archive, tag=self.custom, origin=ArchiveTag.ORIGIN_USER)
        self.assertEqual(self.rows(), [])
        with batched_changes():
            ArchiveTag.objects.filter(archive=self.archive, origin=ArchiveTag.ORIGIN_USER).delete()
            ArchiveTag.objects.create(archive=self.archive, tag=self.other, origin=ArchiveTag.ORIGIN_USER)
        self.assertEqual(self.rows(), [(["custom:other"], ["custom:mine"])])

    def test_tags_set_removing_a_custom_tag(self):
        gallery = Gallery.objects.create(gid="1", provider="panda")
        gallery.tags.add(self.system)
        ArchiveTag.objects.create(archive=self.archive, tag=self.custom, origin=ArchiveTag.ORIGIN_USER)
        ArchiveChange.objects.all().delete()
        self.archive.set_tags_from_gallery(gallery)  # keeps custom tags
        self.assertEqual(self.rows(), [])
        self.archive.set_tags_from_gallery(gallery, preserve_custom=False)
        self.assertEqual(self.rows(), [([], ["custom:mine"])])

    def test_deleting_the_archive_or_the_tag_logs_nothing(self):
        ArchiveTag.objects.create(archive=self.archive, tag=self.custom, origin=ArchiveTag.ORIGIN_USER)
        ArchiveTag.objects.create(archive=self.archive, tag=self.other, origin=ArchiveTag.ORIGIN_USER)
        ArchiveChange.objects.all().delete()
        self.other.delete()
        self.assertEqual(self.rows(), [])
        with transaction.atomic():
            Archive.objects.get(pk=self.archive.pk).delete()
        self.assertFalse(ArchiveChange.objects.exists())

    def test_archive_edit_form(self):
        ArchiveTag.objects.create(archive=self.archive, tag=self.custom, origin=ArchiveTag.ORIGIN_USER)
        ArchiveChange.objects.all().delete()
        self.client.force_login(self.user)
        url = reverse("viewer:archive-update", args=[self.archive.pk])
        form = {
            "title": "A", "title_jpn": "", "source_type": "", "reason": "backup", "details": "",
            "images-TOTAL_FORMS": "0", "images-INITIAL_FORMS": "0",
        }
        # Same custom tags: the view deletes and re-creates them, nothing is logged
        self.client.post(url, {**form, "tags": [self.custom.pk]})
        self.assertEqual(self.rows(), [])
        self.client.post(url, {**form, "title": "B", "tags": [self.other.pk]})
        (change,) = ArchiveChange.objects.filter(archive=self.archive)
        self.assertEqual(change.changes, {"title": ["A", "B"]})
        self.assertEqual((change.custom_tags_added, change.custom_tags_removed), (["custom:other"], ["custom:mine"]))

        response = self.client.get(reverse("viewer:archive-change-log", args=[self.archive.pk]))
        self.assertContains(response, "custom tags")
        self.assertContains(response, "other")


@patch.object(Gallery, "schedule_thumbnail_fetch", lambda self, *a, **k: None)
class ChangeLogViewTests(TestCase):
    def setUp(self):
        self.gallery = Gallery.objects.add_from_values(gallery_data(public=True))
        obj = Gallery.objects.get(pk=self.gallery.pk)
        obj.fjord = True  # private field only
        obj.save()
        obj.title = "Public change"
        obj.save()
        obj.tags.add(Tag.objects.create(scope="misc", name="added"))

    def test_authenticated_sees_every_change(self):
        self.client.force_login(User.objects.create_user("viewer"))
        response = self.client.get(reverse("viewer:gallery-change-log", args=[self.gallery.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 3)
        self.assertContains(response, "<td>fjord</td>", html=False)
        self.assertContains(response, "Public change")
        self.assertContains(response, "added")

    def test_anonymous_sees_public_changes_only(self):
        response = self.client.get(reverse("viewer:gallery-change-log", args=[self.gallery.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertNotContains(response, "<td>fjord</td>", html=False)
        self.assertContains(response, "Public change")

    def test_archive_change_log(self):
        user = User.objects.create_superuser("admin", password="x")
        archive = Archive.objects.create(title="A", zipped="a.zip", user=user)
        obj = Archive.objects.get(pk=archive.pk)
        obj.gallery = self.gallery
        obj.save()
        self.client.force_login(user)
        response = self.client.get(reverse("viewer:archive-change-log", args=[archive.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("viewer:gallery", args=[self.gallery.pk]))
