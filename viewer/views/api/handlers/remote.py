"""Remote operations API (``/api/v1/remote/``, legacy ``/admin-api/`` and ``/jsonapi/``).

Used by the userscript and the admin match/crawl buttons. Needs a session or a token with the ``remote`` scope, and
the ``viewer.use_remote_api`` permission. POST bodies are ``{"operation": "<name>", "args": ...}``.
"""

import json
import logging
from typing import Any

from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponse
from django.urls import reverse

from core.base.setup import Settings
from core.base.utilities import timestamp_or_zero
from viewer.models import Archive, UserArchivePrefs
from viewer.utils.matching import generate_possible_matches_for_archives
from viewer.views.api.errors import ApiError, json_response
from viewer.views.api.filters import filter_galleries_no_request
from viewer.views.api.handlers.tasks import task_reference
from viewer.views.head import gallery_filter_keys
from workers import client
from workers.models import TaskQueue

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)

REMOTE_OPERATIONS = (
    "webcrawler",
    "match_archive",
    "match_archive_internally",
)


def _find_archive_for_link(parsers: list[Any], link: str) -> Archive | None:
    for parser in parsers:
        if parser.id_from_url_implemented():
            urls_filtered = parser.filter_accepted_urls((link,))
            for url_filtered in urls_filtered:
                gallery_gid = parser.id_from_url(url_filtered)
                if gallery_gid:
                    archive = Archive.objects.filter(gallery__gid=gallery_gid, gallery__provider=parser.name).first()
                    if archive:
                        return archive
            if urls_filtered:
                break
    return None


def _find_existing_archives_count(parsers: list[Any], links: list[str]) -> int:
    count = 0
    for link in links:
        if _find_archive_for_link(parsers, link):
            count += 1
    return count


def _with_task(request: HttpRequest, response: dict[str, Any], task: TaskQueue | None) -> HttpResponse:
    if task is not None:
        response.update(task_reference(request, task))
    else:
        response.update({"task_id": None, "status_url": None})
    return json_response(response)


def _crawling_message(links: list[str], existing_archives: int) -> str:
    if len(links) == 1:
        if existing_archives > 0:
            return "Archive exists, crawling to check for redownload: " + links[0]
        return "Crawling: " + links[0]
    if existing_archives == len(links):
        return f"Archive exists, crawling to check for redownload: {len(links)} links"
    if existing_archives > 0:
        return f"Crawling {len(links)} links ({existing_archives} archives exist, checking for redownload)"
    return f"Crawling: {len(links)} links"


def _webcrawler_links(args: dict[str, Any]) -> list[str]:
    links: list[str] = []

    def _add_link(lnk: Any) -> None:
        if isinstance(lnk, str):
            val = lnk.strip()
            if val and val not in links:
                links.append(val)

    if "links" in args:
        raw_links = args["links"]
        if isinstance(raw_links, (list, tuple, set)):
            for item in raw_links:
                _add_link(item)
        elif isinstance(raw_links, str):
            for line in raw_links.replace(",", "\n").splitlines():
                _add_link(line)

    if "link" in args:
        raw_link = args["link"]
        if isinstance(raw_link, (list, tuple, set)):
            for item in raw_link:
                _add_link(item)
        elif isinstance(raw_link, str):
            _add_link(raw_link)
    return links


def _operation_webcrawler(request: HttpRequest, data: dict[str, Any], args: Any, user: User) -> HttpResponse:
    if not isinstance(args, (dict, list, tuple)):
        raise ApiError.bad_request("Wrong format")
    if isinstance(args, (list, tuple)):
        args = {"links": list(args)}

    links = _webcrawler_links(args)
    if not links:
        raise ApiError.bad_request("No links provided")

    ignore_global_filters = bool(
        args.get("ignore_global_filters")
        or args.get("ignore-global-filters")
        or data.get("ignore_global_filters")
        or data.get("ignore-global-filters")
    )
    response: dict[str, Any] = {}
    task: TaskQueue | None = None

    if "downloader" in args:
        current_settings = Settings(load_from_config=crawler_settings.config)
        current_settings.allow_downloaders_only([args["downloader"]], True, True, True)
        parsers = current_settings.provider_context.get_parsers(current_settings)
        current_settings.archive_user = user
        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
        extra_args = []
        if ignore_global_filters:
            current_settings.ignore_global_filters = True
            extra_args.append("--ignore-global-filters")

        existing_archives = _find_existing_archives_count(parsers, links)
        task = client.enqueue(
            args=links + extra_args,
            task_type="web_crawler",
            override_options=current_settings,
            user=user,
        )
        response["message"] = _crawling_message(links, existing_archives)
        response["result"] = str(len(links))
        return _with_task(request, response, task)

    current_settings = Settings(load_from_config=crawler_settings.config)
    current_settings.archive_user = user
    current_settings.archive_origin = Archive.ORIGIN_ADD_URL
    extra_args = []
    reason = args.get("reason")
    if reason:
        extra_args.extend(["-reason", reason])
        current_settings.archive_reason = reason
    if ignore_global_filters:
        current_settings.ignore_global_filters = True
        extra_args.append("--ignore-global-filters")

    if args.get("parentLink"):
        parsers = current_settings.provider_context.get_parsers(current_settings)
        parent_archive = _find_archive_for_link(parsers, args["parentLink"])
        if parent_archive and parent_archive.gallery:
            parent_link = parent_archive.gallery.get_link()
            link_label = links[0] if len(links) == 1 else f"{len(links)} links"
            if args.get("action") == "replaceFound":
                # Preserve old archive extra data
                old_user_favorites = list(
                    UserArchivePrefs.objects.filter(archive=parent_archive).values("user", "favorite_group")
                )
                old_extracted = parent_archive.extracted

                parent_archive.gallery.mark_as_deleted()
                parent_archive.gallery = None
                parent_archive.delete_all_files()
                parent_archive.delete_files_but_archive()
                parent_archive.delete()
                response["message"] = f"Crawling: {link_label}, deleting parent: {parent_link}"
                response["result"] = str(len(links))

                current_settings.preserve_user_favorites = old_user_favorites
                current_settings.preserve_extracted = bool(old_extracted)

                task = client.enqueue(
                    args=links + extra_args,
                    task_type="web_crawler",
                    override_options=current_settings,
                    user=user,
                    reason=reason,
                )
            elif args.get("action") == "queueFound":
                response["message"] = f"Crawling: {link_label}, keeping parent: {parent_link}"
                response["result"] = str(len(links))
                task = client.enqueue(
                    args=links + extra_args,
                    task_type="web_crawler",
                    override_options=current_settings,
                    user=user,
                    reason=reason,
                )
            else:
                response["message"] = "Please confirm deletion of parent: " + parent_link
                response["action"] = "confirmDeletion"
            return _with_task(request, response, task)

    parsers = current_settings.provider_context.get_parsers(current_settings)
    existing_archives = _find_existing_archives_count(parsers, links)
    response["message"] = _crawling_message(links, existing_archives)
    response["result"] = str(len(links))

    task = client.enqueue(
        args=links + extra_args,
        task_type="web_crawler",
        override_options=current_settings,
        user=user,
        reason=reason,
    )
    return _with_task(request, response, task)


def _archive_from_args(args: Any) -> Archive:
    if not isinstance(args, dict) or "archive" not in args:
        raise ApiError.bad_request("Wrong format: args.archive is required.")
    try:
        return Archive.objects.get(pk=int(args["archive"]))
    except (ValueError, TypeError, Archive.DoesNotExist):
        raise ApiError.not_found("Archive does not exist.") from None


def _operation_match_archive(args: Any) -> HttpResponse:
    archive = _archive_from_args(args)
    if "match_filter" not in args:
        raise ApiError.bad_request("Wrong format: args.match_filter is required.")
    generate_possible_matches_for_archives(
        Archive.objects.filter(pk=archive.pk),
        filters=(args["match_filter"],),
        match_local=False,
        match_web=True,
        auto_link=bool(args.get("auto_link")),
    )
    return json_response({"message": "web matcher done, check the logs for results"})


def _operation_match_archive_internally(request: HttpRequest, args: Any) -> HttpResponse:
    archive = _archive_from_args(args)
    clear_title = "clear" in args
    provider_filter = args.get("provider", "")
    try:
        cutoff = float(request.GET.get("cutoff", "0.4"))
    except ValueError:
        cutoff = 0.4
    try:
        max_matches = int(request.GET.get("max-matches", "10"))
    except ValueError:
        max_matches = 10

    archive.generate_possible_matches(
        clear_title=clear_title,
        provider_filter=provider_filter,
        cutoff=cutoff,
        max_matches=max_matches,
        auto_link=bool(args.get("auto_link")),
    )
    return json_response({"message": "internal matcher done, check the archive for results"})


def handle_remote_post(request: HttpRequest, user: User) -> HttpResponse:
    if not request.body:
        raise ApiError.bad_request("Empty request", code="empty_body")
    try:
        data = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError.invalid_json() from None
    if not isinstance(data, dict) or "operation" not in data or "args" not in data:
        raise ApiError.bad_request("Wrong format: the body must be {\"operation\": ..., \"args\": ...}.")

    operation = data["operation"]
    args = data["args"]
    # Used by internal pages and userscript
    if operation == "webcrawler":
        return _operation_webcrawler(request, data, args, user)
    # Used by archive page
    if operation == "match_archive":
        return _operation_match_archive(args)
    if operation == "match_archive_internally":
        return _operation_match_archive_internally(request, args)
    raise ApiError.unknown_command(f"Unknown operation: {operation}")


def handle_remote_get(request: HttpRequest) -> HttpResponse:
    data = request.GET
    if "gc" not in data:
        raise ApiError.unknown_command("Unknown command")
    args = data.copy()

    for k in gallery_filter_keys:
        if k not in args:
            args[k] = ""

    for k in ("sort", "asc_desc"):
        if k not in args:
            args[k] = ""

    # Already authorized by api key.
    args["public"] = ""

    results = filter_galleries_no_request(args).prefetch_related("tags")
    return json_response(
        [
            {
                "gid": gallery.gid,
                "token": gallery.token,
                "title": gallery.title,
                "title_jpn": gallery.title_jpn,
                "category": gallery.category,
                "uploader": gallery.uploader,
                "comment": gallery.comment,
                "posted": int(timestamp_or_zero(gallery.posted)),
                "filecount": gallery.filecount,
                "filesize": gallery.filesize,
                "expunged": gallery.expunged,
                "disowned": gallery.disowned,
                "rating": gallery.rating,
                "hidden": gallery.hidden,
                "fjord": gallery.fjord,
                "public": gallery.public,
                "provider": gallery.provider,
                "dl_type": gallery.dl_type,
                "tags": gallery.tag_list(),
                "link": gallery.get_link(),
                "thumbnail": (
                    request.build_absolute_uri(reverse("viewer:gallery-thumb", args=(gallery.pk,)))
                    if gallery.thumbnail
                    else ""
                ),
                "thumbnail_url": gallery.thumbnail_url,
            }
            for gallery in results
        ],
        sort_keys=True,
    )
