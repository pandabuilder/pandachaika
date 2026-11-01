import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from core.base.setup import Settings
from core.workers.archive_work import ArchiveWorker
from viewer.models import Archive, Gallery
from workers.models import TaskQueue
from workers.runner import WorkerRunner

User = get_user_model()


class AdminToolsBackgroundTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_user",
            email="admin@example.com",
            password="testpassword",
        )
        self.client.login(username="admin_user", password="testpassword")
        self.settings = Settings(load_from_disk=False)

    def test_generate_missing_thumbs_view_no_archives(self):
        Archive.objects.all().delete()
        resp = self.client.get(reverse("viewer:tools-id", args=["generate_missing_thumbs"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TaskQueue.objects.filter(task_type="thumbnails_worker").exists())

    def test_generate_missing_thumbs_view_enqueues_task(self):
        a1 = Archive.objects.create(title="A1", thumbnail="", user=self.admin_user)
        a2 = Archive.objects.create(title="A2", thumbnail="", user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-id", args=["generate_missing_thumbs"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        task = TaskQueue.objects.filter(task_type="thumbnails_worker").latest("pk")
        self.assertEqual(sorted(task.args), sorted([a1.pk, a2.pk]))

    def test_generate_missing_thumbs_view_already_running(self):
        TaskQueue.objects.create(task_type="thumbnails_worker", status=TaskQueue.Status.PENDING)
        Archive.objects.create(title="A1", thumbnail="", user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-id", args=["generate_missing_thumbs"]), HTTP_REFERER="/tools/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Thumbnails worker is already running.")

    def test_generate_missing_thumbs_api_no_archives(self):
        resp = self.client.get(reverse("viewer:tools-api", args=["generate_missing_thumbs"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["message"], "No archives missing thumbnails found.")

    def test_generate_missing_thumbs_api_enqueues_task(self):
        a1 = Archive.objects.create(title="A1", thumbnail="", user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-api", args=["generate_missing_thumbs"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("Thumbnails worker started in background for 1 archives", data["message"])
        task = TaskQueue.objects.filter(task_type="thumbnails_worker").latest("pk")
        self.assertEqual(task.args, [a1.pk])

    def test_generate_missing_thumbs_api_already_running(self):
        TaskQueue.objects.create(task_type="thumbnails_worker", status=TaskQueue.Status.PROCESSING)
        resp = self.client.get(reverse("viewer:tools-api", args=["generate_missing_thumbs"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "Thumbnails worker is already running.")

    def test_calculate_missing_info_view_no_archives(self):
        resp = self.client.get(reverse("viewer:tools-id", args=["calculate_missing_info"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TaskQueue.objects.filter(task_type="fileinfo_worker").exists())

    def test_calculate_missing_info_view_enqueues_task(self):
        a1 = Archive.objects.create(title="A1", crc32="", filesize=0, filecount=0, user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-id", args=["calculate_missing_info"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        task = TaskQueue.objects.filter(task_type="fileinfo_worker").latest("pk")
        self.assertEqual(task.args, [a1.pk])

    def test_calculate_missing_info_view_already_running(self):
        TaskQueue.objects.create(task_type="fileinfo_worker", status=TaskQueue.Status.PENDING)
        Archive.objects.create(title="A1", crc32="", filesize=0, filecount=0, user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-id", args=["calculate_missing_info"]), HTTP_REFERER="/tools/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "File info worker is already running.")

    def test_calculate_missing_info_api_no_archives(self):
        resp = self.client.get(reverse("viewer:tools-api", args=["calculate_missing_info"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["message"], "No archives missing file info found.")

    def test_calculate_missing_info_api_enqueues_task(self):
        a1 = Archive.objects.create(title="A1", crc32="", filesize=0, filecount=0, user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-api", args=["calculate_missing_info"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("File info worker started in background for 1 archives", data["message"])
        task = TaskQueue.objects.filter(task_type="fileinfo_worker").latest("pk")
        self.assertEqual(task.args, [a1.pk])

    def test_calculate_missing_info_api_already_running(self):
        TaskQueue.objects.create(task_type="fileinfo_worker", status=TaskQueue.Status.PROCESSING)
        resp = self.client.get(reverse("viewer:tools-api", args=["calculate_missing_info"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "File info worker is already running.")

    def test_set_all_hidden_as_public_view_no_archives(self):
        resp = self.client.get(reverse("viewer:tools-id", args=["set_all_hidden_as_public"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(TaskQueue.objects.filter(task_type="publish_worker").exists())

    def test_set_all_hidden_as_public_view_enqueues_task(self):
        g = Gallery.objects.create(title="G1", hidden=True)
        a1 = Archive.objects.create(title="A1", gallery=g, public=False, user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-id", args=["set_all_hidden_as_public"]), HTTP_REFERER="/tools/")
        self.assertEqual(resp.status_code, 302)
        task = TaskQueue.objects.filter(task_type="publish_worker").latest("pk")
        self.assertEqual(task.args, [a1.pk])

    def test_set_all_hidden_as_public_view_already_running(self):
        TaskQueue.objects.create(task_type="publish_worker", status=TaskQueue.Status.PENDING)
        g = Gallery.objects.create(title="G1", hidden=True)
        Archive.objects.create(title="A1", gallery=g, public=False, user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-id", args=["set_all_hidden_as_public"]), HTTP_REFERER="/tools/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Publish worker is already running.")

    def test_set_all_hidden_as_public_api_no_archives(self):
        resp = self.client.get(reverse("viewer:tools-api", args=["set_all_hidden_as_public"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["message"], "No archives of hidden galleries found.")

    def test_set_all_hidden_as_public_api_enqueues_task(self):
        g = Gallery.objects.create(title="G1", hidden=True)
        a1 = Archive.objects.create(title="A1", gallery=g, public=False, user=self.admin_user)
        resp = self.client.get(reverse("viewer:tools-api", args=["set_all_hidden_as_public"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("Publish worker started in background for 1 archives", data["message"])
        task = TaskQueue.objects.filter(task_type="publish_worker").latest("pk")
        self.assertEqual(task.args, [a1.pk])

    def test_set_all_hidden_as_public_api_already_running(self):
        TaskQueue.objects.create(task_type="publish_worker", status=TaskQueue.Status.PROCESSING)
        resp = self.client.get(reverse("viewer:tools-api", args=["set_all_hidden_as_public"]))
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["error"], "Publish worker is already running.")

    def test_archive_worker_convenience_methods(self):
        aw = ArchiveWorker(self.settings)
        a1 = Archive.objects.create(title="A1", thumbnail="", crc32="", filesize=0, filecount=0, user=self.admin_user)
        g = Gallery.objects.create(title="G1", hidden=True)
        a2 = Archive.objects.create(title="A2", gallery=g, public=False, user=self.admin_user)

        aw.generate_missing_thumbs(archives=[a1], user=self.admin_user)
        t1 = TaskQueue.objects.filter(task_type="thumbnails_worker").latest("pk")
        self.assertEqual(t1.args, [a1.pk])

        aw.calculate_missing_info(archives=[a1], user=self.admin_user)
        t2 = TaskQueue.objects.filter(task_type="fileinfo_worker").latest("pk")
        self.assertEqual(t2.args, [a1.pk])

        aw.set_all_hidden_as_public(archives=[a2], user=self.admin_user)
        t3 = TaskQueue.objects.filter(task_type="publish_worker").latest("pk")
        self.assertEqual(t3.args, [a2.pk])

    def test_runner_execute_job_aliases(self):
        runner = WorkerRunner(self.settings, enable_schedulers=False)
        a1 = Archive.objects.create(title="A1", thumbnail="", user=self.admin_user)

        job = TaskQueue.objects.create(task_type="generate_missing_thumbs", args=[a1.pk], status=TaskQueue.Status.PROCESSING)
        with patch.object(ArchiveWorker, "regenerate_all_thumbs") as mock_thumbs:
            runner.execute_job(job)
            mock_thumbs.assert_called_once()

        job_info = TaskQueue.objects.create(task_type="calculate_missing_info", args=[a1.pk], status=TaskQueue.Status.PROCESSING)
        with patch.object(ArchiveWorker, "recalc_all_file_info") as mock_fileinfo:
            runner.execute_job(job_info)
            mock_fileinfo.assert_called_once()

        job_pub = TaskQueue.objects.create(task_type="set_all_hidden_as_public", args=[a1.pk], status=TaskQueue.Status.PROCESSING)
        with patch.object(ArchiveWorker, "publish_archives") as mock_pub:
            runner.execute_job(job_pub)
            mock_pub.assert_called_once()
