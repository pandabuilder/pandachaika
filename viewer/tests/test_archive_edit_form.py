"""The archive page's inline edit form: shared by collaborators and staff, with staff-only fields and an
on-demand image editor (it replaced the separate /archive/<pk>/edit/ page)."""
import datetime
import os
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.base.types import GalleryData
from viewer.models import Archive, ArchiveGroup, ArchiveGroupEntry, ArchiveMatches, ArchiveTag, Gallery, Image, Tag


def make_gallery(gid: str, title: str) -> Gallery:
    return Gallery.objects.add_from_values(
        GalleryData(
            gid=gid,
            provider="panda",
            title=title,
            category="Manga",
            filecount=10,
            filesize=1000,
            posted=timezone.make_aware(datetime.datetime(2024, 1, 1)),
            tags=["artist:alice"],
        )
    )


@patch.object(Gallery, "schedule_thumbnail_fetch", lambda self, *a, **k: None)
class ArchiveEditFormTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.collaborator = User.objects.create_user("collaborator", password="x")
        self.collaborator.user_permissions.add(Permission.objects.get(codename="change_archive"))
        self.viewer = User.objects.create_user("viewer", password="x")
        self.archive = Archive.objects.create(title="Original", zipped="a.zip", user=self.staff, public=True)
        self.url = reverse("viewer:archive-new", args=[self.archive.pk])
        self.custom = Tag.objects.create(scope="custom", name="mine")
        self.group = ArchiveGroup.objects.create(title="Group")

    def post(self, **data):
        form = {
            "change-archive": "Submit", "title": "Original", "title_jpn": "", "source_type": "", "reason": "backup",
            "details": "", "zipped": "a.zip",
        }
        form.update(data)
        return self.client.post(self.url, form)

    def test_collaborator_gets_the_form_without_staff_fields(self):
        self.client.force_login(self.collaborator)
        response = self.client.get(self.url)
        form = response.context["edit_form"]
        self.assertIn("gallery", form.fields)
        for name in ("custom_tags", "archive_groups", "zipped", "possible_matches"):
            self.assertNotIn(name, form.fields)
        self.assertNotContains(response, "Load image editor")
        self.assertNotContains(response, "Match tools")

    def test_collaborator_cannot_set_staff_fields(self):
        self.client.force_login(self.collaborator)
        response = self.post(title="Changed", zipped="renamed.zip", custom_tags=[self.custom.pk], archive_groups=[self.group.pk])
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.title, "Changed")
        self.assertEqual(self.archive.zipped.name, "a.zip")
        self.assertFalse(self.archive.custom_tags().exists())
        self.assertFalse(ArchiveGroupEntry.objects.filter(archive=self.archive).exists())

    def test_user_without_permission_gets_no_form_and_cannot_post(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertNotIn("edit_form", response.context)
        self.assertNotContains(response, 'id="change-archive-form"')
        response = self.post(title="Changed")
        self.assertEqual(response.status_code, 200)
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.title, "Original")

    def test_staff_form_has_staff_fields_and_no_image_formset(self):
        Image.objects.create(archive=self.archive, position=1)
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        form = response.context["edit_form"]
        for name in ("custom_tags", "archive_groups", "zipped", "possible_matches"):
            self.assertIn(name, form.fields)
        self.assertNotIn("image_formset", response.context)
        self.assertContains(response, reverse("viewer:archive-images-edit", args=[self.archive.pk]))
        self.assertContains(response, "Match tools")

    def test_staff_sets_custom_tags_and_archive_groups(self):
        other_group = ArchiveGroup.objects.create(title="Other")
        ArchiveGroupEntry.objects.create(archive=self.archive, archive_group=other_group)
        self.client.force_login(self.staff)
        response = self.post(custom_tags=[self.custom.pk], archive_groups=[self.group.pk])
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(list(self.archive.custom_tags()), [self.custom])
        self.assertEqual(list(self.archive.archive_groups.all()), [self.group])

        response = self.post(custom_tags=[], archive_groups=[self.group.pk])
        self.assertFalse(ArchiveTag.objects.filter(archive=self.archive).exists())
        self.assertEqual(ArchiveGroupEntry.objects.filter(archive=self.archive).count(), 1)

    def test_staff_selects_a_possible_match(self):
        old = make_gallery("1", "Old gallery")
        candidate = make_gallery("2", "Candidate gallery")
        candidate.dl_type = "failed:matched"
        candidate.save()
        self.archive.gallery = old
        self.archive.save()
        ArchiveMatches.objects.create(archive=self.archive, gallery=candidate, match_accuracy=0.9)
        self.client.force_login(self.staff)
        self.post(gallery=old.pk, possible_matches=candidate.pk, old_gallery_to_alt="on")
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.gallery, candidate)
        self.assertEqual(self.archive.title, "Candidate gallery")
        self.assertEqual(self.archive.match_type, "manual:cutoff")
        self.assertFalse(self.archive.possible_matches.exists())
        self.assertEqual(list(self.archive.alternative_sources.all()), [old])
        candidate.refresh_from_db()
        self.assertEqual(candidate.dl_type, "manual:matched")

    def test_staff_rename_to_existing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            open(os.path.join(media_root, "taken.zip"), "w").close()
            self.client.force_login(self.staff)
            response = self.post(title="Changed", zipped="taken.zip")
        self.assertEqual(response.status_code, 200)
        self.assertIn("zipped", response.context["edit_form"].errors)
        self.assertContains(response, 'id="change-archive-form" class="collapse mb-3 show"')
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.title, "Original")
        self.assertEqual(self.archive.zipped.name, "a.zip")

    def test_old_edit_urls_redirect_to_the_page(self):
        self.client.force_login(self.staff)
        for edit_name, name in (
            ("viewer:archive-edit", "viewer:archive"),
            ("viewer:archive-new-edit", "viewer:archive-new"),
            ("viewer:archive-old-edit", "viewer:archive-old"),
        ):
            response = self.client.get(reverse(edit_name, args=[self.archive.pk, "edit"]))
            self.assertRedirects(
                response, reverse(name, args=[self.archive.pk]) + "#change-archive-form", fetch_redirect_response=False
            )

    def test_old_template_renders_the_form(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("viewer:archive-old", args=[self.archive.pk]))
        self.assertContains(response, 'id="change-archive-form"')
        self.assertContains(response, "Load image editor")


class ArchiveImagesEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("staff", password="x", is_staff=True)
        self.collaborator = User.objects.create_user("collaborator", password="x")
        self.collaborator.user_permissions.add(Permission.objects.get(codename="change_archive"))
        self.archive = Archive.objects.create(title="A", zipped="a.zip", user=self.staff)
        self.images = [
            Image.objects.create(archive=self.archive, position=i, image_name="{:03d}.jpg".format(i)) for i in (1, 2, 3)
        ]
        self.url = reverse("viewer:archive-images-edit", args=[self.archive.pk])

    def payload(self, order, delete=()):
        return {"order": ",".join(str(image.pk) for image in order), "delete": [image.pk for image in delete]}

    def positions(self):
        return list(self.archive.image_set.values_list("pk", "position"))

    def test_staff_only(self):
        a, b, c = self.images
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.collaborator)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.payload([c, b, a])).status_code, 404)
        self.assertEqual(self.positions(), [(a.pk, 1), (b.pk, 2), (c.pk, 3)])

    def test_get_renders_the_editor(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([name for _, name in response.context["images"]], ["001.jpg", "002.jpg", "003.jpg"])
        self.assertContains(response, 'id="archive-images-editor"')
        self.assertContains(response, "data-image-editor")

    def test_reorder_and_delete(self):
        a, b, c = self.images
        self.client.force_login(self.staff)
        with patch.object(Image, "delete_plus_files", lambda image: image.delete()):
            response = self.client.post(self.url, self.payload([c, a], delete=[b]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["saved"])
        self.assertEqual(self.positions(), [(c.pk, 1), (a.pk, 2)])
        self.assertEqual([image.pk for image, _ in response.context["images"]], [c.pk, a.pk])

    def test_stale_or_invalid_payloads_change_nothing(self):
        a, b, c = self.images
        other = Image.objects.create(archive=Archive.objects.create(title="B", zipped="b.zip", user=self.staff), position=1)
        self.client.force_login(self.staff)
        for data in (
            self.payload([c, a]),  # b missing: added after the editor loaded
            self.payload([c, b, a, other]),  # another archive's image
            self.payload([c, b, a, a]),  # duplicate
            self.payload([c, b, a], delete=[a]),  # both kept and deleted
            {"order": "x,y", "delete": []},
        ):
            response = self.client.post(self.url, data)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context["saved"])
            self.assertContains(response, "Nothing was saved")
        self.assertEqual(self.positions(), [(a.pk, 1), (b.pk, 2), (c.pk, 3)])
