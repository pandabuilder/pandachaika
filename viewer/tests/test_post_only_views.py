# -*- coding: utf-8 -*-
"""Regression tests verifying that state-changing web views reject GET (405) and succeed on POST."""

import ast
import inspect
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, SimpleTestCase, TestCase
from django.urls import URLPattern, URLResolver, reverse

from viewer.models import (
    Archive,
    ArchiveGroup,
    ArchiveGroupEntry,
    ArchiveMatches,
    FoundGallery,
    Gallery,
    GalleryMatch,
    GalleryMatchGroup,
    UserArchivePrefs,
    WantedGallery,
)
from workers import client as workers_client
from workers.models import TaskQueue


class PostOnlyViewsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            username="post_only_staff",
            password="password123",
            is_staff=True,
            is_superuser=True,
        )
        cls.gallery = Gallery.objects.create(
            gid="g-post-1",
            provider="panda",
            title="Gallery One",
            public=False,
            hidden=False,
        )
        cls.gallery2 = Gallery.objects.create(
            gid="g-post-2",
            provider="panda",
            title="Gallery Two",
            public=True,
        )
        cls.archive = Archive.objects.create(
            title="Archive One",
            zipped="a1.zip",
            extracted=False,
            crc32="12345678",
            user=cls.staff,
        )
        cls.wanted = WantedGallery.objects.create(
            title="Wanted One",
            should_search=True,
            public=False,
        )
        cls.group = ArchiveGroup.objects.create(
            title="Group One",
            title_slug="group-one",
            public=True,
        )
        ArchiveGroupEntry.objects.create(archive_group=cls.group, archive=cls.archive, position=1)

    def setUp(self):
        self.client = Client()
        self.client.force_login(self.staff)

    def test_queue_operations_requires_post(self):
        workers_client.enqueue(["https://example.com/g/1/"], task_type="web_crawler")
        url = reverse("viewer:queue-operations", args=["clear", "web_crawler"])

        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count(), 1)

        resp_post = self.client.post(url)
        self.assertEqual(resp_post.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(status=TaskQueue.Status.PENDING).count(), 0)

    def test_archive_action_views_reject_get_and_accept_post(self):
        # extract (with POST body resized=1)
        extract_url = reverse("viewer:archive-extract", args=[self.archive.pk])
        self.assertEqual(self.client.get(extract_url).status_code, 405)
        self.assertEqual(TaskQueue.objects.count(), 0)

        resp = self.client.post(extract_url, {"resized": "1"})
        self.assertEqual(resp.status_code, 302)
        task = TaskQueue.objects.get(task_type="extract_worker")
        self.assertTrue(task.options.get("resized"))

        # reduce
        TaskQueue.objects.all().delete()
        self.archive.extracted = True
        self.archive.save()
        reduce_url = reverse("viewer:archive-reduce", args=[self.archive.pk])
        self.assertEqual(self.client.get(reduce_url).status_code, 405)
        self.assertEqual(TaskQueue.objects.count(), 0)
        self.assertEqual(self.client.post(reduce_url).status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="reduce_worker").count(), 1)

        # check_and_convert_filetype
        TaskQueue.objects.all().delete()
        convert_url = reverse("viewer:check-convert-type", args=[self.archive.pk])
        self.assertEqual(self.client.get(convert_url).status_code, 405)
        self.assertEqual(TaskQueue.objects.count(), 0)
        self.assertEqual(self.client.post(convert_url).status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="convert_archive_worker").count(), 1)

        # mark_similar_archives (with POST body use-recycled=1)
        TaskQueue.objects.all().delete()
        similar_url = reverse("viewer:archive-mark-similar", args=[self.archive.pk])
        self.assertEqual(self.client.get(similar_url).status_code, 405)
        self.assertEqual(TaskQueue.objects.count(), 0)
        self.assertEqual(self.client.post(similar_url, {"use-recycled": "1"}).status_code, 302)
        sim_task = TaskQueue.objects.get(task_type="mark_similar_worker")
        self.assertTrue(sim_task.options.get("use_recycled"))

        # generate_matches (with POST body clear=1)
        TaskQueue.objects.all().delete()
        gen_url = reverse("viewer:archive-generate-matches", args=[self.archive.pk])
        self.assertEqual(self.client.get(gen_url).status_code, 405)
        self.assertEqual(TaskQueue.objects.count(), 0)
        self.assertEqual(self.client.post(gen_url, {"clear": "1", "provider": "panda"}).status_code, 302)
        gen_task = TaskQueue.objects.get(task_type="match_unmatched_worker")
        self.assertTrue(gen_task.options.get("clear_title"))
        self.assertEqual(gen_task.options.get("provider_filter"), "panda")

    def test_archive_update_and_col_archive_update_require_post(self):
        ArchiveMatches.objects.create(archive=self.archive, gallery=self.gallery, match_type="title", match_accuracy=0.9)

        # Admin archive_update tool routes
        select_url = reverse("viewer:archive-update-tool-id", args=[self.archive.pk, "select-as-match", self.gallery.pk])
        clear_url = reverse("viewer:archive-update-tool", args=[self.archive.pk, "clear-possible-matches"])
        self.assertEqual(self.client.get(select_url).status_code, 405)
        self.assertEqual(self.client.get(clear_url).status_code, 405)
        self.archive.refresh_from_db()
        self.assertIsNone(self.archive.gallery_id)
        self.assertEqual(ArchiveMatches.objects.filter(archive=self.archive).count(), 1)

        # Collaborator archive_update tool routes
        col_select_url = reverse(
            "viewer:col-archive-update-tool-id", args=[self.archive.pk, "select-as-match", self.gallery.pk]
        )
        col_clear_url = reverse("viewer:col-archive-update-tool", args=[self.archive.pk, "clear-possible-matches"])
        self.assertEqual(self.client.get(col_select_url).status_code, 405)
        self.assertEqual(self.client.get(col_clear_url).status_code, 405)
        self.archive.refresh_from_db()
        self.assertIsNone(self.archive.gallery_id)

        # POST works
        self.assertEqual(self.client.post(col_select_url).status_code, 302)
        self.archive.refresh_from_db()
        self.assertEqual(self.archive.gallery_id, self.gallery.pk)

    def test_col_gallery_update_requires_post(self):
        add_url = reverse(
            "viewer:col-gallery-update-tool-id", args=[self.gallery.pk, "add-to-match-group", self.gallery2.pk]
        )
        clear_url = reverse("viewer:col-gallery-update-tool", args=[self.gallery.pk, "clear-possible-matches"])

        self.assertEqual(self.client.get(add_url).status_code, 405)
        self.assertEqual(self.client.get(clear_url).status_code, 405)
        self.assertFalse(GalleryMatchGroup.objects.exists())

        resp = self.client.post(add_url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(GalleryMatchGroup.objects.count(), 1)
        group = GalleryMatchGroup.objects.first()
        self.assertIsNotNone(group)
        assert group is not None
        self.assertCountEqual(list(group.galleries.values_list("pk", flat=True)), [self.gallery.pk, self.gallery2.pk])

    def test_gallery_tool_routes_require_post_while_detail_allows_get(self):
        # Plain gallery detail page still allows GET
        self.assertEqual(self.client.get(reverse("viewer:gallery", args=[self.gallery.pk])).status_code, 200)

        for tool in ("download", "toggle-hidden", "toggle-public", "mark-deleted", "mark-normal", "recall-api", "refetch-thumbnail"):
            url = reverse("viewer:gallery-tool", args=[self.gallery.pk, tool])
            self.assertEqual(self.client.get(url).status_code, 405, f"GET unexpectedly allowed for gallery tool {tool}")

        find_tool_url = reverse("viewer:gallery-find-tool", args=["toggle-hidden"]) + f"?gid={self.gallery.gid}"
        self.assertEqual(self.client.get(find_tool_url).status_code, 405)

        self.gallery.refresh_from_db()
        self.assertFalse(self.gallery.hidden)
        self.assertFalse(self.gallery.public)
        self.assertEqual(TaskQueue.objects.count(), 0)

        # POST mutates
        self.assertEqual(self.client.post(reverse("viewer:gallery-tool", args=[self.gallery.pk, "toggle-hidden"])).status_code, 302)
        self.gallery.refresh_from_db()
        self.assertTrue(self.gallery.hidden)

        self.assertEqual(self.client.post(reverse("viewer:gallery-tool", args=[self.gallery.pk, "download"])).status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="web_crawler").count(), 1)

    def test_wanted_gallery_tools_require_post_while_detail_allows_get(self):
        base_url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])
        self.assertEqual(self.client.get(base_url).status_code, 200)

        GalleryMatch.objects.create(wanted_gallery=self.wanted, gallery=self.gallery, match_accuracy=0.8)

        for query in (
            "?tool=toggle-public",
            "?tool=stop-searching",
            "?tool=clear-possible-matches",
            f"?tool=select-as-match&tool-id={self.gallery.pk}",
            f"?tool=remove-match&tool-id={self.gallery.pk}",
            "?tool=create-possible-matches-internally",
            "?tool=create-rule-matches-internally",
            "?tool=create-matches-internally",
            "?tool=backlog-search",
        ):
            self.assertEqual(self.client.get(base_url + query).status_code, 405, f"GET allowed for {query}")

        self.wanted.refresh_from_db()
        self.assertFalse(self.wanted.public)
        self.assertTrue(self.wanted.should_search)
        self.assertEqual(GalleryMatch.objects.filter(wanted_gallery=self.wanted).count(), 1)
        self.assertFalse(FoundGallery.objects.filter(wanted_gallery=self.wanted).exists())

        # POST with body fields works
        resp = self.client.post(base_url, {"tool": "select-as-match", "tool-id": str(self.gallery.pk)})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(FoundGallery.objects.filter(wanted_gallery=self.wanted, gallery=self.gallery).exists())

        resp = self.client.post(base_url, {"tool": "stop-searching"})
        self.assertEqual(resp.status_code, 302)
        self.wanted.refresh_from_db()
        self.assertFalse(self.wanted.should_search)

    def test_wanted_gallery_possible_matches_locally_queues_title_search_with_options(self):
        base_url = reverse("viewer:wanted-gallery", args=[self.wanted.pk])

        resp = self.client.post(
            base_url,
            {
                "tool": "create-possible-matches-internally",
                "provider": "panda",
                "cutoff": "0.7",
                "max-matches": "3",
                "must-be-used": "1",
            },
        )
        self.assertEqual(resp.status_code, 302)
        task = TaskQueue.objects.get(task_type="wanted_local_search_worker")
        self.assertEqual(task.args, [self.wanted.pk])
        self.assertEqual(
            task.options["matching_kwargs"],
            {"provider_filter": "panda", "cutoff": 0.7, "max_matches": 3, "must_be_used": True},
        )

        # Already queued: refuse instead of stacking another run.
        resp = self.client.post(base_url, {"tool": "create-possible-matches-internally"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="wanted_local_search_worker").count(), 1)

        # Empty or invalid values fall back to the defaults, the cutoff is clamped.
        TaskQueue.objects.all().delete()
        self.client.post(base_url, {"tool": "create-possible-matches-internally", "cutoff": "5", "max-matches": ""})
        task = TaskQueue.objects.get(task_type="wanted_local_search_worker")
        self.assertEqual(
            task.options["matching_kwargs"],
            {"provider_filter": "", "cutoff": 1.0, "max_matches": 10, "must_be_used": False},
        )

    def test_archive_group_extract_all_requires_post(self):
        detail_url = reverse("viewer:archive-group", args=[self.group.title_slug])
        edit_url = reverse("viewer:archive-group-edit", args=[self.group.title_slug])

        # Plain GET renders pages
        self.assertEqual(self.client.get(detail_url).status_code, 200)
        self.assertEqual(self.client.get(edit_url).status_code, 200)

        # GET with extract_all is rejected
        self.assertEqual(self.client.get(detail_url + "?extract_all=").status_code, 405)
        self.assertEqual(self.client.get(edit_url + "?extract_all=").status_code, 405)
        self.assertEqual(TaskQueue.objects.count(), 0)

        # POST with extract_all enqueues extraction
        resp = self.client.post(detail_url, {"extract_all": "1"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(TaskQueue.objects.filter(task_type="extract_worker").count(), 1)

    def test_user_archive_preferences_requires_post(self):
        fav_url = reverse("viewer:user-archive-preferences", args=[self.archive.pk, "favorite"])
        self.assertEqual(self.client.get(fav_url).status_code, 405)
        self.assertFalse(UserArchivePrefs.objects.filter(user=self.staff, archive=self.archive).exists())

        resp = self.client.post(fav_url)
        self.assertEqual(resp.status_code, 302)
        pref = UserArchivePrefs.objects.get(user=self.staff, archive=self.archive)
        self.assertEqual(pref.favorite_group, 1)

    def test_logout_requires_post(self):
        logout_url = reverse("viewer:logout")
        self.assertEqual(self.client.get(logout_url).status_code, 405)
        # Still logged in after rejected GET
        self.assertIn("_auth_user_id", self.client.session)

        resp = self.client.post(logout_url)
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)


class PostOnlyViewASTAuditTests(SimpleTestCase):
    """Static AST guard: ensure routed views in viewer/views/ don't perform unguarded GET mutations."""

    MUTATING_CALLS = {
        "save",
        "simple_save",
        "delete",
        "bulk_create",
        "bulk_update",
        "create",
        "update_or_create",
        "enqueue",
        "enqueue_gallery_update",
        "enqueue_contextual_extraction",
        "enqueue_matching_task",
        "request_command",
        "cancel_task",
        "clear_pending_tasks",
        "remove_by_index",
        "mark_as_deleted",
        "mark_as_denied",
        "mark_as_normal",
        "fetch_thumbnail",
        "select_as_match",
        "recalc_fileinfo",
        "create_marks_for_similar_archives",
        "delete_all_files",
        "delete_files_but_archive",
        "prune_database",
        "reconcile_downloads",
        "call_command",
        "logout",
    }

    # Views that legitimately initialize a read-only preference row or delegate to method-checked helpers.
    ALLOWLIST = {
        # gallery_details / gallery_details_gid_provider delegate to process_gallery_page which guards `tool` on POST.
        "viewer.views.head.gallery.gallery_details",
        "viewer.views.head.gallery.gallery_details_gid_provider",
    }

    def _collect_url_view_callbacks(self, patterns):
        callbacks = set()
        for entry in patterns:
            if isinstance(entry, URLPattern):
                cb = entry.callback
                if hasattr(cb, "view_class"):
                    continue
                callbacks.add(inspect.unwrap(cb))
            elif isinstance(entry, URLResolver):
                callbacks.update(self._collect_url_view_callbacks(entry.url_patterns))
        return callbacks

    def test_no_routed_view_mutates_without_post_guard(self):
        from viewer.urls import urlpatterns

        callbacks = self._collect_url_view_callbacks(urlpatterns)
        offenders: list[str] = []

        for fn in callbacks:
            mod = getattr(fn, "__module__", "")
            qual = f"{mod}.{getattr(fn, '__name__', '')}"
            if not mod.startswith("viewer.views.") or qual in self.ALLOWLIST:
                continue
            try:
                src_file = inspect.getsourcefile(fn)
            except TypeError:
                continue
            if not src_file:
                continue

            tree = ast.parse(Path(src_file).read_text(encoding="utf-8"), src_file)
            func_node = next(
                (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn.__name__),
                None,
            )
            if func_node is None:
                continue

            decs = [ast.unparse(d) for d in func_node.decorator_list]
            if any("require_POST" in d or "require_http_methods" in d for d in decs):
                continue

            mutating_hits = []
            for sub in ast.walk(func_node):
                if isinstance(sub, ast.Call):
                    attr = sub.func.attr if isinstance(sub.func, ast.Attribute) else getattr(sub.func, "id", None)
                    if attr in self.MUTATING_CALLS:
                        mutating_hits.append((sub.lineno, attr))

            if not mutating_hits:
                continue

            body_text = ast.unparse(func_node)
            has_method_guard = (
                "request.method" in body_text
                or "request.POST" in body_text
                or "HttpResponseNotAllowed" in body_text
            )
            if not has_method_guard:
                offenders.append(f"{qual} ({src_file}:{func_node.lineno}) calls {mutating_hits}")

        self.assertEqual(offenders, [], "Routed views with unguarded mutating calls:\n" + "\n".join(offenders))
