import json
import logging
import re
from collections import defaultdict
from itertools import groupby
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import permission_required, login_required
from django.contrib.contenttypes.models import ContentType
from django.core import management
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Case, When, QuerySet, Q, F
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, Http404, QueryDict
from django.shortcuts import render
from django.urls import reverse
from django.utils.crypto import get_random_string

from core.base.setup import Settings
from core.base.utilities import thread_exists_in_db, clamp, get_schedulers_status
from viewer.management.commands import update_provider_wanted
from viewer.utils.functions import (
    archive_manage_results_to_json,
    galleries_update_metadata,
    gallery_search_results_to_json,
)
from viewer.utils.general import clean_up_referer
from viewer.utils.actions import event_log
from workers import client
from viewer.forms import (
    GallerySearchForm,
    ArchiveSearchForm,
    WantedGalleryCreateOrEditForm,
    ArchiveCreateForm,
    ArchiveGroupSelectForm,
    GalleryCreateForm,
    ArchiveManageEntrySimpleForm,
    WantedGalleryColSearchForm,
    ArchiveManageSearchSimpleForm,
    AllGalleriesSearchForm,
    EventSearchForm,
    GallerySearchSimpleForm,
    ArchiveExtraFileSimpleForm,
    WantedGalleryCommand,
    GallerySearchFields,
    DownloadHistoryForm,
    MonitoredLinkCreateOrEditForm,
    MonitoredLinkSearchForm,
)
from viewer.models import (
    Archive,
    Gallery,
    EventLog,
    ArchiveMatches,
    Tag,
    WantedGallery,
    ArchiveGroup,
    ArchiveGroupEntry,
    GallerySubmitEntry,
    MonitoredLink,
    ArchiveRecycleEntry,
    ArchiveTag,
    UserLongLivedToken,
    DownloadEvent,
    Category,
    Provider,
    GalleryMatchGroup,
    GalleryMatchGroupEntry,
    GalleryGroupPossibleMatches, ArchiveManageEntry, FoundGallery
)
from viewer.utils.requests import double_check_auth
from viewer.utils.types import AuthenticatedHttpRequest
from viewer.views.head import (
    gallery_filter_keys,
    filter_galleries_simple,
    archive_filter_keys,
    filter_archives_simple,
    render_error,
    wanted_gallery_filter_keys,
    filter_wanted_galleries_simple,
    gallery_list_filters_keys,
)

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)



@permission_required("viewer.view_submitted_gallery")
def submit_queue(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    user_reason = p.get("reason", "")
    entry_reason = p.get("entry_reason", "")
    entry_comment = p.get("entry_comment", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = GallerySearchForm()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-") and isinstance(v, str):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        gallery_entries = GallerySubmitEntry.objects.filter(id__in=pks).order_by(preserved)

        if "deny_galleries" in p and request.user.has_perm("viewer.approve_gallery"):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery:
                    message = "Denying gallery: {}, link: {}, source link: {}".format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if entry_reason != "":
                        message += ", reason: {}".format(entry_reason)
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)
                    gallery.mark_as_denied()
                    gallery_entry.mark_as_denied(reason=entry_reason, comment=entry_comment)
                    event_log(
                        request.user, "DENY_GALLERY", reason=entry_reason, content_object=gallery, result="denied"
                    )
                else:
                    message = "Denying URL: {}, ".format(gallery_entry.submit_url)
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_denied(reason=entry_reason, comment=entry_comment)

                    event_log(
                        request.user, "DENY_URL", reason=entry_reason, content_object=gallery_entry, result="denied"
                    )
        elif "publish_galleries" in p and request.user.has_perm("viewer.publish_gallery"):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery and not gallery.public:
                    message = "Publishing gallery and related archives: {}, link: {}, source link: {}".format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)
                    gallery.set_public()
                    event_log(
                        request.user, "PUBLISH_GALLERY", reason=entry_reason, content_object=gallery, result="success"
                    )
                    if request.user.has_perm("viewer.publish_archive"):
                        for archive in gallery.archive_set.all():
                            if not archive.public:
                                archive.set_public()
                                event_log(
                                    request.user,
                                    "PUBLISH_ARCHIVE",
                                    reason=entry_reason,
                                    content_object=archive,
                                    result="success",
                                )
        elif "approve_galleries" in p and request.user.has_perm("viewer.approve_gallery"):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery:
                    message = "Approving gallery: {}, link: {}, source link: {}".format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    if user_reason:
                        gallery.reason = user_reason
                        gallery.save()
                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    event_log(
                        request.user, "APPROVE_GALLERY", reason=entry_reason, content_object=gallery, result="accepted"
                    )
                else:
                    message = "Approving URL: {}, ".format(gallery_entry.submit_url)
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    event_log(
                        request.user,
                        "APPROVE_URL",
                        reason=entry_reason,
                        content_object=gallery_entry,
                        result="accepted",
                    )

        elif "download_galleries" in p and request.user.has_perm("viewer.approve_gallery"):
            for gallery_entry in gallery_entries:
                gallery = gallery_entry.gallery
                if gallery:
                    message = "Queueing gallery: {}, link: {}, source link: {}".format(
                        gallery.title, gallery.get_absolute_url(), gallery.get_link()
                    )
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    # Force replace_metadata when queueing from this list, since it's mostly used to download non used.
                    current_settings = Settings(load_from_config=crawler_settings.config)

                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True
                    if request.user.is_authenticated:
                        current_settings.archive_user = request.user
                    current_settings.archive_origin = Archive.ORIGIN_ACCEPT_SUBMITTED

                    if "reason" in p and p["reason"] != "":
                        reason = p["reason"]
                        # Force limit string length (reason field max_length)
                        current_settings.archive_reason = reason[:200]
                        current_settings.archive_details = gallery.reason or ""
                        current_settings.gallery_reason = reason[:200]
                    elif gallery.reason:
                        current_settings.archive_reason = gallery.reason

                    current_settings.archive_user = request.user
                    current_settings.event_action = "ADD"

                    client.enqueue(
                        args=[gallery.get_link()],
                        task_type="web_crawler",
                        override_options=current_settings,
                        user=request.user,
                        reason=user_reason,
                        event_action="ADD",
                    )

                    event_log(
                        request.user, "ACCEPT_GALLERY", reason=entry_reason, content_object=gallery, result="accepted"
                    )

                elif gallery_entry.submit_url:
                    message = "Queueing URL: {}".format(gallery_entry.submit_url)
                    if "reason" in p and p["reason"] != "":
                        message += ", reason: {}".format(p["reason"])
                    logger.info("User {}: {}".format(request.user.username, message))
                    messages.success(request, message)

                    gallery_entry.mark_as_approved(reason=entry_reason, comment=entry_comment)

                    current_settings = Settings(load_from_config=crawler_settings.config)

                    current_settings.replace_metadata = True
                    current_settings.retry_failed = True

                    if "reason" in p and p["reason"] != "":
                        reason = p["reason"]
                        # Force limit string length (reason field max_length)
                        current_settings.archive_reason = reason[:200]
                        current_settings.gallery_reason = reason[:200]

                    current_settings.archive_user = request.user
                    current_settings.event_action = "ADD"

                    client.enqueue(
                        args=[gallery_entry.submit_url],
                        task_type="web_crawler",
                        override_options=current_settings,
                        user=request.user,
                        reason=user_reason,
                        event_action="ADD",
                    )

                    event_log(
                        request.user,
                        "ACCEPT_URL",
                        reason=entry_reason,
                        content_object=gallery_entry,
                        result="accepted",
                    )

    providers = Gallery.objects.all().values_list("provider", flat=True).distinct()

    submit_entries: QuerySet[GallerySubmitEntry] = (
        GallerySubmitEntry.objects.all().prefetch_related("gallery", "similar_galleries").order_by("-submit_date")
    )

    if "filter_galleries" in get:
        params: dict[str, str] = {}

        for k, v in get.items():
            if isinstance(v, str):
                params[k] = v

        for k in gallery_filter_keys:
            if k not in params:
                params[k] = ""

        gallery_results = filter_galleries_simple(params)

        submit_entries = submit_entries.filter(gallery__in=gallery_results)
    
    if "hide_submitted" in get:
        allowed_resolved_status = []
    else:
        allowed_resolved_status = [GallerySubmitEntry.RESOLVED_SUBMITTED]

    if "denied" in get:
        allowed_resolved_status.append(GallerySubmitEntry.RESOLVED_DENIED)
    if "approved" in get:
        allowed_resolved_status.append(GallerySubmitEntry.RESOLVED_APPROVED)
    if "already_present" in get:
        allowed_resolved_status.append(GallerySubmitEntry.RESOLVED_ALREADY_PRESENT)

    submit_entries = submit_entries.filter(resolved_status__in=allowed_resolved_status)

    if "submit_reason" in get:
        submit_entries = submit_entries.filter(submit_reason__icontains=get["submit_reason"])

    if "submit_group" in get:
        submit_entries = submit_entries.filter(submit_group__icontains=get["submit_group"])

    if "has_similar" in get:
        submit_entries = submit_entries.annotate(similar_count=Count("similar_galleries")).filter(similar_count__gt=0)

    submit_entries = submit_entries.annotate(archive_count=Count("gallery__archive"))

    if "has_archives" in get:
        submit_entries = submit_entries.filter(archive_count__gt=0)

    if "gallery-public" in get:
        submit_entries = submit_entries.filter(gallery__public=True)

    if "gallery-private" in get:
        submit_entries = submit_entries.filter(gallery__public=False)

    if "submitted_from" in get and get["submitted_from"]:
        submit_entries = submit_entries.filter(submit_date__gte=get["submitted_from"])

    if "submitted_to" in get and get["submitted_to"]:
        submit_entries = submit_entries.filter(submit_date__lte=get["submitted_to"])

    submit_entries = submit_entries.annotate(
        archives_recycled=Count("gallery__archive", filter=Q(gallery__archive__binned=True))
    )

    paginator = Paginator(submit_entries, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "providers": providers, "form": form}
    return render(request, "viewer/collaborators/submit_queue.html", d)



@permission_required("viewer.crawler_adder")
def user_crawler(request: AuthenticatedHttpRequest) -> HttpResponse:
    """Crawl given URLs."""

    d = {}

    p = request.POST

    all_downloaders = crawler_settings.provider_context.get_downloaders_name_priority(
        crawler_settings, filter_type="generic"
    )

    # providers_not_generic = list(set([x[0].provider for x in all_downloaders if not x[0].provider.is_generic()]))
    generic_downloaders = [x[0] for x in all_downloaders]

    user_reason = p.get("reason", "")

    if p:
        current_settings = Settings(load_from_config=crawler_settings.config)
        url_set = set()
        # create a dictionary of properties for each archive
        current_settings.replace_metadata = False
        current_settings.config["allowed"]["replace_metadata"] = "no"
        # Allow collaborators to readd a gallery if it failed.
        current_settings.retry_failed = True
        current_settings.config["allowed"]["retry_failed"] = "yes"
        for k, v in p.items():
            if isinstance(v, str):
                if k == "downloader":
                    if v == "no-generic":
                        continue
                    elif v in generic_downloaders:
                        current_settings.enable_downloader_only(v)
                elif k == "urls":
                    url_list = v.split("\n")
                    for item in url_list:
                        url_set.add(item.rstrip("\r"))
        urls = list(url_set)

        if not urls:
            messages.error(request, "Submission is empty.")
            return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        if "reason" in p and p["reason"] != "":
            reason = p["reason"]
            # Force limit string length (reason field max_length)
            current_settings.archive_reason = reason[:200]
            current_settings.gallery_reason = reason[:200]
        if "source" in p and p["source"] != "":
            source = p["source"]
            # Force limit string length (reason field max_length)
            current_settings.archive_source = source[:50]

        add_as_deleted = "as-deleted" in p and p["as-deleted"] == "1"

        skip_non_current = "skip-non-current" in p and p["skip-non-current"] == "1"
        process_parents_first = "process-parents-first" in p and p["process-parents-first"] == "1"

        if add_as_deleted and request.user.has_perm("viewer.add_deleted_gallery"):
            # Use this to download using info
            current_settings.allow_type_downloaders_only("info")
            current_settings.event_action = "ADD_DELETED_GALLERY"
            current_settings.non_current_links_as_deleted = True
        else:
            current_settings.event_action = "ADD_GALLERY"

        current_settings.archive_user = request.user
        current_settings.archive_origin = Archive.ORIGIN_ADD_URL

        if skip_non_current:
            current_settings.non_current_links_as_deleted = True

        if process_parents_first:
            if 'panda' in current_settings.providers:
                current_settings.providers["panda"].auto_process_parent = True
                current_settings.providers["panda"].auto_process_first = True

        parsers = crawler_settings.provider_context.get_parsers(crawler_settings)

        current_settings.archive_user = request.user
        current_settings.event_action = "ADD"

        client.enqueue(
            args=urls,
            task_type="web_crawler",
            override_options=current_settings,
            user=request.user,
            reason=user_reason,
            event_action="ADD",
        )

        messages.success(
            request,
            "Starting Crawler, if the links were correctly added, they should appear on the archive or gallery list.",
        )
        for url in urls:
            logger.info("User {}: queued link: {}".format(request.user.username, url))
            # event_log(
            #     request.user,
            #     'CRAWL_URL',
            #     reason=user_reason,
            #     data=url,
            #     result='queue'
            # )

        found_valid_urls: list[str] = []

        for parser in parsers:
            if parser.id_from_url_implemented():
                urls_filtered = parser.filter_accepted_urls(urls)
                found_valid_urls.extend(urls_filtered)
                for url_filtered in urls_filtered:
                    gid = parser.id_from_url(url_filtered)
                    gallery = Gallery.objects.filter(gid=gid, provider=parser.name).first()
                    found_states = []
                    if not gallery:
                        if add_as_deleted:
                            messages.warning(request, "{}: New URL, will be added as deleted".format(url_filtered))
                        else:
                            messages.success(request, "{}: New URL, will be processed".format(url_filtered))
                        event_log(request.user, "CRAWL_URL", reason=user_reason, data=url_filtered, result="queued")
                        continue

                    if gallery.is_submitted():
                        found_states.append("submitted")
                    if gallery.is_deleted():
                        found_states.append("deleted")
                    if gallery.is_denied():
                        found_states.append("denied")
                    if gallery.public:
                        found_states.append("public")
                    else:
                        found_states.append("private")
                    if "failed" in gallery.dl_type:
                        found_states.append("failed_download")

                    messages.info(
                        request,
                        "{}: Gallery present: {}, found states: [{}], reason: {}".format(
                            url_filtered,
                            request.build_absolute_uri(gallery.get_absolute_url()),
                            ",".join(found_states),
                            gallery.reason,
                        ),
                    )

                    if gallery.is_submitted():
                        event_log(
                            request.user, "CRAWL_URL", reason=user_reason, data=url_filtered, result="already_submitted"
                        )
                    elif gallery.public:
                        event_log(
                            request.user, "CRAWL_URL", reason=user_reason, data=url_filtered, result="already_public"
                        )
                    elif gallery.is_deleted():
                        event_log(
                            request.user, "CRAWL_URL", reason=user_reason, data=url_filtered, result="already_deleted"
                        )
                    elif gallery.is_denied():
                        event_log(
                            request.user, "CRAWL_URL", reason=user_reason, data=url_filtered, result="already_denied"
                        )
                    else:
                        event_log(
                            request.user, "CRAWL_URL", reason=user_reason, data=url_filtered, result="already_private"
                        )

        extra_urls = [x for x in urls if x not in found_valid_urls]

        for extra_url in extra_urls:
            messages.info(request, "{}: Extra non-provider URLs".format(extra_url))
            event_log(request.user, "CRAWL_URL", reason=user_reason, data=extra_url, result="queued")
        # Not really optimal when there's many commands being queued
        # for command in url_list:
        #     messages.success(request, command)
        return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

    d.update({"downloaders": generic_downloaders})

    return render(request, "viewer/collaborators/gallery_crawler.html", d)


@permission_required("viewer.match_archive")


@permission_required("viewer.upload_with_metadata_archive")
def upload_archive(request: HttpRequest) -> HttpResponse:

    if request.POST.get("submit-archive"):
        # create a form instance and populate it with data from the request:
        edit_form = ArchiveCreateForm(request.POST, request.FILES)
        # check whether it's valid:
        if edit_form.is_valid():
            new_archive = edit_form.save(commit=False)
            if request.user.is_authenticated:
                new_archive.user = request.user
            new_archive.origin = Archive.ORIGIN_UPLOAD_ARCHIVE
            new_archive = edit_form.save()
            if crawler_settings.mark_similar_new_archives:
                new_archive.create_marks_for_similar_archives()
            message = "Archive successfully uploaded: {}"
            messages.success(request, message.format(request.build_absolute_uri(new_archive.get_absolute_url())))
            logger.info("User {}: {}".format(request.user.username, message.format(new_archive.get_absolute_url())))
            event_log(request.user, "ADD_ARCHIVE", content_object=new_archive, result="added")
            # return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")
            # return HttpResponseRedirect(clean_up_referer(request.META["HTTP_REFERER"]))

        return render(request, "viewer/include/messages.html")

    else:
        if "gallery" in request.GET:
            try:
                gallery_id = int(request.GET["gallery"])
                try:
                    gallery: Gallery | None = Gallery.objects.get(pk=gallery_id)
                except Gallery.DoesNotExist:
                    gallery = None
            except ValueError:
                gallery = None
        else:
            gallery = None

        if gallery:
            edit_form = ArchiveCreateForm(
                initial={"gallery": gallery, "reason": gallery.reason, "source_type": gallery.provider}
            )
        else:
            edit_form = ArchiveCreateForm()

    d = {"edit_form": edit_form}
    return render(request, "viewer/collaborators/add_archive.html", d)


# This is an alternative way to add Galleries; the preferred way is to crawl a link.
# Mostly used in scenarios when the metadata is already fetched on another instance or backup.
# The problem is that it only accepts already created tags, magazine, gallery_container
@permission_required("viewer.add_gallery")
def upload_gallery(request: HttpRequest) -> HttpResponse:
    if request.POST.get("submit-gallery"):
        # create a form instance and populate it with data from the request:
        p_local = request.POST.copy()
        del p_local["tags"]
        tags = request.POST.get("tags")
        tag_objects = []
        if tags:
            tag_list = tags.split(",")
            for tag in tag_list:
                tag = tag.strip().replace(" ", "_")

                scope_name = tag.split(":", maxsplit=1)
                if len(scope_name) > 1:
                    tag_name = scope_name[1]
                    tag_scope = scope_name[0]
                else:
                    tag_name = tag
                    tag_scope = ""

                actual_tag = Tag.objects.get_or_create(name=tag_name, scope=tag_scope, defaults={"source": "user"})[0]
                tag_objects.append(actual_tag)
        edit_form = GalleryCreateForm(p_local, request.FILES)
        # check whether it's valid:
        if edit_form.is_valid():
            new_gallery = edit_form.save(commit=False)
            parsers = crawler_settings.provider_context.get_parsers(crawler_settings)
            # for parser in parsers:
            # urls = parser.filter_accepted_urls(list(to_use_urls))

            continue_processing = True

            if 'url' in p_local:
                for parser in parsers:
                    if parser.id_from_url_implemented():
                        urls_filtered = parser.filter_accepted_urls([str(p_local["url"])])
                        for url_filtered in urls_filtered:
                            gallery_gid = parser.id_from_url(url_filtered)
                            if gallery_gid:
                                gallery_exists = Gallery.objects.filter(
                                    gid=gallery_gid, provider=parser.name
                                ).first()
                                if gallery_exists:
                                    continue_processing = False
                                    messages.error(request, "The provided data maps to an already existing Gallery: {}".format(gallery_exists.get_absolute_url()), extra_tags="danger")
                                else:
                                    new_gallery.gid = gallery_gid
                                    new_gallery.provider = parser.name
            if continue_processing:
                if not new_gallery.gid or not new_gallery.provider:
                    messages.error(request, "The provided data is missing the GID and Provider fields that are mandatory", extra_tags="danger")
                else:
                    new_gallery.dl_type = "info"
                    if not new_gallery.uploader:
                        new_gallery.uploader = ""
                    new_gallery.save()
                    new_gallery.tags.set(tag_objects)
                    new_gallery.schedule_thumbnail_fetch()
                    # edit_form.save_m2m()
                    message = "Gallery successfully created: {}".format(new_gallery.get_absolute_url())
                    messages.success(request, message)
                    logger.info("User {}: {}".format(request.user.username, message))
                    event_log(request.user, "CREATE_GALLERY", content_object=new_gallery, result="added")
        else:
            messages.error(request, "The provided data is not valid", extra_tags="danger")
    else:
        if "gallery" in request.GET:
            try:
                gallery_id = int(request.GET["gallery"])
                try:
                    gallery: Gallery | None = Gallery.objects.get(pk=gallery_id)
                except Gallery.DoesNotExist:
                    gallery = None
            except ValueError:
                gallery = None
        else:
            gallery = None

        if gallery:
            edit_form = GalleryCreateForm(instance=gallery)
        else:
            edit_form = GalleryCreateForm()

    d = {"edit_form": edit_form}
    return render(request, "viewer/collaborators/add_gallery.html", d)


