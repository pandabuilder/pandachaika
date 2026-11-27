import json
import os
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    ArchiveManageEntry,
    ArchiveRecycleEntry,
    ArchiveTag,
    Tag,
    UserArchivePrefs,
)
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
from viewer.tests.factories import ArchiveFactory, GalleryFactory
from viewer.utils.dirbrowser import DirBrowser
from workers.models import TaskQueue

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
        for params in (
            {"filecount_from": "many"},
            {"created_from": "yesterday"},
            {"origin": "system"},
            {"archive_group": "my group"},
        ):
            with self.assertRaises(ValueError, msg=params):
                clean_filter(params, self.staff)
        self.assertEqual(clean_filter({"origin": "2"}, self.staff), {"origin": "2"})
        self.assertEqual(clean_filter({"archive_group": "7"}, self.manager), {"archive_group": "7"})
        self.assertEqual(clean_filter({"archive_group": "none"}, self.manager), {"archive_group": "none"})


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

    def test_gallery_with_other_archives(self):
        shared = GalleryFactory.create()
        first = ArchiveFactory.create(gallery=shared, title="First")
        second = ArchiveFactory.create(gallery=shared, title="Second")
        with_recycled_twin = ArchiveFactory.create(title="Recycled twin")
        ArchiveFactory.create(gallery=with_recycled_twin.gallery, title="Binned twin", binned=True)

        self.assertEqual(self.ids(filter_archives({"gallery-other-archives": "1"})), {first.pk, second.pk})
        # The binned twin itself shows up when listing the Recycle Bin, since its gallery has another archive.
        self.assertEqual(
            self.ids(filter_archives({"gallery-other-archives": "1", "recycled": "1"}, can_view_recycled=True)),
            {Archive.objects.get(title="Binned twin").pk},
        )

    def test_recycled_and_file_deleted_are_three_way(self):
        both = ArchiveFactory.create(gallery=None, title="Both", binned=True, file_deleted=True, crc32="DDDD0000")
        visible = {self.normal.pk, self.downloading.pk}

        def ids(**params):
            return self.ids(filter_archives(params, can_view_recycled=True))

        self.assertEqual(ids(recycled="0", file_deleted="0"), visible)
        self.assertEqual(ids(recycled="1", file_deleted="any"), {self.binned.pk, both.pk})
        self.assertEqual(ids(recycled="1", file_deleted="0"), {self.binned.pk})
        self.assertEqual(ids(recycled="any", file_deleted="0"), visible | {self.binned.pk})
        self.assertEqual(ids(recycled="any", file_deleted="1"), {self.deleted.pk, both.pk})
        self.assertEqual(
            ids(recycled="any", file_deleted="any"), visible | {self.binned.pk, self.deleted.pk, both.pk}
        )
        # Without the permission, "any" still hides them.
        self.assertEqual(
            self.ids(filter_archives({"recycled": "any", "file_deleted": "any"}, can_view_recycled=False)), visible
        )
        # The picker keeps "any" when cleaning the filter.
        staff = make_user("s", staff=True)
        self.assertEqual(clean_filter({"recycled": "any", "file_deleted": "0"}, staff), {"recycled": "any"})

    def test_filters_apply(self):
        self.assertEqual(self.ids(filter_archives({"title": "normal"})), {self.normal.pk})
        self.assertEqual(self.ids(filter_archives({"downloading": "1"})), {self.downloading.pk})
        self.assertEqual(self.ids(filter_archives({"filesize_from": "15"})), {self.downloading.pk})

    def test_archive_group(self):
        group = ArchiveGroup.objects.create(title="Group")
        other = ArchiveGroup.objects.create(title="Other")
        ArchiveGroupEntry.objects.create(archive_group=group, archive=self.normal)
        ArchiveGroupEntry.objects.create(archive_group=other, archive=self.normal)
        ArchiveGroupEntry.objects.create(archive_group=other, archive=self.binned)

        self.assertEqual(self.ids(filter_archives({"archive_group": str(group.pk)})), {self.normal.pk})
        # In two groups, but listed once.
        self.assertEqual(filter_archives({"archive_group": str(other.pk)}).count(), 1)
        self.assertEqual(
            self.ids(filter_archives({"archive_group": str(other.pk), "recycled": "any"}, can_view_recycled=True)),
            {self.normal.pk, self.binned.pk},
        )
        self.assertEqual(self.ids(filter_archives({"archive_group": "none"})), {self.downloading.pk})
        self.assertEqual(self.ids(filter_archives({"archive_group": ""})), {self.normal.pk, self.downloading.pk})

    def test_gallery_has_child(self):
        parent = GalleryFactory.create()
        old_version = ArchiveFactory.create(gallery=parent, title="Old version")
        GalleryFactory.create(parent_gallery=parent)
        # A gallery that is its own parent doesn't count.
        own_parent = GalleryFactory.create()
        own_parent.parent_gallery = own_parent
        own_parent.save()
        ArchiveFactory.create(gallery=own_parent, title="Own parent")
        ArchiveFactory.create(title="No children")

        self.assertEqual(self.ids(filter_archives({"gallery-has-child": "1"})), {old_version.pk})
        staff = make_user("s_child", staff=True)
        self.assertEqual(clean_filter({"gallery-has-child": "on"}, staff), {"gallery-has-child": "1"})

    def test_excluded_reason(self):
        self.normal.reason = "backup"
        self.normal.save()
        self.downloading.reason = "wanted"
        self.downloading.save()
        self.assertEqual(self.ids(filter_archives({"reason": "-backup"})), {self.downloading.pk})
        self.assertEqual(self.ids(filter_archives({"reason": "-backup,want"})), {self.downloading.pk})

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

    def test_json_listing_filters_by_archive_group_and_sends_groups_and_autocomplete(self):
        group = ArchiveGroup.objects.create(title="Group")
        grouped = ArchiveFactory.create(gallery=None, title="Grouped")
        ArchiveFactory.create(gallery=None, title="Loose")
        ArchiveGroupEntry.objects.create(archive_group=group, archive=grouped)
        url = reverse("viewer:archive-manager")

        # Without change_archivegroup: the groups are still sent, for the filter.
        self.client.force_login(make_user("manager", "manage_archive"))
        data = json.loads(self.client.get(url, {"json": "1", "archive_group": str(group.pk)}).content)
        self.assertEqual([r["id"] for r in data["results"]], [grouped.pk])
        self.assertEqual(data["groups"], [{"id": group.pk, "title": "Group"}])
        self.assertEqual(data["autocomplete"]["tags"], reverse("tag-json-autocomplete"))
        self.assertEqual(data["autocomplete"]["reason"], reverse("reason-json-autocomplete"))

        data = json.loads(self.client.get(url, {"json": "1", "archive_group": "none"}).content)
        self.assertEqual([r["title"] for r in data["results"]], ["Loose"])

    def test_json_listing_sends_the_newer_versions_of_the_gallery(self):
        parent = GalleryFactory.create(filesize=100, filecount=5)
        old_version = ArchiveFactory.create(gallery=parent, title="Old version")
        child = GalleryFactory.create(parent_gallery=parent, title="New version")
        new_archive = ArchiveFactory.create(gallery=child, title="New archive")
        binned = ArchiveFactory.create(gallery=child, title="Binned new archive", binned=True)
        url = reverse("viewer:archive-manager")

        self.client.force_login(make_user("manager", "manage_archive"))
        data = json.loads(self.client.get(url, {"json": "1", "gallery-has-child": "1"}).content)
        self.assertEqual([r["id"] for r in data["results"]], [old_version.pk])
        gallery = data["results"][0]["gallery"]
        self.assertEqual((gallery["filesize"], gallery["filecount"], gallery["provider"]), (100, 5, "panda"))
        [child_json] = gallery["children"]
        self.assertEqual((child_json["id"], child_json["title"]), (child.pk, "New version"))
        self.assertEqual(
            [(a["id"], a["binned"]) for a in child_json["archives"]], [(new_archive.pk, False), (binned.pk, True)]
        )

        data = json.loads(self.client.get(url, {"json": "1", "title": "New archive"}).content)
        self.assertEqual(data["results"][0]["gallery"]["children"], [])

    def test_run_for_all_uses_the_listing_filters(self):
        shared = GalleryFactory.create()
        twins = [ArchiveFactory.create(gallery=shared, title=f"Twin {n}") for n in (1, 2)]
        single = ArchiveFactory.create(title="Single")
        staff = make_user("staff", "manage_archive", "change_archive", staff=True)
        self.client.force_login(staff)

        url = reverse("viewer:archive-manager") + "?json=1&gallery-other-archives=1"
        resp = self.client.post(url, {"change_archive_reason": "1", "archive_reason": "dupe", "run_for_all": "1"})
        body = json.loads(resp.content)
        self.assertEqual(len(body["task_ids"]), 1)
        task = TaskQueue.objects.get(pk=body["task_ids"][0])
        self.assertEqual(sorted(task.args), sorted([t.pk for t in twins]))

        from core.base.setup import Settings
        from workers.runner import WorkerRunner

        task.status = TaskQueue.Status.PROCESSING
        task.save(update_fields=["status"])
        WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False).execute_job(task)

        self.assertEqual({a.reason for a in Archive.objects.filter(pk__in=[t.pk for t in twins])}, {"dupe"})
        single.refresh_from_db()
        self.assertNotEqual(single.reason, "dupe")

    def test_only_favorites_uses_the_current_users_favorites(self):
        mine = ArchiveFactory.create(gallery=None, title="Mine")
        theirs = ArchiveFactory.create(gallery=None, title="Theirs")
        ArchiveFactory.create(gallery=None, title="Nobody's")
        manager = make_user("manager", "manage_archive")
        other = make_user("other", "manage_archive")
        UserArchivePrefs.objects.create(user=manager, archive=mine, favorite_group=1)
        UserArchivePrefs.objects.create(user=manager, archive=theirs, favorite_group=0)  # Not a favorite.
        UserArchivePrefs.objects.create(user=other, archive=theirs, favorite_group=1)
        url = reverse("viewer:archive-manager")

        self.client.force_login(manager)
        data = json.loads(self.client.get(url, {"json": "1", "only_favorites": "1"}).content)
        self.assertEqual([r["id"] for r in data["results"]], [mine.pk])

        self.client.force_login(other)
        data = json.loads(self.client.get(url, {"json": "1", "only_favorites": "1"}).content)
        self.assertEqual([r["id"] for r in data["results"]], [theirs.pk])

    def test_extract_context_info_and_action(self):
        archive = ArchiveFactory.create(gallery=None, title="Extract me")
        url = reverse("viewer:archive-manager")
        extractor = settings.CRAWLER_SETTINGS.manga_extractor
        plain = make_user("plain", "manage_archive")
        extractor_user = make_user("extractor", "manage_archive", "extract_contextual_info")
        action = {"extract_contextual_info": "1", f"sel-{archive.pk}": str(archive.pk), "profile": "default"}

        with patch.object(extractor, "enable", False):
            self.client.force_login(extractor_user)
            data = json.loads(self.client.get(url, {"json": "1"}).content)
            self.assertEqual(data["manga_extractor"], {"enabled": False, "profiles": [], "default_profile": ""})
            response = self.client.post(url + "?json=1", action)
            self.assertEqual(response.status_code, 400)

        with patch.object(extractor, "enable", True):
            self.client.force_login(plain)
            data = json.loads(self.client.get(url, {"json": "1"}).content)
            self.assertFalse(data["permissions"]["extract_contextual_info"])
            self.assertEqual(data["manga_extractor"]["profiles"], [])

            self.client.force_login(extractor_user)
            data = json.loads(self.client.get(url, {"json": "1"}).content)
            self.assertTrue(data["permissions"]["extract_contextual_info"])
            self.assertTrue(data["manga_extractor"]["enabled"])
            self.assertIn("default", [p["key"] for p in data["manga_extractor"]["profiles"]])

            self.client.post(url + "?json=1", action)
            task = TaskQueue.objects.get(task_type="contextual_extractor_worker")
            self.assertEqual(task.args, [archive.pk])
            self.assertEqual(task.options["manga_extractor_profile"], "default")

    def test_delete_files_action_queues_delete_zipped(self):
        archive = ArchiveFactory.create(gallery=None, title="To delete")
        url = reverse("viewer:archive-manager") + "?json=1"
        data = {
            "delete_files_archives": "1",
            f"sel-{archive.pk}": str(archive.pk),
            "mark_delete_galleries": "1",
            "reason": "duplicate",
        }

        self.client.force_login(make_user("manager", "manage_archive"))
        self.client.post(url, data)
        self.assertFalse(TaskQueue.objects.filter(task_type="delete_archive_worker").exists())

        self.client.force_login(make_user("deleter", "manage_archive", "delete_archive", "mark_delete_gallery"))
        self.client.post(url, data)
        task = TaskQueue.objects.get(task_type="delete_archive_worker")
        self.assertEqual(task.args, [archive.pk])
        self.assertEqual(task.reason, "duplicate")
        self.assertTrue(task.options["delete_zipped"])
        self.assertFalse(task.options["delete_archive"])
        self.assertFalse(task.options["delete_file"])
        self.assertTrue(task.options["mark_gallery_deleted"])
        self.assertFalse(task.options["delete_gallery"])
        self.assertTrue(Archive.objects.filter(pk=archive.pk).exists())

    def test_delete_zipped_worker_keeps_archive_and_recycles_it(self):
        from core.base.setup import Settings
        from core.workers.archive_work import ArchiveWorker

        user = make_user("deleter", "manage_archive", "delete_archive")
        archive = ArchiveFactory.create(gallery=None, title="To delete")
        ArchiveWorker(Settings(load_from_disk=False), 4).delete_archive(
            archive_id=archive.pk, delete_zipped=True, user_reason="duplicate", user=user, force_local=True
        )

        archive.refresh_from_db()
        self.assertTrue(archive.binned)
        self.assertTrue(archive.file_deleted)
        self.assertEqual(archive.recycle_entry.reason, "duplicate")
        self.assertEqual(archive.recycle_entry.user, user)

    def test_json_listing_includes_recycle_info_for_recyclers(self):
        recycler = make_user("recycler", "manage_archive", "recycle_archive")
        binned = ArchiveFactory.create(gallery=None, title="Binned", binned=True)
        ArchiveRecycleEntry.objects.create(archive=binned, reason="duplicate", user=recycler)
        url = reverse("viewer:archive-manager")

        self.client.force_login(recycler)
        data = json.loads(self.client.get(url, {"json": "1", "recycled": "1"}).content)
        [result] = data["results"]
        self.assertTrue(result["binned"])
        self.assertFalse(result["file_deleted"])
        self.assertEqual(result["recycle_entry"]["reason"], "duplicate")
        self.assertEqual(result["recycle_entry"]["user"], "recycler")
        self.assertIsNotNone(result["recycle_entry"]["date"])

    def test_json_listing_without_recycle_entry_row(self):
        ArchiveFactory.create(gallery=None, title="Binned", binned=True)
        self.client.force_login(make_user("recycler", "manage_archive", "recycle_archive"))
        data = json.loads(
            self.client.get(reverse("viewer:archive-manager"), {"json": "1", "recycled": "1"}).content
        )
        self.assertIsNone(data["results"][0]["recycle_entry"])

    def test_json_listing_marks_require_view_marks(self):
        archive = ArchiveFactory.create(gallery=None, title="Marked")
        ArchiveManageEntry.objects.create(archive=archive, mark_reason="duplicate", mark_priority=1)
        ArchiveManageEntry.objects.create(
            archive=archive, mark_reason="hidden", status=ArchiveManageEntry.StatusChoices.NOT_INDEXED
        )
        url = reverse("viewer:archive-manager")

        self.client.force_login(make_user("manager", "manage_archive"))
        data = json.loads(self.client.get(url, {"json": "1"}).content)
        self.assertEqual(data["results"][0]["manage_entries"], [])

        self.client.force_login(make_user("viewer", "manage_archive", "view_marks"))
        data = json.loads(self.client.get(url, {"json": "1"}).content)
        self.assertEqual([m["mark_reason"] for m in data["results"][0]["manage_entries"]], ["duplicate"])


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
