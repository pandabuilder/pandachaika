import datetime
import logging
from itertools import groupby

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Q, Case, When, F, Exists, OuterRef
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.conf import settings
import django.utils.timezone as django_tz

from core.base.utilities import thread_exists_in_db, clamp
from viewer.forms import (
    GallerySearchForm,
    ArchiveSearchForm,
    WantedGallerySearchForm,
    GallerySearchFields,
    WantedGalleryCreateOrEditForm,
)
from viewer.models import (
    Archive,
    Gallery,
    ArchiveMatches,
    Tag,
    WantedGallery,
    GalleryMatch,
    FoundGallery,
    ArchiveTag,
)
from viewer.utils.actions import event_log
from viewer.utils.general import PAGE_SIZE_OPTIONS, page_size, safe_redirect_back
from workers import client
from viewer.views.head import (
    gallery_filter_keys,
    filter_galleries_simple,
    archive_filter_keys,
    filter_archives_simple,
    wanted_gallery_filter_keys,
    filter_wanted_galleries_simple,
    gallery_list_filters_keys,
)

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


def _delete_selected_archives(request: HttpRequest, delete_files: bool) -> None:
    """Queue deletion of the archives checked as ``del-<pk>``, with or without their files."""
    p = request.POST
    pks: list[str] = []
    for k in p.keys():
        if k.startswith("del-"):
            pks.extend(p.getlist(k))
    if not pks:
        messages.warning(request, "No archives were selected.")
        return
    archive_pks = list(Archive.objects.filter(id__in=pks).order_by("-pk").values_list("pk", flat=True))
    if not archive_pks:
        messages.warning(request, "No archives were selected.")
        return
    client.enqueue(
        args=archive_pks,
        task_type="delete_archive_worker",
        override_options={
            "delete_zipped": False,
            "delete_archive": True,
            "delete_file": delete_files,
            "delete_gallery": False,
            "mark_gallery_deleted": False,
            "mark_gallery_denied": False,
        },
        user=request.user,
        event_action="DELETE_ARCHIVE",
    )
    message = "Queued deleting {} archive(s) ({})".format(
        len(archive_pks), "deleting files" if delete_files else "keeping files"
    )
    logger.info("User %s: %s", request.user.username, message)
    messages.success(request, message)


def _galleries_with_archive_details(results_page) -> None:
    """Give each gallery of the page its archives, with how each one compares to the gallery."""
    results_page.object_list = list(results_page.object_list)
    for gallery in results_page.object_list:
        archives = list(gallery.archive_set.all())
        for archive in archives:
            archive.same_size = bool(gallery.filesize) and archive.filesize == gallery.filesize
            archive.same_count = bool(gallery.filecount) and archive.filecount == gallery.filecount
            archive.size_diff = (archive.filesize or 0) - (gallery.filesize or 0)
        gallery.archive_list = archives


@staff_member_required(login_url="viewer:login")
def repeated_archives_for_galleries(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = GallerySearchForm()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})

    if p:
        _delete_selected_archives(request, delete_files="delete_archives_and_files" in p)
        # Post/redirect/get: reloading the page must not repeat the action.
        return HttpResponseRedirect(request.get_full_path())

    params = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    results = results.several_archives()

    results = results.distinct()

    limit = page_size(get, default=50)
    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    _galleries_with_archive_details(results_page)

    providers = list(
        Gallery.objects.filter(pk__in=Gallery.objects.several_archives().order_by().values("pk"))
        .order_by()
        .values("provider")
        .annotate(count=Count("id", distinct=True))
        .order_by("-count", "provider")
    )

    d = {
        "results": results_page,
        "form": form,
        "providers": providers,
        "total": sum(row["count"] for row in providers),
        "limit": limit,
        "per_page_options": PAGE_SIZE_OPTIONS,
    }
    return render(request, "viewer/archives_repeated.html", d)


@staff_member_required(login_url="viewer:login")
def repeated_galleries_by_field(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    providers =  get.getlist("providers")
    categories =  get.getlist("categories")

    if "clear" in get:
        form = GallerySearchForm()
        fields_form = GallerySearchFields()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})
        fields_form = GallerySearchFields(initial={"providers": providers, "categories": categories})

    if p:
        user_reason = p.get("reason", "")
        if "delete_galleries" in p:
            pks = []
            for k in p:
                if k.startswith("sel-"):
                    v_keys = [int(x) for x in p.getlist(k)]
                    pks.extend(v_keys)
            gallery_pks = list(Gallery.objects.filter(id__in=pks).order_by("-create_date").values_list("pk", flat=True))
            if gallery_pks:
                client.enqueue(
                    args=gallery_pks,
                    task_type="gallery_batch_worker",
                    override_options={"operation": "mark_deleted", "event_result": "deleted"},
                    user=request.user,
                    reason=user_reason,
                    event_action="MARK_DELETE_GALLERY",
                )
                message = "Queued marking {} gallery(ies) as deleted".format(len(gallery_pks))
                logger.info("User %s: %s", request.user.username, message)
                messages.success(request, message)
        elif "create_gallery_match_groups" in p:
            groups = []
            pks = []
            order_by_pk = {}
            for k in p:
                if k.startswith("sel-"):
                    v_keys = [int(x) for x in p.getlist(k)]
                    groups.append(v_keys)
                    pks.extend(v_keys)
                elif k.startswith("ord-"):
                    k_value = p.get(k)
                    if k_value is not None:
                        order_by_pk[int(k.replace("ord-", ""))] = int(k_value)

            ordered_groups = [
                sorted(group, key=lambda gid: order_by_pk.get(gid, 0))
                for group in groups
                if group
            ]
            if ordered_groups:
                client.enqueue(
                    args=list(dict.fromkeys(pks)),
                    task_type="gallery_batch_worker",
                    override_options={"operation": "create_match_groups", "groups": ordered_groups},
                    user=request.user,
                    reason=user_reason,
                    event_action="CREATE_GALLERY_MATCH_GROUP",
                )
                message = "Queued creating {} gallery match group(s)".format(len(ordered_groups))
                logger.info("User %s: %s", request.user.username, message)
                messages.success(request, message)


    params: dict[str, list[str] | str] = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k in get:
        if k in gallery_list_filters_keys:
            v: list[str] | str = get.getlist(k)
        else:
            v = get[k]
        if isinstance(v, str) or isinstance(v, list):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    results = results.eligible_for_use().exclude(title__exact="")

    if "has-archives" in get:
        results = results.annotate(archives=Count("archive")).filter(archives__gt=0)

    if "has-size" in get:
        results = results.filter(filesize__gt=0)

    by_title = {}
    by_filesize = {}

    if "by-title" in get:
        if "same-uploader" in get:
            for k_tu, v_tu in groupby(results.order_by("title", "uploader"), lambda x: (x.title, x.uploader)):
                objects = list(v_tu)
                if len(objects) > 1:
                    by_title[str(k_tu)] = objects
        else:
            for k_title, v_title in groupby(results.order_by("title"), lambda x: x.title or ""):
                objects = list(v_title)
                if len(objects) > 1:
                    by_title[k_title] = objects

    if "by-filesize" in get:
        for k_filesize, v_filesize in groupby(results.order_by("filesize"), lambda x: str(x.filesize or "")):
            objects = list(v_filesize)
            if len(objects) > 1:
                by_filesize[k_filesize] = objects

    try:
        inline_thumbnails = bool(get.get("inline-thumbnails", ""))
    except ValueError:
        inline_thumbnails = False

    d = {
        "by_title": by_title,
        "by_filesize": by_filesize,
        "form": form,
        "fields_form": fields_form,
        "inline_thumbnails": inline_thumbnails,
        "has_grouping": "by-title" in get or "by-filesize" in get,
        "total_groups": len(by_title) + len(by_filesize),
    }

    return render(request, "viewer/galleries_repeated_by_fields.html", d)


@staff_member_required(login_url="viewer:login")
def archive_filesize_different_from_gallery(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = GallerySearchForm()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})
    if p:
        if "delete_archives" in p or "delete_archives_and_files" in p:
            _delete_selected_archives(request, delete_files="delete_archives_and_files" in p)
        # Post/redirect/get: reloading the page must not repeat the action.
        return HttpResponseRedirect(request.get_full_path())

    params = {}

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    # A gallery with several archives of a different size would be listed once per archive.
    results = results.different_filesize_archive().distinct()

    limit = page_size(get, default=50)
    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    _galleries_with_archive_details(results_page)

    providers = list(
        Gallery.objects.different_filesize_archive()
        .order_by()
        .values("provider")
        .annotate(count=Count("id", distinct=True))
        .order_by("-count", "provider")
    )

    d = {
        "results": results_page,
        "providers": providers,
        "total": sum(row["count"] for row in providers),
        "form": form,
        "limit": limit,
        "per_page_options": PAGE_SIZE_OPTIONS,
    }
    return render(request, "viewer/archives_different_filesize.html", d)


def public_missing_archives_for_galleries(request: HttpRequest) -> HttpResponse:
    results = Gallery.objects.report_as_missing_galleries(public=True, provider__in=["panda", "fakku"])
    d = {"results": results}
    return render(request, "viewer/archives_missing_for_galleries.html", d)


@staff_member_required(login_url="viewer:login")
def archives_not_matched_with_gallery(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    limit = page_size(get, default=100)
    inline_thumbnails = bool(get.get("inline-thumbnails", ""))

    if "clear" in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-") and isinstance(v, str):
                pks.append(v)

        if not pks:
            messages.warning(request, "No archives were selected.")
            return HttpResponseRedirect(request.get_full_path())

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        try:
            cutoff = clamp(float(p.get("cutoff", "0.4") or "0.4"), 0.0, 1.0)
        except ValueError:
            cutoff = 0.4
        try:
            max_matches = int(p.get("max-matches", "10") or "10")
        except ValueError:
            max_matches = 10
        if "delete_archives" in p or "delete_objects" in p:
            delete_files = "delete_archives" in p
            archive_pks = list(archives.values_list("pk", flat=True))
            if archive_pks:
                client.enqueue(
                    args=archive_pks,
                    task_type="delete_archive_worker",
                    override_options={
                        "delete_zipped": False,
                        "delete_archive": True,
                        "delete_file": delete_files,
                        "delete_gallery": False,
                        "mark_gallery_deleted": False,
                        "mark_gallery_denied": False,
                    },
                    user=request.user,
                    event_action="DELETE_ARCHIVE",
                )
                message = "Queued deleting {} unmatched archive(s) ({})".format(
                    len(archive_pks), "deleting files" if delete_files else "keeping files"
                )
                logger.info("User %s: %s", request.user.username, message)
                messages.success(request, message)
        elif "clear_possible_matches" in p:
            ArchiveMatches.objects.filter(archive__in=archives).delete()
            logger.info("User {}: Clearing possible matches for archives".format(request.user.username))
            messages.success(request, "Cleared the possible matches of {} archive(s).".format(len(pks)))
        elif "create_possible_matches" in p:
            if thread_exists_in_db("web_match_worker"):
                messages.error(request, "The web match worker is already running, wait for it to finish.", extra_tags="danger")
                return HttpResponseRedirect(request.get_full_path())

            matcher_filter = p["create_possible_matches"]

            client.enqueue_matching_task(
                task_type="web_match_worker",
                args=archives,
                matching_kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "filters": (matcher_filter,),
                    "match_local": False,
                    "match_web": True,
                    "auto_link": "auto_link" in p,
                },
                user=request.user,
            )
            messages.success(request, "Starting web match worker.")
        elif "create_possible_matches_internal" in p:
            if thread_exists_in_db("match_unmatched_worker"):
                messages.error(request, "The local matching worker is already running, wait for it to finish.", extra_tags="danger")
                return HttpResponseRedirect(request.get_full_path())
            provider = p["create_possible_matches_internal"]

            logger.info(
                "Looking for possible matches in gallery database "
                "for non-matched archives (cutoff: {}, max matches: {}) "
                'using provider filter "{}"'.format(cutoff, max_matches, provider)
            )
            client.enqueue_matching_task(
                task_type="match_unmatched_worker",
                args=archives,
                matching_kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "filters": (provider,),
                    "match_local": True,
                    "match_web": False,
                    "auto_link": "auto_link" in p,
                },
                user=request.user,
            )
            messages.success(request, "Starting internal match worker.")

        # Post/redirect/get: reloading the page must not repeat the action.
        return HttpResponseRedirect(request.get_full_path())

    params = {
        "sort": "create_date",
        "asc_desc": "desc",
    }

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in archive_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_archives_simple(params, True)

    results = results.filter(gallery__isnull=True).prefetch_related(  # type:ignore[misc]
        Prefetch(
            "archivematches_set",
            queryset=ArchiveMatches.objects.select_related("gallery", "archive")
            .annotate(gallery_archive_count=Count("gallery__archive"))
            .prefetch_related(
                Prefetch("gallery__tags", queryset=Tag.objects.filter(scope__exact="artist"), to_attr="artist_tags"),
                Prefetch(
                    "gallery__tags", queryset=Tag.objects.filter(scope__exact="magazine"), to_attr="magazine_tags"
                ),
            ),
            to_attr="possible_galleries",
        ),
        "possible_galleries__gallery",
    )

    if "no-custom-tags" in get:
        results = results.annotate(
            num_custom_tags=Count("tags", filter=Q(archivetag__origin=ArchiveTag.ORIGIN_USER))
        ).filter(num_custom_tags=0)
    if "with-possible-matches" in get:
        results = results.filter(Exists(ArchiveMatches.objects.filter(archive_id=OuterRef("pk"))))
    if "without-possible-matches" in get:
        results = results.filter(~Exists(ArchiveMatches.objects.filter(archive_id=OuterRef("pk"))))

    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    # Group each archive's possible matches by gallery, best candidate first.
    results_page.object_list = list(results_page.object_list)
    for archive in results_page.object_list:
        groups: dict[int, dict] = {}
        for match in getattr(archive, "possible_galleries", []):
            group = groups.setdefault(
                match.gallery_id,
                {"gallery": match.gallery, "matches": [], "best": 0.0, "archive_count": match.gallery_archive_count},
            )
            group["matches"].append(match)
            group["best"] = max(group["best"], match.match_accuracy or 0.0)
        candidates = sorted(groups.values(), key=lambda g: g["best"], reverse=True)
        setattr(archive, "candidates", candidates)
        setattr(archive, "best_accuracy", candidates[0]["best"] if candidates else None)

    unmatched = Archive.objects.filter(gallery__isnull=True)
    stats = {
        "unmatched": unmatched.count(),
        "with_possible": unmatched.filter(Exists(ArchiveMatches.objects.filter(archive_id=OuterRef("pk")))).count(),
        "local_running": thread_exists_in_db("match_unmatched_worker"),
        "web_running": thread_exists_in_db("web_match_worker"),
    }
    stats["without_possible"] = stats["unmatched"] - stats["with_possible"]

    d = {
        "results": results_page,
        "providers": Gallery.objects.order_by("provider").values_list("provider", flat=True).distinct(),
        "matchers": crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
        "form": form,
        "inline_thumbnails": inline_thumbnails,
        "stats": stats,
        "limit": limit,
        "per_page_options": PAGE_SIZE_OPTIONS,
    }
    return render(request, "viewer/archives_not_matched.html", d)


def wanted_galleries(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    is_staff = request.user.is_staff
    has_view_perm = request.user.has_perm("viewer.view_wantedgallery")
    has_add_perm = request.user.has_perm("viewer.add_wantedgallery")
    is_collaborator_or_staff = is_staff or has_view_perm
    can_add = is_staff or has_add_perm

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = WantedGallerySearchForm()
    else:
        form = WantedGallerySearchForm(initial={"title": title, "tags": tags})

    # Add wanted gallery for collaborators or staff
    edit_form = None
    if can_add:
        if p.get("submit-wanted-gallery"):
            edit_form = WantedGalleryCreateOrEditForm(request.POST)
            if edit_form.is_valid():
                new_wanted_gallery = edit_form.save()
                message = "New wanted gallery successfully created"
                messages.success(request, message)
                logger.info("User {}: {}".format(request.user.username, message))
                event_log(request.user, "ADD_WANTED_GALLERY", content_object=new_wanted_gallery, result="created")
                return HttpResponseRedirect(request.get_full_path())
            else:
                messages.error(request, "The provided data is not valid", extra_tags="danger")
        else:
            edit_form = WantedGalleryCreateOrEditForm()

    if p and is_staff:
        if "delete_galleries" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-") and isinstance(v, str):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = WantedGallery.objects.filter(id__in=pks).reverse()

            for wanted_gallery in results:
                message = "Removing wanted gallery: {}".format(wanted_gallery.title)
                logger.info(message)
                messages.success(request, message)
                wanted_gallery.delete()
        elif "search_for_galleries" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-") and isinstance(v, str):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = WantedGallery.objects.filter(id__in=pks).reverse()
            results.update(should_search=True)

            for wanted_gallery in results:
                message = "Marking gallery as to search for: {}".format(wanted_gallery.title)
                logger.info(message)
                messages.success(request, message)
        elif "toggle-public" in p or "set-public" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-") and isinstance(v, str):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = WantedGallery.objects.filter(id__in=pks).reverse()
            results.update(public=True)

            for wanted_gallery in results:
                message = "Marking gallery as public: {}".format(wanted_gallery.title)
                logger.info(message)
                messages.success(request, message)
        elif "search_provider_galleries" in p:
            if thread_exists_in_db("web_search_worker"):
                messages.error(request, "Web search worker is already running.", extra_tags="danger")
                return safe_redirect_back(request, request.get_full_path())
            pks = []
            for k, v in p.items():
                if k.startswith("sel-") and isinstance(v, str):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = WantedGallery.objects.filter(id__in=pks).reverse()

            provider = p.get("provider", "")

            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            message = "Searching for gallery matches in providers for wanted galleries."
            logger.info(message)
            messages.success(request, message)

            client.enqueue_matching_task(
                task_type="web_search_worker",
                args=results,
                provider=provider,
                matching_kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                },
                user=request.user,
            )
        elif "search_provider_galleries_internal" in p:
            if thread_exists_in_db("wanted_local_search_worker"):
                messages.error(request, "Wanted local matching worker is already running.", extra_tags="danger")
                return safe_redirect_back(request, request.get_full_path())
            pks = []
            for k, v in p.items():
                if k.startswith("sel-") and isinstance(v, str):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = WantedGallery.objects.filter(id__in=pks).reverse()

            provider = p.get("provider", "")

            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            try:
                must_be_used = bool(p.get("must-be-used", False))
            except ValueError:
                must_be_used = False

            message = "Searching for gallery matches locally in providers for wanted galleries."
            logger.info(message)
            messages.success(request, message)

            client.enqueue_matching_task(
                task_type="wanted_local_search_worker",
                args=results,
                matching_kwargs={
                    "provider_filter": provider,
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "must_be_used": must_be_used,
                },
                user=request.user,
            )
        elif "clear_all_matches" in p:
            GalleryMatch.objects.all().delete()
            message = "Clearing matches from every wanted gallery."
            logger.info(message)
            messages.success(request, message)

        if not p.get("submit-wanted-gallery"):
            return HttpResponseRedirect(request.get_full_path())

    if is_collaborator_or_staff:
        params = {}

        for k, v in get.items():
            params[k] = v

        for k in wanted_gallery_filter_keys:
            if k not in params:
                params[k] = ""

        results_filtered = filter_wanted_galleries_simple(params)
    else:
        # The public list only shows what is still being looked for, and only searches by title:
        # the management filters (search state, found, tags, providers...) aren't exposed here.
        results_filtered = WantedGallery.objects.filter(public=True, should_search=True, found=False).order_by(
            F("release_date").desc(nulls_last=True), "-pk"
        )
        if title:
            q_formatted = "%" + title.replace(" ", "%") + "%"
            results_filtered = results_filtered.filter(Q(title__ss=q_formatted) | Q(title_jpn__ss=q_formatted))

    if is_collaborator_or_staff:
        results_filtered = results_filtered.prefetch_related(  # type:ignore[misc]
            Prefetch(
                "gallerymatch_set",
                queryset=GalleryMatch.objects.select_related("gallery", "wanted_gallery").prefetch_related(
                    Prefetch("gallery__tags", queryset=Tag.objects.filter(scope__exact="artist"), to_attr="artist_tags")
                ),
                to_attr="possible_galleries",
            ),
            "possible_galleries__gallery__archive_set",
            "artists",
            "mentions",
        )
    else:
        results_filtered = results_filtered.prefetch_related(
            "artists",
            "mentions",
        )

    limit = page_size(get, default=100)
    paginator = Paginator(results_filtered, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    # The summary tiles are only shown on the management version of the page.
    stats = None
    if is_collaborator_or_staff:
        base_wanted = WantedGallery.objects.all()
        raw_stats = base_wanted.aggregate(
            total_count=Count("id"),
            searching_count=Count("id", filter=Q(should_search=True, found=False)),
            found_count=Count("id", filter=Q(found=True)),
        )
        stats = {
            "total": raw_stats["total_count"],
            "searching": raw_stats["searching_count"],
            "found": raw_stats["found_count"],
            "with_matches": base_wanted.filter(possible_matches__isnull=False).distinct().count(),
        }

    d = {
        "results": results_page,
        "form": form,
        "edit_form": edit_form,
        "can_add": can_add,
        "can_view_internal": is_collaborator_or_staff,
        "stats": stats,
        "limit": limit,
        "per_page_options": PAGE_SIZE_OPTIONS,
    }
    return render(request, "viewer/wanted_galleries.html", d)


FOUND_GALLERY_PERIODS = {"day": 1, "week": 7, "month": 30}


def found_galleries(request: HttpRequest) -> HttpResponse:

    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    can_view_internal = request.user.is_staff or request.user.has_perm("viewer.view_wantedgallery")

    form = None
    if not request.user.is_authenticated:
        base_results = FoundGallery.objects.filter(wanted_gallery__public=True, gallery__public=True)
    else:
        if "clear" in get:
            form = WantedGallerySearchForm()
        else:
            form = WantedGallerySearchForm(initial={"title": title, "tags": tags})

        params = {}

        for k, v in get.items():
            params[k] = v

        for k in wanted_gallery_filter_keys:
            if k not in params:
                params[k] = ""

        wanted_galleries_results = filter_wanted_galleries_simple(params)

        base_results = FoundGallery.objects.filter(wanted_gallery__in=wanted_galleries_results)

    # Tiles and source chips count over the records matching the wanted gallery filters.
    now = django_tz.now()
    stats = base_results.aggregate(
        total=Count("id"),
        day=Count("id", filter=Q(create_date__gte=now - datetime.timedelta(days=1))),
        week=Count("id", filter=Q(create_date__gte=now - datetime.timedelta(days=7))),
        month=Count("id", filter=Q(create_date__gte=now - datetime.timedelta(days=30))),
        wanted=Count("wanted_gallery", distinct=True),
    )
    sources = list(
        base_results.order_by().values("source").annotate(count=Count("id")).order_by("-count", "source")[:12]
    )

    results = base_results
    period = get.get("period", "")
    if period in FOUND_GALLERY_PERIODS:
        results = results.filter(create_date__gte=now - datetime.timedelta(days=FOUND_GALLERY_PERIODS[period]))
    else:
        period = ""
    source = get.get("source")
    if source is not None and source != "":
        results = results.filter(Q(source__isnull=True) | Q(source="")) if source == "-" else results.filter(source=source)

    results = results.select_related("wanted_gallery", "gallery").order_by("-create_date")

    limit = page_size(get, default=100)
    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        "results": results_page,
        "form": form,
        "stats": stats,
        "sources": sources,
        "period": period,
        "can_view_internal": can_view_internal,
        "limit": limit,
        "per_page_options": PAGE_SIZE_OPTIONS,
    }
    return render(request, "viewer/found_galleries.html", d)
