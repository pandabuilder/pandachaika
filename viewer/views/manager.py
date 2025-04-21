import logging
import threading
from itertools import groupby

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.db.models import Prefetch, Count, Q, Case, When
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.conf import settings

from core.base.utilities import thread_exists, clamp
from viewer.forms import GallerySearchForm, ArchiveSearchForm, WantedGallerySearchForm
from viewer.models import Archive, Gallery, ArchiveMatches, Tag, WantedGallery, GalleryMatch, FoundGallery, ArchiveTag
from viewer.utils.actions import event_log
from viewer.utils.matching import (
    generate_possible_matches_for_archives,
    create_matches_wanted_galleries_from_providers,
    create_matches_wanted_galleries_from_providers_internal,
)
from viewer.views.head import (
    render_error,
    gallery_filter_keys,
    filter_galleries_simple,
    archive_filter_keys,
    filter_archives_simple,
    wanted_gallery_filter_keys,
    filter_wanted_galleries_simple,
)

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger(__name__)


@staff_member_required(login_url="viewer:login")  # type: ignore
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
        pks: list[str] = []
        for k, v in p.items():
            if k.startswith("del-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.extend(p.getlist(k))
        results_archive = Archive.objects.filter(id__in=pks).order_by("-pk")

        for archive in results_archive:
            if "delete_archives_and_files" in p:
                message = "Removing archive and deleting file: {}".format(archive.zipped.name)
                logger.info(message)
                messages.success(request, message)
                archive.delete_all_files()
            else:
                message = "Removing archive: {}".format(archive.zipped.name)
                logger.info(message)
                messages.success(request, message)
            archive.delete()

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

    results = results.several_archives()  # type: ignore

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "form": form}
    return render(request, "viewer/archives_repeated.html", d)


@staff_member_required(login_url="viewer:login")  # type: ignore
def repeated_galleries_by_field(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    if "clear" in get:
        form = GallerySearchForm()
    else:
        form = GallerySearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("del-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)
        results_gallery = Gallery.objects.filter(id__in=pks).order_by("-create_date")

        if "delete_galleries" in p:

            user_reason = p.get("reason", "")

            for gallery in results_gallery:
                message = "Removing gallery: {}, link: {}".format(gallery.title, gallery.get_link())
                logger.info(message)
                messages.success(request, message)
                gallery.mark_as_deleted()

                event_log(
                    request.user, "MARK_DELETE_GALLERY", reason=user_reason, content_object=gallery, result="deleted"
                )

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

    results = results.eligible_for_use().exclude(title__exact="")  # type: ignore

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

    providers = Gallery.objects.all().values_list("provider", flat=True).distinct()

    d = {"by_title": by_title, "by_filesize": by_filesize, "form": form, "providers": providers}

    return render(request, "viewer/galleries_repeated_by_fields.html", d)


@staff_member_required(login_url="viewer:login")  # type: ignore
def archive_filesize_different_from_gallery(request: HttpRequest) -> HttpResponse:
    providers = Gallery.objects.all().values_list("provider", flat=True).distinct()
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
        pks: list[str] = []
        for k, v in p.items():
            if k.startswith("del-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.extend(p.getlist(k))
        results_archive = Archive.objects.filter(id__in=pks).order_by("-pk")

        for archive in results_archive:
            if "delete_archives" in p:
                message = "Removing archive: {} and keeping its file: {}".format(archive.title, archive.zipped.name)
                logger.info(message)
                messages.success(request, message)
                archive.delete()
            if "delete_archives_and_files" in p:
                message = "Removing archive: {} and deleting its file: {}".format(archive.title, archive.zipped.name)
                logger.info(message)
                messages.success(request, message)
                archive.delete_all_files()
                archive.delete()

    params = {}

    for k, v in get.items():
        if isinstance(v, str):
            params[k] = v

    for k in gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results = filter_galleries_simple(params)

    results = results.different_filesize_archive()  # type: ignore

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "providers": providers, "form": form}
    return render(request, "viewer/archives_different_filesize.html", d)


def public_missing_archives_for_galleries(request: HttpRequest) -> HttpResponse:
    results = Gallery.objects.report_as_missing_galleries(public=True, provider__in=["panda", "fakku"])  # type: ignore
    d = {"results": results}
    return render(request, "viewer/archives_missing_for_galleries.html", d)


@staff_member_required(login_url="viewer:login")  # type: ignore
def archives_not_present_in_filesystem(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if "clear" in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={"title": title, "tags": tags})

    if p:
        if "delete_archives" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results_archive = Archive.objects.filter(id__in=pks).order_by("-pk")

            for archive in results_archive:
                message = "Removing archive missing in filesystem: {}, path: {}".format(archive.title, archive.zipped.path)
                logger.info(message)
                messages.success(request, message)
                archive.delete()
        elif "mark_deleted" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("del-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results_archive = Archive.objects.filter(id__in=pks).order_by("-pk")

            message = "Marking Archives as deleted: {}".format(len(pks))

            logger.info(message)
            messages.success(request, message)

            results_archive.update(file_deleted=True)

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

    results = filter_archives_simple(params, authenticated=True, show_binned=True)

    results = results.filter_non_existent(crawler_settings.MEDIA_ROOT)  # type: ignore

    paginator = Paginator(results, 50)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "form": form}
    return render(request, "viewer/archives_not_present.html", d)


@staff_member_required(login_url="viewer:login")  # type: ignore
def archives_not_matched_with_gallery(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    try:
        limit = max(1, int(get.get("limit", "100")))
    except ValueError:
        limit = 100

    try:
        inline_thumbnails = bool(get.get("inline-thumbnails", ""))
    except ValueError:
        inline_thumbnails = False

    if "clear" in get:
        form = ArchiveSearchForm()
    else:
        form = ArchiveSearchForm(initial={"title": title, "tags": tags})

    if p:
        pks = []
        for k, v in p.items():
            if k.startswith("sel-"):
                # k, pk = k.split('-')
                # results[pk][k] = v
                pks.append(v)

        preserved = Case(*[When(pk=pk, then=pos) for pos, pk in enumerate(pks)])

        archives = Archive.objects.filter(id__in=pks).order_by(preserved)
        if "delete_archives" in p:
            for archive in archives:
                message = "Removing archive not matched: {} and deleting file: {}".format(
                    archive.title, archive.zipped.path
                )
                logger.info(message)
                messages.success(request, message)
                archive.delete_all_files()
                archive.delete()
        elif "delete_objects" in p:
            for archive in archives:
                message = "Removing archive not matched: {}, keeping file: {}".format(
                    archive.title, archive.zipped.path
                )
                logger.info(message)
                messages.success(request, message)
                archive.delete_files_but_archive()
                archive.delete()
        elif "create_possible_matches" in p:
            if thread_exists("web_match_worker"):
                return render_error(request, "Web match worker is already running.")

            matcher_filter = p["create_possible_matches"]
            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            web_match_thread = threading.Thread(
                name="web_match_worker",
                target=generate_possible_matches_for_archives,
                args=(archives,),
                kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "filters": (matcher_filter,),
                    "match_local": False,
                    "match_web": True,
                },
            )
            web_match_thread.daemon = True
            web_match_thread.start()
            messages.success(request, "Starting web match worker.")
        elif "create_possible_matches_internal" in p:
            if thread_exists("match_unmatched_worker"):
                return render_error(request, "Local matching worker is already running.")
            provider = p["create_possible_matches_internal"]
            try:
                cutoff = clamp(float(p.get("cutoff", "0.4")), 0.0, 1.0)
            except ValueError:
                cutoff = 0.4
            try:
                max_matches = int(p.get("max-matches", "10"))
            except ValueError:
                max_matches = 10

            logger.info(
                "Looking for possible matches in gallery database "
                "for non-matched archives (cutoff: {}, max matches: {}) "
                'using provider filter "{}"'.format(cutoff, max_matches, provider)
            )
            matching_thread = threading.Thread(
                name="match_unmatched_worker",
                target=generate_possible_matches_for_archives,
                args=(archives,),
                kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "filters": (provider,),
                    "match_local": True,
                    "match_web": False,
                },
            )
            matching_thread.daemon = True
            matching_thread.start()
            messages.success(request, "Starting internal match worker.")

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

    results = results.filter(gallery__isnull=True).prefetch_related(
        Prefetch(
            "archivematches_set",
            queryset=ArchiveMatches.objects.select_related("gallery", "archive").prefetch_related(
                Prefetch("gallery__tags", queryset=Tag.objects.filter(scope__exact="artist"), to_attr="artist_tags"),
                Prefetch(
                    "gallery__tags", queryset=Tag.objects.filter(scope__exact="magazine"), to_attr="magazine_tags"
                ),
                "gallery__archive_set",
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
        results = results.annotate(n_possible_matches=Count("possible_matches")).filter(n_possible_matches__gt=0)

    paginator = Paginator(results, limit)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {
        "results": results_page,
        "providers": Gallery.objects.all().values_list("provider", flat=True).distinct(),
        "matchers": crawler_settings.provider_context.get_matchers(crawler_settings, force=True),
        "form": form,
        "inline_thumbnails": inline_thumbnails,
    }
    return render(request, "viewer/archives_not_matched.html", d)


def wanted_galleries(request: HttpRequest) -> HttpResponse:
    p = request.POST
    get = request.GET

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

    if not request.user.is_staff:
        results = (
            WantedGallery.objects.filter(Q(should_search=True) & Q(found=False) & Q(public=True))
            .prefetch_related("artists", "mentions")
            .order_by("-release_date")
        )
        return render(request, "viewer/wanted_galleries.html", {"results": results})

    if p and request.user.is_staff:
        if "delete_galleries" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-"):
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
                if k.startswith("sel-"):
                    # k, pk = k.split('-')
                    # results[pk][k] = v
                    pks.append(v)
            results = WantedGallery.objects.filter(id__in=pks).reverse()
            results.update(should_search=True)

            for wanted_gallery in results:
                message = "Marking gallery as to search for: {}".format(wanted_gallery.title)
                logger.info(message)
                messages.success(request, message)
        elif "toggle-public" in p:
            pks = []
            for k, v in p.items():
                if k.startswith("sel-"):
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
            if thread_exists("web_search_worker"):
                messages.error(request, "Web search worker is already running.", extra_tags="danger")
                return HttpResponseRedirect(request.META["HTTP_REFERER"])
            pks = []
            for k, v in p.items():
                if k.startswith("sel-"):
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

            panda_search_thread = threading.Thread(
                name="web_search_worker",
                target=create_matches_wanted_galleries_from_providers,
                args=(results, provider),
                kwargs={
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                },
            )
            panda_search_thread.daemon = True
            panda_search_thread.start()
        elif "search_provider_galleries_internal" in p:
            if thread_exists("wanted_local_search_worker"):
                messages.error(request, "Wanted local matching worker is already running.", extra_tags="danger")
                return HttpResponseRedirect(request.META["HTTP_REFERER"])
            pks = []
            for k, v in p.items():
                if k.startswith("sel-"):
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

            matching_thread = threading.Thread(
                name="web_search_worker",
                target=create_matches_wanted_galleries_from_providers_internal,
                args=(results,),
                kwargs={
                    "provider_filter": provider,
                    "cutoff": cutoff,
                    "max_matches": max_matches,
                    "must_be_used": must_be_used,
                },
            )
            matching_thread.daemon = True
            matching_thread.start()
        elif "clear_all_matches" in p:
            GalleryMatch.objects.all().delete()
            message = "Clearing matches from every wanted gallery."
            logger.info(message)
            messages.success(request, message)

    params = {}

    for k, v in get.items():
        params[k] = v

    for k in wanted_gallery_filter_keys:
        if k not in params:
            params[k] = ""

    results_filtered = filter_wanted_galleries_simple(params)

    results_filtered = results_filtered.prefetch_related(
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

    paginator = Paginator(results_filtered, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "form": form}
    return render(request, "viewer/wanted_galleries.html", d)


def found_galleries(request: HttpRequest) -> HttpResponse:

    get = request.GET

    title = get.get("title", "")
    tags = get.get("tags", "")

    try:
        page = int(get.get("page", "1"))
    except ValueError:
        page = 1

    if not request.user.is_authenticated:
        results = (
            FoundGallery.objects.filter(wanted_gallery__public=True, gallery__public=True)
            .prefetch_related("wanted_gallery", "gallery")
            .order_by("-create_date")
        )

        paginator = Paginator(results, 100)
        try:
            results_page = paginator.page(page)
        except (InvalidPage, EmptyPage):
            results_page = paginator.page(paginator.num_pages)

        return render(request, "viewer/found_galleries.html", {"results": results_page})

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

    results = (
        FoundGallery.objects.filter(wanted_gallery__in=wanted_galleries_results)
        .prefetch_related("wanted_gallery", "gallery")
        .order_by("-create_date")
    )

    paginator = Paginator(results, 100)
    try:
        results_page = paginator.page(page)
    except (InvalidPage, EmptyPage):
        results_page = paginator.page(paginator.num_pages)

    d = {"results": results_page, "form": form}
    return render(request, "viewer/found_galleries.html", d)
