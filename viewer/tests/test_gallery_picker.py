from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from viewer.models import FoundGallery, Gallery, GalleryMatch, WantedGallery
from viewer.services.gallery_selection import allowed_filter_keys, clean_filter, filter_galleries, parse_gallery_ids
from viewer.tests.factories import ArchiveFactory, GalleryFactory

User = get_user_model()


def make_user(username: str, *perms: str, staff: bool = False):
    user = User.objects.create_user(username=username, password="password", is_staff=staff)
    for perm in perms:
        user.user_permissions.add(Permission.objects.get(codename=perm, content_type__app_label="viewer"))
    return User.objects.get(pk=user.pk)  # Fresh object, so the permission cache sees the new permissions.


class GallerySelectionTests(TestCase):
    def test_parse_gallery_ids(self):
        self.assertEqual(parse_gallery_ids("1, 2 3\n5-7;2"), [1, 2, 3, 5, 6, 7])
        self.assertEqual(parse_gallery_ids("https://example.com/gallery/42/ 9-8"), [42, 8, 9])
        self.assertEqual(parse_gallery_ids("  "), [])
        with self.assertRaises(ValueError):
            parse_gallery_ids("12 abc")
        with self.assertRaises(ValueError):
            parse_gallery_ids("/archive/5/")
        with self.assertRaises(ValueError):
            parse_gallery_ids("1-5000")

    def test_staff_only_filters(self):
        staff = make_user("staff", staff=True)
        plain = make_user("plain")
        self.assertIn("any_status", allowed_filter_keys(staff))
        self.assertNotIn("any_status", allowed_filter_keys(plain))
        self.assertEqual(allowed_filter_keys(None), [])
        self.assertEqual(clean_filter({"any_status": "1", "title": " x "}, plain), {"title": "x"})
        self.assertEqual(clean_filter({"any_status": "1", "sort": "nope"}, staff), {"any_status": "1"})
        with self.assertRaises(ValueError):
            clean_filter({"posted_from": "yesterday"}, staff)

    def test_filters(self):
        used = GalleryFactory.create(title="Used one", gid="111", filecount=10)
        ArchiveFactory.create(gallery=used)
        free = GalleryFactory.create(title="Free one", gid="222", filecount=30)
        deleted = GalleryFactory.create(title="Deleted one", gid="333", status=Gallery.StatusChoices.DELETED)

        def ids(params):
            return set(filter_galleries(params).values_list("pk", flat=True))

        self.assertEqual(ids({"title": "one"}), {used.pk, free.pk})
        self.assertEqual(ids({"title": "one", "any_status": "1"}), {used.pk, free.pk, deleted.pk})
        self.assertEqual(ids({"title": "one", "not_used": "1"}), {free.pk})
        self.assertEqual(ids({"filecount_from": "20"}), {free.pk})
        self.assertEqual(ids({"link": "222"}), {free.pk})
        self.assertEqual(ids({"link": "https://e-hentai.org/g/111/abcdef1234/"}), {used.pk})


class GalleryPickerApiTests(TestCase):
    def setUp(self):
        self.url = reverse("viewer:api-gallery-picker")
        self.staff = make_user("staff", staff=True)
        self.plain = make_user("plain")
        self.galleries = [GalleryFactory.create(title=f"Picker {i}") for i in range(25)]
        self.deleted = GalleryFactory.create(title="Picker deleted", status=Gallery.StatusChoices.DELETED)

    def test_requires_login(self):
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_listing_with_filters_and_pagination(self):
        self.client.force_login(self.plain)
        data = self.client.get(self.url, {"title": "picker", "size": "10", "page": "3", "sort": "title"}).json()
        self.assertEqual(data["count"], 25)
        self.assertEqual(data["num_pages"], 3)
        self.assertEqual(data["number"], 3)
        self.assertEqual(len(data["results"]), 5)
        self.assertFalse(data["is_staff"])
        self.assertNotIn("any_status", data["allowed_filters"])
        self.assertEqual(data["filter"], {"title": "picker", "sort": "title"})
        self.assertEqual(data["autocomplete"]["provider"], reverse("gallery-provider-json-autocomplete"))
        first = data["results"][0]
        self.assertEqual(first["url"], reverse("viewer:gallery", args=(first["id"],)))
        self.assertEqual(first["archive_count"], 0)
        self.assertTrue(first["normal"])

        # Non staff users can't ask for deleted galleries.
        data = self.client.get(self.url, {"title": "deleted", "any_status": "1"}).json()
        self.assertEqual(data["count"], 0)

    def test_staff_can_include_any_status(self):
        self.client.force_login(self.staff)
        data = self.client.get(self.url, {"title": "deleted", "any_status": "1"}).json()
        self.assertEqual([r["id"] for r in data["results"]], [self.deleted.pk])
        self.assertFalse(data["results"][0]["normal"])
        self.assertEqual(data["results"][0]["status"], "Deleted")

    def test_ids_lookup(self):
        self.client.force_login(self.plain)
        wanted = [self.galleries[3].pk, 999999, self.deleted.pk, self.galleries[1].pk]
        data = self.client.get(self.url, {"ids": ",".join(str(x) for x in wanted)}).json()
        self.assertEqual([r["id"] for r in data["results"]], [self.galleries[3].pk, self.galleries[1].pk])
        self.assertEqual(data["missing_ids"], [999999, self.deleted.pk])
        self.assertEqual(self.client.get(self.url, {"ids": "1,x"}).status_code, 400)

    def test_invalid_filter_value(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {"filecount_from": "lots"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("filecount_from", response.json()["error"])


class WantedGalleryAddFoundGalleriesTests(TestCase):
    def setUp(self):
        self.wanted = WantedGallery.objects.create(title="Wanted", search_title="wanted search", public=True)
        self.url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        self.galleries = [GalleryFactory.create(title=f"Candidate {i}") for i in range(3)]
        self.deleted = GalleryFactory.create(title="Deleted", status=Gallery.StatusChoices.DELETED)
        GalleryMatch.objects.create(wanted_gallery=self.wanted, gallery=self.galleries[0], match_accuracy=0.9)
        GalleryMatch.objects.create(wanted_gallery=self.wanted, gallery=self.galleries[2], match_accuracy=0.5)

    def post(self, gallery_ids: str):
        return self.client.post(self.url, {"tool": "add-found-galleries", "gallery_ids": gallery_ids}, follow=True)

    def messages(self, response) -> list[str]:
        return [str(m) for m in response.context["messages"]]

    def test_marks_galleries_as_found(self):
        self.client.force_login(make_user("changer", "change_wantedgallery"))
        a, b, _ = self.galleries
        response = self.post(f"{a.pk}, /gallery/{b.pk}/ 999999")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(FoundGallery.objects.filter(wanted_gallery=self.wanted).values_list("gallery_id", flat=True)),
            {a.pk, b.pk},
        )
        # Galleries marked as found leave the possible matches.
        self.assertEqual(
            list(GalleryMatch.objects.filter(wanted_gallery=self.wanted).values_list("gallery_id", flat=True)),
            [self.galleries[2].pk],
        )
        self.wanted.refresh_from_db()
        self.assertTrue(self.wanted.found)
        self.assertIsNotNone(self.wanted.date_found)
        messages = self.messages(response)
        self.assertIn("Marked 2 galleries as found.", messages)
        self.assertIn("1 gallery could not be found or selected.", messages)

        response = self.post(f"{a.pk}")
        self.assertIn("1 gallery was already found.", self.messages(response))
        self.assertEqual(FoundGallery.objects.filter(wanted_gallery=self.wanted).count(), 2)

    def test_only_staff_can_add_non_normal_galleries(self):
        self.client.force_login(make_user("changer", "change_wantedgallery"))
        self.post(str(self.deleted.pk))
        self.assertFalse(FoundGallery.objects.filter(wanted_gallery=self.wanted).exists())

        self.client.force_login(make_user("staff", staff=True))
        self.post(str(self.deleted.pk))
        self.assertTrue(FoundGallery.objects.filter(wanted_gallery=self.wanted, gallery=self.deleted).exists())

    def test_invalid_or_empty_selection(self):
        self.client.force_login(make_user("staff", staff=True))
        self.assertIn("Select at least one gallery.", self.messages(self.post("")))
        self.assertTrue(any("is not a gallery id" in m for m in self.messages(self.post("nope"))))
        self.assertFalse(FoundGallery.objects.exists())
        self.wanted.refresh_from_db()
        self.assertFalse(self.wanted.found)

    def test_requires_change_permission(self):
        self.client.force_login(make_user("viewer", "view_wantedgallery"))
        self.post(str(self.galleries[0].pk))
        self.assertFalse(FoundGallery.objects.exists())

    def test_picker_shown_only_to_users_who_can_change(self):
        FoundGallery.objects.create(wanted_gallery=self.wanted, gallery=self.galleries[1])
        self.client.force_login(make_user("changer", "change_wantedgallery"))
        response = self.client.get(self.url)
        self.assertContains(response, 'data-chaika-widget="gallery-picker"')
        self.assertContains(response, f'data-marked-ids="{self.galleries[1].pk}"')
        self.assertContains(response, 'data-initial-title="wanted search"')

        self.client.force_login(make_user("viewer", "view_wantedgallery"))
        response = self.client.get(self.url)
        self.assertNotContains(response, 'data-chaika-widget="gallery-picker"')

    def test_select_as_match_still_works(self):
        self.client.force_login(make_user("staff", staff=True))
        self.client.post(self.url, {"tool": "select-as-match", "tool-id": self.galleries[0].pk})
        self.assertTrue(FoundGallery.objects.filter(wanted_gallery=self.wanted, gallery=self.galleries[0]).exists())
        self.assertFalse(GalleryMatch.objects.filter(wanted_gallery=self.wanted, gallery=self.galleries[0]).exists())
