# -*- coding: utf-8 -*-
from unittest.mock import patch
from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse

from core.base.setup import Settings
from viewer.models import Archive
from workers import client
from workers.models import TaskQueue
from workers.runner import WorkerRunner


class ArchiveSingleOperationsBackgroundTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_user = User.objects.create_user(
            username="staff_user", password="password123", is_staff=True
        )
        cls.expand_user = User.objects.create_user(
            username="expand_user", password="password123"
        )
        cls.expand_user.user_permissions.add(Permission.objects.get(codename="expand_archive"))

        cls.tools_user = User.objects.create_user(
            username="tools_user", password="password123"
        )
        cls.tools_user.user_permissions.add(Permission.objects.get(codename="modify_archive_tools"))

        cls.publish_user = User.objects.create_user(
            username="publish_user", password="password123"
        )
        cls.publish_user.user_permissions.add(Permission.objects.get(codename="publish_archive"))

        cls.similar_user = User.objects.create_user(
            username="similar_user", password="password123"
        )
        cls.similar_user.user_permissions.add(Permission.objects.get(codename="mark_similar_archive"))

        cls.regular_user = User.objects.create_user(
            username="regular_user", password="password123"
        )

        cls.archive = Archive.objects.create(
            title="Test Archive",
            extracted=True,
            public=True,
            crc32="12345678",
            user=cls.staff_user,
        )

    def setUp(self):
        self.staff_client = Client()
        self.staff_client.login(username="staff_user", password="password123")

        self.expand_client = Client()
        self.expand_client.login(username="expand_user", password="password123")

        self.tools_client = Client()
        self.tools_client.login(username="tools_user", password="password123")

        self.publish_client = Client()
        self.publish_client.login(username="publish_user", password="password123")

        self.similar_client = Client()
        self.similar_client.login(username="similar_user", password="password123")

        self.regular_client = Client()
        self.regular_client.login(username="regular_user", password="password123")

    def test_reduce_permission_required(self):
        resp = self.regular_client.get(reverse("viewer:archive-reduce", args=[self.archive.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        messages_list = list(resp.context["messages"])
        self.assertTrue(any("don't have the permission" in str(m) for m in messages_list))

    def test_reduce_enqueues_task(self):
        self.archive.extracted = True
        self.archive.save()

        resp = self.expand_client.get(reverse("viewer:archive-reduce", args=[self.archive.pk]))
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="reduce_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.event_action, "REDUCE_ARCHIVE")
        self.assertEqual(task.status, TaskQueue.Status.PENDING)

        # Calling again while task is active should not enqueue a duplicate
        resp2 = self.expand_client.get(reverse("viewer:archive-reduce", args=[self.archive.pk]))
        self.assertEqual(resp2.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="reduce_worker").count(), 1)

    def test_check_and_convert_filetype_enqueues_task(self):
        resp = self.staff_client.get(reverse("viewer:check-convert-type", args=[self.archive.pk]))
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="convert_archive_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.event_action, "CONVERT_TO_ZIP")

    def test_check_and_convert_filetype_requires_staff(self):
        resp = self.regular_client.get(reverse("viewer:check-convert-type", args=[self.archive.pk]), follow=True)
        self.assertEqual(resp.status_code, 200)
        messages_list = list(resp.context["messages"])
        self.assertTrue(any("need to be an admin" in str(m) for m in messages_list))

    def test_clone_plus_enqueues_task(self):
        data = {
            "submit": "1",
            "reorder-by-sha1": "1",
            "sha1s": "hash1\nhash2\nhash3",
            "bin-original": "1",
            "reason": "Test clone reason",
        }
        resp = self.tools_client.post(reverse("viewer:archive-clone-plus", args=[self.archive.pk]), data)
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="clone_archive_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.options["sha1_list"], ["hash1", "hash2", "hash3"])
        self.assertTrue(task.options["bin_original"])
        self.assertEqual(task.reason, "Test clone reason")

    def test_split_nested_enqueues_task(self):
        data = {
            "submit": "1",
            "split-nested": "1",
            "bin-original": "1",
            "reason": "Test split nested",
        }
        resp = self.tools_client.post(reverse("viewer:archive-split", args=[self.archive.pk]), data)
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="split_archive_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertTrue(task.options["split_nested"])
        self.assertTrue(task.options["bin_original"])
        self.assertEqual(task.reason, "Test split nested")

    def test_split_ranges_enqueues_task(self):
        data = {
            "submit": "1",
            "archives-TOTAL_FORMS": "1",
            "archives-INITIAL_FORMS": "0",
            "archives-MIN_NUM_FORMS": "0",
            "archives-MAX_NUM_FORMS": "1000",
            "archives-0-starting_position": "1",
            "archives-0-ending_position": "5",
            "archives-0-new_file_name": "part1.zip",
            "reason": "Test split range",
        }
        self.archive.filecount = 10
        self.archive.save()

        resp = self.tools_client.post(reverse("viewer:archive-split", args=[self.archive.pk]), data)
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="split_archive_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertFalse(task.options["split_nested"])
        self.assertEqual(task.options["cleaned_data"], [[1, 5, "part1.zip", False]])

    def test_calculate_images_sha1_enqueues_task(self):
        resp = self.publish_client.post(reverse("viewer:archive-calc-imgs-sha1", args=[self.archive.pk]))
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="calculate_sha1_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.event_action, "CALCULATE_IMAGES_SHA1")

    def test_recalc_info_enqueues_task(self):
        resp = self.staff_client.post(reverse("viewer:archive-recalc-info", args=[self.archive.pk]))
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="fileinfo_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertTrue(task.options.get("full"))
        self.assertEqual(task.event_action, "RECALCULATE_FILEINFO")

    def test_mark_similar_archives_enqueues_task(self):
        resp = self.similar_client.get(
            reverse("viewer:archive-mark-similar", args=[self.archive.pk]) + "?use-recycled=1"
        )
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="mark_similar_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertTrue(task.options.get("use_recycled"))
        self.assertEqual(task.event_action, "MARK_SIMILAR")

    def test_generate_matches_enqueues_task(self):
        # Must not have gallery
        self.archive.gallery = None
        self.archive.save()

        resp = self.staff_client.get(
            reverse("viewer:archive-generate-matches", args=[self.archive.pk]) + "?cutoff=0.6&max-matches=5&provider=panda"
        )
        self.assertEqual(resp.status_code, 302)

        task = TaskQueue.objects.filter(task_type="match_unmatched_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.options["cutoff"], 0.6)
        self.assertEqual(task.options["max_matches"], 5)
        self.assertEqual(task.options["provider_filter"], "panda")
        self.assertEqual(task.event_action, "GENERATE_MATCHES")

    def test_delete_archive_enqueues_task(self):
        data = {
            "delete_confirm": "1",
            "delete-archive": "1",
            "delete-file": "1",
            "delete-gallery": "1",
            "reason": "Test delete reason",
        }
        resp = self.staff_client.post(reverse("viewer:archive-delete", args=[self.archive.pk]), data)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, reverse("viewer:main-page"))

        task = TaskQueue.objects.filter(task_type="delete_archive_worker").latest("pk")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertTrue(task.options["delete_archive"])
        self.assertTrue(task.options["delete_file"])
        self.assertTrue(task.options["delete_gallery"])
        self.assertEqual(task.reason, "Test delete reason")

    def test_worker_runner_executes_archive_tasks(self):
        runner_settings = Settings(load_from_disk=False)
        runner = WorkerRunner(runner_settings, enable_schedulers=False)

        def claim_and_execute(task, patch_target):
            task.status = TaskQueue.Status.PROCESSING
            task.save()
            with patch(patch_target) as mock_method:
                runner.execute_job(task)
                mock_method.assert_called_once()
            task.refresh_from_db()
            self.assertEqual(task.status, TaskQueue.Status.COMPLETED)

        # 1. reduce_worker
        task_reduce = client.enqueue([self.archive.pk], task_type="reduce_worker")
        claim_and_execute(task_reduce, "core.workers.archive_work.ArchiveWorker.reduce_archives")

        # 2. calculate_sha1_worker
        task_sha1 = client.enqueue([self.archive.pk], task_type="calculate_sha1_worker")
        claim_and_execute(task_sha1, "core.workers.archive_work.ArchiveWorker.calculate_images_sha1")

        # 3. convert_archive_worker
        task_convert = client.enqueue([self.archive.pk], task_type="convert_archive_worker")
        claim_and_execute(task_convert, "core.workers.archive_work.ArchiveWorker.check_and_convert_filetype")

        # 4. clone_archive_worker
        task_clone = client.enqueue([self.archive.pk], task_type="clone_archive_worker", override_options={"sha1_list": ["abc"]})
        claim_and_execute(task_clone, "core.workers.archive_work.ArchiveWorker.clone_archive")

        # 5. split_archive_worker
        task_split = client.enqueue([self.archive.pk], task_type="split_archive_worker", override_options={"split_nested": True})
        claim_and_execute(task_split, "core.workers.archive_work.ArchiveWorker.split_archive")

        # 6. mark_similar_worker
        task_similar = client.enqueue([self.archive.pk], task_type="mark_similar_worker", override_options={"use_recycled": True})
        claim_and_execute(task_similar, "core.workers.archive_work.ArchiveWorker.mark_similar_archives")

        # 7. delete_archive_worker
        task_delete = client.enqueue([self.archive.pk], task_type="delete_archive_worker", override_options={"delete_archive": True})
        claim_and_execute(task_delete, "core.workers.archive_work.ArchiveWorker.delete_archive")

        # 8. fileinfo_worker with full=True
        task_fileinfo = client.enqueue([self.archive.pk], task_type="fileinfo_worker", override_options={"full": True})
        task_fileinfo.status = TaskQueue.Status.PROCESSING
        task_fileinfo.save()
        with patch("core.workers.archive_work.ArchiveWorker.recalc_all_file_info") as mock_fileinfo:
            runner.execute_job(task_fileinfo)
            mock_fileinfo.assert_called_once()
            _, kwargs = mock_fileinfo.call_args
            self.assertTrue(kwargs.get("full"))
        task_fileinfo.refresh_from_db()
        self.assertEqual(task_fileinfo.status, TaskQueue.Status.COMPLETED)

        # 9. match_unmatched_worker with options
        task_match = client.enqueue([self.archive.pk], task_type="match_unmatched_worker", override_options={"cutoff": 0.5, "max_matches": 10})
        claim_and_execute(task_match, "core.workers.archive_work.ArchiveWorker.generate_matches")

    def test_archive_details_is_reducing_context_and_template(self):
        self.archive.extracted = True
        self.archive.save()

        # Without active task: not reducing
        resp = self.expand_client.get(reverse("viewer:archive", args=[self.archive.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["is_reducing"])
        self.assertIn("btn-reduce-archive", resp.content.decode("utf-8"))

        # Enqueue reduction task
        client.enqueue([self.archive.pk], task_type="reduce_worker")
        resp = self.expand_client.get(reverse("viewer:archive", args=[self.archive.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["is_reducing"])
        content = resp.content.decode("utf-8")
        self.assertIn("btn-reducing-archive", content)
        self.assertIn("Reducing...", content)
