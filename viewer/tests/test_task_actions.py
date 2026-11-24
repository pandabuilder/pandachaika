# -*- coding: utf-8 -*-
"""Background task actions answered with a polling status fragment for HTMX requests (viewer.utils.task_actions)."""
from datetime import timedelta
import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse
import django.utils.timezone as django_tz

from core.base.setup import Settings
from viewer.models import Archive, Gallery, UserLongLivedToken, WantedGallery
from workers import client
from workers.models import TaskQueue
from workers.runner import WorkerRunner

HTMX = {"HTTP_HX_REQUEST": "true"}


class TaskActionViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.expand_user = User.objects.create_user(username="expand_user", password="password123")
        cls.expand_user.user_permissions.add(Permission.objects.get(codename="expand_archive"))
        cls.other_user = User.objects.create_user(username="other_user", password="password123")
        cls.other_user.user_permissions.add(Permission.objects.get(codename="expand_archive"))
        cls.staff_user = User.objects.create_user(username="staff_user", password="password123", is_staff=True)
        cls.archive = Archive.objects.create(
            title="Test Archive", extracted=False, crc32="12345678", user=cls.staff_user
        )

    def setUp(self):
        self.client = Client()
        self.client.login(username="expand_user", password="password123")

    def test_htmx_action_returns_polling_fragment(self):
        resp = self.client.post(reverse("viewer:archive-extract", args=[self.archive.pk]), **HTMX)

        self.assertEqual(resp.status_code, 200)
        task = TaskQueue.objects.get(task_type="extract_worker")
        content = resp.content.decode()
        self.assertIn('data-task-state="pending"', content)
        self.assertIn(f'data-task-id="{task.pk}"', content)
        self.assertIn(f'hx-get="{reverse("viewer:task-status", args=[task.pk])}"', content)
        self.assertIn("Extracting: queued", content)
        # The message is only for the redirect flow; it would show up on the next page load otherwise.
        self.assertEqual(len(list(resp.wsgi_request._messages)), 0)

    def test_htmx_action_already_queued_follows_existing_task(self):
        existing = client.enqueue([self.archive.pk], task_type="extract_worker")

        resp = self.client.post(reverse("viewer:archive-extract", args=[self.archive.pk]), **HTMX)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TaskQueue.objects.count(), 1)
        self.assertIn(f'data-task-id="{existing.pk}"', resp.content.decode())

    def test_plain_request_keeps_redirect(self):
        resp = self.client.post(
            reverse("viewer:archive-extract", args=[self.archive.pk]),
            HTTP_REFERER=f"http://testserver{reverse('viewer:archive', args=[self.archive.pk])}",
        )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="extract_worker").count(), 1)
        self.assertEqual([str(m) for m in resp.wsgi_request._messages], ["Queued archive extraction."])

    def test_htmx_without_task_navigates_the_page(self):
        self.archive.extracted = True
        self.archive.save()

        resp = self.client.post(reverse("viewer:archive-extract", args=[self.archive.pk]), **HTMX)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Redirect"], self.archive.get_absolute_url())
        self.assertFalse(TaskQueue.objects.exists())

    def test_htmx_permission_error_navigates_the_page(self):
        regular = User.objects.create_user(username="regular", password="password123")
        self.client.force_login(regular)
        referer = f"http://testserver{reverse('viewer:archive', args=[self.archive.pk])}"

        resp = self.client.post(reverse("viewer:archive-extract", args=[self.archive.pk]), HTTP_REFERER=referer, **HTMX)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Redirect"], referer)
        self.assertFalse(TaskQueue.objects.exists())

    def test_status_endpoint_polls_while_active(self):
        task = client.enqueue([self.archive.pk], task_type="extract_worker", user=self.expand_user)
        url = reverse("viewer:task-status", args=[task.pk])

        resp = self.client.get(url, **HTMX)
        self.assertContains(resp, 'data-task-state="pending"')
        self.assertContains(resp, f'hx-get="{url}"')

        task.status = TaskQueue.Status.PROCESSING
        task.started_at = django_tz.now()
        task.save()
        resp = self.client.get(url, **HTMX)
        self.assertContains(resp, 'data-task-state="processing"')
        self.assertContains(resp, "Extracting...")
        self.assertContains(resp, f'hx-get="{url}"')

    def test_status_endpoint_stops_polling_when_finished(self):
        task = client.enqueue([self.archive.pk], task_type="extract_worker", user=self.expand_user)
        url = reverse("viewer:task-status", args=[task.pk])

        task.status = TaskQueue.Status.COMPLETED
        task.save()
        resp = self.client.get(url, **HTMX)
        self.assertContains(resp, 'data-task-state="completed"')
        self.assertContains(resp, "Extracted")
        self.assertNotContains(resp, "hx-get")

        task.status = TaskQueue.Status.FAILED
        task.error_message = "Corrupt zip"
        task.save()
        resp = self.client.get(url, **HTMX)
        self.assertContains(resp, 'data-task-state="failed"')
        self.assertContains(resp, "Corrupt zip")
        self.assertNotContains(resp, "hx-get")

    def test_status_endpoint_hides_other_users_tasks(self):
        task = client.enqueue([self.archive.pk], task_type="extract_worker", user=self.other_user)
        url = reverse("viewer:task-status", args=[task.pk])

        resp = self.client.get(url, **HTMX)
        self.assertContains(resp, 'data-task-state="missing"')
        self.assertNotContains(resp, "hx-get")

        self.client.force_login(self.staff_user)
        resp = self.client.get(url, **HTMX)
        self.assertContains(resp, 'data-task-state="pending"')

    def test_status_endpoint_unknown_task(self):
        resp = self.client.get(reverse("viewer:task-status", args=[999999]), **HTMX)
        self.assertContains(resp, 'data-task-state="missing"')

    def test_archive_page_renders_polling_status_for_active_tasks(self):
        task = client.enqueue([self.archive.pk], task_type="extract_worker", user=self.expand_user)
        self.client.force_login(self.staff_user)

        resp = self.client.get(reverse("viewer:archive-new", args=[self.archive.pk]))

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tasks"]["extract"], task)
        self.assertTrue(resp.context["is_extracting"])
        content = resp.content.decode()
        self.assertIn(f'hx-get="{reverse("viewer:task-status", args=[task.pk])}"', content)
        self.assertNotIn("btn-extract-archive", content)
        self.assertIn("js/task_actions.js", content)


class ActiveArchiveTasksTests(TestCase):
    def test_finds_active_tasks_by_key_and_alias(self):
        extract = client.enqueue([1], task_type="extract_archives")  # Alias of extract_worker.
        client.enqueue([2], task_type="reduce_worker")
        done = client.enqueue([1], task_type="publish_worker")
        done.status = TaskQueue.Status.COMPLETED
        done.save()

        found = client.get_active_archive_tasks(
            1, {"extract": "extract_worker", "reduce": "reduce_worker", "publish": "publish_worker"}
        )

        self.assertEqual(found, {"extract": extract})
        self.assertTrue(client.is_archive_task_active(1, "extract_worker"))
        self.assertFalse(client.is_archive_task_active(2, "extract_worker"))

    def test_processing_task_wins_over_pending(self):
        pending = client.enqueue([1, 2], task_type="extract_worker")
        processing = client.enqueue([1], task_type="extract_worker")
        processing.status = TaskQueue.Status.PROCESSING
        processing.save()

        self.assertEqual(client.get_active_archive_task(1, "extract_worker"), processing)
        self.assertEqual(client.get_active_archive_task(2, "extract_worker"), pending)

    def test_tasks_ahead(self):
        now = django_tz.now()
        first = client.enqueue([1], task_type="extract_worker")
        first.created_at = now - timedelta(minutes=2)
        first.save()
        urgent = client.enqueue([2], task_type="extract_worker", priority=1)
        client.enqueue([3], task_type="reduce_worker")  # Another queue.
        task = client.enqueue([4], task_type="extract_worker")

        self.assertEqual(client.tasks_ahead(task), 2)
        self.assertEqual(client.tasks_ahead(urgent), 0)
        urgent.status = TaskQueue.Status.PROCESSING
        urgent.save()
        self.assertEqual(client.tasks_ahead(task), 1)


class GalleryTaskActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_user = User.objects.create_user(username="gal_staff", password="password123", is_staff=True)
        cls.collab_user = User.objects.create_user(username="gal_collab", password="password123")
        cls.collab_user.user_permissions.add(
            Permission.objects.get(codename="update_metadata"),
            Permission.objects.get(codename="download_gallery"),
        )
        cls.gallery = Gallery.objects.create(
            gid="123456",
            token="abcdef1234",
            provider="panda",
            title="Test Gallery",
            thumbnail_url="https://example.invalid/thumb.jpg",
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.collab_user)

    def test_gallery_recall_api_htmx_returns_polling_fragment(self):
        url = reverse("viewer:gallery-tool", args=[self.gallery.pk, "recall-api"])
        resp = self.client.post(url, **HTMX)

        self.assertEqual(resp.status_code, 200)
        task = TaskQueue.objects.get(task_type="gallery_updater")
        self.assertIn(self.gallery.get_link(), task.args)
        content = resp.content.decode()
        self.assertIn(f'data-task-id="{task.pk}"', content)
        self.assertIn("Updating metadata: queued", content)
        self.assertIn('class="btn btn-sm btn-outline-warning task-status-btn"', content)

    def test_gallery_download_htmx_returns_polling_fragment(self):
        url = reverse("viewer:gallery-tool", args=[self.gallery.pk, "download"])
        resp = self.client.post(url, **HTMX)

        self.assertEqual(resp.status_code, 200)
        task = TaskQueue.objects.get(task_type="web_crawler")
        self.assertEqual(task.args, [self.gallery.get_link()])
        content = resp.content.decode()
        self.assertIn(f'data-task-id="{task.pk}"', content)
        self.assertIn("Downloading: queued", content)

    def test_gallery_refetch_thumbnail_queues_worker_and_polls(self):
        url = reverse("viewer:gallery-tool", args=[self.gallery.pk, "refetch-thumbnail"])
        resp = self.client.post(url, **HTMX)

        self.assertEqual(resp.status_code, 200)
        task = TaskQueue.objects.get(task_type="gallery_thumbnail_worker")
        self.assertEqual(task.args, [self.gallery.pk])
        content = resp.content.decode()
        self.assertIn(f'data-task-id="{task.pk}"', content)
        self.assertIn("Refetching thumbnail: queued", content)

        # Executing the task in WorkerRunner invokes Gallery.fetch_thumbnail.
        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        task.status = TaskQueue.Status.PROCESSING
        task.save()
        with patch.object(Gallery, "fetch_thumbnail", return_value=True) as mock_fetch:
            runner.execute_job(task)
            mock_fetch.assert_called_once_with(force_redownload=True)

    def test_gallery_refetch_thumbnail_without_url_redirects_with_warning(self):
        no_thumb = Gallery.objects.create(gid="999999", provider="panda", title="No Thumb", thumbnail_url="")
        url = reverse("viewer:gallery-tool", args=[no_thumb.pk, "refetch-thumbnail"])
        resp = self.client.post(url, **HTMX)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Redirect"], no_thumb.get_absolute_url())
        self.assertFalse(TaskQueue.objects.filter(task_type="gallery_thumbnail_worker").exists())

    def test_gallery_new_renders_active_tasks_and_task_actions_js(self):
        recall_task = client.enqueue_gallery_update([self.gallery.get_link()], user=self.collab_user)
        thumb_task = client.enqueue([self.gallery.pk], task_type="gallery_thumbnail_worker", user=self.collab_user)
        dl_task = client.enqueue([self.gallery.get_link()], task_type="web_crawler", user=self.collab_user)

        resp = self.client.get(reverse("viewer:gallery-new", args=[self.gallery.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tasks"]["recall_api"], recall_task)
        self.assertEqual(resp.context["active_tasks"]["refetch_thumbnail"], thumb_task)
        self.assertEqual(resp.context["active_tasks"]["download"], dl_task)
        content = resp.content.decode()
        self.assertIn(f'data-task-id="{recall_task.pk}"', content)
        self.assertIn(f'data-task-id="{thumb_task.pk}"', content)
        self.assertIn(f'data-task-id="{dl_task.pk}"', content)
        self.assertIn("js/task_actions.js", content)


class AdminToolsTaskActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_user = User.objects.create_superuser(
            username="tools_admin", email="tools@example.com", password="password123"
        )
        cls.archive = Archive.objects.create(
            title="Missing Thumb Archive", thumbnail="", crc32="", filesize=0, filecount=0, user=cls.staff_user
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff_user)

    def test_tools_htmx_post_returns_block_polling_fragment(self):
        resp = self.client.post(
            reverse("viewer:tools-id", args=["generate_missing_thumbs"]),
            {"_task_variant": "block"},
            **HTMX,
        )
        self.assertEqual(resp.status_code, 200)
        task = TaskQueue.objects.get(task_type="thumbnails_worker")
        content = resp.content.decode()
        self.assertIn(f'data-task-id="{task.pk}"', content)
        self.assertIn("task-status-block", content)
        self.assertIn("variant=block", content)
        self.assertIn("Generating thumbnails: queued", content)

        # Polling preserves the block variant.
        poll = self.client.get(reverse("viewer:task-status", args=[task.pk]) + "?variant=block", **HTMX)
        self.assertContains(poll, "task-status-block")
        self.assertContains(poll, "variant=block")

    def test_tools_htmx_already_running_follows_existing_task(self):
        existing = TaskQueue.objects.create(task_type="thumbnails_worker", status=TaskQueue.Status.PROCESSING)
        resp = self.client.post(
            reverse("viewer:tools-id", args=["generate_missing_thumbs"]),
            {"_task_variant": "block"},
            **HTMX,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f'data-task-id="{existing.pk}"', resp.content.decode())
        self.assertIn("Generating thumbnails...", resp.content.decode())

    def test_tools_htmx_invalid_date_uses_hx_redirect(self):
        resp = self.client.post(
            reverse("viewer:tools-id", args=["update_newer_than"]),
            {"newer_than": "not-a-date", "_task_variant": "block"},
            **HTMX,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Redirect"], reverse("viewer:tools"))

    def test_tools_page_renders_active_tasks(self):
        thumb_task = client.enqueue([self.archive.pk], task_type="thumbnails_worker", user=self.staff_user)
        retry_task = client.enqueue(["--retry-failed"], task_type="web_crawler", user=self.staff_user)
        wanted = WantedGallery.objects.create(title="Wanted 1", book_type="doujinshi", should_search=True, found=False)
        wanted_task = client.enqueue_matching_task(
            task_type="wanted_local_search_worker", args=[wanted], user=self.staff_user
        )

        resp = self.client.get(reverse("viewer:tools"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["active_tasks"]["generate_missing_thumbs"], thumb_task)
        self.assertEqual(resp.context["active_tasks"]["retry_failed"], retry_task)
        self.assertEqual(resp.context["active_tasks"]["wanted_galleries_possible_matches"], wanted_task)
        content = resp.content.decode()
        self.assertIn(f'data-task-id="{thumb_task.pk}"', content)
        self.assertIn("Retrying failed: queued", content)
        self.assertIn("js/task_actions.js", content)


class TaskBatchStatusAndArchiveManagerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = User.objects.create_user(username="batch_mgr", password="password123")
        cls.manager.user_permissions.add(
            Permission.objects.get(codename="manage_archive"),
            Permission.objects.get(codename="delete_archive"),
            Permission.objects.get(codename="update_metadata"),
            Permission.objects.get(codename="extract_contextual_info"),
        )
        cls.other_user = User.objects.create_user(username="batch_other", password="password123")
        cls.staff_user = User.objects.create_user(username="batch_staff", password="password123", is_staff=True)
        cls.gallery = Gallery.objects.create(gid="777", token="tok777", provider="panda", title="Batch Gallery")
        cls.archive1 = Archive.objects.create(title="Batch A1", gallery=cls.gallery, user=cls.manager)
        cls.archive2 = Archive.objects.create(title="Batch A2", gallery=cls.gallery, user=cls.manager)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.manager)

    def test_archive_manager_json_returns_task_ids_for_background_bulk_actions(self):
        url = reverse("viewer:archive-manager") + "?json=1"

        # 1. delete_files_archives
        resp = self.client.post(
            url,
            {
                "delete_files_archives": "1",
                f"sel-{self.archive1.pk}": str(self.archive1.pk),
                f"sel-{self.archive2.pk}": str(self.archive2.pk),
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data["task_ids"]), 2)
        self.assertEqual(data["batch_status_url"], reverse("viewer:task-batch-status"))
        self.assertEqual(
            data["status_url"], f"{reverse('viewer:api-v1')}?tasks={data['task_ids'][0]},{data['task_ids'][1]}"
        )
        self.assertEqual(self.client.get(data["status_url"]).json()["total"], 2)

        # 2. update_metadata
        resp = self.client.post(
            url,
            {"update_metadata": "1", f"sel-{self.archive1.pk}": str(self.archive1.pk)},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data["task_ids"]), 1)
        # One queued task still gets the batch URL: Archive Manager reads the batch summary (total, tasks, ...).
        self.assertEqual(data["status_url"], f"{reverse('viewer:api-v1')}?tasks={data['task_ids'][0]}")
        batch = self.client.get(data["status_url"]).json()
        self.assertEqual(batch["total"], 1)
        self.assertEqual([t["id"] for t in batch["tasks"]], data["task_ids"])
        self.assertEqual(batch["status_url"], data["status_url"])

        # 3. extract_contextual_info
        with patch.object(settings.CRAWLER_SETTINGS.manga_extractor, "enable", True):
            resp = self.client.post(
                url,
                {
                    "extract_contextual_info": "1",
                    f"sel-{self.archive1.pk}": str(self.archive1.pk),
                    "profile": "default",
                },
            )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(len(data["task_ids"]), 1)

    def test_batch_status_endpoint_summarizes_and_enforces_visibility(self):
        t1 = client.enqueue([self.archive1.pk], task_type="delete_archive_worker", user=self.manager)
        t2 = client.enqueue([self.archive2.pk], task_type="delete_archive_worker", user=self.manager)
        t3 = client.enqueue([self.archive1.pk], task_type="extract_worker", user=self.other_user)
        t1.status = TaskQueue.Status.COMPLETED
        t1.save()
        t2.status = TaskQueue.Status.PROCESSING
        t2.started_at = django_tz.now()
        t2.save()

        url = reverse("viewer:task-batch-status")
        resp = self.client.get(url, {"ids": f"{t1.pk},{t2.pk},{t3.pk}"})
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["completed"], 1)
        self.assertEqual(body["processing"], 1)
        self.assertEqual(body["missing"], 1)  # t3 belongs to other_user
        self.assertTrue(body["active"])
        self.assertGreater(body["poll_delay_ms"], 0)

        # Staff can see all three tasks.
        self.client.force_login(self.staff_user)
        resp = self.client.get(url, {"ids": f"{t1.pk},{t2.pk},{t3.pk}"})
        body = json.loads(resp.content)
        self.assertEqual(body["missing"], 0)
        self.assertEqual(body["pending"], 1)

        # Unauthenticated gets 403.
        self.client.logout()
        self.assertEqual(self.client.get(url, {"ids": str(t1.pk)}).status_code, 403)

        # Read-scoped bearer token works too.
        _, raw_token = UserLongLivedToken.create_for_user(
            self.manager, "read-tok", scopes=[UserLongLivedToken.SCOPE_READ]
        )
        resp = self.client.get(url, {"ids": str(t1.pk)}, HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)["completed"], 1)

    def test_no_worker_running_does_not_render_inline_sibling_badge(self):
        task = client.enqueue([self.archive1.pk], task_type="extract_worker", user=self.manager)
        resp = self.client.get(reverse("viewer:task-status", args=[task.pk]), **HTMX)
        content = resp.content.decode()
        self.assertIn('data-worker-running="false"', content)
        self.assertIn("task-status-btn", content)
        self.assertIn("task-status-text", content)
        self.assertNotIn('<span class="badge bg-danger"', content)
