"""Submit queue, archive matching, activity event log, downloads and server logs pages."""

import datetime
import logging

from django.contrib.auth.models import Permission, User
from django.http import QueryDict
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from django_db_logger.models import StatusLog

from viewer.models import (
    Archive,
    ArchiveMatches,
    DownloadEvent,
    EventLog,
    Gallery,
    GalleryMatchGroup,
    GalleryMatchGroupEntry,
    GallerySubmitEntry,
    MonitoredLink,
    WantedGallery,
)
from viewer.templatetags.viewer_extras import event_action_class
from viewer.utils.general import page_size
from viewer.views.admin import _recent_log_level_counts
from viewer.views.collaborators.upload import submit_queue_statuses


class HelpersTest(TestCase):
    def test_page_size(self):
        self.assertEqual(page_size(QueryDict("limit=25")), 25)
        self.assertEqual(page_size(QueryDict("limit=0")), 1)
        self.assertEqual(page_size(QueryDict("limit=100000")), 500)
        self.assertEqual(page_size(QueryDict("limit=abc"), default=50), 50)
        self.assertEqual(page_size(QueryDict(""), default=50), 50)

    def test_event_action_class(self):
        self.assertEqual(event_action_class("DELETE_ARCHIVE"), "bg-danger")
        self.assertEqual(event_action_class("MARK_DELETED"), "bg-danger")
        self.assertEqual(event_action_class("MATCH_ARCHIVE"), "bg-success")
        self.assertEqual(event_action_class("UNPUBLISH_ARCHIVE"), "bg-warning text-dark")
        self.assertEqual(event_action_class("SOMETHING"), "bg-secondary")
        self.assertEqual(event_action_class(None), "bg-secondary")

    def test_url_filter_replaces_values_and_drops_page(self):
        request = RequestFactory().get("/?actions=A&actions=B&page=3&title=x")
        out = Template("{% load viewer_extras %}{% url_filter 'actions' 'C' %}").render(Context({"request": request}))
        params = QueryDict(out.replace("&amp;", "&"))
        self.assertEqual(params.getlist("actions"), ["C"])
        self.assertNotIn("page", params)
        self.assertEqual(params["title"], "x")

    def test_submit_queue_statuses(self):
        self.assertEqual(submit_queue_statuses(QueryDict("")), [GallerySubmitEntry.RESOLVED_SUBMITTED])
        self.assertEqual(
            submit_queue_statuses(QueryDict("status=denied&status=approved&status=bogus")),
            [GallerySubmitEntry.RESOLVED_APPROVED, GallerySubmitEntry.RESOLVED_DENIED],
        )
        # Older checkbox params still work.
        self.assertEqual(
            submit_queue_statuses(QueryDict("hide_submitted=1&denied=1")), [GallerySubmitEntry.RESOLVED_DENIED]
        )
        # An empty status (all chips removed) falls back to the default.
        self.assertEqual(submit_queue_statuses(QueryDict("status=")), [GallerySubmitEntry.RESOLVED_SUBMITTED])

    def test_recent_log_level_counts(self):
        old = StatusLog.objects.create(logger_name="t", level=logging.ERROR, msg="old")
        StatusLog.objects.filter(pk=old.pk).update(create_datetime=timezone.now() - datetime.timedelta(days=3))
        StatusLog.objects.create(logger_name="t", level=logging.ERROR, msg="e")
        StatusLog.objects.create(logger_name="t", level=logging.WARNING, msg="w")
        StatusLog.objects.create(logger_name="t", level=logging.INFO, msg="i")
        StatusLog.objects.create(logger_name="t", level=logging.DEBUG, msg="d")
        counts = _recent_log_level_counts()
        self.assertEqual(counts, {"error": 1, "warning": 1, "info": 1, "debug": 1, "total": 4})


class PagesTest(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "admin@example.invalid", "pw")
        self.client.force_login(self.admin)
        self.gallery = Gallery.objects.create(gid="1", token="t", provider="panda", title="Gallery One", filecount=10, filesize=1000)
        self.other_gallery = Gallery.objects.create(gid="2", token="t", provider="panda", title="Gallery Two", filecount=10, filesize=1000)

    def test_pages_render(self):
        for name in ("submit-queue", "match-archives", "activity-event-log", "downloads", "logs"):
            response = self.client.get(reverse(f"viewer:{name}"))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "dm-page-header")

    def test_submit_queue_lists_and_filters_by_status(self):
        pending = GallerySubmitEntry.objects.create(submit_url="https://example.invalid/pending")
        denied = GallerySubmitEntry.objects.create(
            submit_url="https://example.invalid/denied", resolved_status=GallerySubmitEntry.RESOLVED_DENIED
        )
        response = self.client.get(reverse("viewer:submit-queue"))
        self.assertEqual([e.pk for e in response.context["results"]], [pending.pk])
        tiles = {t["value"]: t["count"] for t in response.context["status_tiles"]}
        self.assertEqual(tiles["submitted"], 1)
        self.assertEqual(tiles["denied"], 1)

        response = self.client.get(reverse("viewer:submit-queue") + "?status=denied")
        self.assertEqual([e.pk for e in response.context["results"]], [denied.pk])

    def test_submit_queue_gallery_fields_filter_without_checkbox(self):
        entry = GallerySubmitEntry.objects.create(submit_url="a")
        entry.gallery = self.gallery
        entry.save()
        other = GallerySubmitEntry.objects.create(submit_url="b")
        other.gallery = self.other_gallery
        other.save()
        response = self.client.get(reverse("viewer:submit-queue") + "?title=One")
        self.assertEqual([e.pk for e in response.context["results"]], [entry.pk])

    def test_submit_queue_action_redirects(self):
        entry = GallerySubmitEntry.objects.create(submit_url="https://example.invalid/x")
        url = reverse("viewer:submit-queue") + "?status=submitted"
        response = self.client.post(url, {f"sel-{entry.pk}": entry.pk, "deny_galleries": "", "entry_reason": "nope"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        entry.refresh_from_db()
        self.assertEqual(entry.resolved_status, GallerySubmitEntry.RESOLVED_DENIED)
        self.assertEqual(entry.resolved_reason, "nope")

        response = self.client.post(url, {"deny_galleries": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)

    def test_match_archives_groups_candidates(self):
        archive = Archive.objects.create(title="Archive", zipped="a.zip", filecount=10, filesize=1000, user=self.admin)
        ArchiveMatches.objects.create(archive=archive, gallery=self.gallery, match_type="title", match_accuracy=0.5)
        ArchiveMatches.objects.create(archive=archive, gallery=self.gallery, match_type="image", match_accuracy=0.95)
        ArchiveMatches.objects.create(archive=archive, gallery=self.other_gallery, match_type="title", match_accuracy=0.7)
        response = self.client.get(reverse("viewer:match-archives") + "?with-possible-matches=1")
        listed = response.context["results"][0]
        self.assertEqual([c["gallery"].pk for c in listed.candidates], [self.gallery.pk, self.other_gallery.pk])
        self.assertEqual(len(listed.candidates[0]["matches"]), 2)
        self.assertAlmostEqual(listed.best_accuracy, 0.95)
        self.assertEqual(response.context["stats"]["with_possible"], 1)

    def test_archive_update_answers_json_to_fetch(self):
        archive = Archive.objects.create(title="Archive", zipped="a.zip", filecount=10, filesize=1000, user=self.admin)
        ArchiveMatches.objects.create(archive=archive, gallery=self.gallery, match_type="title", match_accuracy=0.9)
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

        url = reverse("viewer:col-archive-update-tool-id", args=(archive.pk, "select-as-match", self.gallery.pk))
        response = self.client.post(url, **ajax)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["gallery"], self.gallery.pk)
        archive.refresh_from_db()
        self.assertEqual(archive.gallery_id, self.gallery.pk)

        response = self.client.post(reverse("viewer:col-archive-update-tool", args=(archive.pk, "nonsense")), **ajax)
        self.assertEqual(response.status_code, 400)

        # Without the header it still redirects back, even with no referer.
        url = reverse("viewer:col-archive-update-tool", args=(archive.pk, "clear-possible-matches"))
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)

    def test_activity_event_log_filters(self):
        other = User.objects.create_user("other")
        EventLog.objects.create(user=self.admin, action="MATCH_ARCHIVE", content_object=self.gallery)
        EventLog.objects.create(user=other, action="DENY_GALLERY", content_object=self.gallery)
        url = reverse("viewer:activity-event-log")

        response = self.client.get(url + "?user=other")
        self.assertEqual([e.action for e in response.context["results"]], ["DENY_GALLERY"])
        self.assertTrue(response.context["show_users"])

        response = self.client.get(url)
        self.assertEqual({a["action"] for a in response.context["top_actions"]}, {"MATCH_ARCHIVE", "DENY_GALLERY"})
        self.assertEqual(response.context["activity"]["last_day"], 2)

        today = timezone.localdate().isoformat()
        response = self.client.get(url + f"?event_date_from={today}&event_date_to={today}")
        self.assertEqual(response.context["results"].paginator.count, 2)

        response = self.client.get(url + "?event_date_from=nope")
        self.assertEqual(response.context["results"].paginator.count, 0)

    def test_activity_event_log_title_searches_both_without_scope(self):
        EventLog.objects.create(user=self.admin, action="A", content_object=self.gallery)
        EventLog.objects.create(user=self.admin, action="B", content_object=self.other_gallery)
        response = self.client.get(reverse("viewer:activity-event-log") + "?title=One")
        self.assertEqual([e.action for e in response.context["results"]], ["A"])

    def test_activity_event_log_hides_user_filter_without_permission(self):
        user = User.objects.create_user("reader")
        user.user_permissions.add(Permission.objects.get(codename="read_activity_logs"))
        self.client.force_login(user)
        EventLog.objects.create(user=self.admin, action="A")
        response = self.client.get(reverse("viewer:activity-event-log") + "?user=nobody&show-users=1")
        self.assertEqual(response.context["results"].paginator.count, 1)
        self.assertFalse(response.context["show_users"])
        self.assertEqual(response.context["top_users"], [])

    def test_downloads_stats_in_page_and_json(self):
        DownloadEvent.objects.create(name="q", method="torrent", status=DownloadEvent.Status.QUEUED)
        DownloadEvent.objects.create(name="d", method="torrent", status=DownloadEvent.Status.IN_PROGRESS)
        DownloadEvent.objects.create(name="f", method="torrent", status=DownloadEvent.Status.FAILED, failed=True)
        response = self.client.get(reverse("viewer:downloads"))
        self.assertEqual(response.context["stats"]["queued"], 1)
        self.assertEqual(response.context["stats"]["downloading"], 1)
        self.assertEqual(response.context["stats"]["failed_day"], 1)
        self.assertEqual(response.context["current_status"], "in_progress")

        data = self.client.get(reverse("viewer:downloads") + "?status=all&json=1").json()
        self.assertEqual(data["stats"]["queued"], 1)
        self.assertEqual(len(data["results"]), 3)
        self.assertIn("create_date_iso", data["results"][0])

    def test_logs_page_counts(self):
        StatusLog.objects.create(logger_name="t", level=logging.ERROR, msg="boom")
        response = self.client.get(reverse("viewer:logs") + "?log-level=ERROR")
        self.assertGreaterEqual(response.context["level_counts"]["error"], 1)
        self.assertEqual(response.context["log_level_filter"], "ERROR")

    def test_migrated_legacy_pages_render_with_dm_pages(self):
        url_names = (
            "monitored-links",
            "manage-galleries",
            "wanted-galleries",
            "col-wanted-galleries",
            "gallery-match-groups",
            "col-missing-archives",
            "public-missing-archives",
            "archives-by-field",
            "galleries-by-field",
        )
        for name in url_names:
            response = self.client.get(reverse(f"viewer:{name}"))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "dm-page-header")
            self.assertContains(response, "dm-pages.css")

    def test_monitored_links_stats_and_page_size(self):
        MonitoredLink.objects.create(
            name="Active Feed",
            url="https://example.invalid/feed1",
            frequency=datetime.timedelta(minutes=30),
            enabled=True,
            auto_start=True,
        )
        MonitoredLink.objects.create(
            name="Paused Feed",
            url="https://example.invalid/feed2",
            frequency=datetime.timedelta(hours=1),
            enabled=False,
            auto_start=False,
        )
        response = self.client.get(reverse("viewer:monitored-links") + "?limit=25")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["limit"], 25)
        self.assertEqual(response.context["stats"]["total"], 2)
        self.assertEqual(response.context["stats"]["enabled"], 1)
        self.assertEqual(response.context["stats"]["disabled"], 1)
        self.assertEqual(response.context["stats"]["auto_start"], 1)

    def test_manage_galleries_stats_size_and_post_redirect(self):
        self.gallery.public = True
        self.gallery.save()
        url = reverse("viewer:manage-galleries") + "?size=25"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["size"], 25)
        self.assertEqual(response.context["stats"]["total"], 2)
        self.assertEqual(response.context["stats"]["public"], 1)
        self.assertEqual(response.context["stats"]["private"], 1)

        response = self.client.post(url, {f"sel-{self.other_gallery.pk}": str(self.other_gallery.pk), "publish_galleries": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.other_gallery.refresh_from_db()
        self.assertTrue(self.other_gallery.public)

    def test_wanted_galleries_stats_public_and_staff(self):
        WantedGallery.objects.create(title="Public Wanted", search_title="pub", public=True, should_search=True, found=False)
        WantedGallery.objects.create(title="Private Found", search_title="priv", public=False, should_search=False, found=True)
        response = self.client.get(reverse("viewer:wanted-galleries") + "?limit=50")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["limit"], 50)
        self.assertEqual(response.context["stats"]["total"], 2)
        self.assertEqual(response.context["stats"]["searching"], 1)
        self.assertEqual(response.context["stats"]["found"], 1)

        self.client.logout()
        response = self.client.get(reverse("viewer:wanted-galleries"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertContains(response, "Public Wanted")
        self.assertNotContains(response, "Private Found")

    def test_gallery_match_groups_stats_and_create_redirect(self):
        group = GalleryMatchGroup.objects.create(title="Group Alpha")
        GalleryMatchGroupEntry.objects.create(gallery_match_group=group, gallery=self.gallery, gallery_position=1)
        url = reverse("viewer:gallery-match-groups")
        response = self.client.get(url + "?limit=25&sort=title&asc_desc=desc")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["limit"], 25)
        self.assertEqual(response.context["stats"]["total_groups"], 1)
        self.assertEqual(response.context["stats"]["total_entries"], 1)

        response = self.client.post(url, {"submit-gallery-match-group": "Submit", "title": "Group Beta"})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertTrue(GalleryMatchGroup.objects.filter(title="Group Beta").exists())

    def test_missing_archives_stats_and_post_redirect(self):
        self.gallery.public = True
        self.gallery.save()
        url = reverse("viewer:col-missing-archives") + "?limit=25"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["limit"], 25)
        self.assertGreaterEqual(response.context["stats"]["total"], 2)

        response = self.client.post(url, {f"sel-{self.other_gallery.pk}": str(self.other_gallery.pk), "publish_galleries": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.other_gallery.refresh_from_db()
        self.assertTrue(self.other_gallery.public)

    def test_similar_archives_and_repeated_galleries_grouping_flags(self):
        Archive.objects.create(title="Dup Archive", zipped="dup1.zip", filecount=10, filesize=1000, crc32="abcd1234", user=self.admin)
        Archive.objects.create(title="Dup Archive", zipped="dup2.zip", filecount=10, filesize=1000, crc32="abcd1234", user=self.admin)
        resp = self.client.get(reverse("viewer:archives-by-field"))
        self.assertFalse(resp.context["has_grouping"])
        self.assertEqual(resp.context["total_groups"], 0)

        resp = self.client.get(reverse("viewer:archives-by-field") + "?filter-fileinfo=1&filter-crc32=1&filter-title=1")
        self.assertTrue(resp.context["has_grouping"])
        self.assertEqual(resp.context["total_groups"], 3)

        self.other_gallery.title = self.gallery.title
        self.other_gallery.save()
        resp = self.client.get(reverse("viewer:galleries-by-field"))
        self.assertFalse(resp.context["has_grouping"])
        self.assertEqual(resp.context["total_groups"], 0)

        url = reverse("viewer:galleries-by-field") + "?by-title=1&by-filesize=1"
        resp = self.client.get(url)
        self.assertTrue(resp.context["has_grouping"])
        self.assertEqual(resp.context["total_groups"], 2)

        resp = self.client.post(
            url,
            {
                "sel-title-1": [str(self.gallery.pk), str(self.other_gallery.pk)],
                f"ord-{self.gallery.pk}": "1",
                f"ord-{self.other_gallery.pk}": "2",
                "create_gallery_match_groups": "",
            },
        )
        self.assertRedirects(resp, url, fetch_redirect_response=False)
        self.assertEqual(GalleryMatchGroup.objects.count(), 1)
