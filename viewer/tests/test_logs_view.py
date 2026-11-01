import datetime
from django.contrib.auth.models import User
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone
from django_db_logger.models import StatusLog


class LogsViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin_user",
            email="admin@example.com",
            password="testpassword",
        )
        self.normal_user = User.objects.create_user(
            username="normal_user",
            email="normal@example.com",
            password="testpassword",
        )

        now = timezone.now()
        # Create sample StatusLog entries with different dates
        # Log 1: 10 days old
        self.log_old_10 = StatusLog.objects.create(
            logger_name="test_logger",
            msg="Log from 10 days ago",
            level=20,
        )
        StatusLog.objects.filter(id=self.log_old_10.id).update(
            create_datetime=now - datetime.timedelta(days=10)
        )

        # Log 2: 3 days old
        self.log_old_3 = StatusLog.objects.create(
            logger_name="test_logger",
            msg="Log from 3 days ago",
            level=30,
        )
        StatusLog.objects.filter(id=self.log_old_3.id).update(
            create_datetime=now - datetime.timedelta(days=3)
        )

        # Log 3: 2 hours old (today)
        self.log_recent = StatusLog.objects.create(
            logger_name="test_logger",
            msg="Recent log entry",
            level=40,
        )
        StatusLog.objects.filter(id=self.log_recent.id).update(
            create_datetime=now - datetime.timedelta(hours=2)
        )

    def test_logs_view_requires_admin(self):
        # Anonymous user gets redirected to login
        response = self.client.get(reverse("viewer:logs"))
        self.assertEqual(response.status_code, 302)

        # Normal non-staff user gets redirected with error message
        self.client.force_login(self.normal_user)
        response = self.client.get(reverse("viewer:logs"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "You need to be an admin to see logs.")

    def test_logs_view_admin_access(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("viewer:logs"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Server logs")
        self.assertContains(response, "Delete logs")
        self.assertContains(response, "deleteLogsModal")
        self.assertContains(response, "Recent log entry")

    def test_delete_logs_older_than_7_days(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(
            reverse("viewer:logs"),
            {"action": "delete_logs", "delete_type": "7_days"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # 10 days old log should be deleted, 3 days and recent should remain
        self.assertFalse(StatusLog.objects.filter(id=self.log_old_10.id).exists())
        self.assertTrue(StatusLog.objects.filter(id=self.log_old_3.id).exists())
        self.assertTrue(StatusLog.objects.filter(id=self.log_recent.id).exists())

    def test_delete_logs_older_than_1_day(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(
            reverse("viewer:logs"),
            {"action": "delete_logs", "delete_type": "1_day"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        # 10 days and 3 days old logs should be deleted, recent remains
        self.assertFalse(StatusLog.objects.filter(id=self.log_old_10.id).exists())
        self.assertFalse(StatusLog.objects.filter(id=self.log_old_3.id).exists())
        self.assertTrue(StatusLog.objects.filter(id=self.log_recent.id).exists())

    def test_delete_logs_custom_date(self):
        self.client.login(username="admin_user", password="testpassword")
        # Target cutoff between 10 days ago and 3 days ago (e.g. 5 days ago)
        cutoff_date = (timezone.now() - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        response = self.client.post(
            reverse("viewer:logs"),
            {
                "action": "delete_logs",
                "delete_type": "custom",
                "custom_date": cutoff_date,
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StatusLog.objects.filter(id=self.log_old_10.id).exists())
        self.assertTrue(StatusLog.objects.filter(id=self.log_old_3.id).exists())
        self.assertTrue(StatusLog.objects.filter(id=self.log_recent.id).exists())

    def test_delete_all_logs(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(
            reverse("viewer:logs"),
            {"action": "delete_logs", "delete_type": "all"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(StatusLog.objects.count(), 0)

    def test_delete_logs_json_request(self):
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.post(
            reverse("viewer:logs"),
            {"action": "delete_logs", "delete_type": "all", "json": "1"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["deleted"], 3)

    def test_logs_view_context_filter(self):
        # Create a sequence of 7 logs with identifiable content
        # Clear existing logs for clarity
        StatusLog.objects.all().delete()
        now = timezone.now()
        logs_created = []
        for i in range(1, 8):
            l = StatusLog.objects.create(
                logger_name="test_logger",
                msg=f"Log sequence step {i}" if i != 4 else "CRITICAL error occurred at step 4",
                level=50 if i == 4 else 20,
            )
            StatusLog.objects.filter(id=l.id).update(
                create_datetime=now + datetime.timedelta(seconds=i)
            )
            logs_created.append(l)

        self.client.login(username="admin_user", password="testpassword")

        # 1. Filter without context: only matching log (step 4) should be returned
        response = self.client.get(reverse("viewer:logs"), {"filter": "CRITICAL", "context": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["display_logs"]), 1)
        self.assertEqual(response.context["display_logs"][0].id, logs_created[3].id)
        self.assertTrue(response.context["display_logs"][0].is_match)

        # 2. Filter with context=1: step 4 plus 1 before (step 3) and 1 after (step 5)
        response = self.client.get(reverse("viewer:logs"), {"filter": "CRITICAL", "context": "1"})
        self.assertEqual(response.status_code, 200)
        display_logs = response.context["display_logs"]
        self.assertEqual(len(display_logs), 3)
        display_ids = [l.id for l in display_logs]
        self.assertIn(logs_created[2].id, display_ids)  # step 3
        self.assertIn(logs_created[3].id, display_ids)  # step 4 (match)
        self.assertIn(logs_created[4].id, display_ids)  # step 5

        # Check is_match property
        for log_entry in display_logs:
            if log_entry.id == logs_created[3].id:
                self.assertTrue(log_entry.is_match)
            else:
                self.assertFalse(log_entry.is_match)

        # 3. Filter with JSON output and context=2
        response = self.client.get(
            reverse("viewer:logs"),
            {"filter": "CRITICAL", "context": "2", "json": "1"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        results = data["results"]
        # With context=2 around step 4: steps 2, 3, 4, 5, 6 should be returned (5 total)
        self.assertEqual(len(results), 5)
        matched_results = [r for r in results if r["is_match"]]
        self.assertEqual(len(matched_results), 1)
        self.assertEqual(matched_results[0]["id"], logs_created[3].id)

