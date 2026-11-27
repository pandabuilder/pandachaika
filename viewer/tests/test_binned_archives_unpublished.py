import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from viewer.models import Archive, EventLog
from viewer.tests.factories import ArchiveFactory

User = get_user_model()


def make_user(username: str, *perms: str):
    user = User.objects.create_user(username=username, password="password")
    for perm in perms:
        user.user_permissions.add(Permission.objects.get(codename=perm, content_type__app_label="viewer"))
    return User.objects.get(pk=user.pk)


def public_archive(**kwargs) -> Archive:
    """A published archive with no file on disk, like one whose ZIP was deleted or went missing."""
    return ArchiveFactory.create(public=True, public_date=timezone.now(), crc32="", **kwargs)


class BinnedArchivesAreUnpublishedTests(TestCase):
    """Binned archives must stop being public: logged out pages other than the search only check public."""

    def test_set_private_without_file(self):
        archive = public_archive()

        archive.set_private()

        archive.refresh_from_db()
        archive.gallery.refresh_from_db()
        self.assertFalse(archive.public)
        self.assertIsNone(archive.public_date)
        self.assertFalse(archive.gallery.public)

    def test_move_to_recycle_bin_without_file(self):
        archive = public_archive()

        archive.move_to_recycle_bin(reason="Failed on ZIP file opening")

        archive.refresh_from_db()
        self.assertTrue(archive.binned)
        self.assertFalse(archive.public)

    def _delete_zipped(self, archive: Archive, user) -> None:
        from core.base.setup import Settings
        from core.workers.archive_work import ArchiveWorker

        ArchiveWorker(Settings(load_from_disk=False), 4).delete_archive(
            archive_id=archive.pk, delete_zipped=True, user_reason="duplicate", user=user, force_local=True
        )

    def test_delete_zipped_worker_unpublishes(self):
        # The ZIP is deleted before the archive is binned, so this always unpublished an archive without a file.
        archive = public_archive()

        self._delete_zipped(archive, make_user("deleter", "manage_archive", "delete_archive"))

        archive.refresh_from_db()
        self.assertTrue(archive.binned)
        self.assertFalse(archive.public)

    def test_delete_zipped_worker_unpublishes_without_user(self):
        archive = public_archive()

        self._delete_zipped(archive, None)

        archive.refresh_from_db()
        self.assertTrue(archive.binned)
        self.assertFalse(archive.public)

    def _run_pending_tasks(self) -> None:
        from core.base.setup import Settings
        from workers.models import TaskQueue
        from workers.runner import WorkerRunner

        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        for task in TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).order_by("pk"):
            task.status = TaskQueue.Status.PROCESSING
            task.save(update_fields=["status"])
            runner.execute_job(task)

    def test_batch_recycle_unpublishes(self):
        archive = public_archive()
        self.client.force_login(make_user("recycler", "manage_archive", "recycle_archive"))

        self.client.post(
            reverse("viewer:manage-archives"),
            {f"sel-{archive.pk}": str(archive.pk), "recycle_archives": "1", "reason": "duplicate"},
        )
        self._run_pending_tasks()

        archive.refresh_from_db()
        self.assertTrue(archive.binned)
        self.assertFalse(archive.public)

    def test_logged_out_counts_after_recycling(self):
        kept = public_archive()
        recycled = public_archive()
        self.client.force_login(make_user("recycler", "manage_archive", "recycle_archive"))
        self.client.post(
            reverse("viewer:manage-archives"),
            {f"sel-{recycled.pk}": str(recycled.pk), "recycle_archives": "1"},
        )
        self._run_pending_tasks()
        self.client.logout()

        data = json.loads(self.client.get(reverse("viewer:main-page"), {"json": "1"}).content)
        self.assertEqual(data["count"], 1)
        self.assertEqual([a["id"] for a in data["archives"]], [kept.pk])
        self.assertEqual(self.client.get(reverse("viewer:archive", args=[recycled.pk])).status_code, 404)


class BinnedArchivesCantBePublishedTests(TestCase):
    """An archive put in the Recycle Bin while private could still be published, ending public and binned."""

    def setUp(self):
        # The file checks pass, so only the Recycle Bin can stop the publishing.
        patcher = patch.object(Archive, "has_zipped_file", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.archive = ArchiveFactory.create(public=False, binned=True, crc32="186EF904")

    def test_set_public_refuses_binned(self):
        self.assertFalse(self.archive.set_public())

        self.archive.refresh_from_db()
        self.assertFalse(self.archive.public)
        self.assertIsNone(self.archive.public_date)

    def test_batch_publish_skips_binned(self):
        self.client.force_login(make_user("publisher", "manage_archive", "publish_archive"))

        self.client.post(
            reverse("viewer:manage-archives"),
            {f"sel-{self.archive.pk}": str(self.archive.pk), "publish_archives": "1"},
        )

        self.archive.refresh_from_db()
        self.assertFalse(self.archive.public)
        self.assertFalse(EventLog.objects.filter(action="PUBLISH_ARCHIVE").exists())

    def test_publish_view_refuses_binned(self):
        from workers.models import TaskQueue

        self.client.force_login(make_user("publisher", "publish_archive"))

        response = self.client.post(reverse("viewer:archive-public", args=[self.archive.pk]))

        self.assertIn("Recycle Bin", " ".join(str(m) for m in get_messages(response.wsgi_request)))
        self.assertFalse(TaskQueue.objects.filter(task_type="publish_worker").exists())

    def test_publish_worker_skips_binned(self):
        from core.base.setup import Settings
        from core.workers.archive_work import ArchiveWorker

        with patch.object(Archive, "test_zip_file") as test_zip_file:
            ArchiveWorker(Settings(load_from_disk=False), 4).publish_archives(
                archives=[self.archive.pk], force_local=True
            )

        test_zip_file.assert_not_called()
        self.archive.refresh_from_db()
        self.assertFalse(self.archive.public)
