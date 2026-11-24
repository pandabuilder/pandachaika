"""Submit queue, archive matching, activity event log, downloads and server logs pages."""

import datetime
import logging
from unittest.mock import patch

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
    FoundGallery,
    Gallery,
    GalleryGroupPossibleMatches,
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

    def test_manage_galleries_pager_does_not_nest_forms(self):
        # A <form> inside the POST form would close it early, leaving the bulk action buttons outside of it.
        from html.parser import HTMLParser

        class FormNesting(HTMLParser):
            def __init__(self):
                super().__init__()
                self.depth = 0
                self.nested = 0

            def handle_starttag(self, tag, attrs):
                if tag == "form":
                    if self.depth:
                        self.nested += 1
                    self.depth += 1

            def handle_endtag(self, tag):
                if tag == "form":
                    self.depth = max(0, self.depth - 1)

        Gallery.objects.create(gid="3", token="t", provider="panda", title="Gallery Three", filecount=10, filesize=1000)
        response = self.client.get(reverse("viewer:manage-galleries") + "?size=1")
        self.assertEqual(response.context["results"].paginator.num_pages, 3)
        self.assertContains(response, "dm-page-jump")
        parser = FormNesting()
        parser.feed(response.content.decode())
        self.assertEqual(parser.nested, 0)

    def test_manage_galleries_dl_type_filter(self):
        self.gallery.dl_type = "manual"
        self.gallery.save()
        self.other_gallery.dl_type = "failed"
        self.other_gallery.save()
        response = self.client.get(reverse("viewer:manage-galleries") + "?dl_type=manual")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([g.pk for g in response.context["results"]], [self.gallery.pk])
        self.assertContains(response, 'name="dl_type"')

        response = self.client.get(reverse("gallery-dl-type-autocomplete") + "?q=man")
        self.assertContains(response, 'data-value="manual"')
        self.assertNotContains(response, "failed")

        self.client.logout()
        response = self.client.get(reverse("gallery-dl-type-autocomplete"))
        self.assertNotContains(response, "manual")

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
        self.assertIsNone(response.context["stats"])
        self.assertContains(response, "Public Wanted")
        self.assertNotContains(response, "Private Found")

    def test_wanted_galleries_public_version(self):
        wanted = WantedGallery.objects.create(
            title="Public Wanted", search_title="pub", public=True, should_search=True, found=False
        )
        WantedGallery.objects.create(
            title="Public Found", search_title="pubfound", public=True, should_search=True, found=True
        )
        WantedGallery.objects.create(
            title="Public Paused", search_title="pubpaused", public=True, should_search=False, found=False
        )
        WantedGallery.objects.create(
            title="Private Wanted", search_title="priv", public=False, should_search=True, found=False
        )
        WantedGallery.objects.create(
            title="Other Wanted", search_title="Public Wanted", public=True, should_search=True, found=False
        )
        self.client.logout()
        url = reverse("viewer:wanted-galleries")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(g.title for g in response.context["results"]), ["Other Wanted", "Public Wanted"])
        # No management filters, status badges or links to the login-only wanted gallery pages.
        self.assertContains(response, 'name="title"')
        for hidden in (
            'name="tags"',
            "more-filters",
            "wanted-should-search",
            'name="sort"',
            "Keep searching",
            ">Public<",
            "dm-stat-sub",
        ):
            self.assertNotContains(response, hidden)
        self.assertNotContains(response, wanted.get_absolute_url())

        # Searching by title keeps the public defaults and only looks at the titles.
        response = self.client.get(url + "?title=public&wanted-found=1&wanted-should-search-not=1")
        self.assertEqual([g.title for g in response.context["results"]], ["Public Wanted"])

        # Logged-in users can open public wanted galleries, so they keep the links.
        self.client.force_login(User.objects.create_user("normaluser", "normal@example.invalid", "pw"))
        response = self.client.get(url)
        self.assertContains(response, wanted.get_absolute_url())
        self.assertNotContains(response, "more-filters")

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


class RedesignedPagesTest(TestCase):
    """Stats, tools, found galleries and the staff archive/gallery review pages on dm-pages."""

    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "admin@example.invalid", "pw")
        self.client.force_login(self.admin)
        self.gallery = Gallery.objects.create(gid="1", token="t", provider="panda", title="Gallery One", filecount=10, filesize=1000)
        self.other_gallery = Gallery.objects.create(gid="2", token="t", provider="fakku", title="Gallery Two", filecount=10, filesize=1000)

    def test_pages_render_with_dm_pages(self):
        url_names = (
            "tools",
            "stats-collection",
            "found-galleries",
            "manage-archives",
            "archives-not-matched",
            "repeated-archives",
            "gallery-match-groups-batch",
            "archive-filesize-different",
        )
        for name in url_names:
            response = self.client.get(reverse(f"viewer:{name}"))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "dm-page-header")
            self.assertContains(response, "dm-pages.css")

        with patch("viewer.views.head.search.crawler_settings.urls.enable_public_stats", True):
            self.client.logout()
            response = self.client.get(reverse("viewer:public-stats"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "dm-page-header")

    def test_full_metadata_stats_link_is_gone(self):
        self.client.logout()
        response = self.client.get(reverse("viewer:found-galleries"))
        self.assertNotContains(response, "/public/stats.html")
        self.assertNotContains(response, "Full Metadata stats")

    def test_settings_object_page_is_gone(self):
        self.assertEqual(self.client.get("/stats/settings/").status_code, 404)
        response = self.client.get(reverse("viewer:tools"))
        self.assertContains(response, reverse("viewer:tools-id", args=["modify_settings_editor"]))

    def test_tools_counts(self):
        Archive.objects.create(title="No thumb", zipped="t.zip", thumbnail="", crc32="", user=self.admin)
        response = self.client.get(reverse("viewer:tools"))
        self.assertEqual(response.context["counts"]["missing_thumbs"], 1)
        self.assertEqual(response.context["counts"]["missing_info"], 1)
        self.assertEqual(response.context["counts"]["unmatched_archives"], 1)
        self.assertContains(response, 'data-dm-filter=".dm-tool"')

    def test_found_galleries_period_and_source(self):
        wanted = WantedGallery.objects.create(title="Wanted", search_title="w", public=True)
        recent = FoundGallery.objects.create(wanted_gallery=wanted, gallery=self.gallery, source="web")
        old = FoundGallery.objects.create(wanted_gallery=wanted, gallery=self.other_gallery, source="")
        FoundGallery.objects.filter(pk=old.pk).update(create_date=timezone.now() - datetime.timedelta(days=10))
        url = reverse("viewer:found-galleries")

        response = self.client.get(url)
        self.assertEqual(response.context["stats"]["total"], 2)
        self.assertEqual(response.context["stats"]["week"], 1)
        self.assertEqual(response.context["stats"]["month"], 2)

        response = self.client.get(url + "?period=week")
        self.assertEqual([f.pk for f in response.context["results"]], [recent.pk])
        response = self.client.get(url + "?source=-")
        self.assertEqual([f.pk for f in response.context["results"]], [old.pk])
        response = self.client.get(url + "?source=web&limit=25")
        self.assertEqual([f.pk for f in response.context["results"]], [recent.pk])
        self.assertEqual(response.context["limit"], 25)

        # Visitors only see public galleries found by public wanted galleries, without the filters.
        self.client.logout()
        self.gallery.public = True
        self.gallery.save()
        response = self.client.get(url)
        self.assertEqual([f.pk for f in response.context["results"]], [recent.pk])
        self.assertIsNone(response.context["form"])
        self.assertNotContains(response, 'id="filter-form"')

    def test_manage_archives_stats_and_post_redirect(self):
        archive = Archive.objects.create(title="Private", zipped="p.zip", public=False, crc32="x", user=self.admin)
        Archive.objects.create(title="Binned", zipped="b.zip", binned=True, crc32="x", user=self.admin)
        url = reverse("viewer:manage-archives") + "?size=20"
        response = self.client.get(url)
        self.assertEqual(response.context["size"], 20)
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertEqual(response.context["stats"]["private"], 1)
        self.assertEqual(response.context["stats"]["recycled"], 1)

        response = self.client.post(
            url, {f"sel-{archive.pk}": str(archive.pk), "change_archive_reason": "", "archive_reason": "new reason"}
        )
        self.assertRedirects(response, url, fetch_redirect_response=False)
        archive.refresh_from_db()
        self.assertEqual(archive.reason, "new reason")

    def test_repeated_and_different_filesize_archives(self):
        same = Archive.objects.create(title="A1", zipped="a1.zip", filesize=1000, filecount=10, gallery=self.gallery, user=self.admin)
        diff = Archive.objects.create(title="A2", zipped="a2.zip", filesize=900, filecount=9, gallery=self.gallery, user=self.admin)
        Archive.objects.create(title="A3", zipped="a3.zip", filesize=800, filecount=9, gallery=self.gallery, user=self.admin)

        response = self.client.get(reverse("viewer:repeated-archives"))
        galleries = list(response.context["results"])
        self.assertEqual([g.pk for g in galleries], [self.gallery.pk])
        archives = {a.pk: a for a in galleries[0].archive_list}
        self.assertTrue(archives[same.pk].same_size)
        self.assertFalse(archives[diff.pk].same_size)
        self.assertEqual(archives[diff.pk].size_diff, -100)
        self.assertEqual(response.context["providers"], [{"provider": "panda", "count": 1}])

        # Several archives of a different size: the gallery is listed once.
        response = self.client.get(reverse("viewer:archive-filesize-different") + "?provider=panda")
        self.assertEqual([g.pk for g in response.context["results"]], [self.gallery.pk])
        self.assertEqual(response.context["total"], 1)

        url = reverse("viewer:repeated-archives")
        response = self.client.post(url, {f"del-{diff.pk}": str(diff.pk), "delete_archives": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertFalse(Archive.objects.filter(pk=diff.pk).exists())

        response = self.client.post(url, {"delete_archives": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)

    def test_archives_not_matched_candidates_and_redirect(self):
        archive = Archive.objects.create(title="Unmatched", zipped="u.zip", filecount=10, filesize=1000, user=self.admin)
        lonely = Archive.objects.create(title="Lonely", zipped="l.zip", filecount=10, filesize=1000, user=self.admin)
        ArchiveMatches.objects.create(archive=archive, gallery=self.gallery, match_type="title", match_accuracy=0.5)
        ArchiveMatches.objects.create(archive=archive, gallery=self.gallery, match_type="image", match_accuracy=0.95)
        ArchiveMatches.objects.create(archive=archive, gallery=self.other_gallery, match_type="title", match_accuracy=0.7)
        url = reverse("viewer:archives-not-matched")

        response = self.client.get(url + "?with-possible-matches=1")
        listed = response.context["results"][0]
        self.assertEqual([c["gallery"].pk for c in listed.candidates], [self.gallery.pk, self.other_gallery.pk])
        self.assertAlmostEqual(listed.best_accuracy, 0.95)
        self.assertEqual(response.context["stats"]["with_possible"], 1)
        self.assertEqual(response.context["stats"]["without_possible"], 1)

        response = self.client.get(url + "?without-possible-matches=1")
        self.assertEqual([a.pk for a in response.context["results"]], [lonely.pk])

        response = self.client.post(url, {f"sel-{archive.pk}": str(archive.pk), "clear_possible_matches": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertFalse(ArchiveMatches.objects.filter(archive=archive).exists())

    def test_staff_archive_update_answers_json_to_fetch(self):
        archive = Archive.objects.create(title="Archive", zipped="a.zip", filecount=10, filesize=1000, user=self.admin)
        ArchiveMatches.objects.create(archive=archive, gallery=self.gallery, match_type="title", match_accuracy=0.9)
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}

        url = reverse("viewer:archive-update-tool-id", args=(archive.pk, "select-as-match", self.gallery.pk))
        response = self.client.post(url, **ajax)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["gallery"], self.gallery.pk)

        response = self.client.post(reverse("viewer:archive-update-tool", args=(archive.pk, "nonsense")), **ajax)
        self.assertEqual(response.status_code, 400)

        response = self.client.post(reverse("viewer:archive-update-tool", args=(archive.pk, "clear-possible-matches")))
        self.assertEqual(response.status_code, 302)

    def test_gallery_match_groups_batch_candidates_and_json(self):
        group = GalleryMatchGroup.objects.create(title="Group")
        GalleryMatchGroupEntry.objects.create(gallery_match_group=group, gallery=self.gallery, gallery_position=1)
        GalleryGroupPossibleMatches.objects.create(
            gallery_match_group=group, gallery=self.other_gallery, match_type="title", match_accuracy=0.8
        )
        url = reverse("viewer:gallery-match-groups-batch")
        response = self.client.get(url + "?with-possible-matches=1")
        listed = response.context["results"][0]
        self.assertEqual(listed.pk, self.gallery.pk)
        self.assertEqual([c["gallery"].pk for c in listed.candidates], [self.other_gallery.pk])
        self.assertEqual(response.context["stats"]["with_possible"], 1)
        self.assertEqual(response.context["stats"]["single"], 1)

        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        add_url = reverse(
            "viewer:col-gallery-update-tool-id", args=(self.gallery.pk, "add-to-match-group", self.other_gallery.pk)
        )
        response = self.client.post(add_url, **ajax)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["match_group"], group.pk)
        self.assertEqual(group.galleries.count(), 2)

        response = self.client.post(url, {"auto_select_first_match": ""})
        self.assertRedirects(response, url, fetch_redirect_response=False)
