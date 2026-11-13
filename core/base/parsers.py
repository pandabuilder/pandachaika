import copy
import json
import sys
import typing
from datetime import datetime, timezone
import time

import django.utils.timezone as django_tz
import logging

import traceback

from collections import defaultdict
from collections.abc import Callable, Iterable

from django.db import close_old_connections
from django.db.models import QuerySet, Q, Value, CharField, F
from django.db.models.functions import Concat, Replace

from core.base import setup_utilities
from core.base.cancellation import TaskCancelledError, ping_activity
from core.base.utilities import send_pushover_notification, chunks, provider_web_request
from core.base.types import GalleryData, ProviderSettings
from viewer.signals import wanted_gallery_found
from viewer.models import Gallery, WantedGallery, FoundGallery

if typing.TYPE_CHECKING:
    import requests
    from core.downloaders.handlers import BaseDownloader
    from core.base.setup import Settings
    from viewer.models import Archive

logger = logging.getLogger(__name__)


def aggregate_wanted_time_taken(func):
    def wrapper_save_time_taken(*args, **kwargs):
        start = time.perf_counter()
        func(*args, **kwargs)
        end = time.perf_counter()
        args[0].time_taken_wanted += end - start

    return wrapper_save_time_taken


class BaseParser[T_ProviderSettings: ProviderSettings]:
    name = ""
    ignore = False
    accepted_urls: list[str] = []
    empty_list: list[str] = []
    # Whether the parser can store the links found on index/feed pages for later (see MonitoredLink's separate
    # frequencies). Parsers without it always crawl and process in a single run.
    supports_link_capture: bool = False

    __SKIP_WAIT_TIME_DOWNLOADER_TYPES = ("info", "submit")

    def __init__(self, settings: "Settings") -> None:
        self.settings = settings
        self.own_settings: T_ProviderSettings = (
            typing.cast(T_ProviderSettings, settings.providers[self.name])
            if hasattr(settings, "providers") and isinstance(settings.providers, dict) and self.name in settings.providers
            else typing.cast(T_ProviderSettings, None)
        )
        self.general_utils = setup_utilities.GeneralUtils(self.settings)
        self.downloaders: list[tuple["BaseDownloader", int]] = self.settings.provider_context.get_downloaders(
            self.settings, self.general_utils, filter_provider=self.name
        )
        self.last_used_downloader: "BaseDownloader" | None = None
        self.time_taken_wanted: float = 0
        self.wanted_matches_count: int = 0
        self.archive_callback: Callable[["Archive" | None, str | None, str], None] | None = None
        self.gallery_callback: Callable[["Gallery" | None, str | None, str], None] | None = None

    def web_request(
        self, url: str, request_dict: dict[str, typing.Any], post: bool = False
    ) -> "requests.models.Response | None":
        """Request to the provider's website, rate limited in the shared "<provider>_web" bucket.

        Raises ProviderBannedError if the provider is in a long cooldown."""
        return provider_web_request(self.name, self.own_settings.wait_timer)(url, request_dict, post=post)

    def notify_archive(self, archive: "Archive | None", crawled_url: str | None, result: str) -> None:
        if archive:
            if getattr(self.settings, "preserve_user_favorites", None):
                try:
                    from viewer.models import UserArchivePrefs
                    for fav in self.settings.preserve_user_favorites:
                        user_val = fav.get("user") or fav.get("user_id")
                        if user_val:
                            fav_grp = fav.get("favorite_group", 1)
                            UserArchivePrefs.objects.get_or_create(
                                archive=archive,
                                user_id=int(user_val),
                                favorite_group=int(fav_grp) if fav_grp is not None else 1,
                            )
                except Exception:
                    logger.critical(traceback.format_exc())

            if getattr(self.settings, "preserve_extracted", False) and not archive.extracted and archive.crc32:
                try:
                    archive.extract()
                except Exception:
                    logger.critical(traceback.format_exc())

        if self.archive_callback:
            self.archive_callback(archive, crawled_url, result)
        elif getattr(self.settings, "archive_user", None):
            try:
                from viewer.utils.actions import event_log
                action = getattr(self.settings, "event_action", None) or (
                    "DOWNLOAD_ARCHIVE" if getattr(self.settings, "redownload", False) else "ADD_ARCHIVE"
                )
                reason = getattr(self.settings, "archive_reason", "") or getattr(self.settings, "gallery_reason", "") or None
                event_log(
                    self.settings.archive_user,
                    action,
                    reason=reason,
                    content_object=archive,
                    result=result,
                    data=crawled_url,
                )
            except Exception:
                logger.critical(traceback.format_exc())

    def notify_gallery(self, gallery: "Gallery | None", crawled_url: str | None, result: str) -> None:
        if getattr(self.settings, "submit_group_uuid", None):
            try:
                from viewer.models import GallerySubmitEntry
                entries = GallerySubmitEntry.objects.filter(submit_group=self.settings.submit_group_uuid)
                if crawled_url:
                    entries.filter(submit_url=crawled_url).update(submit_result=result)
                if gallery:
                    entries.filter(submit_url=gallery.get_link()).update(gallery=gallery)
                    if getattr(gallery, "gid", None):
                        for entry in entries:
                            if entry.submit_url and str(gallery.gid) in entry.submit_url:
                                entry.gallery = gallery
                                entry.save()
            except Exception:
                logger.critical(traceback.format_exc())

        if self.gallery_callback:
            self.gallery_callback(gallery, crawled_url, result)
        elif getattr(self.settings, "archive_user", None):
            try:
                from viewer.utils.actions import event_log
                action = getattr(self.settings, "event_action", None) or (
                    "DOWNLOAD_GALLERY" if getattr(self.settings, "redownload", False) else "ADD_GALLERY"
                )
                reason = getattr(self.settings, "gallery_reason", "") or getattr(self.settings, "archive_reason", "") or None
                event_log(
                    self.settings.archive_user,
                    action,
                    reason=reason,
                    content_object=gallery,
                    result=result,
                    data=crawled_url,
                )
            except Exception:
                logger.critical(traceback.format_exc())

    # We need this dispatcher because some provider have multiple ways of getting data (single, multiple),
    # or some have priorities (json fetch, crawl gallery page).
    # Each provider should set in this method how it needs to call everything, and could even check against a setting
    # to decide (cookie is set, page is available, etc).
    # It should at least check for str (URL) and list (list of URLs).
    def fetch_gallery_data(self, url: str) -> GalleryData | None:
        return None

    def fetch_multiple_gallery_data(self, url_list: list[str]) -> list[GalleryData] | None:
        return None

    def filter_accepted_urls(self, urls: Iterable[str]) -> list[str]:
        return [x for x in urls if any(word in x for word in self.accepted_urls)]

    # Hooks for core.web.gallery_updater.GalleryMetadataUpdater, which refreshes the metadata of already known
    # galleries by link, without going through crawl_urls (no feeds, wanted filters or downloaders involved).

    # How many links are fetched before the results are written to the database.
    update_fetch_chunk_size: int = 25

    def supports_gallery_update(self) -> bool:
        return (
            type(self).fetch_multiple_gallery_data is not BaseParser.fetch_multiple_gallery_data
            or type(self).fetch_gallery_data_for_update is not BaseParser.fetch_gallery_data_for_update
        )

    def filter_update_urls(self, urls: Iterable[str]) -> list[str]:
        """Links that this parser can refresh. Providers should restrict this to single gallery links."""
        return self.filter_accepted_urls(urls)

    def fetch_gallery_data_for_update(self, url_list: list[str]) -> list[GalleryData]:
        """Fetch fresh metadata for the given gallery links. This is the only step that does network requests."""
        return self.fetch_multiple_gallery_data(url_list) or []

    def after_gallery_update(self, gallery: "Gallery", gallery_data: GalleryData) -> None:
        """Provider specific work to do after the Gallery was updated in the database."""
        return None

    # The idea here is: if it failed and 'retry_failed is not set, don't process
    # If it has at least 1 archive, to force redownload, 'redownload' must be set
    # If it has no archives, to force processing, 'replace_metadata' must be set
    # Skipped galleries are not processed again.
    # We don't log directly here because some methods would spam otherwise (feed crawling)
    def discard_gallery_by_internal_checks(
        self, gallery_id: str | None = None, link: str = "", gallery: "Gallery | None" = None
    ) -> tuple[bool, str]:

        discarded, reason = self.do_get_gallery_discard_reason(gallery_id, link, gallery)

        return discarded, reason

    def do_get_gallery_discard_reason(self, gallery_id: str | None = None, link: str = "", gallery: "Gallery | None" = None) -> tuple[bool, str]:
        if self.settings.update_metadata_mode:
            return False, "Gallery link {ext_link} running in update metadata mode, processing.".format(
                ext_link=link,
            )
        if not gallery and gallery_id:
            gallery = Gallery.objects.filter_first(gid=gallery_id, provider=self.name)
        if not gallery:
            return False, "Gallery link {ext_link} has not been added, processing.".format(
                ext_link=link,
            )
        if gallery.is_submitted():
            message = "Gallery {title}, {ext_link} marked as submitted: {link}, reprocessing.".format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return False, message
        if not self.settings.retry_failed and ("failed" in gallery.dl_type):
            message = (
                "Gallery {title}, {ext_link} failed in previous "
                "run: {link}, skipping (setting: retry_failed).".format(
                    ext_link=gallery.get_link(),
                    link=gallery.get_absolute_url(),
                    title=gallery.title,
                )
            )
            return True, message
        if gallery.archive_set.all() and not self.settings.redownload:
            message = (
                "Gallery {title}, {ext_link} already added, dl_type: {dl_type} "
                "and has at least 1 archive: {link}, skipping (setting: redownload).".format(
                    ext_link=gallery.get_link(),
                    link=gallery.get_absolute_url(),
                    title=gallery.title,
                    dl_type=gallery.dl_type,
                )
            )
            return True, message
        if not gallery.archive_set.all() and not self.settings.replace_metadata:
            message = "Gallery {title}, {ext_link} already added: {link}, skipping (setting: replace_metadata).".format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return True, message
        if "skipped" in gallery.dl_type:
            message = "Gallery {title}, {ext_link} marked as skipped: {link}, skipping.".format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return True, message
        if gallery.is_deleted():
            message = "Gallery {title}, {ext_link} marked as deleted: {link}, skipping.".format(
                ext_link=gallery.get_link(),
                link=gallery.get_absolute_url(),
                title=gallery.title,
            )
            return True, message
        message = "Gallery {title}, {ext_link} already added, but was not discarded: {link}, processing.".format(
            ext_link=gallery.get_link(),
            link=gallery.get_absolute_url(),
            title=gallery.title,
        )
        return False, message

    # Priorities are: title, tags then file count.
    @aggregate_wanted_time_taken
    def compare_gallery_with_wanted_filters(
        self,
        gallery: GalleryData,
        link: str,
        wanted_filters: QuerySet,
        gallery_wanted_lists: dict[str, list["WantedGallery"]],
    ) -> None:

        if gallery.title or gallery.title_jpn:
            q_objects = Q()
            q_objects_unwanted = Q()
            q_objects_regexp = Q()
            q_objects_regexp_icase = Q()
            q_objects_unwanted_regexp = Q()
            q_objects_unwanted_regexp_icase = Q()
            if gallery.title:
                wanted_filters = wanted_filters.annotate(g_title=Value(gallery.title, output_field=CharField()))

                q_objects.add(
                    Q(g_title__ss=Concat(Value("%"), Replace(F("search_title"), Value(" "), Value("%")), Value("%"))),
                    Q.OR,
                )
                q_objects_unwanted.add(
                    ~Q(
                        g_title__ss=Concat(Value("%"), Replace(F("unwanted_title"), Value(" "), Value("%")), Value("%"))
                    ),
                    Q.AND,
                )

                q_objects_regexp.add(Q(g_title__regex=F("search_title")), Q.OR)
                q_objects_regexp_icase.add(Q(g_title__iregex=F("search_title")), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title__regex=F("unwanted_title")), Q.AND)
                q_objects_unwanted_regexp_icase.add(~Q(g_title__iregex=F("unwanted_title")), Q.AND)

            if gallery.title_jpn:
                wanted_filters = wanted_filters.annotate(g_title_jpn=Value(gallery.title_jpn, output_field=CharField()))
                q_objects.add(
                    Q(
                        g_title_jpn__ss=Concat(
                            Value("%"), Replace(F("search_title"), Value(" "), Value("%")), Value("%")
                        )
                    ),
                    Q.OR,
                )
                q_objects_unwanted.add(
                    ~Q(
                        g_title_jpn__ss=Concat(
                            Value("%"), Replace(F("unwanted_title"), Value(" "), Value("%")), Value("%")
                        )
                    ),
                    Q.AND,
                )

                q_objects_regexp.add(Q(g_title_jpn__regex=F("search_title")), Q.OR)
                q_objects_regexp_icase.add(Q(g_title_jpn__iregex=F("search_title")), Q.OR)
                q_objects_unwanted_regexp.add(~Q(g_title_jpn__regex=F("unwanted_title")), Q.AND)
                q_objects_unwanted_regexp_icase.add(~Q(g_title_jpn__iregex=F("unwanted_title")), Q.AND)

            filtered_wanted: QuerySet[WantedGallery] = wanted_filters.filter(
                Q(search_title__isnull=True)
                | Q(search_title="")
                | Q(Q(regexp_search_title=False), q_objects)
                | Q(Q(regexp_search_title=True, regexp_search_title_icase=False), q_objects_regexp)
                | Q(Q(regexp_search_title=True, regexp_search_title_icase=True), q_objects_regexp_icase)
            ).filter(
                Q(unwanted_title__isnull=True)
                | Q(unwanted_title="")
                | Q(Q(regexp_unwanted_title=False), q_objects_unwanted)
                | Q(Q(regexp_unwanted_title=True, regexp_unwanted_title_icase=False), q_objects_unwanted_regexp)
                | Q(Q(regexp_unwanted_title=True, regexp_unwanted_title_icase=True), q_objects_unwanted_regexp_icase)
            )

        else:
            filtered_wanted = wanted_filters.filter(Q(search_title__isnull=True) | Q(search_title="")).filter(
                Q(unwanted_title__isnull=True) | Q(unwanted_title="")
            )

        if gallery.category:
            filtered_wanted = filtered_wanted.filter(
                Q(category__isnull=True) | Q(category="") | Q(category__iexact=gallery.category)
            )
            filtered_wanted = filtered_wanted.filter(Q(categories=None) | Q(categories__name=gallery.category))

        if gallery.filecount:
            filtered_wanted = filtered_wanted.filter(
                Q(wanted_page_count_upper=0) | Q(wanted_page_count_upper__gte=gallery.filecount)
            )
            filtered_wanted = filtered_wanted.filter(
                Q(wanted_page_count_lower=0) | Q(wanted_page_count_lower__lte=gallery.filecount)
            )

        filtered_wanted = filtered_wanted.filter(Q(wanted_providers=None) | Q(wanted_providers__slug=self.name))
        filtered_wanted = filtered_wanted.filter(Q(unwanted_providers=None) | ~Q(unwanted_providers__slug=self.name))

        if gallery.posted:
            filtered_wanted = filtered_wanted.filter(
                Q(wait_for_time__isnull=True) | Q(wait_for_time__lte=django_tz.now() - gallery.posted)
            )

        filtered_wanted = filtered_wanted.prefetch_related(
            "wanted_tags",
            "unwanted_tags",
        )

        already_founds = FoundGallery.objects.filter(
            wanted_gallery__in=filtered_wanted, gallery__gid=gallery.gid, gallery__provider=self.name
        ).select_related("gallery", "wanted_gallery")

        for wanted_filter in filtered_wanted:
            # Skip wanted_filter that's already found.
            already_found = [x for x in already_founds if x.wanted_gallery.id == wanted_filter.id]
            # Skip already found unless it's a submitted gallery.
            if already_found and not already_found[0].gallery.is_submitted():
                continue
            accepted = True
            if bool(wanted_filter.wanted_tags.all()):
                if not set(wanted_filter.wanted_tags_list()).issubset(set(gallery.tags)):
                    accepted = False
                # Review based on 'accept if none' scope.
                if not accepted and wanted_filter.wanted_tags_accept_if_none_scope:
                    missing_tags = set(wanted_filter.wanted_tags_list()).difference(set(gallery.tags))
                    # If all the missing tags start with the parameter,
                    # and no other tag is in gallery with this parameter, mark as accepted
                    scope_formatted = wanted_filter.wanted_tags_accept_if_none_scope + ":"
                    if all(x.startswith(scope_formatted) for x in missing_tags) and not any(
                        x.startswith(scope_formatted) for x in gallery.tags
                    ):
                        accepted = True
                # Do not accept galleries that have more than 1 tag in the same wanted tag scope.
                if accepted & wanted_filter.wanted_tags_exclusive_scope:
                    accepted_tags = set(wanted_filter.wanted_tags_list()).intersection(set(gallery.tags))
                    gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in gallery.tags if len(x) > 1]
                    wanted_gallery_tags_scopes = [x.split(":", maxsplit=1)[0] for x in accepted_tags if len(x) > 1]
                    scope_count: dict[str, int] = defaultdict(int)
                    for scope_name in gallery_tags_scopes:
                        if scope_name in wanted_gallery_tags_scopes:
                            if wanted_filter.exclusive_scope_name:
                                if wanted_filter.exclusive_scope_name == scope_name:
                                    scope_count[scope_name] += 1
                            else:
                                scope_count[scope_name] += 1
                    for scope, count in scope_count.items():
                        if count > 1:
                            accepted = False
                    if not accepted:
                        continue

            if not accepted:
                continue

            if bool(wanted_filter.unwanted_tags.all()):
                if any(item in gallery.tags for item in wanted_filter.unwanted_tags_list()):
                    continue

            if wanted_filter.match_expression:
                expression_matched = wanted_filter.evaluate_match_expression(gallery)
                if not expression_matched:
                    continue

            gallery_wanted_lists[gallery.gid].append(wanted_filter)

        if len(gallery_wanted_lists[gallery.gid]) > 0:
            WantedGallery.objects.filter(
                id__in=[x.pk for x in gallery_wanted_lists[gallery.gid]]
            ).update(found=True, date_found=django_tz.now())

            self.wanted_matches_count = getattr(self, "wanted_matches_count", 0) + 1

            if not self.settings.silent_processing:
                if self.wanted_matches_count <= 50:
                    logger.info(
                        "Gallery link: {}, title: {}, matched filters: {}.".format(
                            link,
                            gallery.title,
                            ", ".join([x.get_absolute_url() for x in gallery_wanted_lists[gallery.gid]]),
                        )
                    )
                elif self.wanted_matches_count == 51:
                    logger.info(
                        "Matched 50 WantedGallery filters so far. Suppressing further per-match log entries for this batch (summary will be logged upon completion)."
                    )

            notify_wanted_filters = [
                "({}, {})".format((x.title or "not set"), (x.reason or "not set"))
                for x in gallery_wanted_lists[gallery.gid]
                if x.notify_when_found
            ]

            if notify_wanted_filters and self.settings.pushover.enable:

                message = "Title: {}, link: {}\nFilters title, reason: {}".format(
                    gallery.title, link, ", ".join(notify_wanted_filters)
                )

                send_pushover_notification(
                    self.settings.pushover.user_key,
                    self.settings.pushover.token,
                    message,
                    device=self.settings.pushover.device,
                    sound=self.settings.pushover.sound,
                    title="Wanted Gallery match found",
                )
            return

    @staticmethod
    def id_from_url(url: str) -> str | None:
        pass

    @staticmethod
    def token_from_url(url: str) -> str | None:
        pass

    @classmethod
    def id_from_url_implemented(cls) -> bool:
        if cls.id_from_url is not BaseParser.id_from_url:
            return True
        return False

    def get_feed_urls(self) -> list[str]:
        return self.empty_list

    def crawl_feed(self, feed_url: str = "") -> list[typing.Any]:
        return self.empty_list

    def feed_urls_implemented(self) -> bool:
        if (
            type(self).crawl_feed is not BaseParser.crawl_feed
            and type(self).get_feed_urls is not BaseParser.get_feed_urls
        ):
            return True
        return False

    def crawl_urls_caller(
        self,
        urls: list[str],
        wanted_filters: QuerySet | None = None,
        wanted_only: bool = False,
        preselected_wanted_matches: dict[str, list["WantedGallery"]] | None = None,
    ):
        self.wanted_matches_count = 0
        try:
            self.crawl_urls(
                urls,
                wanted_filters=wanted_filters,
                wanted_only=wanted_only,
                preselected_wanted_matches=preselected_wanted_matches,
            )
        except BaseException:
            logger.critical(traceback.format_exc())
        close_old_connections()

    def crawl_urls(
        self,
        urls: list[str],
        wanted_filters: QuerySet | None = None,
        wanted_only: bool = False,
        preselected_wanted_matches: dict[str, list["WantedGallery"]] | None = None,
    ) -> None:
        pass

    def post_gallery_processing(self, gallery_entry: "Gallery", gallery_data: "GalleryData"):
        pass

    def is_current_link_non_current(self, gallery_data: "GalleryData") -> bool:
        return False

    def log_database_check_summary(
        self,
        total: int,
        existing: int,
        discarded: int,
        to_update: int = 0,
        logger_override: logging.Logger | None = None,
    ) -> None:
        if total == 0:
            return
        log = logger_override or getattr(sys.modules.get(self.__module__), "logger", None) or logging.getLogger(self.__module__)
        new_count = total - existing
        if existing > 0:
            log.info(
                "Database check for %d galleries: %d existing (%d discarded, %d to update), %d new.",
                total,
                existing,
                discarded,
                to_update,
                new_count,
            )
        else:
            log.info(
                "Database check for %d galleries: 0 existing in database (all %d new).",
                total,
                total,
            )

    def log_metadata_evaluation_summary(
        self,
        total_evaluated: int,
        accepted: int,
        matched_wanted: int = 0,
        unmatched_wanted: int = 0,
        banned: int = 0,
        errors: int = 0,
        wanted_filters: bool = False,
        wanted_only: bool = False,
        logger_override: logging.Logger | None = None,
    ) -> None:
        if total_evaluated == 0:
            return
        log = logger_override or getattr(sys.modules.get(self.__module__), "logger", None) or logging.getLogger(self.__module__)
        extra_banned = f", {banned} discarded (banned data)" if banned else ""
        extra_errors = f", {errors} API/network errors" if errors else ""
        if wanted_filters:
            if wanted_only:
                log.info(
                    "Metadata check for %d galleries: %d matched wanted filters, %d skipped as non-matching (wanted-only mode)%s%s.",
                    total_evaluated,
                    matched_wanted,
                    unmatched_wanted,
                    extra_banned,
                    extra_errors,
                )
            else:
                log.info(
                    "Metadata check for %d galleries: %d to process (%d matched wanted filters)%s%s.",
                    total_evaluated,
                    accepted,
                    matched_wanted,
                    extra_banned,
                    extra_errors,
                )
        else:
            log.info(
                "Metadata check for %d galleries: %d to process%s%s.",
                total_evaluated,
                accepted,
                extra_banned,
                extra_errors,
            )

    def pass_gallery_data_to_downloaders(
        self,
        gallery_data_list: list[GalleryData],
        gallery_wanted_lists: dict[str, list["WantedGallery"]],
        force_provider: bool = False,
    ):
        gallery_count = len(gallery_data_list)

        total_matched_galleries = len(gallery_wanted_lists)
        if total_matched_galleries > 0:
            total_filter_matches = sum(len(flist) for flist in gallery_wanted_lists.values())
            logger.info(
                "WantedGallery matching complete: %d galleries matched (%d total filter matches).",
                total_matched_galleries,
                total_filter_matches,
            )

        if self.time_taken_wanted:
            logger.info(
                "Time taken to compare with WantedGallery: {} seconds.".format(int(self.time_taken_wanted + 0.5))
            )

        if gallery_count == 0:
            logger.info("No galleries need downloading, returning.")
            return
        else:
            logger.info("{} galleries for downloaders to work with.".format(gallery_count))

        should_dispatch = (
            getattr(self.settings, "is_worker_process", False)
            and getattr(self.settings, "current_task_type", "") in ("web_crawler", "webcrawler")
            and getattr(self.settings, "dispatch_gallery_creation", True)
            and gallery_count > 0
        )

        if should_dispatch:
            from workers import client as worker_client
            from workers.serialization import serialize_settings_options

            links = [g.link for g in gallery_data_list if g.link] or [f"{g.provider}:{g.gid}" for g in gallery_data_list]
            task_options = serialize_settings_options(self.settings)
            task_options["provider"] = self.name
            task_options["force_provider"] = force_provider
            task_options["galleries_data"] = [g.to_dict() for g in gallery_data_list]
            task_options["gallery_wanted_lists"] = {
                gid: [wg.pk for wg in wg_list if hasattr(wg, "pk")]
                for gid, wg_list in gallery_wanted_lists.items()
            }

            task = worker_client.enqueue(
                args=links,
                task_type="gallery_creator",
                override_options=task_options,
                user=getattr(self.settings, "archive_user", None),
                reason=getattr(self.settings, "archive_reason", "") or "",
                event_action=getattr(self.settings, "event_action", "") or "CRAWL",
                priority=getattr(self.settings, "current_task_priority", 10),
            )
            logger.info(
                "Dispatched %d galleries to 'gallery_creator' task #%d to unlock web_crawler queue.",
                gallery_count,
                task.pk,
            )
            return

        if not self.settings.update_metadata_mode:
            downloaders_msg = "Downloaders (name, priority):"

            for downloader in self.downloaders:
                downloaders_msg += " ({}, {})".format(downloader[0], downloader[1])
            logger.info(downloaders_msg)

        activity_cb = getattr(self.settings, "activity_callback", None)
        is_cancelled = getattr(self.settings, "is_cancelled", None)
        is_bulk = gallery_count > 50 or self.settings.silent_processing

        for i, gallery in enumerate(gallery_data_list, start=1):
            if callable(is_cancelled) and is_cancelled():
                raise TaskCancelledError(f"Task cancelled while processing gallery {i} of {gallery_count}")
            ping_activity(activity_cb)

            if (
                self.last_used_downloader is not None
                and self.last_used_downloader.type not in self.__SKIP_WAIT_TIME_DOWNLOADER_TYPES
                and getattr(self.last_used_downloader, "queued_download_data", None) is None
            ):
                # We can't assume that every parser has its own downloader,
                # could be from another provider, so we can't directly use self.own_settings
                last_used_provider = self.last_used_downloader.provider
                if last_used_provider in self.settings.providers:
                    time.sleep(self.settings.providers[last_used_provider].wait_timer)
                elif self.own_settings:
                    time.sleep(self.own_settings.wait_timer)
                else:
                    time.sleep(self.settings.wait_timer)

            if not self.settings.silent_processing:
                if not is_bulk or i <= 50:
                    logger.info("Working with gallery {} of {}".format(i, gallery_count))
                elif i == 51:
                    logger.info(
                        "Processed 50 galleries so far. Suppressing further per-gallery logs for this batch (milestones will be logged)."
                    )
                elif i % 500 == 0 or i == gallery_count:
                    logger.info("Processing gallery %d of %d (%.1f%%)", i, gallery_count, (i / gallery_count) * 100)
            elif i % 500 == 0 or i == gallery_count:
                logger.info("Processing gallery %d of %d (%.1f%%)", i, gallery_count, (i / gallery_count) * 100)

            if self.settings.add_as_public:
                gallery.public = True
            self.work_gallery_data(gallery, gallery_wanted_lists, force_provider)

        if is_bulk and not self.settings.silent_processing and gallery_count > 50:
            logger.info("Finished processing all %d galleries for provider %s.", gallery_count, self.name)

    def work_gallery_data(
        self, gallery: GalleryData, gallery_wanted_lists: dict[str, list["WantedGallery"]], force_provider: bool = False
    ) -> None:

        if not self.settings.silent_processing:
            if gallery.title is not None:
                logger.info("Title: {}. Link: {}".format(gallery.title, gallery.link))
            else:
                logger.info("Link: {}".format(gallery.link))

        # If there's a WG match, and we are processing submitted galleries, revert the downloaders to default.
        if (
            len(gallery_wanted_lists[gallery.gid]) > 0
            and len(self.downloaders) == 1
            and self.downloaders[0][0].type == "submit"
        ):
            to_use_downloaders = self.settings.provider_context.get_downloaders(
                self.settings, self.general_utils, filter_provider=self.name, priorities=self.settings.back_up_downloaders
            )
            downloaders_msg = "WantedGallery match, reverting to default downloaders (name, priority):"
            for downloader in to_use_downloaders:
                downloaders_msg += " ({}, {})".format(downloader[0], downloader[1])
            logger.info(downloaders_msg)
        elif force_provider:
            to_use_downloaders = self.settings.provider_context.get_downloaders(
                self.settings, self.general_utils, filter_provider=gallery.provider
            )
            downloaders_msg = "Forcing downloaders to Gallery provider: {}. Downloaders (name, priority):".format(
                gallery.provider
            )
            for downloader in to_use_downloaders:
                downloaders_msg += " ({}, {})".format(downloader[0], downloader[1])
            logger.info(downloaders_msg)
        else:
            to_use_downloaders = self.downloaders

        is_link_non_current = False

        if self.settings.non_current_links_as_deleted:
            is_link_non_current = self.is_current_link_non_current(gallery)
            if is_link_non_current:
                to_use_downloaders = self.settings.provider_context.get_downloaders(
                    self.settings, self.general_utils, filter_provider=self.name, filter_type="info", force=True
                )
                logger.info("Link: {} detected as non-current, it will be added as deleted.".format(gallery.link))

        for cnt, downloader in enumerate(to_use_downloaders):
            downloader[0].init_download(copy.deepcopy(gallery), wanted_gallery_list=gallery_wanted_lists[gallery.gid])

            if downloader[0].return_code == 1:

                if (cnt + 1) == len(to_use_downloaders) and downloader[0].mark_hidden_if_last:
                    if downloader[0].gallery_db_entry:
                        downloader[0].gallery_db_entry.hidden = True
                        downloader[0].gallery_db_entry.simple_save()

                self.last_used_downloader = downloader[0]
                if not downloader[0].archive_only:
                    for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                        FoundGallery.objects.get_or_create(
                            wanted_gallery=wanted_gallery, gallery=downloader[0].gallery_db_entry
                        )

                    if len(gallery_wanted_lists[gallery.gid]) > 0:
                        wanted_gallery_found.send(
                            sender=Gallery,
                            gallery=downloader[0].gallery_db_entry,
                            archive=downloader[0].archive_db_entry,
                            wanted_gallery_list=gallery_wanted_lists[gallery.gid],
                        )
                if downloader[0].archive_db_entry:
                    if not downloader[0].archive_only and downloader[0].gallery_db_entry:
                        logger.info(
                            "Download complete, using downloader: {}. Archive link: {}. Gallery link: {}".format(
                                downloader[0],
                                downloader[0].archive_db_entry.get_absolute_url(),
                                downloader[0].gallery_db_entry.get_absolute_url(),
                            )
                        )
                        self.notify_gallery(downloader[0].gallery_db_entry, gallery.link, "success")
                        self.notify_archive(downloader[0].archive_db_entry, gallery.link, "success")
                    else:
                        logger.info(
                            "Download complete, using downloader: {}. Archive link: {}. No gallery associated".format(
                                downloader[0],
                                downloader[0].archive_db_entry.get_absolute_url(),
                            )
                        )
                        self.notify_archive(downloader[0].archive_db_entry, gallery.link, "success")
                elif downloader[0].gallery_db_entry:
                    if not self.settings.silent_processing:
                        logger.info(
                            "Download completed successfully (gallery only), using downloader: {}. Gallery link: {}".format(
                                downloader[0], downloader[0].gallery_db_entry.get_absolute_url()
                            )
                        )
                    self.notify_gallery(downloader[0].gallery_db_entry, gallery.link, "success")

                    # Process possible nested galleries (contained, magazine)
                    # To avoid downloading extra Archives, it will only be possible to auto add Gallery only downloads
                    # Second, to avoid keeping check of already processed galleries, considering they could be
                    # downloaded from different queues, it will only work with 1 level deep, so that no infinite
                    # nesting happens
                    # Note that we have a filter here to not add galleries that already exist.
                    # If the gallery already exists, the relationship will be set backwards, without
                    # needing to process the Gallery directly
                    # Also, don't auto download related galleries when check submissions
                    if (
                        not self.settings.stop_nested
                        and self.settings.auto_download_nested
                        and self.settings.workers.web_queue
                        and self.downloaders[0][0].type != "submit"
                    ):
                        if gallery.gallery_contains_gids:
                            existing_gids = Gallery.objects.filter(
                                gid__in=gallery.gallery_contains_gids, provider=gallery.provider
                            ).values_list("gid", flat=True)

                            gallery_urls = [
                                Gallery(gid=x, provider=gallery.provider).get_link()
                                for x in gallery.gallery_contains_gids
                                if x not in existing_gids
                            ]
                            gallery_urls.append("--stop-nested")

                            logger.info(
                                "Gallery: {} contains galleries: {}, adding to queue".format(
                                    downloader[0].gallery_db_entry.get_absolute_url(), gallery_urls
                                )
                            )

                            self.settings.workers.web_queue.enqueue_args_list(gallery_urls, override_options=self.settings)

                        if gallery.magazine_chapters_gids:
                            existing_gids = Gallery.objects.filter(
                                gid__in=gallery.magazine_chapters_gids, provider=gallery.provider
                            ).values_list("gid", flat=True)

                            gallery_urls = [
                                Gallery(gid=x, provider=gallery.provider).get_link()
                                for x in gallery.magazine_chapters_gids
                                if x not in existing_gids
                            ]

                            if gallery_urls:

                                gallery_urls.append("--stop-nested")

                                logger.info(
                                    "Gallery: {} is a magazine and contains galleries: {}, adding to queue".format(
                                        downloader[0].gallery_db_entry.get_absolute_url(), gallery_urls
                                    )
                                )

                                self.settings.workers.web_queue.enqueue_args_list(gallery_urls, override_options=self.settings)

                        if gallery.magazine_gid and not Gallery.objects.filter(
                            gid=gallery.magazine_gid, provider=gallery.provider
                        ):
                            gallery_url = Gallery(
                                gid=gallery.magazine_gid, provider=gallery.provider
                            ).get_link()
                            logger.info(
                                "Gallery: {} is in magazine: {}, adding to queue".format(
                                    downloader[0].gallery_db_entry.get_absolute_url(), gallery_url
                                )
                            )
                            self.settings.workers.web_queue.enqueue_args_list([gallery_url, "--stop-nested"], override_options=self.settings)

                        if gallery.gallery_container_gid and not Gallery.objects.filter(
                            gid=gallery.gallery_container_gid, provider=gallery.provider
                        ):
                            gallery_url = Gallery(
                                gid=gallery.gallery_container_gid, provider=gallery.provider
                            ).get_link()
                            logger.info(
                                "Gallery: {} is contained in: {}, adding to queue".format(
                                    downloader[0].gallery_db_entry.get_absolute_url(), gallery_url
                                )
                            )
                            self.settings.workers.web_queue.enqueue_args_list([gallery_url, "--stop-nested"], override_options=self.settings)

                    self.post_gallery_processing(downloader[0].gallery_db_entry, gallery)

                    if self.settings.non_current_links_as_deleted:
                        if is_link_non_current:
                            downloader[0].gallery_db_entry.mark_as_deleted()

                return
            elif downloader[0].return_code == 0 and (cnt + 1) == len(to_use_downloaders):
                self.last_used_downloader = None
                if not downloader[0].archive_only:
                    downloader[0].original_gallery = gallery
                    downloader[0].original_gallery.dl_type = "failed"
                    downloader[0].original_gallery.hidden = True
                    downloader[0].update_gallery_db()
                    if downloader[0].gallery_db_entry:
                        logger.warning(
                            "Download completed unsuccessfully using downloader: {},"
                            " set as failed as it's the last one. Gallery link: {}".format(
                                downloader[0], downloader[0].gallery_db_entry.get_absolute_url()
                            )
                        )
                        self.notify_gallery(downloader[0].gallery_db_entry, gallery.link, "failed")
                        for wanted_gallery in gallery_wanted_lists[gallery.gid]:
                            FoundGallery.objects.get_or_create(
                                wanted_gallery=wanted_gallery, gallery=downloader[0].gallery_db_entry
                            )

                        self.post_gallery_processing(downloader[0].gallery_db_entry, gallery)

                        if self.settings.non_current_links_as_deleted:
                            if self.is_current_link_non_current(gallery):
                                downloader[0].gallery_db_entry.mark_as_deleted()

                    else:
                        logger.warning(
                            "Download completed unsuccessfully using downloader: {},"
                            " could not set as failed, no entry was updated on the database".format(downloader[0])
                        )
                else:
                    logger.warning(
                        "Download completed unsuccessfully using downloader: {},"
                        " no entry was updated on the database".format(downloader[0])
                    )
                    self.notify_gallery(None, gallery.link, "failed")
            else:
                logger.info(
                    "Download was unsuccessful, using downloader {}. Trying with the next downloader.".format(
                        downloader[0],
                    )
                )


# This assumes we got the data in the format that the API uses ("gc" format).
class InternalParser(BaseParser):
    name = ""
    ignore = True

    def crawl_json(
        self, json_string: str, wanted_filters: QuerySet | None = None, wanted_only: bool = False
    ) -> None:

        dict_list = []
        json_decoded = json.loads(json_string)

        if isinstance(json_decoded, dict):
            dict_list.append(json_decoded)
        elif isinstance(json_decoded, list):
            dict_list = json_decoded

        galleries_gids = []
        found_galleries = set()
        total_galleries_filtered: list[GalleryData] = []
        gallery_wanted_lists: dict[str, list["WantedGallery"]] = defaultdict(list)

        for gallery in dict_list:
            galleries_gids.append(gallery["gid"])
            gallery["posted"] = datetime.fromtimestamp(int(gallery["posted"]), timezone.utc)
            gallery_data = GalleryData(**gallery)
            total_galleries_filtered.append(gallery_data)

        existing_galleries_in_db = set()
        discarded_count = 0
        for galleries_gid_group in list(chunks(galleries_gids, 900)):
            for found_gallery in Gallery.objects.filter(gid__in=galleries_gid_group):
                existing_galleries_in_db.add(found_gallery.gid)
                discard_approved, discard_message = self.discard_gallery_by_internal_checks(
                    gallery=found_gallery, link=found_gallery.get_link()
                )

                if discard_approved:
                    discarded_count += 1
                    logger.info(discard_message)
                    found_galleries.add(found_gallery.gid)

        self.log_database_check_summary(
            total=len(galleries_gids),
            existing=len(existing_galleries_in_db),
            discarded=discarded_count,
            to_update=len(existing_galleries_in_db) - discarded_count,
        )

        banned_count = 0
        unmatched_wanted_count = 0
        matched_wanted_count = 0
        processed_count = 0

        for count, gallery_data in enumerate(total_galleries_filtered):

            if gallery_data.gid in found_galleries:
                continue

            banned_result, banned_reasons = self.general_utils.discard_by_gallery_data(
                gallery_data.tags, gallery_data.uploader
            )

            if banned_result:
                banned_count += 1
                self.notify_gallery(None, gallery_data.link, "banned_data")
                logger.info(
                    "Gallery {} of {}: Skipping gallery link {}, discarded reasons: {}".format(
                        count, len(total_galleries_filtered), gallery_data.title, banned_reasons
                    )
                )
                continue

            if wanted_filters:
                self.compare_gallery_with_wanted_filters(
                    gallery_data, gallery_data.link, wanted_filters, gallery_wanted_lists
                )
                if wanted_only and not gallery_wanted_lists[gallery_data.gid]:
                    unmatched_wanted_count += 1
                    continue

            if len(gallery_wanted_lists.get(gallery_data.gid, [])) > 0:
                matched_wanted_count += 1

            processed_count += 1
            logger.info(
                "Gallery {} of {}:  Gallery {} will be processed.".format(
                    count, len(total_galleries_filtered), gallery_data.title
                )
            )

            if gallery_data.thumbnail:
                original_thumbnail_url = gallery_data.thumbnail_url

                gallery_data.thumbnail_url = gallery_data.thumbnail

                gallery_instance = Gallery.objects.update_or_create_from_values(gallery_data)

                gallery_instance.thumbnail_url = original_thumbnail_url

                gallery_instance.save()
            else:
                Gallery.objects.update_or_create_from_values(gallery_data)

        self.log_metadata_evaluation_summary(
            total_evaluated=len(total_galleries_filtered) - len(found_galleries),
            accepted=processed_count,
            matched_wanted=matched_wanted_count,
            unmatched_wanted=unmatched_wanted_count,
            banned=banned_count,
            errors=0,
            wanted_filters=bool(wanted_filters),
            wanted_only=wanted_only,
        )
