"""Tests for /api/v1/: task status, the error envelope, the unified auth path, exp.py's reader commands, legacy
route deprecation, the remote operations API and the OpenAPI document."""

import io
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.db import connection, transaction
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from viewer.models import Archive, Gallery, Image, Tag, UserLongLivedToken, WantedGallery
from viewer.views.api.filters import MAX_API_TAG_FILTERS
from viewer.views.api.openapi import build_openapi_document
from viewer.views.api.registry import COMMANDS
from workers.models import TaskQueue

V1 = "/api/v1/"


def bearer(secret: str) -> dict[str, str]:
    return {"HTTP_AUTHORIZATION": f"Bearer {secret}"}


class ApiV1TestBase(TestCase):
    owner: User
    other: User
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.owner = User.objects.create_user(username="v1_owner", password="password123")
        cls.other = User.objects.create_user(username="v1_other", password="password123")
        cls.staff = User.objects.create_user(username="v1_staff", password="password123", is_staff=True)

    def client_for(self, user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client


class TaskStatusTests(ApiV1TestBase):
    def setUp(self) -> None:
        now = timezone.now()
        self.done = TaskQueue.objects.create(
            task_type="delete_archive_worker",
            args=[12],
            options={"delete_archive": True, "delete_file": True, "secret_setting": "x"},
            user=self.owner,
            status=TaskQueue.Status.COMPLETED,
            created_at=now - timedelta(minutes=2),
            started_at=now - timedelta(minutes=1),
            finished_at=now,
        )
        self.failed = TaskQueue.objects.create(
            task_type="web_crawler",
            args=["https://example.com/g/1", "--ignore-global-filters"],
            options={"crawl_batch_stats": {"providers": {"panda": {"db_total": 1}}}},
            user=self.owner,
            status=TaskQueue.Status.FAILED,
            error_message="boom",
            finished_at=now,
        )
        self.pending = TaskQueue.objects.create(task_type="web_crawler", args=["https://example.com/g/2"], user=self.owner)
        self.foreign = TaskQueue.objects.create(task_type="web_crawler", args=["https://example.com/g/3"], user=self.other)

    def test_owner_sees_task_with_timestamps_and_result(self):
        response = self.client_for(self.owner).get(V1, {"task": self.done.pk})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["id"], self.done.pk)
        self.assertEqual(data["status"], "completed")
        self.assertFalse(data["active"])
        self.assertEqual(data["finished_at"], int(self.done.finished_at.timestamp()))
        self.assertIsInstance(data["created_at"], int)
        self.assertEqual(data["status_url"], f"{V1}?task={self.done.pk}")
        self.assertEqual(data["result"]["outcome"], "completed")
        self.assertEqual(data["result"]["targets"], [12])
        self.assertTrue(data["result"]["delete_archive"])
        self.assertNotIn("secret_setting", data["result"])

    def test_result_includes_what_the_worker_recorded(self):
        self.done.result = {"total_deleted": 3}
        self.done.save(update_fields=["result"])
        data = self.client_for(self.owner).get(V1, {"task": self.done.pk}).json()
        self.assertEqual(data["result"]["details"], {"total_deleted": 3})

    def test_batch_has_the_same_keys_as_the_task_batch_status_endpoint(self):
        ids = [self.done.pk, self.pending.pk, 999999]
        client = self.client_for(self.owner)
        v1 = client.get(V1, {"tasks": ",".join(map(str, ids))}).json()
        legacy = client.get(reverse("viewer:task-batch-status"), {"ids": ",".join(map(str, ids))}).json()
        self.assertLessEqual(set(legacy), set(v1))
        for key in legacy:
            if key != "tasks":
                self.assertEqual(v1[key], legacy[key], key)
        for v1_task, legacy_task in zip(v1["tasks"], legacy["tasks"]):
            self.assertLessEqual(set(legacy_task), set(v1_task))

    def test_failed_task_reports_error_and_crawl_stats(self):
        data = self.client_for(self.owner).get(V1, {"task": self.failed.pk}).json()
        self.assertEqual(data["error_message"], "boom")
        self.assertEqual(data["result"]["targets"], ["https://example.com/g/1"])
        self.assertEqual(data["result"]["crawl_stats"]["providers"]["panda"]["db_total"], 1)

    def test_active_task_has_no_result_and_a_poll_delay(self):
        data = self.client_for(self.owner).get(V1, {"task": self.pending.pk}).json()
        self.assertTrue(data["active"])
        self.assertIsNone(data["result"])
        self.assertGreater(data["poll_delay_ms"], 0)
        self.assertIn("worker_running", data)

    def test_other_users_task_is_not_found(self):
        response = self.client_for(self.owner).get(V1, {"task": self.foreign.pk})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": {"code": "not_found", "message": "Task does not exist."}})

    def test_staff_sees_any_task(self):
        response = self.client_for(self.staff).get(V1, {"task": self.foreign.pk})
        self.assertEqual(response.status_code, 200)

    def test_anonymous_gets_401(self):
        response = Client().get(V1, {"task": self.done.pk})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")
        self.assertIn("Bearer", response["WWW-Authenticate"])

    def test_invalid_task_id_is_400(self):
        response = self.client_for(self.owner).get(V1, {"task": "abc"})
        self.assertEqual(response.status_code, 400)

    def test_read_token_can_poll(self):
        _, secret = UserLongLivedToken.create_for_user(self.owner, "reader", scopes=["read"])
        response = Client().get(V1, {"task": self.done.pk}, **bearer(secret))
        self.assertEqual(response.status_code, 200)

    def test_batch_lookup_reports_missing_and_counts(self):
        ids = f"{self.done.pk},{self.pending.pk},{self.foreign.pk},999999"
        response = self.client_for(self.owner).get(V1, {"tasks": ids})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 4)
        self.assertEqual(data["completed"], 1)
        self.assertEqual(data["pending"], 1)
        self.assertEqual(data["missing"], 2)
        self.assertTrue(data["active"])
        self.assertEqual(data["done"], 3)
        self.assertEqual([t["id"] for t in data["tasks"]], [self.done.pk, self.pending.pk, self.foreign.pk, 999999])
        self.assertEqual(data["tasks"][2]["status"], "missing")
        self.assertFalse(data["tasks"][2]["active"])
        self.assertNotIn("result", data["tasks"][2])
        self.assertEqual(data["status_url"], f"{V1}?tasks={ids}")

    def test_batch_accepts_repeated_parameters(self):
        response = self.client_for(self.owner).get(f"{V1}?tasks={self.done.pk}&tasks={self.failed.pk}")
        self.assertEqual(response.json()["total"], 2)

    def test_batch_limit(self):
        ids = ",".join(str(i) for i in range(1, 502))
        response = self.client_for(self.owner).get(V1, {"tasks": ids})
        self.assertEqual(response.status_code, 400)

    def test_legacy_api_serves_task_status_too(self):
        response = self.client_for(self.owner).get(reverse("viewer:api"), {"task": self.done.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status_url"], f"/api/?task={self.done.pk}")


class AsyncResponsesIncludeTaskReferenceTests(ApiV1TestBase):
    gallery: Gallery
    archive: Archive
    archive2: Archive

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        for codename in ("delete_archive", "update_metadata"):
            cls.owner.user_permissions.add(Permission.objects.get(codename=codename))
        cls.gallery = Gallery.objects.create(gid="7001", provider="panda", title="G")
        cls.archive = Archive.objects.create(title="A", user=cls.owner, gallery=cls.gallery)
        cls.archive2 = Archive.objects.create(title="A2", user=cls.owner, gallery=cls.gallery)

    def test_delete_archive_returns_task_id_and_status_url(self):
        response = self.client_for(self.owner).delete(f"{V1}?archive={self.archive.pk}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        task = TaskQueue.objects.get(pk=data["task_id"])
        self.assertEqual(task.user, self.owner)
        self.assertEqual(data["status_url"], f"{V1}?task={task.pk}")
        # The returned URL works for the caller.
        self.assertEqual(self.client_for(self.owner).get(data["status_url"]).status_code, 200)

    def test_delete_gallery_returns_task_ids_and_batch_status_url(self):
        response = self.client_for(self.owner).delete(f"{V1}?gallery={self.gallery.pk}")
        data = response.json()
        self.assertEqual(len(data["task_ids"]), 2)
        self.assertEqual(data["status_url"], f"{V1}?tasks={data['task_ids'][0]},{data['task_ids'][1]}")

    def test_update_metadata_on_legacy_api_points_to_legacy_status_url(self):
        response = self.client_for(self.owner).post(
            f"/api/?update-metadata=&gallery={self.gallery.pk}", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status_url"], f"/api/?task={data['task_id']}")


class ErrorEnvelopeAndAuthTests(ApiV1TestBase):
    private_archive: Archive

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.private_archive = Archive.objects.create(title="Private", public=False, user=cls.owner)

    def test_v1_errors_only_have_the_envelope(self):
        response = Client().get(V1, {"archive": 999999})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": {"code": "not_found", "message": "Archive does not exist."}})

    def test_legacy_errors_keep_result(self):
        response = Client().get("/api/", {"archive": 999999})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {"error": {"code": "not_found", "message": "Archive does not exist."}, "result": "Archive does not exist."},
        )

    def test_unknown_command_and_method(self):
        response = Client().get(V1, {"nope": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unknown_command")
        response = Client().patch(V1)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")
        self.assertIn("GET", response["Allow"])

    def test_invalid_json_and_non_object_body(self):
        client = self.client_for(self.staff)
        response = client.post(f"{V1}?wanted-gallery=", data="{nope", content_type="application/json")
        self.assertEqual(response.json()["error"]["code"], "invalid_json")
        response = client.post(f"{V1}?wanted-gallery=", data="[1]", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_missing_body_field_is_400(self):
        self.staff.user_permissions.add(Permission.objects.get(codename="change_archivegroup"))
        response = self.client_for(self.staff).post(f"{V1}?archive-group=", data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "missing_field")

    def test_permission_denied_is_403(self):
        response = self.client_for(self.other).post(
            f"{V1}?wanted-gallery=", data="{}", content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_invalid_token_is_an_error_on_v1_but_anonymous_on_legacy(self):
        response = Client().get(V1, {"archive": self.private_archive.pk}, **bearer("not-a-token"))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_token")

        response = Client().get("/api/", {"archive": self.private_archive.pk}, **bearer("not-a-token"))
        self.assertEqual(response.status_code, 404)

    def test_token_without_scope_is_403_on_v1(self):
        _, secret = UserLongLivedToken.create_for_user(self.owner, "read-only", scopes=["read"])
        response = Client().post(f"{V1}?wanted-gallery=", data="{}", content_type="application/json", **bearer(secret))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "insufficient_scope")

    def test_token_of_inactive_user_is_rejected(self):
        _, secret = UserLongLivedToken.create_for_user(self.owner, "reader", scopes=["read"])
        self.owner.is_active = False
        self.owner.save()
        response = Client().get(V1, {"me": ""}, **bearer(secret))
        self.assertEqual(response.status_code, 401)

    def test_unexpected_exception_is_a_500_envelope(self):
        with (
            self.settings(DEBUG=False),
            patch("viewer.views.api.routes.dispatch_command", side_effect=RuntimeError("bug")),
            self.assertLogs("django.request"),
        ):
            response = self.client_for(self.owner).get(V1, {"me": ""})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "internal_error")

    def test_v1_index(self):
        response = Client().get(V1)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["openapi"].endswith("/api/v1/openapi.json"))
        self.assertFalse(data["authenticated"])


class ReaderCommandTests(ApiV1TestBase):
    public_archive: Archive
    private_archive: Archive

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.owner.user_permissions.add(Permission.objects.get(codename="expand_archive"))
        cls.public_archive = Archive.objects.create(title="Pub", public=True, extracted=True, user=cls.owner)
        cls.private_archive = Archive.objects.create(title="Priv", public=False, extracted=False, user=cls.owner)
        for position in (1, 2):
            Image.objects.create(archive=cls.public_archive, position=position, extracted=True, sha1=f"{position}" * 40)

    def test_archive_reader_public_and_private(self):
        with patch.object(Image, "dump_image", return_value={"position": 2}):
            response = Client().get(V1, {"archive-reader": self.public_archive.pk, "position": 2})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["startImage"], {"position": 2})
        response = Client().get(V1, {"archive-reader": self.private_archive.pk})
        self.assertEqual(response.status_code, 404)
        response = self.client_for(self.owner).get(V1, {"archive-reader": self.private_archive.pk})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["startImage"])

    def test_archive_images_keep_requested_order(self):
        with patch("django.db.models.fields.files.FieldFile.url", "/media/img.jpg"):
            response = Client().get(f"{V1}?archive-images={self.public_archive.pk}&i=2&i=1")
        self.assertEqual([image["position"] for image in response.json()], [2, 1])

    def test_archives_simple_is_not_taken_by_the_q_command(self):
        response = Client().get(V1, {"archives-simple": "", "q": "Pub"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"id": self.public_archive.pk, "title": "Pub"}])

    def test_me(self):
        self.assertEqual(Client().get(V1, {"me": ""}).status_code, 401)
        data = self.client_for(self.owner).get(V1, {"me": ""}).json()
        self.assertEqual(data["username"], "v1_owner")

    @patch.object(Archive, "extract")
    def test_extract_archive_needs_permission(self, mock_extract):
        response = self.client_for(self.other).post(f"{V1}?extract-archive={self.private_archive.pk}")
        self.assertEqual(response.status_code, 403)
        response = self.client_for(self.owner).post(f"{V1}?extract-archive={self.private_archive.pk}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["result"], "queued")
        self.assertEqual(body["status_url"], f"{V1}?task={body['task_id']}")
        mock_extract.assert_not_called()
        task = TaskQueue.objects.get(pk=body["task_id"])
        self.assertEqual(task.task_type, "extract_worker")
        self.assertEqual(task.args, [self.private_archive.pk])

    def test_extract_unknown_archive_is_404(self):
        response = self.client_for(self.owner).post(f"{V1}?extract-archive=999999")
        self.assertEqual(response.status_code, 404)


class LegacyRoutesTests(ApiV1TestBase):
    archive: Archive

    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.archive = Archive.objects.create(title="Pub", public=True, extracted=False, user=cls.owner)

    def test_r_api_is_deprecated_with_successor_link(self):
        response = Client().get(f"/r-api/archive/{self.archive.pk}/")
        self.assertEqual(response.status_code, 404)  # not extracted: hidden from anonymous readers
        self.assertEqual(response["Deprecation"], "true")
        self.assertEqual(response["Link"], f'</api/v1/?archive-reader={self.archive.pk}>; rel="successor-version"')
        self.assertEqual(response.json()["result"], "error")
        self.assertEqual(response.json()["message"], "Archive does not exist.")

        response = self.client_for(self.owner).get(f"/r-api/archive/{self.archive.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], self.archive.pk)

    def test_r_api_extract_without_permission_is_403(self):
        response = self.client_for(self.other).post(f"/r-api/archive/{self.archive.pk}/extract_archive/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["message"], "You don't have permission for this action.")

    def test_r_api_me_and_unknown_model(self):
        self.assertEqual(Client().get("/r-api/me/").json(), {"id": None, "username": "", "is_staff": False})
        self.assertEqual(self.client_for(self.owner).get("/r-api/me/").json()["username"], "v1_owner")
        response = Client().get("/r-api/unknown/")
        self.assertEqual(response.status_code, 404)

    def test_r_api_archives_list(self):
        response = self.client_for(self.owner).get("/r-api/archives/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("objects", response.json())
        self.assertEqual(response["Link"], '</api/v1/?archives-browse>; rel="successor-version"')

    def test_api_and_jsearch_are_not_deprecated(self):
        for path in ("/api/", "/jsearch/"):
            response = Client().get(path, {"archive": self.archive.pk})
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.has_header("Deprecation"))


class JsearchCompatibilityTests(TestCase):
    """/jsearch/ is used by public clients (sha1 and gsp lookups): its responses must not change."""

    gallery: Gallery
    archive: Archive

    @classmethod
    def setUpTestData(cls) -> None:
        owner = User.objects.create_user(username="jsearch_owner")
        cls.gallery = Gallery.objects.create(gid="555", provider="panda", title="Jsearch Gallery", public=True)
        cls.archive = Archive.objects.create(title="Jsearch Archive", public=True, user=owner, gallery=cls.gallery)
        Image.objects.create(archive=cls.archive, position=1, sha1="a" * 40)

    def test_sha1_lookup(self):
        response = Client().get("/jsearch/", {"sha1": "a" * 40})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        data = response.json()
        self.assertIsInstance(data, list)
        self.assertEqual(
            set(data[0]),
            {
                "id", "title", "title_jpn", "category", "uploader", "posted", "filecount", "filesize", "expunged",
                "disowned", "rating", "fjord", "tags", "download", "gallery",
            },
        )
        self.assertEqual(data[0]["title"], "Jsearch Archive")
        self.assertEqual(data[0]["gallery"], self.gallery.pk)
        self.assertEqual(Client().get("/jsearch/", {"sha1": "b" * 40}).json(), [])
        # Same body on the versioned API.
        self.assertEqual(Client().get(V1, {"sha1": "a" * 40}).content, response.content)

    def test_gsp_search(self):
        response = Client().get("/jsearch/", {"gsp": "", "title": "Jsearch", "tags": ""})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertNotIn("error", data)
        self.assertEqual([g["gid"] for g in data["galleries"]], ["555"])
        self.assertEqual(Client().get(V1, {"gsp": "", "title": "Jsearch", "tags": ""}).content, response.content)


class RemoteApiTests(ApiV1TestBase):
    @classmethod
    def setUpTestData(cls) -> None:
        super().setUpTestData()
        cls.owner.user_permissions.add(Permission.objects.get(codename="use_remote_api"))

    def post(self, client: Client, path: str, body: object, **extra):
        return client.post(path, data=json.dumps(body), content_type="application/json", **extra)

    def test_v1_remote_auth_errors(self):
        response = self.post(Client(), "/api/v1/remote/", {"operation": "webcrawler", "args": []})
        self.assertEqual(response.status_code, 401)
        response = self.post(self.client_for(self.other), "/api/v1/remote/", {"operation": "webcrawler", "args": []})
        self.assertEqual(response.status_code, 403)
        _, read_only = UserLongLivedToken.create_for_user(self.owner, "read-only", scopes=["read"])
        response = self.post(Client(), "/api/v1/remote/", {"operation": "webcrawler", "args": []}, **bearer(read_only))
        self.assertEqual(response.json()["error"]["code"], "insufficient_scope")

    def test_v1_remote_queues_with_task_reference(self):
        _, remote = UserLongLivedToken.create_for_user(self.owner, "remote", scopes=["remote"])
        response = self.post(
            Client(),
            "/api/v1/remote/",
            {"operation": "webcrawler", "args": ["https://example.com/g/1/"]},
            **bearer(remote),
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["result"], "1")
        self.assertEqual(data["status_url"], f"{V1}?task={data['task_id']}")
        self.assertTrue(TaskQueue.objects.filter(pk=data["task_id"], user=self.owner).exists())

        response = self.post(Client(), "/api/v1/remote/", {"operation": "webcrawler", "args": []}, **bearer(remote))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "bad_request")

    def test_v1_remote_webcrawler_rejects_crawler_options_as_links(self):
        client = self.client_for(self.owner)
        for links in (["-f", "/etc/passwd"], ["https://example.com/g/1/", "--ignore-global-filters"], "-f\n/etc/hosts"):
            response = self.post(client, "/api/v1/remote/", {"operation": "webcrawler", "args": {"links": links}})
            self.assertEqual(response.status_code, 400)
            self.assertIn("can't start with '-'", response.json()["error"]["message"])
        self.assertFalse(TaskQueue.objects.filter(task_type="web_crawler").exists())

    def test_v1_remote_errors(self):
        client = self.client_for(self.owner)
        for operation in ("nope", "archive_request", "force_queue_archives", "queue_archives", "queue_galleries", "links"):
            response = self.post(client, "/api/v1/remote/", {"operation": operation, "args": []})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "unknown_command")
        response = client.post("/api/v1/remote/", data="", content_type="application/json")
        self.assertEqual(response.json()["error"]["code"], "empty_body")
        response = self.post(client, "/api/v1/remote/", {"operation": "match_archive", "args": {"archive": 999999}})
        self.assertEqual(response.status_code, 404)

    def test_admin_api_is_a_deprecated_alias(self):
        response = self.post(self.client_for(self.owner), "/admin-api/", {"operation": "webcrawler", "args": {}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["message"], "No links provided")
        self.assertEqual(response["Deprecation"], "true")
        self.assertIn("/api/v1/remote/", response["Link"])


class OpenApiTests(TestCase):
    def test_document_lists_every_command(self):
        document = build_openapi_document()
        self.assertEqual(document["openapi"], "3.1.0")
        for method, commands in COMMANDS.items():
            for command in commands:
                operation = document["paths"][f"{V1}?{command.key}"][method.lower()]
                self.assertEqual(operation["parameters"][0]["name"], command.key)
                if command.perm:
                    self.assertIn("403", operation["responses"])
        self.assertIn("post", document["paths"]["/api/v1/remote/"])
        operation_ids = [op["operationId"] for path in document["paths"].values() for op in path.values()]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_committed_document_is_up_to_date(self):
        # docs/api/openapi.json is what GitLab renders; regenerate it with `manage.py openapi_schema --update`.
        call_command("openapi_schema", "--check", stdout=io.StringIO())

    def test_export_command_writes_the_served_document_with_other_servers(self):
        out = io.StringIO()
        call_command("openapi_schema", "--server", "https://example.com", stdout=out)
        exported = json.loads(out.getvalue())
        self.assertEqual(exported["servers"], [{"url": "https://example.com"}])
        served = Client().get("/api/v1/openapi.json").json()
        self.assertEqual(served["servers"], [{"url": "/", "description": "This server"}])
        self.assertEqual({**exported, "servers": served["servers"]}, served)

    def test_document_and_docs_page_are_served(self):
        response = Client().get("/api/v1/openapi.json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["info"]["title"], "Panda Backup API")
        response = Client().get("/api/v1/docs/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-openapi-url="/api/v1/openapi.json"')


class ApiTagFilterAndTimeoutTests(TestCase):
    """Verify API tag search avoids join explosion, honors tags_or, enforces tag limits, and handles DB timeouts."""

    tags: list[Tag]
    g_first: Gallery
    g_second: Gallery
    g_both: Gallery
    archive_only_title: Archive
    archive_tagged: Archive
    wanted_tagged: WantedGallery

    @classmethod
    def setUpTestData(cls) -> None:
        owner = User.objects.create_user(username="tag_filter_owner")
        cls.tags = [Tag.objects.create(scope="artist", name=f"a{i}") for i in range(60)]
        cls.g_first = Gallery.objects.create(gid="t1", provider="panda", title="First", public=True)
        cls.g_first.tags.add(cls.tags[0])
        cls.g_second = Gallery.objects.create(gid="t2", provider="panda", title="Second", public=True)
        cls.g_second.tags.add(cls.tags[49])
        cls.g_both = Gallery.objects.create(gid="t3", provider="panda", title="Both", public=True)
        cls.g_both.tags.add(cls.tags[0], cls.tags[49])

        cls.archive_only_title = Archive.objects.create(
            title="UniqueTitleNoTagMatch", public=True, extracted=True, user=owner
        )
        cls.archive_tagged = Archive.objects.create(
            title="TaggedArchive", public=True, extracted=True, user=owner, gallery=cls.g_both
        )
        cls.archive_tagged.tags.add(cls.tags[0], cls.tags[49])

        cls.wanted_tagged = WantedGallery.objects.create(title="WantedTagged", public=True)
        cls.wanted_tagged.wanted_tags.add(cls.tags[0])

    def test_gsp_many_exact_tags_or_batches_lookups_and_uses_single_exists(self):
        tags_param = ",".join(f"^artist:a{i}" for i in range(50))
        with CaptureQueriesContext(connection) as ctx:
            response = Client().get(
                V1,
                {"gsp": "", "tags_or": "1", "sort": "posted", "asc_desc": "desc", "count": "48", "page": "1", "tags": tags_param},
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 3)
        self.assertEqual({g["gid"] for g in data["galleries"]}, {"t1", "t2", "t3"})

        sql_statements = [q["sql"] for q in ctx.captured_queries]
        tag_lookup_queries = [s for s in sql_statements if 'FROM "viewer_tag"' in s and "gallery_id" not in s]
        self.assertEqual(len(tag_lookup_queries), 1, "50 exact scoped tags should resolve in a single query")
        for sql in sql_statements:
            self.assertNotIn('INNER JOIN "viewer_tag"', sql)
            self.assertNotIn("SELECT DISTINCT", sql)
        gallery_count_queries = [s for s in sql_statements if 'COUNT(*)' in s and '"viewer_gallery"' in s]
        self.assertEqual(len(gallery_count_queries), 1)
        self.assertEqual(gallery_count_queries[0].count("EXISTS("), 1)

    def test_gsp_tags_or_false_and_zero_require_all_tags(self):
        tags_param = "^artist:a0,^artist:a49"
        for tags_or_val in ("", "0", "false"):
            response = Client().get(V1, {"gsp": "", "tags_or": tags_or_val, "tags": tags_param})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([g["gid"] for g in response.json()["galleries"]], ["t3"])

        for tags_or_val in ("1", "true"):
            response = Client().get(V1, {"gsp": "", "tags_or": tags_or_val, "tags": tags_param})
            self.assertEqual(response.status_code, 200)
            self.assertEqual({g["gid"] for g in response.json()["galleries"]}, {"t1", "t2", "t3"})

    def test_api_endpoints_reject_requests_exceeding_max_tags(self):
        too_many_csv = ",".join(f"^artist:a{i}" for i in range(MAX_API_TAG_FILTERS + 52))
        too_many_spaces = " ".join(f"^artist:a{i}" for i in range(MAX_API_TAG_FILTERS + 52))

        for params in (
            {"gsp": "", "tags_or": "1", "tags": too_many_csv},
            {"gs": "", "tags": too_many_csv},
            {"g": "", "tags": too_many_csv},
            {"q": too_many_csv},
            {"qa": "", "tags": too_many_csv},
            {"as": "", "tags": too_many_csv},
            {"wanted-galleries-search": "", "tags": too_many_csv},
            {"archives-quick": "", "q": too_many_spaces},
            {"archives-browse": "", "tags": too_many_csv},
            {"archives-simple": too_many_csv},
        ):
            response = Client().get(V1, params)
            self.assertEqual(response.status_code, 400, f"Expected 400 for {params.keys()}")
            self.assertEqual(response.json()["error"]["code"], "too_many_tags")

        # Legacy /jsearch/ surface keeps both envelope and result key
        legacy = Client().get("/jsearch/", {"gsp": "", "tags_or": "1", "tags": too_many_csv})
        self.assertEqual(legacy.status_code, 400)
        body = legacy.json()
        self.assertEqual(body["error"]["code"], "too_many_tags")
        self.assertIn("Too many tags", body["result"])

    def test_q_archives_quick_and_wanted_search_work_without_viewer_tag_joins(self):
        # Title match when tag lookup returns 0 matches
        resp_title = Client().get(V1, {"q": "UniqueTitleNoTagMatch"})
        self.assertEqual(resp_title.status_code, 200)
        self.assertEqual([a["id"] for a in resp_title.json()], [self.archive_only_title.pk])

        # Multi-tag q search
        with CaptureQueriesContext(connection) as ctx:
            resp_tags = Client().get(V1, {"q": "^artist:a0,^artist:a49"})
        self.assertEqual(resp_tags.status_code, 200)
        self.assertEqual([a["id"] for a in resp_tags.json()], [self.archive_tagged.pk])
        for q in ctx.captured_queries:
            self.assertNotIn('INNER JOIN "viewer_tag"', q["sql"])
            self.assertNotIn("SELECT DISTINCT", q["sql"])

        # archives-quick space-separated tags
        with CaptureQueriesContext(connection) as ctx:
            resp_quick = Client().get(V1, {"archives-quick": "", "q": "^artist:a0 ^artist:a49"})
        self.assertEqual(resp_quick.status_code, 200)
        self.assertEqual([a["id"] for a in resp_quick.json()["objects"]], [self.archive_tagged.pk])
        for q in ctx.captured_queries:
            self.assertNotIn('INNER JOIN "viewer_tag"', q["sql"])

        # wanted-galleries-search
        with CaptureQueriesContext(connection) as ctx:
            resp_wanted = Client().get(V1, {"wanted-galleries-search": "", "tags": "^artist:a0"})
        self.assertEqual(resp_wanted.status_code, 200)
        self.assertEqual([w["id"] for w in resp_wanted.json()], [self.wanted_tagged.pk])
        for q in ctx.captured_queries:
            self.assertNotIn('JOIN "viewer_tag"', q["sql"])

    def test_postgres_statement_timeout_configured_and_api_returns_504(self):
        from core.base.setup import Settings
        from pandabackup.settings import get_database_config

        web_settings = Settings(load_from_disk=False)
        web_settings.is_worker_process = False
        web_options = get_database_config(web_settings)["default"]["OPTIONS"]
        self.assertEqual(web_options, {"options": "-c statement_timeout=30000"})

        conn_params = connection.get_connection_params()
        conn_params["options"] = web_options["options"]
        raw_conn = connection.get_new_connection(conn_params)
        try:
            with raw_conn.cursor() as cursor:
                cursor.execute("SHOW statement_timeout")
                timeout_val = cursor.fetchone()[0]
        finally:
            raw_conn.close()
        self.assertEqual(timeout_val, "30s")

        def slow_dispatch(_request):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout = '50ms'")
                    cursor.execute("SELECT pg_sleep(0.25)")
            raise AssertionError("Expected PostgreSQL statement_timeout to cancel pg_sleep")

        with patch("viewer.views.api.routes.dispatch_command", side_effect=slow_dispatch):
            response = Client().get(V1, {"gsp": ""})
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"]["code"], "query_timeout")
