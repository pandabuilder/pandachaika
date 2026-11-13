import io
import json
import os
import shutil
import tempfile
import zipfile
import zlib

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from core.base.cancellation import TaskCancelledError
from core.base.setup import Settings
from viewer.models import Archive, ArchiveFileCheck, ArchiveFileCheckIssue
from viewer.services.archive_file_check import (
    TASK_TYPE,
    create_file_check,
    crc32_matches,
    normalize_folder,
    parse_archive_ids,
    run_file_check,
)
from viewer.tests.factories import ArchiveFactory
from workers.models import TaskQueue
from workers.runner import WorkerRunner

User = get_user_model()


class ParseInputTests(TestCase):
    def test_parse_archive_ids(self):
        self.assertEqual(parse_archive_ids("1, 2 3\n5-7;2"), [1, 2, 3, 5, 6, 7])
        self.assertEqual(parse_archive_ids("9-8"), [8, 9])
        self.assertEqual(parse_archive_ids("https://example.com/archive/42/ 43"), [42, 43])
        self.assertEqual(parse_archive_ids("  "), [])
        with self.assertRaises(ValueError):
            parse_archive_ids("12 abc")
        with self.assertRaises(ValueError):
            parse_archive_ids("1-1000000")

    def test_normalize_folder(self):
        root = "/srv/media"
        self.assertEqual(normalize_folder("galleries/2024/", root), "galleries/2024")
        self.assertEqual(normalize_folder("/srv/media/galleries/x", root), "galleries/x")
        self.assertEqual(normalize_folder("a/./b/../c", root), "a/c")
        for bad in ("", "  ", ".", "..", "../etc", "a/../../b", "/etc/passwd", "/srv/media"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_folder(bad, root)


class FileCheckTestBase(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="filecheck_test_")
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.user = User.objects.create_superuser(username="admin_fc", password="password", email="a@example.com")

    @staticmethod
    def zip_bytes(image: bytes, compress: bool = False) -> bytes:
        buffer = io.BytesIO()
        method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        with zipfile.ZipFile(buffer, "w", compression=method) as zf:
            zf.writestr("001.jpg", image)
            zf.writestr("info.txt", b"not an image, not counted in the image size")
        return buffer.getvalue()

    def make_archive(
        self, relative_path: str | None, content: bytes = b"data", compress: bool = False, **kwargs
    ) -> Archive:
        """An archive whose file is a zip with one image, ``content``.

        Like ``Archive.recalc_fileinfo``: ``filesize`` is the uncompressed size of the images, ``crc32`` the file's.
        """
        data = self.zip_bytes(content, compress)
        values = {"crc32": "%08X" % (zlib.crc32(data) & 0xFFFFFFFF), "filesize": len(content)}
        values.update(kwargs)
        archive: Archive = ArchiveFactory.create(gallery=None, **values)
        if relative_path is not None:
            full = os.path.join(self.media_root, relative_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
            Archive.objects.filter(pk=archive.pk).update(zipped=relative_path)
            archive.refresh_from_db()
        return archive

    def issues_by_archive(self, check: ArchiveFileCheck) -> dict[int, str]:
        return dict(check.issues.values_list("archive_id", "kind"))


class RunFileCheckTests(FileCheckTestBase):
    def test_presence_check_finds_missing_and_size_mismatch(self):
        ok = self.make_archive("galleries/ok.zip")
        missing = self.make_archive("galleries/missing.zip")
        os.remove(missing.zipped.path)
        wrong_size = self.make_archive("galleries/size.zip")
        Archive.objects.filter(pk=wrong_size.pk).update(filesize=999)
        corrupted = self.make_archive("galleries/crc.zip")
        Archive.objects.filter(pk=corrupted.pk).update(crc32="DEADBEEF")
        no_path = self.make_archive(None)
        deleted = self.make_archive("galleries/deleted.zip", file_deleted=True)
        os.remove(deleted.zipped.path)
        binned = self.make_archive("galleries/binned.zip", binned=True)
        os.remove(binned.zipped.path)

        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        check = run_file_check(check.pk)

        self.assertEqual(check.status, ArchiveFileCheck.Status.COMPLETED)
        self.assertEqual(check.total, 5)
        self.assertEqual(check.checked, 5)
        # Without CRC32 the corrupted file only looks fine.
        self.assertEqual(check.ok_count, 2)
        self.assertEqual(check.missing_count, 2)
        self.assertEqual(check.size_mismatch_count, 1)
        self.assertEqual(
            self.issues_by_archive(check),
            {
                missing.pk: ArchiveFileCheckIssue.Kind.MISSING,
                no_path.pk: ArchiveFileCheckIssue.Kind.MISSING,
                wrong_size.pk: ArchiveFileCheckIssue.Kind.SIZE_MISMATCH,
            },
        )
        size_issue = check.issues.get(archive=wrong_size)
        self.assertEqual((size_issue.expected_size, size_issue.actual_size), (999, 4))
        self.assertNotIn(ok.pk, self.issues_by_archive(check))

    def test_crc32_check_finds_corrupted_files(self):
        self.make_archive("a/ok.zip", b"x" * 10_000)
        corrupted = self.make_archive("a/bad.zip", b"y" * 5000)
        with open(corrupted.zipped.path, "r+b") as f:
            # Flip a byte of the image data: same size, and the zip's file list still matches.
            offset = f.read().index(b"y" * 100) + 50
            f.seek(offset)
            f.write(b"z")
        no_crc = self.make_archive("a/nocrc.zip", crc32="")

        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, check_crc32=True, user=self.user)
        check = run_file_check(check.pk)

        self.assertEqual(check.status, ArchiveFileCheck.Status.COMPLETED)
        self.assertEqual(check.crc32_mismatch_count, 1)
        self.assertEqual(check.ok_count, 2)
        self.assertEqual(check.no_crc32_count, 1)
        self.assertEqual(check.bytes_total, 10_000 + 5000 + 4)
        self.assertEqual(check.bytes_done, check.bytes_total)
        issue = check.issues.get()
        self.assertEqual(issue.archive_id, corrupted.pk)
        self.assertEqual(issue.kind, ArchiveFileCheckIssue.Kind.CRC32_MISMATCH)
        self.assertEqual(issue.expected_crc32, corrupted.crc32)
        with open(corrupted.zipped.path, "rb") as f:
            self.assertEqual(issue.actual_crc32, "%08X" % (zlib.crc32(f.read()) & 0xFFFFFFFF))
        self.assertEqual(check.progress_dict()["percent"], 100.0)
        self.assertIsNotNone(no_crc)

    def test_image_size_is_compared_not_the_size_on_disk(self):
        """Archive.filesize is the uncompressed size of the images, which differs from the size of a compressed zip."""
        compressed = self.make_archive("z/compressed.zip", b"x" * 20_000, compress=True)
        self.assertLess(os.path.getsize(compressed.zipped.path), compressed.filesize)
        changed = self.make_archive("z/changed.zip", b"y" * 300, compress=True)
        Archive.objects.filter(pk=changed.pk).update(filesize=200)
        rar = self.make_archive("z/rar.cbr")
        with open(rar.zipped.path, "wb") as f:
            f.write(b"Rar!\x1a\x07\x00 not a zip")
        # A file that couldn't be read as a zip when its info was calculated has a filesize of -1.
        Archive.objects.filter(pk=rar.pk).update(filesize=-1)
        broken = self.make_archive("z/broken.zip", b"w" * 1000)
        with open(broken.zipped.path, "wb") as f:
            f.write(b"truncated garbage")

        check = run_file_check(create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user).pk)

        self.assertEqual(check.ok_count, 2)
        self.assertEqual(
            self.issues_by_archive(check),
            {changed.pk: ArchiveFileCheckIssue.Kind.SIZE_MISMATCH, broken.pk: ArchiveFileCheckIssue.Kind.ERROR},
        )
        size_issue = check.issues.get(archive=changed)
        self.assertEqual((size_issue.expected_size, size_issue.actual_size), (200, 300))
        self.assertIn("can't be read as a zip", check.issues.get(archive=broken).message)

    def test_crc32_check_falls_back_to_image_size_without_stored_crc32(self):
        same = self.make_archive("n/same.zip", b"a" * 100, compress=True, crc32="")
        changed = self.make_archive("n/changed.zip", b"b" * 100, compress=True, crc32="")
        Archive.objects.filter(pk=changed.pk).update(filesize=50)
        # With a stored CRC32 only the CRC32 counts, even if the stored image size is off.
        crc_ok = self.make_archive("n/crc_ok.zip", b"c" * 100, compress=True)
        Archive.objects.filter(pk=crc_ok.pk).update(filesize=1)

        check = run_file_check(
            create_file_check(scope=ArchiveFileCheck.Scope.ALL, check_crc32=True, user=self.user).pk
        )

        self.assertEqual(check.no_crc32_count, 2)
        self.assertEqual(check.ok_count, 2)
        self.assertEqual(self.issues_by_archive(check), {changed.pk: ArchiveFileCheckIssue.Kind.SIZE_MISMATCH})
        self.assertIsNotNone(same)

    def test_folder_scope_only_checks_archives_in_those_folders(self):
        inside = self.make_archive("galleries/2024/a.zip")
        nested = self.make_archive("galleries/2024/sub/b.zip")
        outside = self.make_archive("galleries/2025/c.zip")
        prefix_trap = self.make_archive("galleries/2024-old/d.zip")
        for archive in (inside, nested, outside, prefix_trap):
            os.remove(archive.zipped.path)

        check = create_file_check(scope=ArchiveFileCheck.Scope.FOLDERS, folders=["galleries/2024"], user=self.user)
        check = run_file_check(check.pk)

        self.assertEqual(check.total, 2)
        self.assertEqual(set(self.issues_by_archive(check)), {inside.pk, nested.pk})

    def test_archive_scope_includes_binned_and_deleted(self):
        binned = self.make_archive("x/binned.zip", binned=True)
        deleted = self.make_archive("x/deleted.zip", file_deleted=True)
        other = self.make_archive("x/other.zip")
        os.remove(binned.zipped.path)

        check = create_file_check(
            scope=ArchiveFileCheck.Scope.ARCHIVES, archive_ids=[binned.pk, deleted.pk], user=self.user
        )
        check = run_file_check(check.pk)

        self.assertEqual(check.total, 2)
        self.assertEqual(check.ok_count, 1)
        self.assertEqual(self.issues_by_archive(check), {binned.pk: ArchiveFileCheckIssue.Kind.MISSING})
        self.assertNotIn(other.pk, self.issues_by_archive(check))

    def test_rerun_after_requeue_starts_over(self):
        missing = self.make_archive("y/m.zip")
        os.remove(missing.zipped.path)
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        run_file_check(check.pk)
        check = run_file_check(check.pk)
        self.assertEqual(check.missing_count, 1)
        self.assertEqual(check.checked, 1)
        self.assertEqual(check.issues.count(), 1)

    def test_cancellation_stops_and_marks_cancelled(self):
        for i in range(5):
            self.make_archive(f"c/{i}.zip")
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        calls = {"n": 0}

        def is_cancelled() -> bool:
            calls["n"] += 1
            return calls["n"] > 2

        with self.assertRaises(TaskCancelledError):
            run_file_check(check.pk, is_cancelled=is_cancelled)
        check.refresh_from_db()
        self.assertEqual(check.status, ArchiveFileCheck.Status.CANCELLED)
        self.assertEqual(check.checked, 2)
        self.assertIsNotNone(check.finished_at)

    def test_run_stops_when_cancelled_from_the_page(self):
        """Cancelling the run itself (not only its task) stops the worker at the next progress save."""
        from unittest.mock import patch

        from viewer.services import archive_file_check as service

        for i in range(5):
            self.make_archive(f"d/{i}.zip")
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        original = service._CheckRunner.check_archive

        def cancel_after_second(runner, archive):
            original(runner, archive)
            if runner.counters["checked"] == 2:
                ArchiveFileCheck.objects.filter(pk=check.pk).update(status=ArchiveFileCheck.Status.CANCELLED)

        with patch.object(service, "PROGRESS_FLUSH_SECONDS", 0), patch.object(
            service._CheckRunner, "check_archive", cancel_after_second
        ):
            with self.assertRaises(TaskCancelledError):
                run_file_check(check.pk)
        check.refresh_from_db()
        self.assertEqual(check.status, ArchiveFileCheck.Status.CANCELLED)
        self.assertEqual(check.checked, 2)

    def test_create_validates_scope(self):
        with self.assertRaises(ValueError):
            create_file_check(scope="nope")
        with self.assertRaises(ValueError):
            create_file_check(scope=ArchiveFileCheck.Scope.ARCHIVES, archive_ids=[])
        with self.assertRaises(ValueError):
            create_file_check(scope=ArchiveFileCheck.Scope.FOLDERS, folders=[])


class WorkerTaskTests(FileCheckTestBase):
    def test_worker_runs_the_check_task(self):
        missing = self.make_archive("w/m.zip")
        os.remove(missing.zipped.path)
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        task = check.task
        assert task is not None
        self.assertEqual(task.task_type, TASK_TYPE)
        self.assertEqual(task.options.get("check_id"), check.pk)

        TaskQueue.objects.filter(pk=task.pk).update(status=TaskQueue.Status.PROCESSING)
        task.refresh_from_db()
        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        runner.execute_job(task)

        task.refresh_from_db()
        check.refresh_from_db()
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertEqual(check.status, ArchiveFileCheck.Status.COMPLETED)
        self.assertEqual(check.missing_count, 1)

    def test_sync_with_task_finishes_orphaned_runs(self):
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        assert check.task is not None
        TaskQueue.objects.filter(pk=check.task.pk).update(status=TaskQueue.Status.CANCELLED)
        check.refresh_from_db()
        self.assertTrue(check.sync_with_task())
        self.assertEqual(check.status, ArchiveFileCheck.Status.CANCELLED)

        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        assert check.task is not None
        TaskQueue.objects.filter(pk=check.task.pk).update(status=TaskQueue.Status.FAILED, error_message="boom")
        check.refresh_from_db()
        check.sync_with_task()
        self.assertEqual(check.status, ArchiveFileCheck.Status.FAILED)
        self.assertEqual(check.error_message, "boom")


class FileCheckViewTests(FileCheckTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("viewer:archive-file-check")

    def test_old_url_redirects(self):
        response = self.client.get("/archive-not-present/?title=x")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(self.url))

    def test_page_renders_without_checks(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No file checks yet")

    def test_requires_staff(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ArchiveFileCheck.objects.count(), 0)

    def test_start_check_enqueues_task(self):
        os.makedirs(os.path.join(self.media_root, "galleries"))
        response = self.client.post(
            self.url,
            {"action": "start", "scope": "folders", "folders": f"galleries\n{self.media_root}/galleries/", "check_crc32": "1"},
        )
        check = ArchiveFileCheck.objects.get()
        self.assertRedirects(response, f"{self.url}?check={check.pk}", fetch_redirect_response=False)
        self.assertEqual(check.folders, ["galleries"])
        self.assertTrue(check.check_crc32)
        self.assertEqual(check.task.task_type, TASK_TYPE)
        self.assertEqual(check.task.status, TaskQueue.Status.PENDING)

    def test_start_check_rejects_folder_outside_media(self):
        self.client.post(self.url, {"action": "start", "scope": "folders", "folders": "../etc"})
        self.assertEqual(ArchiveFileCheck.objects.count(), 0)
        self.client.post(self.url, {"action": "start", "scope": "archives", "archive_ids": "abc"})
        self.assertEqual(ArchiveFileCheck.objects.count(), 0)

    def test_status_api_and_results_page(self):
        missing = self.make_archive("v/m.zip")
        os.remove(missing.zipped.path)
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)

        data = self.client.get(reverse("viewer:api-archive-file-check-status", args=[check.pk])).json()
        self.assertEqual(data["status"], "queued")
        self.assertFalse(data["is_finished"])

        run_file_check(check.pk)
        data = self.client.get(reverse("viewer:api-archive-file-check-status", args=[check.pk])).json()
        self.assertEqual(data["status"], "completed")
        self.assertEqual(data["missing_count"], 1)
        self.assertEqual(data["percent"], 100.0)

        response = self.client.get(self.url, {"check": check.pk})
        self.assertContains(response, f"#{missing.pk}")
        partial = self.client.get(self.url, {"check": check.pk, "partial": "issues", "kind": "missing"})
        self.assertContains(partial, f"#{missing.pk}")
        self.assertNotContains(partial, "<html")

    def test_cancel_queued_check(self):
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        self.client.post(self.url, {"action": "cancel", "check": check.pk})
        check.refresh_from_db()
        self.assertEqual(check.status, ArchiveFileCheck.Status.CANCELLED)
        self.assertEqual(check.task.status, TaskQueue.Status.CANCELLED)

    def test_issue_actions(self):
        a = self.make_archive("i/a.zip")
        b = self.make_archive("i/b.zip")
        os.remove(a.zipped.path)
        os.remove(b.zipped.path)
        check = run_file_check(create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user).pk)
        issue_a = check.issues.get(archive=a)

        # Re-check only the selected issue.
        self.client.post(
            self.url, {"action": "issues", "check": check.pk, "issue_action": "recheck", "issue": [issue_a.pk]}
        )
        recheck = ArchiveFileCheck.objects.exclude(pk=check.pk).get()
        self.assertEqual(recheck.scope, ArchiveFileCheck.Scope.ARCHIVES)
        self.assertEqual(recheck.archive_ids, [a.pk])

        # Mark everything in the filter as deleted.
        self.client.post(
            self.url,
            {
                "action": "issues",
                "check": check.pk,
                "issue_action": "mark_deleted",
                "kind": "missing",
                "apply_to_all_filtered": "1",
            },
        )
        self.assertEqual(Archive.objects.filter(pk__in=[a.pk, b.pk], file_deleted=True).count(), 2)
        self.assertEqual(check.issues.count(), 0)

    def test_delete_check_requires_finished(self):
        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, user=self.user)
        self.client.post(self.url, {"action": "delete_check", "check": check.pk})
        self.assertTrue(ArchiveFileCheck.objects.filter(pk=check.pk).exists())
        run_file_check(check.pk)
        self.client.post(self.url, {"action": "delete_check", "check": check.pk})
        self.assertFalse(ArchiveFileCheck.objects.filter(pk=check.pk).exists())


class FileCheckSelectionTests(FileCheckTestBase):
    """Starting checks from the archive picker widget (``archive_selection``) and running them again."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = reverse("viewer:archive-file-check")

    def start(self, selection, **extra):
        data = {"action": "start", "scope": "archives", "archive_selection": json.dumps(selection)}
        data.update(extra)
        return self.client.post(self.url, data)

    def test_ids_selection(self):
        a = self.make_archive("s/a.zip")
        self.start({"mode": "ids", "ids": [a.pk, str(a.pk), "x"]})
        check = ArchiveFileCheck.objects.get()
        self.assertEqual(check.scope, ArchiveFileCheck.Scope.ARCHIVES)
        self.assertEqual(check.archive_ids, [a.pk])

    def test_invalid_selections_are_rejected(self):
        for selection in ({"mode": "ids", "ids": []}, {"mode": "filter", "filter": "x"}, ["nope"]):
            self.start(selection)
        self.client.post(self.url, {"action": "start", "scope": "archives", "archive_selection": "{broken"})
        self.assertEqual(ArchiveFileCheck.objects.count(), 0)

    def test_filter_selection_is_applied_when_the_check_runs(self):
        first = self.make_archive("f/first.zip", title="Wanted first")
        self.make_archive("f/other.zip", title="Something else")
        self.start({"mode": "filter", "filter": {"title": "wanted", "recycled": "", "bogus": "1"}, "count": 99}, check_crc32="1")
        check = ArchiveFileCheck.objects.get()
        self.assertEqual(check.scope, ArchiveFileCheck.Scope.FILTER)
        self.assertEqual(check.filter_params, {"title": "wanted"})
        self.assertEqual(check.filter_count, 1)
        self.assertTrue(check.check_crc32)
        self.assertIn("title=wanted", check.scope_description)

        # Archives that match by the time the worker starts are checked too.
        later = self.make_archive("f/later.zip", title="Wanted later")
        os.remove(later.zipped.path)
        check = run_file_check(check.pk)
        self.assertEqual(check.total, 2)
        self.assertEqual(check.ok_count, 1)
        self.assertEqual(self.issues_by_archive(check), {later.pk: ArchiveFileCheckIssue.Kind.MISSING})
        self.assertIsNotNone(first)

    def test_filter_without_matches_is_rejected(self):
        self.start({"mode": "filter", "filter": {"title": "nothing like this"}, "count": 0})
        self.assertEqual(ArchiveFileCheck.objects.count(), 0)

    def test_noscript_id_list_still_works(self):
        a = self.make_archive("n/a.zip")
        self.client.post(self.url, {"action": "start", "scope": "archives", "archive_ids": f"{a.pk}"})
        self.assertEqual(ArchiveFileCheck.objects.get().archive_ids, [a.pk])

    def test_rerun_copies_scope_and_options(self):
        self.make_archive("r/a.zip", title="Rerun me")
        self.start({"mode": "filter", "filter": {"title": "rerun"}, "count": 1}, check_crc32="1")
        first = ArchiveFileCheck.objects.get()
        run_file_check(first.pk)

        response = self.client.post(self.url, {"action": "rerun", "check": first.pk})
        second = ArchiveFileCheck.objects.exclude(pk=first.pk).get()
        self.assertRedirects(response, f"{self.url}?check={second.pk}", fetch_redirect_response=False)
        self.assertEqual(second.scope, ArchiveFileCheck.Scope.FILTER)
        self.assertEqual(second.filter_params, {"title": "rerun"})
        self.assertTrue(second.check_crc32)
        self.assertEqual(second.task.status, TaskQueue.Status.PENDING)

        folders_check = create_file_check(scope=ArchiveFileCheck.Scope.FOLDERS, folders=["r"], include_binned=True)
        self.client.post(self.url, {"action": "rerun", "check": folders_check.pk})
        third = ArchiveFileCheck.objects.latest("pk")
        self.assertEqual((third.scope, third.folders, third.include_binned), ("folders", ["r"], True))

    def test_page_mounts_the_widgets(self):
        a = self.make_archive("p/a.zip")
        response = self.client.get(self.url, {"archives": f"{a.pk}, 7"})
        self.assertContains(response, 'data-chaika-widget="archive-picker"')
        self.assertContains(response, 'data-chaika-widget="media-folder-picker"')
        self.assertContains(response, f'data-initial-ids="{a.pk},7"')
        self.assertContains(response, "admin-widgets/assets/main-")


class Crc32AndZipTestTests(FileCheckTestBase):
    def flip_image_byte(self, archive: Archive, marker: bytes) -> None:
        with open(archive.zipped.path, "r+b") as f:
            offset = f.read().index(marker * 100) + 50
            f.seek(offset)
            f.write(b"!")

    def test_crc32_values_are_compared_as_numbers(self):
        self.assertTrue(crc32_matches("33AC069", "033AC069"))
        self.assertTrue(crc32_matches("c9dab27", "0C9DAB27"))
        self.assertFalse(crc32_matches("33AC069", "133AC069"))
        self.assertFalse(crc32_matches("not hex", "033AC069"))

    def test_crc32_stored_without_leading_zeros_is_not_a_mismatch(self):
        archive = self.make_archive("p/a.zip", b"q" * 3000)
        # Older versions stored the CRC32 with "%X": without leading zeros, and sometimes lowercase.
        stored = "%x" % int(archive.crc32, 16)
        Archive.objects.filter(pk=archive.pk).update(crc32=stored)
        check = run_file_check(create_file_check(scope=ArchiveFileCheck.Scope.ALL, check_crc32=True).pk)
        self.assertEqual((check.ok_count, check.crc32_mismatch_count), (1, 0))

    def test_zip_test_tells_corruption_from_a_wrong_stored_crc32(self):
        wrong_crc = self.make_archive("t/wrong_crc.zip", b"a" * 4000, crc32="12345678")
        corrupted = self.make_archive("t/corrupted.zip", b"b" * 4000)
        self.flip_image_byte(corrupted, b"b")
        not_zip = self.make_archive("t/not_zip.zip", crc32="12345678")
        with open(not_zip.zipped.path, "wb") as f:
            f.write(b"garbage")

        check = create_file_check(scope=ArchiveFileCheck.Scope.ALL, check_crc32=True, test_zip=True)
        self.assertTrue(check.test_zip)
        check = run_file_check(check.pk)

        self.assertEqual(check.crc32_mismatch_count, 3)
        results = dict(check.issues.values_list("archive_id", "zip_test"))
        self.assertEqual(
            results,
            {
                wrong_crc.pk: ArchiveFileCheckIssue.ZipTest.PASSED,
                corrupted.pk: ArchiveFileCheckIssue.ZipTest.FAILED,
                not_zip.pk: ArchiveFileCheckIssue.ZipTest.NOT_ZIP,
            },
        )
        self.assertIn("001.jpg", check.issues.get(archive=corrupted).message)
        self.assertIn("Recalculate", check.issues.get(archive=wrong_crc).message)

    def test_zip_test_is_optional_and_needs_crc32(self):
        archive = self.make_archive("o/a.zip", b"c" * 4000, crc32="12345678")
        self.assertFalse(create_file_check(scope=ArchiveFileCheck.Scope.ALL, test_zip=True).test_zip)
        check = run_file_check(create_file_check(scope=ArchiveFileCheck.Scope.ALL, check_crc32=True).pk)
        self.assertEqual(check.issues.get(archive=archive).zip_test, "")

    def test_page_options_and_zip_test_filter(self):
        self.client.force_login(self.user)
        url = reverse("viewer:archive-file-check")
        wrong_crc = self.make_archive("v/wrong.zip", b"d" * 4000, crc32="12345678")
        corrupted = self.make_archive("v/bad.zip", b"e" * 4000)
        self.flip_image_byte(corrupted, b"e")

        self.client.post(url, {"action": "start", "scope": "all", "check_crc32": "1", "test_zip": "1"})
        check = ArchiveFileCheck.objects.get()
        self.assertTrue(check.test_zip)
        run_file_check(check.pk)

        page = self.client.get(url, {"check": check.pk, "kind": "crc32_mismatch", "zip_test": "passed"})
        self.assertContains(page, f"#{wrong_crc.pk}")
        self.assertNotContains(page, f"#{corrupted.pk}<")
        self.assertContains(page, "Zip corrupted")

        # Re-checking keeps the zip test, and "Run again" too.
        issue = check.issues.get(archive=wrong_crc)
        self.client.post(url, {"action": "issues", "check": check.pk, "issue_action": "recheck", "issue": [issue.pk]})
        self.assertTrue(ArchiveFileCheck.objects.latest("pk").test_zip)
        self.client.post(url, {"action": "rerun", "check": check.pk})
        self.assertTrue(ArchiveFileCheck.objects.latest("pk").test_zip)
