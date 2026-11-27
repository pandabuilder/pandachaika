import datetime

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from viewer.models import EventLog


class MyEventLogViewTests(TestCase):
    user: User
    other: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="event_log_user", password="password123")
        cls.other = User.objects.create_user(username="event_log_other", password="password123")
        EventLog.objects.create(user=cls.user, action="ADD_GALLERY", data="https://example.com/g/1", result="added")
        EventLog.objects.create(user=cls.user, action="DELETE_ARCHIVE", data="deleted one", result="removed")
        old = EventLog.objects.create(user=cls.user, action="ADD_GALLERY", data="old one", result="added")
        EventLog.objects.filter(pk=old.pk).update(create_date=timezone.now() - datetime.timedelta(days=20))
        EventLog.objects.create(user=cls.other, action="SECRET_ACTION", data="not yours", result="hidden")

    def setUp(self):
        self.client = Client()
        self.client.login(username=self.user.username, password="password123")
        self.url = reverse("viewer:my-event-log")

    def test_requires_login(self):
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_lists_only_own_events(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"].paginator.count, 3)
        self.assertEqual(sorted(response.context["actions"]), ["ADD_GALLERY", "DELETE_ARCHIVE"])
        self.assertNotContains(response, "SECRET_ACTION")
        self.assertEqual(response.context["activity"]["last_week"], 2)
        self.assertEqual(response.context["activity"]["distinct_actions"], 2)

    def test_filters(self):
        cases = (
            ({"actions": ["DELETE_ARCHIVE"]}, 1),
            ({"actions": ["ADD_GALLERY", "DELETE_ARCHIVE"]}, 3),
            ({"data_field": "example.com"}, 1),
            ({"result_field": "removed"}, 1),
            ({"event_date_from": (timezone.now() - datetime.timedelta(days=7)).date().isoformat()}, 2),
            ({"event_date_to": (timezone.now() - datetime.timedelta(days=10)).date().isoformat()}, 1),
            ({"content_type": "archive"}, 0),
            ({"content_id": "abc"}, 0),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["results"].paginator.count, expected)

    def test_invalid_date_shows_error(self):
        response = self.client.get(self.url, {"event_date_from": "2026-13-45"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["results"].paginator.count, 0)

    def test_page_size(self):
        response = self.client.get(self.url, {"limit": "1", "page": "2"})
        self.assertEqual(response.context["results"].number, 2)
        self.assertEqual(len(response.context["results"]), 1)


class ActivityEventLogViewTests(TestCase):
    user: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.user = User.objects.create_user(username="activity_log_user", password="password123")
        cls.user.user_permissions.add(Permission.objects.get(codename="read_activity_logs"))
        EventLog.objects.create(user=cls.user, action="ADD_GALLERY", data="first", result="added")
        EventLog.objects.create(action="SYSTEM_JOB", data="second", result="done")

    def test_filters_still_work(self):
        client = Client()
        client.login(username=self.user.username, password="password123")
        url = reverse("viewer:activity-event-log")
        self.assertEqual(client.get(url).context["results"].paginator.count, 2)
        self.assertEqual(client.get(url, {"data_field": "second"}).context["results"].paginator.count, 1)
        self.assertEqual(client.get(url, {"actions": ["ADD_GALLERY"]}).context["results"].paginator.count, 1)
        response = client.get(url)
        self.assertEqual(response.context["activity"]["last_week"], 2)
        self.assertEqual(response.context["activity"]["distinct_actions"], 2)
