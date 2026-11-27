"""Tests for moving remaining heavy operations from web views and APIs onto workers.TaskQueue."""

import json
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse

from core.base.setup import Settings
from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    ArchiveMatches,
    Category,
    EventLog,
    FoundGallery,
    Gallery,
    GalleryGroupPossibleMatches,
    GalleryMatch,
    GalleryMatchGroup,
    GalleryMatchGroupEntry,
    Profile,
    Provider,
    Tag,
    UserArchivePrefs,
    WantedGallery,
)
from workers.models import TaskQueue
from workers.runner import WorkerRunner

User = get_user_model()


def _run_task(task: TaskQueue) -> TaskQueue:
    task.status = TaskQueue.Status.PROCESSING
    task.save(update_fields=["status"])
    runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
    runner.execute_job(task)
    task.refresh_from_db()
    return task


class ReaderAndRemoteApiAsyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="api_async_user", password="password123")
        cls.user.user_permissions.add(
            Permission.objects.get(codename="expand_archive"),
            Permission.objects.get(codename="use_remote_api"),
        )

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.user)
        self.gallery = Gallery.objects.create(
            gid="1001", token="abcdef1234", provider="panda", title="Parent Gallery"
        )
        self.archive = Archive.objects.create(
            title="Parent Archive",
            gallery=self.gallery,
            extracted=False,
            user=self.user,
            match_type="non-match",
            zipped="parent.zip",
        )

    @patch.object(Archive, "extract")
    def test_extract_archive_queues_and_deduplicates(self, mock_extract):
        resp1 = self.client.post(f"/api/v1/?extract-archive={self.archive.pk}&resized=1")
        self.assertEqual(resp1.status_code, 200)
        body1 = resp1.json()
        self.assertEqual(body1["result"], "queued")
        self.assertIn("resized", body1["message"])
        self.assertEqual(body1["status_url"], f"/api/v1/?task={body1['task_id']}")
        mock_extract.assert_not_called()

        task = TaskQueue.objects.get(pk=body1["task_id"])
        self.assertEqual(task.task_type, "extract_worker")
        self.assertEqual(task.args, [self.archive.pk])
        self.assertTrue(task.options["resized"])

        # Second call while active returns the same task without queueing a duplicate.
        resp2 = self.client.post(f"/api/v1/?extract-archive={self.archive.pk}")
        self.assertEqual(resp2.status_code, 200)
        body2 = resp2.json()
        self.assertEqual(body2["result"], "queued")
        self.assertEqual(body2["task_id"], body1["task_id"])
        self.assertEqual(TaskQueue.objects.filter(task_type="extract_worker").count(), 1)

    @patch.object(Archive, "reduce")
    def test_reduce_archive_queues_and_warns_when_already_reduced(self, mock_reduce):
        resp_warn = self.client.post(f"/api/v1/?reduce-archive={self.archive.pk}")
        self.assertEqual(resp_warn.status_code, 200)
        warn_body = resp_warn.json()
        self.assertEqual(warn_body["result"], "warning")
        self.assertIsNone(warn_body["task_id"])
        self.assertEqual(warn_body["change"], {"field": "extracted", "value": False})

        self.archive.extracted = True
        self.archive.save()

        resp = self.client.post(f"/api/v1/?reduce-archive={self.archive.pk}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["result"], "queued")
        self.assertEqual(body["status_url"], f"/api/v1/?task={body['task_id']}")
        mock_reduce.assert_not_called()
        task = TaskQueue.objects.get(pk=body["task_id"])
        self.assertEqual(task.task_type, "reduce_worker")
        self.assertEqual(task.args, [self.archive.pk])

    def test_remote_webcrawler_replace_found_deletes_parent_in_worker(self):
        self.archive.extracted = True
        self.archive.save()
        UserArchivePrefs.objects.create(user=self.user, archive=self.archive, favorite_group=2)

        parent_link = self.gallery.get_link()
        new_link = "https://e-hentai.org/g/2002/0123456789/"

        with patch.object(Archive, "delete_all_files") as mock_del_files:
            resp = self.client.post(
                "/api/v1/remote/",
                data=json.dumps(
                    {
                        "operation": "webcrawler",
                        "args": {
                            "link": new_link,
                            "parentLink": parent_link,
                            "action": "replaceFound",
                            "reason": "upgrade",
                        },
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertIsNotNone(body["task_id"])
            # Parent archive and files must NOT be deleted synchronously inside the HTTP request.
            mock_del_files.assert_not_called()
            self.assertTrue(Archive.objects.filter(pk=self.archive.pk).exists())
            self.gallery.refresh_from_db()
            self.assertFalse(self.gallery.is_deleted())

        task = TaskQueue.objects.get(pk=body["task_id"])
        self.assertEqual(task.task_type, "web_crawler")
        self.assertEqual(task.options["replace_parent_archive_id"], self.archive.pk)
        self.assertTrue(task.options["preserve_extracted"])
        self.assertEqual(
            task.options["preserve_user_favorites"],
            [{"user": self.user.pk, "favorite_group": 2}],
        )

        # Executing the web_crawler task deletes the parent archive before starting the crawl.
        call_order: list[str] = []

        def fake_crawl(crawl_self, args):
            call_order.append("crawl")
            self.assertFalse(Archive.objects.filter(pk=self.archive.pk).exists())

        def fake_del_files(arch_self, *args, **kwargs):
            call_order.append("delete_files")

        with (
            patch.object(Archive, "delete_all_files", autospec=True, side_effect=fake_del_files),
            patch.object(Archive, "delete_files_but_archive"),
            patch("workers.runner.WebCrawler.start_crawling", autospec=True, side_effect=fake_crawl),
        ):
            _run_task(task)

        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertEqual(call_order, ["delete_files", "crawl"])
        self.assertFalse(Archive.objects.filter(pk=self.archive.pk).exists())
        self.gallery.refresh_from_db()
        self.assertTrue(self.gallery.is_deleted())

    def test_remote_match_archive_and_internally_enqueue_tasks(self):
        with patch("viewer.utils.matching.generate_possible_matches_for_archives") as mock_web:
            resp = self.client.post(
                "/api/v1/remote/",
                data=json.dumps(
                    {
                        "operation": "match_archive",
                        "args": {"archive": self.archive.pk, "match_filter": "panda", "auto_link": True},
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            mock_web.assert_not_called()
            task = TaskQueue.objects.get(pk=body["task_id"])
            self.assertEqual(task.task_type, "web_match_worker")
            self.assertEqual(task.args, [self.archive.pk])
            self.assertEqual(task.options["matching_kwargs"]["filters"], ["panda"])
            self.assertTrue(task.options["matching_kwargs"]["auto_link"])

        with patch.object(Archive, "generate_possible_matches") as mock_internal:
            resp2 = self.client.post(
                "/api/v1/remote/?cutoff=0.6&max-matches=5",
                data=json.dumps(
                    {
                        "operation": "match_archive_internally",
                        "args": {"archive": self.archive.pk, "provider": "panda", "clear": "1"},
                    }
                ),
                content_type="application/json",
            )
            self.assertEqual(resp2.status_code, 200)
            body2 = resp2.json()
            mock_internal.assert_not_called()
            task2 = TaskQueue.objects.get(pk=body2["task_id"])
            self.assertEqual(task2.task_type, "match_unmatched_worker")
            self.assertEqual(task2.args, [self.archive.pk])
            self.assertEqual(task2.options["cutoff"], 0.6)
            self.assertEqual(task2.options["max_matches"], 5)
            self.assertEqual(task2.options["provider_filter"], "panda")
            self.assertTrue(task2.options["clear_title"])


class CollaboratorBatchOperationsAsyncTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("batch_admin", "admin@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.admin)

    def test_manage_archives_bulk_actions_queue_tasks_and_execute(self):
        g1 = Gallery.objects.create(gid="g1", token="t1", provider="panda", title="G1")
        g2 = Gallery.objects.create(gid="g2", token="t2", provider="panda", title="G2")
        a1 = Archive.objects.create(title="A1", gallery=g1, extracted=True, public=True, crc32="11111111", user=self.admin)
        a2 = Archive.objects.create(title="A2", gallery=g2, extracted=False, public=False, crc32="22222222", user=self.admin)
        group = ArchiveGroup.objects.create(title="My Group", title_slug="my-group")
        url = reverse("viewer:archive-manager") + "?json=1"

        # 1. unpublish_archives
        resp = self.client.post(url, {"unpublish_archives": "1", f"sel-{a1.pk}": str(a1.pk), "reason": "hide"})
        body = resp.json()
        self.assertEqual(len(body["task_ids"]), 1)
        task = _run_task(TaskQueue.objects.get(pk=body["task_ids"][0]))
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        a1.refresh_from_db()
        self.assertFalse(a1.public)

        # 2. add_to_group
        resp = self.client.post(
            url, {"add_to_group": "1", f"sel-{a1.pk}": str(a1.pk), "archive_group": str(group.pk)}
        )
        task = _run_task(TaskQueue.objects.get(pk=resp.json()["task_ids"][0]))
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertTrue(ArchiveGroupEntry.objects.filter(archive=a1, archive_group=group).exists())

        # 3. release_gallery
        resp = self.client.post(url, {"release_gallery": "1", f"sel-{a1.pk}": str(a1.pk)})
        task = _run_task(TaskQueue.objects.get(pk=resp.json()["task_ids"][0]))
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        a1.refresh_from_db()
        self.assertIsNone(a1.gallery)

        # 4. gallery_to_alternative
        resp = self.client.post(url, {"gallery_to_alternative": "1", f"sel-{a2.pk}": str(a2.pk)})
        task = _run_task(TaskQueue.objects.get(pk=resp.json()["task_ids"][0]))
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        a2.refresh_from_db()
        self.assertIsNone(a2.gallery)
        self.assertTrue(a2.alternative_sources.filter(pk=g2.pk).exists())

        # 5. reduce_archives (only extracted archive a1)
        with patch.object(Archive, "reduce") as mock_reduce:
            resp = self.client.post(
                url, {"reduce_archives": "1", f"sel-{a1.pk}": str(a1.pk), f"sel-{a2.pk}": str(a2.pk)}
            )
            mock_reduce.assert_not_called()
            task = TaskQueue.objects.get(pk=resp.json()["task_ids"][0])
            self.assertEqual(task.task_type, "reduce_worker")
            self.assertEqual(task.args, [a1.pk])
            _run_task(task)
            mock_reduce.assert_called_once()

        # 6. recalc_fileinfo
        with patch.object(Archive, "recalc_fileinfo") as mock_recalc:
            resp = self.client.post(url, {"recalc_fileinfo": "1", f"sel-{a1.pk}": str(a1.pk)})
            mock_recalc.assert_not_called()
            task = TaskQueue.objects.get(pk=resp.json()["task_ids"][0])
            self.assertEqual(task.task_type, "fileinfo_worker")
            _run_task(task)
            mock_recalc.assert_called_once()
            self.assertTrue(EventLog.objects.filter(action="RECALC_ARCHIVE").exists())

        # 7. mark_similar
        with patch.object(Archive, "create_marks_for_similar_archives") as mock_similar:
            resp = self.client.post(url, {"mark_similar": "1", f"sel-{a1.pk}": str(a1.pk)})
            mock_similar.assert_not_called()
            task = TaskQueue.objects.get(pk=resp.json()["task_ids"][0])
            self.assertEqual(task.task_type, "mark_similar_worker")
            _run_task(task)
            mock_similar.assert_called_once()

        # 8. delete_archives (batch multi-PK delete_archive_worker)
        with patch.object(Archive, "delete_all_files"), patch.object(Archive, "delete_files_but_archive"):
            resp = self.client.post(
                url, {"delete_archives": "1", f"sel-{a1.pk}": str(a1.pk), f"sel-{a2.pk}": str(a2.pk)}
            )
            self.assertTrue(Archive.objects.filter(pk__in=[a1.pk, a2.pk]).count() == 2)
            task = TaskQueue.objects.get(pk=resp.json()["task_ids"][0])
            self.assertEqual(task.task_type, "delete_archive_worker")
            self.assertEqual(task.args, [a1.pk, a2.pk])
            _run_task(task)
            self.assertFalse(Archive.objects.filter(pk__in=[a1.pk, a2.pk]).exists())

    def test_unmatched_archives_auto_select_first_match_queues_task(self):
        gallery = Gallery.objects.create(gid="m1", token="t1", provider="panda", title="Match Gallery")
        archive = Archive.objects.create(title="Unmatched", user=self.admin)
        ArchiveMatches.objects.create(archive=archive, gallery=gallery, match_type="title", match_accuracy=0.9)

        resp = self.client.post(
            reverse("viewer:match-archives"),
            {"auto_select_first_match": "1", f"sel-{archive.pk}": str(archive.pk)},
        )
        self.assertEqual(resp.status_code, 302)
        archive.refresh_from_db()
        self.assertIsNone(archive.gallery)

        task = TaskQueue.objects.filter(task_type="archive_batch_worker").latest("pk")
        self.assertEqual(task.options["operation"], "auto_select_first_match")
        _run_task(task)
        archive.refresh_from_db()
        self.assertEqual(archive.gallery_id, gallery.pk)

    def test_manage_galleries_batch_actions_queue_tasks(self):
        g1 = Gallery.objects.create(gid="mg1", token="t1", provider="panda", title="MG1", public=True)
        g2 = Gallery.objects.create(gid="mg2", token="t2", provider="panda", title="MG2", public=False)
        g3 = Gallery.objects.create(gid="mg3", token="t3", provider="panda", title="MG3", public=False)
        group = GalleryMatchGroup.objects.create(title="Group")
        GalleryMatchGroupEntry.objects.create(gallery_match_group=group, gallery=g2, gallery_position=1)
        GalleryGroupPossibleMatches.objects.create(
            gallery_match_group=group, gallery=g3, match_type="title", match_accuracy=0.9
        )

        url = reverse("viewer:manage-galleries")

        # private_galleries
        self.client.post(url, {"private_galleries": "1", f"sel-{g1.pk}": str(g1.pk)})
        g1.refresh_from_db()
        self.assertTrue(g1.public)
        _run_task(TaskQueue.objects.filter(task_type="gallery_batch_worker").latest("pk"))
        g1.refresh_from_db()
        self.assertFalse(g1.public)

        # auto_select_first_match
        self.client.post(url, {"auto_select_first_match": "1", f"sel-{g2.pk}": str(g2.pk)})
        _run_task(TaskQueue.objects.filter(task_type="gallery_batch_worker").latest("pk"))
        self.assertTrue(group.galleries.filter(pk=g3.pk).exists())
        self.assertFalse(GalleryGroupPossibleMatches.objects.filter(gallery_match_group=group).exists())

        # delete_galleries (mark deleted)
        self.client.post(url, {"delete_galleries": "1", f"sel-{g1.pk}": str(g1.pk)})
        _run_task(TaskQueue.objects.filter(task_type="gallery_batch_worker").latest("pk"))
        g1.refresh_from_db()
        self.assertTrue(g1.is_deleted())

        # real_delete_galleries
        self.client.post(url, {"real_delete_galleries": "1", f"sel-{g1.pk}": str(g1.pk)})
        self.assertTrue(Gallery.objects.filter(pk=g1.pk).exists())
        _run_task(TaskQueue.objects.filter(task_type="gallery_batch_worker").latest("pk"))
        self.assertFalse(Gallery.objects.filter(pk=g1.pk).exists())

    def test_gallery_batch_worker_chunks_large_pk_lists_and_deduplicates(self):
        galleries = [
            Gallery.objects.create(gid=f"chunk-{i}", token="t", provider="panda", title=f"C{i}", public=False)
            for i in range(5)
        ]
        # Pass duplicate PKs and force a small chunk size to verify chunking & deduplication.
        pks = [galleries[4].pk, galleries[2].pk, galleries[0].pk, galleries[2].pk, galleries[3].pk, galleries[1].pk]
        task = TaskQueue.objects.create(
            task_type="gallery_batch_worker",
            args=pks,
            options={"operation": "publish"},
            user=self.admin,
        )
        with patch("workers.batch_tasks.BATCH_FETCH_CHUNK_SIZE", 2):
            _run_task(task)
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertEqual(task.result, {"operation": "publish", "processed": 5, "skipped": 0})
        self.assertEqual(Gallery.objects.filter(pk__in=[g.pk for g in galleries], public=True).count(), 5)


class ExternalAndMaintenanceViewsAsyncTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("ext_admin", "ext_admin@example.com", "pw")
        self.client = Client()
        self.client.force_login(self.admin)

    def test_create_wanted_galleries_from_command_enqueues_task(self):
        provider = Provider.objects.create(name="Panda", slug="panda")
        category = Category.objects.create(name="Doujinshi")
        Tag.objects.create(scope="artist", name="test_artist")

        with patch("django.core.management.call_command") as mock_cmd:
            resp = self.client.post(
                reverse("viewer:col-create-wanted-galleries-from-command"),
                {
                    "submit-wanted-artist-gallery": "Submit",
                    "wanted_publisher": "panda",
                    "artist_or_group_tag": ["artist:test_artist"],
                    "wanted_providers": [str(provider.pk)],
                    "categories": [str(category.pk)],
                },
            )
            self.assertEqual(resp.status_code, 200)
            mock_cmd.assert_not_called()

        task = TaskQueue.objects.filter(task_type="update_provider_wanted_worker").latest("pk")
        self.assertEqual(task.options["wanted_publisher"], "panda")
        self.assertEqual(task.options["artist_or_group_tag"], ["artist:test_artist"])
        self.assertEqual(task.options["wanted_providers"], ["panda"])
        self.assertEqual(task.options["categories"], ["Doujinshi"])

        _run_task(task)
        self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
        self.assertTrue(WantedGallery.objects.filter(publisher="panda").exists())
        self.assertTrue(EventLog.objects.filter(action="CREATE_WANTED_GALLERY_FROM_COMMAND").exists())

    def test_url_submit_enqueues_email_worker_instead_of_sending_synchronously(self):
        Profile.objects.update_or_create(user=self.admin, defaults={"notify_new_submissions": True})

        with (
            patch.object(settings.CRAWLER_SETTINGS.urls, "enable_public_submit", True),
            patch("workers.batch_tasks.send_mass_html_mail", return_value=1) as mock_mail,
        ):
            resp = self.client.post(
                reverse("viewer:url-submit"),
                {"submit": "1", "urls": "https://e-hentai.org/g/12345/abcdef1234/", "reason": "please add"},
            )
            self.assertEqual(resp.status_code, 302)
            mock_mail.assert_not_called()

            task = TaskQueue.objects.filter(task_type="send_email_worker").latest("pk")
            self.assertEqual(len(task.options["messages"]), 1)
            self.assertEqual(task.options["messages"][0][4], ["ext_admin@example.com"])

            _run_task(task)
            self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
            self.assertEqual(task.result, {"attempted": 1, "sent": 1})
            mock_mail.assert_called_once()

    def test_wanted_gallery_internal_matching_tools_enqueue_tasks(self):
        wanted = WantedGallery.objects.create(title="Wanted Test", search_title="Special Title", should_search=True)
        gallery = Gallery.objects.create(gid="wg1", token="tok", provider="panda", title="Special Title Book")

        with (
            patch.object(WantedGallery, "create_gallery_matches_internally") as mock_rule,
            patch.object(WantedGallery, "create_found_galleries_internally") as mock_found,
        ):
            resp1 = self.client.post(
                reverse("viewer:wanted-gallery", args=[wanted.pk]),
                {"tool": "create-rule-matches-internally", "provider": "panda"},
            )
            self.assertEqual(resp1.status_code, 302)
            mock_rule.assert_not_called()

            resp2 = self.client.post(
                reverse("viewer:wanted-gallery", args=[wanted.pk]),
                {"tool": "create-matches-internally"},
            )
            self.assertEqual(resp2.status_code, 302)
            mock_found.assert_not_called()

        tasks = list(TaskQueue.objects.filter(task_type="wanted_local_search_worker").order_by("pk"))
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].options["mode"], "rule_matches")
        self.assertEqual(tasks[0].options["provider_filter"], "panda")
        self.assertEqual(tasks[1].options["mode"], "found_matches")

        _run_task(tasks[0])
        self.assertTrue(GalleryMatch.objects.filter(wanted_gallery=wanted, gallery=gallery).exists())

        _run_task(tasks[1])
        self.assertTrue(FoundGallery.objects.filter(wanted_gallery=wanted, gallery=gallery).exists())

    def test_reconcile_downloads_view_and_api_enqueue_and_record_result(self):
        fake_stats = {
            "total_checked": 4,
            "rescued_from_disk": 1,
            "rescued_from_staging": 0,
            "recycled_failed": 1,
            "recycled_orphaned": 0,
            "recycled_false_completed": 0,
            "requeued": 1,
            "kept_in_progress": 1,
        }
        with patch("viewer.utils.downloads.reconcile_downloads", return_value=fake_stats) as mock_reconcile:
            # HTMX request on /tools/reconcile_downloads/
            resp = self.client.post(
                reverse("viewer:tools-id", args=["reconcile_downloads"]),
                HTTP_HX_REQUEST="true",
            )
            self.assertEqual(resp.status_code, 200)
            mock_reconcile.assert_not_called()
            task = TaskQueue.objects.get(task_type="reconcile_downloads_worker")
            self.assertIn(f'data-task-id="{task.pk}"', resp.content.decode())

            # /tools/ reports active reconcile_downloads task
            tools_resp = self.client.get(reverse("viewer:tools"))
            self.assertEqual(tools_resp.context["active_tasks"]["reconcile_downloads"], task)

            # /tools-api/reconcile_downloads/ sees it is already running
            api_dup = self.client.post(reverse("viewer:tools-api", args=["reconcile_downloads"]))
            self.assertIn("already running", api_dup.json()["error"])

            # Execute worker task and verify result persisted
            _run_task(task)
            mock_reconcile.assert_called_once()
            self.assertEqual(task.status, TaskQueue.Status.COMPLETED)
            self.assertEqual(task.result, fake_stats)

            # Now /tools-api/reconcile_downloads/ enqueues a fresh task
            api_resp = self.client.post(reverse("viewer:tools-api", args=["reconcile_downloads"]))
            self.assertEqual(api_resp.status_code, 200)
            self.assertIn("task_id", api_resp.json())

    def test_upload_archive_enqueues_mark_similar_when_enabled(self):
        zip_bytes = b"PK\x05\x06" + b"\x00" * 18
        uploaded = SimpleUploadedFile("test_upload.zip", zip_bytes, content_type="application/zip")

        orig_mark_similar = settings.CRAWLER_SETTINGS.mark_similar_new_archives
        settings.CRAWLER_SETTINGS.mark_similar_new_archives = True
        try:
            with (
                patch.object(Archive, "create_marks_for_similar_archives") as mock_similar,
                patch("viewer.forms.ArchiveCreateForm.is_valid", return_value=True),
                patch("viewer.forms.ArchiveCreateForm.save") as mock_save,
            ):
                created = Archive.objects.create(title="Uploaded Archive", crc32="deadbeef", user=self.admin)
                mock_save.return_value = created

                resp = self.client.post(
                    reverse("viewer:upload-archive"),
                    {"submit-archive": "1", "zipped": uploaded},
                )
                self.assertEqual(resp.status_code, 200)
                mock_similar.assert_not_called()
        finally:
            settings.CRAWLER_SETTINGS.mark_similar_new_archives = orig_mark_similar

        task = TaskQueue.objects.get(task_type="mark_similar_worker")
        self.assertEqual(task.args, [created.pk])
