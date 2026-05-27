"""URL-facing JSON API views (login, public /api, private jsonapi)."""

import json
import logging
from collections import defaultdict
from typing import Any, Optional

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.http import (
    HttpResponse,
    HttpRequest,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt

from core.base.setup import Settings
from core.base.utilities import timestamp_or_zero
from viewer.models import Archive, Gallery, UserArchivePrefs
from viewer.utils.matching import generate_possible_matches_for_archives
from viewer.utils.requests import authenticate_by_token, double_check_auth
from viewer.views.head import gallery_filter_keys
from viewer.views.api.dispatch import (
    json_api_handle_delete,
    json_api_handle_get,
    json_api_handle_post,
    json_api_handle_put,
)
from viewer.views.api.filters import filter_galleries_no_request

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)

def api_login(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        if not username or not password:
            return HttpResponse(
                json.dumps({"success": False, "message": "Username or password is empty."}),
                content_type="application/json",
            )
        user = authenticate(username=username, password=password)
        if user is not None:
            if user.is_active:
                login(request, user)
                data: dict[str, Any] = {"success": True}
            else:
                data = {"success": False, "message": "This account has been disabled."}
        else:
            data = {"success": False, "message": "Invalid login credentials."}

        return HttpResponse(json.dumps(data), content_type="application/json")

    return HttpResponseBadRequest()


@csrf_exempt
def api_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    data = {"success": True, "message": "Logged out."}
    return HttpResponse(json.dumps(data), content_type="application/json")



# NOTE: This is used by 3rd parties, do not modify, at most create a new function if something needs changing
# Public API, does not check for any token, but filters if the user is authenticated or not.
@csrf_exempt
def json_api(request: HttpRequest) -> HttpResponse:

    token_valid, token_user = authenticate_by_token(request)

    user_is_authenticated = request.user.is_authenticated or token_valid

    if request.method == "GET":
        return json_api_handle_get(request, user_is_authenticated)
    elif request.method == "POST":
        if user_is_authenticated:
            return json_api_handle_post(request, token_user or request.user)
        else:
            return HttpResponseForbidden(
                json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
            )
    elif request.method == "PUT":
        if user_is_authenticated:
            return json_api_handle_put(request, token_user or request.user)
        else:
            return HttpResponseForbidden(
                json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
            )
    elif request.method == "DELETE":
        if user_is_authenticated:
            return json_api_handle_delete(request, token_user or request.user)
        else:
            return HttpResponseForbidden(
                json.dumps({"result": "Not authorized"}), content_type="application/json; charset=utf-8"
            )
    else:
        return HttpResponseNotAllowed(
            ["GET", "PUT", "DELETE"],
            json.dumps({"result": "Unsupported request method"}),
            content_type="application/json; charset=utf-8",
        )



# Private API, checks for the API key.
@csrf_exempt
def json_parser(request: HttpRequest) -> HttpResponse:
    response = {}

    authenticated, actual_user = double_check_auth(request)

    if not authenticated or not actual_user or not actual_user.has_perm("viewer.use_remote_api"):
        response["error"] = "Not authorized"
        return HttpResponse(json.dumps(response), status=401, content_type="application/json; charset=utf-8")

    if request.method == "POST":
        if not request.body:
            response["error"] = "Empty request"
            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
        data = json.loads(request.body.decode("utf-8"))

        if "operation" not in data or "args" not in data:
            response["error"] = "Wrong format"
        else:
            args = data["args"]
            response = {}
            # Used by internal pages and userscript
            if data["operation"] == "webcrawler" and "link" in args:
                if not crawler_settings.workers.web_queue:
                    response["error"] = "The webqueue is not running"
                elif "downloader" in args:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    if not current_settings.workers.web_queue:
                        response["error"] = "The webqueue is not running"
                    else:
                        current_settings.allow_downloaders_only([args["downloader"]], True, True, True)
                        archive = None
                        parsers = current_settings.provider_context.get_parsers(current_settings)
                        current_settings.archive_user = actual_user
                        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                        for parser in parsers:
                            if parser.id_from_url_implemented():
                                urls_filtered = parser.filter_accepted_urls((args["link"],))
                                for url_filtered in urls_filtered:
                                    gallery_gid = parser.id_from_url(url_filtered)
                                    if gallery_gid:
                                        archive = Archive.objects.filter(
                                            gallery__gid=gallery_gid, gallery__provider=parser.name
                                        ).first()
                                if urls_filtered:
                                    break
                        current_settings.workers.web_queue.enqueue_args_list(
                            (args["link"],), override_options=current_settings
                        )
                        if archive:
                            response["message"] = "Archive exists, crawling to check for redownload: " + args["link"]
                        else:
                            response["message"] = "Crawling: " + args["link"]
                else:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    current_settings.archive_user = actual_user
                    current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                    extra_args = []
                    if "reason" in args and args["reason"]:
                        extra_args.extend(["-reason", args["reason"]])

                    if "parentLink" in args:
                        parent_archive = None
                        if not current_settings.workers.web_queue:
                            response["error"] = "The webqueue is not running"
                            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
                        parsers = current_settings.provider_context.get_parsers(current_settings)
                        for parser in parsers:
                            if parser.id_from_url_implemented():
                                urls_filtered = parser.filter_accepted_urls((args["parentLink"],))
                                for url_filtered in urls_filtered:
                                    gallery_gid = parser.id_from_url(url_filtered)
                                    if gallery_gid:
                                        parent_archive = Archive.objects.filter(
                                            gallery__gid=gallery_gid, gallery__provider=parser.name
                                        ).first()
                                if urls_filtered:
                                    break
                        if parent_archive and parent_archive.gallery:
                            link = parent_archive.gallery.get_link()
                            if "action" in args and args["action"] == "replaceFound":
                                # Preserve old archive extra data
                                old_user_favorites = UserArchivePrefs.objects.filter(archive=parent_archive).values(
                                    "user", "favorite_group"
                                )
                                old_extracted = parent_archive.extracted

                                parent_archive.gallery.mark_as_deleted()
                                parent_archive.gallery = None
                                parent_archive.delete_all_files()
                                parent_archive.delete_files_but_archive()
                                parent_archive.delete()
                                response["message"] = "Crawling: " + args["link"] + ", deleting parent: " + link

                                def archive_callback(
                                    x: Optional["Archive"], crawled_url: Optional[str], result: str
                                ) -> None:

                                    if x:
                                        logger.info(
                                            "Preserving old extra info for archive: {}".format(x.get_absolute_url())
                                        )
                                        for old_user_favorite in old_user_favorites:
                                            UserArchivePrefs.objects.get_or_create(
                                                archive=x,
                                                user=old_user_favorite["user"],
                                                favorite_group=old_user_favorite["favorite_group"],
                                            )

                                        if old_extracted and not x.extracted and x.crc32:
                                            x.extract()

                                current_settings.workers.web_queue.enqueue_args_list(
                                    [args["link"]] + extra_args, archive_callback=archive_callback
                                )
                            elif "action" in args and args["action"] == "queueFound":
                                response["message"] = "Crawling: " + args["link"] + ", keeping parent: " + link
                                current_settings.workers.web_queue.enqueue_args_list(
                                    [args["link"]] + extra_args, override_options=current_settings
                                )
                            else:
                                response["message"] = "Please confirm deletion of parent: " + link
                                response["action"] = "confirmDeletion"
                        else:
                            archive = None
                            parsers = current_settings.provider_context.get_parsers(current_settings)
                            for parser in parsers:
                                if parser.id_from_url_implemented():
                                    urls_filtered = parser.filter_accepted_urls((args["link"],))
                                    for url_filtered in urls_filtered:
                                        gallery_gid = parser.id_from_url(url_filtered)
                                        if gallery_gid:
                                            archive = Archive.objects.filter(
                                                gallery__gid=gallery_gid, gallery__provider=parser.name
                                            ).first()
                                    if urls_filtered:
                                        break
                            if archive:
                                response["message"] = (
                                    "Archive exists, crawling to check for redownload: " + args["link"]
                                )
                            else:
                                response["message"] = "Crawling: " + args["link"]

                            current_settings.workers.web_queue.enqueue_args_list(
                                [args["link"]] + extra_args, override_options=current_settings
                            )
                    else:
                        archive = None
                        if not current_settings.workers.web_queue:
                            response["error"] = "The webqueue is not running"
                            return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
                        parsers = current_settings.provider_context.get_parsers(current_settings)
                        for parser in parsers:
                            if parser.id_from_url_implemented():
                                urls_filtered = parser.filter_accepted_urls((args["link"],))
                                for url_filtered in urls_filtered:
                                    gallery_gid = parser.id_from_url(url_filtered)
                                    if gallery_gid:
                                        archive = Archive.objects.filter(
                                            gallery__gid=gallery_gid, gallery__provider=parser.name
                                        ).first()
                                if urls_filtered:
                                    break
                        if archive:
                            response["message"] = "Archive exists, crawling to check for redownload: " + args["link"]
                        else:
                            response["message"] = "Crawling: " + args["link"]
                        current_settings.workers.web_queue.enqueue_args_list(
                            [args["link"]] + extra_args, override_options=current_settings
                        )
                if not response:
                    response["error"] = "Could not parse request"
                return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
            # Used by remotesite command
            elif data["operation"] == "archive_request":
                provider_dict: dict[str, list[str]] = defaultdict(list)
                for gid_provider in args:
                    provider_dict[gid_provider[1]].append(gid_provider[0])
                gallery_ids: list[int] = []
                for provider, gid_list in provider_dict.items():
                    pks = Gallery.objects.filter(provider=provider, gid__in=gid_list).values_list("pk", flat=True)
                    gallery_ids.extend(pks)
                archives_query = Archive.objects.filter_non_existent(
                    crawler_settings.MEDIA_ROOT, gallery__pk__in=gallery_ids
                )
                archives = [
                    {
                        "gid": archive.gallery.gid,
                        "provider": archive.gallery.provider,
                        "id": archive.id,
                        "zipped": archive.zipped.name,
                        "filesize": archive.filesize,
                    }
                    for archive in archives_query
                    if archive.gallery
                ]
                response_text = json.dumps({"result": archives})
                return HttpResponse(response_text, content_type="application/json; charset=utf-8")
            elif data["operation"] == "force_queue_archives":
                pages_links = args
                if len(pages_links) > 0:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    if "archive_reason" in data:
                        current_settings.archive_reason = data["archive_reason"]
                    if "archive_details" in data:
                        current_settings.archive_details = data["archive_details"]
                    current_settings.allow_type_downloaders_only("fake")
                    current_settings.set_enable_download()
                    if current_settings.workers.web_queue:
                        current_settings.archive_user = actual_user
                        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                        current_settings.workers.web_queue.enqueue_args_list(
                            pages_links, override_options=current_settings
                        )
                    else:
                        pages_links = []
                return HttpResponse(
                    json.dumps({"result": str(len(pages_links))}), content_type="application/json; charset=utf-8"
                )

            # Used by remotesite command
            elif data["operation"] in ("queue_archives", "queue_galleries"):
                urls = args
                new_urls_set = set()
                gids_set = set()

                parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
                for parser in parsers:
                    if parser.id_from_url_implemented():
                        urls_filtered = parser.filter_accepted_urls(urls)
                        for url in urls_filtered:
                            gid = parser.id_from_url(url)
                            gids_set.add(gid)

                gids_list = list(gids_set)

                existing_galleries = Gallery.objects.filter(gid__in=gids_list).exclude(
                    status=Gallery.StatusChoices.DELETED
                )
                for gallery_object in existing_galleries:
                    if gallery_object.is_submitted():
                        gallery_object.delete()
                    # Delete queue galleries that failed, and does not have archives.
                    elif (
                        data["operation"] == "queue_archives"
                        and "failed" in gallery_object.dl_type
                        and not gallery_object.archive_set.all()
                    ):
                        gallery_object.delete()
                    elif data["operation"] == "queue_archives" and not gallery_object.archive_set.all():
                        gallery_object.delete()
                already_present_gids = list(Gallery.objects.filter(gid__in=gids_list).values_list("gid", flat=True))
                # new_gids = list(gids_set - set(already_present_gids))

                for parser in parsers:
                    if parser.id_from_url_implemented():
                        urls_filtered = parser.filter_accepted_urls(urls)
                        for url in urls_filtered:
                            gid = parser.id_from_url(url)
                            if gid not in already_present_gids:
                                new_urls_set.add(url)

                pages_links = list(new_urls_set)
                if len(pages_links) > 0:
                    current_settings = Settings(load_from_config=crawler_settings.config)
                    if data["operation"] == "queue_galleries":
                        current_settings.allow_type_downloaders_only("info")
                    elif data["operation"] == "queue_archives":
                        if "archive_reason" in data:
                            current_settings.archive_reason = data["archive_reason"]
                        if "archive_details" in data:
                            current_settings.archive_details = data["archive_details"]
                        current_settings.allow_type_downloaders_only("fake")
                    if current_settings.workers.web_queue:
                        current_settings.archive_user = actual_user
                        current_settings.archive_origin = Archive.ORIGIN_ADD_URL
                        current_settings.workers.web_queue.enqueue_args_list(
                            pages_links, override_options=current_settings
                        )
                    else:
                        pages_links = []
                return HttpResponse(
                    json.dumps({"result": str(len(pages_links))}), content_type="application/json; charset=utf-8"
                )
            # Used by remotesite command
            elif data["operation"] == "links":
                links = args
                if len(links) > 0 and crawler_settings.workers.web_queue:
                    crawler_settings.workers.web_queue.enqueue_args_list(links)
                return HttpResponse(
                    json.dumps({"result": str(len(links))}), content_type="application/json; charset=utf-8"
                )
            # Used by archive page
            elif data["operation"] == "match_archive":
                archive_obj = Archive.objects.filter(pk=args["archive"])
                if archive_obj:
                    generate_possible_matches_for_archives(
                        archive_obj,
                        filters=(args["match_filter"],),
                        match_local=False,
                        match_web=True,
                    )
                return HttpResponse(
                    json.dumps({"message": "web matcher done, check the logs for results"}),
                    content_type="application/json; charset=utf-8",
                )
            elif data["operation"] == "match_archive_internally":
                archive = Archive.objects.get(pk=args["archive"])
                if archive:
                    clear_title = True if "clear" in args else False
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
                        clear_title=clear_title, provider_filter=provider_filter, cutoff=cutoff, max_matches=max_matches
                    )
                    archive.save()
                return HttpResponse(
                    json.dumps({"message": "internal matcher done, check the archive for results"}),
                    content_type="application/json; charset=utf-8",
                )
            else:
                response["error"] = "Unknown function"
    elif request.method == "GET":
        data = request.GET
        if "gc" in data:
            args = data.copy()

            for k in gallery_filter_keys:
                if k not in args:
                    args[k] = ""

            keys = ("sort", "asc_desc")

            for k in keys:
                if k not in args:
                    args[k] = ""

            # args = data
            # Already authorized by api key.
            args["public"] = ""

            results = filter_galleries_no_request(args).prefetch_related("tags")
            if not results:
                return HttpResponse(json.dumps([]), content_type="application/json; charset=utf-8")
            response_text = json.dumps(
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
                # indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            return HttpResponse(response_text, content_type="application/json; charset=utf-8")
        else:
            response["error"] = "Unknown function"
    else:
        response["error"] = "Unsupported method: {}".format(request.method)
    return HttpResponse(json.dumps(response), content_type="application/json; charset=utf-8")
