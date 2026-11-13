import json
import os
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from viewer.models import ArchiveManageEntry, ArchiveTag, Tag
from viewer.services.archive_selection import (
    ADMIN_FILTERS,
    ALL_FILTERS,
    MARK_FILTERS,
    allowed_filter_keys,
    archives_matching,
    can_use_archive_picker,
    clean_filter,
    filter_archives,
)
from viewer.tests.factories import ArchiveFactory
from viewer.utils.dirbrowser import DirBrowser

User = get_user_model()


def make_user(username: str, *perms: str, staff: bool = False):
    user = User.objects.create_user(username=username, password="password", is_staff=staff)
    for perm in perms:
        user.user_permissions.add(Permission.objects.get(codename=perm, content_type__app_label="viewer"))
    return User.objects.get(pk=user.pk)  # Fresh object, so the permission cache sees the new permissions.


class FilterPermissionTests(TestCase):
    def setUp(self):
        self.staff = make_user("staff", staff=True)
        self.manager = make_user("manager", "manage_archive")
        self.plain = make_user("plain")

    def test_who_may_use_the_picker(self):
        self.assertTrue(can_use_archive_picker(self.staff))
        self.assertTrue(can_use_archive_picker(self.manager))
        self.assertFalse(can_use_archive_picker(self.plain))
        self.assertFalse(can_use_archive_picker(None))

    def test_allowed_filters_by_profile(self):
        self.assertEqual(allowed_filter_keys(self.staff), ALL_FILTERS)
        manager_keys = set(allowed_filter_keys(self.manager))
        self.assertFalse(manager_keys & MARK_FILTERS)
        self.assertFalse(manager_keys & ADMIN_FILTERS)
        self.assertIn("title", manager_keys)
        self.assertIn("diff-filesize", manager_keys)
        self.assertEqual(allowed_filter_keys(self.plain), [])

    def test_clean_filter_keeps_allowed_and_valid_values(self):
        params = {
            "title": "  foo ",
            "public": "on",
            "hidden": "0",
            "recycled": "1",
            "mark_reason": "x",
            "filecount_from": "3",
            "unknown": "x",
            "sort": "title",
            "asc_desc": "asc",
            "tags": "",
        }
        self.assertEqual(
            clean_filter(params, self.staff),
            {
                "title": "foo",
                "public": "1",
                "recycled": "1",
                "mark_reason": "x",
                "filecount_from": "3",
                "sort": "title",
                "asc_desc": "asc",
            },
        )
        self.assertEqual(
            clean_filter(params, self.manager),
            {"title": "foo", "public": "1", "filecount_from": "3", "sort": "title", "asc_desc": "asc"},
        )
        self.assertEqual(clean_filter({"sort": "password", "asc_desc": "sideways"}, self.staff), {})
        self.assertEqual(clean_filter({"public": True, "hidden": False}, self.staff), {"public": "1"})

    def test_clean_filter_rejects_invalid_values(self):
        for params in ({"filecount_from": "many"}, {"created_from": "yesterday"}, {"origin": "system"}):
            with self.assertRaises(ValueError, msg=params):
                clean_filter(params, self.staff)
        self.assertEqual(clean_filter({"origin": "2"}, self.staff), {"origin": "2"})


class FilterArchivesTests(TestCase):
    def setUp(self):
        self.user = make_user("owner", staff=True)
        self.normal = ArchiveFactory.create(gallery=None, title="Normal one", crc32="AAAA0000", filesize=10)
        self.downloading = ArchiveFactory.create(gallery=None, title="Downloading one", crc32="", filesize=20)
        self.binned = ArchiveFactory.create(gallery=None, title="Binned one", binned=True, crc32="BBBB0000")
        self.deleted = ArchiveFactory.create(gallery=None, title="Deleted one", file_deleted=True, crc32="CCCC0000")

    def ids(self, queryset) -> set[int]:
        return set(queryset.values_list("pk", flat=True))

    def test_binned_and_deleted_only_when_asked_and_allowed(self):
        self.assertEqual(self.ids(filter_archives({})), {self.normal.pk, self.downloading.pk})
        self.assertEqual(self.ids(filter_archives({"recycled": "1"}, can_view_recycled=True)), {self.binned.pk})
        self.assertEqual(self.ids(filter_archives({"file_deleted": "1"}, can_view_recycled=True)), {self.deleted.pk})
        self.assertEqual(
            self.ids(filter_archives({"recycled": "1"}, can_view_recycled=False)), {self.normal.pk, self.downloading.pk}
        )

    def test_filters_apply(self):
        self.assertEqual(self.ids(filter_archives({"title": "normal"})), {self.normal.pk})
        self.assertEqual(self.ids(filter_archives({"downloading": "1"})), {self.downloading.pk})
        self.assertEqual(self.ids(filter_archives({"filesize_from": "15"})), {self.downloading.pk})

    def test_archives_matching_counts_annotated_and_joined_filters(self):
        tag = Tag.objects.create(name="custom", scope="")
        ArchiveTag.objects.create(archive=self.normal, tag=tag, origin=ArchiveTag.ORIGIN_USER)
        ArchiveManageEntry.objects.create(archive=self.normal, mark_reason="dup", mark_check=True, mark_priority=3)
        ArchiveManageEntry.objects.create(archive=self.normal, mark_reason="dup2", mark_check=True, mark_priority=4)

        matching = archives_matching({"with-custom-tags": "1", "mark_reason": "dup", "sort": "title"})
        self.assertEqual(self.ids(matching), {self.normal.pk})
        self.assertEqual(matching.count(), 1)
        self.assertEqual(archives_matching({"marked": "1"}).count(), 1)
        self.assertEqual(archives_matching({"no-custom-tags": "1"}).count(), 1)

    def test_flags_report_mark_and_extra_file_filters(self):
        flags: dict[str, bool] = {}
        filter_archives({"mark_reason": "x"}, flags=flags)
        self.assertEqual(flags, {"mark_filters": True, "extra_file_filters": False})


class ArchivePickerApiTests(TestCase):
    def setUp(self):
        self.url = reverse("viewer:api-archive-picker")
        self.staff = make_user("staff", staff=True)
        self.manager = make_user("manager", "manage_archive")
        self.plain = make_user("plain")
        self.archives = [ArchiveFactory.create(gallery=None, title=f"Picker {i}", zipped=f"a/{i}.zip") for i in range(25)]
        self.binned = ArchiveFactory.create(gallery=None, title="Binned picker", binned=True)

    def test_requires_permission(self):
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_staff_listing_with_filters_and_pagination(self):
        self.client.force_login(self.staff)
        data = self.client.get(self.url, {"title": "picker", "size": "10", "page": "2", "sort": "title"}).json()
        self.assertEqual(data["count"], 25)
        self.assertEqual(data["num_pages"], 3)
        self.assertEqual(data["number"], 2)
        self.assertEqual(len(data["results"]), 10)
        self.assertTrue(data["is_staff"])
        self.assertIn("recycled", data["allowed_filters"])
        self.assertEqual(data["filter"], {"title": "picker", "sort": "title"})
        self.assertIn("file", data["results"][0])

        data = self.client.get(self.url, {"recycled": "1"}).json()
        self.assertEqual([r["id"] for r in data["results"]], [self.binned.pk])

    def test_manager_gets_less(self):
        self.client.force_login(self.manager)
        data = self.client.get(self.url, {"recycled": "1", "mark_reason": "x"}).json()
        self.assertFalse(data["is_staff"])
        self.assertNotIn("recycled", data["allowed_filters"])
        self.assertNotIn("mark_reason", data["allowed_filters"])
        # Filters the user may not use are ignored, so the binned archive isn't listed.
        self.assertEqual(data["filter"], {})
        self.assertEqual(data["count"], 25)
        self.assertNotIn("file", data["results"][0])

    def test_ids_lookup(self):
        self.client.force_login(self.manager)
        wanted = [self.archives[3].pk, 999999, self.archives[1].pk]
        data = self.client.get(self.url, {"ids": ",".join(str(x) for x in wanted)}).json()
        self.assertEqual([r["id"] for r in data["results"]], [self.archives[3].pk, self.archives[1].pk])
        self.assertEqual(data["missing_ids"], [999999])
        self.assertEqual(self.client.get(self.url, {"ids": "1,x"}).status_code, 400)

    def test_invalid_filter_value(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url, {"filecount_from": "lots"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("filecount_from", response.json()["error"])


class ArchiveManagerListingTests(TestCase):
    """The archive manager listing uses the shared filters."""

    def test_json_listing_respects_recycle_permission(self):
        binned = ArchiveFactory.create(gallery=None, title="Binned", binned=True)
        ArchiveFactory.create(gallery=None, title="Normal")
        url = reverse("viewer:archive-manager")

        self.client.force_login(make_user("manager", "manage_archive"))
        data = json.loads(self.client.get(url, {"json": "1", "recycled": "1"}).content)
        self.assertEqual([r["title"] for r in data["results"]], ["Normal"])

        self.client.force_login(make_user("recycler", "manage_archive", "recycle_archive"))
        data = json.loads(self.client.get(url, {"json": "1", "recycled": "1"}).content)
        self.assertEqual([r["id"] for r in data["results"]], [binned.pk])


class DirBrowserTests(TestCase):
    def test_stays_inside_the_root(self):
        parent = tempfile.mkdtemp()
        root = os.path.join(parent, "media")
        sibling = os.path.join(parent, "media_other")
        os.makedirs(os.path.join(root, "sub"))
        os.makedirs(sibling)
        browser = DirBrowser(root)
        self.assertEqual(browser.relativepath("sub"), "sub")
        self.assertEqual(browser.relativepath("../media_other"), ".")
        self.assertEqual(browser.relativepath("../../etc"), ".")

    def test_dirs_only_listing(self):
        root = tempfile.mkdtemp()
        os.makedirs(os.path.join(root, "folder"))
        open(os.path.join(root, "file.zip"), "w").close()
        user = make_user("staff_browser", staff=True)
        self.client.force_login(user)
        with self.settings(MEDIA_ROOT=root):
            full = self.client.get(reverse("viewer:directory-parser")).json()
            dirs = self.client.get(reverse("viewer:directory-parser"), {"dirs_only": "1"}).json()
        self.assertEqual(full, [".", [["folder", True], ["file.zip", False]]])
        self.assertEqual(dirs, [".", [["folder", True]]])


class PickerAutocompleteTests(TestCase):
    """The picker's filter fields autocomplete with the native (database) endpoints."""

    def setUp(self):
        self.staff = make_user("staff_ac", staff=True)
        self.manager = make_user("manager_ac", "manage_archive")
        self.archive = ArchiveFactory.create(title="Auto", source_type="chaika", reason="backup")
        ArchiveFactory.create(title="Empty source", source_type="", reason="wanted")
        ArchiveManageEntry.objects.create(archive=self.archive, mark_reason="duplicate", mark_check=True)

    def test_picker_api_lists_autocompletes_for_allowed_filters(self):
        url = reverse("viewer:api-archive-picker")
        self.client.force_login(self.staff)
        autocomplete = self.client.get(url).json()["autocomplete"]
        self.assertEqual(
            set(autocomplete), {"tags", "source_type", "reason", "provider", "category", "mark_reason"}
        )
        self.assertEqual(autocomplete["tags"], reverse("tag-json-autocomplete"))
        self.assertNotIn("es-autocomplete", "".join(autocomplete.values()))

        self.client.force_login(self.manager)
        self.assertNotIn("mark_reason", self.client.get(url).json()["autocomplete"])

    def test_json_endpoints(self):
        self.client.force_login(self.manager)

        def suggestions(name: str, q: str) -> list[str]:
            data = self.client.get(reverse(name), {"q": q}).json()
            return [result["text"] for result in data["results"]]

        self.assertEqual(suggestions("source-json-autocomplete", "cha"), ["chaika"])
        # Empty values aren't suggested.
        self.assertNotIn("", suggestions("source-json-autocomplete", ""))
        self.assertEqual(suggestions("reason-json-autocomplete", "back"), ["backup"])
        self.assertEqual(suggestions("provider-json-autocomplete", "pan"), ["panda"])
        # Marks are only for staff (or users that can view them).
        self.assertEqual(suggestions("archive-manager-reason-json-autocomplete", "dup"), [])
        self.client.force_login(self.staff)
        self.assertEqual(suggestions("archive-manager-reason-json-autocomplete", "dup"), ["duplicate"])
