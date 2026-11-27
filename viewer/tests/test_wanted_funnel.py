import datetime

from django.contrib.auth.models import Permission, User
from django.test import Client, TestCase
from django.urls import reverse

from viewer.models import (
    Archive,
    AutoWantedSource,
    Category,
    FoundGallery,
    Gallery,
    GalleryMatch,
    Provider,
    UserLongLivedToken,
    WantedGallery,
)
from viewer.services.wanted_funnel import (
    BASELINE_CREATE,
    BASELINE_EFFECTIVE,
    BASELINE_RELEASE,
    EMPTY_SENTINEL,
    OVERDUE_SORT_POSSIBLE,
    OVERDUE_SORT_PUBLISHER,
    OVERDUE_SORT_TITLE,
    SCOPE_ACTIVE_OR_FOUND,
    SCOPE_ONE_SHOT,
    SCOPE_RECURRING,
    SCOPE_SEARCHING,
    FunnelOptions,
    build_wanted_funnel,
    format_duration,
)


def _set_create_date(model_cls, pk: int, dt: datetime.datetime) -> None:
    model_cls.objects.filter(pk=pk).update(create_date=dt)


class WantedFunnelHelpersTestCase(TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(None), "—")
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(180), "3m")
        self.assertEqual(format_duration(3600 * 4 + 60 * 15), "4h 15m")
        self.assertEqual(format_duration(3600 * 6), "6h")
        self.assertEqual(format_duration(86400 * 2.5), "2.5d")
        self.assertEqual(format_duration(86400 * 45), "45d")
        self.assertEqual(format_duration(86400 * 400), "1y 1mo")
        self.assertEqual(format_duration(-86400 * 3), "-3.0d")


class WantedFunnelServiceTestCase(TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)
        self.user = User.objects.create_superuser(
            username="funnel_admin",
            email="funnel@example.com",
            password="testpassword",
        )
        self.prov_panda = Provider.objects.create(name="Panda", slug="panda")
        self.prov_fakku = Provider.objects.create(name="Fakku", slug="fakku")
        self.cat_doujin = Category.objects.create(name="Doujinshi")
        self.cat_anthology = Category.objects.create(name="Anthology")
        self.auto_src = AutoWantedSource.objects.create(name="Wani Feed", provider="wanimagazine")

    def _make_wanted(
        self,
        *,
        title: str,
        created_days_ago: float,
        release_days_ago: float | None = None,
        found: bool = False,
        date_found_days_ago: float | None = None,
        should_search: bool = True,
        keep_searching: bool = False,
        publisher: str = "wanimagazine",
        category: str = "Manga",
        book_type: str = "new_publication",
        reason: str = "wanimagazine",
        auto_source: AutoWantedSource | None = None,
    ) -> WantedGallery:
        release_dt = (
            self.now - datetime.timedelta(days=release_days_ago) if release_days_ago is not None else None
        )
        date_found_dt = (
            self.now - datetime.timedelta(days=date_found_days_ago) if date_found_days_ago is not None else None
        )
        wg = WantedGallery.objects.create(
            title=title,
            release_date=release_dt,
            found=found,
            date_found=date_found_dt,
            should_search=should_search,
            keep_searching=keep_searching,
            publisher=publisher,
            category=category,
            book_type=book_type,
            reason=reason,
            auto_wanted_source=auto_source,
        )
        _set_create_date(WantedGallery, wg.pk, self.now - datetime.timedelta(days=created_days_ago))
        wg.refresh_from_db()
        return wg

    def _make_gallery(self, gid: str, provider: str = "panda", *, with_archive: bool = False, binned: bool = False):
        g = Gallery.objects.create(title=f"Gallery {gid}", gid=gid, provider=provider)
        if with_archive:
            Archive.objects.create(title=f"Archive {gid}", user=self.user, gallery=g, binned=binned)
        return g

    def _make_found(self, wg: WantedGallery, gallery: Gallery, found_days_ago: float) -> FoundGallery:
        fg = FoundGallery.objects.create(wanted_gallery=wg, gallery=gallery)
        _set_create_date(FoundGallery, fg.pk, self.now - datetime.timedelta(days=found_days_ago))
        return fg

    def test_empty_database(self):
        result = build_wanted_funnel(now=self.now)
        self.assertEqual(result["summary"]["total"], 0)
        self.assertEqual(result["summary"]["found"], 0)
        self.assertIsNone(result["summary"]["hit_rate"])
        self.assertEqual(result["time_to_find"]["sample_count"], 0)
        self.assertEqual(result["time_to_find"]["median"]["display"], "—")
        self.assertEqual(result["breakdowns"]["publishers"], [])
        self.assertEqual(result["breakdowns"]["categories"], [])
        self.assertEqual(result["overdue"]["total_count"], 0)
        self.assertEqual(result["overdue"]["items"], [])

    def test_funnel_stages_first_found_and_archived(self):
        # 1. Recurring rule created 100d ago: first FoundGallery at 98d ago (TTF = 2d),
        #    later FoundGallery at 1d ago overwrote date_found to 1d ago! Must measure 2d, not 99d.
        wg_recurring = self._make_wanted(
            title="Monthly Magazine",
            created_days_ago=100,
            release_days_ago=100,
            found=True,
            date_found_days_ago=1,
            keep_searching=True,
            publisher="wanimagazine",
            category="Manga",
            auto_source=self.auto_src,
        )
        wg_recurring.wanted_providers.add(self.prov_panda)
        g1 = self._make_gallery("g1", provider="panda", with_archive=True)
        g2 = self._make_gallery("g2", provider="panda", with_archive=False)
        self._make_found(wg_recurring, g1, found_days_ago=98)
        self._make_found(wg_recurring, g2, found_days_ago=1)

        # 2. Found via date_found fallback (no FoundGallery row) -> TTF = 10d, NOT archived.
        self._make_wanted(
            title="Found Legacy",
            created_days_ago=30,
            release_days_ago=40,
            found=True,
            date_found_days_ago=20,
            publisher="wanimagazine",
            category="Manga",
        )

        # 3. Found with binned archive only -> does NOT count as archived; missing timestamp tracked.
        wg_binned = self._make_wanted(
            title="Binned Only",
            created_days_ago=20,
            found=True,
            date_found_days_ago=None,
            publisher="irodori",
            category="",
        )
        wg_binned.categories.add(self.cat_doujin)
        g_binned = self._make_gallery("g_binned", provider="fakku", with_archive=True, binned=True)
        # Note: has no FoundGallery -> tests missing timestamp AND binned archive check; add separate binned one below.
        wg_binned_fg = self._make_wanted(
            title="Binned With FG",
            created_days_ago=20,
            found=True,
            date_found_days_ago=10,
            publisher="irodori",
            category="Doujinshi",
        )
        self._make_found(wg_binned_fg, g_binned, found_days_ago=10)

        # 4. Actively searching, overdue (45d >= 30d), with 2 possible matches.
        wg_overdue = self._make_wanted(
            title="Overdue Active",
            created_days_ago=45,
            release_days_ago=60,
            found=False,
            should_search=True,
            publisher="\r\n\t\tirodori",  # Leading whitespace should normalize into "irodori"
            category="Doujinshi",
        )
        g_cand1 = self._make_gallery("cand1")
        g_cand2 = self._make_gallery("cand2")
        GalleryMatch.objects.create(wanted_gallery=wg_overdue, gallery=g_cand1, match_accuracy=0.9)
        GalleryMatch.objects.create(wanted_gallery=wg_overdue, gallery=g_cand2, match_accuracy=0.8)

        # 5. Actively searching, fresh (5d < 30d).
        self._make_wanted(
            title="Fresh Active",
            created_days_ago=5,
            release_days_ago=5,
            found=False,
            should_search=True,
            publisher="wanimagazine",
            category="Manga",
        )

        # 6. Actively searching, upcoming release (release_date 10d in the future) -> NOT overdue!
        self._make_wanted(
            title="Future Release",
            created_days_ago=50,
            release_days_ago=-10,
            found=False,
            should_search=True,
            publisher="wanimagazine",
            category="Manga",
        )

        # 7. Paused unfound (should_search=False, 200d old) -> hidden from overdue by default.
        self._make_wanted(
            title="Paused Backlog",
            created_days_ago=200,
            release_days_ago=200,
            found=False,
            should_search=False,
            publisher="fakku",
            category="",
        )

        result = build_wanted_funnel(FunnelOptions(overdue_days=30), now=self.now)
        summary = result["summary"]

        self.assertEqual(summary["total"], 8)
        self.assertEqual(summary["searched_or_found"], 7)  # excludes Paused Backlog
        self.assertEqual(summary["found"], 4)
        self.assertEqual(summary["archived"], 1)  # only wg_recurring has non-binned archive
        self.assertEqual(summary["searching_unfound"], 3)
        self.assertEqual(summary["paused_unfound"], 1)
        self.assertEqual(summary["upcoming_unfound"], 1)
        self.assertEqual(summary["fresh_searching_unfound"], 1)
        self.assertEqual(summary["overdue_active"], 1)
        self.assertEqual(summary["overdue_paused"], 1)
        self.assertEqual(summary["overdue_displayed"], 1)
        self.assertEqual(summary["unfound_with_possible"], 1)
        self.assertEqual(summary["hit_rate"], 50.0)
        self.assertEqual(summary["active_hit_rate"], 57.1)
        self.assertEqual(summary["archive_rate"], 25.0)

        # Time-to-find samples: 2d (wg_recurring), 10d (Found Legacy), 10d (Binned With FG) -> median 10d, min 2d
        ttf = result["time_to_find"]
        self.assertEqual(ttf["sample_count"], 3)
        self.assertEqual(ttf["missing_timestamp_count"], 1)
        self.assertAlmostEqual(ttf["min"]["days"], 2.0, places=2)
        self.assertAlmostEqual(ttf["median"]["days"], 10.0, places=2)
        self.assertAlmostEqual(ttf["max"]["days"], 10.0, places=2)

        # Publisher whitespace normalization: "\r\n\t\tirodori" merged with "irodori" (total=3)
        pubs = {row["key"]: row for row in result["breakdowns"]["publishers"]}
        self.assertEqual(pubs["irodori"]["total"], 3)
        self.assertEqual(pubs["irodori"]["found"], 2)
        self.assertEqual(pubs["irodori"]["overdue"], 1)
        self.assertEqual(pubs["wanimagazine"]["total"], 4)
        self.assertEqual(pubs["wanimagazine"]["found"], 2)
        self.assertEqual(pubs["fakku"]["total"], 1)
        self.assertEqual(pubs["fakku"]["paused"], 1)

        # Category breakdown includes M2M category ("Doujinshi" from wg_binned + wg_binned_fg + wg_overdue = 3)
        cats = {row["key"]: row for row in result["breakdowns"]["categories"]}
        self.assertEqual(cats["Doujinshi"]["total"], 3)
        self.assertEqual(cats["Doujinshi"]["found"], 2)
        self.assertEqual(cats["Manga"]["total"], 4)
        self.assertEqual(cats[""]["total"], 1)  # Paused Backlog has no category or M2M categories

        # Auto-wanted source breakdown
        auto_sources = {row["key"]: row for row in result["breakdowns"]["auto_wanted_sources"]}
        self.assertEqual(auto_sources["Wani Feed"]["total"], 1)
        self.assertEqual(auto_sources["Wani Feed"]["found"], 1)

        # Found gallery source providers
        found_provs = {row["provider"]: row for row in result["breakdowns"]["found_providers"]}
        self.assertEqual(found_provs["panda"]["wanted_galleries"], 1)
        self.assertEqual(found_provs["panda"]["found_entries"], 2)
        self.assertEqual(found_provs["fakku"]["wanted_galleries"], 1)

        # Overdue list (active only by default)
        overdue = result["overdue"]
        self.assertEqual(overdue["total_count"], 1)
        self.assertEqual(overdue["items"][0]["id"], wg_overdue.pk)
        self.assertEqual(overdue["items"][0]["possible_match_count"], 2)
        self.assertEqual(overdue["items"][0]["publisher"], "irodori")

        # Include paused -> both Overdue Active and Paused Backlog appear, longest waiting first
        with_paused = build_wanted_funnel(FunnelOptions(overdue_days=30, include_paused=True), now=self.now)
        self.assertEqual(with_paused["overdue"]["total_count"], 2)
        self.assertEqual([i["title"] for i in with_paused["overdue"]["items"]], ["Paused Backlog", "Overdue Active"])

    def test_baselines_and_pre_release_leak(self):
        # Created 20d ago, released 10d ago, found 12d ago (2d BEFORE official release -> pre-release leak!)
        wg_leak = self._make_wanted(
            title="Pre-release Leak",
            created_days_ago=20,
            release_days_ago=10,
            found=True,
            date_found_days_ago=12,
        )
        g = self._make_gallery("leak1")
        self._make_found(wg_leak, g, found_days_ago=12)

        # Unfound item with NO release_date (40d since created)
        self._make_wanted(
            title="No Release Date",
            created_days_ago=40,
            release_days_ago=None,
            found=False,
            should_search=True,
        )

        # Baseline: create_date -> TTF = +8d, No Release Date IS overdue
        res_create = build_wanted_funnel(FunnelOptions(baseline=BASELINE_CREATE, overdue_days=30), now=self.now)
        self.assertAlmostEqual(res_create["time_to_find"]["median"]["days"], 8.0, places=2)
        self.assertEqual(res_create["time_to_find"]["before_baseline_count"], 0)
        self.assertEqual(res_create["overdue"]["total_count"], 1)

        # Baseline: release_date -> TTF = -2d (leak), No Release Date tracked in missing_baseline_count
        res_rel = build_wanted_funnel(FunnelOptions(baseline=BASELINE_RELEASE, overdue_days=30), now=self.now)
        self.assertAlmostEqual(res_rel["time_to_find"]["median"]["days"], -2.0, places=2)
        self.assertEqual(res_rel["time_to_find"]["before_baseline_count"], 1)
        bucket_keys = [b["key"] for b in res_rel["time_to_find"]["buckets"]]
        self.assertIn("lt_0", bucket_keys)
        self.assertEqual(res_rel["overdue"]["total_count"], 0)
        self.assertEqual(res_rel["overdue"]["missing_baseline_count"], 1)

        # Baseline: effective -> max(created 20d ago, release 10d ago) = 10d ago -> TTF = -2d;
        # No Release Date falls back to create_date -> IS overdue.
        res_eff = build_wanted_funnel(FunnelOptions(baseline=BASELINE_EFFECTIVE, overdue_days=30), now=self.now)
        self.assertAlmostEqual(res_eff["time_to_find"]["median"]["days"], -2.0, places=2)
        self.assertEqual(res_eff["overdue"]["total_count"], 1)

    def test_scopes_filters_and_overdue_sorting_pagination(self):
        self._make_wanted(
            title="Beta Item",
            created_days_ago=90,
            found=False,
            should_search=True,
            keep_searching=False,
            publisher="zeta_pub",
            category="Manga",
        )
        wg_alpha = self._make_wanted(
            title="Alpha Item",
            created_days_ago=60,
            found=False,
            should_search=True,
            keep_searching=True,
            publisher="alpha_pub",
            category="",
        )
        wg_alpha.categories.add(self.cat_anthology)
        wg_alpha.wanted_providers.add(self.prov_panda)
        GalleryMatch.objects.create(wanted_gallery=wg_alpha, gallery=self._make_gallery("m1"), match_accuracy=0.95)

        self._make_wanted(
            title="Paused Item",
            created_days_ago=120,
            found=False,
            should_search=False,
            publisher="",
            category="",
        )

        # Scope filters
        self.assertEqual(build_wanted_funnel(FunnelOptions(scope=SCOPE_ACTIVE_OR_FOUND), now=self.now)["summary"]["total"], 2)
        self.assertEqual(build_wanted_funnel(FunnelOptions(scope=SCOPE_SEARCHING), now=self.now)["summary"]["total"], 2)
        self.assertEqual(build_wanted_funnel(FunnelOptions(scope=SCOPE_RECURRING), now=self.now)["summary"]["total"], 1)
        self.assertEqual(build_wanted_funnel(FunnelOptions(scope=SCOPE_ONE_SHOT), now=self.now)["summary"]["total"], 2)

        # Dimension filters including EMPTY_SENTINEL and M2M category/provider
        self.assertEqual(build_wanted_funnel(FunnelOptions(publisher=EMPTY_SENTINEL), now=self.now)["summary"]["total"], 1)
        self.assertEqual(build_wanted_funnel(FunnelOptions(category="Anthology"), now=self.now)["summary"]["total"], 1)
        self.assertEqual(build_wanted_funnel(FunnelOptions(category=EMPTY_SENTINEL), now=self.now)["summary"]["total"], 1)
        self.assertEqual(build_wanted_funnel(FunnelOptions(provider="panda"), now=self.now)["summary"]["total"], 1)
        self.assertEqual(build_wanted_funnel(FunnelOptions(provider=EMPTY_SENTINEL), now=self.now)["summary"]["total"], 2)

        # Sorting overdue items
        by_title = build_wanted_funnel(FunnelOptions(overdue_sort=OVERDUE_SORT_TITLE), now=self.now)
        self.assertEqual([i["title"] for i in by_title["overdue"]["items"]], ["Alpha Item", "Beta Item"])

        by_possible = build_wanted_funnel(FunnelOptions(overdue_sort=OVERDUE_SORT_POSSIBLE), now=self.now)
        self.assertEqual([i["title"] for i in by_possible["overdue"]["items"]], ["Alpha Item", "Beta Item"])

        by_pub = build_wanted_funnel(FunnelOptions(overdue_sort=OVERDUE_SORT_PUBLISHER), now=self.now)
        self.assertEqual([i["publisher"] for i in by_pub["overdue"]["items"]], ["alpha_pub", "zeta_pub"])

        # Pagination
        page1 = build_wanted_funnel(FunnelOptions(overdue_per_page=1, overdue_page=1), now=self.now)
        page2 = build_wanted_funnel(FunnelOptions(overdue_per_page=1, overdue_page=2), now=self.now)
        self.assertEqual(page1["overdue"]["num_pages"], 2)
        self.assertTrue(page1["overdue"]["has_next"])
        self.assertEqual(page1["overdue"]["items"][0]["title"], "Beta Item")
        self.assertTrue(page2["overdue"]["has_previous"])
        self.assertEqual(page2["overdue"]["items"][0]["title"], "Alpha Item")


class WantedFunnelViewTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("viewer:stats-wanted")
        self.staff_user = User.objects.create_user(
            username="staff_user", password="password123", is_staff=True
        )
        self.plain_user = User.objects.create_user(
            username="plain_user", password="password123", is_staff=False
        )
        self.wanted_perm_user = User.objects.create_user(
            username="wanted_col", password="password123", is_staff=False
        )
        self.wanted_perm_user.user_permissions.add(Permission.objects.get(codename="view_wantedgallery"))

        self.stats_perm_user = User.objects.create_user(
            username="stats_col", password="password123", is_staff=False
        )
        self.stats_perm_user.user_permissions.add(Permission.objects.get(codename="read_private_stats"))

    def test_permissions_html_and_json(self):
        # Anonymous -> login redirect for HTML, 403 for JSON
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("viewer:login"), resp["Location"])

        resp_json = self.client.get(f"{self.url}?format=json")
        self.assertEqual(resp_json.status_code, 403)

        # Authenticated without perms -> redirect with error message / 403 JSON
        self.client.force_login(self.plain_user)
        resp_plain = self.client.get(self.url, follow=True)
        self.assertContains(resp_plain, "permissions")
        self.assertEqual(self.client.get(f"{self.url}?format=json").status_code, 403)

        # Collaborators with view_wantedgallery or read_private_stats -> allowed
        for user in (self.wanted_perm_user, self.stats_perm_user, self.staff_user):
            self.client.force_login(user)
            r_html = self.client.get(self.url)
            self.assertEqual(r_html.status_code, 200)
            self.assertContains(r_html, "Wanted-Gallery Funnel")
            r_json = self.client.get(f"{self.url}?format=json")
            self.assertEqual(r_json.status_code, 200)
            payload = r_json.json()
            self.assertIn("summary", payload)
            self.assertIn("time_to_find", payload)
            self.assertIn("breakdowns", payload)
            self.assertIn("overdue", payload)

    def test_bearer_token_json_access(self):
        _, raw_token = UserLongLivedToken.create_for_user(
            self.staff_user,
            "funnel-reader",
            scopes=[UserLongLivedToken.SCOPE_READ],
        )
        _, plain_token = UserLongLivedToken.create_for_user(
            self.plain_user,
            "plain-reader",
            scopes=[UserLongLivedToken.SCOPE_READ],
        )
        _, write_only_token = UserLongLivedToken.create_for_user(
            self.staff_user,
            "write-only",
            scopes=[UserLongLivedToken.SCOPE_WRITE],
        )
        resp = self.client.get(f"{self.url}?format=json", HTTP_AUTHORIZATION=f"Bearer {raw_token}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("summary", resp.json())

        self.assertEqual(
            self.client.get(f"{self.url}?format=json", HTTP_AUTHORIZATION=f"Bearer {plain_token}").status_code,
            403,
        )
        self.assertEqual(
            self.client.get(f"{self.url}?format=json", HTTP_AUTHORIZATION=f"Bearer {write_only_token}").status_code,
            403,
        )

    def test_invalid_params_handling(self):
        self.client.force_login(self.staff_user)
        # JSON returns 400 on invalid params
        resp_bad_json = self.client.get(f"{self.url}?format=json&overdue_days=notanumber")
        self.assertEqual(resp_bad_json.status_code, 400)
        self.assertIn("error", resp_bad_json.json())

        resp_bad_scope = self.client.get(f"{self.url}?format=json&scope=bogus")
        self.assertEqual(resp_bad_scope.status_code, 400)

        # HTML falls back cleanly to defaults and displays an error message
        resp_bad_html = self.client.get(f"{self.url}?overdue_days=0")
        self.assertEqual(resp_bad_html.status_code, 200)
        self.assertContains(resp_bad_html, "overdue_days must be between")

    def test_no_n_plus_one_queries(self):
        """Query count must stay constant as publishers, categories, providers, and overdue items grow."""
        now = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)
        # Seed 1 initial item so non-empty code paths (found, overdue, m2m) all execute in baseline.
        prov0 = Provider.objects.create(name="Prov0", slug="prov_0")
        cat0 = Category.objects.create(name="Cat0")
        g0 = Gallery.objects.create(title="G0", gid="q0", provider="prov_0")
        Archive.objects.create(title="A0", user=self.staff_user, gallery=g0)
        wg0 = WantedGallery.objects.create(
            title="Found 0", found=True, date_found=now, publisher="pub_0", category="Cat0"
        )
        wg0.wanted_providers.add(prov0)
        wg0.categories.add(cat0)
        FoundGallery.objects.create(wanted_gallery=wg0, gallery=g0)

        wg_ov0 = WantedGallery.objects.create(
            title="Overdue 0", found=False, should_search=True, publisher="pub_0", category="Cat0"
        )
        _set_create_date(WantedGallery, wg_ov0.pk, now - datetime.timedelta(days=60))

        self.client.force_login(self.staff_user)

        with self.assertNumQueries(14):
            resp = self.client.get(self.url)
            self.assertEqual(resp.status_code, 200)

        # Add 6 more distinct publishers, categories, providers, found + overdue wanted galleries.
        for i in range(1, 7):
            prov = Provider.objects.create(name=f"Prov{i}", slug=f"prov_{i}")
            cat = Category.objects.create(name=f"Cat{i}")
            g = Gallery.objects.create(title=f"G{i}", gid=f"q{i}", provider=f"prov_{i}")
            Archive.objects.create(title=f"A{i}", user=self.staff_user, gallery=g)
            wg_f = WantedGallery.objects.create(
                title=f"Found {i}", found=True, date_found=now, publisher=f"pub_{i}", category=f"Cat{i}"
            )
            wg_f.wanted_providers.add(prov)
            wg_f.categories.add(cat)
            FoundGallery.objects.create(wanted_gallery=wg_f, gallery=g)

            wg_u = WantedGallery.objects.create(
                title=f"Overdue {i}", found=False, should_search=True, publisher=f"pub_{i}", category=f"Cat{i}"
            )
            wg_u.wanted_providers.add(prov)
            _set_create_date(WantedGallery, wg_u.pk, now - datetime.timedelta(days=60 + i))

        # Query count MUST remain identical!
        with self.assertNumQueries(14):
            resp = self.client.get(self.url)
            self.assertEqual(resp.status_code, 200)
