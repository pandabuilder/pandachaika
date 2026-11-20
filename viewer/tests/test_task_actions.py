# -*- coding: utf-8 -*-
"""Background task actions answered with a polling status fragment for HTMX requests (viewer.utils.task_actions)."""
from datetime import timedelta

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse
import django.utils.timezone as django_tz

from viewer.models import Archive
from workers import client
from workers.models import TaskQueue

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
        resp = self.client.get(reverse("viewer:archive-extract", args=[self.archive.pk]), **HTMX)

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

        resp = self.client.get(reverse("viewer:archive-extract", args=[self.archive.pk]), **HTMX)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TaskQueue.objects.count(), 1)
        self.assertIn(f'data-task-id="{existing.pk}"', resp.content.decode())

    def test_plain_request_keeps_redirect(self):
        resp = self.client.get(
            reverse("viewer:archive-extract", args=[self.archive.pk]),
            HTTP_REFERER=f"http://testserver{reverse('viewer:archive', args=[self.archive.pk])}",
        )

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="extract_worker").count(), 1)
        self.assertEqual([str(m) for m in resp.wsgi_request._messages], ["Queued archive extraction."])

    def test_htmx_without_task_navigates_the_page(self):
        self.archive.extracted = True
        self.archive.save()

        resp = self.client.get(reverse("viewer:archive-extract", args=[self.archive.pk]), **HTMX)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["HX-Redirect"], self.archive.get_absolute_url())
        self.assertFalse(TaskQueue.objects.exists())

    def test_htmx_permission_error_navigates_the_page(self):
        regular = User.objects.create_user(username="regular", password="password123")
        self.client.force_login(regular)
        referer = f"http://testserver{reverse('viewer:archive', args=[self.archive.pk])}"

        resp = self.client.get(reverse("viewer:archive-extract", args=[self.archive.pk]), HTTP_REFERER=referer, **HTMX)

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
