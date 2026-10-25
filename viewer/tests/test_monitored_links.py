# -*- coding: utf-8 -*-
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse

import django.utils.timezone as django_tz
from core.base.setup import Settings
from viewer.models import MonitoredLink, Provider
from workers.models import SchedulerState, TaskQueue
from viewer.templatetags.dict_key import dict_key
from workers.runner import WorkerRunner

User = get_user_model()


class MonitoredLinkModelTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="linkuser", password="password123")
        self.provider = Provider.objects.create(name="Test Provider", slug="test-prov")

    def test_save_creates_scheduler_state(self):
        link = MonitoredLink.objects.create(
            name="RSS Feed",
            url="https://example.com/feed.xml",
            frequency=timedelta(minutes=30),
            provider=self.provider,
            enabled=True,
            auto_start=False,
            user=self.user,
        )
        state = SchedulerState.objects.filter(name=link.scheduler_name).first()
        self.assertIsNotNone(state)
        self.assertEqual(state.cycle_timer, 1800.0)
        self.assertEqual(state.display_name, f"MonitoredLink: {link.name}")
        self.assertEqual(state.command_requested, "reload")

    def test_save_auto_start_requests_start_command(self):
        link = MonitoredLink.objects.create(
            name="Auto Link",
            url="https://example.com/auto.xml",
            frequency=timedelta(hours=1),
            enabled=True,
            auto_start=True,
        )
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "start")
        self.assertEqual(state.command_arg, "3600.0")

    def test_save_disabled_requests_stop_command(self):
        link = MonitoredLink.objects.create(
            name="Disabled Link",
            url="https://example.com/disabled.xml",
            frequency=timedelta(hours=2),
            enabled=False,
            auto_start=True,
        )
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "stop")

    def test_start_running_requests_command(self):
        link = MonitoredLink.objects.create(
            name="Start Test",
            url="https://example.com/start.xml",
            frequency=timedelta(minutes=15),
            enabled=True,
        )
        link.start_running()
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "start")
        self.assertEqual(state.command_arg, "900.0")

    def test_stop_running_requests_command(self):
        link = MonitoredLink.objects.create(
            name="Stop Test",
            url="https://example.com/stop.xml",
            frequency=timedelta(minutes=15),
            enabled=True,
        )
        link.stop_running()
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "stop")

    def test_force_run_requests_command(self):
        link = MonitoredLink.objects.create(
            name="Force Run Test",
            url="https://example.com/force.xml",
            frequency=timedelta(minutes=15),
            enabled=True,
        )
        link.force_run()
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "force_run")

    def test_delete_requests_delete_command_and_removes_link(self):
        link = MonitoredLink.objects.create(
            name="Delete Test",
            url="https://example.com/delete.xml",
            frequency=timedelta(minutes=15),
            enabled=True,
        )
        sched_name = link.scheduler_name
        link_pk = link.pk
        link.delete()

        self.assertFalse(MonitoredLink.objects.filter(pk=link_pk).exists())
        state = SchedulerState.objects.filter(name=sched_name).first()
        self.assertIsNotNone(state)
        self.assertEqual(state.command_requested, "delete")

    def test_is_running_property(self):
        link = MonitoredLink.objects.create(
            name="Running Check",
            url="https://example.com/run.xml",
            frequency=timedelta(hours=1),
            enabled=True,
        )
        self.assertFalse(link.is_running)

        SchedulerState.objects.filter(name=link.scheduler_name).update(
            is_running=True,
            last_heartbeat=django_tz.now(),
        )
        self.assertTrue(link.is_running)


class MonitoredLinkWorkerRunnerTests(TestCase):

    def setUp(self):
        self.settings = Settings(load_from_disk=False)
        self.settings.is_worker_process = True
        self.settings.monitored_links.enable = True
        self.runner = WorkerRunner(
            settings=self.settings,
            enable_schedulers=True,
        )
        # Ensure fresh timed_link_monitors list
        self.settings.workers.timed_link_monitors = []

    def tearDown(self):
        # Stop any running threads and wait for them to finish
        for lm in list(self.settings.workers.timed_link_monitors):
            lm.stop_running(force_local=True)
            if lm.job_thread and lm.job_thread.is_alive():
                lm.job_thread.join(timeout=2.0)
        self.settings.workers.timed_link_monitors = []

    def test_worker_runner_handles_start_for_new_link(self):
        link = MonitoredLink.objects.create(
            name="Dynamic Link",
            url="https://example.com/dynamic.xml",
            frequency=timedelta(minutes=10),
            enabled=True,
            auto_start=True,
        )
        self.assertEqual(len(self.settings.workers.timed_link_monitors), 0)

        # Worker runner processes scheduler commands
        self.runner.check_scheduler_commands()

        # Monitor should be dynamically created and running locally
        self.assertEqual(len(self.settings.workers.timed_link_monitors), 1)
        monitor = self.settings.workers.timed_link_monitors[0]
        self.assertEqual(monitor.monitored_link.pk, link.pk)
        self.assertEqual(monitor.timer, 600.0)
        self.assertTrue(monitor.is_running_locally())

        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "")
        self.assertTrue(state.is_running)

    def test_worker_runner_handles_stop(self):
        link = MonitoredLink.objects.create(
            name="Stop Dyn Link",
            url="https://example.com/stopdyn.xml",
            frequency=timedelta(minutes=10),
            enabled=True,
            auto_start=True,
        )
        self.runner.check_scheduler_commands()
        monitor = self.settings.workers.timed_link_monitors[0]
        self.assertTrue(monitor.is_running_locally())

        # Now dispatch stop command
        link.stop_running()
        self.runner.check_scheduler_commands()

        self.assertFalse(monitor.is_running_locally())
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertFalse(state.is_running)
        self.assertEqual(state.command_requested, "")

    def test_worker_runner_handles_reload_updates_frequency(self):
        link = MonitoredLink.objects.create(
            name="Reload Dyn Link",
            url="https://example.com/reloaddyn.xml",
            frequency=timedelta(minutes=10),
            enabled=True,
            auto_start=True,
        )
        self.runner.check_scheduler_commands()
        monitor = self.settings.workers.timed_link_monitors[0]
        self.assertEqual(monitor.timer, 600.0)

        # Update frequency to 2 hours (7200s)
        link.frequency = timedelta(hours=2)
        link.save()

        self.runner.check_scheduler_commands()
        self.assertEqual(monitor.timer, 7200.0)
        self.assertTrue(monitor.is_running_locally())

    def test_worker_runner_handles_reload_when_disabled(self):
        link = MonitoredLink.objects.create(
            name="Disable Dyn Link",
            url="https://example.com/disable.xml",
            frequency=timedelta(minutes=10),
            enabled=True,
            auto_start=True,
        )
        self.runner.check_scheduler_commands()
        monitor = self.settings.workers.timed_link_monitors[0]
        self.assertTrue(monitor.is_running_locally())

        link.enabled = False
        link.save()

        self.runner.check_scheduler_commands()
        self.assertFalse(monitor.is_running_locally())

    def test_worker_runner_handles_delete(self):
        link = MonitoredLink.objects.create(
            name="Delete Dyn Link",
            url="https://example.com/deldyn.xml",
            frequency=timedelta(minutes=10),
            enabled=True,
            auto_start=True,
        )
        sched_name = link.scheduler_name
        self.runner.check_scheduler_commands()
        self.assertEqual(len(self.settings.workers.timed_link_monitors), 1)

        link.delete()
        self.runner.check_scheduler_commands()

        self.assertEqual(len(self.settings.workers.timed_link_monitors), 0)
        self.assertFalse(SchedulerState.objects.filter(name=sched_name).exists())

    def test_worker_runner_sync_monitored_links_cleans_deleted(self):
        link = MonitoredLink.objects.create(
            name="Prune Link",
            url="https://example.com/prune.xml",
            frequency=timedelta(minutes=5),
            enabled=True,
            auto_start=True,
        )
        self.runner.check_scheduler_commands()
        self.assertEqual(len(self.settings.workers.timed_link_monitors), 1)

        # Delete row directly from DB without model delete() command
        MonitoredLink.objects.filter(pk=link.pk).delete()
        self.runner.sync_monitored_links()

        self.assertEqual(len(self.settings.workers.timed_link_monitors), 0)


class MonitoredLinkViewsTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="viewer_user", password="password123")
        perm = Permission.objects.get(codename="view_monitored_links")
        self.user.user_permissions.add(perm)

        self.client = Client()
        self.client.login(username="viewer_user", password="password123")
        self.provider = Provider.objects.create(name="Main Provider", slug="main-prov")

    def test_monitored_links_view_requires_permission(self):
        anon_client = Client()
        res = anon_client.get(reverse("viewer:monitored-links"))
        # PermissionRequiredMixin redirects anonymous users to login
        self.assertEqual(res.status_code, 302)

    def test_monitored_links_view_returns_200(self):
        link = MonitoredLink.objects.create(
            name="Feed A",
            url="https://example.com/a.xml",
            frequency=timedelta(minutes=30),
            provider=self.provider,
            enabled=True,
        )
        res = self.client.get(reverse("viewer:monitored-links"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Feed A")
        self.assertContains(res, "https://example.com/a.xml")

    def test_monitored_links_view_filtering(self):
        MonitoredLink.objects.create(
            name="Alpha RSS",
            url="https://alpha.com/feed",
            frequency=timedelta(minutes=10),
            provider=self.provider,
            enabled=True,
            auto_start=True,
        )
        MonitoredLink.objects.create(
            name="Beta RSS",
            url="https://beta.com/feed",
            frequency=timedelta(minutes=60),
            provider=None,
            enabled=False,
            auto_start=False,
        )

        # Search by name
        res = self.client.get(reverse("viewer:monitored-links"), {"name": "Alpha"})
        self.assertContains(res, "Alpha RSS")
        self.assertNotContains(res, "Beta RSS")

        # Filter by enabled
        res_disabled = self.client.get(reverse("viewer:monitored-links"), {"enabled": "no"})
        self.assertContains(res_disabled, "Beta RSS")
        self.assertNotContains(res_disabled, "Alpha RSS")

    def test_monitored_links_inline_create(self):
        post_data = {
            "name": "New Inline Link",
            "url": "https://example.com/inline",
            "provider": self.provider.pk,
            "frequency": "00:45:00",
            "enabled": True,
            "auto_start": True,
            "submit-monitored-link": "1",
        }
        res = self.client.post(reverse("viewer:monitored-links"), post_data)
        self.assertEqual(res.status_code, 302)

        link = MonitoredLink.objects.filter(name="New Inline Link").first()
        self.assertIsNotNone(link)
        self.assertEqual(link.frequency, timedelta(minutes=45))
        self.assertTrue(link.enabled)
        self.assertEqual(link.user, self.user)

    def test_create_monitored_link_view(self):
        res = self.client.get(reverse("viewer:col-create-monitored-link"))
        self.assertEqual(res.status_code, 200)

        post_data = {
            "name": "Page Created Link",
            "url": "https://example.com/page",
            "frequency": "01:30:00",
            "enabled": True,
        }
        res_post = self.client.post(reverse("viewer:col-create-monitored-link"), post_data)
        self.assertEqual(res_post.status_code, 302)
        self.assertTrue(MonitoredLink.objects.filter(name="Page Created Link").exists())

    def test_edit_monitored_link_view(self):
        link = MonitoredLink.objects.create(
            name="Before Edit",
            url="https://example.com/before",
            frequency=timedelta(minutes=20),
            enabled=False,
        )
        res = self.client.get(reverse("viewer:col-edit-monitored-link", args=[link.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Before Edit")

        post_data = {
            "name": "After Edit",
            "url": "https://example.com/after",
            "frequency": "02:00:00",
            "enabled": True,
            "auto_start": True,
        }
        res_post = self.client.post(reverse("viewer:col-edit-monitored-link", args=[link.pk]), post_data)
        self.assertEqual(res_post.status_code, 302)

        link.refresh_from_db()
        self.assertEqual(link.name, "After Edit")
        self.assertEqual(link.frequency, timedelta(hours=2))
        self.assertTrue(link.enabled)

    def test_delete_monitored_link_view(self):
        link = MonitoredLink.objects.create(
            name="To Be Deleted",
            url="https://example.com/delete-me",
            frequency=timedelta(minutes=10),
        )
        res_get = self.client.get(reverse("viewer:col-delete-monitored-link", args=[link.pk]))
        self.assertEqual(res_get.status_code, 200)
        self.assertContains(res_get, "To Be Deleted")

        res_post = self.client.post(reverse("viewer:col-delete-monitored-link", args=[link.pk]))
        self.assertEqual(res_post.status_code, 302)
        self.assertFalse(MonitoredLink.objects.filter(pk=link.pk).exists())

    def test_monitored_link_action_view(self):
        link = MonitoredLink.objects.create(
            name="Action Link",
            url="https://example.com/action",
            frequency=timedelta(minutes=25),
            enabled=True,
        )
        # Start
        res = self.client.get(reverse("viewer:col-monitored-link-action", args=[link.pk, "start"]))
        self.assertEqual(res.status_code, 302)
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertEqual(state.command_requested, "start")

        # Stop
        res = self.client.get(reverse("viewer:col-monitored-link-action", args=[link.pk, "stop"]))
        self.assertEqual(res.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "stop")

        # Force run
        res = self.client.get(reverse("viewer:col-monitored-link-action", args=[link.pk, "force_run"]))
        self.assertEqual(res.status_code, 302)
        state.refresh_from_db()
        self.assertEqual(state.command_requested, "force_run")

    def test_monitored_links_view_renders_last_run_and_next_run(self):
        link = MonitoredLink.objects.create(
            name="Run Feed",
            url="https://example.com/runfeed.xml",
            frequency=timedelta(hours=1),
            enabled=True,
        )
        now = django_tz.now()
        past = now - timedelta(minutes=15)
        future = now + timedelta(minutes=45)
        SchedulerState.objects.filter(name=link.scheduler_name).update(
            last_run=past,
            next_run=future,
            is_running=True,
            last_heartbeat=now,
        )

        res = self.client.get(reverse("viewer:monitored-links"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Run Feed")
        self.assertContains(res, past.strftime("%y-%m-%d"))
        self.assertContains(res, future.strftime("%y-%m-%d"))

    def test_monitored_links_view_backfills_last_run_from_task_queue(self):
        link = MonitoredLink.objects.create(
            name="Backfill Link",
            url="https://example.com/backfill.xml",
            frequency=timedelta(hours=1),
            enabled=True,
        )
        # Ensure SchedulerState exists with null last_run
        SchedulerState.objects.filter(name=link.scheduler_name).update(last_run=None, next_run=None)

        task_finished = django_tz.now() - timedelta(minutes=20)
        TaskQueue.objects.create(
            task_type="web_crawler",
            args=[link.url, "-wanted"],
            status=TaskQueue.Status.COMPLETED,
            finished_at=task_finished,
        )

        res = self.client.get(reverse("viewer:monitored-links"))
        self.assertEqual(res.status_code, 200)
        state = SchedulerState.objects.get(name=link.scheduler_name)
        self.assertIsNotNone(state.last_run)
        self.assertIsNotNone(state.next_run)
        self.assertEqual(state.last_run, task_finished)
        self.assertContains(res, task_finished.strftime("%y-%m-%d"))

    def test_dict_key_templatetag(self):
        # Dictionary lookup
        self.assertEqual(dict_key({"a": 10, "b": 20}, "a"), 10)
        self.assertEqual(dict_key({"a": 10}, "c"), "")
        self.assertEqual(dict_key(None, "a"), "")

        # Sequence / tuple indexing
        t = ("item0", "item1", "item2")
        self.assertEqual(dict_key(t, 0), "item0")
        self.assertEqual(dict_key(t, 2), "item2")
        self.assertEqual(dict_key(t, "1"), "item1")
        self.assertEqual(dict_key(t, 99), "")

        # Object attribute lookup
        class Dummy:
            def __init__(self):
                self.title = "test"
        self.assertEqual(dict_key(Dummy(), "title"), "test")

