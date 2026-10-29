import json
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase

from viewer.models import Archive


class ExpApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin_user = User.objects.create_user(username="admin_user", password="password123")
        perm = Permission.objects.get(codename="expand_archive")
        cls.admin_user.user_permissions.add(perm)

        cls.regular_user = User.objects.create_user(username="regular_user", password="password123")
        cls.archive = Archive.objects.create(title="Test Archive", extracted=False, user=cls.admin_user)

    def setUp(self):
        self.admin_client = Client()
        self.admin_client.login(username="admin_user", password="password123")

        self.regular_client = Client()
        self.regular_client.login(username="regular_user", password="password123")

        self.anon_client = Client()

    def test_get_on_extract_toggle_is_deprecated_and_rejected(self):
        response = self.admin_client.get(f"/r-api/archive/{self.archive.pk}/extract_toggle/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(response["Deprecation"], "true")
        self.assertIn("deprecated", response["Warning"])

        data = json.loads(response.content)
        self.assertEqual(data["result"], "error")
        self.assertIn("deprecated", data["message"])

    def test_get_on_extract_archive_is_deprecated_and_rejected(self):
        response = self.admin_client.get(f"/r-api/archive/{self.archive.pk}/extract_archive/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")
        self.assertEqual(response["Deprecation"], "true")

        data = json.loads(response.content)
        self.assertEqual(data["result"], "error")
        self.assertIn("deprecated", data["message"])

    def test_get_on_reduce_archive_is_deprecated_and_rejected(self):
        response = self.admin_client.get(f"/r-api/archive/{self.archive.pk}/reduce_archive/")
        self.assertEqual(response.status_code, 405)
        self.assertIn("POST", response["Allow"])
        self.assertIn("DELETE", response["Allow"])
        self.assertEqual(response["Deprecation"], "true")

        data = json.loads(response.content)
        self.assertEqual(data["result"], "error")
        self.assertIn("deprecated", data["message"])

    @patch.object(Archive, "extract_toggle")
    def test_post_extract_toggle_success(self, mock_toggle):
        def fake_toggle(self_archive):
            self_archive.extracted = True

        mock_toggle.side_effect = lambda: fake_toggle(self.archive)

        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/extract_toggle/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "ok")
        self.assertEqual(data["change"]["field"], "extracted")
        self.assertEqual(response["Deprecation"], "true")
        self.assertIn("deprecated", response["Warning"])
        mock_toggle.assert_called_once()

    @patch.object(Archive, "extract")
    def test_post_extract_archive_success(self, mock_extract):
        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/extract_archive/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "ok")
        self.assertEqual(data["message"], "Extraction successful.")
        mock_extract.assert_called_once()

    def test_post_extract_archive_already_extracted_warning(self):
        self.archive.extracted = True
        self.archive.save()

        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/extract_archive/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "warning")
        self.assertIn("already extracted", data["message"])

    @patch.object(Archive, "reduce")
    def test_post_reduce_archive_success(self, mock_reduce):
        self.archive.extracted = True
        self.archive.save()

        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/reduce_archive/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "ok")
        self.assertEqual(data["message"], "Reduction successful.")
        mock_reduce.assert_called_once()

    @patch.object(Archive, "reduce")
    def test_delete_reduce_archive_success(self, mock_reduce):
        self.archive.extracted = True
        self.archive.save()

        response = self.admin_client.delete(f"/r-api/archive/{self.archive.pk}/reduce_archive/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "ok")
        self.assertEqual(data["message"], "Reduction successful.")
        mock_reduce.assert_called_once()

    def test_post_reduce_archive_already_reduced_warning(self):
        self.archive.extracted = False
        self.archive.save()

        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/reduce_archive/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["result"], "warning")
        self.assertIn("already reduced", data["message"])

    def test_modifying_actions_permission_denied_for_regular_user(self):
        response = self.regular_client.post(f"/r-api/archive/{self.archive.pk}/extract_toggle/")
        data = json.loads(response.content)
        self.assertEqual(data["result"], "Admin only action.")

        response = self.regular_client.post(f"/r-api/archive/{self.archive.pk}/extract_archive/")
        data = json.loads(response.content)
        self.assertEqual(data["result"], "error")
        self.assertIn("permission", data["message"])

        response = self.regular_client.post(f"/r-api/archive/{self.archive.pk}/reduce_archive/")
        data = json.loads(response.content)
        self.assertEqual(data["result"], "error")
        self.assertIn("permission", data["message"])

    def test_csrf_exempt_allows_post_without_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="admin_user", password="password123")

        with patch.object(Archive, "extract"):
            response = csrf_client.post(f"/r-api/archive/{self.archive.pk}/extract_archive/")
            self.assertEqual(response.status_code, 200)

    def test_read_only_endpoint_rejects_post(self):
        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/")
        self.assertEqual(response.status_code, 405)

        response = self.admin_client.post(f"/r-api/archive/{self.archive.pk}/image_list/")
        self.assertEqual(response.status_code, 405)

        response = self.admin_client.post("/r-api/archives/")
        self.assertEqual(response.status_code, 405)

    def test_read_only_endpoint_accepts_get(self):
        response = self.admin_client.get(f"/r-api/archive/{self.archive.pk}/")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data["id"], self.archive.pk)

    @patch.object(Archive, "extract")
    def test_view_extract_idempotent(self, mock_extract):
        from workers.models import TaskQueue
        from workers import client

        response = self.admin_client.get(f"/archive/{self.archive.pk}/extract/", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        mock_extract.assert_not_called()

        tasks = TaskQueue.objects.filter(task_type="extract_worker")
        self.assertEqual(tasks.count(), 1)
        task = tasks.first()
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.options.get("resized"), False)
        self.assertTrue(client.is_archive_extracting(self.archive.pk))

        # Calling again while task is pending does not enqueue a duplicate task
        response = self.admin_client.get(f"/archive/{self.archive.pk}/extract/?resized=1", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="extract_worker").count(), 1)

        # Archive details page shows Extracting... state while queued
        self.archive.crc32 = "AABBCCDD"
        self.archive.save()
        detail_resp = self.admin_client.get(f"/archive/{self.archive.pk}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, "btn-extracting-archive")

        # Once already extracted, calling /extract/ does not enqueue new tasks
        TaskQueue.objects.all().delete()
        self.archive.extracted = True
        self.archive.save()
        response = self.admin_client.get(f"/archive/{self.archive.pk}/extract/", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="extract_worker").count(), 0)
        mock_extract.assert_not_called()

    def test_archive_group_extract_all_enqueues_background_task(self):
        from viewer.models import ArchiveGroup, ArchiveGroupEntry
        from workers.models import TaskQueue

        perm_change = Permission.objects.get(codename="change_archivegroup")
        self.admin_user.user_permissions.add(perm_change)

        a2 = Archive.objects.create(title="Test Archive 2", extracted=True, user=self.admin_user)
        group = ArchiveGroup.objects.create(title="Test Group", title_slug="test-group", public=True)
        ArchiveGroupEntry.objects.create(archive_group=group, archive=self.archive, position=1)
        ArchiveGroupEntry.objects.create(archive_group=group, archive=a2, position=2)

        response = self.admin_client.get(
            f"/archive-group/{group.title_slug}/?extract_all=",
            HTTP_REFERER=f"/archive-group/{group.title_slug}/",
        )
        self.assertEqual(response.status_code, 302)
        tasks = TaskQueue.objects.filter(task_type="extract_worker")
        self.assertEqual(tasks.count(), 1)
        task = tasks.first()
        self.assertEqual(task.args, [self.archive.pk])
        self.assertEqual(task.options.get("resized"), True)

    @patch.object(Archive, "reduce")
    def test_view_reduce_idempotent(self, mock_reduce):
        self.archive.extracted = True
        self.archive.save()
        response = self.admin_client.get(f"/archive/{self.archive.pk}/reduce/", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        mock_reduce.assert_called_once()

        self.archive.extracted = False
        self.archive.save()
        mock_reduce.reset_mock()
        response = self.admin_client.get(f"/archive/{self.archive.pk}/reduce/", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        mock_reduce.assert_not_called()

    @patch.object(Archive, "set_public")
    def test_view_public_idempotent(self, mock_set_public):
        from workers.models import TaskQueue
        from workers import client

        perm_publish = Permission.objects.get(codename="publish_archive")
        self.admin_user.user_permissions.add(perm_publish)

        response = self.admin_client.get(f"/archive/{self.archive.pk}/public/", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        mock_set_public.assert_not_called()

        tasks = TaskQueue.objects.filter(task_type="publish_worker")
        self.assertEqual(tasks.count(), 1)
        task = tasks.first()
        self.assertEqual(task.args, [self.archive.pk])
        self.assertTrue(client.is_archive_publishing(self.archive.pk))

        # Calling again while task is pending does not enqueue a duplicate task
        response = self.admin_client.get(f"/archive/{self.archive.pk}/public/", HTTP_REFERER="/archive/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="publish_worker").count(), 1)

        # Archive details page shows Publishing... state while queued
        self.archive.crc32 = "AABBCCDD"
        self.archive.save()
        detail_resp = self.admin_client.get(f"/archive/{self.archive.pk}/")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertContains(detail_resp, "btn-publishing-archive")

        # Once already public, calling /public/ does not enqueue new tasks
        TaskQueue.objects.all().delete()
        self.archive.public = True
        self.archive.save()
        response = self.admin_client.get(f"/archive/{self.archive.pk}/public/", HTTP_REFERER="/archive/")
        self.assertEqual(TaskQueue.objects.filter(task_type="publish_worker").count(), 0)
        mock_set_public.assert_not_called()

